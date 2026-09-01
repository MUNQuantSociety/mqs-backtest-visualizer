"""The database settings accept the deploy stack's variable names.

MQS_AWS_INFRA injects credentials as MARKET_DATA_* (SSM parameters); this
codebase reads POSTGRES_*. A container that only received the former used to
boot with an empty host and fail on the first backtest. These tests pin the
fallback and its precedence.

``Settings`` evaluates its defaults when the class body runs, so the module is
reloaded under a patched environment and reloaded again afterwards so the
process-wide ``settings`` object other tests import is not left pointing at
fake credentials.

    pytest tests/unit/test_config_env_fallback.py -v
"""

import importlib
from collections.abc import Iterator

import pytest

import src.core.config as config_module

_POSTGRES = ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER",
             "POSTGRES_PASSWORD", "POSTGRES_SSLMODE")
_MARKET = ("MARKET_DATA_HOST", "MARKET_DATA_PORT", "MARKET_DATA_DB", "MARKET_DATA_USER",
           "MARKET_DATA_PASSWORD", "MARKET_DATA_SSLMODE")


@pytest.fixture
def reload_config(monkeypatch: pytest.MonkeyPatch) -> Iterator:
    """Yield a function that reloads the module; restore the real one after."""
    # Blank, not deleted. ``load_dotenv(override=False)`` runs again on every
    # reload and would refill a *missing* variable from the repo's .env — but it
    # leaves a variable that exists alone, even when empty. Blank is also the
    # honest model of a container: a key the deploy did not populate arrives
    # as "", not as absent, and the fallback treats both the same.
    for name in _POSTGRES + _MARKET:
        monkeypatch.setenv(name, "")

    def reload():
        return importlib.reload(config_module)

    try:
        yield reload
    finally:
        monkeypatch.undo()
        importlib.reload(config_module)


def test_infra_names_reach_the_database_settings(
    monkeypatch: pytest.MonkeyPatch, reload_config
) -> None:
    monkeypatch.setenv("MARKET_DATA_HOST", "db.example.internal")
    monkeypatch.setenv("MARKET_DATA_PORT", "5433")
    monkeypatch.setenv("MARKET_DATA_DB", "mqsdb")
    monkeypatch.setenv("MARKET_DATA_USER", "reader")
    monkeypatch.setenv("MARKET_DATA_PASSWORD", "s3cret")
    monkeypatch.setenv("MARKET_DATA_SSLMODE", "require")

    settings = reload_config().Settings()

    assert settings.postgres_host == "db.example.internal"
    assert settings.postgres_port == 5433
    assert settings.postgres_user == "reader"
    assert settings.postgres_password == "s3cret"
    # A required-SSL deploy must stay required; nothing downgrades it.
    assert settings.postgres_sslmode == "require"
    assert settings.database_configured


def test_postgres_names_win_when_both_are_set(
    monkeypatch: pytest.MonkeyPatch, reload_config
) -> None:
    monkeypatch.setenv("MARKET_DATA_HOST", "old.example.internal")
    monkeypatch.setenv("POSTGRES_HOST", "new.example.internal")
    monkeypatch.setenv("POSTGRES_USER", "app")
    monkeypatch.setenv("MARKET_DATA_USER", "reader")

    settings = reload_config().Settings()

    assert settings.postgres_host == "new.example.internal"
    assert settings.postgres_user == "app"


def test_neither_family_means_not_configured(reload_config) -> None:
    settings = reload_config().Settings()
    assert settings.postgres_host == ""
    assert not settings.database_configured
    # Defaults still apply where a value is not a credential.
    assert settings.postgres_port == 25060
    assert settings.postgres_sslmode == "prefer"
