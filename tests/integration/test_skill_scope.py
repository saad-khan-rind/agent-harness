"""Integration: a skill's ``allowed-tools`` refuses a call, through the real CLI.

`tests/unit/test_skill_policy.py` proves the wrapper decides correctly, and
`tests/e2e/test_agent_skill_scope.py` proves a real model cannot get past it.
Neither of those can reliably show the gate *firing*, and the reason is the
feature working: `skill_read` tells the model "you may use only these tools",
so a cooperative model reads that and never tries. The e2e run then passes
without the policy having been asked to refuse anything.

This is the tier where that can be forced. The model is scripted (ADR-014),
so it calls a tool the skill never declared whether or not a real one would,
and the assertion is on the refusal the harness produced and on the workspace
afterwards. Everything else is production wiring: real `cli.main()`, real
registry, real policy, real skill loader (ADR-074, ADR-082).
"""

import sys
from pathlib import Path

import pytest
from harness import cli, session
from tests.fakes import ScriptedLLM, call, text

# The toy MCP server, addressed absolutely: the tests run from a temporary
# project directory, so a relative path would not resolve.
_TOY_SERVER = Path(__file__).resolve().parents[1] / "mcp_toy_server.py"

_CONFIG = """
llm_base_url: https://example.invalid
llm_model: test-model
temperature: 0.0
max_iterations: 20
workspace_path: workspace
memory_enabled: false
mcp_enabled: false
skills_enabled: true
skills_path: skills
skills_enforce_allowed_tools: {enforced}
max_tool_output_chars: 4000
max_search_results: 50
"""

# The same project with the toy MCP server attached, so the registry holds
# a tool whose name carries a server prefix (`toy__add`, ADR-042).
_MCP_CONFIG = (
    _CONFIG.replace("mcp_enabled: false", "mcp_enabled: true")
    + f"""
mcp_connect_timeout: 30.0
mcp_servers:
  - name: toy
    command: {sys.executable}
    args: ["{_TOY_SERVER}"]
    transport: stdio
    timeout: 20.0
"""
)

# Declares a read tool and nothing else, so `write_file` is outside it and
# `read_file` inside — one skill covers both halves of the assertion.
_SKILL = """---
name: reader
description: Reads things and reports on them.
allowed-tools:
  - read_file
---
Read what you are asked about and report it.
"""

# Declares the toy server's tool by its *bare* name, exactly as the shipped
# `web-research` skill declares `web_search` — the author writes the name
# the server documents, not the one the adapter builds from config.yaml.
_MCP_SKILL = """---
name: adder
description: Adds numbers with the toy server.
allowed-tools:
  - add
---
Use the add tool. It appears in your tool list with a server prefix.
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A runnable project with one skill and one file worth reading."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.md").write_text("read me\n", encoding="utf-8")
    skill = tmp_path / "skills" / "reader"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(_SKILL, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    return workspace


def write_config(project_root: Path, *, enforced: bool, mcp: bool = False) -> None:
    """Write ``config.yaml`` with enforcement on or off, optionally with MCP."""
    template = _MCP_CONFIG if mcp else _CONFIG
    (project_root / "config.yaml").write_text(
        template.format(enforced=str(enforced).lower()), encoding="utf-8"
    )


def write_mcp_skill(project_root: Path) -> None:
    """Add the skill that declares the toy server's tool by its bare name."""
    skill = project_root / "skills" / "adder"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(_MCP_SKILL, encoding="utf-8")


def feed(monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
    """Script the REPL's stdin, raising EOF when the lines run out."""
    pending = list(lines)

    def _input(_: str = "") -> str:
        if not pending:
            raise EOFError
        return pending.pop(0)

    monkeypatch.setattr("builtins.input", _input)


def run(monkeypatch: pytest.MonkeyPatch, script: ScriptedLLM, *argv: str) -> int:
    """Run ``cli.main(argv)`` with only the model replaced."""
    monkeypatch.setattr(session, "OpenAICompatibleClient", lambda settings, **_: script)
    return cli.main(list(argv))


def observations(script: ScriptedLLM) -> list[str]:
    """Return the tool results the model was shown, oldest first."""
    messages, _ = script.calls[-1]
    return [str(m["content"]) for m in messages if m.get("role") == "tool"]


def read_then_write() -> list[object]:
    """A script that loads the skill and then calls a tool it did not declare."""
    return [
        call("skill_read", skill="reader"),
        call("write_file", path="findings.md", content="hello"),
        text("done"),
    ]


def test_a_tool_the_skill_did_not_declare_is_refused(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate fires, and the model is told why in a way it can act on.

    The refusal has to reach the model as an observation rather than as an
    exception — that is what lets it change approach instead of the run
    ending (ADR-048).
    """
    write_config(project.parent, enforced=True)
    script = ScriptedLLM(responses=read_then_write())

    assert run(monkeypatch, script, "read the notes and save them") == 0

    refusals = [line for line in observations(script) if "not permitted" in line]
    assert refusals, observations(script)
    assert "write_file" in refusals[0]


def test_the_refused_write_never_reaches_the_workspace(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decision is enforced, not merely reported.

    A gate that logged a denial and ran the tool anyway would pass the test
    above and fail this one, which is why both exist.
    """
    write_config(project.parent, enforced=True)
    script = ScriptedLLM(responses=read_then_write())

    run(monkeypatch, script, "read the notes and save them")

    assert not (project / "findings.md").exists()


def test_a_tool_the_skill_did_declare_still_runs(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Narrowing must not become blocking.

    Without this, a policy that refused everything would pass both tests
    above while making the skill useless.
    """
    write_config(project.parent, enforced=True)
    script = ScriptedLLM(
        responses=[
            call("skill_read", skill="reader"),
            call("read_file", path="notes.md"),
            text("done"),
        ]
    )

    run(monkeypatch, script, "read the notes")

    assert any("read me" in line for line in observations(script)), observations(script)


def test_reset_lifts_the_restriction_with_the_conversation(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/reset`` is the one way out, and it is a real way out.

    The body leaves the context and the restriction leaves with it; a user
    who wants an unskilled task after a skilled one has a command for that,
    and it says so in ``/help``.
    """
    write_config(project.parent, enforced=True)
    script = ScriptedLLM(
        responses=[
            call("skill_read", skill="reader"),
            text("loaded"),
            call("write_file", path="later.md", content="ok"),
            text("done"),
        ]
    )
    monkeypatch.setattr(session, "OpenAICompatibleClient", lambda settings, **_: script)
    feed(monkeypatch, ["read the skill", "/reset", "just save a file", "/exit"])

    cli.main([])

    assert not [line for line in observations(script) if "not permitted" in line]
    assert (project / "later.md").read_text(encoding="utf-8") == "ok"
