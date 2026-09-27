"""Hold a final answer to what the turn actually did.

A model can end a turn by *saying* it acted instead of acting. Against the
lab's endpoint this is not rare: asked to "create a file called x.md
containing the word podman, then tell me you did", the model replied "I have
created the file" four runs out of four without calling a tool, and the
workspace stayed empty. Worded without the last clause, it wrote the file
every time. Nothing downstream can tell: the run succeeds, the answer reads
well, and every metric is green.

This wrapper is the structural check the brief's generate-then-verify
pattern asks for (Section 2), placed at the model-call seam rather than in
``loop.py``, which stays free of it for the same reason it stays free of the
budget (ADR-088). A reply is **ungrounded** when all three hold:

1. It would end the turn — tools were offered and none was called.
2. It claims a completed action ("I have created", "has been saved", ...).
3. No tool has returned anything since the user's last message.

An ungrounded reply is sent back once, with a note naming the gap, and the
second reply is what the loop receives: usually the tool call that should
have come first, sometimes an honest correction. It is never sent back
twice, so the check costs at most one model call per turn and cannot loop.

What it deliberately does not do: judge whether a claim is *true* when tools
did run (that is the task-quality verdict, ADR-096), or rewrite the model's
text. It only refuses to let a turn with no evidence end on a claim of
success (ADR-116).
"""

import re
from typing import Any

from harness.llm.protocol import LLMClient, LLMResponse

# Past-tense completion claims about the kinds of thing this harness's tools
# do: files, folders, notes, memory, calendar events. Anchored on a subject
# or an auxiliary so that "I can create", "to create" and "should be saved"
# do not match, and case-insensitive because models capitalise freely.
_CLAIM = re.compile(
    r"\b(?:i|i'?ve|i have|it|this|that|they|file|folder|note|event|"
    r"has been|have been|was|were|is now|are now)\s+"
    r"(?:now\s+|successfully\s+|just\s+|already\s+|also\s+)*"
    r"(?:created|wrote|written|saved|updated|added|appended|edited|modified|"
    r"stored|scheduled|booked|recorded|made the change|made the changes)\b",
    re.IGNORECASE,
)

# The note the model gets back. Worded so the honest answer to a question
# about an *earlier* turn stays available — "repeat your answer" — because
# the check cannot tell a fresh claim from a recollection, and a recollection
# is not an error.
GROUNDING_NOTE = (
    "[harness check] Your reply says an action was completed, but no tool "
    "has been called since the user's last message, so nothing was done in "
    "this turn. If the action is still needed, call the tool now. If you "
    "are describing work finished in an earlier turn, or the action is not "
    "possible, answer again and say so plainly. Do not claim an action that "
    "no tool result shows."
)


def claims_an_action(text: str | None) -> bool:
    """Return whether ``text`` reports a completed action.

    Args:
        text: Assistant content, possibly ``None``.

    Returns:
        ``True`` when a completion claim is found.
    """
    return bool(text) and _CLAIM.search(text or "") is not None


def acted_this_turn(messages: list[dict[str, Any]]) -> bool:
    """Return whether any tool answered since the last user message.

    Args:
        messages: The conversation as the loop is about to send it.

    Returns:
        ``True`` if a ``tool`` message follows the last ``user`` message.
    """
    for message in reversed(messages):
        role = message.get("role")
        if role == "tool":
            return True
        if role == "user":
            return False
    return False


class GroundingLLMClient:
    """An :class:`~harness.llm.protocol.LLMClient` that re-asks ungrounded replies.

    Wraps whatever client the session built — the budgeted one, so the extra
    call is metered like any other. Satisfies the protocol structurally.
    """

    def __init__(self, inner: LLMClient) -> None:
        """Args:
        inner: The client every request is forwarded to.
        """
        self._inner = inner
        # How many replies were sent back, for the session and the tests.
        # Read-only from outside; one int, so no lock is needed for the
        # worker threads concurrent sub-agents share this instance on.
        self.checks = 0

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        """Forward the request, and re-ask once if the reply is ungrounded.

        Args:
            messages: OpenAI-style chat messages.
            tools: Function-calling schemas, or an empty list for none.

        Returns:
            The reply the loop should act on. When a re-ask happened, its
            token counts include both calls, so usage is not undercounted.
        """
        response = self._inner.chat(messages, tools)
        if (
            not tools
            or response.tool_calls
            or not claims_an_action(response.content)
            or acted_this_turn(messages)
        ):
            return response

        self.checks += 1
        # Sent, never stored: the loop appends only the reply returned here,
        # so the conversation keeps no trace of the ungrounded claim or of
        # this note, and the next request is well formed either way.
        retry = self._inner.chat(
            [
                *messages,
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": GROUNDING_NOTE},
            ],
            tools,
        )
        retry.prompt_tokens += response.prompt_tokens
        retry.completion_tokens += response.completion_tokens
        return retry
