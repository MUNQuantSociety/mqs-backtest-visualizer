"""Storage for user-uploaded strategy source: an S3 bucket, or local disk shaped like one.

Every call site is written against S3 vocabulary — opaque keys, whole-object
put/get, no seeking, no partial writes — so the two backends behind
:class:`StrategyStore` are interchangeable: :class:`S3StrategyStore` for a
deploy (``STRATEGY_STORE_BACKEND=s3``) and :class:`LocalStrategyStore` for a
laptop or a test. Callers never learn which one they hold.

Objects are laid out identically in both, mirroring ``engine/strategies/<portfolio>``::

    <root or s3://bucket/prefix>/strategies/<strategy_key>/strategy.py
    <root or s3://bucket/prefix>/strategies/<strategy_key>/config.json

That is load-bearing, not cosmetic: the engine's ``BasePortfolio`` discovers a
strategy's ``config.json`` by looking next to the file that defines the class
(``inspect.getfile`` sibling lookup). :meth:`StrategyStore.materialize` writes a
key's objects into a directory in exactly that shape, so a materialized user
strategy loads through the unmodified engine.

Layering (BACKEND_PLAN rule 11): this is the only module that imports boto3,
and no botocore type crosses its boundary. A missing object is a ``KeyError``,
an unsafe key is a ``ValueError``, and everything else the bucket can do wrong
is a :class:`StrategyStoreError` — the same three outcomes the local backend
produces, so a caller that handles one backend handles both.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from src.core.config import settings

if TYPE_CHECKING:  # pragma: no cover - typing only; boto3 is never imported at module scope
    from mypy_boto3_s3 import S3Client  # type: ignore[import-not-found]

# Keys are S3 keys, so the separator is always "/" regardless of platform.
KEY_SEPARATOR = "/"

# Every key the application stores lives under this prefix, mirroring how the
# real bucket will be organised (other prefixes are free for future object
# kinds without colliding with strategies).
STRATEGY_KEY_PREFIX = "strategies"

# S3's ``DeleteObjects`` accepts at most 1000 keys per request. A module
# constant rather than a literal so a test can shrink it and exercise the
# chunking without creating a thousand objects.
_DELETE_BATCH = 1000

_WINDOWS_DEVICES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{prefix}{digit}" for prefix in ("COM", "LPT") for digit in "123456789¹²³"
}


def strategy_key(strategy_id: str) -> str:
    """Build the store key for a strategy id, e.g. ``strategies/my-strat-a1b2/``.

    Callers should use this rather than hand-assembling keys so the prefix
    exists in exactly one place when the bucket layout is reviewed.
    """
    return f"{STRATEGY_KEY_PREFIX}{KEY_SEPARATOR}{strategy_id.strip(KEY_SEPARATOR)}{KEY_SEPARATOR}"


class StrategyStoreError(RuntimeError):
    """The store could not do what it was asked, and it was not a missing object.

    Raised for the bucket-level failures S3 can produce (no such bucket, access
    denied, endpoint unreachable, a partial batch delete). It exists so that
    ``botocore`` exceptions never leave ``src/integrations``: the packaging
    service already treats any exception from ``delete`` as "log and move on",
    and the worker turns any non-``KeyError`` from ``materialize`` into a
    failed run with the exception's message, so this type slots into both
    without either learning about AWS.
    """


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


# ---------------------------------------------------------------------------
# Key and filename validation, shared by every backend
# ---------------------------------------------------------------------------


def _key_segments(key: str) -> list[str]:
    """Split a key on ``"/"`` and reject anything that could walk a filesystem.

    A key arriving from an HTTP request must never escape the store, so
    traversal segments are rejected outright rather than normalised away —
    S3 has no parent directory, and neither does this.

    The backslash check is what makes the guard hold on Windows. A key is
    split on ``"/"`` only (S3's separator), so ``"..\\..\\pwned"`` is one
    segment to this code and three path components to ``pathlib``. It applies
    to the S3 backend too: an object key is only ever turned back into a local
    path by :meth:`S3StrategyStore.materialize`, and that path must land inside
    the destination on whatever platform the worker runs.
    """
    parts = [part for part in key.strip(KEY_SEPARATOR).split(KEY_SEPARATOR) if part]
    if not parts:
        raise ValueError("strategy store key must not be empty")
    for part in parts:
        _object_name(part)
    return parts


def _object_name(filename: str) -> str:
    """A filename is one path component: no separators, no dot-names, not blank."""
    name = filename
    # Apply Windows rules even on Linux: these objects may later be loaded by
    # a Windows worker. Colons include drive-relative paths and NTFS streams;
    # trailing dots/spaces and device names alias paths or bypass normal files.
    device = name.split(".", 1)[0].rstrip(" ").upper()
    if (
        not name
        or name != name.strip()
        or name.endswith(".")
        or any(char in '/\\<>:"|?*' or ord(char) < 32 for char in name)
        or device in _WINDOWS_DEVICES
    ):
        raise ValueError(f"invalid strategy store filename: {filename!r}")
    return name


def _materialize_target(destination: Path, relative: str) -> Path:
    """Validate without normalizing S3 names into colliding local paths."""
    segments = [_object_name(part) for part in relative.split(KEY_SEPARATOR)]
    target = destination.joinpath(*segments)
    root = destination.resolve()
    resolved = target.resolve()
    if root not in resolved.parents:
        raise ValueError(f"path escapes materialize destination: {relative!r}")
    current = destination
    for segment in segments:
        current = current / segment
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ValueError(f"linked materialize path: {relative!r}")
    return target


def _write_materialized_file(target: Path, body: Any) -> None:
    """A failed stream must not truncate an existing file or leave half a file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".strategy-", delete=False) as handle:
            temporary = Path(handle.name)
            shutil.copyfileobj(body, handle)
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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

        Two checks. :func:`_key_segments` rejects traversal segments and
        backslashes; the containment check behind it is what makes the
        guarantee hold whatever else a platform decides a separator is: drive
        letters, alternate data streams, or a segment type nobody has thought
        of yet.
        """
        parts = _key_segments(key)
        directory = self.root.joinpath(*parts).resolve()
        if directory != self.root and self.root not in directory.parents:
            raise ValueError(f"invalid strategy store key: {key!r}")
        return directory

    def _object_path(self, key: str, filename: str) -> Path:
        return _materialize_target(self._key_dir(key), _object_name(filename))

    # ------------------------------------------------------------------
    # StrategyStore protocol
    # ------------------------------------------------------------------
    def put(self, key: str, filename: str, content: str) -> None:
        path = self._object_path(key, filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        # UTF-8 and "\n" explicitly: the same bytes must come back on Windows
        # and Linux, because the S3 backend does not translate line endings.
        path.write_text(content, encoding="utf-8", newline="\n")

    def get(self, key: str, filename: str) -> str:
        path = self._object_path(key, filename)
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                return handle.read()
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise KeyError(f"{key}{filename}") from exc

    def exists(self, key: str) -> bool:
        directory = self._key_dir(key)
        return directory.is_dir() and any(directory.iterdir())

    def delete(self, key: str) -> None:
        directory = self._key_dir(key)
        try:
            shutil.rmtree(directory)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise StrategyStoreError(f"could not delete strategy key {key!r}: {type(exc).__name__}") from exc

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
            relative = item.relative_to(source).as_posix()
            try:
                _materialize_target(source, relative)
                target = _materialize_target(destination, relative)
            except ValueError as exc:
                raise StrategyStoreError(f"refusing to materialize unsafe object key {relative!r}") from exc
            with item.open("rb") as body:
                _write_materialized_file(target, body)
            copied = True

        if not copied:
            raise KeyError(key)
        return destination


class S3StrategyStore:
    """Bucket-backed store: one object per file, under ``<prefix>strategies/<id>/``.

    Same semantics as :class:`LocalStrategyStore`, deliberately — the tests
    run the shared contract against both. Whole-object puts overwrite, a
    missing object is a ``KeyError``, ``exists`` is a prefix probe, ``delete``
    is an idempotent prefix sweep, and ``materialize`` streams every object
    under the key into a directory in the layout the engine imports from.

    The boto3 client is created on first use, not in ``__init__``, for three
    reasons that all bite in practice. The worker pool spawns its processes
    (``src/workers/job_manager.py``), so the child rebuilds the store from
    scratch and must not inherit a socket-holding client through pickling;
    :meth:`__getstate__` drops the client for the same reason. The API process
    builds the store at import time through :func:`get_strategy_store` and a
    client that opens connections there would fail boot on a box with no AWS
    reachability (a laptop running the local backend still imports this
    module). And moto only intercepts clients created while its mock is
    active, so eager creation would make the backend untestable.

    Credentials are never taken from settings: the SDK's default chain supplies
    them (the ECS task role in a deploy, the developer's CLI profile locally).
    Region and endpoint are passed explicitly because ``src/core/config.py``
    is the one module allowed to read the environment.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        region: str | None = None,
        endpoint_url: str | None = None,
    ) -> None:
        bucket = (bucket or "").strip()
        if not bucket:
            raise ValueError("S3StrategyStore needs a bucket name")
        self.bucket = bucket
        # Normalised to either "" or "segment/segment/": the object-key
        # builders below concatenate it blindly, so exactly one slash at the
        # end is the whole contract.
        cleaned = (prefix or "").strip().strip(KEY_SEPARATOR)
        self.prefix = f"{cleaned}{KEY_SEPARATOR}" if cleaned else ""
        self.region = (region or "").strip() or None
        self.endpoint_url = (endpoint_url or "").strip() or None
        self._client: Any = None
        self._client_pid: int | None = None

    # ------------------------------------------------------------------
    # Client lifecycle
    # ------------------------------------------------------------------
    @property
    def _s3(self) -> S3Client:
        """The boto3 client, built on first access (see the class docstring).

        Not locked: boto3 clients are thread-safe, and if two request threads
        race here they build two equivalent clients and the last one wins —
        a wasted socket, not a bug.
        """
        if self._client is None or self._client_pid != os.getpid():
            import boto3
            from botocore.config import Config

            # Path-style addressing only when an endpoint is set: LocalStack
            # and MinIO resolve ``http://host:port/bucket``, while real S3
            # should keep the SDK's default virtual-hosted style.
            config = Config(
                connect_timeout=5,
                read_timeout=10,
                retries={"mode": "standard", "total_max_attempts": 3},
                s3={"addressing_style": "path"} if self.endpoint_url else {},
            )
            self._client = boto3.client(
                "s3",
                region_name=self.region,
                endpoint_url=self.endpoint_url,
                config=config,
            )
            self._client_pid = os.getpid()
        return self._client

    def __getstate__(self) -> dict[str, Any]:
        state = self.__dict__.copy()
        state["_client"] = None
        state["_client_pid"] = None
        return state

    # ------------------------------------------------------------------
    # Key translation
    # ------------------------------------------------------------------
    def _prefix_for(self, key: str) -> str:
        """``<prefix>strategies/<id>/`` — the trailing slash is the boundary.

        Without it ``strategies/ab/`` would match every object under
        ``strategies/abc/``; with it, S3's prefix listing is exactly "inside
        this directory".
        """
        return f"{self.prefix}{KEY_SEPARATOR.join(_key_segments(key))}{KEY_SEPARATOR}"

    def _object_key(self, key: str, filename: str) -> str:
        # Validation runs before any network call, so a bad key creates
        # nothing — the S3 twin of "nothing written above the root".
        return f"{self._prefix_for(key)}{_object_name(filename)}"

    def _list_keys(self, key: str, *, include_markers: bool = False) -> Iterator[str]:
        """Every real object key under ``key``, across however many pages S3 returns.

        Keys ending in ``/`` are skipped for reads: the console creates them
        as "folders" and the engine cannot load them. Deletes include them.
        """
        prefix = self._prefix_for(key)
        try:
            paginator = self._s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                for item in page.get("Contents", []):
                    object_key = item["Key"]
                    if not object_key.startswith(prefix):
                        raise StrategyStoreError(f"S3 returned a key outside requested prefix {prefix!r}")
                    if not include_markers and object_key.endswith(KEY_SEPARATOR):
                        continue
                    yield object_key
        except Exception as exc:  # noqa: BLE001 - translated below, never re-raised raw
            raise self._translate(exc, prefix) from exc

    # ------------------------------------------------------------------
    # StrategyStore protocol
    # ------------------------------------------------------------------
    def put(self, key: str, filename: str, content: str) -> None:
        object_key = self._object_key(key, filename)
        try:
            self._s3.put_object(
                Bucket=self.bucket,
                Key=object_key,
                # Bytes stored verbatim: no newline translation on either
                # backend, so an upload reads back identically everywhere.
                Body=content.encode("utf-8"),
                ContentType=_content_type_for(object_key),
            )
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc, object_key) from exc

    def get(self, key: str, filename: str) -> str:
        object_key = self._object_key(key, filename)
        try:
            response = self._s3.get_object(Bucket=self.bucket, Key=object_key)
            with closing(response["Body"]) as body:
                return body.read().decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            translated = self._translate(exc, object_key, missing_object=True)
            if isinstance(translated, KeyError):
                raise KeyError(f"{key}{filename}") from exc
            raise translated from exc

    def exists(self, key: str) -> bool:
        for _ in self._list_keys(key):
            return True
        return False

    def delete(self, key: str) -> None:
        """Sweep the prefix. Absent keys are a no-op, as S3 itself treats them.

        On a versioned bucket this leaves delete markers rather than freeing
        storage; ``exists``/``materialize`` stop seeing the objects either
        way, which is the contract.
        """
        keys = list(self._list_keys(key, include_markers=True))
        failures: list[dict[str, Any]] = []
        for start in range(0, len(keys), _DELETE_BATCH):
            chunk = keys[start : start + _DELETE_BATCH]
            try:
                response = self._s3.delete_objects(
                    Bucket=self.bucket,
                    Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True},
                )
            except Exception as exc:  # noqa: BLE001
                raise self._translate(exc, self._prefix_for(key)) from exc
            failures.extend(response.get("Errors") or [])
        if failures:
            # Try every batch, but never report success for HTTP-200 partial
            # failures. A caller can retry the idempotent sweep after repair.
            detail = "; ".join(
                f"{e.get('Key', '?')}: {e.get('Code', '?')}" for e in failures[:5]
            )
            raise StrategyStoreError(
                f"S3 could not delete {len(failures)} object(s) under "
                f"s3://{self.bucket}/{self._prefix_for(key)}: {detail}"
            )

    def materialize(self, key: str, dest_dir: Path) -> Path:
        """Stream every object under ``key`` into ``dest_dir``.

        Plain ``get_object`` per file rather than the transfer manager: a
        strategy is two small files, and the transfer manager's thread pool is
        unwelcome inside a worker process that is about to import user code.

        A key's relative path is validated with the same guard the writers
        use before it becomes a filesystem path. The store only ever writes
        safe names, but the bucket is shared infrastructure and an object
        somebody else put at ``strategies/x/../evil.py`` must not be written
        outside ``dest_dir`` — S3 has no traversal, the worker's disk does.
        """
        prefix = self._prefix_for(key)
        destination = Path(dest_dir)
        try:
            destination.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise self._translate(exc, prefix) from exc

        targets: list[tuple[str, Path]] = []
        paths: set[str] = set()
        directories: set[str] = set()
        for object_key in self._list_keys(key):
            relative = object_key[len(prefix):]
            try:
                target = _materialize_target(destination, relative)
                # On Windows two distinct S3 keys can address the same file.
                # Reject such packages on every platform before writing any.
                folded = relative.casefold()
                parents = {
                    parent.as_posix().casefold()
                    for parent in Path(relative).parents if parent != Path(".")
                }
                if folded in paths or folded in directories or parents & paths:
                    raise ValueError("colliding materialize paths")
                paths.add(folded)
                directories.update(parents)
            except ValueError as exc:
                raise StrategyStoreError(
                    f"refusing to materialize unsafe object key "
                    f"s3://{self.bucket}/{object_key}"
                ) from exc
            except OSError as exc:
                raise self._translate(exc, object_key) from exc
            targets.append((object_key, target))

        if not targets:
            raise KeyError(key)
        for object_key, target in targets:
            try:
                with closing(self._s3.get_object(Bucket=self.bucket, Key=object_key)["Body"]) as body:
                    _write_materialized_file(target, body)
            except Exception as exc:  # noqa: BLE001
                raise self._translate(exc, object_key, missing_object=True) from exc
        return destination

    # ------------------------------------------------------------------
    # Error translation — the one place botocore vocabulary is understood
    # ------------------------------------------------------------------
    def _translate(self, exc: Exception, object_key: str, *, missing_object: bool = False) -> Exception:
        """Map an SDK failure to the store's contract; the caller raises it.

        Returned rather than raised so ``get`` can rewrite the ``KeyError``
        message into the same ``key + filename`` shape the local backend uses.
        Filesystem and decoding failures also become store errors. Programming
        errors pass through unchanged so they are not mislabeled as S3 failures.
        """
        from botocore.exceptions import BotoCoreError, ClientError

        if isinstance(exc, ClientError):
            error = exc.response.get("Error", {})
            code = str(error.get("Code", "")) or type(exc).__name__
            # Bare 404/NotFound can mean a missing bucket or bad endpoint.
            # Only an explicit missing-object response to a read is absence.
            if missing_object and code == "NoSuchKey":
                return KeyError(object_key)
            return StrategyStoreError(
                f"S3 {code} on s3://{self.bucket}/{object_key}"
            )
        if isinstance(exc, BotoCoreError):
            # SDK messages may include endpoint URLs with credentials/query
            # parameters; report the type and object, never the raw message.
            return StrategyStoreError(
                f"S3 {type(exc).__name__} on s3://{self.bucket}/{object_key}"
            )
        if isinstance(exc, (OSError, UnicodeError)):
            return StrategyStoreError(
                f"{type(exc).__name__} accessing stored object s3://{self.bucket}/{object_key}"
            )
        return exc


def _content_type_for(object_key: str) -> str:
    """A truthful Content-Type, so the console and any presigned download behave."""
    lowered = object_key.lower()
    if lowered.endswith(".py"):
        return "text/x-python; charset=utf-8"
    if lowered.endswith(".json"):
        return "application/json"
    return "text/plain; charset=utf-8"


def build_strategy_store() -> StrategyStore:
    """Construct the store the environment selects (``STRATEGY_STORE_BACKEND``).

    A misconfigured S3 selection fails here, with one sentence, rather than at
    the first upload: call this (through :func:`get_strategy_store`) at boot
    and a deploy missing its bucket name never comes up half-working.
    """
    backend = settings.strategy_store_backend
    if backend == "local":
        return LocalStrategyStore(settings.strategy_store_root)
    if backend == "s3":
        if not settings.strategy_store_s3_bucket.strip():
            raise ValueError(
                "STRATEGY_STORE_BACKEND=s3 requires STRATEGY_STORE_S3_BUCKET to "
                "name the bucket that holds uploaded strategies"
            )
        return S3StrategyStore(
            settings.strategy_store_s3_bucket,
            prefix=settings.strategy_store_s3_prefix,
            region=settings.aws_region or None,
            endpoint_url=settings.strategy_store_s3_endpoint_url or None,
        )
    raise ValueError(
        f"unknown STRATEGY_STORE_BACKEND {backend!r}; expected 'local' or 's3'"
    )


_store: StrategyStore | None = None


def get_strategy_store() -> StrategyStore:
    """Process-wide store instance.

    Cached because worker processes call it per run and the local backend's
    constructor touches the filesystem; the object itself is stateless apart
    from the S3 client, which is created lazily and per process, so sharing
    it is safe. A spawned worker starts with ``_store`` unset and rebuilds it.
    """
    global _store
    if _store is None:
        _store = build_strategy_store()
    return _store
