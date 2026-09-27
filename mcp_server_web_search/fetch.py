"""Fetch one web page and reduce it to readable text.

Same rule as :mod:`mcp_server_web_search.search`: no MCP imports, an
injectable transport, and every bound stated as a constant so the tool
schema can quote it.

**Why this module is careful.** ``web_search`` reads an index; ``web_fetch``
retrieves whatever URL it is handed, and the URL can come from a search
result written by someone else. That makes it the first tool in this harness
that will follow an attacker's pointer, so two guards are not optional:

* **SSRF.** A URL naming ``localhost``, a private range, or a cloud metadata
  address (``169.254.169.254``) must be refused *before* the request is
  made, and again after every redirect — a public host that redirects to the
  metadata service is the standard bypass. Nothing in the container is worth
  reaching this way today, but the guard costs twenty lines and the absence
  of one is the sort of thing that is only noticed afterwards (ADR-056).
* **Size.** The download is capped in bytes before decoding, not trimmed
  after, so a multi-megabyte page cannot be pulled into memory just to throw
  it away.

HTML is reduced with the standard library rather than a parsing dependency.
The job here is narrow — drop scripts, styles and markup, keep the text,
collapse the whitespace — and a real extraction library would earn its place
only if we were doing article-boundary detection, which we are not. The same
reasoning kept ``tzlocal`` out of the time server (ADR-044).
"""

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import urljoin, urlparse

# Bounds quoted in the tool schema.
MIN_CHARS = 200
MAX_CHARS = 20000
DEFAULT_CHARS = 4000

# Hard ceiling on the download, applied before decoding. Well above any
# reasonable article and well below anything that would strain the container.
MAX_DOWNLOAD_BYTES = 3_000_000

# Redirect hops followed before giving up. Enough for the usual
# http -> https -> www chain, few enough to make a loop terminate.
MAX_REDIRECTS = 5

_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})

# Content types worth handing to a language model. Anything else — a PDF, an
# image, a zip — is named in the error rather than decoded into mojibake.
_READABLE_TYPES = ("text/html", "text/plain", "application/xhtml+xml")

# Elements whose *contents* are never page text.
#
# `nav` and `footer` are here for a measured reason rather than a stylistic
# one: fetching a Wikipedia article with a 400-character budget returned
# "Jump to content Main menu ... Contribute Help" and never reached the
# article. Site chrome is the single biggest consumer of the budget, and
# dropping it is worth more than any amount of tuning elsewhere.
#
# `header` and `aside` are deliberately *not* skipped. Plenty of sites put
# the headline in a `header`, and dropping it would lose the one line most
# worth keeping.
_SKIP_ELEMENTS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "svg",
        "template",
        "head",
        "iframe",
        "nav",
        "footer",
    }
)

# Elements that imply a line break, so the extracted text keeps some of the
# page's structure instead of running together into one paragraph.
_BLOCK_ELEMENTS = frozenset(
    {
        "p",
        "div",
        "br",
        "li",
        "tr",
        "section",
        "article",
        "header",
        "footer",
        "nav",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "pre",
        "table",
        "ul",
        "ol",
        "figure",
        "main",
        "aside",
    }
)


class InvalidArgument(ValueError):
    """Raised when a URL cannot be used as given, including a blocked host."""


class FetchError(RuntimeError):
    """Raised when a page could not be retrieved or was not readable."""


@dataclass(frozen=True)
class Response:
    """The parts of an HTTP response this module uses.

    A narrow value type rather than ``httpx.Response`` so the transport can
    be faked in tests without constructing a real response object.

    Attributes:
        status_code: HTTP status.
        content_type: Lowercased ``Content-Type``, parameters stripped.
        text: Decoded body, already capped at :data:`MAX_DOWNLOAD_BYTES`.
        location: ``Location`` header when the status is a redirect.
    """

    status_code: int
    content_type: str
    text: str
    location: str | None = None


USER_AGENT = "agent-harness/0.1"
"""Product token. An honest agent says what it is."""

# A User-Agent alone is not enough for two different reasons.
#
# Some large sites answer 403 to a request that names no acceptable content
# type, because that is what a bare scraper looks like. Accept and
# Accept-Language fix that; they are not a disguise, since the User-Agent
# still says exactly what this is.
#
# Wikimedia goes further and requires a *contact* — a URL or an address —
# in the User-Agent, and returns 403 with a link to its robot policy
# without one. Wikipedia is the first result for a great many research
# questions, so this is not a corner case. The contact is configuration
# rather than a constant: it belongs to whoever runs the agent, and
# baking one person's address into the source would put it in every
# request the whole team makes (ADR-055).
BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def build_headers(contact: str = "") -> dict[str, str]:
    """Return the request headers, naming a contact when one is configured.

    Args:
        contact: A URL or email address reachable by a site operator, or an
            empty string. Empty is usable — most sites serve it — but
            Wikimedia properties will refuse with a readable error saying
            so, which is the intended nudge to configure one.

    Returns:
        Headers for every outgoing request.
    """
    agent = f"{USER_AGENT} (+{contact})" if contact.strip() else USER_AGENT
    return {"User-Agent": agent, **BASE_HEADERS}


class HttpClient(Protocol):
    """Whatever performs one HTTP GET without following redirects."""

    def get(self, url: str) -> Response:
        """Retrieve one URL.

        Args:
            url: Absolute http/https URL, already validated.

        Returns:
            The response, with redirects reported rather than followed —
            each hop is re-checked against the SSRF guard.
        """
        ...


Resolver = Callable[[str], list[str]]
"""Maps a hostname to its IP addresses. Injectable so tests need no DNS."""


class HttpxClient:
    """The shipped transport: ``httpx``, which already ships in the image.

    No new dependency: ``httpx`` and ``certifi`` arrive with ``openai`` and
    ``mcp``. Declaring it in ``requirements.txt`` anyway is the honest move
    now that this package imports it directly rather than transitively.
    """

    def __init__(self, timeout: float = 20.0, contact: str = "") -> None:
        """Store the per-request timeout and the contact to advertise.

        Args:
            timeout: Seconds allowed for one page retrieval.
            contact: URL or address named in the User-Agent.
        """
        self._timeout = timeout
        self._headers = build_headers(contact)

    def get(self, url: str) -> Response:
        """Retrieve one URL, capping the body as it is read.

        Args:
            url: Absolute http/https URL.

        Returns:
            The response, redirects unfollowed.

        Raises:
            FetchError: On any transport failure.
        """
        import httpx

        try:
            with httpx.Client(
                timeout=self._timeout,
                follow_redirects=False,
                headers=self._headers,
            ) as client:
                with client.stream("GET", url) as response:
                    body = b""
                    for chunk in response.iter_bytes():
                        body += chunk
                        if len(body) >= MAX_DOWNLOAD_BYTES:
                            body = body[:MAX_DOWNLOAD_BYTES]
                            break
                    raw_type = response.headers.get("content-type", "")
                    return Response(
                        status_code=response.status_code,
                        content_type=raw_type.split(";")[0].strip().lower(),
                        text=body.decode(
                            response.encoding or "utf-8", errors="replace"
                        ),
                        location=response.headers.get("location"),
                    )
        except Exception as exc:  # noqa: BLE001 - normalised, not swallowed
            raise FetchError(f"could not retrieve {url}: {exc}") from exc


def fetch(
    url: str,
    max_chars: int = DEFAULT_CHARS,
    client: HttpClient | None = None,
    resolve: Resolver | None = None,
) -> dict[str, object]:
    """Retrieve a page and return its readable text.

    Args:
        url: Absolute http or https URL.
        max_chars: Characters of page text to return, 200 to 20000.
        client: Transport. Defaults to :class:`HttpxClient`.
        resolve: Hostname resolver, injected so tests need no DNS.

    Returns:
        ``url``, ``final_url``, ``title``, ``content``, ``chars`` and
        ``truncated`` — the last so the model knows it is reading a prefix
        and can say so rather than treating it as the whole page.

    Raises:
        InvalidArgument: If the URL or the character bound is unusable, or
            the host is one this harness refuses to reach.
        FetchError: If the page could not be retrieved or was not text.
    """
    if not MIN_CHARS <= max_chars <= MAX_CHARS:
        raise InvalidArgument(
            f"max_chars must be between {MIN_CHARS} and {MAX_CHARS}, got {max_chars}"
        )

    transport = client or HttpxClient()
    current = url.strip()

    for _ in range(MAX_REDIRECTS + 1):
        guard_url(current, resolve)
        response = transport.get(current)

        if response.status_code in _REDIRECT_CODES and response.location:
            # Re-checked at the top of the next iteration. A public host
            # redirecting to 169.254.169.254 is the standard SSRF bypass,
            # so following blindly here would undo the guard entirely.
            current = urljoin(current, response.location)
            continue

        if response.status_code >= 400:
            raise FetchError(f"{current} returned HTTP {response.status_code}")
        if response.content_type and response.content_type not in _READABLE_TYPES:
            raise FetchError(
                f"{current} is {response.content_type}, not a readable page"
            )

        title, body = extract_text(response.text)
        truncated = len(body) > max_chars
        if truncated:
            body = body[:max_chars].rstrip() + "\n... [page truncated]"
        return {
            "url": url,
            "final_url": current,
            "title": title,
            "content": body,
            "chars": len(body),
            "truncated": truncated,
        }

    raise FetchError(f"{url} redirected more than {MAX_REDIRECTS} times")


def guard_url(url: str, resolve: Resolver | None = None) -> None:
    """Refuse a URL this harness must not fetch.

    Args:
        url: Candidate URL, absolute.
        resolve: Hostname resolver. Defaults to the system resolver.

    Raises:
        InvalidArgument: If the scheme is not http/https, the host is
            missing, or the host resolves to a private, loopback,
            link-local, reserved or multicast address.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise InvalidArgument(
            f"only http and https URLs can be fetched, got {parsed.scheme or 'none'!r}"
        )
    host = parsed.hostname
    if not host:
        raise InvalidArgument(f"no host in URL {url!r}")

    for address in (resolve or _system_resolve)(host):
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise InvalidArgument(
                f"refusing to fetch {host!r}: it resolves to the non-public "
                f"address {address}"
            )


def _system_resolve(host: str) -> list[str]:
    """Resolve a hostname with the system resolver.

    Returns:
        Every address the host resolves to. All of them are checked, because
        a host answering with one public and one private address would
        otherwise pass on the public one and connect to the private one.

    Raises:
        InvalidArgument: If the name does not resolve, which is a bad URL
            rather than a transport failure.
    """
    try:
        info = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise InvalidArgument(f"cannot resolve host {host!r}: {exc}") from exc
    return [str(entry[4][0]) for entry in info]


# Elements that mark the real content of a modern page. When one is
# present, everything outside it is chrome.
#
# This is the one heuristic in the module, and it earns its place: dropping
# `nav` and `footer` was not enough on a real Wikipedia article, whose
# language switcher and search box are plain `div`s and consumed the whole
# budget before the prose began. Restricting to `main`/`article` when the
# page offers one is the difference between reading an article and reading
# its menu. Pages without either fall back to the whole body, so nothing is
# lost on older markup (ADR-055).
_MAIN_ELEMENTS = frozenset({"main", "article"})


class _TextExtractor(HTMLParser):
    """Collect visible text, dropping markup, scripts and site chrome."""

    def __init__(self) -> None:
        """Start with nothing collected and no skipped element open."""
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._parts: list[str] = []
        self._main_parts: list[str] = []
        self._skip_depth = 0
        self._main_depth = 0
        self._saw_main = False
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: object) -> None:
        """Open an element, tracking skipped regions and the title."""
        if tag in _SKIP_ELEMENTS:
            self._skip_depth += 1
            return
        if tag in _MAIN_ELEMENTS:
            self._main_depth += 1
            self._saw_main = True
        if tag == "title":
            self._in_title = True
        elif tag in _BLOCK_ELEMENTS or tag in _MAIN_ELEMENTS:
            self._collect("\n")

    def handle_endtag(self, tag: str) -> None:
        """Close an element."""
        if tag in _SKIP_ELEMENTS:
            # Clamped at zero: real pages close tags they never opened, and
            # a negative depth would silently disable the skip for the rest
            # of the document.
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        elif tag in _BLOCK_ELEMENTS or tag in _MAIN_ELEMENTS:
            self._collect("\n")
        if tag in _MAIN_ELEMENTS:
            self._main_depth = max(0, self._main_depth - 1)

    def handle_data(self, data: str) -> None:
        """Collect text, unless it is inside a skipped element."""
        if self._in_title:
            self.title += data
        # `head` is skipped, so the title is captured above before this test.
        self._collect(data)

    def _collect(self, text: str) -> None:
        """Record a fragment against the whole page and, inside, the article."""
        if self._skip_depth:
            return
        self._parts.append(text)
        if self._main_depth:
            self._main_parts.append(text)

    def text(self) -> str:
        """Return the article text if the page marked one, else the page.

        Falling back rather than insisting on ``main``: a page with neither
        element is not broken, it is just older, and returning nothing for
        it would be worse than returning its chrome along with its prose.
        """
        # `any(strip())`, not merely a non-empty list: an empty <main>
        # still collects the newlines its own open and close tags emit,
        # which would read as content and return a blank page.
        has_article = self._saw_main and any(p.strip() for p in self._main_parts)
        chosen = self._main_parts if has_article else self._parts
        collapsed = [
            " ".join(line.split())
            for line in "".join(chosen).replace("\r", "").split("\n")
        ]
        return "\n".join(line for line in collapsed if line)


def extract_text(html: str) -> tuple[str, str]:
    """Reduce an HTML document to a title and readable body text.

    Args:
        html: Raw document. Plain text passes through essentially unchanged,
            which is why ``text/plain`` needs no separate branch.

    Returns:
        ``(title, body)``. The title is empty when the page has none.
    """
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup is not a crash
        # HTMLParser is lenient, but a truncated download can still trip it.
        # Whatever was collected before the failure is better than nothing.
        pass
    return " ".join(parser.title.split()), parser.text()


__all__ = [
    "DEFAULT_CHARS",
    "MAX_CHARS",
    "MAX_DOWNLOAD_BYTES",
    "MAX_REDIRECTS",
    "MIN_CHARS",
    "BASE_HEADERS",
    "USER_AGENT",
    "FetchError",
    "HttpClient",
    "HttpxClient",
    "InvalidArgument",
    "Resolver",
    "Response",
    "build_headers",
    "extract_text",
    "fetch",
    "guard_url",
]
