"""GET /news and GET /indicators: payload shape, validation and failure modes.

The database is stubbed: these tests pin what the frontend parses and what the
service promises, not the SQL.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from src.api.dependencies.current_user import require_current_user
from src.api.routes import market_context as market_context_api
from src.db import engine as engine_module
from src.repositories.news_sentiment import ArticleRow
from src.schemas.market_context import IndicatorsResponse, NewsResponse
from src.services import market_context

# Copied from Backtest_Visualiser_FE/src/features/market/types.ts.
FE_INDICATOR_KEYS = {
    "ticker", "last", "rsi14", "macdHistogram", "smaRegime", "momentum20d",
    "sentiment7d", "sentimentDelta7d", "asOf",
}
FE_ARTICLE_KEYS = {
    "id", "source", "publishedAt", "headline", "summary", "url", "tickers", "score",
}

LAST_SESSION = date(2026, 7, 15)


def _article(**overrides) -> ArticleRow:
    fields = {
        "id": 42,
        "ticker": "aapl ",
        "article_url": "https://www.reuters.com/markets/apple-beats",
        "published_at": datetime(2026, 7, 14, 13, 30),
        "sentiment_score": 0.25,
        "content_summary": "Apple beats on revenue",
    }
    return ArticleRow(**{**fields, **overrides})


def _closes(count: int) -> list[tuple[date, float]]:
    return [(LAST_SESSION - timedelta(days=count - 1 - n), 100.0 + n) for n in range(count)]


@pytest.fixture
def stub_session(monkeypatch):
    @asynccontextmanager
    async def fake_scope():
        yield object()

    monkeypatch.setattr(market_context, "session_scope", fake_scope)
    monkeypatch.setattr(market_context, "news_session_scope", fake_scope)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(market_context_api.router, prefix="/api")
    app.dependency_overrides[require_current_user] = lambda: uuid.UUID(int=1)
    return TestClient(app)


def test_news_article_carries_exactly_the_frontend_keys():
    article = market_context.to_news_article(_article())

    assert set(article.model_dump(by_alias=True)) == FE_ARTICLE_KEYS


def test_news_article_source_is_the_host_without_www():
    assert market_context.to_news_article(_article()).source == "reuters.com"


def test_news_article_source_is_unknown_without_a_url():
    assert market_context.to_news_article(_article(article_url=None)).source == "Unknown"


def test_news_article_carries_the_whole_stored_summary():
    text = "Apple beats on revenue. " * 40

    summary = market_context.to_news_article(_article(content_summary=text)).summary

    assert summary == text.strip()


def test_news_article_summary_collapses_whitespace():
    summary = market_context.to_news_article(_article(content_summary="Apple\n\n beats\t")).summary

    assert summary == "Apple beats"


def test_news_article_links_to_the_publisher_page():
    assert (
        market_context.to_news_article(_article()).url
        == "https://www.reuters.com/markets/apple-beats"
    )


@pytest.mark.parametrize(
    "link",
    [None, "", "javascript:alert(1)", "data:text/html,x", "ftp://example.com/a", "https:///no-host"],
)
def test_news_article_drops_a_link_that_is_not_http(link):
    assert market_context.to_news_article(_article(article_url=link)).url is None


def test_news_article_timestamp_is_marked_utc():
    published = market_context.to_news_article(_article()).published_at

    assert published == "2026-07-14T13:30:00+00:00"


def test_news_article_ticker_is_normalised():
    assert market_context.to_news_article(_article()).tickers == ["AAPL"]


def test_long_summary_is_cut_at_a_word_with_an_ellipsis():
    summary = "Apple " * 60

    headline = market_context.to_news_article(_article(content_summary=summary)).headline

    assert len(headline) <= market_context.HEADLINE_MAX_CHARS
    assert headline.endswith("Apple…")


def test_empty_summary_falls_back_to_the_source():
    headline = market_context.to_news_article(_article(content_summary="  ")).headline

    assert headline == "reuters.com"


def test_indicators_carry_exactly_the_frontend_keys():
    row = market_context.build_indicators("AAPL", _closes(250), [])

    assert set(row.model_dump(by_alias=True)) == FE_INDICATOR_KEYS


def test_indicators_are_computed_at_the_last_close():
    row = market_context.build_indicators("AAPL", _closes(250), [])

    assert (row.last, row.as_of) == (349.0, "2026-07-15")


def test_indicators_are_none_below_200_closes():
    assert market_context.build_indicators("AAPL", _closes(199), []) is None


def test_indicator_sentiment_excludes_news_after_the_session_close():
    close_utc = datetime(2026, 7, 15, 20, 0)  # 16:00 New York in July
    articles = [(close_utc - timedelta(hours=1), 0.4), (close_utc + timedelta(hours=1), -1.0)]

    row = market_context.build_indicators("AAPL", _closes(250), articles)

    assert row.sentiment7d == pytest.approx(0.4)


RUN_START = date(2026, 5, 1)
RUN_END = date(2026, 5, 29)
RUN_PARAMS = {"tickers": "AAPL", "start": "2026-05-01", "end": "2026-05-29"}


def _news(tickers=("AAPL",), start=RUN_START, end=RUN_END, limit=5):
    return asyncio.run(market_context.news_for_run(list(tickers), start, end, limit))


def test_run_news_rejects_a_limit_above_the_cap():
    with pytest.raises(ValueError, match="limit"):
        _news(limit=market_context.NEWS_LIMIT_MAX + 1)


def test_run_news_rejects_an_end_before_the_start():
    with pytest.raises(ValueError, match="start"):
        _news(start=RUN_END, end=RUN_START)


def test_run_news_asks_for_whole_new_york_days_of_the_run(monkeypatch, stub_session):
    received = {}

    async def between(_session, tickers, start, end, limit):
        received.update(tickers=tickers, start=start, end=end, limit=limit)
        return []

    monkeypatch.setattr(market_context.news_repo, "articles_between", between)

    _news(tickers=("msft", "AAPL", "MSFT"))

    # Midnight New York (EDT, UTC-4) on the first day through midnight after the last.
    assert received == {
        "tickers": ["MSFT", "AAPL"],
        "start": datetime(2026, 5, 1, 4, 0),
        "end": datetime(2026, 5, 30, 4, 0),
        "limit": 5,
    }


def test_run_news_accepts_a_single_day_run(monkeypatch, stub_session):
    received = {}

    async def between(_session, _tickers, start, end, _limit):
        received.update(start=start, end=end)
        return []

    monkeypatch.setattr(market_context.news_repo, "articles_between", between)

    _news(start=RUN_START, end=RUN_START)

    assert received["end"] - received["start"] == timedelta(days=1)


def test_run_news_reports_a_database_failure_as_unavailable(monkeypatch, stub_session):
    async def broken(*_args):
        raise OperationalError("SELECT", {}, Exception("connection refused"))

    monkeypatch.setattr(market_context.news_repo, "articles_between", broken)

    with pytest.raises(market_context.MarketContextUnavailable):
        _news()


def test_indicators_omit_tickers_without_enough_history(monkeypatch, stub_session):
    async def last_bar(_session, ticker):
        return None if ticker == "NEWCO" else LAST_SESSION

    async def closes(_session, _ticker, _since):
        return _closes(250)

    async def no_scores(*_args):
        return []

    monkeypatch.setattr(market_context.market_data_repo, "last_bar_date", last_bar)
    monkeypatch.setattr(market_context.market_data_repo, "daily_closes", closes)
    monkeypatch.setattr(market_context.news_repo, "scores_between", no_scores)

    response = asyncio.run(market_context.indicators_for(["AAPL", "NEWCO"]))

    assert [row.ticker for row in response.items] == ["AAPL"]


def test_news_route_returns_items_envelope(monkeypatch, client):
    async def news(*_args):
        return NewsResponse(items=[market_context.to_news_article(_article())])

    monkeypatch.setattr(market_context_api.market_context_service, "news_for_run", news)

    response = client.get("/api/news", params={**RUN_PARAMS, "limit": 8})

    assert response.status_code == 200
    assert set(response.json()["items"][0]) == FE_ARTICLE_KEYS


def test_news_route_passes_the_run_tickers_and_dates(monkeypatch, client):
    received = {}

    async def news(tickers, start, end, limit):
        received.update(tickers=tickers, start=start, end=end, limit=limit)
        return NewsResponse(items=[])

    monkeypatch.setattr(market_context_api.market_context_service, "news_for_run", news)

    client.get("/api/news", params={**RUN_PARAMS, "tickers": "AAPL,MSFT", "limit": 3})

    assert received == {
        "tickers": ["AAPL", "MSFT"],
        "start": RUN_START,
        "end": RUN_END,
        "limit": 3,
    }


@pytest.mark.parametrize("missing", ["tickers", "start", "end"])
def test_news_route_requires_the_run_tickers_and_dates(client, missing):
    params = {key: value for key, value in RUN_PARAMS.items() if key != missing}

    assert client.get("/api/news", params=params).status_code == 422


@pytest.mark.parametrize("start", ["2026-13-01", "May 1", "2026-05-01T09:30"])
def test_news_route_rejects_a_malformed_date(client, start):
    assert client.get("/api/news", params={**RUN_PARAMS, "start": start}).status_code == 422


def test_news_route_rejects_an_end_before_the_start(client):
    params = {**RUN_PARAMS, "start": "2026-05-29", "end": "2026-05-01"}

    assert client.get("/api/news", params=params).status_code == 422


@pytest.mark.parametrize("limit", [0, 51, "many"])
def test_news_route_rejects_an_out_of_range_limit(client, limit):
    assert client.get("/api/news", params={**RUN_PARAMS, "limit": limit}).status_code == 422


def test_news_route_rejects_malformed_tickers(client):
    response = client.get("/api/news", params={**RUN_PARAMS, "tickers": "AAPL,DROP TABLE"})

    assert response.status_code == 422


def test_news_route_answers_503_when_the_database_is_down(monkeypatch, client):
    async def down(*_args):
        raise market_context.MarketContextUnavailable("News is unavailable.")

    monkeypatch.setattr(market_context_api.market_context_service, "news_for_run", down)

    response = client.get("/api/news", params=RUN_PARAMS)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "30"


def test_indicators_route_requires_tickers(client):
    assert client.get("/api/indicators").status_code == 422


def test_indicators_route_rejects_an_unsupported_window(client):
    response = client.get("/api/indicators", params={"tickers": "AAPL", "window": "30d"})

    assert response.status_code == 422


def test_indicators_route_returns_items_envelope(monkeypatch, client):
    async def indicators(_tickers):
        row = market_context.build_indicators("AAPL", _closes(250), [])
        return IndicatorsResponse(items=[row])

    monkeypatch.setattr(market_context_api.market_context_service, "indicators_for", indicators)

    response = client.get("/api/indicators", params={"tickers": "AAPL", "window": "7d"})

    assert response.status_code == 200
    assert set(response.json()["items"][0]) == FE_INDICATOR_KEYS


def test_indicators_route_answers_503_when_the_database_is_down(monkeypatch, client):
    async def down(_tickers):
        raise market_context.MarketContextUnavailable("Indicators are unavailable.")

    monkeypatch.setattr(market_context_api.market_context_service, "indicators_for", down)

    assert client.get("/api/indicators", params={"tickers": "AAPL"}).status_code == 503


def test_routes_require_a_signed_in_user():
    app = FastAPI()
    app.include_router(market_context_api.router, prefix="/api")

    response = TestClient(app).get("/api/news", params=RUN_PARAMS)

    assert response.status_code == 401


def test_indicators_skip_the_news_database_when_no_ticker_has_history(monkeypatch, stub_session):
    async def no_bars(_session, _ticker):
        return None

    @asynccontextmanager
    async def news_must_not_open():
        pytest.fail("the live news database was opened for nothing")
        yield

    monkeypatch.setattr(market_context.market_data_repo, "last_bar_date", no_bars)
    monkeypatch.setattr(market_context, "news_session_scope", news_must_not_open)

    assert asyncio.run(market_context.indicators_for(["NEWCO"])).items == []


def test_news_is_unavailable_when_the_live_database_is_not_configured(monkeypatch):
    blank = replace(engine_module.settings, news_postgres_host="", news_postgres_user="")
    monkeypatch.setattr(engine_module, "settings", blank)

    with engine_module.detached_async_engine():
        with pytest.raises(market_context.MarketContextUnavailable):
            _news()


def test_news_never_falls_back_to_the_app_database(monkeypatch):
    blank = replace(engine_module.settings, news_postgres_host="", news_postgres_user="")
    monkeypatch.setattr(engine_module, "settings", blank)
    monkeypatch.setattr(
        market_context, "session_scope", lambda: pytest.fail("news read the app database")
    )

    with engine_module.detached_async_engine():
        with pytest.raises(market_context.MarketContextUnavailable):
            _news()


def test_news_connections_open_in_read_only_mode(monkeypatch):
    configured = replace(
        engine_module.settings,
        news_postgres_host="live.example",
        news_postgres_user="reader",
        news_postgres_password="not-a-real-password",
    )
    captured = {}

    def fake_create_async_engine(url, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(engine_module, "settings", configured)
    monkeypatch.setattr(engine_module, "create_async_engine", fake_create_async_engine)
    monkeypatch.setattr(engine_module, "async_sessionmaker", lambda **_kwargs: object())

    with engine_module.detached_async_engine():
        engine_module.get_news_session_factory()

    server_settings = captured["connect_args"]["server_settings"]
    assert server_settings["default_transaction_read_only"] == "on"


def test_news_session_rolls_back_and_never_commits(monkeypatch):
    calls = []

    class RecordingSession:
        async def commit(self):
            calls.append("commit")

        async def rollback(self):
            calls.append("rollback")

        async def close(self):
            calls.append("close")

    monkeypatch.setattr(engine_module, "get_news_session_factory", lambda: RecordingSession)

    async def use_session():
        async with engine_module.news_session_scope():
            pass

    asyncio.run(use_session())

    assert calls == ["rollback", "close"]
