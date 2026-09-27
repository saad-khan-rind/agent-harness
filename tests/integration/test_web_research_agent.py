"""The web tools driven by a running agent, not called directly.

``test_mcp_web_search.py`` proves the two tools answer correctly over a real
MCP conversation. It stops at the registry. This file takes the same real
client and real server and hands them to a real ``AgentLoop``, so what is
under test is the thing the demo actually shows: search, then fetch, then
write the answer down — a chain where each call's arguments come out of the
previous call's observation.

The only fakes are the model (``ScriptedLLM``, ADR-014) and the far end of
the network — the backend, the HTTP transport and the DNS resolver are all
injected, as they are in the unit tests, because `.github/workflows/ci.yml` promises
neither network nor credentials.

``cli.main`` is deliberately not the entry point here: it reads
``config.yaml`` and launches servers as subprocesses, which is exactly what
makes injecting a scripted backend impossible. The loop, the registry, the
adapter, the client and the server are all real; only the process boundary
is collapsed.
"""

from pathlib import Path
from typing import Any

import pytest
from harness.context import RunContext
from harness.loop import AgentLoop
from harness.mcp.client import SdkMcpClient
from harness.tools.registry import ToolRegistry
from harness.toolset import build_agent_registry
from mcp_server_web_search.fetch import Response
from mcp_server_web_search.server import build_server
from tests.fakes import ScriptedLLM, call, text

PREFIX = "web__"

_PAGE = (
    "<html><head><title>Model Context Protocol</title></head><body>"
    "<nav>skip me</nav><script>tracker()</script>"
    "<p>MCP is an open protocol that connects models to tools over "
    "stdio or HTTP.</p></body></html>"
)


class _Backend:
    """A search backend with two distinguishable results."""

    def text(self, query: str, max_results: int) -> list[dict[str, Any]]:
        """Return scripted hits, narrowed by ``max_results`` like the real one."""
        return [
            {
                "title": "Model Context Protocol",
                "href": "https://example.com/mcp",
                "body": "An open protocol for connecting models to tools.",
            },
            {
                "title": "MCP transports",
                "href": "https://example.com/transports",
                "body": "stdio and Streamable HTTP.",
            },
        ][:max_results]


class _Http:
    """A transport that answers one page and refuses everything else."""

    def get(self, url: str) -> Response:
        """Return the scripted page, or a 404 for an unknown URL."""
        if url == "https://example.com/mcp":
            return Response(status_code=200, content_type="text/html", text=_PAGE)
        return Response(status_code=404, content_type="text/html", text="missing")


@pytest.fixture
def registry(tmp_path: Path) -> ToolRegistry:
    """A full agent registry: file tools plus the two remote web tools."""
    client = SdkMcpClient(
        server_name="web",
        target=build_server(
            backend=_Backend(),
            http_client=_Http(),
            resolve=lambda _host: ["93.184.216.34"],
        ),
        timeout=20.0,
    )
    client.connect()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    built = build_agent_registry(workspace, mcp_tools=client.discover_tools())
    yield built
    client.close()


def _context(task: str) -> RunContext:
    """Build a context the way the CLI does, ready for one turn."""
    context = RunContext(task="")
    context.ensure_system_prompt("SYSTEM")
    context.start_turn(task)
    return context


def _observations(context: RunContext) -> list[str]:
    """Return the observations the model was shown, oldest first."""
    return [str(m["content"]) for m in context.messages if m.get("role") == "tool"]


# --- web_search ------------------------------------------------------------


def test_the_agent_searches_and_reads_the_result(registry: ToolRegistry) -> None:
    """The ordinary case: a question, a search, an answer from the hits."""
    script = ScriptedLLM(
        [
            call(f"{PREFIX}web_search", call_id="1", query="what is MCP"),
            text("MCP is an open protocol for connecting models to tools."),
        ]
    )
    context = _context("what is MCP?")

    assert "open protocol" in AgentLoop(script, registry).run(context)
    observation = _observations(context)[-1]
    assert "https://example.com/mcp" in observation
    assert "Model Context Protocol" in observation


def test_a_bad_search_argument_is_recoverable_inside_one_run(
    registry: ToolRegistry,
) -> None:
    """An empty query is refused with a message the model can act on."""
    script = ScriptedLLM(
        [
            call(f"{PREFIX}web_search", call_id="1", query="   "),
            call(f"{PREFIX}web_search", call_id="2", query="mcp"),
            text("Recovered."),
        ]
    )
    context = _context("search for it")

    assert AgentLoop(script, registry).run(context) == "Recovered."

    refused, worked = _observations(context)
    assert refused.startswith("Error:")
    assert not worked.startswith("Error:")


# --- web_fetch -------------------------------------------------------------


def test_a_refused_url_is_refused_by_the_server_not_the_model(
    registry: ToolRegistry,
) -> None:
    """The SSRF guard runs on the server, so a compromised model cannot skip it.

    ``file://`` is not a scheme the tool accepts. The refusal is the
    server's, arrives as an ordinary observation, and the run continues
    (ADR-056).
    """
    script = ScriptedLLM(
        [
            call(f"{PREFIX}web_fetch", call_id="1", url="file:///etc/passwd"),
            call(f"{PREFIX}web_fetch", call_id="2", url="https://example.com/mcp"),
            text("I could not read the local file, so I read the page instead."),
        ]
    )
    context = _context("read /etc/passwd")

    AgentLoop(script, registry).run(context)

    refused, allowed = _observations(context)
    assert refused.startswith("Error:")
    assert "root:" not in refused
    assert not allowed.startswith("Error:")


# --- both, beside the built-ins --------------------------------------------


def test_research_ends_in_a_file_the_user_can_read(registry: ToolRegistry) -> None:
    """The whole scenario: search, fetch, then write the briefing note.

    Remote tools and file tools in one run, dispatched through one registry,
    with the loop unable to tell which is which (ADR-039).
    """
    script = ScriptedLLM(
        [
            call(f"{PREFIX}web_search", call_id="1", query="mcp"),
            call(f"{PREFIX}web_fetch", call_id="2", url="https://example.com/mcp"),
            call(
                "write_file",
                call_id="3",
                path="brief.md",
                content=(
                    "# MCP\n\nAn open protocol connecting models to tools.\n\n"
                    "Source: https://example.com/mcp\n"
                ),
            ),
            text("Written to brief.md with the source cited."),
        ]
    )
    context = _context("research MCP and write me a short brief with the source")

    AgentLoop(script, registry).run(context)

    workspace = registry.get("write_file").workspace_root
    brief = (workspace / "brief.md").read_text(encoding="utf-8")
    assert "https://example.com/mcp" in brief
    assert not _observations(context)[-1].startswith("Error:")
