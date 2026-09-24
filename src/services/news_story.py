"""A news story's own title and summary paragraph, for its card.

The stored text is title and summary joined, with no mark where the title ends,
so the summary is read from the publisher's page instead (see
``src/integrations/article_page.py``). When the page gives a title but no
description, that title is exactly the prefix to cut off the stored text.
When the page cannot be read at all, the card gets no summary rather than a
guess that repeats or truncates the title.

Pages are fetched on demand, a few at a time, and remembered: a story does not
change, and one card opening should not cost a publisher a request each time.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import logging
import time

from sqlalchemy.exc import SQLAlchemyError

from src.db.engine import NewsDatabaseNotConfigured, news_session_scope
from src.integrations import article_page
from src.integrations.article_page import PageStory
from src.repositories import news_sentiment as news_repo
from src.repositories.news_sentiment import ArticleRow
from src.schemas.market_context import NewsStory
from src.services.market_context import MarketContextUnavailable, to_news_article

logger = logging.getLogger(__name__)

_FOUND_TTL_SECONDS = 24 * 3600
# A failed read is retried later, not on every click.
_MISSING_TTL_SECONDS = 15 * 60
_CACHE_SIZE = 512
_CONCURRENT_FETCHES = 4

_cache: OrderedDict[int, tuple[float, PageStory | None]] = OrderedDict()
_fetch_slots = asyncio.Semaphore(_CONCURRENT_FETCHES)
_clock = time.monotonic


class StoryNotFound(LookupError):
    """No scored article has that id."""


def _normalised(text: str) -> str:
    return " ".join(text.split()).casefold()


def _stored_summary(stored: str, title: str) -> str | None:
    """The stored text after ``title``, when the stored text starts with it."""
    words = stored.split()
    title_words = title.split()
    if not title_words or len(words) <= len(title_words):
        return None
    if _normalised(" ".join(words[: len(title_words)])) != _normalised(title):
        return None
    return " ".join(words[len(title_words) :])


def build_story(row: ArticleRow, page: PageStory | None) -> NewsStory:
    """Choose the card's title and summary from the page and the stored row. Pure.

    The page's title is trusted only alongside a real description, or when the
    stored text starts with it. A bot-check or consent page ("Pardon Our
    Interruption") has a title but neither, and must not rename the story.
    """
    headline = to_news_article(row).headline
    stored = " ".join((row.content_summary or "").split())
    page_title = page.title if page else ""
    if page and page.summary and _normalised(page.summary) != _normalised(page_title):
        return NewsStory(
            id=str(row.id), title=page_title or headline, summary=page.summary, origin="publisher"
        )
    summary = _stored_summary(stored, page_title) if page_title else None
    if summary:
        return NewsStory(id=str(row.id), title=page_title, summary=summary, origin="stored")
    return NewsStory(id=str(row.id), title=headline, summary=None, origin="none")


def _remembered(article_id: int) -> tuple[bool, PageStory | None]:
    entry = _cache.get(article_id)
    if entry is None:
        return False, None
    expires, story = entry
    if expires <= _clock():
        del _cache[article_id]
        return False, None
    _cache.move_to_end(article_id)
    return True, story


def _remember(article_id: int, story: PageStory | None) -> None:
    ttl = _FOUND_TTL_SECONDS if story else _MISSING_TTL_SECONDS
    _cache[article_id] = (_clock() + ttl, story)
    _cache.move_to_end(article_id)
    while len(_cache) > _CACHE_SIZE:
        _cache.popitem(last=False)


async def _page_for(row: ArticleRow) -> PageStory | None:
    known, story = _remembered(row.id)
    if known:
        return story
    if not row.article_url:
        story = None
    else:
        async with _fetch_slots:
            story = await asyncio.to_thread(article_page.fetch_page_story, row.article_url)
    _remember(row.id, story)
    return story


async def news_story(article_id: int) -> NewsStory:
    """The title and real summary paragraph for one article.

    Raises:
        StoryNotFound: no scored article has ``article_id``.
        MarketContextUnavailable: the news database could not be read.
    """
    try:
        async with news_session_scope() as session:
            row = await news_repo.article_by_id(session, article_id)
    except (SQLAlchemyError, OSError, NewsDatabaseNotConfigured) as exc:
        logger.error("News story query failed: %s", type(exc).__name__)
        raise MarketContextUnavailable("News is unavailable. Please try again later.") from exc
    if row is None:
        raise StoryNotFound(article_id)
    return build_story(row, await _page_for(row))


def clear_cache() -> None:
    """Forget every fetched page. For tests."""
    _cache.clear()
