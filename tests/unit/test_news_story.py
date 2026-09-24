"""GET /news/{id}/story: the card's own title and real summary paragraph."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from src.api.dependencies.current_user import require_current_user
from src.api.routes import market_context as market_context_api
from src.integrations.article_page import PageStory
from src.repositories.news_sentiment import ArticleRow
from src.services import news_story
from src.services.market_context import MarketContextUnavailable

STORED = (
    "Why Shares of Altria Group Soared in April Altria Group's shares rose 10% in April, "
    "driven by consistent earnings growth."
)
TITLE = "Why Shares of Altria Group Soared in April"


def _row(**overrides):
    fields = {
        "id": 7,
        "ticker": "MO",
        "article_url": "https://www.fool.com/investing/altria",
        "published_at": datetime(2026, 5, 2, 13, 0),
        "sentiment_score": 0.3,
        "content_summary": STORED,
    }
    return ArticleRow(**{**fields, **overrides})


def test_uses_the_publisher_summary():
    page = PageStory(title=TITLE, summary="Altria's shares rose 10% in April.")

    story = news_story.build_story(_row(), page)

    assert (story.title, story.summary, story.origin) == (
        TITLE, "Altria's shares rose 10% in April.", "publisher",
    )


def test_cuts_the_publisher_title_off_the_stored_text_when_the_page_has_no_description():
    story = news_story.build_story(_row(), PageStory(title=TITLE, summary=""))

    assert story.summary == "Altria Group's shares rose 10% in April, driven by consistent earnings growth."
    assert story.origin == "stored"


def test_title_matching_ignores_case_and_spacing():
    story = news_story.build_story(_row(), PageStory(title="why shares of  altria group soared in april", summary=""))

    assert story.origin == "stored"


def test_ignores_a_publisher_summary_that_only_repeats_the_title():
    story = news_story.build_story(_row(), PageStory(title=TITLE, summary=TITLE))

    assert story.origin == "stored"


def test_gives_no_summary_rather_than_repeat_the_title_when_the_page_cannot_be_read():
    story = news_story.build_story(_row(), None)

    assert (story.summary, story.origin) == (None, "none")
    assert story.title.startswith("Why Shares of Altria")


def test_gives_no_summary_when_the_stored_text_is_only_the_title():
    story = news_story.build_story(_row(content_summary=TITLE), PageStory(title=TITLE, summary=""))

    assert story.origin == "none"


def test_gives_no_summary_when_the_stored_text_does_not_start_with_the_page_title():
    story = news_story.build_story(_row(), PageStory(title="A different headline", summary=""))

    assert story.origin == "none"


def test_a_bot_check_page_title_does_not_rename_the_story():
    story = news_story.build_story(_row(), PageStory(title="Pardon Our Interruption", summary=""))

    assert story.title.startswith("Why Shares of Altria")


@pytest.fixture
def one_article(monkeypatch):
    news_story.clear_cache()

    @asynccontextmanager
    async def fake_scope():
        yield object()

    async def by_id(_session, article_id):
        return _row() if article_id == 7 else None

    monkeypatch.setattr(news_story, "news_session_scope", fake_scope)
    monkeypatch.setattr(news_story.news_repo, "article_by_id", by_id)
    yield
    news_story.clear_cache()


def test_fetches_a_page_once_and_remembers_it(monkeypatch, one_article):
    calls = []

    def fetch(url):
        calls.append(url)
        return PageStory(title=TITLE, summary="Summary.")

    monkeypatch.setattr(news_story.article_page, "fetch_page_story", fetch)

    asyncio.run(news_story.news_story(7))
    asyncio.run(news_story.news_story(7))

    assert calls == ["https://www.fool.com/investing/altria"]


def test_retries_a_failed_page_after_its_shorter_wait(monkeypatch, one_article):
    now = [1000.0]
    calls = []
    monkeypatch.setattr(news_story, "_clock", lambda: now[0])
    monkeypatch.setattr(
        news_story.article_page, "fetch_page_story", lambda url: calls.append(url)
    )

    asyncio.run(news_story.news_story(7))
    now[0] += news_story._MISSING_TTL_SECONDS + 1
    asyncio.run(news_story.news_story(7))

    assert len(calls) == 2


def test_unknown_article_is_not_found(one_article):
    with pytest.raises(news_story.StoryNotFound):
        asyncio.run(news_story.news_story(99))


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(market_context_api.router, prefix="/api")
    app.dependency_overrides[require_current_user] = lambda: uuid.UUID(int=1)
    return TestClient(app)


def test_route_returns_the_story(monkeypatch, client):
    async def story(article_id):
        return news_story.build_story(_row(id=article_id), PageStory(title=TITLE, summary="S."))

    monkeypatch.setattr(market_context_api.news_story_service, "news_story", story)

    response = client.get("/api/news/7/story")

    assert response.status_code == 200
    assert response.json() == {"id": "7", "title": TITLE, "summary": "S.", "origin": "publisher"}


def test_route_answers_404_for_an_unknown_article(monkeypatch, client):
    async def missing(_article_id):
        raise news_story.StoryNotFound(_article_id)

    monkeypatch.setattr(market_context_api.news_story_service, "news_story", missing)

    assert client.get("/api/news/99/story").status_code == 404


@pytest.mark.parametrize("article_id", ["0", "-3", "abc"])
def test_route_rejects_an_invalid_id(client, article_id):
    assert client.get(f"/api/news/{article_id}/story").status_code == 422


def test_route_answers_503_when_the_database_is_down(monkeypatch, client):
    async def down(_article_id):
        raise MarketContextUnavailable("News is unavailable.")

    monkeypatch.setattr(market_context_api.news_story_service, "news_story", down)

    assert client.get("/api/news/7/story").status_code == 503


def test_route_requires_a_signed_in_user():
    app = FastAPI()
    app.include_router(market_context_api.router, prefix="/api")

    assert TestClient(app).get("/api/news/7/story").status_code == 401
