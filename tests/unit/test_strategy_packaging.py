"""Source packaging failure recovery against disk and moto; no database."""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from src.integrations.strategy_store import LocalStrategyStore, S3StrategyStore, StrategyStoreError, strategy_key
from src.services.strategy_validation import packaging
from src.services.strategy_validation.template import STARTER_SOURCE
from test_strategy_store import REGION, s3_bucket  # noqa: F401 - shared offline AWS fixture


@pytest.fixture(params=["local", "s3"])
def package_store(request, tmp_path, monkeypatch):
    if request.param == "local":
        store = LocalStrategyStore(tmp_path / "store")
    else:
        store = S3StrategyStore(request.getfixturevalue("s3_bucket"), region=REGION)
    monkeypatch.setattr(packaging, "get_strategy_store", lambda: store)
    return store


def test_package_contains_real_template_and_generated_engine_config(package_store, tmp_path):
    from engine.strategies.user_loader import load_user_strategy
    import sys

    config = packaging.build_config("packaged")
    storage = packaging.store_strategy_source("packaged", STARTER_SOURCE, config)
    assert storage == strategy_key("packaged")
    loaded = load_user_strategy(storage_key=storage, store=package_store,
                                dest_dir=tmp_path / "loaded", token="packaging")
    try:
        assert loaded.strategy_class.__name__ == "MyStrategy"
        assert not loaded.strategy_class.__abstractmethods__
        assert json.loads((loaded.directory / "config.json").read_text()) == config
    finally:
        sys.modules.pop(loaded.strategy_class.__module__, None)


@pytest.mark.parametrize("config", [{"invalid": object()}, {"invalid": float("nan")}])
def test_config_serialization_fails_before_resolving_store(monkeypatch, config):
    get_store = Mock(side_effect=AssertionError("store must not be reached"))
    monkeypatch.setattr(packaging, "get_strategy_store", get_store)
    with pytest.raises((TypeError, ValueError)):
        packaging.store_strategy_source("bad-config", STARTER_SOURCE, config)
    get_store.assert_not_called()


def test_invalid_utf8_source_fails_before_resolving_store(monkeypatch):
    get_store = Mock(side_effect=AssertionError("store must not be reached"))
    monkeypatch.setattr(packaging, "get_strategy_store", get_store)
    with pytest.raises(UnicodeEncodeError):
        packaging.store_strategy_source("bad-source", "\ud800", {})
    get_store.assert_not_called()


@pytest.mark.parametrize("failed_write", [1, 2])
@pytest.mark.parametrize("accepted_before_failure", [False, True])
def test_write_failure_cleans_only_the_new_package(package_store, monkeypatch, failed_write, accepted_before_failure):
    keep = strategy_key("new-neighbor")
    package_store.put(keep, "strategy.py", "existing unrelated source")
    failure = StrategyStoreError("write response lost")
    real_put = package_store.put
    calls = 0

    def failing_put(key, filename, content):
        nonlocal calls
        calls += 1
        if calls == failed_write:
            if accepted_before_failure:
                real_put(key, filename, content)
            raise failure
        real_put(key, filename, content)

    monkeypatch.setattr(package_store, "put", failing_put)
    with pytest.raises(StrategyStoreError) as caught:
        packaging.store_strategy_source("new", STARTER_SOURCE, packaging.build_config("new"))
    assert caught.value is failure
    assert not package_store.exists(strategy_key("new"))
    assert package_store.get(keep, "strategy.py") == "existing unrelated source"


@pytest.mark.parametrize("existing", [
    {"notes.txt": "unrelated"},
    {"strategy.py": "old source"},
    {"config.json": "old config"},
    {"strategy.py": "old source", "config.json": "old config", "notes.txt": "unrelated"},
])
def test_existing_prefix_is_never_overwritten_or_cleaned(package_store, monkeypatch, existing):
    key = strategy_key("existing")
    for filename, content in existing.items():
        package_store.put(key, filename, content)
    put, delete = Mock(), Mock()
    monkeypatch.setattr(package_store, "put", put)
    monkeypatch.setattr(package_store, "delete", delete)
    with pytest.raises(StrategyStoreError, match="existing strategy package"):
        packaging.store_strategy_source("existing", STARTER_SOURCE, packaging.build_config("existing"))
    put.assert_not_called()
    delete.assert_not_called()
    for filename, content in existing.items():
        assert package_store.get(key, filename) == content


def test_identical_package_retry_preserves_additional_objects(package_store, monkeypatch):
    config = packaging.build_config("retry")
    # Windows uploads must remain byte-identical through local reads as well
    # as S3, or a completed package is incorrectly rejected on migration retry.
    source = STARTER_SOURCE.replace("\n", "\r\n")
    key = packaging.store_strategy_source("retry", source, config)
    package_store.put(key, "notes.txt", "keep")
    put, delete = Mock(), Mock()
    monkeypatch.setattr(package_store, "put", put)
    monkeypatch.setattr(package_store, "delete", delete)
    assert packaging.store_strategy_source("retry", source, config) == key
    put.assert_not_called()
    delete.assert_not_called()
    assert package_store.get(key, "notes.txt") == "keep"


@pytest.mark.parametrize("probe", ["exists", "get"])
def test_preflight_failure_does_not_attempt_write_or_cleanup(package_store, monkeypatch, probe):
    if probe == "get":
        package_store.put(strategy_key("probe"), "strategy.py", STARTER_SOURCE)
    failure = StrategyStoreError("AccessDenied")
    monkeypatch.setattr(package_store, probe, Mock(side_effect=failure))
    put, delete = Mock(), Mock()
    monkeypatch.setattr(package_store, "put", put)
    monkeypatch.setattr(package_store, "delete", delete)
    with pytest.raises(StrategyStoreError) as caught:
        packaging.store_strategy_source("probe", STARTER_SOURCE, {})
    assert caught.value is failure
    put.assert_not_called()
    delete.assert_not_called()


def test_cleanup_failure_preserves_original_error_and_reports_orphan(package_store, monkeypatch, caplog):
    failure = StrategyStoreError("config write failed")
    real_put = package_store.put

    def put(key, filename, content):
        if filename == packaging.CONFIG_FILENAME:
            raise failure
        real_put(key, filename, content)

    monkeypatch.setattr(package_store, "put", put)
    cleanup = Mock(side_effect=StrategyStoreError("delete denied"))
    monkeypatch.setattr(package_store, "delete", cleanup)
    with pytest.raises(StrategyStoreError) as caught:
        packaging.store_strategy_source("orphan", STARTER_SOURCE, {})
    assert caught.value is failure
    assert any("Cleanup failed" in note and "stored objects may remain" in note for note in failure.__notes__)
    assert "Partial strategy package strategies/orphan/ could not be removed" in caplog.text
    cleanup.assert_called_once_with(strategy_key("orphan"))
    assert package_store.get(strategy_key("orphan"), "strategy.py") == STARTER_SOURCE
