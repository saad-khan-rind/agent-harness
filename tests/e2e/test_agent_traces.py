"""**Real model.** The shipped image leaves a readable trace of a real run.

``tests/unit/test_span_trace.py`` proves the hook writes the right lines
and ``tests/unit/test_cli.py`` proves ``cli.main()`` installs it. Neither
proves what a TA will do after the demo: open the memory volume and read
``traces/<run_id>.jsonl`` for a run a real model just did. Between those
tests and that moment sit the Dockerfile, the non-root user, the
``agent-harness-memory`` volume and a model nobody scripted — and every one of them
has broken something in this project before (ADR-076, ADR-083).

One-shot mode is fine here, unlike the metrics tier: the trace is a file on
a volume, so it outlives the process that wrote it. That is the point of
putting it there.

Nothing asserts on *how* the model did the task — which tool, how many
calls. A trace records whatever happened, so the assertions are about the
shape of the record: one file, every line parses, the run is in it once,
and the decisions the harness made are in it (ADR-078).

Since ADR-093 a line is a finished *span*, not an event. Two consequences
these tests are written around: the v1 keys are still the first twelve on
every line, in order, with the span-tree keys after them (ADR-094); and a
span is written when it *closes*, so the run's own line is the last one in
the file rather than the first, and there is no ``run_start`` line at all.
"""

import json
from typing import Any

import pytest
from harness.observability.exporters.jsonl import SCHEMA_VERSION
from tests.e2e.conftest import MEMORY, WORKSPACE, Stack

pytestmark = pytest.mark.e2e

TRACES = f"{MEMORY}/traces"

# The v1 keys, first on every line and in this order — the contract
# `test_span_trace.py` pins in-process, checked once more on the file the
# container produced. Kept as a literal rather than imported, so a change
# to the exporter's own tuple cannot silently move the goalposts.
V1_KEYS = (
    "ts",
    "run_id",
    "parent_run_id",
    "role",
    "event",
    "tool",
    "permission",
    "ok",
    "latency_ms",
    "tokens_prompt",
    "tokens_completion",
    "exit_reason",
)

# What v2 added. Presence is asserted, not position: these are what the
# reader nests by, and a file without them is a v1 file with a v2 label.
SPAN_KEYS = ("schema_version", "span", "trace_id", "span_id", "parent_span_id")


def only_trace(stack: Stack) -> list[dict[str, Any]]:
    """Return the parsed events of the single trace file on the memory volume.

    Exactly one, because the stack ran exactly one task: a second file would
    mean a run was split, and none would mean the hook never wrote.
    """
    files = [path for path in stack.listing(TRACES) if path.endswith(".jsonl")]
    assert len(files) == 1, (
        f"expected one trace file, found {files}; tree:\n{stack.tree()}"
    )
    lines = stack.read(files[0]).splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_a_real_run_leaves_one_readable_trace_on_the_memory_volume(
    stack: Stack,
) -> None:
    """The demo: do a task, open the file, read what happened.

    On the *memory* volume, not the workspace — a sibling root the file
    tools cannot reach, so the record of what the agent did is not something
    the agent could have rewritten (ADR-035, ADR-083).
    """
    run = stack.run("Create a file called notes.md containing the word ready.")
    run.assert_completed()

    events = only_trace(stack)

    # Every line: the v1 keys first and in order, then the span tree.
    for event in events:
        assert tuple(event)[: len(V1_KEYS)] == V1_KEYS, event
        assert event["schema_version"] == SCHEMA_VERSION, event
        for key in SPAN_KEYS:
            assert key in event, f"{key!r} missing from {event}"
    # Exactly one run, and it is the last line: spans are written when they
    # close and the run closes last. Everything else in the file is inside
    # it — same trace, a parent, never a second root.
    runs = [event for event in events if event["event"] == "run_end"]
    assert len(runs) == 1, runs
    run_span = events[-1]
    assert run_span["event"] == "run_end"
    assert run_span["span"] == "agent.run"
    assert run_span["parent_span_id"] is None
    assert run_span["ok"] is True, "the model answered but run_end says it did not"
    for event in events[:-1]:
        assert event["parent_span_id"] is not None, f"a second root span: {event}"
    assert {event["trace_id"] for event in events} == {run_span["run_id"]}
    # Identity is inherited, so `grep <run_id>` returns the whole run —
    # memory and permission spans included (ADR-095).
    assert {event["role"] for event in events} == {"main"}
    assert {event["parent_run_id"] for event in events} == {None}
    assert {event["run_id"] for event in events} == {run_span["run_id"]}
    # A real model made at least one request; the trace saw it and its cost.
    llm = [event for event in events if event["event"] == "llm"]
    assert llm, "no llm events — the model was called but nothing was recorded"
    assert all(event["latency_ms"] >= 0 for event in llm)
    # Tokens are whatever the endpoint reported; it may report none
    # (ADR-031), so the assertion is on shape, not on a number.
    assert isinstance(run_span["tokens_prompt"], int)
    assert run_span["tokens_prompt"] >= 0
    # Nothing escaped into the workspace.
    assert not any("traces" in path for path in stack.listing(WORKSPACE))


def test_a_tool_call_and_its_permission_decision_are_both_in_the_trace(
    stack: Stack,
) -> None:
    """Whatever route the model took, it had to call *something* to write a file.

    Each call is two lines: the policy's decision and the call itself. The
    pair is the thing a person needs when "the agent did nothing" turns out
    to be a permission setting rather than a bug — and a trace with the call
    but not the decision, or the reverse, would send them the wrong way.

    The pairing is structural: the ``permission`` span is a *child* of the
    ``tool`` span it decided, read from ``parent_span_id`` rather than
    inferred from which line came next. Adjacency was correct until a model
    requested two tools in one turn (ADR-093).
    """
    run = stack.run("Create a file called notes.md containing the word ready.")
    run.assert_completed()

    events = only_trace(stack)
    decisions = [event for event in events if event["event"] == "permission"]
    calls = {event["span_id"]: event for event in events if event["event"] == "tool"}

    assert calls, "the file was created but no tool event was traced"
    assert decisions, "a tool ran but no permission decision was traced"
    # Every tool line names its tool and carries a verdict; every decision
    # line names the tool it was about and the outcome as a value.
    for event in calls.values():
        assert event["tool"], event
        assert event["ok"] in (True, False), event
    for event in decisions:
        assert event["tool"], event
        assert event["permission"] in ("allow", "deny", "confirmed", "declined"), event
    # Every decision belongs to a tool call, and to one about the same tool.
    for decision in decisions:
        parent = calls.get(decision["parent_span_id"])
        assert parent is not None, (
            f"decision for {decision['tool']} is not nested in a tool span"
        )
        assert parent["tool"] == decision["tool"], (decision, parent)


def test_a_denied_write_is_traced_as_a_denial(stack: Stack) -> None:
    """``deny_mutations``, a model that genuinely wants to write, and the file.

    ``tests/e2e/test_agent_permissions.py`` proves the write did not happen
    and the agent said so. This proves the *record* says so too: a
    ``permission: "deny"`` line, and no tool line with ``ok: true`` for a
    mutating call. The deny is written as a value at decision time, not
    inferred later from an error string (ADR-058, ADR-081).
    """
    stack.write(f"{WORKSPACE}/notes/plan.md", "# Plan\noriginal\n")

    run = stack.run(
        "Add a line saying 'ship it' to notes/plan.md.",
        permissions="deny_mutations",
    )
    run.assert_completed()

    events = only_trace(stack)
    denials = [event for event in events if event["permission"] == "deny"]

    assert denials, (
        "deny_mutations refused nothing — either the model never tried to "
        f"write, or the refusal was not traced:\n{json.dumps(events, indent=1)}"
    )
    # A refused call is a failed call in the record, never a successful one.
    refused = {event["tool"] for event in denials}
    for event in events:
        if event["event"] == "tool" and event["tool"] in refused:
            assert event["ok"] is False, event
    # And the file is untouched, so the record and the disk agree.
    assert stack.read(f"{WORKSPACE}/notes/plan.md") == "# Plan\noriginal\n"
