"""Real signed-token verification with fake trusted-JWKS transport; no network/DB."""

import asyncio
import io
import json
import time
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.error import URLError

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from src.api.dependencies import current_user
from src.api.routes import auth, backtests
from src.api.routes import market_data as market_data_api, strategies as strategies_api
from src.integrations.cognito import AuthenticationUnavailable, CognitoVerifier, InvalidAccessToken, get_verifier
from src.models import AppUser, BacktestRun
from src.repositories import runs, strategies, users
from src.schemas.backtests import BacktestListResponse
from src.services import auth as auth_service
from src.services import backtests as backtests_service

ISSUER = "https://cognito-idp.us-east-2.amazonaws.com/us-east-2_TestPool"
CLIENT = "testclient123"
OWNER = uuid.UUID("00000000-0000-0000-0000-000000000001")
OTHER = uuid.UUID("00000000-0000-0000-0000-000000000002")


@pytest.fixture(scope="module")
def keys():
    return [rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2)]


def _jwk(key, kid="trusted"):
    return dict(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True), kid=kid, use="sig", alg="RS256")


def _token(key, *, claims=None, remove=(), headers=None):
    now = int(time.time())
    payload = dict(iss=ISSUER, sub="opaque-subject/not-a-uuid", iat=now - 10,
                   exp=now + 600, client_id=CLIENT, token_use="access")
    payload.update(claims or {})
    for name in remove:
        payload.pop(name)
    return jwt.encode(payload, key, algorithm="RS256", headers=dict(kid="trusted", **(headers or {})))


@pytest.fixture
def transport(monkeypatch, keys):
    state = SimpleNamespace(jwks={"keys": [_jwk(keys[0])]}, calls=[], fail=False)

    def open_request(request, timeout):
        state.calls.append((request.full_url, timeout))
        if state.fail:
            raise URLError("simulated trusted endpoint outage")
        return io.BytesIO(json.dumps(state.jwks).encode())

    monkeypatch.setattr("jwt.jwks_client.urllib.request.build_opener",
                        lambda *args: SimpleNamespace(open=open_request))
    get_verifier.cache_clear()
    yield state
    get_verifier.cache_clear()


def test_valid_access_uses_configured_jwks_and_opaque_subject(keys, transport):
    verifier = CognitoVerifier(ISSUER, CLIENT)
    token = _token(keys[0], claims={"aud": "optional-resource-binding"},
                   headers={"jku": "https://attacker.invalid/keys", "x5u": "https://attacker.invalid/cert"})
    identity = verifier.verify(token)
    assert identity.issuer == ISSUER and identity.subject == "opaque-subject/not-a-uuid"
    assert verifier.verify(token) == identity
    assert transport.calls == [(ISSUER + "/.well-known/jwks.json", 5)]


@pytest.mark.parametrize("claims", [
    {"iss": ISSUER + "other"}, {"client_id": "anotherclient"},
    {"token_use": "id", "aud": CLIENT}, {"token_use": "refresh"},
    {"exp": 1}, {"iat": 4_000_000_000}, {"iat": True}, {"exp": "4000000000"},
    {"sub": ""}, {"sub": 123}, {"sub": "x" * 2049},
])
def test_wrong_claims_rejected(keys, transport, claims):
    with pytest.raises(InvalidAccessToken):
        CognitoVerifier(ISSUER, CLIENT).verify(_token(keys[0], claims=claims))


@pytest.mark.parametrize("claim", ["iss", "sub", "exp", "iat", "client_id", "token_use"])
def test_required_claims_rejected_if_missing(keys, transport, claim):
    with pytest.raises(InvalidAccessToken):
        CognitoVerifier(ISSUER, CLIENT).verify(_token(keys[0], remove=[claim]))


def test_signature_algorithm_and_malformed_tokens_rejected(keys, transport):
    verifier = CognitoVerifier(ISSUER, CLIENT)
    for token in (_token(keys[1]), "invalid.jwt", "x" * 16_385,
                  jwt.encode({"sub": "attacker"}, "secret-at-least-thirty-two-bytes!!", algorithm="HS256", headers={"kid": "trusted"}),
                  jwt.encode({"sub": "attacker"}, None, algorithm="none", headers={"kid": "trusted"})):
        with pytest.raises(InvalidAccessToken):
            verifier.verify(token)


def test_unknown_key_cooldown_rotation_and_cached_key_during_outage(keys, transport):
    verifier = CognitoVerifier(ISSUER, CLIENT)
    known = _token(keys[0])
    verifier.verify(known)
    rotated = jwt.encode(jwt.decode(known, options={"verify_signature": False}), keys[1],
                         algorithm="RS256", headers={"kid": "rotated"})
    for _ in range(3):
        with pytest.raises(InvalidAccessToken):
            verifier.verify(rotated)
    assert len(transport.calls) == 1  # unknown keys cannot force a fetch per request
    transport.jwks["keys"].append(_jwk(keys[1], "rotated"))
    verifier.jwks._last_successful_fetch -= 31
    assert verifier.verify(rotated).issuer == ISSUER
    assert len(transport.calls) == 2
    transport.fail = True
    assert verifier.verify(known).issuer == ISSUER
    assert len(transport.calls) == 2


def test_key_endpoint_outage_fails_closed(keys, transport):
    transport.fail = True
    with pytest.raises(AuthenticationUnavailable):
        CognitoVerifier(ISSUER, CLIENT).verify(_token(keys[0]))


@pytest.mark.parametrize("issuer,client", [("", ""), (ISSUER, ""), ("", CLIENT),
    ("http://cognito-idp.us-east-2.amazonaws.com/us-east-2_Test", CLIENT),
    ("https://attacker.invalid/pool", CLIENT), (ISSUER + "?redirect=evil", CLIENT)])
def test_invalid_configuration_never_fetches(transport, issuer, client):
    with pytest.raises(AuthenticationUnavailable):
        CognitoVerifier(issuer, client)
    assert transport.calls == []


@pytest.fixture
def api(monkeypatch, transport):
    configuration = SimpleNamespace(app_env="production", auth_allow_dev_identity=False,
        auth_cognito_issuer=ISSUER, auth_cognito_client_id=CLIENT, temporary_user_id=str(OTHER))
    monkeypatch.setattr(current_user, "settings", configuration)

    @asynccontextmanager
    async def scope():
        yield object()

    mapping = AsyncMock(side_effect=lambda session, issuer, subject:
        AppUser(id=OWNER if subject == "opaque-subject/not-a-uuid" else OTHER,
                issuer=issuer, subject=subject, email=None, display_name=None))
    monkeypatch.setattr(auth_service, "session_scope", scope)
    monkeypatch.setattr(auth_service, "ensure_schema", AsyncMock())
    monkeypatch.setattr(auth_service.users, "get_or_create_user", mapping)
    onboarding = AsyncMock()
    monkeypatch.setattr(
        auth_service.starter_reports, "ensure_starter_reports", onboarding
    )
    mapping.onboarding = onboarding
    legacy = AsyncMock(side_effect=AssertionError("JWT auth must not access legacy credentials"))
    monkeypatch.setattr(current_user.user_creds_repo, "get_user", legacy)
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    app.include_router(backtests.router, prefix="/api")
    app.include_router(strategies_api.router, prefix="/api")
    app.include_router(market_data_api.router, prefix="/api")
    with TestClient(app) as client:
        yield client, configuration, mapping


def test_me_returns_mapped_user_and_never_uses_forged_owner(keys, api):
    client, _, mapping = api
    response = client.get("/api/auth/me", headers={"Authorization": "Bearer " + _token(keys[0]), "X-User-Id": str(OTHER)})
    assert response.status_code == 200
    assert response.json() == {"id": str(OWNER), "email": None, "displayName": None}
    assert response.headers["cache-control"] == "private, no-store"
    assert mapping.await_args.kwargs == {"issuer": ISSUER, "subject": "opaque-subject/not-a-uuid"}
    mapping.onboarding.assert_awaited_once()


@pytest.mark.parametrize("environment", ["production", "staging", "development", "test"])
def test_header_and_temporary_fallback_disabled_by_default(api, environment):
    client, configuration, mapping = api
    configuration.app_env = environment
    configuration.auth_cognito_issuer = configuration.auth_cognito_client_id = ""
    for headers in ({}, {"X-User-Id": str(OWNER)}):
        assert client.get("/api/auth/me", headers=headers).status_code == 401
    mapping.assert_not_awaited()


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_development_flag_cannot_enable_production_bypass(api, environment):
    client, configuration, _ = api
    configuration.app_env = environment
    configuration.auth_allow_dev_identity = True
    configuration.auth_cognito_issuer = configuration.auth_cognito_client_id = ""
    assert client.get("/api/auth/me", headers={"X-User-Id": str(OWNER)}).status_code == 401


@pytest.mark.parametrize("authorization", ["", "Basic abc", "Bearer", "Bearer abc def", "Bearer invalid"])
def test_bad_authorization_never_uses_legacy_fallback(api, authorization):
    client, configuration, _ = api
    configuration.app_env = "test"
    configuration.auth_allow_dev_identity = True
    response = client.get("/api/auth/me", headers={"Authorization": authorization, "X-User-Id": str(OWNER)})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_auth_config_and_network_failures_are_generic_503(api, keys, transport):
    client, configuration, _ = api
    headers = {"Authorization": "Bearer " + _token(keys[0])}
    configuration.auth_cognito_client_id = ""
    assert client.get("/api/auth/me", headers=headers).status_code == 503
    configuration.auth_cognito_client_id = CLIENT
    transport.fail = True
    response = client.get("/api/auth/me", headers=headers)
    assert response.status_code == 503
    assert response.json() == {"detail": "Sign-in is temporarily unavailable."}


def test_bearer_owner_reaches_list_and_other_owners_resources_remain_404(api, keys, monkeypatch):
    client, _, _ = api
    listing = AsyncMock(return_value=BacktestListResponse(items=[], total=0, page=1, page_size=25))
    monkeypatch.setattr(backtests_service, "list_backtests", listing)
    # Keep the real service ownership path. Only the DB and absent job manager
    # are replaced; capture the actual owner-scoped repository SQL predicates.
    statements = []
    async def execute(statement):
        statements.append(statement)
        return SimpleNamespace(scalar_one_or_none=lambda: None, rowcount=0)
    @asynccontextmanager
    async def scope():
        yield SimpleNamespace(execute=execute)
    monkeypatch.setattr(backtests_service, "ensure_schema", AsyncMock())
    monkeypatch.setattr(backtests_service, "session_scope", scope)
    monkeypatch.setattr("src.workers.job_manager.get_job_manager", Mock(side_effect=RuntimeError("not started")))
    client.headers.update({"Authorization": "Bearer " + _token(keys[0]), "X-User-Id": str(OTHER)})
    assert client.get("/api/backtests").status_code == 200
    assert listing.await_args.kwargs["owner_id"] == OWNER
    prefix = f"/api/backtests/{uuid.uuid4()}"
    for suffix in ("", "/equity?period=max&endDate=2026-01-01", "/exports/report.json"):
        assert client.get(prefix + suffix).status_code == 404
    assert client.delete(prefix).status_code == 404
    assert len(statements) == 4
    for statement in statements:
        compiled = statement.compile(dialect=postgresql.dialect())
        assert "owner_id =" in str(compiled)
        assert OWNER in compiled.params.values() and OTHER not in compiled.params.values()


def test_anonymous_protected_routes_reject_before_service_calls(api):
    client, _, _ = api
    prefix = f"/api/backtests/{uuid.uuid4()}"
    for method, path in (("get", "/api/backtests"), ("post", "/api/backtests"),
        ("get", prefix), ("get", prefix + "/equity?period=max&endDate=2026-01-01"),
        ("get", prefix + "/exports/report.json"), ("delete", prefix),
        # Every route that creates or destroys a strategy, reads back its
        # source, or spends provider quota is gated the same way. Catalogue
        # reads and checks stay open.
        ("post", "/api/strategies"), ("post", "/api/strategies/upload"),
        ("post", "/api/strategies/draft"), ("delete", "/api/strategies/user-x-1"),
        ("get", "/api/strategies/user-x-1/source"),
        ("get", "/api/market-data/validate-tickers?tickers=AAPL")):
        assert client.request(method, path).status_code == 401, (method, path)


def test_legacy_repository_owner_predicate_and_public_catalogue_privacy():
    statement = select(BacktestRun)
    scoped = runs.for_user(statement, OWNER).compile(dialect=postgresql.dialect())
    assert "owner_id =" in str(scoped) and OWNER in scoped.params.values()
    assert runs.for_user(statement, None) is statement
    catalogue = strategies._catalogue_statement().compile(dialect=postgresql.dialect())
    assert "backtest_report" not in str(catalogue) and "backtest_run" not in str(catalogue)
    assert list(catalogue.params.values()) == [0, None, None, None]


def test_existing_user_lookup_does_not_write(monkeypatch):
    existing = AppUser(id=OWNER, issuer=ISSUER, subject="opaque")
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: existing)))
    assert asyncio.run(users.get_or_create_user(session, issuer=ISSUER, subject="opaque")) is existing
    session.execute.assert_awaited_once()
    assert str(session.execute.await_args.args[0]).startswith("SELECT")


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_production_startup_rejects_missing_configuration_before_workers(monkeypatch, transport, environment):
    import server
    monkeypatch.setattr(server, "settings", SimpleNamespace(app_env=environment,
        auth_cognito_issuer="", auth_cognito_client_id=""))
    lifespan = Mock(side_effect=AssertionError("Workers must not start with invalid auth configuration"))
    monkeypatch.setattr(server, "application_lifespan", lifespan)
    with pytest.raises(AuthenticationUnavailable):
        with TestClient(FastAPI(lifespan=server.authenticated_lifespan)):
            pass
    lifespan.assert_not_called()
    assert transport.calls == []


def test_valid_startup_checks_configuration_without_network(transport):
    auth_service.validate_configuration(SimpleNamespace(app_env="production",
        auth_cognito_issuer=ISSUER, auth_cognito_client_id=CLIENT))
    auth_service.validate_configuration(SimpleNamespace(app_env="development",
        auth_cognito_issuer="", auth_cognito_client_id=""))
    assert transport.calls == []
