"""Shared pytest fixtures.

Rule from ``AGENTS.md``: unit tests must not need a live InnKube key. The
``_no_settings_env`` fixture below enforces that mechanically rather than by
convention — if a test ever starts depending on a developer's real
environment, it fails on the next machine instead of passing by luck.

Observability knobs belong on this list too. Env wins over YAML, so a
``METRICS_PORT=9091`` left in the process (a sourced ``.env``, a GitLab
CI variable copied from ``.env.example``) makes a test that wrote a
free port into ``config.yaml`` scrape the wrong socket. That is how the
metrics and traces tests went red in CI while the test image stayed
green: the image has no ``.env``, the gate job did.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from harness.memory import HashingEmbedder, QdrantMemoryStore, SessionLog
from harness.skills import SkillLibrary, load_skills
from harness.tools import ToolRegistry, build_registry
from harness.toolset import build_agent_registry

# Every variable ``.env.example`` documents. Kept in one place so that
# adding a setting means updating this list too — and so a CI job that
# inherited the demo's env cannot override a test's ``config.yaml``.
_SETTINGS_ENV_VARS = (
    "LLM_PROVIDER",
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_TIMEOUT",
    # The legacy names Settings still accepts as aliases (ADR-121). A
    # developer's old .env must not leak into a test either.
    "INNKUBE_API_KEY",
    "INNKUBE_BASE_URL",
    "INNKUBE_MODEL",
    "INNKUBE_TIMEOUT",
    "METRICS_ENABLED",
    "METRICS_PORT",
    "TRACES_ENABLED",
    "TRACES_DIRNAME",
    # Added with the span rewrite. `TELEMETRY_REDACTION` matters most of the
    # three: a developer with `values` in their own `.env` would otherwise
    # run the redaction tests under a *different* policy from the one they
    # assert, and they would pass — which is the exact failure this list
    # exists to prevent (ADR-094, ADR-098).
    "OTLP_ENABLED",
    "OTLP_ENDPOINT",
    "OTLP_UI_URL",
    "TELEMETRY_REDACTION",
    "BUDGET_ENABLED",
    "BUDGET_DAILY_TOKENS",
    "BUDGET_PROMPT_TOKENS",
    "NOTES_ENABLED",
    "NOTES_MAX_CHARS",
)


@pytest.fixture(autouse=True)
def _no_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip documented process env from every test.

    ``autouse`` so no test can forget it. Tests that *want* a key set it
    themselves with ``monkeypatch.setenv``, which runs after this fixture.
    """
    for name in _SETTINGS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # The delegation planner asks the model an extra question at the first
    # read of a turn. A scripted model answers from a fixed list, so every
    # script written before the planner existed would have one reply spent
    # on that question. Off here, through the environment because it beats
    # every test's own config.yaml; the planner's tests switch it back on,
    # and test_config pins that the shipped default is on (ADR-117).
    monkeypatch.setenv("SUBAGENT_AUTO_PLAN", "false")


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Path]:
    """Return an empty sandbox directory for file-tool tests.

    Built on ``tmp_path`` so each test gets a fresh, isolated tree that
    pytest cleans up — file tools must never be pointed at the repository
    itself.
    """
    root = tmp_path / "workspace"
    root.mkdir()
    yield root


@pytest.fixture
def memory_root(tmp_path: Path) -> Path:
    """Return an empty long-term memory directory.

    A **sibling** of the ``workspace`` fixture, never a child of it. That is
    the production arrangement the memory security argument rested on, and a
    fixture that nested them would let a test pass while the shipped
    configuration was unsafe (ADR-035). The directory now holds only the run
    log — the facts live in Qdrant (ADR-065, ADR-068) — but the sibling
    arrangement is still what ``load_settings`` enforces.
    """
    root = tmp_path / "memory"
    root.mkdir()
    return root


@pytest.fixture
def store(memory_root: Path) -> Iterator[QdrantMemoryStore]:
    """Return a vector store backed by an in-process Qdrant.

    ``QdrantClient(":memory:")`` is the real query engine — the same
    filtering, the same cosine, the same payload handling — running inside
    the test process instead of behind HTTP. That is what lets the memory
    suite exercise the shipped store rather than a mock of it, in a pipeline
    that has no Docker and no network (ADR-069).

    The embedder is the local lexical one for the same reason. Retrieval
    quality differs from production; retrieval *mechanics* do not, and the
    mechanics are what these tests are about.
    """
    from qdrant_client import QdrantClient

    built = QdrantMemoryStore(
        embedder=HashingEmbedder(),
        session_log=SessionLog(memory_root),
        client=QdrantClient(":memory:"),
    )
    yield built
    built.close()


@pytest.fixture
def skills_root(tmp_path: Path) -> Path:
    """Return a skills directory holding one minimal, valid skill.

    One real skill on disk rather than a mock, because the loader's job is
    parsing files, and the tool contract tests need a library that is not
    empty in order to see ``skill_read`` at all.
    """
    root = tmp_path / "skills"
    (root / "haiku").mkdir(parents=True)
    (root / "haiku" / "SKILL.md").write_text(
        "---\nname: haiku\ndescription: Write a haiku in 5-7-5 syllables.\n---\n"
        "Count the syllables per line: five, seven, five.\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def skills(skills_root: Path) -> SkillLibrary:
    """Return the library discovered under ``skills_root``."""
    return load_skills(skills_root)


@pytest.fixture
def registry(workspace: Path) -> ToolRegistry:
    """Return a registry with the built-in file tools bound to ``workspace``.

    Scoped to the file tools on purpose. The tests that use this fixture are
    about the workspace tool set — its risk split, its exact membership, its
    ordering — and folding memory and skills in would make those assertions
    about three subsystems at once.
    """
    return build_registry(workspace)


@pytest.fixture
def agent_registry(
    workspace: Path, store: QdrantMemoryStore, skills: SkillLibrary
) -> ToolRegistry:
    """Return the agent's complete tool set, bound to the fixture roots.

    Uses ``build_agent_registry``, the same assembler the CLI uses, so what
    is validated is the tool set that actually ships rather than a
    hand-assembled approximation. ``test_tool_contract.py`` overrides
    ``registry`` with this, which is what makes ``AGENTS.md``'s promise true:
    a new tool needs nothing added to the contract tests, because they
    iterate whatever the production assembler produced.
    """
    return build_agent_registry(
        workspace,
        memory_store=store,
        skill_library=skills,
    )


# --- explaining an empty run ----------------------------------------------
#
# The default `pytest` deselects the end-to-end tier (`-m "not e2e"` in
# pyproject.toml, ADR-074). That is right for the gate and confusing for a
# person: `pytest tests/e2e` answers "27 deselected in 0.03s" and says
# nothing about why or what to type instead.
#
# So when a run selects *nothing* and the reason was the e2e filter, the two
# hooks below say so. Only in that case — printing it on every full run would
# be noise on a line nobody needs to read.

_DESELECTED_E2E: list[str] = []


def pytest_deselected(items: list[pytest.Item]) -> None:
    """Record which deselected tests came from the end-to-end tier.

    Args:
        items: The tests pytest removed because of a ``-m`` filter.
    """
    _DESELECTED_E2E.extend(
        item.nodeid for item in items if item.nodeid.startswith("tests/e2e/")
    )


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """Explain an empty run caused by the end-to-end marker filter.

    Args:
        terminalreporter: pytest's reporter, used to see whether anything
            actually ran and to write the note.
    """
    if not _DESELECTED_E2E:
        return
    # Only real outcomes count as "something ran". `stats` also carries a
    # `deselected` key, so summing every bucket would find 27 of them and
    # conclude the run was busy — which is precisely the run this exists to
    # explain.
    outcomes = ("passed", "failed", "error", "skipped", "xfailed", "xpassed")
    if any(terminalreporter.stats.get(name) for name in outcomes):
        return

    terminalreporter.write_sep("=", "nothing ran", yellow=True)
    terminalreporter.write_line(
        f"All {len(_DESELECTED_E2E)} end-to-end tests were deselected by "
        f'`-m "not e2e"` in pyproject.toml.'
    )
    terminalreporter.write_line(
        "They are opt-in: they run the shipped image against the real model "
        "and need Docker plus .env.test (ADR-074, ADR-075)."
    )
    terminalreporter.write_line("")
    terminalreporter.write_line("  ./scripts/e2e.sh            # from the host")
    terminalreporter.write_line(
        "  pytest tests/e2e -m e2e --no-cov    # the same, by hand"
    )
    terminalreporter.write_line("")
    terminalreporter.write_line(
        "Not from inside agent-harness-tests: that image has no Docker "
        "socket and no token, so the tier would only skip itself."
    )
