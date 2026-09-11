"""Stored built-ins use the same package path as uploads, with no local fallback."""

import asyncio
import hashlib
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from scripts.publish_builtin_strategies import packages_for, publish_packages
from src.integrations import strategy_store
from src.integrations.strategy_store import S3StrategyStore, StrategyStoreError
from src.repositories.strategies import StrategyRow
from src.services import backtests, strategies, strategy_availability
from src.services.run_controls import split_controls
from src.workers import run_job as worker


@pytest.fixture
def stored(monkeypatch):
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "testing")
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    import boto3
    from moto import mock_aws

    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="builtin-pipeline-test")
        store = S3StrategyStore("builtin-pipeline-test", prefix="development", region="us-east-1")
        monkeypatch.setattr(strategy_store, "_store", store)
        monkeypatch.setattr(worker, "settings", replace(worker.settings, strategy_store_backend="s3"))
        monkeypatch.setattr(strategies, "settings", replace(strategies.settings, strategy_store_backend="s3"))
        monkeypatch.setattr(backtests, "settings", replace(backtests.settings, strategy_store_backend="s3"))
        yield store


def test_publication_is_dry_by_default_and_repeatable(stored):
    packages = packages_for(["portfolio_1", "portfolio_2"])
    publish_packages(packages, stored, apply=False)
    assert not stored.exists("strategies/portfolio_1/")
    publish_packages(packages, stored, apply=True)
    publish_packages(packages, stored, apply=True)
    for package in packages:
        for filename, content in package["files"].items():
            assert stored.get(package["storage_key"], filename) == content


def test_publication_refuses_different_content_before_writing_any_package(stored):
    stored.put("strategies/portfolio_2/", "strategy.py", "existing source")
    with pytest.raises(ValueError, match="overwrite different"):
        publish_packages(packages_for(["portfolio_1", "portfolio_2"]), stored, apply=True)
    assert not stored.exists("strategies/portfolio_1/")
    assert stored.get("strategies/portfolio_2/", "strategy.py") == "existing source"


def test_revision_keeps_original_package_and_materializes_corrected_source(stored):
    stored.put("strategies/portfolio_1/", "strategy.py", "original source retained for old runs")
    packages = packages_for(["portfolio_1"], revision=True)
    publish_packages(packages, stored, apply=True)
    publish_packages(packages, stored, apply=True)
    assert stored.get("strategies/portfolio_1/", "strategy.py") == "original source retained for old runs"
    class_path, workdir = worker._resolve_class_path(uuid4(), "portfolio_1", "builtin", "invalid:Local", packages[0]["storage_key"])
    try:
        assert class_path.endswith(":VolMomentum")
        assert (workdir / "strategy.py").read_bytes() == packages[0]["files"]["strategy.py"].encode()
    finally:
        worker._remove_workdir(workdir)
        sys.modules.pop(class_path.split(":")[0], None)


def test_publication_resumes_an_equal_partial_package(stored):
    packages = packages_for(["portfolio_1"])
    stored.put(packages[0]["storage_key"], "strategy.py", packages[0]["files"]["strategy.py"])
    assert not asyncio.run(strategy_availability.package_available(packages[0]["storage_key"]))
    publish_packages(packages, stored, apply=True)
    assert asyncio.run(strategy_availability.package_available(packages[0]["storage_key"]))


@pytest.mark.parametrize("key,expected_class", [("portfolio_1", "VolMomentum"), ("portfolio_2", "MomentumStrategy")])
def test_worker_downloads_each_builtin_and_applies_selected_values(stored, tmp_path, monkeypatch, key, expected_class):
    packages = packages_for([key])
    publish_packages(packages, stored, apply=True)
    # A broken local class path proves resolution does not silently use imports.
    run_id = uuid4()
    class_path, workdir = worker._resolve_class_path(
        run_id, key, "builtin", "not_a_real_local_module:Missing", packages[0]["storage_key"]
    )
    try:
        assert class_path.endswith(":" + expected_class)
        assert (workdir / "strategy.py").read_bytes() == packages[0]["files"]["strategy.py"].encode()
        params, controls, tickers = split_controls(
            {"universe": ["CRWV", "NBIS"], "slippageBps": 5, "commissionPerShare": 0.005},
            packages[0]["row"]["universe"], "event",
        )
        context = worker._RunContext(
            run_id, key, class_path, date(2025, 4, 1), date(2025, 11, 7),
            200_000, "event", {**params, **controls}, workdir, packages[0]["storage_key"],
        )
        monkeypatch.setattr(worker, "settings", replace(worker.settings, artifact_dir=tmp_path / "artifacts"))
        request = worker._build_request(context, SimpleNamespace(on_progress=lambda *a: None, should_cancel=lambda: False))
        assert request.initial_capital == 200_000
        assert request.params["TICKERS"] == tickers == ["CRWV", "NBIS"]
        assert request.params["WEIGHTS"] == {"CRWV": 0.5, "NBIS": 0.5}
        assert request.slippage == 0.0005
        assert request.commission_per_share == 0.005
        provenance = worker._strategy_source(context)
        assert provenance["backend"] == "s3"
        assert provenance["sourceSha256"] == hashlib.sha256(packages[0]["files"]["strategy.py"].encode()).hexdigest()
    finally:
        worker._remove_workdir(workdir)
        sys.modules.pop(class_path.split(":")[0], None)


def test_s3_builtin_never_falls_back_to_local_source(stored):
    with pytest.raises(RuntimeError, match="not been published"):
        worker._resolve_class_path(uuid4(), "portfolio_1", "builtin", "engine.strategies.portfolio_1.strategy.VolMomentum", None)
    from engine.strategies.user_loader import UserStrategyError
    with pytest.raises(UserStrategyError, match="nothing stored"):
        worker._resolve_class_path(uuid4(), "portfolio_1", "builtin", "engine.strategies.portfolio_1.strategy.VolMomentum", "strategies/portfolio_1/")


def test_legacy_local_builtin_still_resolves(monkeypatch):
    monkeypatch.setattr(worker, "settings", replace(worker.settings, strategy_store_backend="local"))
    assert worker._resolve_class_path(uuid4(), "p", "builtin", "existing:Strategy", None) == ("existing:Strategy", None)


@asynccontextmanager
async def fake_session():
    yield None


def registry_row(key, storage_key):
    return StrategyRow(SimpleNamespace(
        key=key, name=key, class_path="example.Strategy", description="", status="active",
        tags=[], param_specs=[], universe=["AAPL"], validation_run_id=None,
        storage_key=storage_key, enabled=True,
    ), 0, None, None, None)


def test_catalogue_is_registered_complete_s3_packages_not_arbitrary_objects(stored, monkeypatch):
    publish_packages(packages_for(["portfolio_1", "portfolio_2"]), stored, apply=True)
    stored.put("strategies/partial/", "strategy.py", "not executed")
    stored.put("strategies/unregistered/", "strategy.py", "raise RuntimeError('must not execute')")
    stored.put("strategies/unregistered/", "config.json", "{}")
    rows = [registry_row(key, "strategies/" + key + "/") for key in ("portfolio_1", "portfolio_2", "partial", "absent")]
    rows.append(registry_row("unpublished_builtin", None))
    monkeypatch.setattr(strategies, "ensure_schema", AsyncMock())
    monkeypatch.setattr(strategies, "session_scope", fake_session)
    monkeypatch.setattr(strategies.strategies_repo, "list_strategies", AsyncMock(return_value=rows))
    reply = asyncio.run(strategies.list_strategies())
    assert [item.id for item in reply.items] == ["portfolio_1", "portfolio_2"]
    assert reply.total == 2


def test_storage_outage_is_not_reported_as_missing(stored, monkeypatch):
    def broken(*args):
        raise StrategyStoreError("storage offline")
    monkeypatch.setattr(stored, "get", broken)
    with pytest.raises(StrategyStoreError):
        asyncio.run(strategy_availability.package_available("strategies/portfolio_1/"))


@pytest.mark.parametrize("key", [None, "strategies/missing/"])
def test_direct_submission_rejects_unavailable_package(stored, monkeypatch, key):
    monkeypatch.setattr(backtests, "ensure_schema", AsyncMock())
    monkeypatch.setattr(backtests, "session_scope", fake_session)
    monkeypatch.setattr(backtests.strategies_repo, "get_strategy", AsyncMock(return_value=registry_row("portfolio_1", key).strategy))
    with pytest.raises(backtests.RunSubmissionError, match="no complete package"):
        asyncio.run(backtests._load_runnable_strategy("portfolio_1"))


def test_coverage_queries_use_ticker_timestamp_index_shape():
    from src.repositories.market_data import _EARLIEST_BAR_SQL, _LATEST_BAR_SQL
    for query, direction in ((_EARLIEST_BAR_SQL, "ASC"), (_LATEST_BAR_SQL, "DESC")):
        assert f'WHERE ticker = :ticker ORDER BY "timestamp" {direction} LIMIT 1' in str(query)
        assert "SELECT date" in str(query)


def test_new_ticker_intersection_rejects_screenshot_window(monkeypatch):
    from src.services import market_data
    monkeypatch.setenv("MARKET_DATA_SOURCE", "database")
    spans = {"CRWV": (date(2025, 3, 28), date(2026, 5, 26)), "NBIS": (date(2024, 10, 21), date(2025, 11, 7))}
    monkeypatch.setattr(market_data, "session_scope", fake_session)
    monkeypatch.setattr(market_data.market_data_repo, "ticker_coverage", AsyncMock(return_value=spans))
    coverage = asyncio.run(market_data.coverage_for(list(spans)))
    assert (coverage.start, coverage.end) == ("2025-03-28", "2025-11-07")
    with pytest.raises(backtests.RunSubmissionError, match="2025-03-28 to 2025-11-07"):
        asyncio.run(backtests._validated_coverage(list(spans), date(2024, 7, 15), date(2026, 7, 15)))
