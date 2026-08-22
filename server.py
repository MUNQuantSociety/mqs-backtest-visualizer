"""ASGI entrypoint — the FastAPI app the frontend talks to.

Run with:  uvicorn server:app --reload --port 8000

The frontend calls ``/api/*``. In development Vite proxies that prefix to this
server (see the frontend's ``vite.config.ts``), so the browser sees a same-origin
URL and CORS is never exercised. CORS is configured anyway for the case where the
app is served from a different origin than the API.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.router import api_router
from src.core.config import settings
from src.workers.job_manager import application_lifespan

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Backend for the MQS backtest visualizer.",
    # Schema creation belongs to startup, not to whichever request happens to
    # arrive first: a CREATE SCHEMA / create_all against the production
    # instance is not something to run on the request path. The composed
    # lifespan also builds the worker pool, in that order — the reconciler it
    # runs first has to find the tables it corrects. Without this, POST
    # /backtests would insert rows nothing ever picks up.
    lifespan=application_lifespan,
)

# Browsers block :5173 → :8000 unless the API allows the frontend origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.api_prefix)


# Kept from the first scaffold commit so anything already pointing at the
# versioned path keeps working. New routes belong on api_router.
@app.get("/api/v1/health", tags=["meta"])
async def health_v1() -> dict[str, str]:
    return {"status": "ok"}
