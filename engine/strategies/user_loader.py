"""Load an uploaded strategy class from a strategy-store key.

A built-in strategy is a package inside this repository, so the runner reaches
it with ``importlib.import_module``. An upload is a pair of objects in the
strategy store, so it has to be brought to disk before anything can import it:
this module materializes the key into a directory, imports ``strategy.py`` from
that path, and returns the ``BasePortfolio`` subclass it defines.

Two details are load-bearing rather than incidental:

* **The whole key is materialized, not just the source.** ``BasePortfolio``
  finds its ``config.json`` by looking beside the file its class was defined in
  (``inspect.getfile``), so ``strategy.py`` and ``config.json`` must land in the
  same directory — the same shape as ``engine/strategies/portfolio_*``. That is
  why the store's ``materialize`` copies the whole prefix.
* **The imported module is registered in ``sys.modules``.** The engine's
  ``load_strategy_class`` resolves a ``class_path`` with
  ``importlib.import_module``, which returns an already-registered module
  without touching the filesystem. Registering under a per-run synthetic name
  therefore lets an uploaded strategy travel through the unmodified run
  pipeline as a perfectly ordinary ``"module:ClassName"`` path.

This module imports nothing from ``src`` — the engine has to stay runnable on
its own — so the store arrives as an injected object satisfying
:class:`StrategyMaterializer`.

SECURITY, PLAINLY: importing a module executes every statement at its top
level. By the time :func:`load_user_strategy` returns, arbitrary user-supplied
Python has already run in this process, with whatever credentials and network
access the process has. Nothing in this file is a sandbox and nothing in it
tries to be one. The upload-time source scan is a speed bump for accidents, not
a boundary against a determined author. Real isolation — a container, no
network egress, a database role scoped to ``public.market_data`` — is required
before this pipeline is exposed to anyone outside the club.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from engine.strategies.portfolio_BASE.strategy import BasePortfolio

# The two objects that make up an uploaded strategy. Both names match the
# built-in strategy folders exactly, because the engine's config discovery is
# what reads the second one.
SOURCE_FILENAME = "strategy.py"
CONFIG_FILENAME = "config.json"

# Synthetic module names are prefixed so a stack trace from inside user code is
# recognisable as user code, and are never dotted: a dotted name would make
# Python try to import a parent package that does not exist.
MODULE_PREFIX = "mqs_user_strategy_"


@runtime_checkable
class StrategyMaterializer(Protocol):
    """The one thing this loader needs from the strategy store."""

    def materialize(self, key: str, dest_dir: Path) -> Path:
        """Write every object under ``key`` into ``dest_dir`` and return it."""
        ...


class UserStrategyError(Exception):
    """An upload that cannot be loaded, with a sentence explaining why.

    A distinct type because the message reaches a student through the run
    row's ``error_message``: "your file defines two strategies" is actionable,
    while an ``AttributeError`` from deep inside importlib is not.
    """


@dataclass(frozen=True)
class LoadedUserStrategy:
    """What the worker needs after an upload has been brought to disk."""

    # Spelled for ``engine.run_single.load_strategy_class`` — the module half
    # resolves out of ``sys.modules``, so no file path travels with it.
    class_path: str
    strategy_class: type[BasePortfolio]
    directory: Path


def module_name_for(token: str) -> str:
    """A module name unique to one run.

    Per run rather than per strategy: a student fixes a bug and re-uploads, and
    a module name reused across runs in a long-lived worker process would serve
    the first version of the code forever.
    """
    safe = "".join(char if char.isalnum() else "_" for char in str(token))
    return f"{MODULE_PREFIX}{safe}"


def load_user_strategy(
    *,
    storage_key: str,
    store: StrategyMaterializer,
    dest_dir: Path | str,
    token: str,
) -> LoadedUserStrategy:
    """Materialize an upload and import the strategy class it defines.

    ``token`` distinguishes this load from every other one in the process; the
    worker passes the run id. Raises :class:`UserStrategyError` for anything an
    author can fix, which is most of what can go wrong here.
    """
    directory = _materialize(storage_key, store, Path(dest_dir))
    source_path = directory / SOURCE_FILENAME

    if not source_path.is_file():
        raise UserStrategyError(
            f"the stored strategy at {storage_key!r} has no {SOURCE_FILENAME}"
        )
    if not (directory / CONFIG_FILENAME).is_file():
        # Failing here rather than letting the engine hit its own missing-config
        # error, because at this point the key is the thing that is wrong and
        # naming it is the whole diagnosis.
        raise UserStrategyError(
            f"the stored strategy at {storage_key!r} has no {CONFIG_FILENAME}; "
            "source and config are stored together and must stay together"
        )

    module_name = module_name_for(token)
    module = _import_module(module_name, source_path)
    strategy_class = _sole_strategy_class(module, module_name)

    return LoadedUserStrategy(
        class_path=f"{module_name}:{strategy_class.__name__}",
        strategy_class=strategy_class,
        directory=directory,
    )


def _materialize(
    storage_key: str, store: StrategyMaterializer, dest_dir: Path
) -> Path:
    try:
        return Path(store.materialize(storage_key, dest_dir))
    except KeyError as exc:
        raise UserStrategyError(
            f"there is nothing stored under {storage_key!r}; the upload was "
            "lost or never completed"
        ) from exc


def _import_module(module_name: str, source_path: Path):
    """Import a file as a module and register it under ``module_name``.

    Registered *before* execution, which is what the import system does for
    every module: user code that inspects ``sys.modules[__name__]`` — dataclass
    machinery does, among others — would otherwise fail with a KeyError. The
    registration is undone if execution raises, so a broken upload leaves
    nothing half-imported behind for the next run to find.
    """
    spec = importlib.util.spec_from_file_location(module_name, source_path)
    if spec is None or spec.loader is None:
        raise UserStrategyError(f"{source_path.name} could not be read as Python")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException as exc:
        sys.modules.pop(module_name, None)
        raise UserStrategyError(
            f"{source_path.name} failed while being imported "
            f"({type(exc).__name__}: {exc})"
        ) from exc
    return module


def _sole_strategy_class(module, module_name: str) -> type[BasePortfolio]:
    """The one ``BasePortfolio`` subclass the module defines.

    ``__module__`` is compared so an imported base class — every strategy
    imports ``BasePortfolio``, and some import a sibling to subclass it — is not
    mistaken for the strategy the author wrote.
    """
    candidates = [
        value
        for value in vars(module).values()
        if isinstance(value, type)
        and issubclass(value, BasePortfolio)
        and value is not BasePortfolio
        and value.__module__ == module_name
    ]

    if not candidates:
        raise UserStrategyError(
            f"{SOURCE_FILENAME} defines no BasePortfolio subclass; a strategy is "
            "a class that inherits from BasePortfolio and implements OnData"
        )
    if len(candidates) > 1:
        names = ", ".join(sorted(candidate.__name__ for candidate in candidates))
        raise UserStrategyError(
            f"{SOURCE_FILENAME} defines more than one BasePortfolio subclass "
            f"({names}); it must define exactly one so there is no question "
            "which one to run"
        )
    return candidates[0]
