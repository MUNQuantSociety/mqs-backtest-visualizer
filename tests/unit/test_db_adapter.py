"""Deployment aliases and safe error envelopes, with every connection mocked."""

import logging

import psycopg2
import pytest

from engine.data import db_adapter


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.setattr(db_adapter, "_load_env_file", lambda: None)
    for prefix in ("POSTGRES", "MARKET_DATA"):
        for suffix in ("HOST", "PORT", "DB", "USER", "PASSWORD", "SSLMODE"):
            monkeypatch.delenv(f"{prefix}_{suffix}", raising=False)
    monkeypatch.delenv("DB_CONNECT_TIMEOUT_SECONDS", raising=False)
    def forbidden(*args, **kwargs):
        pytest.fail("Adapter unit tests must never contact PostgreSQL")
    monkeypatch.setattr(db_adapter.psycopg2, "connect", forbidden)


@pytest.mark.parametrize("blank_primary", [False, True])
def test_market_data_aliases_reach_connection_kwargs(monkeypatch, blank_primary):
    aliases = {"HOST": "fixture.invalid", "PORT": "5433", "DB": "fixture", "USER": "reader", "PASSWORD": "fixture-secret", "SSLMODE": "verify-full"}
    for suffix, value in aliases.items():
        monkeypatch.setenv(f"MARKET_DATA_{suffix}", value)
        if blank_primary:
            monkeypatch.setenv(f"POSTGRES_{suffix}", "")
    adapter = db_adapter.EngineDBAdapter()
    assert adapter._conn_kwargs == {
        "host": "fixture.invalid", "port": 5433, "dbname": "fixture", "user": "reader",
        "password": "fixture-secret", "sslmode": "verify-full", "connect_timeout": 10,
    }


def test_postgres_values_and_explicit_overrides_win(monkeypatch):
    primary = {"HOST": "primary.invalid", "PORT": "5434", "DB": "primary", "USER": "primary", "PASSWORD": "primary-secret", "SSLMODE": "require"}
    aliases = {"HOST": "alias.invalid", "PORT": "5433", "DB": "alias", "USER": "alias", "PASSWORD": "alias-secret", "SSLMODE": "prefer"}
    for suffix in primary:
        monkeypatch.setenv(f"POSTGRES_{suffix}", primary[suffix])
        monkeypatch.setenv(f"MARKET_DATA_{suffix}", aliases[suffix])
    adapter = db_adapter.EngineDBAdapter()
    assert adapter._conn_kwargs == {
        "host": "primary.invalid", "port": 5434, "dbname": "primary", "user": "primary",
        "password": "primary-secret", "sslmode": "require", "connect_timeout": 10,
    }
    explicit = db_adapter.EngineDBAdapter(host="override.invalid", sslmode="verify-full")
    assert explicit._conn_kwargs["host"] == "override.invalid"
    assert explicit._conn_kwargs["sslmode"] == "verify-full"


def test_defaults_are_unchanged():
    adapter = db_adapter.EngineDBAdapter()
    assert adapter._conn_kwargs["host"] == ""
    assert adapter._conn_kwargs["port"] == 25060
    assert adapter._conn_kwargs["sslmode"] == "prefer"


def test_connection_error_has_no_secret_or_tls_downgrade(monkeypatch, caplog):
    monkeypatch.setenv("MARKET_DATA_PASSWORD", "private-test-token")
    monkeypatch.setenv("MARKET_DATA_SSLMODE", "require")
    calls = []
    def fail(**kwargs):
        calls.append(kwargs)
        raise psycopg2.OperationalError("postgres://reader:private-test-token@fixture.invalid SQL parameter private-test-token")
    monkeypatch.setattr(db_adapter.psycopg2, "connect", fail)
    with caplog.at_level(logging.ERROR):
        result = db_adapter.EngineDBAdapter().execute_query("SELECT %s", ["private-test-token"], fetch=True)
    assert result["status"] == "error"
    assert "OperationalError" in result["message"]
    assert "private-test-token" not in result["message"] + caplog.text
    assert "postgres://" not in result["message"] + caplog.text
    assert len(calls) == 1 and calls[0]["sslmode"] == "require"


def test_invalid_numeric_setting_does_not_echo_raw_value(monkeypatch):
    monkeypatch.setenv("MARKET_DATA_PORT", "private-test-token")
    with pytest.raises(ValueError) as error:
        db_adapter.EngineDBAdapter()
    assert "private-test-token" not in str(error.value)
