"""Integration: the shipped MCP server, launched as a real subprocess.

Discovery, invocation and result handling, over the transport the harness
actually uses. Nothing here is faked: ``python -m mcp_server_time_plus`` is
started as a child process, the real
:class:`~harness.mcp.client.StdioMcpClient` completes the handshake, the real
adapter wraps what the server advertises, the real
:class:`~harness.tools.registry.ToolRegistry` registers it, and the
observation asserted on is the string the model would receive.

This is the counterpart to ``tests/integration/test_mcp_tools.py``, which drives the
whole CLI against the *toy* server. That one proves the harness can talk to
any server; this one proves the server we ship is one of them. Between the
two, both halves of the Week 2 MCP deliverable have an integration test that
runs a subprocess.

The agent-loop test for ``find_meeting_slot`` — a scripted model asking for a
meeting time and using the answer — belongs to the review ring rather than
here: the person who reviews this slice writes it, so the tool is described
from the outside by someone who did not build it.
"""

import json
import sys
from pathlib import Path

import pytest
from harness.mcp.client import StdioMcpClient
from harness.tools.registry import ToolRegistry

from mcp import StdioServerParameters

# The repository root, so the child process can import the package. Resolved
# from this file rather than from the working directory, which pytest changes
# in several other suites.
REPO_ROOT = Path(__file__).resolve().parents[2]

SERVER_LABEL = "time_plus"
PREFIX = f"{SERVER_LABEL}__"


@pytest.fixture(scope="module")
def client() -> StdioMcpClient:
    """Launch the real server once for this module and shut it down after."""
    connected = StdioMcpClient(
        server_name=SERVER_LABEL,
        # sys.executable, not "python": the interpreter running the tests is
        # the only one guaranteed to exist on a contributor's machine and
        # inside the test image.
        target=StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcp_server_time_plus", "--local-timezone", "Europe/Berlin"],
            cwd=str(REPO_ROOT),
        ),
        timeout=20.0,
    )
    connected.connect()
    yield connected
    connected.close()


@pytest.fixture(scope="module")
def registry(client: StdioMcpClient) -> ToolRegistry:
    """Discover the server's tools and register them like the CLI does."""
    built = ToolRegistry()
    for tool in client.discover_tools():
        built.register(tool)
    return built


def _observation(registry: ToolRegistry, name: str, **arguments: object) -> str:
    """Run one discovered tool exactly as the loop would."""
    return registry.execute(f"{PREFIX}{name}", dict(arguments))


def test_discovery_finds_all_four_tools_under_the_server_prefix(
    registry: ToolRegistry,
) -> None:
    """Names arrive prefixed, so two servers can offer ``get_current_time``."""
    assert registry.names() == [
        f"{PREFIX}get_current_time",
        f"{PREFIX}convert_time",
        f"{PREFIX}find_meeting_slot",
        f"{PREFIX}count_working_days",
    ]


def test_invoking_the_new_tool_returns_a_usable_observation(
    registry: ToolRegistry,
) -> None:
    """The full path: model → registry → adapter → subprocess → back.

    Asserted on the JSON the model would read, not on an internal object,
    because that text is the actual deliverable of the invocation.
    """
    observation = _observation(
        registry,
        "find_meeting_slot",
        timezones=["Europe/Berlin", "Asia/Karachi"],
        date="2026-09-09",
        max_results=2,
    )

    payload = json.loads(observation)
    assert payload["slots_found"] == 21
    assert payload["slots"][0]["start_utc"] == "2026-09-09T07:00+00:00"
    assert payload["slots"][0]["local"][1]["start"] == "12:00"


def test_the_second_new_tool_reports_a_bad_argument_across_the_boundary(
    registry: ToolRegistry,
) -> None:
    """Its errors follow the same contract as the first new tool's.

    Worth asserting per tool rather than once for the server: the
    ``ToolError`` mapping is applied at each registration, so a tool added
    later without it would silently lose its messages and only this kind of
    test would notice.
    """
    observation = _observation(
        registry,
        "count_working_days",
        start_date="2026-09-30",
        end_date="2026-09-01",
    )

    assert observation.startswith("Error:")
    assert "is before start_date" in observation


def test_a_remote_failure_becomes_an_error_observation(
    registry: ToolRegistry,
) -> None:
    """A rejected argument must not end the run, and must explain itself.

    This is the whole failure contract in one assertion: the message crosses
    the process boundary intact (the server's ``ToolError`` mapping), and the
    harness turns it into an observation rather than an exception (ADR-006,
    ADR-016), so the agent can fix the argument and try again.
    """
    observation = _observation(
        registry,
        "find_meeting_slot",
        timezones=["Europe/Berlin", "Mars/Olympus"],
        date="2026-09-09",
    )

    assert observation.startswith("Error:")
    assert "Mars/Olympus" in observation
    assert "IANA timezone" in observation
