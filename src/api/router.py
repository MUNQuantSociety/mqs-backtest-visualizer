"""Root API router.

Mounted at ``settings.api_prefix`` ("/api"), which is what the frontend's
``VITE_API_BASE_URL`` resolves to. The client sends ``/api/backtests``; Vite's
dev proxy forwards it here unchanged, so the paths below are the public
contract.
"""

from __future__ import annotations

from fastapi import APIRouter

from src.api.routes import backtests, portfolios, strategies, system

api_router = APIRouter()
api_router.include_router(backtests.router)
api_router.include_router(strategies.router)
api_router.include_router(portfolios.router)
api_router.include_router(system.router)


@api_router.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    """Liveness probe. Cheap on purpose — no database, no engine, no I/O."""
    return {"status": "ok"}
