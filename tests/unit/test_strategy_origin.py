"""Catalogue ``origin`` labelling and the never-failing optional identity; no network/DB."""

import asyncio
import uuid
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.dependencies import current_user
from src.api.routes import strategies as strategies_api
from src.repositories.strategies import StrategyRow
from src.schemas.auth import AuthUser
from src.schemas.strategies import StrategyListResponse, StrategyOrigin
from src.services import auth as auth_service
from src.services import strategies as strategies_service

VIEWER = uuid.UUID("00000000-0000-0000-0000-000000000001")
OTHER = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _row(kind: str, owner_id: uuid.UUID | None) -> StrategyRow:
    return StrategyRow(SimpleNamespace(
        key="k", name="k", class_path="example.Strategy", description="", status="active",
        tags=[], param_specs=[], universe=[], validation_run_id=None, authoring=None,
        kind=kind, owner_id=owner_id,
    ), 0, None, None, None)


def test_builtin_is_labelled_builtin():
    assert strategies_service.to_schema(_row("builtin", None), VIEWER).origin is StrategyOrigin.BUILTIN


def test_viewers_own_upload_is_labelled_own():
    assert strategies_service.to_schema(_row("user", VIEWER), VIEWER).origin is StrategyOrigin.OWN


def test_another_users_upload_is_labelled_community():
    assert strategies_service.to_schema(_row("user", OTHER), VIEWER).origin is StrategyOrigin.COMMUNITY


def test_unowned_legacy_upload_is_community_not_builtin():
    assert strategies_service.to_schema(_row("user", None), VIEWER).origin is StrategyOrigin.COMMUNITY


def test_anonymous_caller_sees_uploads_as_community():
    assert strategies_service.to_schema(_row("user", VIEWER), None).origin is StrategyOrigin.COMMUNITY


@pytest.fixture
def no_dev_identity(monkeypatch):
    monkeypatch.setattr(current_user, "settings", replace(
        current_user.settings, auth_allow_dev_identity=False,
    ))


def _optional(authorization: str | None) -> uuid.UUID | None:
    return asyncio.run(current_user.optional_current_user(authorization, None))


def test_optional_identity_is_none_without_credentials(no_dev_identity):
    assert _optional(None) is None


def test_optional_identity_is_none_for_a_malformed_header(no_dev_identity):
    assert _optional("Basic abc") is None


def test_optional_identity_is_none_for_a_rejected_token(no_dev_identity, monkeypatch):
    monkeypatch.setattr(auth_service, "authenticate",
                        AsyncMock(side_effect=auth_service.InvalidAccessToken("expired")))
    assert _optional("Bearer expired") is None


def test_optional_identity_is_none_during_a_provider_outage(no_dev_identity, monkeypatch):
    monkeypatch.setattr(auth_service, "authenticate",
                        AsyncMock(side_effect=auth_service.AuthenticationUnavailable("down")))
    assert _optional("Bearer token") is None


def test_optional_identity_returns_the_verified_user(no_dev_identity, monkeypatch):
    monkeypatch.setattr(auth_service, "authenticate", AsyncMock(return_value=AuthUser(id=VIEWER)))
    assert _optional("Bearer token") == VIEWER


def test_anonymous_catalogue_request_still_succeeds(no_dev_identity, monkeypatch):
    listed = AsyncMock(return_value=StrategyListResponse(items=[], total=0))
    monkeypatch.setattr(strategies_service, "list_strategies", listed)
    app = FastAPI()
    app.include_router(strategies_api.router, prefix="/api")

    response = TestClient(app).get("/api/strategies")

    assert response.status_code == 200
    listed.assert_awaited_once_with(viewer_id=None)
