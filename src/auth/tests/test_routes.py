"""Exercise auth alone, without importing the rest of the application."""

import importlib

import pytest
from fastapi import HTTPException


@pytest.fixture
def auth(monkeypatch):
    defaults = {
        "SECRET_KEY": "test-secret-with-at-least-thirty-two-characters",
        "ALGORITHM": "HS256",
        "ACCESS_TOKEN_EXPIRE_MINUTES": "5",
        "TEMP_TOKEN_EXPIRE_MINUTES": "2",
        "REFRESH_TOKEN_EXPIRE_DAYS": "30",
        "DISCORD_BASE_URL": "https://example.invalid",
        "DISCORD_REDIRECT_URI": "https://example.invalid/callback",
        "DISCORD_CLIENT_ID": "test",
        "DISCORD_CLIENT_SECRET": "test",
        "DISCORD_GUILD_ID": "test",
    }
    for key, value in defaults.items():
        monkeypatch.setenv(key, value)
    routes = importlib.import_module("src.auth.router")
    return routes


def test_login_uses_database_user_and_rejects_missing_user(auth, monkeypatch):
    routes = auth
    monkeypatch.setattr(routes, "get_user_via_email", lambda *_: None)
    data = routes.LoginRequest(email="someone@example.com", password="secret")
    with pytest.raises(HTTPException) as error:
        routes.login_with_password(data, object())
    assert error.value.status_code == 401
    monkeypatch.setattr(
        routes,
        "get_user_via_email",
        lambda *_: {
            "user_id": "user-1",
            "hashed_password": routes.hash_password("secret"),
        },
    )
    response = routes.login_with_password(data, object())
    assert routes.verify_refresh_token(response["refresh"])["user_id"] == "user-1"


def test_refresh_reuse_is_rejected(auth, monkeypatch):
    routes = auth
    consumed = set()

    def consume(connection, jti, expiry):
        if jti in consumed:
            return False
        consumed.add(jti)
        return True

    monkeypatch.setattr(routes, "consume_refresh_token", consume)
    token = routes.obtain_token_pair("user-1")["refresh"]
    data = routes.TokenRefreshRequest(refresh_token=token)
    assert "access" in routes.token_refresh(data, object())
    with pytest.raises(HTTPException) as error:
        routes.token_refresh(data, object())
    assert error.value.status_code == 401
