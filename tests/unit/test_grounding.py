"""A final answer that claims an action the turn never took (ADR-116).

Observed against the lab endpoint: "Create a file called engine-check.md
containing the word podman, then tell me you did" was answered "I have
created the file" four runs out of four, with no tool call and an empty
workspace. These tests pin the check that sends such a reply back once.
"""

from pathlib import Path
from typing import Any

import pytest
from harness.context import RunContext
from harness.llm.grounding import (
    GROUNDING_NOTE,
    GroundingLLMClient,
    acted_this_turn,
    claims_an_action,
)
from harness.loop import AgentLoop
from harness.toolset import build_agent_registry
from tests.fakes import ScriptedLLM, call, text

_TOOLS: list[dict[str, Any]] = [{"type": "function", "function": {"name": "x"}}]


@pytest.mark.parametrize(
    "reply",
    [
        "I have created the file `engine-check.md` containing the word podman.",
        "I've written the summary to notes.md.",
        "The file has been saved.",
        "Done — I created output/numbers.md.",
        "I have successfully updated the config.",
        "The event was scheduled for 10:00.",
    ],
)
def test_completion_claims_are_recognised(reply: str) -> None:
    """The past-tense claims a model makes when it skips the tool."""
    assert claims_an_action(reply)


@pytest.mark.parametrize(
    "reply",
    [
        None,
        "",
        "Hello! How can I help?",
        "I can create that file if you want.",
        "I have not created the file, because writing is not allowed.",
        "The file was not saved.",
        "To create a file, tell me its name.",
        "notes.txt has 5 lines.",
    ],
)
def test_other_replies_are_not_claims(reply: str | None) -> None:
    """Offers, refusals, negations and plain answers pass untouched."""
    assert not claims_an_action(reply)


def test_a_tool_result_after_the_user_message_counts_as_acting() -> None:
    """Only this turn's evidence counts; an earlier turn's does not."""
    earlier = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": None, "tool_calls": []},
        {"role": "tool", "tool_call_id": "a", "content": "ok"},
    ]
    assert acted_this_turn(earlier)
    assert not acted_this_turn([*earlier, {"role": "user", "content": "two"}])


def test_an_ungrounded_claim_is_sent_back_once_and_usage_is_summed() -> None:
    """The second reply is returned; both calls' tokens are counted."""
    inner = ScriptedLLM(
        [
            text("I have created the file.", prompt_tokens=10, completion_tokens=2),
            call("write_file", prompt_tokens=12, completion_tokens=3),
        ]
    )
    client = GroundingLLMClient(inner)
    messages = [{"role": "user", "content": "make a file then tell me you did"}]

    response = client.chat(messages, _TOOLS)

    assert response.tool_calls and response.tool_calls[0].name == "write_file"
    assert (response.prompt_tokens, response.completion_tokens) == (22, 5)
    assert client.checks == 1
    resent = inner.calls[1][0]
    assert resent[-1] == {"role": "user", "content": GROUNDING_NOTE}
    # The caller's list is not touched: nothing is stored on its behalf.
    assert messages == [{"role": "user", "content": "make a file then tell me you did"}]


def test_it_never_asks_twice() -> None:
    """A model that repeats the claim is answered with its second reply."""
    inner = ScriptedLLM([text("I have created it."), text("I have created it.")])
    client = GroundingLLMClient(inner)

    response = client.chat([{"role": "user", "content": "go"}], _TOOLS)

    assert response.content == "I have created it."
    assert len(inner.calls) == 2


@pytest.mark.parametrize(
    ("messages", "tools"),
    [
        # Tools ran this turn: the claim has evidence behind it.
        (
            [
                {"role": "user", "content": "go"},
                {"role": "tool", "tool_call_id": "a", "content": "Wrote 3 bytes"},
            ],
            _TOOLS,
        ),
        # No tools offered: a harness-internal call, not the agent's.
        ([{"role": "user", "content": "go"}], []),
    ],
)
def test_it_does_not_intervene_when_the_claim_may_be_true(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> None:
    """Exactly one model call, and the reply passes through."""
    inner = ScriptedLLM([text("I have created the file.")])
    client = GroundingLLMClient(inner)

    assert client.chat(messages, tools).content == "I have created the file."
    assert len(inner.calls) == 1


def test_the_loop_writes_the_file_it_would_otherwise_only_claim(
    tmp_path: Path,
) -> None:
    """End to end through the real loop and file tools, model scripted.

    Without the check the run ends on the first reply and the workspace is
    empty; with it the file exists and the conversation holds no trace of
    the ungrounded claim.
    """
    inner = ScriptedLLM(
        [
            text("I have created the file engine-check.md."),
            call("write_file", path="engine-check.md", content="podman"),
            text("I created engine-check.md containing podman."),
        ]
    )
    registry = build_agent_registry(tmp_path)
    loop = AgentLoop(GroundingLLMClient(inner), registry)
    context = RunContext(task="")
    context.ensure_system_prompt("SYSTEM")
    context.start_turn("Create engine-check.md with podman, then tell me you did.")

    answer = loop.run(context)

    assert (tmp_path / "engine-check.md").read_text(encoding="utf-8") == "podman"
    assert "engine-check.md" in answer
    assert not any(m.get("content") == GROUNDING_NOTE for m in context.messages), (
        "the harness note leaked into the stored conversation"
    )
