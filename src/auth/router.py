from fastapi import APIRouter, HTTPException, status
from pwdlib import PasswordHash
from pydantic import BaseModel, EmailStr

from src.auth.jwt import obtain_token_pair

router = APIRouter(prefix="/auth", tags=["auth"])

password_hash = PasswordHash.recommended()


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


# DUMMY USER DATA, NEEDS TO CONNECT WITH SQLALCHEMY AND ALL LATER ON
users = {
    "test@example.com": {
        "user_id": "550e8400-e29b-41d4-a716-446655440000",
        "email": "test@example.com",
        "password": password_hash.hash("password123"),
    }
}


def get_user_via_email(email: str) -> dict | None:
    return users.get(email)


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:

    return password_hash.verify(plain_password, hashed_password)


@router.post("/login")
def login_with_password(data: LoginRequest) -> dict[str, str]:
    user = get_user_via_email(str(data.email))

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password"
        )

    if not verify_password(data.password, user["password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password"
        )

    return obtain_token_pair(user["user_id"])
