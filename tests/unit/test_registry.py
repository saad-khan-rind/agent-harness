"""The tool registry: the loop's only view of the agent's capabilities."""

from pathlib import Path
from typing import Any

import pytest
from harness.tools import ToolRegistry
from harness.tools.base import Tool, ToolResult


class _Echo(Tool):
    """Minimal tool used to test registry mechanics in isolation."""

    name = "echo"
    description = "Echo the message back."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
    }
    risk = "observe"

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        """Return the message argument unchanged."""
        return ToolResult(self._string_argument(arguments, "message"))


def test_register_and_execute(workspace: Path) -> None:
    """A registered tool can be dispatched by name."""
    registry = ToolRegistry()
    registry.register(_Echo(workspace))
    assert registry.execute("echo", {"message": "hi"}) == "hi"


def test_duplicate_name_is_rejected(workspace: Path) -> None:
    """Silent shadowing would be a security bug once MCP registers tools."""
    registry = ToolRegistry()
    registry.register(_Echo(workspace))
    with pytest.raises(ValueError, match="Duplicate tool name"):
        registry.register(_Echo(workspace))


def test_unknown_tool_returns_an_observation(workspace: Path) -> None:
    """A hallucinated tool name is recoverable, so it must not raise.

    The message lists the real names so the model can correct itself.
    """
    registry = ToolRegistry()
    registry.register(_Echo(workspace))
    result = registry.execute("teleport", {})
    assert result.startswith("Error:")
    assert "echo" in result


def test_schemas_match_the_openai_shape(registry: ToolRegistry) -> None:
    """The schema list is sent verbatim to the API, so its shape is a contract."""
    schemas = registry.schemas()
    assert len(schemas) == 8
    for schema in schemas:
        assert schema["type"] == "function"
        function = schema["function"]
        assert set(function) == {"name", "description", "parameters"}
        assert function["parameters"]["type"] == "object"


def test_observe_tools_are_listed_first(registry: ToolRegistry) -> None:
    """Ordering nudges the model to look before it mutates."""
    risks = [registry.get(name).risk for name in registry.names()]
    assert risks == ["observe"] * 4 + ["mutate"] * 4
