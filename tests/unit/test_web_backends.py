"""Unit tests for the two shipped backends and the entry point.

These are the only places ``mcp_server_web_search`` touches a third-party
library, so they are covered by injecting a fake module into
``sys.modules`` rather than by reaching the network. What is under test is
our own code either side of the call: the byte cap, the content-type
parsing, and the normalisation of an arbitrary library failure into the one
exception type the server knows how to report.
"""

import sys
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from mcp_server_web_search.fetch import (
    MAX_DOWNLOAD_BYTES,
    FetchError,
    HttpxClient,
    build_headers,
)
from mcp_server_web_search.search import DdgsBackend, SearchError


class _FakeDDGS:
    """Stands in for ``ddgs.DDGS``: a context manager with ``text``."""

    def __init__(self, hits: list[dict[str, Any]] | None, boom: bool) -> None:
        self.hits = hits
        self.boom = boom
        self.timeout: object = None

    def __call__(self, timeout: object = None) -> "_FakeDDGS":
        self.timeout = timeout
        return self

    def __enter__(self) -> "_FakeDDGS":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def text(self, query: str, max_results: int) -> list[dict[str, Any]]:
        if self.boom:
            raise RuntimeError("202 Ratelimit")
        return list(self.hits or [])


@pytest.fixture
def fake_ddgs(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeDDGS]:
    """Install a fake ``ddgs`` module for the duration of one test."""
    stub = _FakeDDGS([{"title": "T", "href": "https://x.test/", "body": "B"}], False)
    monkeypatch.setitem(sys.modules, "ddgs", SimpleNamespace(DDGS=stub))
    yield stub


def test_the_ddgs_backend_returns_the_librarys_hits(fake_ddgs: _FakeDDGS) -> None:
    assert DdgsBackend().text("query", 3) == [
        {"title": "T", "href": "https://x.test/", "body": "B"}
    ]


def test_any_library_failure_becomes_a_search_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _FakeDDGS(None, boom=True)
    monkeypatch.setitem(sys.modules, "ddgs", SimpleNamespace(DDGS=stub))
    with pytest.raises(SearchError, match="Ratelimit") as caught:
        DdgsBackend().text("query", 1)
    # The original is kept for anyone debugging, not discarded.
    assert isinstance(caught.value.__cause__, RuntimeError)


class _FakeStream:
    def __init__(self, chunks: list[bytes], status: int, headers: dict[str, str]):
        self.chunks = chunks
        self.status_code = status
        self.headers = headers
        self.encoding = "utf-8"

    def __enter__(self) -> "_FakeStream":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def iter_bytes(self) -> Iterator[bytes]:
        yield from self.chunks


class _FakeHttpxClient:
    def __init__(self, stream: _FakeStream, **_kw: object) -> None:
        self._stream = stream

    def __enter__(self) -> "_FakeHttpxClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def stream(self, _method: str, _url: str) -> _FakeStream:
        return self._stream


def _install_httpx(
    monkeypatch: pytest.MonkeyPatch, stream: _FakeStream | None, boom: bool = False
) -> None:
    def client(**kw: object) -> _FakeHttpxClient:
        if boom:
            raise OSError("connection reset")
        assert stream is not None
        return _FakeHttpxClient(stream, **kw)

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(Client=client))


def test_the_body_is_capped_before_it_is_decoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Three chunks that together exceed the cap; reading must stop, not
    # pull the whole thing into memory to throw most of it away.
    chunk = b"x" * (MAX_DOWNLOAD_BYTES // 2)
    _install_httpx(
        monkeypatch,
        _FakeStream([chunk, chunk, chunk], 200, {"content-type": "text/html"}),
    )
    assert len(HttpxClient().get("https://x.test/").text) == MAX_DOWNLOAD_BYTES


def test_a_transport_failure_becomes_a_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_httpx(monkeypatch, None, boom=True)
    with pytest.raises(FetchError, match="could not retrieve"):
        HttpxClient().get("https://x.test/")


def test_the_entry_point_parses_the_timeout_and_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_server_web_search

    served: list[tuple[float, str]] = []

    async def fake_serve(timeout: float = 20.0, contact: str = "") -> None:
        served.append((timeout, contact))

    monkeypatch.setattr(mcp_server_web_search, "serve", fake_serve)
    monkeypatch.setattr(
        sys,
        "argv",
        ["mcp_server_web_search", "--timeout", "45", "--contact", "a@b.test"],
    )
    mcp_server_web_search.main()
    assert served == [(45.0, "a@b.test")]


def test_a_configured_contact_is_named_in_the_user_agent() -> None:
    """Wikimedia refuses a request whose User-Agent names no contact.

    Verified against the live site: without a contact the response is 403
    and a link to the robot policy; with one it is 200 (ADR-055).
    """
    assert build_headers("team@example.com")["User-Agent"] == (
        "agent-harness/0.1 (+team@example.com)"
    )
    # Blank and whitespace-only mean "not configured", not an empty bracket.
    assert build_headers()["User-Agent"] == "agent-harness/0.1"
    assert build_headers("   ")["User-Agent"] == "agent-harness/0.1"


def test_a_library_printing_to_stdout_cannot_corrupt_the_protocol(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """stdout is the MCP channel; a stray print would desynchronise it.

    ``ddgs`` really does narrate its requests, so this is a live footgun
    rather than a hypothetical one: the line must land on stderr, where it
    is a log entry, not on stdout, where it is corrupt JSON-RPC.
    """

    class Chatty(_FakeDDGS):
        def text(self, query: str, max_results: int) -> list[dict[str, Any]]:
            print("response: https://engine.test/ 200")
            return [{"title": "T", "href": "https://x.test/", "body": "B"}]

    monkeypatch.setitem(sys.modules, "ddgs", SimpleNamespace(DDGS=Chatty(None, False)))
    DdgsBackend().text("query", 1)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "response:" in captured.err
