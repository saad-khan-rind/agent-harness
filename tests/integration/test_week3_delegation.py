"""Integration: delegation through the real CLI, with the evidence it leaves.

The unit suite proves the factory is correct in isolation. What no unit test
can show is the thing §8.4 asks for — that a *running harness* delegates,
enforces, and records. Those are properties of the wiring in ``cli.main()``:
a factory built from settings, a ``delegate`` tool registered on the parent
registry only, one trace hook shared with every child, one recorder counting
both.

So this file drives the real ``cli.main()`` with only the model replaced
(ADR-014) and reads the artifacts a real run leaves behind: the workspace,
the JSONL trace on the memory volume, the delegation records beside it, and
the Prometheus exposition.

The scenario is the one the brief asks for — a task that *benefits* from
delegation rather than one that merely demonstrates it. The user asks for a
brief on a topic that has to be looked up in the workspace and written to a
file. The coordinating agent delegates the reading to a researcher, hands the
researcher's finding to a writer, has a critic check the result, and answers
from what came back. What makes it a real benefit is visible in the
assertions: the bulky source material never enters the coordinator's context,
and the final answer is built from the children's structured results.
"""

import json
import socket
from pathlib import Path
from typing import Any

import pytest
from harness import cli, session
from tests.fakes import RoutedLLM, ScriptedLLM, call, text

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
permission_mode: {permissions}
metrics_enabled: {metrics}
metrics_port: {port}
traces_enabled: true
traces_dirname: traces
subagents_enabled: {subagents}
subagent_max_iterations: 6
subagent_max_tool_calls: 8
subagent_timeout_seconds: 60
subagent_max_result_chars: 2000
subagent_max_concurrency: {concurrency}
"""

# The bulky material the coordinator must never see. One distinctive token
# repeated enough that its absence from the parent's context is a real
# assertion rather than a coincidence.
BULK = "LEDGER-ENTRY-9F2\n" * 300

RESEARCH_JSON = json.dumps(
    {
        "answer": "The ledger holds 300 entries, all of type 9F2.",
        "evidence": [
            {
                "claim": "Every line of ledger.txt is a 9F2 entry",
                "source": "ledger.txt",
                "kind": "fact",
            }
        ],
        "uncertainties": ["Whether 9F2 is the only type used historically"],
        "confidence": "high",
    }
)

WRITE_JSON = json.dumps(
    {
        "summary": "Wrote the ledger brief.",
        "files_written": ["brief.md"],
        "verified": True,
        "notes": "",
    }
)

REVIEW_JSON = json.dumps(
    {
        "verdict": "approve",
        "findings": [],
        "strengths": ["States the count and the source"],
        "checked": ["brief.md"],
    }
)


def fenced(payload: str, prose: str) -> str:
    """Return a child answer in the shape the output contract asks for."""
    return f"{prose}\n```json\n{payload}\n```"


def free_port() -> int:
    """Return a port nothing is listening on right now."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A runnable project whose workspace holds material worth delegating."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "ledger.txt").write_text(BULK, encoding="utf-8")
    (tmp_path / "memory").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    return tmp_path


def write_config(
    project: Path,
    *,
    port: int = 0,
    metrics: bool = False,
    permissions: str = "allow_all",
    subagents: bool = True,
    concurrency: int = 1,
) -> None:
    """Write ``config.yaml`` for this run.

    ``concurrency`` defaults to 1 so the existing scenarios keep the strictly
    sequential execution their single ``ScriptedLLM`` script depends on.
    """
    (project / "config.yaml").write_text(
        _CONFIG.format(
            port=port or free_port(),
            metrics=str(metrics).lower(),
            permissions=permissions,
            subagents=str(subagents).lower(),
            concurrency=concurrency,
        ),
        encoding="utf-8",
    )


def run(monkeypatch: pytest.MonkeyPatch, script: ScriptedLLM, *argv: str) -> int:
    """Run ``cli.main(argv)`` with only the model replaced."""
    monkeypatch.setattr(session, "OpenAICompatibleClient", lambda settings, **_: script)
    return cli.main(list(argv))


def research_then_write_then_review() -> ScriptedLLM:
    """Script one coordinator, three children, in the order the loop asks.

    The model client is shared, so the responses are consumed in execution
    order: the parent's delegation, then the child it started, then back to
    the parent. Writing the script this way is itself a check on the
    design — if a child were not an ordinary nested run, this order would
    not hold.
    """
    return ScriptedLLM(
        [
            # Parent: plan and delegate the reading.
            call(
                "delegate",
                call_id="d1",
                role="researcher",
                task=(
                    "Summarise what is in ledger.txt: how many "
                    "entries and of what type."
                ),
                context="The workspace contains ledger.txt.",
                success_criteria="A count and a type.",
            ),
            # Researcher child: read the bulky file, then report.
            call("read_file", call_id="r1", path="ledger.txt"),
            text(fenced(RESEARCH_JSON, "The ledger is uniform.")),
            # Parent: hand the finding to a writer. Note the context is the
            # researcher's *finding*, not the file.
            call(
                "delegate",
                call_id="d2",
                role="writer",
                task="Write brief.md with the ledger summary.",
                context="The ledger holds 300 entries, all of type 9F2.",
            ),
            # Writer child: write the file, then report.
            call(
                "write_file",
                call_id="w1",
                path="brief.md",
                content="# Ledger brief\n\n300 entries, all of type 9F2.\n",
            ),
            text(fenced(WRITE_JSON, "Written.")),
            # Parent: have it checked before answering.
            call(
                "delegate",
                call_id="d3",
                role="critic",
                task=(
                    "Review brief.md against the claim that there are 300 9F2 entries."
                ),
                context="brief.md was just written from a researcher's finding.",
            ),
            # Critic child: read the brief, then report.
            call("read_file", call_id="c1", path="brief.md"),
            text(fenced(REVIEW_JSON, "It holds up.")),
            # Parent: answer from what came back.
            text(
                "I had the ledger read, wrote brief.md, and had it reviewed: "
                "300 entries, all type 9F2, approved by the critic."
            ),
        ]
    )


def trace_events(project: Path) -> list[dict[str, Any]]:
    """Return every event from the one trace file this run wrote."""
    (path,) = (project / "memory" / "traces").glob("*.jsonl")
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# -- the end-to-end path ----------------------------------------------------


def test_a_delegated_task_reaches_the_workspace(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole chain, judged by the thing the user asked for.

    Not "the children executed" — the file exists, written by a sub-agent
    that the coordinating agent never told how to write it.
    """
    write_config(project)

    assert run(monkeypatch, research_then_write_then_review(), "brief the ledger") == 0

    brief = project / "workspace" / "brief.md"
    assert brief.is_file()
    assert "300 entries" in brief.read_text(encoding="utf-8")


def test_the_coordinator_is_offered_delegation_and_the_children_are_not(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recursion guard, checked on the tool schemas actually sent.

    Every request the harness makes carries the caller's tool list, so the
    set of requests that advertise ``delegate`` is exactly the set of
    parent turns. A child that could delegate would show up here.
    """
    script = research_then_write_then_review()
    write_config(project)

    run(monkeypatch, script, "brief the ledger")

    offered = [
        {schema["function"]["name"] for schema in tools} for _, tools in script.calls
    ]
    assert any("delegate" in names for names in offered)
    for names in offered:
        if "delegate" in names:
            continue
        # A child's list: file tools only, and never the delegation tool.
        assert names <= {
            "list_files",
            "read_file",
            "search_text",
            "find_files",
            "create_folder",
            "write_file",
            "append_file",
            "edit_file",
        }


# -- observability ----------------------------------------------------------


def test_delegation_can_be_switched_off(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``subagents_enabled: false`` removes the tool, not just its effect.

    A disabled feature that leaves a tool in the list which refuses every
    call is worse than one that is absent: the model tries it, is refused,
    and tries again with a different role.
    """
    write_config(project, subagents=False)
    script = ScriptedLLM([text("nothing to delegate to")])

    run(monkeypatch, script, "brief the ledger")

    (_, tools) = script.calls[0]
    assert "delegate" not in {schema["function"]["name"] for schema in tools}


def test_a_read_only_session_refuses_to_delegate_a_write(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Consistency, which is what §8.4 asks for rather than special cases.

    ``delegate`` is a mutating tool dispatched through the same gate as
    every other one, so a ``deny_mutations`` session refuses it at the
    parent's own permission check — before a child exists. The model is
    told plainly, and the run continues.
    """
    write_config(project, permissions="deny_mutations")
    script = ScriptedLLM(
        [
            call("delegate", role="writer", task="write brief.md"),
            text("I was not allowed to delegate that."),
        ]
    )

    run(monkeypatch, script, "brief the ledger")

    assert not (project / "workspace" / "brief.md").exists()
    decisions = [
        event["permission"]
        for event in trace_events(project)
        if event["event"] == "permission"
    ]
    assert decisions == ["deny"]


# -- running several children at once ---------------------------------------


def test_concurrent_children_are_recorded_under_the_parent_that_ran_them(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The §8.4 requirement that fan-out would otherwise quietly break.

    "Record properly sub-agent executions as part of the corresponding
    parent run in the observability data" is a statement about the *tree*,
    and the tree used to be expressed by one shared stack of open spans. A
    stack says "innermost" and means "mine" only while runs nest in time;
    once three children run at once, "innermost" is whichever sibling
    started last, and every span lands under the wrong parent.

    So this asserts the parentage rather than the presence: three concurrent
    children, one trace, every child's run span hanging off the ``tool.call``
    that delegated it, and every span that opened also closed.
    """
    write_config(project, concurrency=3)
    routed = RoutedLLM(
        {
            # The coordinator: one call, three independent lookups.
            "brief the ledger": [
                call(
                    "delegate",
                    call_id="d1",
                    tasks=[
                        {"role": "researcher", "task": "count the alpha entries"},
                        {"role": "researcher", "task": "count the beta entries"},
                        {"role": "researcher", "task": "count the gamma entries"},
                    ],
                ),
                text("Three researchers reported back."),
            ],
            "count the alpha entries": [text(fenced(RESEARCH_JSON, "Alpha done."))],
            "count the beta entries": [text(fenced(RESEARCH_JSON, "Beta done."))],
            "count the gamma entries": [text(fenced(RESEARCH_JSON, "Gamma done."))],
        }
    )

    assert run(monkeypatch, routed, "brief the ledger") == 0

    events = trace_events(project)

    children = [event for event in events if event["span"] == "subagent.run"]
    assert len(children) == 3, "all three children should be recorded"

    delegations = [
        event["span_id"]
        for event in events
        if event["span"] == "tool.call" and event.get("tool") == "delegate"
    ]
    assert len(delegations) == 1, "one delegate call ran all three"

    # Every child hangs off that one delegate span, not off a sibling and
    # not off nothing. This is the assertion that fails without a per-thread
    # span stack: with one shared stack the children nest inside whichever
    # sibling happened to start last.
    assert {child["parent_span_id"] for child in children} == set(delegations)

    # Three distinct runs, with three distinct ids, all of them successful.
    assert len({child["run_id"] for child in children}) == 3
    assert all(child["role"] == "researcher" for child in children)
    assert all(child["exit_reason"] == "success" for child in children)

    # Every span that a child opened also closed: a span left open is a span
    # no exporter ever sees, and the adopted parent must not be closed twice.
    assert len([event for event in events if event["span"] == "tool.call"]) == 1

    # One trace file, so the children are in the parent's trace rather than
    # in files of their own.
    assert len(list((project / "memory" / "traces").glob("*.jsonl"))) == 1
    assert len({event["trace_id"] for event in events}) == 1


def test_a_batched_delegation_writes_one_record_per_child(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The data plane keeps per-child accounting under fan-out.

    Three children, three records, each naming its own run id — which is
    what makes a line in the parent's context traceable to a full account of
    the run that produced it.
    """
    write_config(project, concurrency=3)
    routed = RoutedLLM(
        {
            "brief the ledger": [
                call(
                    "delegate",
                    call_id="d1",
                    tasks=[
                        {"role": "researcher", "task": f"lookup {n}"} for n in range(3)
                    ],
                ),
                text("Done."),
            ],
            **{
                f"lookup {n}": [text(fenced(RESEARCH_JSON, "Found it."))]
                for n in range(3)
            },
        }
    )

    run(monkeypatch, routed, "brief the ledger")

    records = sorted((project / "memory" / "traces" / "delegations").glob("*.json"))
    assert len(records) == 3
    for path in records:
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["result"]["child_run_id"] == path.stem
        assert record["result"]["status"] == "completed"


def test_the_planner_fans_out_a_request_nobody_asked_to_delegate(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delegation decided at run time, through the real CLI (ADR-117).

    The request never mentions sub-agents. The coordinator starts reading as
    it would on its own; the planner is asked whether the work splits, says
    yes, and the loop receives a batched ``delegate`` in place of the read.
    From there it is ordinary delegation — the permission gate, the factory,
    the concurrent children, the trace — and the coordinator answers from
    the children's results.
    """
    write_config(project, concurrency=3)
    monkeypatch.setenv("SUBAGENT_AUTO_PLAN", "true")
    plan = json.dumps(
        {
            "split": True,
            "reason": "Two independent counts.",
            "tasks": [
                {"task": "count the alpha entries", "context": "ledger.txt"},
                {"task": "count the beta entries", "context": "ledger.txt"},
            ],
        }
    )
    routed = RoutedLLM(
        {
            # Checked first: the planner's question also quotes the request.
            "You plan work": [text(plan)],
            "tally the ledger": [
                call("read_file", call_id="r1", path="ledger.txt"),
                text("Alpha and beta were both counted."),
            ],
            "count the alpha entries": [text(fenced(RESEARCH_JSON, "Alpha done."))],
            "count the beta entries": [text(fenced(RESEARCH_JSON, "Beta done."))],
        }
    )

    assert run(monkeypatch, routed, "tally the ledger") == 0

    events = trace_events(project)
    delegations = [
        event
        for event in events
        if event["span"] == "tool.call" and event.get("tool") == "delegate"
    ]
    children = [event for event in events if event["span"] == "subagent.run"]
    assert len(delegations) == 1
    assert len(children) == 2
    assert {child["parent_span_id"] for child in children} == {
        delegations[0]["span_id"]
    }
    assert all(child["exit_reason"] == "success" for child in children)
    # The coordinator's own read was replaced, never run: the bulk stayed out
    # of its context and no read_file span belongs to the main run.
    assert not [
        event
        for event in events
        if event["span"] == "tool.call"
        and event.get("tool") == "read_file"
        and event["role"] == "main"
    ]
    # The planner's question is its own model call, visible on the trace.
    assert "You plan work" in routed.seen
