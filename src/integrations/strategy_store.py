"""Storage for user-uploaded strategy source, shaped like S3 from day one.

The product owner wants uploaded strategies to live in an S3 bucket. That
bucket does not exist yet and must not block the upload feature, so every call
site is written against S3 vocabulary — opaque keys, whole-object put/get, no
seeking, no partial writes — and backed by local disk today. When the bucket is
provisioned, the swap is a new class behind :class:`StrategyStore`, not a
refactor of the callers.

The local layout is deliberately identical to ``engine/strategies/<portfolio>``::

    <root>/strategies/<strategy_key>/strategy.py
    <root>/strategies/<strategy_key>/config.json

That is load-bearing, not cosmetic: the engine's ``BasePortfolio`` discovers a
strategy's ``config.json`` by looking next to the file that defines the class
(``inspect.getfile`` sibling lookup). :meth:`StrategyStore.materialize` writes a
key's objects into a directory in exactly that shape, so a materialized user
strategy loads through the unmodified engine.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Protocol, runtime_checkable

from src.core.config import settings

# Keys are S3 keys, so the separator is always "/" regardless of platform.
KEY_SEPARATOR = "/"

# Every key the application stores lives under this prefix, mirroring how the
# real bucket will be organised (other prefixes are free for future object
# kinds without colliding with strategies).
STRATEGY_KEY_PREFIX = "strategies"


def strategy_key(strategy_id: str) -> str:
    """Build the store key for a strategy id, e.g. ``strategies/my-strat-a1b2/``.

    Callers should use this rather than hand-assembling keys so the prefix
    exists in exactly one place when the bucket layout is reviewed.
    """
    return f"{STRATEGY_KEY_PREFIX}{KEY_SEPARATOR}{strategy_id.strip(KEY_SEPARATOR)}{KEY_SEPARATOR}"


@runtime_checkable
class StrategyStore(Protocol):
    """The only surface through which strategy source is read or written."""

    def put(self, key: str, filename: str, content: str) -> None:
        """Write ``content`` as the object ``key + filename``, replacing any prior value."""
        ...

    def get(self, key: str, filename: str) -> str:
        """Return the object's text. Raises ``KeyError`` when it does not exist."""
        ...

    def exists(self, key: str) -> bool:
        """True when at least one object lives under ``key``."""
        ...

    def delete(self, key: str) -> None:
        """Remove every object under ``key``. Deleting an absent key is a no-op."""
        ...

    def materialize(self, key: str, dest_dir: Path) -> Path:
        """Write every object under ``key`` into ``dest_dir`` and return it."""
        ...


class LocalStrategyStore:
    """Disk-backed store rooted at a single gitignored directory.

    Objects are files, keys are relative directories. The implementation goes
    out of its way to behave like object storage: puts overwrite silently and
    create their parents, a missing object is a ``KeyError`` (never a
    ``FileNotFoundError`` leaking the local path), and nothing outside ``root``
    is reachable through a key.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()

    # ------------------------------------------------------------------
    # Key/path translation
    # ------------------------------------------------------------------
    def _key_dir(self, key: str) -> Path:
        """Resolve a key to a directory inside ``root``.

        A key arriving from an HTTP request must never escape the store, so
        traversal segments are rejected outright rather than normalised away —
        S3 has no parent directory, and neither does this.

        Two checks, because the first one alone is not enough on Windows. A key
        is split on ``"/"`` only (S3's separator), so ``"..\\..\\pwned"`` is one
        segment to this code and three path components to ``pathlib`` — a
        backslash inside a segment therefore has to be rejected explicitly. The
        containment check behind it is what makes the guarantee hold whatever
        else a platform decides a separator is: drive letters, alternate data
        streams, or a segment type nobody has thought of yet.
        """
        parts = [part for part in key.strip(KEY_SEPARATOR).split(KEY_SEPARATOR) if part]
        if not parts:
            raise ValueError("strategy store key must not be empty")
        if any(part in {".", ".."} or "\\" in part for part in parts):
            raise ValueError(f"invalid strategy store key: {key!r}")

        directory = self.root.joinpath(*parts).resolve()
        if directory != self.root and self.root not in directory.parents:
            raise ValueError(f"invalid strategy store key: {key!r}")
        return directory

    def _object_path(self, key: str, filename: str) -> Path:
        name = filename.strip()
        if not name or KEY_SEPARATOR in name or "\\" in name or name in {".", ".."}:
            raise ValueError(f"invalid strategy store filename: {filename!r}")
        return self._key_dir(key) / name

    # ------------------------------------------------------------------
    # StrategyStore protocol
    # ------------------------------------------------------------------
    def put(self, key: str, filename: str, content: str) -> None:
        path = self._object_path(key, filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        # UTF-8 and "\n" explicitly: the same bytes must come back on Windows
        # and Linux, because the S3 backend will not translate line endings.
        path.write_text(content, encoding="utf-8", newline="\n")

    def get(self, key: str, filename: str) -> str:
        path = self._object_path(key, filename)
        try:
            return path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise KeyError(f"{key}{filename}") from exc

    def exists(self, key: str) -> bool:
        directory = self._key_dir(key)
        return directory.is_dir() and any(directory.iterdir())

    def delete(self, key: str) -> None:
        shutil.rmtree(self._key_dir(key), ignore_errors=True)

    def materialize(self, key: str, dest_dir: Path) -> Path:
        """Copy every object under ``key`` into ``dest_dir``.

        The engine loads a strategy by importing ``strategy.py`` and reading the
        ``config.json`` sitting beside it, so the whole key has to land together
        in one directory — copying only the source file would produce a
        strategy the engine cannot configure.
        """
        source = self._key_dir(key)
        if not source.is_dir():
            raise KeyError(key)

        destination = Path(dest_dir)
        destination.mkdir(parents=True, exist_ok=True)

        copied = False
        for item in sorted(source.rglob("*")):
            if not item.is_file():
                continue
            target = destination / item.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item, target)
            copied = True

        if not copied:
            raise KeyError(key)
        return destination


class S3StrategyStore:
    """Stub for the eventual bucket-backed store.

    It exists so the selection seam and the call sites are real today. There is
    no boto3 dependency and no bucket; every method raises. Implementing this
    class against the :class:`StrategyStore` protocol is the entire migration.
    """

    _UNAVAILABLE = "S3 backend arrives with infrastructure"

    def __init__(self, bucket: str) -> None:
        self.bucket = bucket

    def put(self, key: str, filename: str, content: str) -> None:
        raise NotImplementedError(self._UNAVAILABLE)

    def get(self, key: str, filename: str) -> str:
        raise NotImplementedError(self._UNAVAILABLE)

    def exists(self, key: str) -> bool:
        raise NotImplementedError(self._UNAVAILABLE)

    def delete(self, key: str) -> None:
        raise NotImplementedError(self._UNAVAILABLE)

    def materialize(self, key: str, dest_dir: Path) -> Path:
        raise NotImplementedError(self._UNAVAILABLE)


def build_strategy_store() -> StrategyStore:
    """Construct the store the environment selects (``STRATEGY_STORE_BACKEND``)."""
    backend = settings.strategy_store_backend
    if backend == "local":
        return LocalStrategyStore(settings.strategy_store_root)
    if backend == "s3":
        return S3StrategyStore(settings.strategy_store_s3_bucket)
    raise ValueError(
        f"unknown STRATEGY_STORE_BACKEND {backend!r}; expected 'local' or 's3'"
    )


_store: StrategyStore | None = None


def get_strategy_store() -> StrategyStore:
    """Process-wide store instance.

    Cached because worker processes call it per run and the local backend's
    constructor touches the filesystem; the object itself is stateless, so
    sharing it is safe.
    """
    global _store
    if _store is None:
        _store = build_strategy_store()
    return _store
