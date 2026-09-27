"""The delegation planner: fanning out is decided at run time (ADR-117).

Measured against the lab model, the agent never chose ``delegate`` on its
own — zero of six runs on tasks that split into independent parts — with
the old prompt or a rewritten one. These tests pin the planner that asks the
narrow question instead, and prove through the real loop, registry and
factory that the fan-out it produces runs as ordinary, gated delegation.
"""

import json
from typing import Any

import pytest
from harness.llm.protocol import LLMResponse
from harness.subagents.planner import (
    PLANNED_ROLE,
    DelegationPlanner,
    planned_tasks,
    render_turn,
)
from harness.subagents.roles import DELEGATE_TOOL_NAME
from tests.fakes import ScriptedLLM, call, text

_WITH_DELEGATE: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "read_file"}},
    {"type": "function", "function": {"name": DELEGATE_TOOL_NAME}},
]
_WITHOUT_DELEGATE = _WITH_DELEGATE[:1]

_SPLIT = {
    "split": True,
    "reason": "Three unrelated ledgers.",
    "tasks": [
        {"task": "Count ENTRY lines in alpha.txt", "context": "alpha.txt"},
        {"task": "Count ENTRY lines in beta.txt", "context": "beta.txt"},
        {"task": "Count ENTRY lines in gamma.txt", "context": "gamma.txt"},
    ],
}


def _messages(
    request: str = "Count the entries in alpha, beta and gamma",
) -> list[dict[str, Any]]:
    """Return a conversation whose turn has not read anything yet."""
    return [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": request},
    ]


def _planner(
    agent: list[LLMResponse], plans: list[str], max_tasks: int = 3
) -> tuple[DelegationPlanner, ScriptedLLM, ScriptedLLM]:
    """Build a planner over two scripted clients: the agent's and the question's."""
    inner = ScriptedLLM(agent)
    question = ScriptedLLM([text(plan) for plan in plans])
    return DelegationPlanner(inner, question, max_tasks), inner, question


def test_a_split_becomes_one_delegate_call_with_every_part() -> None:
    """The agent's first read is replaced by a batched ``delegate``."""
    planner, _, question = _planner(
        [call("read_file", path="alpha.txt", prompt_tokens=7)], [json.dumps(_SPLIT)]
    )

    response = planner.chat(_messages(), _WITH_DELEGATE)

    assert [c.name for c in response.tool_calls] == [DELEGATE_TOOL_NAME]
    tasks = response.tool_calls[0].arguments["tasks"]
    assert [t["task"] for t in tasks] == [t["task"] for t in _SPLIT["tasks"]]
    assert {t["role"] for t in tasks} == {PLANNED_ROLE}
    assert "3 independent parts" in (response.content or "")
    assert response.prompt_tokens == 7
    assert planner.splits == 1
    # The question carried the request and asked for JSON, and nothing else.
    sent_system, sent_user = question.calls[0][0]
    assert "JSON" in sent_system["content"]
    assert "alpha, beta and gamma" in sent_user["content"]
    assert question.calls[0][1] == []


@pytest.mark.parametrize(
    "answer",
    [
        '{"split": false, "reason": "One small file."}',
        "not json at all",
        '{"split": true, "tasks": [{"task": "only one part"}]}',
        '{"split": true, "tasks": "nope"}',
        '{"split": "yes", "tasks": []}',
    ],
)
def test_anything_short_of_a_clear_split_leaves_the_agent_alone(answer: str) -> None:
    """No split, bad JSON, one task: the agent's own reply goes through."""
    planner, _, _ = _planner([call("read_file", path="notes.txt")], [answer])

    response = planner.chat(_messages("How many lines in notes.txt?"), _WITH_DELEGATE)

    assert response.tool_calls[0].name == "read_file"
    assert planner.splits == 0


def test_a_failing_planner_call_never_fails_the_run() -> None:
    """The question is auxiliary: an exhausted script is just "no split"."""
    planner, _, _ = _planner([call("read_file", path="a.txt")], [])

    assert planner.chat(_messages(), _WITH_DELEGATE).tool_calls[0].name == "read_file"


@pytest.mark.parametrize(
    ("reply", "tools"),
    [
        # Not reading yet: listing is how the agent learns what the parts are.
        (call("list_files", path="."), _WITH_DELEGATE),
        (call("find_files", path=".", pattern="*"), _WITH_DELEGATE),
        # A final answer: nothing to plan.
        (text("17 times 23 is 391."), _WITH_DELEGATE),
        # No delegate offered: a child, or sub-agents switched off.
        (call("read_file", path="a.txt"), _WITHOUT_DELEGATE),
    ],
)
def test_it_only_asks_when_the_agent_starts_reading_and_can_delegate(
    reply: LLMResponse, tools: list[dict[str, Any]]
) -> None:
    """Any other moment passes straight through with no extra call."""
    planner, _, question = _planner([reply], [json.dumps(_SPLIT)])

    assert planner.chat(_messages(), tools) is reply
    assert question.calls == []


def test_it_asks_at_most_once_per_turn() -> None:
    """A "no" is final for the turn: the next read does not ask again."""
    planner, _, question = _planner(
        [call("read_file", path="a.txt"), call("read_file", path="b.txt")],
        ['{"split": false}', json.dumps(_SPLIT)],
    )
    messages = _messages()

    planner.chat(messages, _WITH_DELEGATE)
    planner.chat(messages, _WITH_DELEGATE)

    assert len(question.calls) == 1


def test_a_new_turn_is_asked_again() -> None:
    """The once-per-turn key is the user message, not the session."""
    planner, _, question = _planner(
        [call("read_file", path="a.txt"), call("read_file", path="b.txt")],
        ['{"split": false}', '{"split": false}'],
    )
    first = _messages("first request")
    planner.chat(first, _WITH_DELEGATE)
    planner.chat([*first, {"role": "user", "content": "second"}], _WITH_DELEGATE)

    assert len(question.calls) == 2


def test_a_turn_that_already_delegated_is_not_planned() -> None:
    """The agent chose to delegate itself; the planner stays out of it."""
    planner, _, question = _planner([call("read_file", path="a.txt")], [])
    messages = [
        *_messages(),
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "1", "function": {"name": DELEGATE_TOOL_NAME, "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "1", "content": "done"},
    ]

    planner.chat(messages, _WITH_DELEGATE)

    assert question.calls == []


def test_a_disabled_session_is_never_asked() -> None:
    """A read-only session refuses ``delegate``; planning it wastes a call."""
    inner = ScriptedLLM([call("read_file", path="a.txt")])
    question = ScriptedLLM([])
    planner = DelegationPlanner(inner, question, 3, enabled=lambda: False)

    assert planner.chat(_messages(), _WITH_DELEGATE).tool_calls[0].name == "read_file"
    assert question.calls == []


def test_no_parallelism_means_no_question() -> None:
    """With one task per call there is nothing to fan out."""
    planner, _, question = _planner(
        [call("read_file", path="a.txt")], [json.dumps(_SPLIT)], max_tasks=1
    )

    planner.chat(_messages(), _WITH_DELEGATE)

    assert question.calls == []


def test_tasks_are_capped_and_blank_ones_dropped() -> None:
    """Never more than one call accepts, never an empty task."""
    plan = {
        "split": True,
        "tasks": [{"task": f"part {n}"} for n in range(5)] + [{"task": "  "}, "x"],
    }

    tasks = planned_tasks(plan, 3)

    assert [t["task"] for t in tasks] == ["part 0", "part 1", "part 2"]


def test_the_question_sees_what_the_turn_found_but_not_whole_files() -> None:
    """A listing reaches the planner; a long file is clipped."""
    messages = [
        *_messages("Summarise each report"),
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "1", "function": {"name": "list_files", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "1", "content": "q1.md\nq2.md\n" + "x" * 5000},
    ]

    rendered = render_turn(messages, 1)

    assert "Summarise each report" in rendered
    assert "q1.md" in rendered and "q2.md" in rendered
    assert len(rendered) < 4000
