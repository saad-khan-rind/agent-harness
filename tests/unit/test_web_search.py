"""Unit tests for the search capability.

No network: every test injects a scripted backend, which is what
``.github/workflows/ci.yml`` requires of the whole suite. The code under test is the
production ``search()`` — only the far end is a double (ADR-013, ADR-041).
"""

from typing import Any

import pytest
from mcp_server_web_search.search import (
    MAX_RESULTS,
    MAX_SNIPPET_CHARS,
    InvalidArgument,
    Result,
    SearchError,
    search,
)


class ScriptedBackend:
    """A ``SearchBackend`` that replays fixed hits and records its calls."""

    def __init__(self, hits: list[dict[str, Any]]) -> None:
        self.hits = hits
        self.calls: list[tuple[str, int]] = []

    def text(self, query: str, max_results: int) -> list[dict[str, Any]]:
        self.calls.append((query, max_results))
        return self.hits


class FailingBackend:
    """A backend that always raises, like a throttled engine."""

    def text(self, query: str, max_results: int) -> list[dict[str, Any]]:
        raise SearchError("backend refused: 202 Ratelimit")


def _hit(n: int = 1, **over: Any) -> dict[str, Any]:
    """Return one backend-shaped hit, overridable field by field."""
    return {
        "title": f"Result {n}",
        "href": f"https://example.com/{n}",
        "body": f"Snippet {n}.",
    } | over


def test_results_are_normalised_to_title_url_snippet() -> None:
    backend = ScriptedBackend([_hit(1), _hit(2)])
    results = search("model context protocol", backend=backend)
    assert results == [
        Result("Result 1", "https://example.com/1", "Snippet 1."),
        Result("Result 2", "https://example.com/2", "Snippet 2."),
    ]


def test_a_hit_with_no_url_is_dropped_rather_than_cited() -> None:
    backend = ScriptedBackend([_hit(1, href=""), _hit(2)])
    results = search("valid query", backend=backend)
    assert [r.url for r in results] == ["https://example.com/2"]


def test_a_long_snippet_is_clipped_and_marked() -> None:
    backend = ScriptedBackend([_hit(1, body="x" * 900)])
    snippet = search("valid query", backend=backend)[0].snippet
    assert len(snippet) == MAX_SNIPPET_CHARS
    assert snippet.endswith("…")


def test_no_hits_is_an_empty_list_not_an_error() -> None:
    # A query with no answers must not look like a failure, or the model
    # retries a search that will keep returning nothing.
    assert search("valid query", backend=ScriptedBackend([])) == []


@pytest.mark.parametrize("query", ["", "x"])
def test_a_too_short_query_is_rejected(query: str) -> None:
    with pytest.raises(InvalidArgument, match="at least"):
        search(query, backend=ScriptedBackend([]))


@pytest.mark.parametrize("count", [0, -1, MAX_RESULTS + 1])
def test_an_out_of_range_result_count_is_rejected(count: int) -> None:
    with pytest.raises(InvalidArgument, match="max_results"):
        search("valid query", max_results=count, backend=ScriptedBackend([]))


def test_a_backend_failure_surfaces_as_search_error() -> None:
    with pytest.raises(SearchError, match="Ratelimit"):
        search("valid query", backend=FailingBackend())
