"""What went wrong, as a bounded vocabulary — and whose fault it was.

Until now a failure in this harness was one bit. ``ToolOutcome.ok`` was
``False``, the observation started with ``Error:``, and the trace wrote
``ok: false``. That bit is enough to count failures and useless for
explaining them: a file that does not exist, an MCP server that died, a
policy that said no and a model that hallucinated a tool name all produce
exactly the same evidence, and §9 of our observability brief asks us to tell
them apart from the trace alone (ADR-094).

So every failure now carries an ``error.type`` from the list below, and
every ``error.type`` maps to a **responsible layer**. That second mapping is
the one that earns its keep. The question a person actually asks at 2am is
not "what was the exception" but "is this my agent, my tools, my policy, or
the model", and :data:`RESPONSIBLE_LAYER` answers it in one lookup — which
is what makes a single "failures by layer" panel possible instead of a wall
of error strings nobody reads.

**Bounded on purpose.** These values become a Prometheus label. An
unbounded label — an exception message, a file path, a server's error text
— is how a metrics database is made unusable inside a day (ADR-081). The
free text lives on the span in ``harness.error.message``, truncated, where
no metric reads it. :func:`classify_exception` therefore never invents a
value: anything it does not recognise becomes :data:`UNKNOWN`, and a new
entry here is a deliberate edit with a test that pins the list.

**Guards are not failures.** ``guard.*`` entries exist because the iteration
cap, the repetition stop and the retired-tool rule are the harness working
as designed (ADR-047, ADR-048, ADR-049). Filing them under errors would
make a healthy safety net look like a fault, which is the same mistake
:data:`~harness.observability.recorder.RunStatus` avoids by separating
``capped`` and ``no_progress`` from ``error``.
"""

from typing import Final

# --- the vocabulary ---------------------------------------------------------

UNKNOWN: Final = "unknown"

# The model endpoint. Split finely because the operational response differs:
# a rate limit is capacity, a timeout may be a too-large context, and an auth
# failure is a person's problem that no retry will fix.
LLM_TIMEOUT: Final = "llm.timeout"
LLM_RATE_LIMITED: Final = "llm.rate_limited"
LLM_UNAVAILABLE: Final = "llm.unavailable"
LLM_BAD_REQUEST: Final = "llm.bad_request"
LLM_AUTH: Final = "llm.auth"
LLM_MALFORMED_RESPONSE: Final = "llm.malformed_response"
LLM_ERROR: Final = "llm.error"

# What the *model* got wrong, as opposed to what the tool got wrong. These
# are the "the LLM succeeded but asked for something impossible" cases, and
# keeping them apart from `tool.*` is the difference between blaming the
# tool layer and blaming the model's tool selection.
TOOL_UNKNOWN: Final = "tool.unknown"
TOOL_INVALID_ARGUMENTS: Final = "tool.invalid_arguments"

# What the tool got wrong, or what the world did to it.
TOOL_NOT_FOUND: Final = "tool.not_found"
TOOL_PATH_ESCAPE: Final = "tool.path_escape"
TOOL_ACCESS_DENIED: Final = "tool.access_denied"
TOOL_TIMEOUT: Final = "tool.timeout"
TOOL_FAILED: Final = "tool.failed"

# A remote tool. Separated from `tool.*` so "which MCP server is responsible
# for a failure" is answerable without string-matching tool names.
MCP_CONNECTION: Final = "mcp.connection"
MCP_TOOL_ERROR: Final = "mcp.tool_error"
MCP_TIMEOUT: Final = "mcp.timeout"

# The permission layer. Two values because a policy denial and a person
# saying no are different events with different fixes (ADR-057, ADR-081).
PERMISSION_DENIED: Final = "permission.denied"
PERMISSION_DECLINED: Final = "permission.declined"

# Long-term memory. A missing store degrades a run rather than ending it
# (ADR-069), so these appear on a `memory.*` span whose parent `agent.run`
# is very often a success — which is exactly the case a single `ok` bit
# could never represent.
MEMORY_UNAVAILABLE: Final = "memory.unavailable"
MEMORY_EMBEDDING: Final = "memory.embedding"
MEMORY_FAILED: Final = "memory.failed"

# Delegation.
SUBAGENT_UNKNOWN_ROLE: Final = "subagent.unknown_role"
SUBAGENT_DEPTH_EXCEEDED: Final = "subagent.depth_exceeded"
SUBAGENT_BUDGET_EXHAUSTED: Final = "subagent.budget_exhausted"
SUBAGENT_INVALID_OUTPUT: Final = "subagent.invalid_output"
SUBAGENT_FAILED: Final = "subagent.failed"

# Guards doing their job. Not faults; see the module docstring.
GUARD_ITERATION_CAP: Final = "guard.iteration_cap"
GUARD_NO_PROGRESS: Final = "guard.no_progress"
GUARD_TOOL_RETIRED: Final = "guard.tool_retired"

# The agent itself: nothing below it failed, and the task still is not done.
# This is the residual category §9 cares most about, because a run that
# lands here is a *reasoning* problem and no amount of infrastructure work
# will fix it.
AGENT_INCOMPLETE: Final = "agent.incomplete"

# The observability layer, or the harness around it.
HARNESS_INTERNAL: Final = "harness.internal"

ERROR_TYPES: Final = frozenset(
    {
        UNKNOWN,
        LLM_TIMEOUT,
        LLM_RATE_LIMITED,
        LLM_UNAVAILABLE,
        LLM_BAD_REQUEST,
        LLM_AUTH,
        LLM_MALFORMED_RESPONSE,
        LLM_ERROR,
        TOOL_UNKNOWN,
        TOOL_INVALID_ARGUMENTS,
        TOOL_NOT_FOUND,
        TOOL_PATH_ESCAPE,
        TOOL_ACCESS_DENIED,
        TOOL_TIMEOUT,
        TOOL_FAILED,
        MCP_CONNECTION,
        MCP_TOOL_ERROR,
        MCP_TIMEOUT,
        PERMISSION_DENIED,
        PERMISSION_DECLINED,
        MEMORY_UNAVAILABLE,
        MEMORY_EMBEDDING,
        MEMORY_FAILED,
        SUBAGENT_UNKNOWN_ROLE,
        SUBAGENT_DEPTH_EXCEEDED,
        SUBAGENT_BUDGET_EXHAUSTED,
        SUBAGENT_INVALID_OUTPUT,
        SUBAGENT_FAILED,
        GUARD_ITERATION_CAP,
        GUARD_NO_PROGRESS,
        GUARD_TOOL_RETIRED,
        AGENT_INCOMPLETE,
        HARNESS_INTERNAL,
    }
)

# --- attribution ------------------------------------------------------------
# The layer a person should go and look at. Bounded to nine values, which is
# a legible legend on a graph; the error type itself is the drill-down.
Layer = str

LAYER_MODEL: Final = "model"
LAYER_TOOL: Final = "tool"
LAYER_MCP: Final = "mcp"
LAYER_POLICY: Final = "policy"
LAYER_MEMORY: Final = "memory"
LAYER_SUBAGENT: Final = "subagent"
LAYER_GUARD: Final = "guard"
LAYER_AGENT: Final = "agent"
LAYER_HARNESS: Final = "harness"
LAYER_UNKNOWN: Final = "unknown"

RESPONSIBLE_LAYER: Final[dict[str, Layer]] = {
    LLM_TIMEOUT: LAYER_MODEL,
    LLM_RATE_LIMITED: LAYER_MODEL,
    LLM_UNAVAILABLE: LAYER_MODEL,
    LLM_BAD_REQUEST: LAYER_MODEL,
    LLM_AUTH: LAYER_MODEL,
    LLM_MALFORMED_RESPONSE: LAYER_MODEL,
    LLM_ERROR: LAYER_MODEL,
    # The model picked a name that does not exist, or arguments that do not
    # fit the schema. The tool layer behaved correctly by refusing, so the
    # attribution is the agent's reasoning, not the tool.
    TOOL_UNKNOWN: LAYER_AGENT,
    TOOL_INVALID_ARGUMENTS: LAYER_AGENT,
    TOOL_NOT_FOUND: LAYER_TOOL,
    TOOL_PATH_ESCAPE: LAYER_TOOL,
    TOOL_ACCESS_DENIED: LAYER_TOOL,
    TOOL_TIMEOUT: LAYER_TOOL,
    TOOL_FAILED: LAYER_TOOL,
    MCP_CONNECTION: LAYER_MCP,
    MCP_TOOL_ERROR: LAYER_MCP,
    MCP_TIMEOUT: LAYER_MCP,
    PERMISSION_DENIED: LAYER_POLICY,
    PERMISSION_DECLINED: LAYER_POLICY,
    MEMORY_UNAVAILABLE: LAYER_MEMORY,
    MEMORY_EMBEDDING: LAYER_MEMORY,
    MEMORY_FAILED: LAYER_MEMORY,
    SUBAGENT_UNKNOWN_ROLE: LAYER_AGENT,
    SUBAGENT_DEPTH_EXCEEDED: LAYER_GUARD,
    SUBAGENT_BUDGET_EXHAUSTED: LAYER_GUARD,
    SUBAGENT_INVALID_OUTPUT: LAYER_SUBAGENT,
    SUBAGENT_FAILED: LAYER_SUBAGENT,
    GUARD_ITERATION_CAP: LAYER_GUARD,
    GUARD_NO_PROGRESS: LAYER_GUARD,
    GUARD_TOOL_RETIRED: LAYER_GUARD,
    AGENT_INCOMPLETE: LAYER_AGENT,
    HARNESS_INTERNAL: LAYER_HARNESS,
    UNKNOWN: LAYER_UNKNOWN,
}

LAYERS: Final = frozenset(RESPONSIBLE_LAYER.values())

# How much of an exception's text may reach telemetry. Short because the
# useful part of a message is its beginning and the dangerous part — a path,
# a query, a fragment of a document — tends to be what follows.
MAX_MESSAGE_CHARS: Final = 200

# Substrings that identify a failure when the exception type does not.
# Checked lowercase, most specific first. This is text matching, which is
# exactly what ADR-058 warns against for *control flow* — and is acceptable
# here because nothing branches on the answer: a wrong guess mislabels a
# graph, it does not change what the agent does.
_HTTP_STATUS_TYPES: Final[tuple[tuple[str, str], ...]] = (
    ("429", LLM_RATE_LIMITED),
    ("503", LLM_UNAVAILABLE),
    ("502", LLM_UNAVAILABLE),
    ("504", LLM_TIMEOUT),
    ("500", LLM_UNAVAILABLE),
    ("401", LLM_AUTH),
    ("403", LLM_AUTH),
    ("400", LLM_BAD_REQUEST),
    ("404", LLM_BAD_REQUEST),
)

_LLM_TEXT_TYPES: Final[tuple[tuple[str, str], ...]] = (
    ("rate limit", LLM_RATE_LIMITED),
    ("too many requests", LLM_RATE_LIMITED),
    ("timed out", LLM_TIMEOUT),
    ("timeout", LLM_TIMEOUT),
    ("unauthorized", LLM_AUTH),
    ("authentication", LLM_AUTH),
    ("api key", LLM_AUTH),
    ("no choices", LLM_MALFORMED_RESPONSE),
    ("could not be parsed", LLM_MALFORMED_RESPONSE),
    ("connection", LLM_UNAVAILABLE),
)


def layer_for(error_type: str | None) -> Layer:
    """Return the layer a person should investigate for ``error_type``.

    Args:
        error_type: A value from :data:`ERROR_TYPES`, or ``None``.

    Returns:
        One of :data:`LAYERS`. ``unknown`` for anything unrecognised, which
        keeps the label bounded even if a future error type forgets to
        register itself here.
    """
    if not error_type:
        return LAYER_UNKNOWN
    return RESPONSIBLE_LAYER.get(error_type, LAYER_UNKNOWN)


def safe_message(exc: BaseException) -> str:
    """Return a short, truncated description of an exception.

    The exception *class* is included because it is often the only
    non-sensitive part: ``FileNotFoundError`` tells a reader almost
    everything and reveals nothing, whereas the message that follows it is
    a path.

    Args:
        exc: The exception.

    Returns:
        ``"ClassName: first 200 characters"``.
    """
    text = str(exc).strip().replace("\n", " ")
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[:MAX_MESSAGE_CHARS] + "…"
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def classify_exception(exc: BaseException) -> str:
    """Map an exception to an :data:`ERROR_TYPES` entry.

    Ordered by how much the answer can be trusted. The exception *type* is
    checked first, because a class is a fact the code asserted; the message
    is only consulted when the class is too broad to be useful, which is the
    case for ``LLMError`` (one class covering every endpoint failure) and
    ``RuntimeError`` (which ``McpConnectionError`` and ``McpToolError``
    subclass so they survive the SDK's own exception hierarchy).

    Args:
        exc: Any exception raised inside an instrumented operation.

    Returns:
        An entry from :data:`ERROR_TYPES`, never a value outside it.
    """
    name = type(exc).__name__
    text = str(exc).lower()

    # -- transport and filesystem, by class -----------------------------
    if isinstance(exc, TimeoutError):
        return TOOL_TIMEOUT
    if isinstance(exc, FileNotFoundError):
        return TOOL_NOT_FOUND
    if isinstance(exc, PermissionError):
        return TOOL_ACCESS_DENIED

    # -- harness classes, by name -------------------------------------
    # By name rather than by import: this module is imported by the tracer,
    # which is imported by memory and by the loop. Importing `harness.llm`
    # and `harness.mcp` here would make three packages depend on each other
    # for the sake of an isinstance check that a string does just as well.
    if name == "PathEscapeError":
        return TOOL_PATH_ESCAPE
    if name == "McpConnectionError":
        return MCP_CONNECTION
    if name == "McpToolError":
        timed_out = "timed out" in text or "timeout" in text
        return MCP_TIMEOUT if timed_out else MCP_TOOL_ERROR
    if name == "LLMError":
        return _classify_llm_text(text)
    if name in ("ValidationError", "ValueError") and "argument" in text:
        return TOOL_INVALID_ARGUMENTS

    # -- generic transport ---------------------------------------------
    if isinstance(exc, OSError):
        return MCP_CONNECTION if "mcp" in text else TOOL_FAILED
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        return UNKNOWN
    return TOOL_FAILED if isinstance(exc, Exception) else UNKNOWN


def _classify_llm_text(text: str) -> str:
    """Narrow an ``LLMError`` using its message.

    ``LLMError`` is deliberately one class (ADR-004: the loop should not
    branch on provider taxonomy), so the detail that distinguishes a 429
    from a dead endpoint only survives in the message. Status codes are
    checked before words because they are unambiguous, whereas "connection"
    appears in plenty of messages that mean something else.
    """
    for needle, error_type in _HTTP_STATUS_TYPES:
        if needle in text:
            return error_type
    for needle, error_type in _LLM_TEXT_TYPES:
        if needle in text:
            return error_type
    return LLM_ERROR


def classify_tool_failure(observation: str, *, mcp: bool = False) -> str:
    """Map a failed tool observation to an :data:`ERROR_TYPES` entry.

    The last resort, and the only classifier in this module that reads text
    the *tool* produced rather than an exception the harness raised. It
    exists because ``Tool.invoke`` is a template method that converts every
    exception into an observation string before anything else sees it
    (see :mod:`harness.tools.base`) — by the time telemetry is involved the
    exception is gone.

    The long-term fix is the one this release makes: ``ToolOutcome`` now
    carries ``error_type``, set where the exception is still in scope, and
    this function is the fallback for an outcome that does not have one.

    Args:
        observation: The failed observation, ``Error:`` prefix included.
        mcp: Whether the tool was a remote one, which changes the default
            from ``tool.failed`` to ``mcp.tool_error``.

    Returns:
        An entry from :data:`ERROR_TYPES`.
    """
    text = observation.lower()
    if "not permitted by the active permission policy" in text:
        return PERMISSION_DENIED
    if "asked to approve" in text and "declined" in text:
        return PERMISSION_DECLINED
    if "times in a row and is treated as not working" in text:
        return GUARD_TOOL_RETIRED
    if "unknown tool" in text:
        return TOOL_UNKNOWN
    if "mcp server" in text:
        if "timed out" in text or "timeout" in text:
            return MCP_TIMEOUT
        if "connect" in text or "not connected" in text or "launch" in text:
            return MCP_CONNECTION
        return MCP_TOOL_ERROR
    if "outside the workspace" in text or "escape" in text:
        return TOOL_PATH_ESCAPE
    if "no such file" in text or "not found" in text or "does not exist" in text:
        return TOOL_NOT_FOUND
    if "timed out" in text or "timeout" in text:
        return TOOL_TIMEOUT
    if "permission denied" in text:
        return TOOL_ACCESS_DENIED
    if "invalid" in text or "expected" in text or "must be" in text:
        return TOOL_INVALID_ARGUMENTS
    return MCP_TOOL_ERROR if mcp else TOOL_FAILED
