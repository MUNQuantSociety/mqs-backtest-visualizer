from pydantic import BaseModel, EmailStr


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
