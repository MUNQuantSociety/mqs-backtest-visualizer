"""Publisher-page reading: what is parsed, and what is never fetched."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

import pytest

from src.integrations import article_page
from src.integrations.article_page import PageStory, fetch_page_story, parse_page_story

PUBLIC = "93.184.216.34"
PAGE = b"""<html><head>
<title>Fallback title</title>
<meta property="og:title" content="Why Shares of Altria Group Soared in April">
<meta property="og:description" content="Altria&#39;s shares rose 10% in April.">
</head><body><p>Body text</p></body></html>"""


def _public(_host, _port):
    return [PUBLIC]


def _html(body=PAGE, status=200, location=None, content_type="text/html; charset=utf-8"):
    return article_page._Response(status, location, content_type, body)


def test_parses_the_open_graph_title_and_description():
    story = parse_page_story(PAGE.decode())

    assert story == PageStory(
        title="Why Shares of Altria Group Soared in April",
        summary="Altria's shares rose 10% in April.",
    )


def test_falls_back_to_the_twitter_card_and_meta_description():
    page = (
        '<head><meta name="twitter:title" content="T"><meta name="description" content="D">'
        "</head>"
    )

    assert parse_page_story(page) == PageStory(title="T", summary="D")


def test_falls_back_to_the_title_element():
    assert parse_page_story("<head><title> Plain  title </title></head>").title == "Plain title"


def test_ignores_meta_tags_in_the_body():
    page = '<head></head><body><meta property="og:description" content="late"></body>'

    assert parse_page_story(page).summary == ""


def test_strips_markup_escaped_inside_the_description():
    page = '<head><meta property="og:description" content="&lt;p&gt;Values-based &lt;b&gt;ETFs&lt;/b&gt;."></head>'

    assert parse_page_story(page).summary == "Values-based ETFs."


def test_caps_an_oversized_summary():
    page = f'<head><meta property="og:description" content="{"x" * 5000}"></head>'

    assert len(parse_page_story(page).summary) == article_page.SUMMARY_MAX_CHARS


def test_fetches_and_parses_a_public_page():
    story = fetch_page_story("https://example.com/a", _public, lambda _target: _html())

    assert story is not None
    assert story.summary == "Altria's shares rose 10% in April."


def test_connects_to_the_checked_address_with_the_real_host():
    seen = []

    def request(target):
        seen.append(target)
        return _html()

    fetch_page_story("https://example.com/a/b?x=1", _public, request)

    assert (seen[0].host, seen[0].address, seen[0].path) == ("example.com", PUBLIC, "/a/b?x=1")


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "fd00::1", "0.0.0.0",
     "::ffff:127.0.0.1", "224.0.0.1"],
)
def test_refuses_a_host_that_resolves_to_a_non_public_address(address):
    story = fetch_page_story(
        "https://example.com/a",
        lambda _h, _p: [address],
        lambda _t: pytest.fail("a non-public address was requested"),
    )

    assert story is None


def test_refuses_when_any_resolved_address_is_non_public():
    story = fetch_page_story(
        "https://example.com/a",
        lambda _h, _p: [PUBLIC, "10.0.0.5"],
        lambda _t: pytest.fail("a mixed answer was requested"),
    )

    assert story is None


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "ftp://example.com/a", "javascript:alert(1)", "https:///nohost",
     "https://example.com:8443/a", "http://example.com:22/a"],
)
def test_refuses_a_url_it_does_not_fetch(url):
    story = fetch_page_story(url, _public, lambda _t: pytest.fail(f"{url} was requested"))

    assert story is None


def test_rechecks_every_redirect_hop():
    hops = iter([_html(status=302, location="http://internal.example/admin", body=b"")])

    def resolve(host, _port):
        return ["10.0.0.9"] if host == "internal.example" else [PUBLIC]

    story = fetch_page_story("https://example.com/a", resolve, lambda _t: next(hops))

    assert story is None


def test_follows_a_relative_redirect_on_the_same_site():
    seen = []
    responses = iter([_html(status=301, location="/final", body=b""), _html()])

    def request(target):
        seen.append(target.path)
        return next(responses)

    story = fetch_page_story("https://example.com/start", _public, request)

    assert story is not None
    assert seen == ["/start", "/final"]


def test_gives_up_after_too_many_redirects():
    loop = _html(status=302, location="/again", body=b"")

    assert fetch_page_story("https://example.com/a", _public, lambda _t: loop) is None


@pytest.mark.parametrize("status", [403, 404, 500])
def test_returns_none_for_an_error_page(status):
    assert fetch_page_story("https://example.com/a", _public, lambda _t: _html(status=status)) is None


def test_returns_none_for_a_non_html_response():
    response = _html(body=b"", content_type="application/pdf")

    assert fetch_page_story("https://example.com/a", _public, lambda _t: response) is None


def test_returns_none_when_the_network_fails():
    def broken(_target):
        raise TimeoutError("slow")

    assert fetch_page_story("https://example.com/a", _public, broken) is None


def test_returns_none_when_dns_fails():
    def unresolvable(_host, _port):
        raise OSError("no such host")

    assert fetch_page_story("https://example.com/a", unresolvable, lambda _t: _html()) is None


def test_honours_the_declared_charset():
    body = '<head><meta property="og:description" content="caf\xe9"></head>'.encode("latin-1")
    response = _html(body=body, content_type="text/html; charset=iso-8859-1")

    story = fetch_page_story("https://example.com/a", _public, lambda _t: response)

    assert story is not None
    assert story.summary == "café"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - the stdlib's name
        body = PAGE + b"x" * (article_page.MAX_BYTES * 2)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.server.seen_host = self.headers["Host"]
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def test_the_pinned_connection_reads_a_bounded_body_and_sends_the_real_host():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    target = article_page._Target("http", "news.example", server.server_port, "127.0.0.1", "/a")

    response = article_page._request(target)
    thread.join(timeout=5)
    server.server_close()

    assert response.status == 200
    assert len(response.body) == article_page.MAX_BYTES
    assert server.seen_host == f"news.example:{server.server_port}"
