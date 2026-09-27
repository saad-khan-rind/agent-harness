"""A synchronous MCP client over an asyncio SDK.

This module is the *async bridge*. The official ``mcp`` SDK is asyncio-based
and its session is an async context manager that must stay open across many
calls; :mod:`harness.loop` and
:class:`~harness.llm.openai_compatible.OpenAICompatibleClient` are synchronous.
Something has to reconcile the two, and the rule from
``AGENTS.md`` is that the seam stays synchronous so ``loop.py`` and
``ToolRegistry`` are unaffected (ADR-039).

The shape chosen is **one owned event-loop thread per server**:

* a background thread runs an event loop and, inside it, a single
  long-lived coroutine that holds the session open;
* the synchronous side submits work with
  :func:`asyncio.run_coroutine_threadsafe` and blocks on the future.

The alternative — ``asyncio.run`` per call — was rejected because it would
tear the session down and relaunch the server subprocess on every tool call.

Why the session is entered and exited inside *one* coroutine rather than by
two separate submissions: the SDK is built on anyio task groups, and a
cancel scope entered in one task cannot be exited from another. Splitting
``__aenter__`` and ``__aexit__`` across two ``run_coroutine_threadsafe``
calls is exactly that error, so the lifetime lives in :meth:`_serve` and the
sync side only ever signals it to finish.

Known limitation, recorded rather than hidden: a server that accepts the
launch but never finishes the handshake is abandoned after
``mcp_connect_timeout``, and its child process survives until the harness
process exits. The REPL is not blocked — which was the requirement — but
the child is not reaped either. Reaping it means reaching past the SDK's
context manager into the process it owns, which is not worth doing before
the transport question is settled (ADR-039).
"""

import asyncio
import re
import tempfile
import threading
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TextIO

from mcp.client.stdio import stdio_client

from harness.tools.base import Tool
from mcp import Client, StdioServerParameters

# Extra seconds allowed on top of the configured per-call timeout before the
# synchronous side gives up on the background loop. The SDK enforces the
# real deadline; this is only a backstop against a wedged event loop, so a
# hung server can never block the REPL forever.
_FUTURE_TIMEOUT_MARGIN = 5.0

# How long to wait for the session handshake, and for the thread to unwind.
DEFAULT_CONNECT_TIMEOUT = 30.0
_SHUTDOWN_TIMEOUT = 5.0


class McpToolError(RuntimeError):
    """Raised when a call to a connected server fails.

    Subclasses ``RuntimeError`` deliberately. The SDK's own ``MCPError``
    derives straight from ``Exception``, which nothing in the tool layer
    catches — an SDK timeout would sail past ``RemoteTool._run`` *and*
    ``Tool.execute`` and end the run. Normalising provider exceptions into
    our own vocabulary at the client boundary is the same move
    :class:`~harness.llm.openai_compatible.OpenAICompatibleClient` makes for the
    OpenAI SDK
    (ADR-004), and it means the tool layer needs to know one exception type
    instead of the SDK's whole hierarchy.
    """


class McpConnectionError(RuntimeError):
    """Raised when a server cannot be launched or its handshake fails.

    Distinct from a tool failure, and handled differently: a tool failure is
    an observation the agent recovers from, while an unreachable server means
    this server contributes no tools at all. The CLI degrades to the built-in
    tool set and says so rather than aborting the session (ADR-006, ADR-016).
    """


class SdkMcpClient:
    """One MCP server, reached over the SDK and spoken to synchronously.

    Implements the :class:`~harness.mcp.protocol.McpClient` protocol
    structurally. One instance owns one server: aggregating several servers
    is the caller's job, which keeps this class's lifetime story simple —
    one thread, one session, one ``close``.

    Transport-agnostic by construction. ``target`` decides: launch
    parameters start a subprocess, a URL string opens a Streamable HTTP
    session, and everything below that line — the owned event loop, the
    reconnect, the timeouts, the deny-list — is identical (ADR-061).

    Attributes:
        server_name: Configured label for this server. It is also the prefix
            applied to every discovered tool name, so it reaches the model.
        refused_tools: Names this server advertised and the deny-list
            rejected. Filled in by :meth:`discover_tools` so the CLI can
            say what was blocked rather than leaving it invisible.
    """

    def __init__(
        self,
        server_name: str,
        target: StdioServerParameters | Any,
        timeout: float = 30.0,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        denied_tools: frozenset[str] = frozenset(),
    ) -> None:
        """Store connection parameters. Does not start the server.

        Construction is deliberately inert so that building the client and
        launching a subprocess are separate, observable steps: the CLI can
        report *which* server failed to start only if starting is its own
        call.

        Args:
            server_name: Label from ``config.yaml``, used as the tool prefix.
            target: What to connect to. ``StdioServerParameters`` launches
                a subprocess; a ``str`` is treated as a Streamable HTTP URL;
                the SDK also accepts an in-process server object, which is
                what the tests pass so that the real client code runs
                against a real protocol conversation (ADR-041, ADR-061).
            timeout: Seconds allowed for one tool call.
            connect_timeout: Seconds allowed for the launch and handshake.
            denied_tools: Names refused at discovery time, checked before
                the prefix is applied (ADR-042).
        """
        self.server_name = server_name
        self._target = target
        # Where a launched server's stderr goes. The SDK's default is the
        # harness's own stderr — the user's terminal — where every failed
        # tool call would print twice: raw from the server's log, and as the
        # harness's own `← ! Error` line. The server's log is for diagnosis,
        # so it goes to a file, and :meth:`connect` quotes its tail when a
        # launch fails.
        self.log_path = _server_log_path(server_name)
        self._log: TextIO | None = None
        self._timeout = timeout
        self._connect_timeout = connect_timeout
        self._denied_tools = denied_tools
        self.refused_tools: list[str] = []
        # The same refusals with their reason attached, so the CLI can say
        # whether a server declared the tool destructive or we matched a
        # word in its name (ADR-059).
        self.refusals: list[tuple[str, str]] = []
        # Whether a session was ever successfully opened. Reconnecting is
        # recovery of a *lost* session, never an implicit first connect: a
        # call before ``connect()`` is a caller bug and must still fail
        # loudly rather than quietly starting a subprocess (ADR-060).
        self._ever_connected = False
        # How many times this server had to be restarted mid-session. Kept
        # rather than logged so Week 3's observability layer has a number to
        # report: a server restarting on every call is a different problem
        # from one that never does (ADR-060).
        self.reconnects = 0

        # Owned by the background thread once connect() runs.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Client | None = None
        self._thread: threading.Thread | None = None
        self._shutdown: asyncio.Event | None = None

        # Crossing the thread boundary: _ready is set by the background
        # thread once the session is open *or* has failed, and _error carries
        # the failure back to the caller of connect().
        self._ready = threading.Event()
        self._error: BaseException | None = None

    # -- lifecycle ----------------------------------------------------------

    def connect(self) -> None:
        """Launch the server and complete the MCP handshake.

        Blocks until the session is usable, so a caller that returns from
        this method can rely on :meth:`discover_tools` working.

        Raises:
            McpConnectionError: If the command cannot be launched, the
                handshake fails, or neither happens within the timeout.
        """
        if self._thread is not None:
            raise McpConnectionError(
                f"MCP server {self.server_name!r} is already connected."
            )

        # Daemon thread so a wedged server can never keep the interpreter
        # alive after the user has left the REPL.
        self._thread = threading.Thread(
            target=self._run_event_loop,
            name=f"mcp-{self.server_name}",
            daemon=True,
        )
        self._thread.start()

        if not self._ready.wait(self._connect_timeout):
            raise McpConnectionError(
                f"MCP server {self.server_name!r} did not start within "
                f"{self._connect_timeout}s."
            )
        if self._error is not None:
            # The server's own explanation — a traceback, a missing module —
            # is in its log now rather than on the terminal, so the useful
            # end of it travels with the error.
            tail = self.log_tail()
            detail = f"\n  server log ({self.log_path}):\n  {tail}" if tail else ""
            raise McpConnectionError(
                f"Cannot start MCP server {self.server_name!r}: {self._error}{detail}"
            )
        self._ever_connected = True

    def _run_event_loop(self) -> None:
        """Thread entry point: own an event loop for this server's lifetime."""
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        finally:
            loop.close()

    async def _serve(self) -> None:
        """Hold the session open until :meth:`close` signals shutdown.

        Every failure is captured rather than raised, because this coroutine
        runs on a thread with no one to catch it; ``connect`` re-raises it on
        the caller's thread where it can be reported.
        """
        self._shutdown = asyncio.Event()
        try:
            # read_timeout_seconds is the SDK's own per-request deadline, so
            # a server that accepts a call and never answers still returns.
            async with Client(
                self._transport(), read_timeout_seconds=self._timeout
            ) as client:
                self._client = client
                self._ready.set()
                await self._shutdown.wait()
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            self._error = exc
        finally:
            self._client = None
            # Always set, so a connect() waiting on the handshake is released
            # by a failure just as it is by success.
            self._ready.set()

    def _transport(self) -> Any:
        """Return what the SDK connects through, with stderr sent to the log.

        Only a launched subprocess has a stderr to redirect. A URL or an
        in-process server is passed through as it was.
        """
        if not isinstance(self._target, StdioServerParameters):
            return self._target
        if self._log is None:
            try:
                self._log = self.log_path.open("a", encoding="utf-8")
            except OSError:
                # No writable temp directory: the terminal is noisier, the
                # server still works. Never a reason to refuse a server.
                return self._target
        return stdio_client(self._target, errlog=self._log)

    def log_tail(self, lines: int = 5) -> str:
        """Return the last few lines of the server's log, or an empty string.

        Args:
            lines: How many lines to return.
        """
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(text.strip().splitlines()[-lines:])

    def is_connected(self) -> bool:
        """Return whether a usable session is open right now.

        Reads the state ``_serve`` maintains rather than a flag of our own:
        that coroutine clears ``_client`` in its ``finally``, so a server
        whose subprocess died has already reported it here by the time the
        next call arrives.
        """
        return (
            self._loop is not None
            and self._client is not None
            and not self._loop.is_closed()
        )

    def _reconnect(self) -> bool:
        """Tear the dead session down and start a fresh one.

        Servers die: a subprocess is OOM-killed, a network transport drops,
        a server crashes on some other client's request. Without this, the
        first such death makes every later call to that server fail for the
        rest of the session, and the harness reports a permanently broken
        tool for a problem that a restart fixes.

        Everything ``connect`` guards on is reset first, including
        ``_error`` — the old failure is still stored there, and leaving it
        would make the new ``connect`` re-raise the death we are recovering
        from.

        Bounded by construction rather than by a counter: at most one
        attempt per call. A server that keeps dying therefore costs one
        restart per call, and the loop's own failure limit takes the tool out
        of service after five (ADR-048, ADR-060).

        Returns:
            Whether a usable session is open again.
        """
        self.close()
        self._loop = None
        self._client = None
        self._thread = None
        self._shutdown = None
        self._ready = threading.Event()
        self._error = None
        try:
            self.connect()
        except McpConnectionError:
            return False
        self.reconnects += 1
        return True

    def close(self) -> None:
        """Shut the session down and stop the background thread.

        Safe to call twice and safe to call on a client that never connected,
        because the CLI's cleanup path runs whether or not startup succeeded.
        """
        loop, shutdown = self._loop, self._shutdown
        # The loop may already be gone: a server that failed to launch makes
        # ``_serve`` return, and ``_run_event_loop`` then closes the loop in
        # its own ``finally``. Signalling a closed loop raises, and this
        # method is called from ``connect_mcp_servers``' failure path — so an
        # unguarded call would turn "one server is unavailable" into a crash
        # at startup, which is the exact opposite of the degradation policy.
        if loop is not None and shutdown is not None and not loop.is_closed():
            try:
                # call_soon_threadsafe is the only sanctioned way to touch an
                # event loop from another thread.
                loop.call_soon_threadsafe(shutdown.set)
            except RuntimeError:
                # Closed between the check and the call. The thread is
                # already unwinding, which is what we were asking it to do.
                pass
        if self._thread is not None:
            self._thread.join(timeout=_SHUTDOWN_TIMEOUT)
        self._thread = None
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None
        # A deliberate shutdown ends the session for good. Clearing this is
        # what stops a later call from quietly relaunching a server the
        # caller just closed: reconnection recovers a *lost* session, never
        # an abandoned one (ADR-060). ``_reconnect`` calls ``close`` first
        # and then ``connect``, which sets it again.
        self._ever_connected = False

    # -- the protocol -------------------------------------------------------

    def discover_tools(self) -> list[Tool]:
        """Return this server's tools, wrapped as harness tools.

        Returning our own :class:`~harness.tools.base.Tool` type is what lets
        remote tools register on the same registry the loop already uses
        instead of needing a second dispatcher.

        Returns:
            Wrapped tools, denied names excluded. An empty list is a valid
            answer: a server may legitimately expose no tools.

        Raises:
            McpConnectionError: If the listing fails or the client is not
                connected.
        """
        # Imported here rather than at module scope: the adapter builds tools
        # that call back into this class, and a top-level import in both
        # directions would be a cycle.
        from harness.mcp.adapter import denied_entries, wrap_remote_tools

        listed = self._submit(self._list_tools())
        self.refusals = denied_entries(listed, self._denied_tools)
        self.refused_tools = [name for name, _ in self.refusals]
        return wrap_remote_tools(
            server_name=self.server_name,
            remote_tools=listed,
            call=self.call_tool,
            denied_tools=self._denied_tools,
        )

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Invoke one remote tool and return its observation text.

        The name is the *remote* name, without the server prefix: prefixing
        is a presentation concern for the model, and the server would not
        recognise the decorated name.

        Args:
            name: Tool name as the server advertises it.
            arguments: JSON-object arguments.

        Returns:
            Observation text for the ReAct context.

        Raises:
            McpConnectionError: If the client is not connected.
            McpToolError: If the tool reported a failure, the call timed
                out, or the transport broke. The caller is
                ``RemoteTool._run``, which turns it into an ``Error:``
                observation rather than ending the run (ADR-006).
        """
        try:
            result = self._submit(self._call_tool(name, arguments))
        except (McpConnectionError, McpToolError):
            # One retry, and only for the failure that a retry can fix: the
            # session is gone, so the subprocess died or the transport broke
            # underneath us. A tool that ran and failed, or a server that is
            # merely slow, is still connected — those re-raise untouched,
            # because retrying them would double the wait and change nothing
            # (ADR-060).
            if self.is_connected() or not self._ever_connected:
                raise
            if not self._reconnect():
                raise
            result = self._submit(self._call_tool(name, arguments))
        text = _result_text(result)
        # is_error is how the SDK reports a tool that ran and failed. Raising
        # here routes it through Tool.execute, which owns the "Error:" prefix
        # so remote failures read exactly like local ones (ADR-006).
        if result.is_error:
            raise McpToolError(text or f"remote tool {name!r} failed")
        return text

    # -- crossing the thread boundary --------------------------------------

    async def _list_tools(self) -> list[Any]:
        """Ask the server for its tool list. Runs on the background loop."""
        assert self._client is not None
        listed = await self._client.list_tools()
        return list(listed.tools)

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Invoke one remote tool. Runs on the background loop."""
        assert self._client is not None
        return await self._client.call_tool(name, arguments)

    def _submit(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """Run a coroutine on the background loop and wait for its result.

        Args:
            coro: Coroutine to schedule on this client's event loop.

        Returns:
            Whatever the coroutine returned.

        Raises:
            McpConnectionError: If the client is not connected.
            McpToolError: On any SDK or transport failure, so the tool
                layer turns it into an observation instead of a crash.
        """
        if self._loop is None or self._client is None:
            # Close the coroutine explicitly: an un-awaited coroutine would
            # otherwise emit a RuntimeWarning far away from the real problem.
            coro.close()
            raise McpConnectionError(
                f"MCP server {self.server_name!r} is not connected."
            )
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=self._timeout + _FUTURE_TIMEOUT_MARGIN)
        except TimeoutError as exc:
            # The backstop fired, meaning the background loop itself is stuck.
            # Cancel so it is not left computing a result nobody will collect.
            future.cancel()
            raise McpToolError(
                f"MCP server {self.server_name!r} did not respond within "
                f"{self._timeout}s."
            ) from exc
        except Exception as exc:
            # Deliberately broad, and deliberately here. This is the one line
            # in the harness that faces the MCP SDK, and the failures it can
            # produce are open-ended: protocol errors, its own read timeout,
            # a subprocess that died mid-call and left a broken stream. The
            # tool layer must see one type it can turn into an observation,
            # so everything is re-raised as McpToolError with the original
            # attached as __cause__ for anyone debugging.
            raise McpToolError(
                f"MCP server {self.server_name!r} failed: {exc}"
            ) from exc


def _result_text(result: Any) -> str:
    """Flatten a ``CallToolResult`` into one observation string.

    The MCP content model is a list of typed blocks (text, image, audio,
    resource links). The model reads plain text, so text blocks are joined
    and anything else is named rather than dropped silently — a tool that
    returned only an image should say so, not appear to have returned
    nothing.

    Args:
        result: The SDK's ``CallToolResult``.

    Returns:
        Observation text, possibly empty.
    """
    parts: list[str] = []
    for block in result.content or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
        else:
            parts.append(f"[{getattr(block, 'type', 'unknown')} content]")
    return "\n".join(parts).strip()


# The class was called ``StdioMcpClient`` while stdio was the only transport
# (ADR-039). The name is kept as an alias rather than removed: it is imported
# by modules and tests on open branches, and a rename that breaks a branch
# in flight costs more than one line of indirection (ADR-061).
StdioMcpClient = SdkMcpClient


def _server_log_path(server_name: str) -> Path:
    """Return the log file for one server's stderr.

    In the system temp directory: it is diagnostics for this machine, not
    state worth a volume, and every harness process may write there. The
    name is reduced to safe characters because it comes from ``config.yaml``.

    Args:
        server_name: The configured server label.

    Returns:
        ``<tmp>/agent-harness-mcp-<name>.log``.
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", server_name) or "server"
    return Path(tempfile.gettempdir()) / f"agent-harness-mcp-{safe}.log"
