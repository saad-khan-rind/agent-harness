"""Web search, with no MCP in sight.

The same split :mod:`mcp_server_time_plus.meeting` makes: this module owns
the *capability* — validating a query, calling a backend, bounding what
comes back — and knows nothing about tools, schemas or the protocol. That
is what lets it be tested exhaustively without a protocol conversation, and
it is what lets the backend be swapped without touching the interface.

**Why a backend protocol rather than a direct ``ddgs`` call.** Two reasons,
and the second is the one that matters. Swapping DuckDuckGo for a keyed
provider later should be a new class, not a rewrite (ADR-055). And the test
suite must pass with no network and no API key — ``.github/workflows/ci.yml`` says so
in as many words — so the tests inject a scripted backend and the real code
path runs unchanged. It is the same move ``ScriptedLLM`` makes for the model
and ``toy_mcp_server`` makes for MCP (ADR-013, ADR-041).
"""

import contextlib
import sys
from dataclasses import dataclass
from typing import Any, Protocol

# Bounds quoted in the tool schema, so the model is told them rather than
# discovering them through a failed call (ADR-045).
MIN_RESULTS = 1
MAX_RESULTS = 10
DEFAULT_RESULTS = 5

# Query bounds. The maximum is generous but finite: a "query" of several
# kilobytes is a sign the model has pasted a document into the argument,
# which no search backend answers usefully.
MIN_QUERY_CHARS = 2
MAX_QUERY_CHARS = 400

# Per-result snippet cap. Measured, not guessed: a live DuckDuckGo result
# averages ~390 characters, so five results land near 1,950 and ten near
# 3,900 against the harness's 4,000-character observation budget. Clipping
# each snippet keeps a ten-result call inside that budget instead of
# relying on the harness to truncate the tail away (ADR-055).
MAX_SNIPPET_CHARS = 300


class InvalidArgument(ValueError):
    """Raised when an argument cannot be used as given.

    Separate from :class:`SearchError` because the two deserve different
    advice: an invalid argument is something the model can fix on the next
    turn, while a backend failure is something it can only retry.
    """


class SearchError(RuntimeError):
    """Raised when the backend could not answer.

    Covers a throttle, a transport failure, and a backend that returned
    something unparseable. All three are transient from the agent's point
    of view, so they are one type.
    """


@dataclass(frozen=True)
class Result:
    """One search hit, reduced to the three fields the model needs.

    Frozen because a result is a value. Deliberately *not* the backend's own
    dictionary: pinning the shape here means a backend swap cannot silently
    change what reaches the model.

    Attributes:
        title: Page title as the backend reported it.
        url: Absolute URL of the page.
        snippet: Short description, clipped to :data:`MAX_SNIPPET_CHARS`.
    """

    title: str
    url: str
    snippet: str

    def as_dict(self) -> dict[str, str]:
        """Return this result as the JSON object the server will emit."""
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


class SearchBackend(Protocol):
    """Whatever actually performs a search.

    One method, chosen to be the smallest thing a provider must implement.
    ``ddgs`` satisfies it today; a Tavily or Serper client would satisfy it
    with about fifteen lines.
    """

    def text(self, query: str, max_results: int) -> list[dict[str, Any]]:
        """Return raw backend hits, newest-relevance first.

        Args:
            query: Already-validated search terms.
            max_results: How many hits to return, already bounded.

        Returns:
            Backend-shaped dictionaries. Missing keys are tolerated by the
            caller; an exception means the search failed.
        """
        ...


class DdgsBackend:
    """The shipped backend: DuckDuckGo through the ``ddgs`` metasearch library.

    ``ddgs`` rotates across several engines rather than hitting one, which is
    what makes a key-free backend survivable: when DuckDuckGo throttles, the
    library falls through to another engine instead of failing the call.
    That was the deciding factor in choosing it over a single-engine scraper
    (ADR-055).

    The import is deferred to :meth:`text` rather than done at module scope,
    so the unit tests — which inject a scripted backend — never import
    ``ddgs`` at all and the suite stays runnable without it.
    """

    def __init__(self, timeout: float = 20.0) -> None:
        """Store the per-search timeout. Imports nothing yet.

        Args:
            timeout: Seconds allowed for one backend search.
        """
        self._timeout = timeout

    def text(self, query: str, max_results: int) -> list[dict[str, Any]]:
        """Run one search against DuckDuckGo.

        Args:
            query: Already-validated search terms.
            max_results: How many hits to return.

        Returns:
            Raw ``ddgs`` result dictionaries.

        Raises:
            SearchError: If the library raised for any reason. The original
                is attached as ``__cause__``; the model gets the message.
        """
        from ddgs import DDGS

        try:
            # stdout is the MCP protocol channel. A dependency that prints a
            # progress line — and this one does — would inject text straight
            # into the JSON-RPC stream and desynchronise the session. Any
            # stray write is redirected to stderr, where it lands in the
            # server log and harms nothing (ADR-055).
            # DDGS types its timeout as whole seconds; the harness works
            # in floats throughout, so the conversion happens here rather
            # than forcing an int up through the server's signature.
            with contextlib.redirect_stdout(sys.stderr):
                with DDGS(timeout=int(self._timeout)) as client:
                    return list(client.text(query, max_results=max_results))
        except Exception as exc:  # noqa: BLE001 - normalised, not swallowed
            # Deliberately broad and deliberately here: this is the one line
            # facing a third-party library whose failure modes are
            # open-ended (throttles, transport errors, HTML that stopped
            # parsing). The interface above needs one type it can map to a
            # readable observation, the same normalisation StdioMcpClient
            # performs at the MCP SDK boundary (ADR-004, ADR-039).
            raise SearchError(f"search backend failed: {exc}") from exc


def search(
    query: str,
    max_results: int = DEFAULT_RESULTS,
    backend: SearchBackend | None = None,
) -> list[Result]:
    """Search the web and return bounded, normalised results.

    Args:
        query: What to search for.
        max_results: How many hits to return, 1 to 10.
        backend: Where to search. Defaults to :class:`DdgsBackend`;
            the tests pass a scripted one.

    Returns:
        Results in the backend's own ranking order, each with a clipped
        snippet. An empty list is a valid answer — a query with no hits is
        not an error, and reporting it as one would make the model retry a
        search that will keep returning nothing.

    Raises:
        InvalidArgument: If the query or the result count is unusable.
        SearchError: If the backend could not answer.
    """
    cleaned = " ".join(query.split())
    if len(cleaned) < MIN_QUERY_CHARS:
        raise InvalidArgument(
            f"query must be at least {MIN_QUERY_CHARS} characters, got {cleaned!r}"
        )
    if len(cleaned) > MAX_QUERY_CHARS:
        raise InvalidArgument(
            f"query must be at most {MAX_QUERY_CHARS} characters, got {len(cleaned)}"
        )
    if not MIN_RESULTS <= max_results <= MAX_RESULTS:
        raise InvalidArgument(
            f"max_results must be between {MIN_RESULTS} and {MAX_RESULTS}, "
            f"got {max_results}"
        )

    hits = (
        backend.text(cleaned, max_results)
        if backend
        else DdgsBackend().text(cleaned, max_results)
    )

    results: list[Result] = []
    for hit in hits:
        # A hit with no URL cannot be fetched or cited, so it is worse than
        # useless to the model — it invites a citation that goes nowhere.
        url = str(hit.get("href") or hit.get("url") or "").strip()
        if not url:
            continue
        results.append(
            Result(
                title=_one_line(hit.get("title"), fallback=url),
                url=url,
                snippet=_clip(_one_line(hit.get("body"), fallback="")),
            )
        )
        if len(results) >= max_results:
            break
    return results


def _one_line(raw: object, fallback: str) -> str:
    """Collapse a backend field to a single line, or fall back.

    Args:
        raw: Whatever the backend put in the field, possibly ``None``.
        fallback: Used when the field is empty.

    Returns:
        Whitespace-collapsed text, never containing a newline.
    """
    text = " ".join(str(raw or "").split())
    return text or fallback


def _clip(text: str) -> str:
    """Clip one snippet, marking it when it clips.

    A silent clip would let the model treat half a sentence as the whole
    description, which is the same reasoning behind ``tools.base.truncate``.
    """
    if len(text) <= MAX_SNIPPET_CHARS:
        return text
    return text[: MAX_SNIPPET_CHARS - 1].rstrip() + "…"


__all__ = [
    "DEFAULT_RESULTS",
    "MAX_QUERY_CHARS",
    "MAX_RESULTS",
    "MAX_SNIPPET_CHARS",
    "MIN_QUERY_CHARS",
    "MIN_RESULTS",
    "DdgsBackend",
    "InvalidArgument",
    "Result",
    "SearchBackend",
    "SearchError",
    "search",
]
