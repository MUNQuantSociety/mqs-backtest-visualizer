"""The multipart file endpoints share one pipeline with their JSON twins.

These tests need no database: ``POST /strategies/upload/check`` reads the
file with ``ast`` and touches nothing else, which makes it the right place to
prove the file-handling layer — extension, size, encoding, emptiness — and
that a file produces the *same* verdict as the same bytes sent as JSON.

    pytest tests/unit/test_strategy_file_upload.py -v
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from server import app
from src.schemas.strategies import MAX_SOURCE_BYTES
from src.services.strategy_validation.template import STARTER_SOURCE


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _file(name: str, content: bytes, mime: str = "text/x-python"):
    return {"file": (name, content, mime)}


def test_a_file_and_the_same_text_as_json_get_the_same_verdict(client) -> None:
    via_json = client.post(
        "/api/strategies/check",
        json={"source": STARTER_SOURCE, "filename": "strategy.py"},
    ).json()
    via_file = client.post(
        "/api/strategies/upload/check",
        files=_file("strategy.py", STARTER_SOURCE.encode()),
    ).json()

    assert via_file["ok"] is True
    # Same source, same answer — the transport must not change the verdict.
    assert via_file["status"] == via_json["status"]
    assert via_file["className"] == via_json["className"] == "MyStrategy"
    assert via_file["issues"] == via_json["issues"]


def test_an_incompatible_file_is_still_a_200_with_the_problems_listed(client) -> None:
    source = STARTER_SOURCE.replace("import logging", "import os\nimport logging")
    response = client.post(
        "/api/strategies/upload/check", files=_file("bad.py", source.encode())
    )
    assert response.status_code == 200

    body = response.json()
    assert body["ok"] is False
    assert body["status"] == "incompatible"
    # The banned import is named with its line, not flattened into one string.
    assert any("os" in issue["message"] for issue in body["issues"])
    assert all(issue["line"] >= 1 for issue in body["issues"])


def test_a_non_python_file_is_refused_by_extension(client) -> None:
    response = client.post(
        "/api/strategies/upload/check",
        files=_file("strategy.txt", STARTER_SOURCE.encode(), "text/plain"),
    )
    assert response.status_code == 422
    assert ".py" in response.json()["detail"]
    assert "strategy.txt" in response.json()["detail"]


def test_extension_check_is_case_insensitive(client) -> None:
    response = client.post(
        "/api/strategies/upload/check",
        files=_file("Strategy.PY", STARTER_SOURCE.encode()),
    )
    assert response.status_code == 200


def test_an_empty_file_is_refused(client) -> None:
    response = client.post(
        "/api/strategies/upload/check", files=_file("empty.py", b"   \n")
    )
    assert response.status_code == 422
    assert "empty" in response.json()["detail"]


def test_a_file_that_is_not_utf8_is_refused_with_the_offending_byte(client) -> None:
    response = client.post(
        "/api/strategies/upload/check",
        files=_file("latin1.py", b"# caf\xe9\nclass X: pass\n"),
    )
    assert response.status_code == 422
    assert "UTF-8" in response.json()["detail"]


def test_a_file_over_the_limit_is_a_413_like_the_json_endpoint(client) -> None:
    oversized = b"# " + b"x" * MAX_SOURCE_BYTES
    response = client.post(
        "/api/strategies/upload/check", files=_file("huge.py", oversized)
    )
    assert response.status_code == 413
    assert str(MAX_SOURCE_BYTES) in response.json()["detail"]


def test_upload_submit_validates_the_form_name_with_one_sentence(client) -> None:
    """A blank name is the student's mistake, so it is a 422 with a string."""
    response = client.post(
        "/api/strategies/upload",
        files=_file("strategy.py", STARTER_SOURCE.encode()),
        data={"name": "   "},
    )
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)
    assert "name" in response.json()["detail"]


def test_literal_routes_are_not_swallowed_by_the_key_route(client) -> None:
    """``/template`` must resolve to the template, not to a strategy keyed 'template'."""
    response = client.get("/api/strategies/template")
    assert response.status_code == 200
    assert response.json()["filename"] == "strategy.py"
