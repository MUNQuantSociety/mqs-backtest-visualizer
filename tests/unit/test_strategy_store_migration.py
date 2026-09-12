"""Offline cutover tests: fake registry inventory, temporary disk and moto only."""

from __future__ import annotations

import io
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from scripts import migrate_strategy_store_s3 as migration
from src.integrations.strategy_store import LocalStrategyStore, S3StrategyStore

BUCKET = "strategy-migration-test"
REGION = "us-east-1"
KEY = "strategies/existing-registry-id/"
CONTENT = {
    "strategy.py": b"\xef\xbb\xbf# source-content-secret\r\nclass Strategy: pass\r\n",
    "config.json": b'{"config-content-secret": "caf\xc3\xa9"}\r\n',
}


@pytest.fixture(autouse=True)
def offline_environment(monkeypatch):
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(migration, "create_engine", Mock(side_effect=AssertionError("live DB forbidden")))


@pytest.fixture
def stores(tmp_path, monkeypatch):
    import boto3
    from moto import mock_aws

    source = LocalStrategyStore(tmp_path / "local")
    monkeypatch.setattr(migration, "settings", SimpleNamespace(strategy_store_root=source.root, aws_region=REGION))
    with mock_aws():
        boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
        yield source, S3StrategyStore(BUCKET, prefix="cutover", region=REGION)


def local_pair(source, key=KEY, contents=None):
    for filename, content in (CONTENT if contents is None else contents).items():
        path = source._object_path(key, filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def remote_pair(destination, key=KEY, contents=None):
    for filename, content in (CONTENT if contents is None else contents).items():
        destination._s3.put_object(Bucket=BUCKET, Key=destination._object_key(key, filename), Body=content)


def bucket_objects(destination):
    listing = destination._s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])
    result = {}
    for item in listing:
        response = destination._s3.get_object(Bucket=BUCKET, Key=item["Key"])
        try:
            result[item["Key"]] = response["Body"].read()
        finally:
            response["Body"].close()
    return result


def run(stores, *, keys=(KEY,), apply=True, **kwargs):
    return migration.migrate(*stores, inventory=lambda limit: iter(keys),
                             apply=apply, api_workers_stopped=apply, **kwargs)


def test_cli_defaults_to_dry_run_and_uses_injected_inventory(stores, capsys):
    source, destination = stores
    local_pair(source)
    inventory = Mock(return_value=[KEY])
    assert migration.main(["--bucket", BUCKET, "--prefix", "cutover"], inventory=inventory) == 0
    inventory.assert_called_once_with(1000)
    assert bucket_objects(destination) == {}
    output = capsys.readouterr().out
    assert "mode=dry-run" in output and "missing=2" in output and "copied=0" in output
    assert KEY in output
    for content in CONTENT.values():
        assert content.decode("utf-8") not in output
    assert "source-content-secret" not in output and "config-content-secret" not in output


@pytest.mark.parametrize("args", [[], ["--bucket", BUCKET], ["--prefix", "cutover"],
    ["--bucket", " ", "--prefix", "/"],
    ["--bucket", BUCKET, "--prefix", "../escape"],
    ["--bucket", BUCKET, "--prefix", "cutover", "--max-packages", "0"],
    ["--bucket", BUCKET, "--prefix", "cutover", "--max-packages", "invalid"]])
def test_cli_requires_explicit_valid_target_and_bounds(args):
    inventory = Mock()
    with pytest.raises(SystemExit) as caught:
        migration.main(args, inventory=inventory)
    assert caught.value.code == 2
    inventory.assert_not_called()


def test_apply_requires_stopped_services_before_any_inventory_or_io(stores):
    inventory = Mock()
    with pytest.raises(SystemExit) as caught:
        migration.main(["--bucket", BUCKET, "--prefix", "cutover", "--apply"], inventory=inventory)
    assert caught.value.code == 2
    with pytest.raises(migration.MigrationError, match="api-workers-stopped"):
        migration.migrate(*stores, inventory=inventory, apply=True)
    inventory.assert_not_called()


def test_cli_apply_preserves_exact_bytes_and_keys(stores, capsys):
    source, destination = stores
    local_pair(source)
    before = {filename: source._object_path(KEY, filename).read_bytes() for filename in CONTENT}
    assert migration.main(["--bucket", BUCKET, "--prefix", "cutover", "--apply", "--api-workers-stopped"],
                          inventory=lambda limit: [KEY]) == 0
    assert bucket_objects(destination) == {"cutover/" + KEY + filename: data for filename, data in CONTENT.items()}
    assert {filename: source._object_path(KEY, filename).read_bytes() for filename in CONTENT} == before
    assert "copied=2 verified=1" in capsys.readouterr().out


def test_explicit_root_prefix_and_no_references_do_no_aws_io(stores, monkeypatch):
    monkeypatch.setattr(S3StrategyStore, "_s3", property(lambda self: pytest.fail("no S3 needed")))
    assert migration.main(["--bucket", BUCKET, "--prefix", "/"], inventory=lambda limit: []) == 0


def test_copies_only_referenced_pair_and_deduplicates_inventory(stores):
    source, destination = stores
    local_pair(source)
    local_pair(source, "strategies/unreferenced/")
    source.put(KEY, "notes.txt", "do not migrate")
    remote_pair(destination, "strategies/untouched/", {"notes.txt": b"keep"})
    operations = []
    destination._s3.meta.events.register(
        "before-parameter-build.s3", lambda model, **kwargs: operations.append(model.name)
    )
    report = run(stores, keys=(KEY, KEY))
    assert report.packages == 1 and report.copied == 2 and report.verified == 1
    assert set(operations) == {"GetObject", "PutObject"}
    assert operations.count("GetObject") == 6 and operations.count("PutObject") == 2
    assert bucket_objects(destination) == {
        **{"cutover/" + KEY + name: data for name, data in CONTENT.items()},
        "cutover/strategies/untouched/notes.txt": b"keep",
    }


def test_equal_destination_is_idempotent_and_never_puts(stores, monkeypatch):
    source, destination = stores
    local_pair(source)
    remote_pair(destination)
    put = Mock(side_effect=AssertionError("equal objects must not be written"))
    monkeypatch.setattr(destination._s3, "put_object", put)
    for apply in (False, True):
        report = run(stores, apply=apply)
        assert report.equal == 2 and report.missing == 0 and report.copied == 0
        assert report.verified == int(apply)
    put.assert_not_called()


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("filename", migration.FILENAMES)
def test_any_local_missing_object_blocks_all_writes_before_s3(stores, monkeypatch, apply, filename):
    source, destination = stores
    local_pair(source, "strategies/a-complete/")
    local_pair(source, "strategies/z-incomplete/", {name: data for name, data in CONTENT.items() if name != filename})
    get = Mock(side_effect=AssertionError("inventory must finish before S3"))
    monkeypatch.setattr(destination._s3, "get_object", get)
    with pytest.raises(migration.MigrationError, match="local-read-failed"):
        run(stores, keys=("strategies/a-complete/", "strategies/z-incomplete/"), apply=apply)
    get.assert_not_called()
    assert bucket_objects(destination) == {}


@pytest.mark.parametrize("apply", [False, True])
def test_late_destination_conflict_blocks_all_writes(stores, monkeypatch, apply):
    source, destination = stores
    keys = ["strategies/a-empty/", "strategies/z-conflict/"]
    for key in keys:
        local_pair(source, key)
    remote_pair(destination, keys[1], {"config.json": b"different"})
    before = bucket_objects(destination)
    put = Mock(side_effect=AssertionError("preflight must catch all conflicts"))
    monkeypatch.setattr(destination._s3, "put_object", put)
    with pytest.raises(migration.MigrationError, match="conflict-or-mismatch"):
        run(stores, keys=keys, apply=apply)
    put.assert_not_called()
    assert bucket_objects(destination) == before


@pytest.mark.parametrize("code", ["AccessDenied", "NoSuchBucket", "404"])
def test_destination_errors_are_not_treated_as_missing(stores, monkeypatch, code):
    from botocore.exceptions import ClientError

    source, destination = stores
    local_pair(source)
    monkeypatch.setattr(destination._s3, "get_object", Mock(side_effect=ClientError(
        {"Error": {"Code": code, "Message": "secret-from-service"}}, "GetObject")))
    put = Mock()
    monkeypatch.setattr(destination._s3, "put_object", put)
    with pytest.raises(migration.MigrationError, match="destination-read-failed") as caught:
        run(stores)
    assert "secret-from-service" not in str(caught.value)
    put.assert_not_called()


@pytest.mark.parametrize("equal", [False, True])
def test_conditional_put_never_overwrites_a_racing_writer(stores, monkeypatch, equal):
    source, destination = stores
    local_pair(source)
    client = destination._s3
    real_put = client.put_object
    calls = []

    def racing_put(**kwargs):
        calls.append(kwargs)
        assert kwargs["IfNoneMatch"] == "*"
        if len(calls) == 1:
            real_put(Bucket=BUCKET, Key=kwargs["Key"], Body=kwargs["Body"] if equal else b"other writer")
        return real_put(**kwargs)

    monkeypatch.setattr(client, "put_object", racing_put)
    if equal:
        report = run(stores)
        assert report.copied == 1 and report.verified == 1
    else:
        with pytest.raises(migration.MigrationError, match="conflict-or-mismatch"):
            run(stores)
        assert bucket_objects(destination) == {"cutover/" + KEY + "strategy.py": b"other writer"}
    assert len(calls) == (2 if equal else 1)


def test_interrupted_pair_retains_local_and_existing_remote_then_resumes(stores, monkeypatch):
    from botocore.exceptions import ClientError

    source, destination = stores
    local_pair(source)
    real_put = destination._s3.put_object

    def fail_config(**kwargs):
        if kwargs["Key"].endswith("config.json"):
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")
        return real_put(**kwargs)

    with monkeypatch.context() as context:
        context.setattr(destination._s3, "put_object", fail_config)
        with pytest.raises(migration.MigrationError, match="destination-write-failed"):
            run(stores)
    assert bucket_objects(destination) == {"cutover/" + KEY + "strategy.py": CONTENT["strategy.py"]}
    assert all(source._object_path(KEY, name).read_bytes() == data for name, data in CONTENT.items())
    report = run(stores)
    assert report.equal == 1 and report.copied == 1 and report.verified == 1


@pytest.mark.parametrize("present", [False, True])
def test_conditional_409_accepts_only_equal_existing_bytes_without_retrying_put(stores, monkeypatch, present):
    from botocore.exceptions import ClientError

    source, destination = stores
    if present:
        remote_pair(destination)
    put = Mock(side_effect=ClientError({"Error": {"Code": "ConditionalRequestConflict"}}, "PutObject"))
    monkeypatch.setattr(destination._s3, "put_object", put)
    if present:
        assert migration._put_missing(destination, KEY, "strategy.py", CONTENT["strategy.py"]) is False
    else:
        with pytest.raises(migration.MigrationError, match="conflict-or-mismatch"):
            migration._put_missing(destination, KEY, "strategy.py", CONTENT["strategy.py"])
    put.assert_called_once()


def test_readback_detects_corruption_even_when_put_reports_success(stores, monkeypatch):
    source, destination = stores
    local_pair(source)
    real_put = destination._s3.put_object

    def corrupt_put(**kwargs):
        kwargs["Body"] = b"corrupted"
        return real_put(**kwargs)

    monkeypatch.setattr(destination._s3, "put_object", corrupt_put)
    report = migration.Report()
    with pytest.raises(migration.MigrationError, match="conflict-or-mismatch"):
        run(stores, report=report)
    assert report.copied == 2 and report.verified == 0


def test_local_change_after_plan_blocks_writes(stores, monkeypatch):
    source, destination = stores
    local_pair(source)
    put = Mock()
    monkeypatch.setattr(destination._s3, "put_object", put)

    def change_after_plan(message):
        source._object_path(KEY, "config.json").write_bytes(b"changed")

    with pytest.raises(migration.MigrationError, match="local-changed"):
        run(stores, emit=change_after_plan)
    put.assert_not_called()


@pytest.mark.parametrize("keys", [["../escape/"], ["strategies/../escape/"], ["strategies/C:escape/"],
                                  ["strategies//alias/"], ["strategies/id"], ["strategies/"], [None]])
def test_unsafe_or_aliased_inventory_fails_without_accessing_source(stores, keys):
    with pytest.raises(migration.MigrationError, match="invalid-registry-storage-key"):
        run(stores, keys=keys)


def test_inventory_count_is_bounded_even_for_an_infinite_iterator(stores):
    import itertools

    with pytest.raises(migration.MigrationError, match="exceeds-max-packages"):
        run(stores, keys=itertools.repeat(KEY), max_packages=2)


def test_local_object_size_is_bounded_before_any_remote_access(stores, monkeypatch):
    source, destination = stores
    local_pair(source)
    monkeypatch.setattr(migration, "MAX_OBJECT_BYTES", 4)
    with pytest.raises(migration.MigrationError, match="local-object-too-large"):
        run(stores)
    assert destination._client is None


@pytest.mark.parametrize("length, content, expected", [(20, b"large", "too-large"), (3, b"x", "incomplete")])
def test_remote_read_is_bounded_and_body_is_closed(stores, monkeypatch, length, content, expected):
    source, destination = stores
    body = io.BytesIO(content)
    monkeypatch.setattr(migration, "MAX_OBJECT_BYTES", 10)
    monkeypatch.setattr(destination._s3, "get_object", Mock(return_value={"ContentLength": length, "Body": body}))
    with pytest.raises(migration.MigrationError, match=expected):
        migration._remote_bytes(destination, KEY, "config.json")
    assert body.closed


def test_remote_stream_error_closes_body_and_redacts_sdk_details(stores, monkeypatch):
    from botocore.exceptions import ReadTimeoutError

    source, destination = stores
    body = io.BytesIO(b"partial")
    body.read = Mock(side_effect=ReadTimeoutError(endpoint_url="https://secret@example.test"))
    monkeypatch.setattr(destination._s3, "get_object", Mock(return_value={"ContentLength": 7, "Body": body}))
    with pytest.raises(migration.MigrationError, match="destination-read-failed") as caught:
        migration._remote_bytes(destination, KEY, "strategy.py")
    assert body.closed and "secret" not in str(caught.value)


def test_registry_reader_uses_only_bounded_user_references_in_readonly_transaction(monkeypatch):
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.sql import Select

    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execution_options.return_value = connection
    connection.execute.return_value.scalars.return_value = iter([KEY])
    factory = Mock(return_value=engine)
    monkeypatch.setattr(migration, "create_engine", factory)
    monkeypatch.setattr(migration, "settings", SimpleNamespace(database_url_sync="postgresql://offline"))
    assert migration.read_registry_keys(17) == [KEY]
    assert factory.call_args.kwargs == {"connect_args": {"connect_timeout": 5}, "poolclass": migration.NullPool}
    connection.execution_options.assert_called_once_with(postgresql_readonly=True)
    connection.exec_driver_sql.assert_called_once_with("SET LOCAL statement_timeout = '15s'")
    statement = connection.execute.call_args.args[0]
    assert isinstance(statement, Select)
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "SELECT DISTINCT app.strategies.storage_key" in sql
    assert "app.strategies.kind =" in sql and "app.strategies.storage_key IS NOT NULL" in sql
    assert "user" in compiled.params.values() and 18 in compiled.params.values()
    assert "status" not in sql and "source_staging" not in sql and "enabled" not in sql
    connection.commit.assert_not_called()
    engine.dispose.assert_called_once()


def test_registry_engine_is_disposed_on_failure(monkeypatch):
    engine = MagicMock()
    engine.connect.side_effect = RuntimeError("fake driver credentials")
    monkeypatch.setattr(migration, "create_engine", Mock(return_value=engine))
    monkeypatch.setattr(migration, "settings", SimpleNamespace(database_url_sync="postgresql://offline"))
    with pytest.raises(RuntimeError):
        migration.read_registry_keys(1)
    engine.dispose.assert_called_once()


def test_cli_redacts_inventory_exception_and_returns_nonzero(stores, capsys):
    inventory = Mock(side_effect=RuntimeError("postgresql://admin:private-secret@host/db"))
    assert migration.main(["--bucket", BUCKET, "--prefix", "cutover"], inventory=inventory) == 1
    output = capsys.readouterr().err
    assert "registry-inventory-failed" in output
    assert "private-secret" not in output and "postgresql" not in output


def test_cli_defaults_to_production_inventory_callable_without_using_live_db(stores, monkeypatch):
    inventory = Mock(return_value=[])
    monkeypatch.setattr(migration, "read_registry_keys", inventory)
    assert migration.main(["--bucket", BUCKET, "--prefix", "/", "--max-packages", "12"]) == 0
    inventory.assert_called_once_with(12)
