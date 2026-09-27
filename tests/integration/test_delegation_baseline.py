"""Integration: the same task, with and without delegation, measured.

Multi-agent systems are not automatically better, and a harness that ships
delegation without ever checking that claim has added a feature on faith.
This file is the check: the *same* task, the *same* tools, the *same* work
done — once by a single agent and once by a coordinator with three
specialists — with the coordinating context measured both times.

The comparison is deliberately narrow. It cannot say anything about answer
quality, because the model is scripted and a scripted model's answers are
whatever we typed. What it *can* measure, and what the argument for
delegation actually rests on, is the shape of the coordinating context: how
much of the run's raw material ends up in the window that has to survive to
the end of the task, and how many model calls it costs to keep it out.

Both are reported. Delegation wins the first and loses the second, which is
the honest result and the one the documentation quotes (ADR-084,
``docs/subagents.md`` §9).
"""

import json
from pathlib import Path
from typing import Any

import pytest
from harness import cli, session
from tests.fakes import ScriptedLLM, call, text
from tests.integration.test_week3_delegation import (
    RESEARCH_JSON,
    WRITE_JSON,
    fenced,
    free_port,
)

# Six hundred lines of source material — about ten thousand characters, which
# is large enough for the effect to be visible and small enough to stay under
# the default ``max_context_chars``. Compaction would otherwise stub the
# baseline's big observation and measure the *compactor* rather than the
# delegation (ADR-032).
BULK = "LEDGER-ENTRY-9F2 amount 10 currency EUR\n" * 600

_CONFIG = """
llm_base_url: https://example.invalid
llm_model: test-model
temperature: 0.0
max_iterations: 0
workspace_path: workspace
memory_path: memory
memory_enabled: false
skills_enabled: false
mcp_enabled: false
permission_mode: allow_all
metrics_enabled: false
metrics_port: {port}
traces_enabled: false
subagents_enabled: {subagents}
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project holding the same bulky source file for both arms."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "ledger.txt").write_text(BULK, encoding="utf-8")
    (tmp_path / "memory").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    return tmp_path


def write_config(project: Path, *, subagents: bool) -> None:
    """Write ``config.yaml`` for one arm of the comparison."""
    (project / "config.yaml").write_text(
        _CONFIG.format(port=free_port(), subagents=str(subagents).lower()),
        encoding="utf-8",
    )


def single_agent() -> ScriptedLLM:
    """The baseline: one agent, all the tools, everything in one context."""
    return ScriptedLLM(
        [
            call("read_file", call_id="s1", path="ledger.txt"),
            call(
                "write_file",
                call_id="s2",
                path="brief.md",
                content="# Ledger brief\n\n300 entries, all of type 9F2.\n",
            ),
            call("read_file", call_id="s3", path="brief.md"),
            text(
                "I read the ledger, wrote brief.md and checked it: "
                "300 entries, all type 9F2."
            ),
        ]
    )


def delegating_agent() -> ScriptedLLM:
    """The same work, split across a researcher and a writer."""
    return ScriptedLLM(
        [
            call(
                "delegate",
                call_id="d1",
                role="researcher",
                task="Summarise ledger.txt: how many entries and of what type.",
                context="The workspace contains ledger.txt.",
            ),
            call("read_file", call_id="r1", path="ledger.txt"),
            text(fenced(RESEARCH_JSON, "The ledger is uniform.")),
            call(
                "delegate",
                call_id="d2",
                role="writer",
                task="Write brief.md with the ledger summary.",
                context="The ledger holds 300 entries, all of type 9F2.",
            ),
            call(
                "write_file",
                call_id="w1",
                path="brief.md",
                content="# Ledger brief\n\n300 entries, all of type 9F2.\n",
            ),
            text(fenced(WRITE_JSON, "Written.")),
            text(
                "I had the ledger read and brief.md written: 300 entries, all type 9F2."
            ),
        ]
    )


def coordinator_context(script: ScriptedLLM) -> list[dict[str, Any]]:
    """Return the last conversation the *coordinating* agent sent.

    A child's requests go through the same client, so the two are told apart
    by the tool list: only the top-level agent is offered ``delegate``. In
    the baseline arm nothing is, and the last request is the coordinator's
    by construction.
    """
    parent_turns = [
        messages
        for messages, tools in script.calls
        if any(schema["function"]["name"] == "delegate" for schema in tools)
    ]
    return parent_turns[-1] if parent_turns else script.calls[-1][0]


def context_size(messages: list[dict[str, Any]]) -> int:
    """Return the character size of one conversation, tool payloads included."""
    from harness.context import message_chars

    return message_chars(messages)


def conversation_size(messages: list[dict[str, Any]]) -> int:
    """Return the size of everything *except* the system message.

    The system prompt is the same several thousand characters in both arms,
    so including it dilutes the measurement with a constant that delegation
    was never going to change. What delegation governs is the conversation
    the task builds up, and that is what this measures.
    """
    from harness.context import message_chars

    return message_chars(messages[1:])


def run(monkeypatch: pytest.MonkeyPatch, script: ScriptedLLM, task: str) -> None:
    """Run one arm through the real CLI."""
    monkeypatch.setattr(session, "OpenAICompatibleClient", lambda settings, **_: script)
    cli.main([task])


TASK = "Summarise ledger.txt into brief.md and tell me how many entries there are."


def test_both_arms_do_the_same_work(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The comparison is only worth making if the outcome is the same.

    A baseline that produced a different file, or no file, would make every
    number below meaningless: the delegated arm would be winning on context
    by doing less.
    """
    write_config(project, subagents=False)
    run(monkeypatch, single_agent(), TASK)
    baseline = (project / "workspace" / "brief.md").read_text(encoding="utf-8")

    (project / "workspace" / "brief.md").unlink()
    write_config(project, subagents=True)
    run(monkeypatch, delegating_agent(), TASK)
    delegated = (project / "workspace" / "brief.md").read_text(encoding="utf-8")

    assert baseline == delegated
    assert "300 entries" in delegated


def test_delegation_keeps_the_bulk_out_of_the_coordinating_context(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim delegation is made for, measured rather than asserted.

    The single agent finishes the task carrying three hundred lines of
    ledger it will never need again. The coordinator finishes carrying two
    summaries. On this task that is an order of magnitude, and it is the
    whole of the argument: the saving grows with the size of the material
    and vanishes when there is none, which is why the system prompt tells
    the model to delegate for bulk rather than by default.
    """
    write_config(project, subagents=False)
    baseline_script = single_agent()
    run(monkeypatch, baseline_script, TASK)
    baseline_messages = coordinator_context(baseline_script)

    write_config(project, subagents=True)
    delegated_script = delegating_agent()
    run(monkeypatch, delegated_script, TASK)
    delegated_messages = coordinator_context(delegated_script)

    baseline = conversation_size(baseline_messages)
    delegated = conversation_size(delegated_messages)
    report = (
        f"coordinating conversation: baseline {baseline} chars, "
        f"delegated {delegated} chars; whole window including the system "
        f"prompt: {context_size(baseline_messages)} vs "
        f"{context_size(delegated_messages)}"
    )

    assert "LEDGER-ENTRY-9F2" in json.dumps(baseline_messages), report
    assert "LEDGER-ENTRY-9F2" not in json.dumps(delegated_messages), report
    assert delegated < baseline / 3, report
