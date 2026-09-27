"""In-run context bookkeeping.

Small module, but every bug here shows up as a confusing API error or as an
agent that has quietly forgotten what it was doing.
"""

import json

from harness.context import (
    COMPACTION_SECTION_HEADER,
    COMPACTION_SUMMARY_LIMIT,
    RunContext,
    message_chars,
)


def test_system_prompt_is_seeded_once() -> None:
    """Repeated calls must not insert a second system message."""
    context = RunContext(task="")
    context.ensure_system_prompt("SYSTEM")
    context.ensure_system_prompt("SYSTEM")
    assert [m["role"] for m in context.messages] == ["system"]


def test_start_turn_records_the_task() -> None:
    """The current task is both stored and appended to the conversation."""
    context = RunContext(task="")
    context.ensure_system_prompt("SYSTEM")
    context.start_turn("first")
    assert context.task == "first"
    assert context.messages[-1] == {"role": "user", "content": "first"}


def test_reset_clears_in_place() -> None:
    """Callers hold a reference, so reset must mutate rather than replace."""
    context = RunContext(task="")
    context.ensure_system_prompt("SYSTEM")
    context.start_turn("something")
    context.plan = "a plan"
    context.record_usage(10, 4)
    messages = context.messages

    context.reset()

    assert context.messages == []
    assert context.plan is None
    assert context.prompt_tokens == 0
    assert context.completion_tokens == 0
    # Same list object: a caller holding it sees the clear.
    assert context.messages is messages


def _history_with_tool_results(count: int, blob: str) -> RunContext:
    """Build a session with ``count`` read_file observations of ``blob``."""
    context = RunContext(task="")
    context.ensure_system_prompt("SYSTEM")
    context.start_turn("go")
    for index in range(count):
        call_id = f"c{index}"
        context.add_assistant_message(
            None,
            [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        )
        context.add_tool_result(call_id, "read_file", f"{blob} file{index}")
    return context


def test_compact_stubs_old_tool_results_and_keeps_recent() -> None:
    """Older dumps become pointers; the latest ones and their ids stay."""
    context = _history_with_tool_results(6, "x" * 400)
    assert context.compact(max_chars=2500, keep_recent=2) is True

    tools = [message for message in context.messages if message["role"] == "tool"]
    assert len(tools) == 6
    assert all(message["content"].startswith("[compacted]") for message in tools[:-2])
    assert not tools[-1]["content"].startswith("[compacted]")
    assert not tools[-2]["content"].startswith("[compacted]")
    assert tools[0]["tool_call_id"] == "c0"
    assert tools[-1]["tool_call_id"] == "c5"
    # The system prompt's own text is never a compaction target. It does
    # gain a section — compaction writes its summary there (ADR-090) — so
    # the assertion is that the prompt survives, not that the message is
    # untouched.
    assert context.messages[0]["role"] == "system"
    assert context.messages[0]["content"].startswith("SYSTEM")


# -- compacting what the model sent -------------------------------------------


def _history_with_written_files(count: int, payload: str) -> RunContext:
    """Build a session of ``count`` write_file calls carrying ``payload``.

    The shape of a bulk task, and the one the observation-only compactor
    could not touch: the results are one short line each, and every byte
    worth reclaiming is on the assistant messages.
    """
    context = RunContext(task="")
    context.ensure_system_prompt("SYSTEM")
    context.start_turn("write a hundred files")
    for index in range(count):
        call_id = f"w{index}"
        context.add_assistant_message(
            None,
            [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps(
                            {"path": f"f{index}.md", "content": payload}
                        ),
                    },
                }
            ],
        )
        context.add_tool_result(call_id, "write_file", f"wrote f{index}.md")
    return context


def _arguments(context: RunContext) -> list[dict[str, str]]:
    """Return every tool call's decoded arguments, oldest first."""
    return [
        json.loads(call["function"]["arguments"])
        for message in context.messages
        if message.get("role") == "assistant"
        for call in message.get("tool_calls") or []
    ]


def test_compaction_shrinks_payload_arguments_not_just_results() -> None:
    """The bug this half of ADR-090 exists for.

    A bulk run's history is almost entirely what the model *sent*. With only
    observations stubbed, `compact` returned False on a conversation seven
    times over budget and the run died of a context the compactor had just
    declared fine.
    """
    context = _history_with_written_files(20, "x" * 2_000)
    before = message_chars(context.messages)

    assert context.compact(max_chars=5_000, keep_recent=2) is True
    assert message_chars(context.messages) < before // 2


def test_the_path_survives_and_the_payload_becomes_a_measurement() -> None:
    """A stub has to stay a pointer: which call this was, minus the payload.

    `path` is how the model knows f3.md is already written. Dropping it with
    the body would save marginally more and make the summary useless.
    """
    context = _history_with_written_files(8, "x" * 2_000)
    context.compact(max_chars=2_000, keep_recent=1)

    oldest = _arguments(context)[0]
    assert oldest["path"] == "f0.md"
    assert oldest["content"] == "[compacted] 2,000 chars"


# -- the summary the model reads instead --------------------------------------


def test_the_summary_lists_what_was_compacted() -> None:
    """The point of the ledger: "have I already written f3.md" without a call.

    Forty pointers scattered through a conversation answer that badly. One
    list at a fixed place in the system prompt answers it in a glance
    (ADR-090).
    """
    context = _history_with_written_files(8, "x" * 2_000)
    context.compact(max_chars=2_000, keep_recent=1)

    system = context.messages[0]["content"]
    assert COMPACTION_SECTION_HEADER in system
    assert "write_file (path=f0.md)" in system
    assert "do not redo them" in system


def test_a_long_run_drops_the_oldest_entries_and_says_how_many() -> None:
    """The summary is bounded too, or it becomes the thing crowding the window.

    Losing the detail of the oldest work while keeping the fact that it
    happened is the right trade in that order.
    """
    context = _history_with_written_files(COMPACTION_SUMMARY_LIMIT + 10, "x" * 2_000)
    context.compact(max_chars=2_000, keep_recent=1)

    system = context.messages[0]["content"]
    listed = [line for line in system.splitlines() if line.startswith("- write_file")]
    assert len(listed) == COMPACTION_SUMMARY_LIMIT
    assert "earlier calls not listed)" in system


def _unanswered_ids(messages: list[dict[str, object]]) -> list[str]:
    """Return every requested tool-call id that no tool message answers."""
    answered = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
    requested = [
        str(tool_call["id"])  # type: ignore[index]
        for m in messages
        if m.get("role") == "assistant"
        for tool_call in m.get("tool_calls") or []  # type: ignore[attr-defined]
    ]
    return [call_id for call_id in requested if call_id not in answered]


def test_a_turn_cut_off_mid_call_does_not_poison_the_next_request() -> None:
    """Every tool call is answered before the next user message is appended.

    A Ctrl-C while a tool runs, or a guard that stops the run partway
    through a multi-call reply, leaves an assistant message whose calls have
    no results. OpenAI-compatible servers reject that history outright, so
    one interrupted turn would fail every turn after it.
    """
    context = RunContext(task="")
    context.ensure_system_prompt("SYSTEM")
    context.start_turn("first")
    context.add_assistant_message(
        None,
        [
            {"id": "a", "type": "function", "function": {"name": "x", "arguments": ""}},
            {"id": "b", "type": "function", "function": {"name": "y", "arguments": ""}},
        ],
    )
    context.add_tool_result("a", "x", "done")

    context.start_turn("second")

    assert _unanswered_ids(context.messages) == []
    roles = [m["role"] for m in context.messages]
    # The stub sits with its siblings, before the new user message.
    assert roles == ["system", "user", "assistant", "tool", "tool", "user"]
    stub = context.messages[4]
    assert stub["tool_call_id"] == "b"
    assert stub["name"] == "y"
    assert "not run" in stub["content"]


def test_a_complete_history_is_left_untouched() -> None:
    """The repair is a no-op when nothing is missing."""
    context = RunContext(task="")
    context.ensure_system_prompt("SYSTEM")
    context.start_turn("first")
    context.add_assistant_message(
        None,
        [{"id": "a", "type": "function", "function": {"name": "x", "arguments": ""}}],
    )
    context.add_tool_result("a", "x", "done")
    context.add_assistant_message("answer")
    before = [dict(m) for m in context.messages]

    context.start_turn("second")

    assert context.messages[:-1] == before
