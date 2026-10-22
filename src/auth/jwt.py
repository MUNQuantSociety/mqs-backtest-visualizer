import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
from jwt.exceptions import InvalidTokenError

from src.auth import config 

SECRET_KEY = os.environ["SECRET_KEY"]
ALGORITHM = os.environ["ALGORITHM"]

ACCESS_TOKEN_EXPIRE_MINUTES = int(os.environ["ACCESS_TOKEN_EXPIRE_MINUTES"])

TEMP_TOKEN_EXPIRE_MINUTES = int(os.environ["TEMP_TOKEN_EXPIRE_MINUTES"])

REFRESH_TOKEN_EXPIRE_DAYS = int(os.environ["REFRESH_TOKEN_EXPIRE_DAYS"])


def create_access_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

    payload = {
        "sub": user_id,
        "type": "access",
        "exp": expire,
    }

    return jwt.encode(
        payload,
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def create_refresh_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)

    jti = str(uuid4())

    payload = {
        "sub": user_id,
        "type": "refresh",
        "exp": expire,
        "jti": jti,
    }

    return jwt.encode(
        payload,
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def obtain_token_pair(user_id: str) -> dict[str, str]:
    return {
        "access": create_access_token(user_id),
        "refresh": create_refresh_token(user_id),
    }


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM],
            options={
                "require": ["sub", "exp", "type"],
            },
        )

    except InvalidTokenError:
        return None


def verify_access_token(
    access_token: str,
) -> dict | None:
    payload = decode_token(access_token)

    if payload is None:
        return None

    if payload["type"] != "access":
        return None

    if not payload["sub"]:
        return None

    return {
        "user_id": payload["sub"],
    }


def verify_refresh_token(
    refresh_token: str,
) -> dict | None:
    payload = decode_token(refresh_token)

    if payload is None:
        return None

    if payload["type"] != "refresh":
        return None

    if not isinstance(payload.get("jti"), str) or not payload["jti"]:
        return None

    try:
        payload["expires_at"] = datetime.fromtimestamp(payload["exp"], timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return None

    return {
        "user_id": payload["sub"],
        "jti": payload["jti"],
        "expires_at": payload["expires_at"],
    }


def create_discord_temp_token(
    discord_user_id: str,
    roles: list[str],
) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=TEMP_TOKEN_EXPIRE_MINUTES)

    payload = {
        "sub": discord_user_id,
        "roles": roles,
        "type": "discord_registration",
        "exp": expire,
    }

    return jwt.encode(
        payload,
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def verify_discord_temp_token(
    discord_temp_token: str,
) -> dict | None:
    payload = decode_token(discord_temp_token)

    if payload is None:
        return None

    if payload["type"] != "discord_registration":
        return None

    if not payload["sub"]:
        return None

    return {
        "discord_user_id": payload["sub"],
        "roles": payload.get("roles", []),
    }
