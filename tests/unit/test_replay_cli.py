"""The replay command line, which is how a trace is read without the agent.

``python -m harness.observability.replay`` exists so a person can open a
traces directory copied off a container that is no longer running. Nothing
in it imports the loop, a hook, or ``prometheus_client`` — a trace file is
the whole input — and these tests are what keep that true, because the
moment it grows an import of the harness it stops working for the case it
was built for (ADR-087).
"""

import json
from pathlib import Path
from typing import Any

import pytest
from harness.observability.replay import main

_RUN = "a" * 32


def _record(**fields: Any) -> dict[str, Any]:
    """One v2 trace record with the keys a reader needs."""
    base: dict[str, Any] = {
        "ts": "2026-09-16T10:00:00.000Z",
        "run_id": _RUN,
        "parent_run_id": None,
        "role": "main",
        "event": "run_end",
        "tool": None,
        "permission": None,
        "ok": True,
        "latency_ms": 1200,
        "tokens_prompt": 120,
        "tokens_completion": 30,
        "exit_reason": "success",
        "schema_version": 2,
        "span": "agent.run",
        "trace_id": _RUN,
        "span_id": "b" * 16,
        "parent_span_id": None,
        "kind": "internal",
        "status": "ok",
        "iteration": 1,
        "error_type": None,
        "error_layer": None,
        "start_unix_ns": 1_789_000_000_000_000_000,
        "duration_ms": 1200,
        "duration_ns": 1_200_000_000,
        "attributes": {},
    }
    base.update(fields)
    return base


@pytest.fixture
def traces(tmp_path: Path) -> Path:
    """A traces directory holding one good run and one failed one."""
    directory = tmp_path / "traces"
    directory.mkdir()
    (directory / f"{_RUN}.jsonl").write_text(
        json.dumps(_record()) + "\n", encoding="utf-8"
    )
    failed = "c" * 32
    (directory / f"{failed}.jsonl").write_text(
        json.dumps(
            _record(
                run_id=failed,
                trace_id=failed,
                span_id="d" * 16,
                ok=False,
                status="error",
                exit_reason="error",
                error_type="mcp.connection",
                error_layer="mcp",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return directory


def test_no_arguments_lists_every_run(
    traces: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default is the listing, because "which run?" comes first."""
    assert main(["--dir", str(traces)]) == 0

    out = capsys.readouterr().out
    assert out.count("aaaaaaaa") == 1
    assert out.count("cccccccc") == 1


def test_a_run_id_renders_that_run(
    traces: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A prefix is enough, as ``git`` does it."""
    assert main(["--dir", str(traces), "aaaa"]) == 0
    assert "run aaaaaaaa" in capsys.readouterr().out


def test_failures_only_narrows_the_listing(
    traces: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--failures`` is the flag for "show me what went wrong".

    A directory of a hundred successful runs and two bad ones is exactly
    when a person needs this, and exactly when scrolling does not work.
    """
    assert main(["--dir", str(traces), "--failures"]) == 0

    out = capsys.readouterr().out
    assert "cccccccc" in out
    assert "aaaaaaaa" not in out


def test_json_output_is_machine_readable(
    traces: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """So the signals can be fed to something other than a person."""
    assert main(["--dir", str(traces), "aaaa", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_reason"] == "success"
    assert payload["prompt_tokens"] == 0  # no llm spans in this fixture


def test_a_missing_run_is_an_error_status_and_a_readable_line(
    traces: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A non-zero exit so a script can tell, and a sentence so a person can."""
    assert main(["--dir", str(traces), "zzzz"]) == 1
    assert "no run id starts with" in capsys.readouterr().err
