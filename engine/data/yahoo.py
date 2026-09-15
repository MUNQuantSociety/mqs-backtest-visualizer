"""Symbol suggestions from Yahoo Finance, for when FMP cannot answer.

Unofficial and unsupported: the ``v1/finance/search`` endpoint is what the
Yahoo Finance site itself calls, not a published API, so it needs a browser
User-Agent and can change without notice. (The older ``autoc.finance.yahoo``
endpoint that prototypes used to call is gone — HTTP 404 as of 2026-09.)

That is why this is a fallback for *suggestions only*. A symbol chosen from
here is still verified with FMP before it joins a universe, so nothing Yahoo
says is ever trusted on its own; it only helps the person find the spelling.
"""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from engine.contracts.errors import EngineError

SEARCH_ENDPOINT = "https://query2.finance.yahoo.com/v1/finance/search"
# Enough that the US-only filter downstream still leaves a page.
SEARCH_LIMIT = 25


class YahooUnavailable(EngineError):
    """The fallback could not answer either; the caller decides what that means."""


def search_symbols(query: str, *, limit: int = SEARCH_LIMIT) -> list[dict]:
    """Yahoo's quotes for ``query``: ``symbol``, ``shortname``, ``exchDisp``, ``quoteType``."""
    url = SEARCH_ENDPOINT + "?" + urlencode({
        "q": query, "quotesCount": limit, "newsCount": 0, "listsCount": 0, "enableFuzzyQuery": "false",
    })
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    try:
        with urlopen(request, timeout=6) as response:
            payload = json.loads(response.read(512_000))
    except HTTPError as exc:
        exc.close()
        raise YahooUnavailable(f"Yahoo symbol search failed (HTTP {exc.code}).") from None
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise YahooUnavailable(f"Yahoo symbol search failed: {type(exc).__name__}.") from None
    quotes = payload.get("quotes") if isinstance(payload, dict) else None
    if not isinstance(quotes, list) or any(not isinstance(row, dict) for row in quotes):
        raise YahooUnavailable("Yahoo symbol search returned an unexpected payload.")
    return quotes
