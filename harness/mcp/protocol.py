"""MCP client protocol.

Discovered remote tools must ``register()`` on the same ``ToolRegistry``
the loop already uses. Do not add a second dispatcher in ``loop.py``.
"""

from typing import Any, Protocol

from harness.tools.base import Tool


class McpClient(Protocol):
    """Connect to an MCP server and surface its tools as ``Tool`` objects."""

    def connect(self) -> None:
        """Open the configured MCP session."""

    def discover_tools(self) -> list[Tool]:
        """Return remote tools ready to register.

        Returns:
            ``Tool`` wrappers with schemas from the server.
        """

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Invoke a remote tool and return an observation.

        Args:
            name: Tool name advertised by the server.
            arguments: JSON-object arguments.

        Returns:
            Observation string for the ReAct context.
        """
