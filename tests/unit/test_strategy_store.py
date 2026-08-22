"""Unit tests for the S3-shaped strategy store. No database, no network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.integrations.strategy_store import (
    LocalStrategyStore,
    S3StrategyStore,
    StrategyStore,
    build_strategy_store,
    strategy_key,
)

SOURCE = "class MyStrategy(BasePortfolio):\n    def OnData(self, context):\n        pass\n"
CONFIG = json.dumps({"TICKERS": ["AAPL", "MSFT"], "LOOKBACK_DAYS": 30})


@pytest.fixture()
def store(tmp_path: Path) -> LocalStrategyStore:
    return LocalStrategyStore(tmp_path / "store")


def test_key_helper_builds_prefixed_directory_key() -> None:
    assert strategy_key("my-strat-a1b2") == "strategies/my-strat-a1b2/"


def test_local_store_satisfies_the_protocol(store: LocalStrategyStore) -> None:
    assert isinstance(store, StrategyStore)


def test_put_then_get_round_trips_exact_content(store: LocalStrategyStore) -> None:
    key = strategy_key("round-trip")
    store.put(key, "strategy.py", SOURCE)

    assert store.get(key, "strategy.py") == SOURCE


def test_put_overwrites_like_an_object_write(store: LocalStrategyStore) -> None:
    key = strategy_key("overwrite")
    store.put(key, "strategy.py", SOURCE)
    store.put(key, "strategy.py", "# replaced\n")

    assert store.get(key, "strategy.py") == "# replaced\n"


def test_get_missing_object_raises_key_error(store: LocalStrategyStore) -> None:
    key = strategy_key("absent")
    with pytest.raises(KeyError):
        store.get(key, "strategy.py")

    store.put(key, "strategy.py", SOURCE)
    with pytest.raises(KeyError):
        store.get(key, "config.json")


def test_exists_tracks_the_key_lifecycle(store: LocalStrategyStore) -> None:
    key = strategy_key("lifecycle")
    assert store.exists(key) is False

    store.put(key, "strategy.py", SOURCE)
    assert store.exists(key) is True

    store.delete(key)
    assert store.exists(key) is False


def test_delete_removes_every_object_and_is_idempotent(
    store: LocalStrategyStore,
) -> None:
    key = strategy_key("delete-me")
    store.put(key, "strategy.py", SOURCE)
    store.put(key, "config.json", CONFIG)

    store.delete(key)
    store.delete(key)  # deleting an absent key is a no-op, as in S3

    with pytest.raises(KeyError):
        store.get(key, "strategy.py")


def test_delete_leaves_other_keys_alone(store: LocalStrategyStore) -> None:
    keep, drop = strategy_key("keep"), strategy_key("drop")
    store.put(keep, "strategy.py", SOURCE)
    store.put(drop, "strategy.py", SOURCE)

    store.delete(drop)

    assert store.exists(keep) is True


def test_materialize_reproduces_the_engine_folder_shape(
    store: LocalStrategyStore, tmp_path: Path
) -> None:
    """The engine finds config.json beside strategy.py, so both must land together."""
    key = strategy_key("materialize")
    store.put(key, "strategy.py", SOURCE)
    store.put(key, "config.json", CONFIG)

    dest = store.materialize(key, tmp_path / "run-dir" / "strategy_pkg")

    assert dest == tmp_path / "run-dir" / "strategy_pkg"
    assert (dest / "strategy.py").read_text(encoding="utf-8") == SOURCE
    assert json.loads((dest / "config.json").read_text(encoding="utf-8")) == json.loads(
        CONFIG
    )


def test_materialize_missing_key_raises_key_error(
    store: LocalStrategyStore, tmp_path: Path
) -> None:
    with pytest.raises(KeyError):
        store.materialize(strategy_key("nothing-here"), tmp_path / "dest")


def test_keys_cannot_escape_the_store_root(store: LocalStrategyStore) -> None:
    with pytest.raises(ValueError):
        store.put("strategies/../../etc/", "passwd", "nope")
    with pytest.raises(ValueError):
        store.put("", "strategy.py", SOURCE)
    with pytest.raises(ValueError):
        store.put(strategy_key("nested"), "sub/strategy.py", SOURCE)


@pytest.mark.parametrize(
    "key",
    [
        "..\\..\\pwned/",           # a separator to pathlib, one segment to str.split("/")
        "strategies/..\\..\\pwned/",
        "strategies/sub\\..\\..\\pwned/",
        "C:\\Windows\\Temp\\",
    ],
)
def test_backslash_keys_cannot_escape_the_store_root(tmp_path: Path, key: str) -> None:
    """Windows treats "\\" as a separator; a "/"-only check does not see it.

    This is not hypothetical: task 9 feeds HTTP-derived keys straight into the
    store, and before the guard was fixed
    ``put("..\\\\..\\\\pwned/", "owned.py", ...)`` wrote two directory levels
    above the root. The store is rooted three levels deep here so an escape has
    somewhere real to land.
    """
    root = tmp_path / "a" / "b" / "store"
    store = LocalStrategyStore(root)
    store.put(strategy_key("legit"), "strategy.py", SOURCE)

    with pytest.raises(ValueError):
        store.put(key, "owned.py", "ESCAPED")

    escaped = [
        path
        for path in tmp_path.rglob("owned.py")
    ]
    assert not escaped, f"wrote outside the store: {escaped}"
    # Nothing was created above the root either — an escape that raises after
    # doing the damage is not a fix.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a"]


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("put", ("strategies/k/", "strategy.py", SOURCE)),
        ("get", ("strategies/k/", "strategy.py")),
        ("exists", ("strategies/k/",)),
        ("delete", ("strategies/k/",)),
        ("materialize", ("strategies/k/", Path("dest"))),
    ],
)
def test_s3_backend_is_an_unimplemented_stub(method: str, args: tuple) -> None:
    s3 = S3StrategyStore(bucket="mqs-strategies")
    assert s3.bucket == "mqs-strategies"

    with pytest.raises(NotImplementedError, match="S3 backend arrives"):
        getattr(s3, method)(*args)


def test_backend_selection_reads_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    import dataclasses

    import src.integrations.strategy_store as module

    def _with_backend(backend: str) -> None:
        # Settings is a frozen dataclass, so the whole object is swapped rather
        # than one attribute assigned.
        monkeypatch.setattr(
            module,
            "settings",
            dataclasses.replace(module.settings, strategy_store_backend=backend),
        )

    _with_backend("s3")
    assert isinstance(build_strategy_store(), S3StrategyStore)

    _with_backend("local")
    assert isinstance(build_strategy_store(), LocalStrategyStore)

    _with_backend("gcs")
    with pytest.raises(ValueError, match="unknown STRATEGY_STORE_BACKEND"):
        build_strategy_store()
