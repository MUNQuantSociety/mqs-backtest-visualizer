"""The authenticated application identity exposed to the frontend."""

import uuid

from src.schemas.common import CamelModel


class AuthUser(CamelModel):
    id: uuid.UUID
    email: str | None = None
    display_name: str | None = None
