"""Integration: a long run stops for a reason, or it does not stop.

The bug behind this file was reported as "ask it to make a hundred files and
somewhere in the middle it just stops". The guess at the time was an
iteration cap. It was not: with ``max_iterations: 0`` the loop has no fixed
ceiling, ``max_tool_failures`` counts *consecutive* failures of one tool and
resets on success, and ``max_repeated_calls`` needs an identical name,
arguments *and* observation — none of which a hundred different files
produce. The two things that really ended those runs were the token budget
and a conversation that grew without bound because the compactor only ever
looked at tool results, and on a bulk task the payloads are on what the model
*sent* (ADR-090).

So the property under test is a negative one, which is the awkward kind: a
hundred-call run finishes, and the only things that may stop it early are a
tool that genuinely keeps failing and a spent allowance. Scripted model
(ADR-014), real loop, real registry, real file tools, real workspace.
"""

import json
from pathlib import Path

import pytest
from harness import cli, session
from harness.llm.protocol import LLMResponse, ToolCall
from tests.fakes import ScriptedLLM, text

# Big enough that the old behaviour failed and small enough to stay a unit of
# seconds. The reported failure was at a hundred; the mechanism is the same.
FILE_COUNT = 100

# Each write carries a payload far above MAX_KEPT_ARGUMENT_CHARS, so the
# conversation is dominated by arguments — the shape the compactor used to be
# blind to.
PAYLOAD = "x" * 2_000

_CONFIG = """
llm_base_url: https://example.invalid
llm_model: test-model
temperature: 0.0
max_iterations: {max_iterations}
max_tool_failures: {max_tool_failures}
max_repeated_calls: 10
max_context_chars: {max_context_chars}
workspace_path: workspace
memory_path: memory
memory_enabled: false
skills_enabled: false
mcp_enabled: false
metrics_enabled: false
traces_enabled: false
notes_enabled: false
subagents_enabled: false
budget_enabled: {budget_enabled}
budget_daily_tokens: {limit}
budget_prompt_tokens: 0
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A runnable project directory with an empty workspace."""
    (tmp_path / "workspace").mkdir()
    (tmp_path / "memory").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    return tmp_path


def write_config(
    project: Path,
    *,
    max_iterations: int = 0,
    max_tool_failures: int = 5,
    max_context_chars: int = 24_000,
    budget_enabled: bool = False,
    limit: int = 1_000_000,
) -> None:
    """Write ``config.yaml`` for this run."""
    (project / "config.yaml").write_text(
        _CONFIG.format(
            max_iterations=max_iterations,
            max_tool_failures=max_tool_failures,
            max_context_chars=max_context_chars,
            budget_enabled=str(budget_enabled).lower(),
            limit=limit,
        ),
        encoding="utf-8",
    )


def bulk_write_script(
    count: int = FILE_COUNT,
    payload: str = PAYLOAD,
    prompt_tokens: int = 0,
) -> list[LLMResponse]:
    """Return ``count`` write_file calls followed by a final answer."""
    responses: list[LLMResponse] = [
        LLMResponse(
            content=None,
            tool_calls=[
                ToolCall(
                    id=f"w{index}",
                    name="write_file",
                    arguments={"path": f"f{index}.md", "content": payload},
                )
            ],
            prompt_tokens=prompt_tokens,
            completion_tokens=0,
        )
        for index in range(count)
    ]
    responses.append(text(f"wrote {count} files"))
    return responses


def run(monkeypatch: pytest.MonkeyPatch, script: ScriptedLLM, *argv: str) -> int:
    """Run ``cli.main(argv)`` with only the model replaced."""
    monkeypatch.setattr(session, "OpenAICompatibleClient", lambda settings, **_: script)
    return cli.main(list(argv))


def sent_chars(script: ScriptedLLM) -> int:
    """Return the size of the last conversation the model was sent."""
    messages, _ = script.calls[-1]
    return sum(len(json.dumps(message)) for message in messages)


# -- the run that should not stop ---------------------------------------------


def test_a_hundred_tool_calls_run_to_the_end(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The headline claim: nothing in the loop stops a long, healthy run.

    A hundred different files means a hundred different arguments and a
    hundred successful observations, so neither the failure guard nor the
    no-progress guard has anything to fire on — and with ``max_iterations:
    0`` there is no ceiling to hit either (ADR-047).
    """
    write_config(project)
    script = ScriptedLLM(bulk_write_script())

    assert run(monkeypatch, script, "make a hundred files") == 0

    out = capsys.readouterr().out
    assert f"Agent: wrote {FILE_COUNT} files" in out
    written = list((project / "workspace").glob("f*.md"))
    assert len(written) == FILE_COUNT
    assert len(script.calls) == FILE_COUNT + 1


# -- the reasons it may stop --------------------------------------------------


def test_a_tool_that_keeps_failing_still_stops_the_loop(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ "Does not stop" is not "never stops".

    A tool failing over and over is a real fault, and retiring it with an
    explanation the model can act on is the behaviour ADR-048 specifies.
    Removing that in the name of long runs would trade one silent failure
    for a louder one.
    """
    write_config(project, max_tool_failures=3)
    # Reading a file that does not exist fails every time, with a different
    # path each time so the no-progress guard is not what fires.
    script = ScriptedLLM(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id=f"r{index}",
                        name="read_file",
                        arguments={"path": f"missing{index}.md"},
                    )
                ],
                prompt_tokens=0,
                completion_tokens=0,
            )
            for index in range(6)
        ]
        + [text("giving up")]
    )

    run(monkeypatch, script, "read some files")

    out = capsys.readouterr().out
    assert "Agent: giving up" in out
    messages, _ = script.calls[-1]
    observations = [
        str(message["content"]) for message in messages if message.get("role") == "tool"
    ]
    assert any("Do not call it again" in line for line in observations), observations


def test_a_long_run_stops_when_the_allowance_runs_out(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other legitimate stop, and the one that was really ending those runs.

    A million-token window divided by a bulk task's per-call cost is a
    number of calls, and it is smaller than people expect. Worth a test
    because the honest fix for "it stopped at file 77" is to say which
    limit it was, not to remove the limit.
    """
    write_config(project, budget_enabled=True, limit=10_000)
    script = ScriptedLLM(bulk_write_script(prompt_tokens=1_000))

    run(monkeypatch, script, "make a hundred files")

    out = capsys.readouterr().out
    assert "Budget stop:" in out
    assert f"Agent: wrote {FILE_COUNT} files" not in out
    # It got as far as the allowance allowed and no further: ten calls at
    # 1,000 tokens, then the eleventh is refused before it is made.
    assert len(script.calls) == 10
    assert len(list((project / "workspace").glob("f*.md"))) == 10
