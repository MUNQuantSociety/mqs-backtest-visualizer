"""ASGI entrypoint — the FastAPI app the frontend talks to.

Run with: python -X faulthandler -u -m uvicorn server:app --host 127.0.0.1 --port 8000

The frontend calls ``/api/*``. In development Vite proxies that prefix to this
server (see the frontend's ``vite.config.ts``), so the browser sees a same-origin
URL and CORS is never exercised. CORS is configured anyway for the case where the
app is served from a different origin than the API.
"""

import faulthandler
import logging
import time
import uuid

# Native TLS faults bypass Python exception handlers. Keep thread stacks
# visible in the launching terminal without dumping locals or credentials.
if not faulthandler.is_enabled():
    faulthandler.enable(all_threads=True)

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api.router import api_router
from src.core.config import settings
from src.integrations.strategy_store import StrategyStoreError
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


@app.middleware("http")
async def log_request(request: Request, call_next):
    """Show arrivals immediately, then their outcome; never log bodies/tokens."""
    logger = logging.getLogger("src.api.requests")
    request_id = uuid.uuid4().hex[:12]
    started = time.perf_counter()
    logger.info("HTTP IN | request=%s %s %s", request_id, request.method, request.url.path)
    try:
        response = await call_next(request)
    except Exception as exc:
        logger.error(
            "HTTP ERROR | request=%s %s %s error_type=%s elapsed_ms=%.0f",
            request_id, request.method, request.url.path, type(exc).__name__,
            (time.perf_counter() - started) * 1000,
        )
        raise
    log = logger.warning if response.status_code >= 400 else logger.info
    log(
        "HTTP OUT | request=%s %s %s status=%d elapsed_ms=%.0f",
        request_id, request.method, request.url.path, response.status_code,
        (time.perf_counter() - started) * 1000,
    )
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(StrategyStoreError)
async def strategy_storage_unavailable(
    request: Request, exc: StrategyStoreError
) -> JSONResponse:
    """Give upload clients a retryable error without exposing bucket details."""
    logging.getLogger(__name__).error(
        "Strategy storage unavailable on %s: %s", request.url.path, exc
    )
    return JSONResponse(
        status_code=503,
        content={"detail": "Strategy storage is unavailable. Please try again later."},
        headers={"Retry-After": "30"},
    )


# Kept from the first scaffold commit so anything already pointing at the
# versioned path keeps working. New routes belong on api_router.
@app.get("/api/v1/health", tags=["meta"])
async def health_v1() -> dict[str, str]:
    return {"status": "ok"}
