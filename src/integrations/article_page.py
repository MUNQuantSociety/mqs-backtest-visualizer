"""Read a news article's own title and summary from its publisher's page.

The article table stores title and summary joined into one field with no mark
where the title ends, so the real summary paragraph cannot be cut out of it.
Publishers state both in the page head (``og:title``/``og:description``, the
Twitter card, or ``<meta name="description">``); this reads them there.

The URL comes from the scraped article table, never from a browser, but it is
still untrusted, so every request is guarded against server-side request
forgery:

* http(s) only, default ports only;
* the host must resolve only to public addresses (no loopback, private,
  link-local, reserved or multicast), checked again on every redirect hop;
* the connection is made to the checked address itself, so a second DNS answer
  cannot swap in an internal one (TLS still verifies the real hostname);
* bounded time, redirects and bytes, and only ``text/html`` is parsed.

Every failure is ``None``: the caller falls back to the stored text.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import html
from html.parser import HTMLParser
import http.client
import ipaddress
import logging
import re
import socket
import ssl
from urllib.parse import urljoin, urlsplit

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 5.0
MAX_REDIRECTS = 3
MAX_BYTES = 512 * 1024
SUMMARY_MAX_CHARS = 2000
TITLE_MAX_CHARS = 300

_DEFAULT_PORTS = {"http": 80, "https": 443}
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_READ_CHUNK = 16 * 1024
# A plain browser identity: many publishers refuse unknown clients outright.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en",
    "Accept-Encoding": "identity",
}
_TAG = re.compile(r"<[^>]*>")
_SPACE_BEFORE_PUNCTUATION = re.compile(r" ([.,;:!?])")
_TITLE_KEYS = ("og:title", "twitter:title")
_SUMMARY_KEYS = ("og:description", "twitter:description", "description")


@dataclass(frozen=True)
class PageStory:
    """What a publisher's page says about itself. Either field may be empty."""

    title: str
    summary: str


@dataclass(frozen=True)
class _Target:
    scheme: str
    host: str
    port: int
    address: str
    path: str


@dataclass(frozen=True)
class _Response:
    status: int
    location: str | None
    content_type: str
    body: bytes


class _Refused(ValueError):
    """A URL this module will not fetch."""


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def _resolve(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _target(url: str, resolve: Callable[[str, int], list[str]]) -> _Target:
    """Validate ``url`` and pin it to one checked public address.

    Raises:
        _Refused: a scheme, port, host or address this module will not reach.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise _Refused(f"scheme {scheme!r} is not fetched")
    host = parts.hostname
    if not host:
        raise _Refused("no host")
    port = parts.port or _DEFAULT_PORTS[scheme]
    if port != _DEFAULT_PORTS[scheme]:
        raise _Refused(f"port {port} is not fetched")
    try:
        addresses = resolve(host, port)
    except OSError as exc:
        raise _Refused(f"{host} does not resolve") from exc
    if not addresses or not all(_is_public(address) for address in addresses):
        raise _Refused(f"{host} resolves to a non-public address")
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return _Target(scheme, host, port, addresses[0], path)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a fixed address, verifying the certificate for the real host name."""

    def __init__(self, host: str, address: str, port: int, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Plain HTTP to a fixed address, with the real host name in the Host header."""

    def __init__(self, host: str, address: str, port: int, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


def _request(target: _Target) -> _Response:
    connection_class = (
        _PinnedHTTPSConnection if target.scheme == "https" else _PinnedHTTPConnection
    )
    connection = connection_class(target.host, target.address, target.port, TIMEOUT_SECONDS)
    try:
        connection.request("GET", target.path, headers=_HEADERS)
        response = connection.getresponse()
        content_type = (response.getheader("Content-Type") or "").lower()
        body = b""
        if response.status == 200 and "text/html" in content_type:
            chunks: list[bytes] = []
            size = 0
            while size < MAX_BYTES:
                chunk = response.read(min(_READ_CHUNK, MAX_BYTES - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            body = b"".join(chunks)
        return _Response(response.status, response.getheader("Location"), content_type, body)
    finally:
        connection.close()


class _HeadParser(HTMLParser):
    """Collects meta tags and ``<title>`` from the head; ignores the page body."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self._in_title = False
        self.done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # The rest of a chunk still arrives after the head closes; body tags
        # are page content, not the page's description of itself.
        if self.done:
            return
        if tag == "body":
            self.done = True
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            values = {name.lower(): value or "" for name, value in attrs}
            key = (values.get("property") or values.get("name") or "").lower()
            if key and key not in self.meta and values.get("content"):
                self.meta[key] = values["content"]

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "head":
            self.done = True

    def handle_data(self, data: str) -> None:
        if self._in_title and not self.done:
            self.title += data


def _clean(text: str, limit: int) -> str:
    # Some publishers (Yahoo) put escaped markup inside the meta content itself.
    plain = " ".join(_TAG.sub(" ", html.unescape(text)).split())
    # A removed closing tag leaves a space before punctuation ("ETFs .").
    return _SPACE_BEFORE_PUNCTUATION.sub(r"\1", plain)[:limit]


def parse_page_story(document: str) -> PageStory:
    """The title and summary a page's head declares. Pure: no I/O."""
    parser = _HeadParser()
    for start in range(0, len(document), _READ_CHUNK):
        parser.feed(document[start : start + _READ_CHUNK])
        if parser.done:
            break
    title = next((parser.meta[key] for key in _TITLE_KEYS if key in parser.meta), parser.title)
    summary = next((parser.meta[key] for key in _SUMMARY_KEYS if key in parser.meta), "")
    return PageStory(title=_clean(title, TITLE_MAX_CHARS), summary=_clean(summary, SUMMARY_MAX_CHARS))


def _charset(content_type: str) -> str:
    for part in content_type.split(";"):
        name, _, value = part.strip().partition("=")
        if name == "charset" and value:
            return value.strip("\"'")
    return "utf-8"


def fetch_page_story(
    url: str,
    resolve: Callable[[str, int], list[str]] = _resolve,
    request: Callable[[_Target], _Response] = _request,
) -> PageStory | None:
    """The publisher page's own title and summary, or None if it cannot be read.

    Blocking; call it from a worker thread. ``resolve`` and ``request`` exist
    so tests can stand in for DNS and the network.
    """
    current = url
    try:
        for _ in range(MAX_REDIRECTS + 1):
            target = _target(current, resolve)
            response = request(target)
            if response.status in _REDIRECT_STATUSES and response.location:
                current = urljoin(current, response.location)
                continue
            if response.status != 200 or not response.body:
                logger.info("Article page gave nothing to read: status=%s", response.status)
                return None
            try:
                document = response.body.decode(_charset(response.content_type), errors="replace")
            except LookupError:
                document = response.body.decode("utf-8", errors="replace")
            story = parse_page_story(document)
            return story if story.title or story.summary else None
        logger.info("Article page redirected more than %d times", MAX_REDIRECTS)
        return None
    except _Refused as exc:
        logger.warning("Article page refused: %s", exc)
        return None
    except (OSError, http.client.HTTPException, ValueError) as exc:
        logger.info("Article page could not be read: %s", type(exc).__name__)
        return None
