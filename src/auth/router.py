from uuid import uuid4

from fastapi import APIRouter, HTTPException, status
from pwdlib import PasswordHash
from pydantic import BaseModel, EmailStr

from src.auth.jwt import (
    blacklist_refresh_token,
    obtain_token_pair,
    verify_discord_temp_token,
    verify_refresh_token,
)
from src.auth.services.discord import handle_discord_callback


router = APIRouter(prefix="/auth", tags=["auth"])

password_hash = PasswordHash.recommended()


# ------------------------------------------------------------------
# REQUEST MODELS
# ------------------------------------------------------------------

class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenRefreshRequest(BaseModel):
    refresh_token: str


class DiscordRegisterRequest(BaseModel):
    discord_temp_token: str
    email: EmailStr
    password: str
    confirm_password: str

class DiscordCallbackRequest(BaseModel):
    code: str


# ------------------------------------------------------------------
# DUMMY DATA
# TODO: Replace all of these with database tables
# ------------------------------------------------------------------

# TODO: Replace with User table
users = {
    "test@example.com": {
        "user_id": "550e8400-e29b-41d4-a716-446655440000",
        "email": "test@example.com",
        "hashed_password": password_hash.hash("password123"),
    }
}


# TODO: Replace with Role table
roles = {
    "role-member": {
        "role_id": "role-member",
        "name": "member",
    },
    "role-developer": {
        "role_id": "role-developer",
        "name": "developer",
    },
    "role-admin": {
        "role_id": "role-admin",
        "name": "admin",
    },
}


# TODO: Replace with UserRole table
user_roles: list[dict[str, str]] = [
    {
        "user_id": "550e8400-e29b-41d4-a716-446655440000",
        "role_id": "role-member",
    }
]


# TODO: Replace with ThirdPartyAccount table
#
# Future DB idea:
#
# ThirdPartyAccount
# -----------------
# user_id
# provider
# provider_user_id
#
# UNIQUE(provider, provider_user_id)
#
third_party_accounts = {
    (
        "discord",
        "123456789012345678",
    ): {
        "user_id": "550e8400-e29b-41d4-a716-446655440000",
    }
}


# ------------------------------------------------------------------
# DUMMY USER HELPERS
# ------------------------------------------------------------------

def get_user_via_email(email: str) -> dict | None:
    return users.get(email.lower())


def get_third_party_user(
    provider: str,
    provider_user_id: str,
) -> dict | None:
    return third_party_accounts.get(
        (provider, provider_user_id)
    )


def get_role_via_name(role_name: str) -> dict | None:
    for role in roles.values():
        if role["name"] == role_name:
            return role

    return None


# ------------------------------------------------------------------
# PASSWORD HELPERS
# ------------------------------------------------------------------

def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(
    plain_password: str,
    hashed_password: str,
) -> bool:
    return password_hash.verify(
        plain_password,
        hashed_password,
    )


# ------------------------------------------------------------------
# LOGIN
# ------------------------------------------------------------------

@router.post("/login")
def login_with_password(
    data: LoginRequest,
) -> dict[str, str]:

    user = get_user_via_email(
        str(data.email)
    )

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

    return obtain_token_pair(
        user["user_id"]
    )


# ------------------------------------------------------------------
# REFRESH TOKEN
# ------------------------------------------------------------------

@router.post("/token/refresh")
def token_refresh(
    data: TokenRefreshRequest,
) -> dict[str, str]:

    token_data = verify_refresh_token(
        data.refresh_token
    )

    if token_data is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )

    blacklist_refresh_token(
        token_data["jti"]
    )

    return obtain_token_pair(
        token_data["user_id"]
    )


# ------------------------------------------------------------------
# DISCORD CALLBACK
# ------------------------------------------------------------------

@router.post("/discord/callback")
def discord_callback(
    data: DiscordCallbackRequest,
) -> dict[str, str]:

    # TODO:
    # Add OAuth state validation before production.

    registration_token = handle_discord_callback(
        data.code
    )

    return {
        "registration_token": registration_token,
    }


# ------------------------------------------------------------------
# DISCORD REGISTRATION
# ------------------------------------------------------------------

@router.post("/discord/register")
def register_with_discord(
    data: DiscordRegisterRequest,
) -> dict[str, str]:

    # Verify the short-lived token created after
    # successful Discord guild verification.
    token_data = verify_discord_temp_token(
        data.discord_temp_token
    )

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
    if get_user_via_email(email) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this email already exists",
        )

    discord_user_id = token_data["discord_user_id"]

    # Check our separate third-party identity map.
    if get_third_party_user(
        provider="discord",
        provider_user_id=discord_user_id,
    ) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This Discord account is already registered",
        )

    # Roles in the temporary JWT have already been mapped
    # from Discord role IDs -> our application role names.
    mapped_roles = []

    for role_name in set(
        token_data.get("roles", [])
    ):
        role = get_role_via_name(
            role_name
        )

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

    # TODO:
    # Replace with database User creation.
    users[email] = {
        "user_id": user_id,
        "email": email,
        "hashed_password": hash_password(
            data.password
        ),
    }

    # TODO:
    # Replace with ThirdPartyAccount database creation.
    third_party_accounts[
        (
            "discord",
            discord_user_id,
        )
    ] = {
        "user_id": user_id,
    }

    # TODO:
    # Replace with UserRole database creation.
    for role in mapped_roles:
        user_roles.append(
            {
                "user_id": user_id,
                "role_id": role["role_id"],
            }
        )

    # Registration succeeded, so immediately
    # authenticate the newly created user.
    return obtain_token_pair(
        user_id
    )
