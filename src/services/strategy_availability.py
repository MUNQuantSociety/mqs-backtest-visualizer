"""Check registered packages without importing or executing stored Python."""

from __future__ import annotations

import asyncio
import logging
import time

from src.integrations.strategy_store import get_strategy_store

logger = logging.getLogger(__name__)


async def package_available(storage_key: str | None) -> bool:
    """Both files must exist. Outages propagate as 503, not an empty catalogue."""
    if not storage_key:
        logger.info("STORAGE CHECK | No published package reference; excluding from S3 catalogue")
        return False
    return await asyncio.to_thread(_package_available, storage_key)


def _package_available(storage_key: str) -> bool:
    store = get_strategy_store()
    started = time.perf_counter()
    logger.info("STORAGE CHECK | Checking source/config; key=%s", storage_key)
    try:
        # A prefix can exist while an interrupted upload contains only one file.
        # Never execute source while answering a catalogue request.
        store.get(storage_key, "strategy.py")
        store.get(storage_key, "config.json")
    except KeyError:
        logger.warning("STORAGE CHECK | Package missing/incomplete; key=%s", storage_key)
        return False
    except Exception:
        logger.error("STORAGE CHECK | Package check failed; key=%s", storage_key)
        raise
    logger.info("STORAGE CHECK | Source/config present; key=%s elapsed_ms=%.0f", storage_key, (time.perf_counter() - started) * 1000)
    return True
