"""Unit tests for the page-retrieval capability.

No network and no DNS: the transport and the resolver are both injected, so
these run identically on a laptop, in CI, and in the test image.

The SSRF cases carry the most weight. ``web_fetch`` is the first tool in
this harness that follows a pointer chosen by someone else, so "it refuses
to reach the metadata service" is a claim that has to be tested rather than
asserted (ADR-055).
"""

import pytest
from mcp_server_web_search.fetch import (
    MAX_CHARS,
    MIN_CHARS,
    FetchError,
    InvalidArgument,
    Response,
    extract_text,
    fetch,
    guard_url,
)

PUBLIC = ["93.184.216.34"]


class ScriptedHttp:
    """An ``HttpClient`` replaying one response per URL, recording requests."""

    def __init__(self, pages: dict[str, Response]) -> None:
        self.pages = pages
        self.requested: list[str] = []

    def get(self, url: str) -> Response:
        self.requested.append(url)
        try:
            return self.pages[url]
        except KeyError:  # pragma: no cover - a test asked for a page it never set
            raise AssertionError(f"unexpected request for {url}") from None


def _html(status: int = 200, body: str = "<p>Hello</p>", **over: object) -> Response:
    return Response(
        status_code=status,
        content_type=str(over.get("content_type", "text/html")),
        text=body,
        location=over.get("location"),  # type: ignore[arg-type]
    )


def _public(_host: str) -> list[str]:
    """A resolver that says every host is public."""
    return PUBLIC


# ---------------------------------------------------------------- guard ----


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",  # loopback
        "10.0.0.5",  # private
        "169.254.169.254",  # cloud metadata — the one that matters
        "::1",  # loopback, v6
    ],
)
def test_a_non_public_address_is_refused(address: str) -> None:
    with pytest.raises(InvalidArgument, match="non-public"):
        guard_url("https://evil.test/", resolve=lambda _host: [address])


def test_a_public_address_is_allowed() -> None:
    guard_url("https://example.com/page", resolve=_public)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x.test/", "javascript:1"])
def test_a_non_http_scheme_is_refused(url: str) -> None:
    with pytest.raises(InvalidArgument, match="only http and https"):
        guard_url(url, resolve=_public)


def test_one_private_address_among_several_still_refuses() -> None:
    # A host answering with one public and one private address would
    # otherwise pass the check and connect to the private one.
    with pytest.raises(InvalidArgument, match="non-public"):
        guard_url("https://x.test/", resolve=lambda _h: ["93.184.216.34", "10.0.0.1"])


# ---------------------------------------------------------------- fetch ----


def test_a_page_is_reduced_to_title_and_text() -> None:
    page = _html(
        body="<html><head><title>T</title></head><body><p>Hi</p></body></html>"
    )
    result = fetch(
        "https://example.com/a",
        client=ScriptedHttp({"https://example.com/a": page}),
        resolve=_public,
    )
    assert result["title"] == "T"
    assert result["content"] == "Hi"
    assert result["truncated"] is False
    assert result["final_url"] == "https://example.com/a"


def test_scripts_and_styles_are_dropped() -> None:
    body = (
        "<html><body><script>alert('x')</script>"
        "<style>p{color:red}</style><p>Real text</p></body></html>"
    )
    result = fetch(
        "https://example.com/a",
        client=ScriptedHttp({"https://example.com/a": _html(body=body)}),
        resolve=_public,
    )
    assert result["content"] == "Real text"
    assert "alert" not in str(result["content"])


def test_long_content_is_truncated_and_says_so() -> None:
    page = _html(body="<p>" + ("word " * 5000) + "</p>")
    result = fetch(
        "https://example.com/a",
        max_chars=MIN_CHARS,
        client=ScriptedHttp({"https://example.com/a": page}),
        resolve=_public,
    )
    assert result["truncated"] is True
    assert str(result["content"]).endswith("[page truncated]")


def test_redirects_are_followed_and_the_final_url_is_reported() -> None:
    client = ScriptedHttp(
        {
            "https://example.com/a": _html(301, location="https://example.com/b"),
            "https://example.com/b": _html(body="<p>Arrived</p>"),
        }
    )
    result = fetch("https://example.com/a", client=client, resolve=_public)
    assert result["final_url"] == "https://example.com/b"
    assert result["content"] == "Arrived"


def test_a_redirect_to_a_private_address_is_refused() -> None:
    # The standard SSRF bypass: a public host that redirects inward. The
    # guard runs again on every hop, so this must not connect.
    client = ScriptedHttp(
        {"https://example.com/a": _html(302, location="http://169.254.169.254/latest")}
    )

    def resolver(host: str) -> list[str]:
        return ["169.254.169.254"] if host == "169.254.169.254" else PUBLIC

    with pytest.raises(InvalidArgument, match="non-public"):
        fetch("https://example.com/a", client=client, resolve=resolver)
    assert client.requested == ["https://example.com/a"]


def test_a_redirect_loop_terminates() -> None:
    client = ScriptedHttp(
        {"https://example.com/a": _html(302, location="https://example.com/a")}
    )
    with pytest.raises(FetchError, match="redirected more than"):
        fetch("https://example.com/a", client=client, resolve=_public)


def test_an_http_error_status_is_reported() -> None:
    client = ScriptedHttp({"https://example.com/a": _html(404)})
    with pytest.raises(FetchError, match="HTTP 404"):
        fetch("https://example.com/a", client=client, resolve=_public)


def test_a_binary_content_type_is_refused_by_name() -> None:
    client = ScriptedHttp(
        {"https://example.com/a": _html(content_type="application/pdf")}
    )
    with pytest.raises(FetchError, match="application/pdf"):
        fetch("https://example.com/a", client=client, resolve=_public)


@pytest.mark.parametrize("chars", [MIN_CHARS - 1, 0, MAX_CHARS + 1])
def test_an_out_of_range_char_bound_is_rejected(chars: int) -> None:
    with pytest.raises(InvalidArgument, match="max_chars"):
        fetch("https://e.test/a", max_chars=chars, resolve=_public)


# -------------------------------------------------------------- extract ----


def test_an_unresolvable_host_is_reported_as_a_bad_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A name that does not resolve is a bad URL, not a transport failure:
    # the model should correct the URL, not retry the same one.
    import socket

    def boom(*_args: object, **_kw: object) -> object:
        raise OSError("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(InvalidArgument, match="cannot resolve host"):
        guard_url("https://nowhere.invalid/")


def test_only_the_article_is_returned_when_the_page_marks_one() -> None:
    """Dropping nav and footer was not enough on a real Wikipedia article.

    Its language switcher and search box are plain divs, and they consumed
    the whole character budget before the prose began (ADR-055).
    """
    _title, body = extract_text(
        "<body><div>Search 22 languages Deutsch Español Français</div>"
        "<main><p>The article itself.</p></main>"
        "<div>Retrieved from ...</div></body>"
    )
    assert body == "The article itself."
