"""Test doubles for the LLM and MCP seams.

The whole point of ``LLMClient`` being a ``Protocol`` is that the loop can be
driven without a network call. These fakes are what cash that in: every loop
and E2E test below runs the *real* ``AgentLoop``, the *real* ``ToolRegistry``
and the *real* file tools, with only the model replaced.

The MCP doubles follow the same rule, one level down. What is faked is the
**server**, never the client: :func:`toy_mcp_server` builds a real
``MCPServer`` from the SDK, and the tests hand it to the real
``StdioMcpClient``, which speaks the real protocol to it. So the discovery
call, the adapter, the async bridge and the error mapping are all genuinely
exercised; only the thing on the far end is a toy (ADR-041).

Faking the *client* instead would have been easier and worth much less: it
would replace the code this slice exists to deliver, which is the mistake
ADR-014 already refused to make for the loop.

They live in ``tests/`` rather than in ``harness/`` so that no test-only
code ships in the container image (ADR-013).
"""

import threading
from dataclasses import dataclass, field
from typing import Any

from harness.llm.protocol import LLMResponse, ToolCall
from mcp.server.mcpserver import MCPServer
from mcp.types import Tool as RemoteToolDefinition
from mcp.types import ToolAnnotations


@dataclass
class ScriptedLLM:
    """An ``LLMClient`` that replays a fixed list of responses.

    Deterministic by construction, which is what makes it usable in CI: a
    real model would make these tests flaky and would need a token the brief
    says must not be committed.

    Attributes:
        responses: Replies to return, in order, one per ``chat`` call.
        calls: Every ``(messages, tools)`` pair received, so tests can assert
            on what the loop actually sent — the system prompt, the tool
            schemas, and that observations were appended in the right shape.
    """

    responses: list[LLMResponse]
    calls: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = field(
        default_factory=list
    )

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        """Return the next scripted response.

        Args:
            messages: Conversation the loop built.
            tools: Tool schemas the loop sent.

        Returns:
            The next scripted reply.

        Raises:
            AssertionError: If the loop asks for more turns than the script
                provides. Failing loudly beats returning a default, which
                would silently turn a runaway loop into a passing test.
        """
        # Copy the message list: the loop keeps mutating the same object, so
        # storing the reference would leave every recorded call pointing at
        # the final state.
        self.calls.append(([dict(message) for message in messages], list(tools)))
        assert self.responses, "ScriptedLLM ran out of responses"
        return self.responses.pop(0)


@dataclass
class FailingLLM:
    """An ``LLMClient`` that always raises, to test error propagation.

    Attributes:
        error: The exception instance to raise.
    """

    error: Exception

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        """Always raise ``self.error``."""
        raise self.error


class RoutedLLM:
    """An ``LLMClient`` whose reply depends on what it was asked.

    :class:`ScriptedLLM` replays one list in order, which is exactly right
    for one agent and unusable for several at once: concurrent children draw
    from the same list in whatever order the scheduler produces, so the test
    asserts on a race rather than on the harness.

    This routes instead. Each child's conversation carries a marker — its
    role in the system prompt, a phrase in its task — and the first matching
    script answers it. Two children therefore get their *own* answers no
    matter who reaches the model first, which is what makes a concurrent
    test deterministic.

    Attributes:
        routes: ``marker -> replies``. The marker is matched against the
            whole rendered conversation.
        fallback: Replayed when no marker matches, so a misrouted test fails
            on a readable assertion rather than a ``KeyError``.
        seen: Every marker that was matched, in the order the calls arrived.
            Order is not deterministic under concurrency; membership is, and
            membership is what the tests assert on.
    """

    def __init__(
        self,
        routes: dict[str, list[LLMResponse]],
        fallback: list[LLMResponse] | None = None,
    ) -> None:
        """Args:
        routes: Marker to the replies that conversation should receive.
        fallback: Replies for a conversation matching no marker.
        """
        self.routes = {marker: list(replies) for marker, replies in routes.items()}
        self.fallback = list(fallback or [])
        self.seen: list[str] = []
        self.calls: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []
        # Every child calls this object from its own thread. Without the
        # lock, two `pop(0)` calls can read the same element.
        self._lock = threading.Lock()

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        """Return the next reply for whichever route this conversation matches.

        Args:
            messages: Conversation the loop built.
            tools: Tool schemas the loop sent.

        Returns:
            The next reply on the matching route.

        Raises:
            AssertionError: If the matching route has run out, which means
                a child took more turns than the test expected.
        """
        rendered = " ".join(str(message.get("content") or "") for message in messages)
        with self._lock:
            self.calls.append(([dict(message) for message in messages], list(tools)))
            for marker, replies in self.routes.items():
                if marker in rendered:
                    self.seen.append(marker)
                    assert replies, f"RoutedLLM ran out of replies for {marker!r}"
                    return replies.pop(0)
            assert self.fallback, (
                "RoutedLLM had no route for this conversation and no fallback"
            )
            return self.fallback.pop(0)


class BlockingLLM:
    """An ``LLMClient`` that waits for a barrier before answering.

    Exists to prove children really do overlap. A test that merely observes
    two results cannot tell concurrent execution from fast sequential
    execution; a barrier can, because it only releases once the expected
    number of children have arrived at it. If they ran one after another the
    first would wait forever and the test would fail on a timeout rather
    than pass by luck.

    Attributes:
        barrier: Released when ``parties`` children have reached it.
        inner: Where the call goes once the barrier releases.
    """

    def __init__(self, inner: Any, parties: int, timeout: float = 5.0) -> None:
        """Args:
        inner: The client to delegate to once everyone has arrived.
        parties: How many concurrent callers to wait for.
        timeout: Seconds to wait before giving up, so a failure is a failed
            test rather than a hung suite.
        """
        self.inner = inner
        self.barrier = threading.Barrier(parties, timeout=timeout)
        self._armed = True
        self._lock = threading.Lock()

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        """Wait at the barrier on the first call per child, then delegate."""
        with self._lock:
            armed = self._armed
        if armed:
            # Only the first round waits: later turns of the same children
            # need not line up, and requiring them to would deadlock any
            # child that finishes in fewer turns than its siblings.
            self.barrier.wait()
            with self._lock:
                self._armed = False
        response: LLMResponse = self.inner.chat(messages, tools)
        return response


def text(
    content: str, prompt_tokens: int = 0, completion_tokens: int = 0
) -> LLMResponse:
    """Build a terminal response: assistant text, no tool calls.

    Args:
        content: The assistant's final answer.
        prompt_tokens: Optional usage for tests that assert on totals.
        completion_tokens: Optional usage for tests that assert on totals.

    Returns:
        A response that ends the loop.
    """
    return LLMResponse(
        content=content,
        tool_calls=[],
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def call(
    name: str,
    call_id: str = "call-1",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    **arguments: Any,
) -> LLMResponse:
    """Build a response containing exactly one tool call.

    Args:
        name: Tool to invoke.
        call_id: Identifier echoed back on the tool result message.
        prompt_tokens: Optional usage for tests that assert on totals.
        completion_tokens: Optional usage for tests that assert on totals.
        **arguments: Tool arguments.

    Returns:
        A response that keeps the loop running.
    """
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(id=call_id, name=name, arguments=dict(arguments))],
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


# --- MCP doubles -----------------------------------------------------------

# Name of the toy server's deletion tool. Named here so the deny-list tests
# assert against the same string the server advertises, rather than a copy
# that could drift.
DENIED_TOOL_NAME = "delete_everything"

# A tool whose *name* passes every word in the deny-list and whose
# annotations say plainly that it destroys things. The whole point of
# reading ``destructive_hint`` is that this one is refused too (ADR-059).
DESTRUCTIVE_TOOL_NAME = "sweep_stale_entries"


def toy_mcp_server(name: str = "toy") -> MCPServer:
    """Build a real in-process MCP server covering the cases that matter.

    The SDK lets a ``Client`` connect straight to a server object, so this
    needs no subprocess and no port while still being a real protocol
    conversation. Tests that specifically need the *stdio* transport launch
    ``tests/mcp_toy_server.py`` instead.

    The tool set is chosen to cover one branch each:

    * ``add`` — the ordinary case, with a usable docstring description.
    * ``ping`` — annotated ``read_only_hint``, so it must wrap as ``observe``
      while everything else defaults to ``mutate``.
    * ``explode`` — raises, so the client must turn a remote failure into an
      ``Error:`` observation rather than an exception.
    * ``delete_everything`` — must never reach the registry (ADR-042).
    * ``sweep_stale_entries`` — innocent name, ``destructive_hint: true``;
      refused on its annotation rather than its spelling (ADR-059).

    Args:
        name: Server label. Also the prefix the adapter applies.

    Returns:
        A server ready to hand to ``StdioMcpClient(target=...)``.
    """
    server = MCPServer(name=name)

    @server.tool()
    def add(a: int, b: int) -> str:
        """Add two whole numbers and return the sum."""
        return str(a + b)

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def ping() -> str:
        """Report that the server is alive and answering."""
        return "pong"

    @server.tool()
    def explode() -> str:
        """Always fail, so the error path can be tested."""
        raise ValueError("the toy server exploded on purpose")

    @server.tool(name=DENIED_TOOL_NAME)
    def delete_everything() -> str:
        """Offer a destructive capability the harness must refuse."""
        return "nothing was deleted"

    @server.tool(
        name=DESTRUCTIVE_TOOL_NAME,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True),
    )
    def sweep_stale_entries() -> str:
        """Declare itself destructive under an innocent name (ADR-059)."""
        return "nothing was swept"

    return server


def remote_tool_definition(
    name: str,
    description: str | None = None,
    input_schema: dict[str, Any] | None = None,
    read_only: bool | None = None,
    open_world: bool | None = None,
) -> RemoteToolDefinition:
    """Build one raw ``mcp.Tool`` as a server would advertise it.

    Used by the adapter tests, which are about *normalising* definitions and
    therefore need to construct hostile ones — no description, a schema with
    undescribed properties, a name that is not snake_case — that a
    well-behaved server would never produce.

    Args:
        name: Tool name as advertised.
        description: Description, or ``None`` for a server that sent none.
        input_schema: JSON Schema, defaulting to an empty object schema.
        read_only: Value for the ``read_only_hint`` annotation, or ``None``
            to advertise no annotations at all (unless ``open_world`` is
            given, in which case the annotations carry only that hint).
        open_world: Value for the ``open_world_hint`` annotation, or
            ``None`` to leave it unset.

    Returns:
        The raw definition, ready to pass to ``wrap_remote_tools``.
    """
    return RemoteToolDefinition(
        name=name,
        description=description,
        input_schema=input_schema
        if input_schema is not None
        else {"type": "object", "properties": {}},
        annotations=(
            None
            if read_only is None and open_world is None
            else ToolAnnotations(read_only_hint=read_only, open_world_hint=open_world)
        ),
    )


def parallel_calls(
    *specs: tuple[str, dict[str, Any]],
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> LLMResponse:
    """Build one response asking for several tool calls at once.

    Models do this, and it is the case the flat Week 3 trace could not
    represent: with two calls in a single turn, the events interleave and
    any rule that pairs a permission decision with "the tool line after it"
    attributes one of them to the wrong call. Span parentage makes the
    pairing a fact, and this helper is how a test can produce the situation
    that proves it (ADR-093).

    Args:
        *specs: ``(tool_name, arguments)`` pairs, in request order.
        prompt_tokens: Optional usage for tests that assert on totals.
        completion_tokens: Optional usage for tests that assert on totals.

    Returns:
        A response carrying every requested call, each with its own id — the
        API pairs results to calls by that id, so duplicates would produce a
        conversation the endpoint rejects.
    """
    return LLMResponse(
        content=None,
        tool_calls=[
            ToolCall(id=f"call-{index}", name=name, arguments=dict(arguments))
            for index, (name, arguments) in enumerate(specs, start=1)
        ],
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def malformed_call(
    name: str,
    decode_error: str = (
        'the arguments were not valid JSON. Received: \'{"text": "bir\''
    ),
    call_id: str = "call-1",
) -> LLMResponse:
    """Build a response whose one tool call had unparseable arguments.

    The shape a model produces when it truncates its own JSON — empty
    arguments plus a reason. Separate from :func:`call` because the two are
    genuinely different events: ``call(name)`` is a model that sent no
    arguments, and the loop must answer them differently or the model cannot
    tell which mistake it made.

    Args:
        name: Tool the model tried to invoke.
        decode_error: Why the arguments did not parse.
        call_id: Identifier echoed back on the tool result message.

    Returns:
        A response that keeps the loop running.
    """
    return LLMResponse(
        content=None,
        tool_calls=[
            ToolCall(id=call_id, name=name, arguments={}, decode_error=decode_error)
        ],
        prompt_tokens=0,
        completion_tokens=0,
    )
