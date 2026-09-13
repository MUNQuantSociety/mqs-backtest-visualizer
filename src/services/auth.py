"""Map a verified provider identity to the application's stable owner UUID."""

import asyncio

from src.db.engine import session_scope
from src.db.init import ensure_schema
from src.integrations.cognito import AuthenticationUnavailable, InvalidAccessToken, get_verifier
from src.repositories import users
from src.schemas.auth import AuthUser


def validate_configuration(configuration) -> None:
    """Fail production startup on invalid config, without making a JWKS request."""
    if (configuration.app_env.lower() not in {"development", "test"}
            or configuration.auth_cognito_issuer or configuration.auth_cognito_client_id):
        get_verifier(configuration.auth_cognito_issuer, configuration.auth_cognito_client_id)


async def authenticate(token: str, *, issuer: str, client_id: str) -> AuthUser:
    verifier = get_verifier(issuer, client_id)
    identity = await asyncio.to_thread(verifier.verify, token)
    await ensure_schema()
    async with session_scope() as session:
        user = await users.get_or_create_user(session, issuer=identity.issuer, subject=identity.subject)
        # No unverified profile claims or legacy passwords enter this mapping.
        return AuthUser(id=user.id, email=user.email, display_name=user.display_name)
