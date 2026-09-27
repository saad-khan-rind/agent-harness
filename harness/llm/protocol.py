"""LLM client protocol used by the ReAct loop.

The loop depends on this ``Protocol``, never on the InnKube class. That is
what makes the loop testable without a network call and what makes swapping
providers an implementation detail rather than a rewrite (ADR-004).

The dataclasses below are a **normalisation layer**. Provider SDKs return
their own response objects; if those leaked into the loop, every provider
change would ripple through the control plane and every test would need a
mock shaped like the OpenAI SDK.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol


class LLMError(RuntimeError):
    """Raised when the model endpoint cannot be reached or answers badly.

    Distinct from a *tool* failure. A tool failure is an observation the
    agent can recover from on the next turn; an LLM failure means the loop
    has no next turn to take, so it surfaces to the CLI as a message to the
    user rather than being fed back into the context.
    """


@dataclass
class ToolCall:
    """One model-requested tool invocation.

    Attributes:
        id: Provider-assigned identifier. Must be echoed back on the ``tool``
            message or the API rejects the follow-up request.
        name: Tool name the model chose. Not trusted — the registry checks it.
        arguments: Decoded JSON arguments. Decoding happens in the client so
            malformed JSON is caught at the boundary, not in the loop.
        decode_error: Why ``arguments`` is empty, when it is empty *because
            decoding failed* rather than because the model sent none.

            The distinction is the whole point of the field. Without it both
            cases arrive as ``{}`` and the tool reports the only thing it can
            see — ``missing required argument 'text'`` — to a model that did
            send the text and watched its JSON get truncated. It reads that
            error, concludes it forgot an argument, sends the same broken
            call again, and repeats until a guard stops it. An observed run
            did exactly that three times in one turn.

            ``None`` means the arguments are trustworthy: either they parsed,
            or the model genuinely sent none.
    """

    id: str
    name: str
    arguments: dict[str, Any]
    decode_error: str | None = None


@dataclass
class LLMResponse:
    """Normalised chat completion the loop can inspect.

    Attributes:
        content: Assistant text, if any. Present alongside tool calls when
            the model narrates its reasoning before acting.
        tool_calls: Requested tool calls. An empty list is the loop's
            terminal condition: no action requested means final answer.
        prompt_tokens: Tokens in the request, as reported by the endpoint.
            ``0`` when the server omitted usage — a missing measurement,
            not a free call (ADR-031). We do not invent a substitute.
        completion_tokens: Tokens in the reply, same convention.
        finish_reason: Why the endpoint stopped generating — ``stop``,
            ``tool_calls``, ``length``, or whatever else it sends. ``None``
            when it sent none, which some OpenAI-compatible servers do.

            Carried for exactly one reason, and it is a good one:
            ``length`` means the answer was cut off mid-sentence, and a
            truncated answer is indistinguishable from a correct one on
            every other signal the harness has. The run succeeded, the
            model returned 200, no tool failed, and the user got half a
            sentence. Nothing in the control plane reads this — the loop
            still terminates on "no tool calls" and nothing else — so it is
            telemetry, not behaviour (ADR-094).
    """

    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str | None = None

    @property
    def total_tokens(self) -> int:
        """Return prompt plus completion tokens for this one call."""
        return self.prompt_tokens + self.completion_tokens


class LLMClient(Protocol):
    """Stateless chat interface. InnKube in production, a fake in tests.

    Stateless is deliberate: conversation state lives in
    :class:`~harness.context.RunContext`, so the same client instance can
    serve a parent agent and any number of sub-agents (Week 3) without their
    histories bleeding into each other.
    """

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        """Send messages and tool schemas, and return one response.

        Args:
            messages: OpenAI-style chat messages, oldest first.
            tools: Function-calling schemas from ``ToolRegistry.schemas()``.
                An empty list is legal and means "no tools this call";
                the InnKube client then omits the field entirely, because
                some OpenAI-compatible servers reject ``tools: []``.

        Returns:
            Assistant content and/or tool calls.

        Raises:
            LLMError: On transport failure, timeout, or an unusable reply.
        """
