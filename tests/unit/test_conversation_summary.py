"""Folding old REPL turns into a rolling summary (ADR-108).

The guarantee is a bound: once a chat is over ``max_context_chars``, the
next turn starts with only the system prompt, a summary, the kept turns and
itself — whether or not a model is available to write the summary. The
invariant that must survive every fold is the API's: no ``tool`` message
without the assistant message that requested it.
"""

from harness.context import RunContext, message_chars
from harness.llm.protocol import LLMError
from harness.memory.conversation import (
    CONVERSATION_SECTION_HEADER,
    ConversationSummaryHook,
    fallback_summary,
    render_transcript,
)
from tests.fakes import FailingLLM, ScriptedLLM, text


def _conversation(turns: int, *, padding: int = 400) -> RunContext:
    """A REPL context with ``turns`` finished turns and a new one just started.

    Each finished turn is a user question, a tool call, its result, and an
    answer — the shape that makes a careless cut orphan a tool message.
    """
    context = RunContext(task="")
    context.ensure_system_prompt("You are a test agent.")
    for number in range(1, turns + 1):
        context.start_turn(f"question {number} " + "q" * padding)
        context.add_assistant_message(
            None,
            [
                {
                    "id": f"call-{number}",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a.md"}'},
                }
            ],
        )
        context.add_tool_result(f"call-{number}", "read_file", "contents " * 20)
        context.add_assistant_message(f"answer {number} " + "a" * padding)
    context.start_turn("the current question")
    return context


def _user_questions(context: RunContext) -> list[str]:
    """The user messages still in the conversation, first word pairs only."""
    return [
        " ".join(str(message["content"]).split()[:2])
        for message in context.messages
        if message.get("role") == "user"
    ]


def _assert_pairs_intact(context: RunContext) -> None:
    """Every tool result follows the assistant message that asked for it."""
    requested: set[str] = set()
    for message in context.messages:
        for call in message.get("tool_calls") or []:
            requested.add(call["id"])
        if message.get("role") == "tool":
            assert message["tool_call_id"] in requested


# --- when it folds ----------------------------------------------------------


def test_a_conversation_under_budget_is_left_alone() -> None:
    """No fold, no call, no section."""
    context = _conversation(3)
    before = [dict(message) for message in context.messages]
    llm = ScriptedLLM([])

    ConversationSummaryHook(10**6, llm).on_run_start(context)

    assert context.messages == before
    assert llm.calls == []


def test_zero_budget_disables_folding_as_it_disables_compaction() -> None:
    """``max_context_chars: 0`` means never compact, and never fold."""
    context = _conversation(5)
    count = len(context.messages)

    ConversationSummaryHook(0).on_run_start(context)

    assert len(context.messages) == count


def test_old_turns_are_folded_and_recent_ones_kept() -> None:
    """Keep two earlier turns and the current one; fold the rest."""
    context = _conversation(5)
    llm = ScriptedLLM([text("- User asked questions 1 to 3.")])

    ConversationSummaryHook(3_000, llm, keep_turns=2).on_run_start(context)

    assert _user_questions(context) == [
        "question 4",
        "question 5",
        "the current",
    ]
    assert context.conversation_summary == "- User asked questions 1 to 3."
    system = str(context.messages[0]["content"])
    assert CONVERSATION_SECTION_HEADER in system
    assert "User asked questions 1 to 3" in system
    _assert_pairs_intact(context)


def test_the_summariser_sees_a_transcript_and_no_tools() -> None:
    """One narrow request: previous summary plus rendered turns."""
    context = _conversation(4)
    llm = ScriptedLLM([text("- summary")])

    ConversationSummaryHook(2_000, llm, keep_turns=1).on_run_start(context)

    messages, tools = llm.calls[0]
    request = str(messages[1]["content"])
    assert tools == []
    assert "PREVIOUS SUMMARY:\n(none)" in request
    assert "User: question 1" in request
    assert "Assistant called read_file" in request
    assert "question 4" not in request


def test_the_summary_rolls_forward() -> None:
    """A second fold merges into the first rather than replacing it."""
    context = _conversation(4)
    llm = ScriptedLLM([text("- first summary"), text("- merged summary")])
    # No kept turns, so each start folds exactly once.
    hook = ConversationSummaryHook(2_000, llm, keep_turns=0)
    hook.on_run_start(context)

    context.add_assistant_message("answer current " + "a" * 2_000)
    context.start_turn("another question " + "q" * 2_000)
    hook.on_run_start(context)

    second_request = str(llm.calls[1][0][1]["content"])
    assert "PREVIOUS SUMMARY:\n- first summary" in second_request
    assert context.conversation_summary == "- merged summary"
    assert str(context.messages[0]["content"]).count(CONVERSATION_SECTION_HEADER) == 1


def test_kept_turns_are_folded_too_when_still_over_budget() -> None:
    """The kept turns are a preference; the budget is the rule."""
    context = _conversation(3, padding=3_000)

    ConversationSummaryHook(4_000, keep_turns=2).on_run_start(context)

    assert _user_questions(context) == ["the current"]
    _assert_pairs_intact(context)


def test_the_current_turn_is_never_folded() -> None:
    """Even alone and over budget: it is the question being answered."""
    context = RunContext(task="")
    context.ensure_system_prompt("You are a test agent.")
    context.start_turn("x" * 10_000)

    ConversationSummaryHook(1_000).on_run_start(context)

    assert _user_questions(context) == ["x" * 10_000]


# --- without a model --------------------------------------------------------


def test_a_failed_summariser_still_folds_into_a_plain_summary() -> None:
    """The bound does not depend on the endpoint."""
    context = _conversation(4)
    before = message_chars(context.messages)

    ConversationSummaryHook(
        2_000, FailingLLM(LLMError("down")), keep_turns=0
    ).on_run_start(context)

    assert message_chars(context.messages) < before
    lines = context.conversation_summary.splitlines()
    assert len(lines) == 4
    assert lines[0].startswith("- User asked: question 1")
    assert "-> answer 1" in lines[0]


def test_the_plain_summary_drops_oldest_lines_first() -> None:
    """Newest turns are the likeliest to be referred to."""
    previous = "\n".join(f"- User asked: old {n} -> fine" for n in range(50))
    old = [
        {"role": "user", "content": "newest question"},
        {"role": "assistant", "content": "newest answer"},
    ]

    summary = fallback_summary(previous, old, 200)

    assert len(summary) <= 200
    assert summary.splitlines()[-1] == (
        "- User asked: newest question -> newest answer"
    )
    assert "old 0 " not in summary


def test_an_unanswered_turn_says_so() -> None:
    """A turn that ended on an error has a question and no answer."""
    summary = fallback_summary("", [{"role": "user", "content": "q"}], 500)

    assert summary == "- User asked: q -> (no answer)"


def test_reset_forgets_the_summary() -> None:
    """``/reset`` means forget the conversation, including its summary."""
    context = _conversation(4)
    ConversationSummaryHook(2_000, keep_turns=1).on_run_start(context)
    assert context.conversation_summary

    context.reset()

    assert context.conversation_summary == ""


def test_the_transcript_is_clipped_per_message() -> None:
    """A giant old observation cannot fill the summariser's request."""
    rendered = render_transcript(
        [
            {"role": "user", "content": "short"},
            {"role": "tool", "name": "read_file", "content": "y" * 5_000},
            {"role": "tool", "name": "list_files", "content": ""},
        ]
    )

    lines = rendered.splitlines()
    assert lines[0] == "User: short"
    assert lines[1].startswith("Result of read_file: yyy")
    assert len(lines[1]) < 250
    assert lines[2] == "Result of list_files: "


# --- tool calls in the plain summary ----------------------------------------


def _assistant_calls(*calls: tuple[str, str]) -> dict[str, object]:
    """An assistant message requesting ``(name, arguments_json)`` calls."""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": f"call-{index}",
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
            for index, (name, arguments) in enumerate(calls)
        ],
    }


def test_the_plain_summary_keeps_which_files_each_turn_touched() -> None:
    """The path is what lets a later turn re-read a file instead of guessing."""
    old = [
        {"role": "user", "content": "summarise the notes"},
        _assistant_calls(
            ("read_file", '{"path": "notes.txt"}'),
            (
                "write_file",
                '{"content": "[compacted] 4,000 chars", "path": "out/s.md"}',
            ),
        ),
        {"role": "tool", "tool_call_id": "call-0", "name": "read_file", "content": "x"},
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "write_file",
            "content": "ok",
        },
        {"role": "assistant", "content": "Done."},
    ]

    summary = fallback_summary("", old, 1_000)

    assert summary == (
        "- User asked: summarise the notes -> Done. "
        "[tools: read_file(notes.txt), write_file(out/s.md)]"
    )


def test_a_call_without_a_path_shows_its_first_useful_argument() -> None:
    """A query or a fact says what the call was about; a stub says nothing."""
    old = [
        {"role": "user", "content": "q"},
        _assistant_calls(
            ("memory_search", '{"query": "which editor"}'),
            ("note_write", '{"content": "[compacted] 900 chars"}'),
            ("list_files", "{}"),
        ),
        {"role": "assistant", "content": "a"},
    ]

    summary = fallback_summary("", old, 1_000)

    assert summary.endswith(
        "[tools: memory_search(which editor), note_write, list_files]"
    )


def test_repeated_calls_collapse_and_a_long_turn_is_capped() -> None:
    """A bulk turn reads as a count, not forty copies of one call."""
    repeated = [("read_file", '{"path": "a.md"}')] * 3
    distinct = [("read_file", f'{{"path": "f{n}.md"}}') for n in range(10)]
    old = [
        {"role": "user", "content": "q"},
        _assistant_calls(*repeated, *distinct),
        {"role": "assistant", "content": "a"},
    ]

    summary = fallback_summary("", old, 2_000)

    assert "read_file(a.md) ×3, read_file(f0.md)" in summary
    assert summary.endswith("+3 more]")


def test_a_fold_without_a_model_records_the_tool_calls() -> None:
    """Through the hook: every folded turn names its file read."""
    context = _conversation(2)

    ConversationSummaryHook(1_500, keep_turns=0).on_run_start(context)

    lines = context.conversation_summary.splitlines()
    assert len(lines) == 2
    assert all(line.endswith("[tools: read_file(a.md)]") for line in lines)


# --- cheapest step first, and a budget folding can actually meet ------------


def test_pointers_are_tried_before_any_turn_is_folded() -> None:
    """Big old file reads: compaction alone fits, so nothing is folded.

    Before the fix the hook measured first and folded whole turns that
    turning their tool results into pointers would have made fit.
    """
    context = RunContext(task="")
    context.ensure_system_prompt("You are a test agent.")
    for number in range(1, 7):
        context.start_turn(f"read file {number}")
        context.add_assistant_message(
            None,
            [
                {
                    "id": f"call-{number}",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": f'{{"path": "f{number}.md"}}',
                    },
                }
            ],
        )
        context.add_tool_result(f"call-{number}", "read_file", "x" * 2_000)
        context.add_assistant_message(f"file {number} is all x")
    context.start_turn("the current question")
    llm = ScriptedLLM([])

    ConversationSummaryHook(6_000, llm, keep_turns=1).on_run_start(context)

    assert llm.calls == []
    assert context.conversation_summary == ""
    assert len(_user_questions(context)) == 7
    assert context.compactions == 1


def test_a_system_prompt_that_fills_the_budget_does_not_fold_every_turn() -> None:
    """The conversation keeps a quarter of the budget whatever the prompt holds.

    Before the fix a 30,000-character system prompt under a 24,000 budget
    made every turn "over budget": each one folded, each one paid for a
    summary, and none of it helped.
    """
    context = RunContext(task="")
    context.ensure_system_prompt("s" * 30_000)
    context.start_turn("first question")
    context.add_assistant_message("first answer")
    context.start_turn("second question")
    llm = ScriptedLLM([])

    ConversationSummaryHook(24_000, llm).on_run_start(context)

    assert llm.calls == []
    assert _user_questions(context) == ["first question", "second question"]


def test_the_floor_still_bounds_a_conversation_under_a_huge_prompt() -> None:
    """A floor, not an exemption: past a quarter of the budget, it folds."""
    context = RunContext(task="")
    context.ensure_system_prompt("s" * 30_000)
    for number in range(1, 6):
        context.start_turn(f"question {number} " + "q" * 2_000)
        context.add_assistant_message(f"answer {number} " + "a" * 2_000)
    context.start_turn("the current question")

    ConversationSummaryHook(24_000, keep_turns=1).on_run_start(context)

    assert message_chars(context.messages[1:]) <= 6_000
    assert _user_questions(context)[-1] == "the current"
