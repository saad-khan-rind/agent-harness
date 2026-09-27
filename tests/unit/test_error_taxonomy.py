"""Every branch of the error taxonomy, because each one is a claim.

:mod:`harness.observability.errors` is small and almost entirely table
lookups, which makes it look like a module not worth testing. It is the
opposite: every entry is an assertion that *this* failure means *that* layer
is at fault, and a wrong one sends a person to the wrong place while looking
entirely correct on a dashboard. Getting `mcp.connection` filed under the
tool layer would mean somebody debugging their own file tools while an MCP
server was dead.

So the parametrised cases below are the specification, and reading them is
how you check the attribution without reading the table (ADR-094).
"""

import pytest
from harness.observability import errors
from harness.observability.errors import (
    ERROR_TYPES,
    LAYERS,
    MAX_MESSAGE_CHARS,
    RESPONSIBLE_LAYER,
    classify_exception,
    classify_tool_failure,
    layer_for,
    safe_message,
)


class _LLMError(RuntimeError):
    """Stands in for :class:`~harness.llm.protocol.LLMError`.

    Matched by class *name* in the classifier rather than by import, so a
    local double with the right name exercises the real path — and this test
    does not drag ``harness.llm`` into a module that deliberately imports
    nothing from ``harness``.
    """

    __name__ = "LLMError"


def _named(name: str, message: str) -> Exception:
    """Build an exception whose class name is ``name``."""
    return type(name, (RuntimeError,), {})(message)


# --- the vocabulary is closed ------------------------------------------------


def test_every_error_type_has_a_layer() -> None:
    """An unmapped type would fall through to ``unknown`` on a dashboard.

    The fall-through exists as a safety net so a new type cannot break a
    panel; this test is what keeps it a net rather than the normal path.
    """
    assert set(RESPONSIBLE_LAYER) >= ERROR_TYPES
    for error_type in ERROR_TYPES:
        assert layer_for(error_type) in LAYERS


def test_an_unknown_type_is_attributed_rather_than_dropped() -> None:
    """``None`` and nonsense both become ``unknown``, never a new label."""
    assert layer_for(None) == "unknown"
    assert layer_for("") == "unknown"
    assert layer_for("something.invented") == "unknown"


# --- exceptions --------------------------------------------------------------


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        # Filesystem, by class — a fact the code asserted, so checked first.
        (FileNotFoundError("ledger.txt"), errors.TOOL_NOT_FOUND),
        (PermissionError("denied"), errors.TOOL_ACCESS_DENIED),
        (TimeoutError("took too long"), errors.TOOL_TIMEOUT),
        # Harness classes, by name. By name rather than by import because
        # this module is imported by the tracer, which is imported by memory
        # and by the loop: importing `harness.llm` and `harness.mcp` here
        # would make three packages depend on each other for an isinstance.
        (_named("PathEscapeError", "outside the workspace"), errors.TOOL_PATH_ESCAPE),
        (_named("McpConnectionError", "server died"), errors.MCP_CONNECTION),
        (_named("McpToolError", "remote tool failed"), errors.MCP_TOOL_ERROR),
        (_named("McpToolError", "call timed out after 30s"), errors.MCP_TIMEOUT),
        # Generic transport.
        (OSError("broken pipe"), errors.TOOL_FAILED),
        (OSError("mcp transport died"), errors.MCP_CONNECTION),
        (ValueError("argument must be a string"), errors.TOOL_INVALID_ARGUMENTS),
        # Not a failure of anything: a person pressed Ctrl-C.
        (KeyboardInterrupt(), errors.UNKNOWN),
    ],
)
def test_exceptions_are_classified(exception: BaseException, expected: str) -> None:
    """Each mapping is an attribution claim; read the list as the spec."""
    assert classify_exception(exception) == expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # Status codes first, because they are unambiguous.
        ("InnKube request failed: 429 Too Many Requests", errors.LLM_RATE_LIMITED),
        ("InnKube request failed: 503 Service Unavailable", errors.LLM_UNAVAILABLE),
        ("InnKube request failed: 401 Unauthorized", errors.LLM_AUTH),
        ("the request timed out", errors.LLM_TIMEOUT),
        ("something entirely new went wrong", errors.LLM_ERROR),
    ],
)
def test_llm_errors_are_narrowed_by_their_message(message: str, expected: str) -> None:
    """``LLMError`` is deliberately one class, so the detail is in the text.

    ADR-004 says the loop must not branch on provider taxonomy, which is
    right — and it means the only place a 429 is distinguishable from a dead
    endpoint is the message. Text matching is acceptable here precisely
    because nothing branches on the answer: a wrong guess mislabels a graph
    and cannot change what the agent does.
    """
    assert classify_exception(_named("LLMError", message)) == expected


def test_classification_never_invents_a_value() -> None:
    """Whatever it is handed, the answer is inside the closed vocabulary."""
    for exception in (
        RuntimeError("?"),
        Exception(""),
        ZeroDivisionError("division by zero"),
        _named("SomethingNobodyAnticipated", "???"),
    ):
        assert classify_exception(exception) in ERROR_TYPES


# --- observations ------------------------------------------------------------


@pytest.mark.parametrize(
    ("observation", "expected"),
    [
        (
            "Error: write_file is not permitted by the active permission policy",
            errors.PERMISSION_DENIED,
        ),
        (
            "Error: the user was asked to approve this call and declined",
            errors.PERMISSION_DECLINED,
        ),
        (
            "Error: read_file failed 5 times in a row and is treated as not "
            "working for the rest of this task",
            errors.GUARD_TOOL_RETIRED,
        ),
        ("Error: unknown tool 'frobnicate'", errors.TOOL_UNKNOWN),
        ("Error: web MCP server: connection closed", errors.MCP_CONNECTION),
        ("Error: web MCP server: call timed out", errors.MCP_TIMEOUT),
        ("Error: web MCP server: the tool reported a failure", errors.MCP_TOOL_ERROR),
        ("Error: path is outside the workspace", errors.TOOL_PATH_ESCAPE),
        ("Error: No such file or directory: a.md", errors.TOOL_NOT_FOUND),
        ("Error: permission denied by the operating system", errors.TOOL_ACCESS_DENIED),
        ("Error: expected exactly one match", errors.TOOL_INVALID_ARGUMENTS),
    ],
)
def test_failed_observations_are_classified(observation: str, expected: str) -> None:
    """The fallback path, for a failure with no exception left to read.

    ``Tool.invoke`` converts every exception into a string before anything
    else sees it, so by the time telemetry is involved the exception is
    gone. The real fix is ``ToolOutcome.error_type``, set where the
    exception is still in scope; this is what happens when that is absent.
    """
    assert classify_tool_failure(observation) == expected


def test_an_unrecognised_failure_defaults_by_where_it_came_from() -> None:
    """A remote failure defaults to remote, a local one to local.

    Better than one shared default: an unclassifiable failure from an MCP
    server is still known to be *from an MCP server*, and that alone points
    at the right half of the system.
    """
    assert classify_tool_failure("Error: something odd") == errors.TOOL_FAILED
    assert classify_tool_failure("Error: something odd", mcp=True) == (
        errors.MCP_TOOL_ERROR
    )


# --- messages ----------------------------------------------------------------


def test_a_message_keeps_the_class_and_truncates_the_rest() -> None:
    """The class name is the part that is both useful and safe.

    ``FileNotFoundError`` tells a reader almost everything and reveals
    nothing; the message after it is a path.
    """
    message = safe_message(FileNotFoundError("/home/user/private/ledger.txt"))
    assert message.startswith("FileNotFoundError:")


def test_a_long_message_is_clipped() -> None:
    """An unbounded string on a span is an unbounded span."""
    message = safe_message(RuntimeError("x" * 5000))
    assert len(message) < MAX_MESSAGE_CHARS + 50
    assert message.endswith("…")


def test_an_exception_with_no_message_still_names_its_class() -> None:
    """The empty case must not produce a bare colon."""
    assert safe_message(RuntimeError()) == "RuntimeError"
