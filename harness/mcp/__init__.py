"""MCP client package.

Remote tools reach a run the same way memory and skills do: they become
:class:`~harness.tools.base.Tool` objects and register on the registry the
loop already holds. ``loop.py`` does not import this package and does not
know a tool is remote (ADR-039).
"""

from harness.mcp.adapter import (
    NAME_SEPARATOR,
    RemoteTool,
    denied_entries,
    denied_names,
    is_denied,
    is_destructive,
    refusal_reason,
    wrap_remote_tools,
)
from harness.mcp.client import (
    McpConnectionError,
    McpToolError,
    SdkMcpClient,
    StdioMcpClient,
)
from harness.mcp.discovery import McpDiscovery, connect_mcp_servers
from harness.mcp.protocol import McpClient

# Re-exported from its new home in the tool contract, where the permission
# policy also reads it, so both halves of the ADR-008 guarantee — refusal at
# registration and refusal at call time — share one list (ADR-057).
from harness.tools.base import DENIED_TOOL_WORDS

__all__ = [
    "DENIED_TOOL_WORDS",
    "NAME_SEPARATOR",
    "McpClient",
    "McpConnectionError",
    "McpDiscovery",
    "McpToolError",
    "RemoteTool",
    "SdkMcpClient",
    "StdioMcpClient",
    "connect_mcp_servers",
    "denied_entries",
    "denied_names",
    "is_denied",
    "is_destructive",
    "refusal_reason",
    "wrap_remote_tools",
]
