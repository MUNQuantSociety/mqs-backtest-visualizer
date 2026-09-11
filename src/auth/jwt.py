from datetime import datetime, timedelta, timezone

import jwt

# TODO: Move to .env
SECRET_KEY = "09d25e094faa6ca2556c818166b7a9563b93f7099f6f0f4caa6cf63b88e8d3e7"
ALGORITHM = "HS256"

ACCESS_TOKEN_EXPIRE_MINUTES = 5
REFRESH_TOKEN_EXPIRE_DAYS = 30


"""
@params: user_id as string
Note this function does not care if the user_id is valid and will
always spit out a token for whatever user_id was provided

@return: access_token for the user_id provided as string
"""


def create_access_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

    payload = {"sub": user_id, "type": "access", "exp": expire}

    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


"""
@params: user_id as string
Note this function does not care if the user_id is valid and will
always spit out a token for whatever user_id was provided

@return: refresh_token for the user_id provided as string
"""


def create_refresh_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)

    payload = {"sub": user_id, "type": "refresh", "exp": expire}

    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


"""
@params: user_id as string
@returns: access and refresh token
Intentional kept boring because why not!
"""


def obtain_token_pair(user_id: str) -> dict[str, str]:
    return {
        "access": create_access_token(user_id),
        "refresh": create_refresh_token(user_id),
    }


def verify_accesss_token(access_token: str) -> bool:
    return False
