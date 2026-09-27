"""Connect the configured MCP servers and collect their tools.

One function stands between ``config.yaml`` and the tool registry. It exists
so the CLI does not grow a loop over servers with error handling inside it,
and so the *degradation policy* lives in one readable place.

That policy, following the Week 1 split (ADR-006, ADR-016): a server that
will not start costs the harness that server's tools and nothing else. The
session continues with whatever did start, up to and including nothing at
all. An MCP server is an optional capability, and a harness that refused to
run because a weather server was down would be worse than one that runs
without weather.

Nothing here prints. ``AGENTS.md`` bans ``print`` outside ``cli.py``, so
problems are returned as ``warnings`` and the CLI decides how to show them.
"""

from dataclasses import dataclass, field

from harness.config import McpServerSettings
from harness.mcp.client import (
    DEFAULT_CONNECT_TIMEOUT,
    McpConnectionError,
    SdkMcpClient,
)
from harness.tools.base import DEFAULT_MAX_OUTPUT_CHARS, Tool
from mcp import StdioServerParameters


@dataclass
class McpDiscovery:
    """What connecting to the configured servers produced.

    Attributes:
        tools: Every wrapped remote tool, ready to register. Ordered by
            server, then by the order each server listed them, so the schema
            list the model sees is stable across runs.
        clients: Connected clients, kept only so they can be closed. The
            registry holds the tools; this holds the subprocesses.
        warnings: Human-readable lines for the CLI to print — servers that
            failed, and tools that were refused by the deny-list.
    """

    tools: list[Tool] = field(default_factory=list)
    clients: list[SdkMcpClient] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def close(self) -> None:
        """Shut every connected server down.

        Never raises. This runs from the CLI's cleanup path, which may itself
        be unwinding from an error; a failure to stop one child process must
        not mask the reason the session is ending.
        """
        for client in self.clients:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - cleanup must not raise
                continue


def connect_mcp_servers(
    servers: list[McpServerSettings],
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
) -> McpDiscovery:
    """Start each enabled server and wrap the tools it advertises.

    Args:
        servers: Configured servers, in ``config.yaml`` order.
        max_output_chars: Per-observation character cap, applied to remote
            tools exactly as it is to built-in ones.
        connect_timeout: Seconds allowed per server for launch and handshake.

    Returns:
        The tools, the clients to close later, and any warnings to print.
    """
    discovery = McpDiscovery()

    for server in servers:
        # A disabled entry is skipped silently: switching one off is a
        # deliberate act, not a problem to report on every start.
        if not server.enabled:
            continue

        client = SdkMcpClient(
            server_name=server.name,
            target=_target_for(server),
            timeout=server.timeout,
            connect_timeout=connect_timeout,
            denied_tools=frozenset(server.denied_tools),
        )
        try:
            client.connect()
            tools = client.discover_tools()
        except (McpConnectionError, RuntimeError, OSError) as exc:
            # Degrade rather than abort. The client may have started a
            # subprocess before failing, so it is closed either way.
            client.close()
            discovery.warnings.append(
                f"MCP server {server.name!r} is unavailable and its tools "
                f"were not loaded: {exc}"
            )
            continue

        discovery.clients.append(client)
        discovery.tools.extend(tools)
        if client.refusals:
            # Named *and* reasoned, not counted. The user needs to know
            # which tool is missing before they go looking for why the agent
            # will not use it, and whether the server declared it destructive
            # or we matched a word in its name (ADR-042, ADR-059).
            listed = ", ".join(f"{name} ({reason})" for name, reason in client.refusals)
            discovery.warnings.append(
                f"MCP server {server.name!r} offered tools this harness "
                f"refuses to register: {listed}."
            )

    return discovery


def _target_for(server: McpServerSettings) -> StdioServerParameters | str:
    """Return what the SDK should connect to for this server entry.

    One function so the transport choice is made in exactly one place. The
    SDK reads the type of what it is handed: a ``StdioServerParameters``
    launches a subprocess, a URL string opens a Streamable HTTP session. The
    rest of the client is identical either way, which is the reason adding
    HTTP was an addition rather than a second client (ADR-061).
    """
    if server.transport == "http":
        # Validated at startup to be an absolute http(s) URL, so nothing
        # here has to defend against a path or a bare hostname.
        return str(server.url)
    return _stdio_parameters(server)


def _stdio_parameters(server: McpServerSettings) -> StdioServerParameters:
    """Build the SDK's launch parameters for one configured server.

    ``env`` is passed through as configured rather than merged with the
    harness's own environment: a child process should inherit exactly what
    the configuration says it needs, and nothing about our InnKube token.
    """
    return StdioServerParameters(
        command=str(server.command),
        args=list(server.args),
        env=dict(server.env) or None,
    )
