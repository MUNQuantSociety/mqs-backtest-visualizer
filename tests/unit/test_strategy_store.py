"""Unit tests for the S3-shaped strategy store. No database, no network.

The round-trip contract runs against both backends: ``LocalStrategyStore`` on
a temp directory and ``S3StrategyStore`` against moto's in-process S3. The
moto fixture injects fake credentials and clears ``AWS_PROFILE`` *before* the
mock starts, so a developer machine holding real keys can never be reached
by a test — that, plus the store creating its client lazily, is what keeps
this file offline-safe.
"""

from __future__ import annotations

import dataclasses
import io
import json
import multiprocessing
import os
import pickle
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.integrations.strategy_store import (
    LocalStrategyStore,
    S3StrategyStore,
    StrategyStore,
    StrategyStoreError,
    build_strategy_store,
    strategy_key,
)

SOURCE = "class MyStrategy(BasePortfolio):\n    def OnData(self, context):\n        pass\n"
CONFIG = json.dumps({"TICKERS": ["AAPL", "MSFT"], "LOOKBACK_DAYS": 30})

BUCKET = "mqs-strategies-test"
REGION = "us-east-1"  # the one region whose create_bucket needs no LocationConstraint


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def s3_bucket(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """An empty moto bucket, with the real AWS environment shut out.

    Fake credentials go into the environment rather than a client argument
    because the store must build its own client from the default chain, the
    same way it does in a deploy; the mock intercepts whatever that chain
    produces as long as it is active first.
    """
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SECURITY_TOKEN", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3"):
        monkeypatch.delenv(name, raising=False)

    import boto3
    from moto import mock_aws

    mock = mock_aws()
    mock.start()
    try:
        boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
        yield BUCKET
    finally:
        mock.stop()


@pytest.fixture()
def s3_store(s3_bucket: str) -> S3StrategyStore:
    return S3StrategyStore(s3_bucket, region=REGION)


@pytest.fixture(params=["local", "s3"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> StrategyStore:
    """Both backends, so every contract test below runs twice unchanged."""
    if request.param == "local":
        return LocalStrategyStore(tmp_path / "store")
    return request.getfixturevalue("s3_store")


def _bucket_keys(bucket: str) -> list[str]:
    """Every key in the mock bucket, straight from boto3, bypassing the store."""
    import boto3

    keys: list[str] = []
    paginator = boto3.client("s3", region_name=REGION).get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        keys.extend(item["Key"] for item in page.get("Contents", []))
    return sorted(keys)


# ---------------------------------------------------------------------------
# Contract shared by both backends
# ---------------------------------------------------------------------------


def test_key_helper_builds_prefixed_directory_key() -> None:
    assert strategy_key("my-strat-a1b2") == "strategies/my-strat-a1b2/"


def test_store_satisfies_the_protocol(store: StrategyStore) -> None:
    assert isinstance(store, StrategyStore)


def test_put_then_get_round_trips_exact_content(store: StrategyStore) -> None:
    key = strategy_key("round-trip")
    store.put(key, "strategy.py", SOURCE)

    assert store.get(key, "strategy.py") == SOURCE


def test_put_overwrites_like_an_object_write(store: StrategyStore) -> None:
    key = strategy_key("overwrite")
    store.put(key, "strategy.py", SOURCE)
    store.put(key, "strategy.py", "# replaced\n")

    assert store.get(key, "strategy.py") == "# replaced\n"


def test_get_missing_object_raises_key_error(store: StrategyStore) -> None:
    key = strategy_key("absent")
    with pytest.raises(KeyError):
        store.get(key, "strategy.py")

    store.put(key, "strategy.py", SOURCE)
    with pytest.raises(KeyError):
        store.get(key, "config.json")


def test_exists_tracks_the_key_lifecycle(store: StrategyStore) -> None:
    key = strategy_key("lifecycle")
    assert store.exists(key) is False

    store.put(key, "strategy.py", SOURCE)
    assert store.exists(key) is True

    store.delete(key)
    assert store.exists(key) is False


def test_delete_removes_every_object_and_is_idempotent(store: StrategyStore) -> None:
    key = strategy_key("delete-me")
    store.put(key, "strategy.py", SOURCE)
    store.put(key, "config.json", CONFIG)

    store.delete(key)
    store.delete(key)  # deleting an absent key is a no-op, as in S3

    with pytest.raises(KeyError):
        store.get(key, "strategy.py")


def test_delete_leaves_other_keys_alone(store: StrategyStore) -> None:
    keep, drop = strategy_key("keep"), strategy_key("drop")
    store.put(keep, "strategy.py", SOURCE)
    store.put(drop, "strategy.py", SOURCE)

    store.delete(drop)

    assert store.exists(keep) is True


def test_exists_respects_the_key_boundary(store: StrategyStore) -> None:
    """``strategies/ab/`` must not see ``strategies/abc/``: a prefix probe needs its slash."""
    store.put(strategy_key("abc"), "strategy.py", SOURCE)

    assert store.exists(strategy_key("ab")) is False
    assert store.exists(strategy_key("abc")) is True


def test_materialize_reproduces_the_engine_folder_shape(
    store: StrategyStore, tmp_path: Path
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
    store: StrategyStore, tmp_path: Path
) -> None:
    with pytest.raises(KeyError):
        store.materialize(strategy_key("nothing-here"), tmp_path / "dest")


def test_keys_cannot_escape_the_store_root(store: StrategyStore) -> None:
    with pytest.raises(ValueError):
        store.put("strategies/../../etc/", "passwd", "nope")
    with pytest.raises(ValueError):
        store.put("", "strategy.py", SOURCE)
    with pytest.raises(ValueError):
        store.put(strategy_key("nested"), "sub/strategy.py", SOURCE)


def test_unsafe_keys_are_rejected_before_any_read(store: StrategyStore) -> None:
    """Every method validates first — get/exists/delete/materialize too, not only put."""
    for bad in ("strategies/../x/", ".", "..", "strategies/a\\b/"):
        with pytest.raises(ValueError):
            store.get(bad, "strategy.py")
        with pytest.raises(ValueError):
            store.exists(bad)
        with pytest.raises(ValueError):
            store.delete(bad)
        with pytest.raises(ValueError):
            store.materialize(bad, Path("unused"))


BACKSLASH_KEYS = [
    "..\\..\\pwned/",           # a separator to pathlib, one segment to str.split("/")
    "strategies/..\\..\\pwned/",
    "strategies/sub\\..\\..\\pwned/",
    "C:\\Windows\\Temp\\",
]


@pytest.mark.parametrize("key", BACKSLASH_KEYS)
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


@pytest.mark.parametrize("key", BACKSLASH_KEYS)
def test_backslash_keys_create_no_s3_object(s3_store: S3StrategyStore, key: str) -> None:
    """The S3 twin: after the ValueError the bucket holds only the legitimate object."""
    s3_store.put(strategy_key("legit"), "strategy.py", SOURCE)

    with pytest.raises(ValueError):
        s3_store.put(key, "owned.py", "ESCAPED")

    assert _bucket_keys(BUCKET) == ["strategies/legit/strategy.py"]


# ---------------------------------------------------------------------------
# S3-specific behaviour
# ---------------------------------------------------------------------------


def test_s3_constructor_requires_a_bucket() -> None:
    with pytest.raises(ValueError, match="bucket"):
        S3StrategyStore("")
    with pytest.raises(ValueError, match="bucket"):
        S3StrategyStore("   ")


def test_s3_constructor_creates_no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Construction must be free of AWS calls: it happens at import time in the
    API process and again in every spawned worker, and moto only sees clients
    created after its mock starts."""
    import boto3

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("boto3.client called during construction")

    monkeypatch.setattr(boto3, "client", _boom)

    store = S3StrategyStore("any-bucket", prefix="/team-a/", region="eu-west-1")

    assert store._client is None
    assert store.bucket == "any-bucket"
    assert store.prefix == "team-a/"
    assert store.region == "eu-west-1"
    assert store.endpoint_url is None
    # Only a method that talks to S3 reaches for the client.
    with pytest.raises(AssertionError, match="boto3.client called"):
        store.exists(strategy_key("x"))


def test_s3_store_pickles_without_its_client(s3_store: S3StrategyStore) -> None:
    """The worker pool spawns; a store carrying a live client would not cross."""
    s3_store.put(strategy_key("pickle"), "strategy.py", SOURCE)
    assert s3_store._client is not None

    clone = pickle.loads(pickle.dumps(s3_store))

    assert clone._client is None
    assert (clone.bucket, clone.prefix, clone.region) == (
        s3_store.bucket, s3_store.prefix, s3_store.region
    )
    # And it works after the trip — the client is rebuilt on demand.
    assert clone.get(strategy_key("pickle"), "strategy.py") == SOURCE


def test_s3_object_layout_matches_the_engine_shape(s3_bucket: str) -> None:
    plain = S3StrategyStore(s3_bucket, region=REGION)
    plain.put(strategy_key("layout"), "strategy.py", SOURCE)
    plain.put(strategy_key("layout"), "config.json", CONFIG)

    assert _bucket_keys(s3_bucket) == [
        "strategies/layout/config.json",
        "strategies/layout/strategy.py",
    ]

    prefixed = S3StrategyStore(s3_bucket, prefix="team-a", region=REGION)
    prefixed.put(strategy_key("layout"), "strategy.py", SOURCE)

    assert "team-a/strategies/layout/strategy.py" in _bucket_keys(s3_bucket)
    # Prefixes are namespaces: the plain store does not see the prefixed one's key.
    assert prefixed.exists(strategy_key("layout")) is True
    prefixed.delete(strategy_key("layout"))
    assert plain.exists(strategy_key("layout")) is True


def test_s3_put_sets_a_truthful_content_type(s3_store: S3StrategyStore) -> None:
    import boto3

    key = strategy_key("ctype")
    s3_store.put(key, "strategy.py", SOURCE)
    s3_store.put(key, "config.json", CONFIG)

    client = boto3.client("s3", region_name=REGION)
    py = client.head_object(Bucket=BUCKET, Key="strategies/ctype/strategy.py")
    js = client.head_object(Bucket=BUCKET, Key="strategies/ctype/config.json")
    assert py["ContentType"].startswith("text/x-python")
    assert js["ContentType"] == "application/json"


def test_s3_round_trips_bytes_verbatim(s3_store: S3StrategyStore) -> None:
    """No newline translation and no encoding surprises: CRLF and non-ASCII survive."""
    content = "# résumé\r\nclass A:\r\n    pass\r\n"
    key = strategy_key("verbatim")
    s3_store.put(key, "strategy.py", content)

    assert s3_store.get(key, "strategy.py") == content


def test_s3_paginates_past_one_page_for_materialize_and_delete(
    s3_store: S3StrategyStore, tmp_path: Path
) -> None:
    """list_objects_v2 returns at most 1000 keys per page and delete_objects
    accepts at most 1000 per request. More than that under one key must still
    be fully materialized and fully deleted, or a run would silently see a
    partial strategy and a delete would silently leak objects."""
    import boto3

    key = strategy_key("big")
    client = boto3.client("s3", region_name=REGION)
    count = 1005
    for i in range(count):
        # Written directly, not through the store: this test is about reading
        # and deleting past a page boundary, not about put.
        client.put_object(Bucket=BUCKET, Key=f"strategies/big/f{i:04d}.txt", Body=b"x")

    dest = s3_store.materialize(key, tmp_path / "big")
    files = sorted(p.name for p in dest.iterdir())
    assert len(files) == count
    assert files[0] == "f0000.txt" and files[-1] == f"f{count - 1:04d}.txt"

    s3_store.delete(key)

    assert _bucket_keys(BUCKET) == []
    assert s3_store.exists(key) is False


def test_s3_delete_chunks_at_the_configured_batch_size(
    s3_store: S3StrategyStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same chunking, cheaply: shrink the batch and count the delete calls."""
    import src.integrations.strategy_store as module

    monkeypatch.setattr(module, "_DELETE_BATCH", 2)
    key = strategy_key("chunked")
    for i in range(5):
        s3_store.put(key, f"f{i}.txt", "x")

    real = s3_store._s3.delete_objects
    calls: list[int] = []

    def counting(**kwargs: object):
        calls.append(len(kwargs["Delete"]["Objects"]))  # type: ignore[index]
        return real(**kwargs)

    monkeypatch.setattr(s3_store._s3, "delete_objects", counting)
    s3_store.delete(key)

    assert calls == [2, 2, 1]
    assert s3_store.exists(key) is False


def test_s3_materialize_skips_folder_markers_and_refuses_traversal_keys(
    s3_store: S3StrategyStore, tmp_path: Path
) -> None:
    """A console-made 'folder' is not a file; a foreign key with ``..`` in it is
    a reason to stop, not something to write outside the destination."""
    import boto3

    client = boto3.client("s3", region_name=REGION)
    key = strategy_key("marker")
    client.put_object(Bucket=BUCKET, Key="strategies/marker/", Body=b"")
    s3_store.put(key, "strategy.py", SOURCE)
    s3_store.put(key, "config.json", CONFIG)

    dest = s3_store.materialize(key, tmp_path / "out" / "pkg")
    assert sorted(p.name for p in dest.iterdir()) == ["config.json", "strategy.py"]

    # A marker alone is "nothing stored", same as an empty local directory.
    client.put_object(Bucket=BUCKET, Key="strategies/only-marker/", Body=b"")
    with pytest.raises(KeyError):
        s3_store.materialize(strategy_key("only-marker"), tmp_path / "out2")

    # Traversal injected straight into the bucket: refuse, and prove nothing
    # landed above the destination.
    client.put_object(Bucket=BUCKET, Key="strategies/evil/../evil.py", Body=b"ESCAPED")
    client.put_object(Bucket=BUCKET, Key="strategies/evil/strategy.py", Body=SOURCE.encode())
    with pytest.raises(StrategyStoreError, match="unsafe object key"):
        s3_store.materialize(strategy_key("evil"), tmp_path / "jail" / "deep" / "pkg")
    assert list(tmp_path.rglob("evil.py")) == []


def test_s3_bucket_failures_are_store_errors_not_key_errors(s3_bucket: str, tmp_path: Path) -> None:
    """NoSuchBucket is an operator problem; it must not read as "strategy missing"."""
    store = S3StrategyStore("no-such-bucket-" + s3_bucket, region=REGION)
    key = strategy_key("x")

    with pytest.raises(StrategyStoreError, match="NoSuchBucket"):
        store.put(key, "strategy.py", SOURCE)
    with pytest.raises(StrategyStoreError, match="NoSuchBucket"):
        store.get(key, "strategy.py")
    with pytest.raises(StrategyStoreError, match="NoSuchBucket"):
        store.exists(key)
    with pytest.raises(StrategyStoreError, match="NoSuchBucket"):
        store.materialize(key, tmp_path / "dest")


def test_s3_missing_object_is_exactly_a_key_error(s3_store: S3StrategyStore) -> None:
    key = strategy_key("nokey")
    with pytest.raises(KeyError) as excinfo:
        s3_store.get(key, "strategy.py")
    # Same message shape as the local backend: key + filename.
    assert excinfo.value.args[0] == f"{key}strategy.py"
    assert not isinstance(excinfo.value, StrategyStoreError)


@pytest.mark.parametrize("method", ["put", "get", "exists", "delete", "materialize"])
@pytest.mark.parametrize("code", ["AccessDenied", "NoSuchBucket", "404", "NotFound"])
def test_s3_operation_errors_are_not_absence(s3_store, tmp_path, monkeypatch, method, code):
    from botocore.exceptions import ClientError

    key = strategy_key("denied")
    s3_store.put(key, "strategy.py", SOURCE)
    operation = {"put": "put_object", "get": "get_object", "delete": "delete_objects"}.get(method, "list_objects_v2")
    failure = ClientError({"Error": {"Code": code, "Message": "untrusted-service-detail"}}, operation)
    monkeypatch.setattr(s3_store._s3, operation, Mock(side_effect=failure))
    args = {
        "put": (key, "strategy.py", SOURCE), "get": (key, "strategy.py"),
        "exists": (key,), "delete": (key,), "materialize": (key, tmp_path / "pkg"),
    }[method]
    with pytest.raises(StrategyStoreError, match=code) as caught:
        getattr(s3_store, method)(*args)
    assert "untrusted-service-detail" not in str(caught.value)


@pytest.mark.parametrize("method", ["get", "materialize"])
def test_s3_object_disappearing_after_listing_is_missing(s3_store, tmp_path, monkeypatch, method):
    from botocore.exceptions import ClientError

    key = strategy_key("vanished")
    s3_store.put(key, "strategy.py", SOURCE)
    failure = ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
    monkeypatch.setattr(s3_store._s3, "get_object", Mock(side_effect=failure))
    with pytest.raises(KeyError):
        if method == "get":
            s3_store.get(key, "strategy.py")
        else:
            s3_store.materialize(key, tmp_path / "pkg")


@pytest.mark.parametrize("method", ["put", "get", "exists", "delete", "materialize"])
def test_s3_client_creation_failure_is_translated(monkeypatch, tmp_path, method):
    import boto3
    from botocore.exceptions import NoCredentialsError

    store = S3StrategyStore(BUCKET, region=REGION)
    monkeypatch.setattr(boto3, "client", Mock(side_effect=NoCredentialsError()))
    key = strategy_key("no-client")
    args = {"put": (key, "strategy.py", SOURCE), "get": (key, "strategy.py"),
            "exists": (key,), "delete": (key,), "materialize": (key, tmp_path / "pkg")}[method]
    with pytest.raises(StrategyStoreError, match="NoCredentialsError"):
        getattr(store, method)(*args)


@pytest.mark.parametrize("endpoint", [None, "http://localhost:4566"])
def test_s3_timeouts_and_retry_attempts_override_sdk_defaults(s3_bucket, monkeypatch, endpoint):
    monkeypatch.setenv("AWS_MAX_ATTEMPTS", "99")
    monkeypatch.setenv("AWS_RETRY_MODE", "adaptive")
    store = S3StrategyStore(s3_bucket, region=REGION, endpoint_url=endpoint)
    config = store._s3.meta.config
    assert config.connect_timeout == 5
    assert config.read_timeout == 10
    assert config.retries == {"mode": "standard", "total_max_attempts": 3}
    if endpoint:
        assert config.s3["addressing_style"] == "path"


def test_s3_inherited_client_is_rebuilt(s3_store, monkeypatch):
    import src.integrations.strategy_store as module

    old = s3_store._s3
    with monkeypatch.context() as context:
        context.setattr(module.os, "getpid", lambda: 123456789)
        new = s3_store._s3
        assert new is not old
        assert s3_store._s3 is new


def _spawn_store_probe(store, pipe):
    """The child owns its moto backend; parent mocks cannot cross spawn."""
    import boto3
    from moto import mock_aws

    try:
        lazy = store._client is None and store._client_pid is None
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=store.bucket)
            key = strategy_key("child")
            store.put(key, "strategy.py", SOURCE)
            pipe.send((lazy, store.get(key, "strategy.py"), store._client_pid))
    finally:
        pipe.close()


def test_s3_store_works_in_a_real_spawned_process(s3_store):
    s3_store.put(strategy_key("parent"), "strategy.py", SOURCE)
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_spawn_store_probe, args=(s3_store, sender))
    try:
        process.start()
        sender.close()
        assert receiver.poll(30), "spawned mock S3 check did not respond"
        lazy, content, pid = receiver.recv()
        process.join(10)
        assert process.exitcode == 0
        assert lazy and content == SOURCE and pid == process.pid
        assert not s3_store.exists(strategy_key("child"))
    finally:
        if process.is_alive():
            process.terminate()
            process.join(10)
        receiver.close()
        sender.close()


def test_s3_partial_delete_reports_failures_and_finishes_other_batches(s3_store, monkeypatch):
    import src.integrations.strategy_store as module

    monkeypatch.setattr(module, "_DELETE_BATCH", 2)
    key = strategy_key("partial")
    for filename in ["a.txt", "b.txt", "c.txt"]:
        s3_store.put(key, filename, "x")
    real = s3_store._s3.delete_objects
    calls = []

    def partially_delete(**kwargs):
        objects = kwargs["Delete"]["Objects"]
        calls.append(objects)
        if len(calls) == 1:
            real(Bucket=BUCKET, Delete={"Objects": objects[1:]})
            return {"Errors": [{"Key": objects[0]["Key"], "Code": "AccessDenied"}]}
        return real(**kwargs)

    with monkeypatch.context() as context:
        context.setattr(s3_store._s3, "delete_objects", partially_delete)
        with pytest.raises(StrategyStoreError, match=r"1 object\(s\).*a.txt: AccessDenied"):
            s3_store.delete(key)
    assert len(calls) == 2
    assert _bucket_keys(BUCKET) == [f"{key}a.txt"]
    s3_store.delete(key)
    assert not s3_store.exists(key)


def test_s3_delete_includes_folder_markers(s3_store):
    key = strategy_key("markers")
    for suffix in ["", "nested/"]:
        s3_store._s3.put_object(Bucket=BUCKET, Key=key + suffix, Body=b"")
    assert not s3_store.exists(key)
    s3_store.delete(key)
    assert _bucket_keys(BUCKET) == []


def test_s3_exists_reads_past_a_marker_only_page(s3_store, monkeypatch):
    from botocore.stub import Stubber

    key = strategy_key("pages")
    with Stubber(s3_store._s3) as stub:
        stub.add_response("list_objects_v2", {
            "IsTruncated": True, "NextContinuationToken": "next",
            "Contents": [{"Key": key}],
        }, {"Bucket": BUCKET, "Prefix": key})
        stub.add_response("list_objects_v2", {
            "IsTruncated": False, "Contents": [{"Key": key + "strategy.py"}],
        }, {"Bucket": BUCKET, "Prefix": key, "ContinuationToken": "next"})
        assert s3_store.exists(key)
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("method", ["delete", "materialize"])
def test_s3_later_page_failure_never_silently_completes(s3_store, monkeypatch, tmp_path, method):
    from botocore.stub import Stubber

    key = strategy_key("page-error")
    delete = Mock()
    monkeypatch.setattr(s3_store._s3, "delete_objects", delete)
    with Stubber(s3_store._s3) as stub:
        stub.add_response("list_objects_v2", {
            "IsTruncated": True, "NextContinuationToken": "next",
            "Contents": [{"Key": key + "strategy.py"}],
        }, {"Bucket": BUCKET, "Prefix": key})
        stub.add_client_error("list_objects_v2", service_error_code="AccessDenied",
                              http_status_code=403,
                              expected_params={"Bucket": BUCKET, "Prefix": key, "ContinuationToken": "next"})
        with pytest.raises(StrategyStoreError, match="AccessDenied"):
            if method == "delete":
                s3_store.delete(key)
            else:
                s3_store.materialize(key, tmp_path / "pkg")
        stub.assert_no_pending_responses()
    delete.assert_not_called()
    assert not list(tmp_path.rglob("strategy.py"))


UNSAFE_NAMES = ["C:escape.py", "config.json:stream", "NUL", "con.py", "COM1.txt",
                "lpt9.log", "COM¹.txt", "CONOUT$", "file.", "file ", "<bad>", "bad?", "bad\x00name"]


@pytest.mark.parametrize("name", UNSAFE_NAMES)
def test_windows_unsafe_names_are_rejected_on_both_backends(store, name):
    with pytest.raises(ValueError):
        store.put(strategy_key("safe"), name, "x")
    with pytest.raises(ValueError):
        store.exists(f"strategies/{name}/")


@pytest.mark.parametrize("relative", UNSAFE_NAMES + ["/absolute.py", "sub//alias.py", "../escape.py", "sub/../escape.py", "sub\\escape.py"])
def test_foreign_s3_paths_are_rejected_before_writing(s3_store, tmp_path, relative):
    key = strategy_key("unsafe")
    s3_store.put(key, "000-safe.txt", "must not be materialized")
    s3_store._s3.put_object(Bucket=BUCKET, Key=key + relative, Body=b"ESCAPED")
    with pytest.raises(StrategyStoreError, match="unsafe object key"):
        s3_store.materialize(key, tmp_path / "pkg")
    assert not list(tmp_path.rglob("000-safe.txt"))


@pytest.mark.parametrize("names", [["strategy.py", "Strategy.py"], ["sub", "sub/file.py"], ["sub/file.py", "SUB"]])
def test_s3_materialize_rejects_path_aliases(s3_store, tmp_path, names):
    key = strategy_key("aliases")
    for name in names:
        s3_store._s3.put_object(Bucket=BUCKET, Key=key + name, Body=b"x")
    with pytest.raises(StrategyStoreError, match="unsafe object key"):
        s3_store.materialize(key, tmp_path / "pkg")
    assert not list((tmp_path / "pkg").iterdir())


def test_materialize_refuses_existing_directory_links(store, tmp_path):
    key = strategy_key("linked")
    store.put(key + "nested/", "strategy.py", SOURCE)
    destination, outside = tmp_path / "pkg", tmp_path / "outside"
    destination.mkdir()
    outside.mkdir()
    link = destination / "nested"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(outside), str(link))
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(StrategyStoreError, match="unsafe object key"):
            store.materialize(key, destination)
        assert list(outside.iterdir()) == []
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


@pytest.mark.parametrize("method", ["get", "materialize"])
@pytest.mark.parametrize("fail_read", [False, True])
def test_s3_closes_response_stream_even_on_failure(s3_store, tmp_path, monkeypatch, method, fail_read):
    from botocore.exceptions import ReadTimeoutError

    key = strategy_key("body")
    s3_store.put(key, "strategy.py", SOURCE)
    body = io.BytesIO(SOURCE.encode())
    if fail_read:
        failure = ReadTimeoutError(endpoint_url="https://user:secret@example.test/?token=secret")
        # Materialization writes one chunk before the stream fails, proving
        # that cleanup handles a partial download, not only a failed request.
        body.read = Mock(side_effect=[b"partial", failure] if method == "materialize" else failure)
    monkeypatch.setattr(s3_store._s3, "get_object", Mock(return_value={"Body": body}))
    destination = tmp_path / "pkg"
    destination.mkdir()
    target = destination / "strategy.py"
    target.write_text("previous complete file")

    def perform():
        return s3_store.get(key, "strategy.py") if method == "get" else s3_store.materialize(key, destination)

    if fail_read:
        with pytest.raises(StrategyStoreError, match="ReadTimeoutError") as caught:
            perform()
        assert "secret" not in str(caught.value)
        assert target.read_text() == "previous complete file"
    else:
        perform()
    assert body.closed
    assert sorted(p.name for p in destination.iterdir()) == ["strategy.py"]


def test_s3_destination_io_failure_is_a_store_error(s3_store, tmp_path):
    key = strategy_key("io-error")
    s3_store.put(key, "strategy.py", SOURCE)
    dest = tmp_path / "file-not-directory"
    dest.write_text("keep existing file")
    with pytest.raises(StrategyStoreError, match="FileExistsError"):
        s3_store.materialize(key, dest)
    assert dest.read_text() == "keep existing file"


def test_local_delete_does_not_hide_cleanup_failure(tmp_path, monkeypatch):
    import src.integrations.strategy_store as module

    store = LocalStrategyStore(tmp_path / "store")
    monkeypatch.setattr(module.shutil, "rmtree", Mock(side_effect=PermissionError("locked")))
    with pytest.raises(StrategyStoreError, match="PermissionError"):
        store.delete(strategy_key("locked"))


def test_s3_store_feeds_the_engine_loader_end_to_end(
    s3_store: S3StrategyStore, tmp_path: Path
) -> None:
    """The worker seam: materialize into a temp dir and import through the
    unmodified engine loader, as run_job does — proven under moto."""
    from engine.strategies.user_loader import load_user_strategy
    from engine.run_single import load_strategy_class
    from src.services.strategy_validation.template import STARTER_SOURCE

    source = STARTER_SOURCE
    config = json.dumps(
        {
            "PORTFOLIO_ID": "s3-e2e",
            "TICKERS": ["AAPL"],
            "WEIGHTS": {"AAPL": 1.0},
            "INTERVAL": 60,
            "LOOKBACK_DAYS": 30,
            "DATA_FEEDS": [],
        }
    )
    key = strategy_key("s3-e2e")
    s3_store.put(key, "strategy.py", source)
    s3_store.put(key, "config.json", config)

    loaded = load_user_strategy(
        storage_key=key, store=s3_store, dest_dir=tmp_path / "work", token="e2e-token"
    )

    try:
        assert loaded.strategy_class.__name__ == "MyStrategy"
        assert not loaded.strategy_class.__abstractmethods__
        assert load_strategy_class(loaded.class_path) is loaded.strategy_class
        assert loaded.directory == tmp_path / "work"
        assert json.loads((loaded.directory / "config.json").read_text()) == json.loads(config)
    finally:
        sys.modules.pop(loaded.strategy_class.__module__, None)


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------


def test_verification_script_runs_only_against_moto(s3_bucket, capsys):
    from scripts.verify_s3_store import main

    assert main(["--bucket", s3_bucket, "--region", REGION]) == 0
    output = capsys.readouterr().out
    assert "engine loader" in output and "all checks passed" in output
    assert "prefix_probe" in output and "head_bucket" not in output
    assert _bucket_keys(s3_bucket) == []


def test_verification_script_cleans_a_failed_write(s3_bucket, monkeypatch, capsys):
    from scripts import verify_s3_store as script

    store = S3StrategyStore(s3_bucket, region=REGION)
    real_put = store.put

    def put(key, filename, content):
        real_put(key, filename, content)
        if filename == "config.json":
            raise StrategyStoreError("response lost")

    monkeypatch.setattr(store, "put", put)
    monkeypatch.setattr(script, "S3StrategyStore", lambda *a, **k: store)
    assert script.main(["--bucket", s3_bucket, "--region", REGION]) == 1
    assert "response lost" in capsys.readouterr().out
    assert _bucket_keys(s3_bucket) == []


def test_verification_script_preserves_preexisting_key(s3_bucket, monkeypatch, capsys):
    from scripts import verify_s3_store as script

    store = S3StrategyStore(s3_bucket, region=REGION)
    key = f"{script.VERIFY_PREFIX}/collision/"
    store.put(key, "notes.txt", "keep")
    monkeypatch.setattr(script.uuid, "uuid4", lambda: Mock(hex="collision"))
    assert script.main(["--bucket", s3_bucket, "--region", REGION]) == 1
    assert "left untouched" in capsys.readouterr().out
    assert store.get(key, "notes.txt") == "keep"


@pytest.mark.parametrize("prefix", ["", "team-a"])
def test_verification_script_uses_only_prefix_scoped_listing(s3_bucket, monkeypatch, capsys, prefix):
    from botocore.exceptions import ClientError
    from scripts import verify_s3_store as script

    store = S3StrategyStore(s3_bucket, prefix=prefix, region=REGION)
    client = store._s3
    head = Mock(side_effect=ClientError({"Error": {"Code": "AccessDenied"}}, "HeadBucket"))
    monkeypatch.setattr(client, "head_bucket", head)
    real_list = client.list_objects_v2
    prefixes = []

    def scoped_list(**kwargs):
        prefixes.append(kwargs.get("Prefix"))
        if not kwargs.get("Prefix", "").startswith(store.prefix + "strategies/"):
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "ListObjectsV2")
        return real_list(**kwargs)

    monkeypatch.setattr(client, "list_objects_v2", scoped_list)
    monkeypatch.setattr(script, "S3StrategyStore", lambda *args, **kwargs: store)
    assert script.main(["--bucket", s3_bucket, "--prefix", prefix, "--region", REGION]) == 0
    assert prefixes[0] == f"{store.prefix}{script.VERIFY_PREFIX}/probe/"
    head.assert_not_called()
    output = capsys.readouterr().out
    assert "prefix_probe" in output and "head_bucket" not in output
    assert _bucket_keys(s3_bucket) == []


@pytest.mark.parametrize("code", ["AccessDenied", "NoSuchBucket"])
def test_verification_script_reports_store_error_at_prefix_probe(s3_store, monkeypatch, capsys, code):
    from botocore.exceptions import ClientError
    from scripts import verify_s3_store as script

    failure = ClientError({"Error": {"Code": code}}, "ListObjectsV2")
    monkeypatch.setattr(s3_store._s3, "list_objects_v2", Mock(side_effect=failure))
    put, delete = Mock(), Mock()
    monkeypatch.setattr(s3_store._s3, "put_object", put)
    monkeypatch.setattr(s3_store._s3, "delete_objects", delete)
    monkeypatch.setattr(script, "S3StrategyStore", lambda *args, **kwargs: s3_store)
    assert script.main(["--bucket", BUCKET, "--region", REGION]) == 1
    output = capsys.readouterr().out
    assert "prefix_probe" in output and code in output
    assert "head_bucket" not in output and "round trip" not in output
    put.assert_not_called()
    delete.assert_not_called()


def _swap_settings(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    """Settings is a frozen dataclass, so the whole object is replaced."""
    import src.integrations.strategy_store as module

    monkeypatch.setattr(
        module, "settings", dataclasses.replace(module.settings, **overrides)
    )
    # Never let a cached store from another test leak across backends.
    monkeypatch.setattr(module, "_store", None)


def test_backend_selection_reads_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    _swap_settings(
        monkeypatch,
        strategy_store_backend="s3",
        strategy_store_s3_bucket="mqs-strategies",
        strategy_store_s3_prefix="/team-a/",
        strategy_store_s3_endpoint_url="http://localhost:4566",
        aws_region="us-east-2",
    )
    built = build_strategy_store()
    assert isinstance(built, S3StrategyStore)
    assert built.bucket == "mqs-strategies"
    assert built.prefix == "team-a/"
    assert built.endpoint_url == "http://localhost:4566"
    assert built.region == "us-east-2"

    _swap_settings(
        monkeypatch,
        strategy_store_backend="s3",
        strategy_store_s3_bucket="mqs-strategies",
        strategy_store_s3_prefix="",
        strategy_store_s3_endpoint_url="",
        aws_region="",
    )
    built = build_strategy_store()
    assert isinstance(built, S3StrategyStore)
    # Blank settings become None so the SDK's own defaults apply.
    assert built.prefix == ""
    assert built.endpoint_url is None
    assert built.region is None

    _swap_settings(monkeypatch, strategy_store_backend="local")
    assert isinstance(build_strategy_store(), LocalStrategyStore)

    _swap_settings(monkeypatch, strategy_store_backend="gcs")
    with pytest.raises(ValueError, match="unknown STRATEGY_STORE_BACKEND"):
        build_strategy_store()


def test_s3_backend_without_a_bucket_fails_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """One sentence, at build time — never a NoSuchBucket on the first upload."""
    from src.integrations.strategy_store import get_strategy_store

    _swap_settings(monkeypatch, strategy_store_backend="s3", strategy_store_s3_bucket="")

    with pytest.raises(ValueError, match="requires STRATEGY_STORE_S3_BUCKET"):
        build_strategy_store()
    with pytest.raises(ValueError, match="requires STRATEGY_STORE_S3_BUCKET"):
        get_strategy_store()


def test_settings_expose_the_s3_knobs() -> None:
    """The additive settings exist with blank defaults, and no credential field does."""
    from src.core.config import Settings

    names = {f.name for f in dataclasses.fields(Settings)}
    assert {"strategy_store_s3_prefix", "strategy_store_s3_endpoint_url", "aws_region"} <= names
    assert not any("secret" in n or "access_key" in n for n in names)
    # The template documents the knobs the app reads.
    template = (Path(__file__).resolve().parents[2] / ".env.example").read_text(encoding="utf-8")
    for var in ("STRATEGY_STORE_S3_BUCKET", "STRATEGY_STORE_S3_PREFIX", "STRATEGY_STORE_S3_ENDPOINT_URL"):
        assert f"{var}=" in template
    assert "AWS_SECRET_ACCESS_KEY" not in template
    assert "AWS_SECRET_ACCESS_KEY" not in os.environ.get("__never_set__", "")
