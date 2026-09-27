"""Integration: ``/metrics`` is really served, by the real CLI.

``tests/unit/test_span_metrics.py`` proves the spans are counted correctly. What
no unit test can show is the thing §8.4 actually asks for — that a running
harness *exposes* those numbers to a scraper. That is a property of the
wiring in ``cli.main()``: a recorder built from settings, an HTTP server on a
thread, a hook composed in the right place. So it is asserted here in the
shape every file in this directory uses — the real CLI, real config, a
scripted model (ADR-014) — with one addition: a real HTTP request over a
real socket, because "the exporter works" is not a claim about Python
objects.

Each test picks its own free port. A fixed one would make the suite fail
when a developer has a harness running, and would make two tests in the same
session fight over it — the exact failure ``stop_metrics`` exists to prevent
and therefore the exact failure this file must not depend on not happening.
"""

import socket
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from harness import cli, session
from tests.fakes import ScriptedLLM, call, text

_CONFIG = """
llm_base_url: https://example.invalid
llm_model: test-model
temperature: 0.0
max_iterations: 20
workspace_path: workspace
memory_enabled: false
skills_enabled: false
mcp_enabled: false
permission_mode: allow_all
metrics_enabled: {enabled}
metrics_port: {port}
"""


def free_port() -> int:
    """Return a port nothing is listening on right now.

    Bound and released rather than guessed: a hard-coded number is how a
    test suite starts failing on one person's laptop for reasons that have
    nothing to do with the code.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    return port


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A runnable project with one file worth reading."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.md").write_text("read me\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    return tmp_path


def write_config(project: Path, port: int, enabled: bool = True) -> None:
    """Write ``config.yaml`` for this run."""
    (project / "config.yaml").write_text(
        _CONFIG.format(enabled=str(enabled).lower(), port=port), encoding="utf-8"
    )


def run(monkeypatch: pytest.MonkeyPatch, script: ScriptedLLM, *argv: str) -> int:
    """Run ``cli.main(argv)`` with only the model replaced."""
    monkeypatch.setattr(session, "OpenAICompatibleClient", lambda settings, **_: script)
    return cli.main(list(argv))


def scrape(port: int) -> str:
    """Fetch the exposition text from a running exporter."""
    with urllib.request.urlopen(  # noqa: S310 - fixed localhost URL
        f"http://127.0.0.1:{port}/metrics", timeout=5
    ) as response:
        return str(response.read().decode("utf-8"))


def test_a_run_is_visible_to_a_scraper(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The numbers a session produced can be read over HTTP while it runs.

    Asserted against the live endpoint rather than the registry object,
    because everything between the two — the thread, the port, the
    exposition format — is exactly what a unit test cannot check and what
    a demo depends on.
    """
    port = free_port()
    write_config(project, port)
    script = ScriptedLLM(
        responses=[call("read_file", path="notes.md"), text("It says: read me")]
    )
    scraped: dict[str, str] = {}

    # Scraped from inside the session: `main()` closes the exporter on the
    # way out, which is correct behaviour and would make a scrape afterwards
    # a test of nothing.
    original_ask = cli.Repl.ask

    def ask_then_scrape(self: cli.Repl, task: str) -> None:
        original_ask(self, task)
        scraped["body"] = scrape(port)

    monkeypatch.setattr(cli.Repl, "ask", ask_then_scrape)

    assert run(monkeypatch, script, "summarise notes.md") == 0

    body = scraped["body"]
    assert 'harness_runs_total{role="main",status="success"} 1.0' in body
    assert 'harness_active_runs{role="main"} 0.0' in body
    assert 'tool="read_file"' in body
    assert 'harness_permission_decisions_total{outcome="allow"}' in body


def test_no_series_is_labelled_by_run(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cardinality, checked on the wire rather than on the objects.

    A ``run_id`` label would be invisible in a registry inspection that
    only looked at declared label names — it would arrive as a *value*.
    This reads the exposition the scraper reads.
    """
    port = free_port()
    write_config(project, port)
    script = ScriptedLLM(
        responses=[call("read_file", path="notes.md"), text("It says: read me")]
    )
    scraped: dict[str, str] = {}
    original_ask = cli.Repl.ask

    def ask_then_scrape(self: cli.Repl, task: str) -> None:
        original_ask(self, task)
        scraped["body"] = scrape(port)

    monkeypatch.setattr(cli.Repl, "ask", ask_then_scrape)
    run(monkeypatch, script, "summarise notes.md")

    assert "run_id" not in scraped["body"]


def test_a_taken_port_degrades_instead_of_failing_the_run(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent must not refuse to answer because a metrics port is busy.

    This is the same rule long-term memory follows for an absent Qdrant
    (ADR-069), and observability has strictly less claim to be
    load-bearing than memory does.
    """
    port = free_port()
    write_config(project, port)
    script = ScriptedLLM(responses=[text("done")])

    with socket.socket() as squatter:
        squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        squatter.bind(("0.0.0.0", port))  # noqa: S104 - matches the exporter's bind
        squatter.listen(1)

        assert run(monkeypatch, script, "say hello") == 0
