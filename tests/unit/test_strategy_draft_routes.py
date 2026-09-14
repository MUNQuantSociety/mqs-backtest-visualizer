"""The fragment endpoints: check, submit, and the extended template.

Step 2 of ``docs/ondata-only-authoring-plan.md``, and test 9 of its list. The
behaviour worth protecting is that a draft is not a second pipeline: once
assembled it goes through exactly what an uploaded file goes through.
"""

import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.dependencies.current_user import require_current_user
from src.api.routes.strategies import router
from src.schemas.strategies import MAX_BODY_BYTES, StrategySubmissionResult
from src.services import strategies as strategies_service

OWNER = uuid.UUID(int=1)


@pytest.fixture
def api() -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[require_current_user] = lambda: OWNER
    return TestClient(app)


GOOD_BODY = "for ticker in self.tickers:\n    asset = context.Market[ticker]"


class TestCheckDraft:
    def test_a_clean_fragment_passes_and_returns_the_assembled_file(self, api):
        response = api.post("/api/strategies/check/draft", json={"body": GOOD_BODY})

        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["issues"] == []
        # The editor renders this rather than assembling a preview itself.
        assert "class MyStrategy(BasePortfolio):" in payload["assembledSource"]
        assert payload["bodyOffset"] >= 1

    def test_the_fragments_own_line_number_comes_back(self, api):
        response = api.post(
            "/api/strategies/check/draft", json={"body": "pass\npass\nimport os"}
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is False
        # Line 3 of what they wrote, not line 15 of a file they never saw.
        assert [issue["line"] for issue in payload["issues"] if "os" in issue["message"]] == [3]

    def test_incompatible_is_still_a_200(self, api):
        # Same rule as the full-file check: the request was fine, the answer is
        # no, and a 4xx would flatten a list of problems into one string.
        assert api.post("/api/strategies/check/draft", json={"body": "import os"}).status_code == 200

    def test_indicators_reach_the_generated_class(self, api):
        response = api.post(
            "/api/strategies/check/draft",
            json={
                "body": GOOD_BODY,
                "indicators": [
                    {"attribute": "fast_sma", "indicator": "SimpleMovingAverage", "params": {"period": 20}}
                ],
            },
        )

        assert '"fast_sma": ("SimpleMovingAverage"' in response.json()["assembledSource"]

    def test_an_oversized_body_is_refused_before_assembly(self, api):
        response = api.post(
            "/api/strategies/check/draft", json={"body": "x = 1\n" * MAX_BODY_BYTES}
        )

        assert response.status_code in (413, 422)

    def test_a_bad_attribute_name_is_a_field_error(self, api):
        # `self.not an identifier[...]` could not parse, so it is refused as a
        # malformed request rather than assembled into a broken file.
        response = api.post(
            "/api/strategies/check/draft",
            json={
                "body": GOOD_BODY,
                "indicators": [{"attribute": "not an identifier", "indicator": "X", "params": {}}],
            },
        )

        assert response.status_code == 422


class TestReservedNames:
    """STATE and INDICATORS entries become ``self.<name>`` via setattr, after
    ``__init__`` — a draft naming ``executor`` would replace the real one."""

    def _check(self, api, **fields):
        return api.post("/api/strategies/check/draft", json={"body": GOOD_BODY, **fields})

    @pytest.mark.parametrize("key", ["executor", "tickers", "OnData", "AddIndicator", "STATE"])
    def test_a_state_key_the_base_class_owns_is_refused(self, api, key):
        response = self._check(api, state={key: 0})

        assert response.status_code == 422
        assert "reserved" in response.text

    @pytest.mark.parametrize("key", ["not an identifier", "class", "None", "for"])
    def test_a_state_key_that_is_not_a_usable_identifier_is_refused(self, api, key):
        response = self._check(api, state={key: 0})

        assert response.status_code == 422
        assert "identifiers" in response.text

    def test_an_indicator_attribute_that_is_a_keyword_is_refused(self, api):
        response = self._check(
            api, indicators=[{"attribute": "class", "indicator": "SimpleMovingAverage", "params": {}}]
        )

        assert response.status_code == 422
        assert "keyword" in response.text

    def test_an_indicator_attribute_the_base_class_owns_is_refused(self, api):
        response = self._check(
            api, indicators=[{"attribute": "tickers", "indicator": "SimpleMovingAverage", "params": {}}]
        )

        assert response.status_code == 422
        assert "reserved" in response.text

    def test_a_name_used_as_both_state_and_indicator_is_refused(self, api):
        response = self._check(
            api,
            state={"fast": 0},
            indicators=[{"attribute": "fast", "indicator": "SimpleMovingAverage", "params": {}}],
        )

        assert response.status_code == 422
        assert "both" in response.text

    def test_an_ordinary_state_key_still_passes(self, api):
        response = self._check(api, state={"last_signal": {}, "cooldown_until": None})

        assert response.status_code == 200


class TestSubmitDraft:
    def test_it_delegates_to_the_upload_path(self, api, monkeypatch):
        submit = AsyncMock(
            return_value=StrategySubmissionResult(
                id="user-x-1", name="Mine", status="draft", message="queued",
                validation_run_id=None,
            )
        )
        monkeypatch.setattr(strategies_service, "submit_strategy", submit)

        response = api.post(
            "/api/strategies/draft",
            json={"name": "Mine", "description": "d", "body": GOOD_BODY},
        )

        assert response.status_code == 201
        # The assembled file is what gets submitted — same scan, same store,
        # same validation backtest as an upload. No forked pipeline.
        sent = submit.await_args.args[0]
        assert "class MyStrategy(BasePortfolio):" in sent.source
        assert GOOD_BODY.splitlines()[0] in sent.source
        assert sent.name == "Mine"
        # The draft is attributed to its author exactly as an upload is.
        assert submit.await_args.kwargs["owner_id"] == OWNER

    def test_a_nameless_draft_is_refused(self, api):
        assert api.post("/api/strategies/draft", json={"body": GOOD_BODY}).status_code == 422


class TestTemplate:
    def test_it_serves_the_fragment_half_too(self, api):
        payload = api.get("/api/strategies/template").json()

        assert payload["filename"] == "strategy.py"
        assert "class MyStrategy(BasePortfolio):" in payload["source"]
        # Both halves derive from one text, so the two editors cannot be taught
        # different contracts.
        assert payload["body"]
        assert [spec["attribute"] for spec in payload["indicators"]] == ["fast_sma", "slow_sma"]

    def test_the_served_body_checks_clean_as_a_draft(self, api):
        body = api.get("/api/strategies/template").json()["body"]

        verdict = api.post("/api/strategies/check/draft", json={"body": body}).json()

        # The first screen a member sees must pass our own checker.
        assert verdict["ok"] is True
        assert verdict["issues"] == []


class TestIndicatorCatalogue:
    """The engine's indicator list, served rather than guessed at.

    Two callers were inventing it: the draft editor's rows, and the run form's
    signal list, which offered MACD and Bollinger — neither of which the engine
    ships.
    """

    def test_it_lists_what_the_engine_ships(self, api):
        payload = api.get("/api/strategies/indicators").json()
        names = [item["name"] for item in payload["items"]]

        assert payload["total"] == len(payload["items"])
        assert "SimpleMovingAverage" in names
        # Sorted, so the editor's list does not reshuffle between calls.
        assert names == sorted(names)

    def test_each_one_describes_the_parameters_it_reads(self, api):
        items = {item["name"]: item["parameters"] for item in api.get("/api/strategies/indicators").json()["items"]}

        # Parsed from `kwargs.get("period", 14)` in the class body — the
        # signature is `**kwargs` and says nothing.
        dma = {param["key"]: param for param in items["DisplacedMovingAverage"]}
        assert dma["period"]["default"] == 14
        assert dma["displacement"]["kind"] == "number"
        # Column plumbing is reported but marked a string, so the editor can
        # leave it alone rather than asking a member about it.
        assert dma["price_col"]["kind"] == "string"
        # A required parameter reports no default rather than an invented one.
        sma = {param["key"]: param for param in items["SimpleMovingAverage"]}
        assert sma["period"]["default"] is None

    def test_every_name_is_accepted_by_the_draft_check(self, api):
        # The list and the validator have to agree, or the editor offers names
        # its own API then refuses.
        for item in api.get("/api/strategies/indicators").json()["items"]:
            name = item["name"]
            response = api.post(
                "/api/strategies/check/draft",
                json={
                    "body": "pass",
                    "indicators": [{"attribute": "x", "indicator": name, "params": {}}],
                },
            )
            assert response.status_code == 200, name

    def test_the_path_is_not_swallowed_by_the_key_route(self, api):
        # `/strategies/{key}` would match "indicators" if declared first.
        assert api.get("/api/strategies/indicators").status_code == 200
