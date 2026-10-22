from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from psycopg2.errors import UniqueViolation
from psycopg2.extensions import connection as Connection

from src.auth.db import transaction
from src.auth.jwt import (
    obtain_token_pair,
    verify_discord_temp_token,
    verify_refresh_token,
)
from src.auth.passwords import hash_password, verify_password
from src.auth.repository import (
    consume_refresh_token,
    create_user,
    get_role_via_name,
    get_third_party_user,
    get_user_via_email,
)
from src.auth.schemas import (
    DiscordCallbackRequest,
    DiscordRegisterRequest,
    LoginRequest,
    TokenRefreshRequest,
)
from src.auth.services.discord import handle_discord_callback

router = APIRouter(prefix="/auth", tags=["auth"])


def get_connection():
    with transaction() as connection:
        yield connection


@router.post("/login")
def login_with_password(
    data: LoginRequest,
    connection: Annotated[Connection, Depends(get_connection, scope="function")],
) -> dict[str, str]:

    user = get_user_via_email(connection, str(data.email))

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    if not verify_password(
        data.password,
        user["hashed_password"],
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    return obtain_token_pair(str(user["user_id"]))


# ------------------------------------------------------------------
# REFRESH TOKEN
# ------------------------------------------------------------------


@router.post("/token/refresh")
def token_refresh(
    data: TokenRefreshRequest,
    connection: Annotated[Connection, Depends(get_connection, scope="function")],
) -> dict[str, str]:

    token_data = verify_refresh_token(data.refresh_token)

    if token_data is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )

    if not consume_refresh_token(
        connection, token_data["jti"], token_data["expires_at"]
    ):
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    return obtain_token_pair(token_data["user_id"])


# ------------------------------------------------------------------
# DISCORD CALLBACK
# ------------------------------------------------------------------


@router.post("/discord/callback")
def discord_callback(
    data: DiscordCallbackRequest,
) -> dict[str, str]:

    # TODO:
    # Add OAuth state validation before production.

    registration_token = handle_discord_callback(data.code)

    return {
        "registration_token": registration_token,
    }


# ------------------------------------------------------------------
# DISCORD REGISTRATION
# ------------------------------------------------------------------


@router.post("/discord/register")
def register_with_discord(
    data: DiscordRegisterRequest,
    connection: Annotated[Connection, Depends(get_connection, scope="function")],
) -> dict[str, str]:

    # Verify the short-lived token created after
    # successful Discord guild verification.
    token_data = verify_discord_temp_token(data.discord_temp_token)

    if token_data is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired Discord registration token",
        )

    # Ensure password confirmation matches.
    if data.password != data.confirm_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Passwords do not match",
        )

    email = str(data.email).lower()

    # Check if email already belongs to an account.
    if get_user_via_email(connection, email) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this email already exists",
        )

    discord_user_id = token_data["discord_user_id"]

    # Check our separate third-party identity map.
    if (
        get_third_party_user(
            connection,
            provider="discord",
            provider_user_id=discord_user_id,
        )
        is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This Discord account is already registered",
        )

    # Roles in the temporary JWT have already been mapped
    # from Discord role IDs -> our application role names.
    mapped_roles = []

    for role_name in set(token_data.get("roles", [])):
        role = get_role_via_name(connection, role_name)

        if role is not None:
            mapped_roles.append(role)

    # Since a valid application role is required,
    # don't create a user without one.
    if not mapped_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No valid application role assigned",
        )

    user_id = str(uuid4())

    try:
        create_user(
            connection,
            user_id,
            email,
            hash_password(data.password),
            discord_user_id,
            mapped_roles,
        )
    except UniqueViolation as exc:
        raise HTTPException(
            status_code=409, detail="Email or Discord account is already registered"
        ) from exc

    # Registration succeeded, so immediately
    # authenticate the newly created user.
    return obtain_token_pair(user_id)
