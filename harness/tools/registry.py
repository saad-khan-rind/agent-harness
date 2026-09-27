"""Register tools and expose OpenAI-style schemas to the loop.

The registry is the *only* view the ReAct loop has of the agent's
capabilities. That single indirection is what lets Week 2 add MCP tools and
Week 3 add permission policies without editing ``loop.py``: an MCP client
discovers remote tools, wraps them as :class:`~harness.tools.base.Tool`
objects, and calls :meth:`ToolRegistry.register` on the same instance the
loop already holds.
"""

from typing import Any

from harness.tools.base import ERROR_PREFIX, Tool, ToolOutcome


class ToolRegistry:
    """Name to tool map. The ReAct loop's only view of capabilities."""

    def __init__(self) -> None:
        """Create an empty registry.

        Backed by a plain ``dict``, which preserves insertion order in
        Python 3.7+. Order matters more than it looks: it fixes the sequence
        of schemas sent to the model, and a stable prompt is a prerequisite
        for reproducible runs.
        """
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Add a tool, rejecting duplicate names.

        Duplicates are a hard error rather than a silent overwrite. Two tools
        sharing a name is always a bug — and once MCP servers register
        alongside built-ins (Week 2), a silent overwrite would let a remote
        server shadow a local tool, which is a security problem, not a
        naming inconvenience.

        Args:
            tool: Instance to expose to the LLM.

        Raises:
            ValueError: If ``tool.name`` is already registered.
        """
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        """Look up a tool by name.

        Args:
            name: Registered tool name.

        Returns:
            The matching tool.

        Raises:
            KeyError: If ``name`` is not registered.
        """
        return self._tools[name]

    def schemas(self) -> list[dict[str, Any]]:
        """Return OpenAI function-calling schemas for every registered tool.

        Rebuilt on each call rather than cached: tools can be registered at
        runtime (MCP discovery), and a stale cache would hide a tool from
        the model with no visible symptom.
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self._tools.values()
        ]

    def invoke(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        """Dispatch ``name`` and return its outcome.

        An unknown tool name returns a failed outcome instead of raising. The
        model *does* hallucinate tool names, and that is a recoverable
        mistake: listing the real names lets it correct itself on the next
        turn, whereas an exception would end the run. It is reported as
        ``ok=False`` so the loop's failure guard counts it like any other
        failure — a model inventing the same name repeatedly is exactly the
        runaway ADR-048 exists to stop.

        Args:
            name: Tool name the model asked for.
            arguments: Parsed JSON arguments.

        Returns:
            The tool's outcome, or a failed one naming the valid tools.
        """
        tool = self._tools.get(name)
        if tool is None:
            available = ", ".join(self.names()) or "none"
            return ToolOutcome(
                f"{ERROR_PREFIX} unknown tool {name!r}. Available tools: {available}.",
                ok=False,
            )
        return tool.invoke(arguments)

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        """Dispatch ``name`` and return only the observation text.

        The string-only form of :meth:`invoke`, kept for callers that have no
        use for the boolean. The loop is not one of them (ADR-058).

        Args:
            name: Tool name the model asked for.
            arguments: Parsed JSON arguments.

        Returns:
            Tool observation string, or an error naming the valid tools.
        """
        return self.invoke(name, arguments).text

    def names(self) -> list[str]:
        """Return registered tool names in insertion order."""
        return list(self._tools.keys())

    def __len__(self) -> int:
        """Return how many tools are registered.

        Lets the CLI and loop test ``if not registry`` rather than
        reaching into the private dict — used by the banner, and by the
        tests that assert an empty registry sends no tool schemas.
        """
        return len(self._tools)
