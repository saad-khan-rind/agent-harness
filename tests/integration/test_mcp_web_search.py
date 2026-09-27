"""Integration: the web research server over a real MCP conversation.

The server is built in-process and handed to the real
:class:`~harness.mcp.client.StdioMcpClient`, so the handshake, ``list_tools``,
the adapter, the registry and the observation the model would read are all
genuine — only the network is replaced, by an injected backend, transport and
resolver. That is the line ADR-041 draws: fake the far end, never the client.

A subprocess test like ``test_mcp_time_plus.py`` is deliberately not the
shape here. Launching ``python -m mcp_server_web_search`` for real would put
the public internet inside the test suite, and ``.github/workflows/ci.yml`` promises
neither network nor credentials.
"""

import json
from typing import Any

import pytest
from harness.mcp.client import StdioMcpClient
from harness.tools.registry import ToolRegistry
from mcp_server_web_search.fetch import Response
from mcp_server_web_search.server import build_server

SERVER_LABEL = "web"
PREFIX = f"{SERVER_LABEL}__"

PAGE = (
    "<html><head><title>MCP spec</title></head><body>"
    "<script>tracker()</script><p>The protocol is transport agnostic.</p>"
    "</body></html>"
)


class Backend:
    """Scripted search backend."""

    def text(self, query: str, max_results: int) -> list[dict[str, Any]]:
        return [
            {
                "title": "Model Context Protocol",
                "href": "https://example.com/mcp",
                "body": "An open protocol for connecting models to tools.",
            }
        ][:max_results]


class Http:
    """Scripted transport."""

    def get(self, url: str) -> Response:
        return Response(status_code=200, content_type="text/html", text=PAGE)


@pytest.fixture(scope="module")
def registry() -> ToolRegistry:
    """Connect a real client to the real server and register what it offers."""
    client = StdioMcpClient(
        server_name=SERVER_LABEL,
        target=build_server(
            backend=Backend(),
            http_client=Http(),
            resolve=lambda _host: ["93.184.216.34"],
        ),
        timeout=20.0,
    )
    client.connect()
    built = ToolRegistry()
    for tool in client.discover_tools():
        built.register(tool)
    yield built
    client.close()


def test_both_tools_are_discovered_and_prefixed(registry: ToolRegistry) -> None:
    assert registry.names() == [f"{PREFIX}web_search", f"{PREFIX}web_fetch"]


def test_web_search_returns_title_url_and_snippet(registry: ToolRegistry) -> None:
    payload = json.loads(
        registry.execute(f"{PREFIX}web_search", {"query": "model context protocol"})
    )
    assert payload["count"] == 1
    assert payload["results"][0] == {
        "title": "Model Context Protocol",
        "url": "https://example.com/mcp",
        "snippet": "An open protocol for connecting models to tools.",
    }


def test_web_fetch_returns_readable_text_without_scripts(
    registry: ToolRegistry,
) -> None:
    payload = json.loads(
        registry.execute(f"{PREFIX}web_fetch", {"url": "https://example.com/mcp"})
    )
    assert payload["title"] == "MCP spec"
    assert payload["content"] == "The protocol is transport agnostic."
    assert "tracker" not in payload["content"]


def test_a_rejected_url_reaches_the_model_as_a_readable_error(
    registry: ToolRegistry,
) -> None:
    # The message must survive the trip: ToolError -> is_error -> McpToolError
    # -> RemoteTool._run -> "Error: ...". A model that mistyped a scheme has
    # to be told what was wrong with it (ADR-006).
    observation = registry.execute(f"{PREFIX}web_fetch", {"url": "file:///etc/passwd"})
    assert observation.startswith("Error:")
    assert "only http and https" in observation
