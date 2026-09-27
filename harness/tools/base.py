"""Tool contract shared by built-in file tools and later MCP wrappers.

Design note (why a template method):
    The lab brief stresses that tool failures "must be communicated upstream
    effectively" (Section 2, *Tools*). An agent that crashes on a missing
    file is useless; an agent that *reads* "Error: no such file" can recover
    on the next turn. So :meth:`Tool.invoke` is a **template method** that
    wraps the subclass hook :meth:`Tool._run` in one shared try/except.

    Consequences:
    * A new tool cannot forget its error handling — it gets it by existing.
    * The ReAct loop never sees an exception from a tool, only an
      observation string, which keeps ``loop.py`` free of tool-specific
      recovery logic.
    * Truncation is applied in one place, so no single tool can blow up the
      context window (the "context rot" problem in Section 2, *Context*).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from harness.observability import errors
from harness.tools.paths import PathEscapeError, resolve_in_workspace

# Split used both for documentation and, from Week 3, for permission
# policies: "observe" tools only read, "mutate" tools change the workspace.
# Deletion is deliberately absent — it is not a risk level we support.
RiskLevel = Literal["observe", "mutate"]

# The second axis of a tool's permission tier, orthogonal to ``risk``:
# not "does it change something" but "does anything leave the sandbox".
# A web search changes nothing and is still worth a person's yes, because
# the query goes to a third party; a calendar write changes something and
# stays on this disk. ``risk`` alone could not tell those apart, which is
# why the web tools were filed under the same label as ``read_file`` for a
# week (ADR-055's follow-up, ADR-102).
Reach = Literal["local", "open_world"]

# Fallback cap on a single observation. Overridden per-process from
# ``Settings.max_tool_output_chars`` so it can be tuned without a code edit.
DEFAULT_MAX_OUTPUT_CHARS = 4000

# The marker every failed observation starts with. Defined once because
# three places depend on it: the tools that produce it, the registry's
# unknown-name message, and the loop, which reads it to count a tool's
# consecutive failures (ADR-048). A string literal copied around would let
# one of them drift and silently stop counting.
ERROR_PREFIX = "Error:"

# Tool names refused wherever a name is checked. It lives here, in the tool
# contract, rather than in ``harness/mcp/adapter.py`` where it started,
# because two unrelated subsystems now have to agree on it: the MCP adapter
# refuses these names at *registration* (ADR-042), and the permission policy
# refuses them at *call time* (ADR-057). Two copies of the list would let one
# drift, and the thing that drifts would be a safety guarantee — ADR-008 says
# this agent cannot delete anything. A rename or a move is a delete of the
# old path, so both are here too.
DENIED_TOOL_WORDS = frozenset(
    {
        "delete",
        "remove",
        "unlink",
        "rmdir",
        "destroy",
        "purge",
        "truncate",
        "rename",
        "move",
        "drop",
    }
)


@dataclass(frozen=True)
class ToolResult:
    """Observation returned to the ReAct loop after a tool call.

    Frozen because a result is a value, not a mutable buffer: once a tool has
    decided what happened, nothing downstream should edit it.

    Attributes:
        content: Human-readable tool output or error text.
        ok: Whether the tool completed without a handled failure. ``False``
            still produces an observation — the loop keeps running and lets
            the model correct itself.
    """

    content: str
    ok: bool = True

    def as_observation(self) -> str:
        """Format this result as the string the LLM will read.

        The ``Error:`` prefix is load-bearing: it is the only signal the
        model gets that its last action failed, since a failed tool call and
        a successful one arrive through the same message channel.
        """
        return self.content if self.ok else f"{ERROR_PREFIX} {self.content}"


@dataclass(frozen=True)
class ToolOutcome:
    """The finished result of one dispatched tool call.

    Distinct from :class:`ToolResult`, and the difference is the whole point
    of the type. ``ToolResult`` is what a *tool author* returns from
    :meth:`Tool._run`: raw content, not yet truncated, not yet marked as an
    error. ``ToolOutcome`` is what a *caller* receives from
    :meth:`Tool.invoke` and :meth:`~harness.tools.registry.ToolRegistry.invoke`:
    ``text`` is final — already truncated, already carrying the ``Error:``
    prefix when it failed — so there is nothing left for the caller to
    remember to do to it.

    It exists because the loop needs both halves of a tool call and used to
    get only one. ``execute`` returned a string, so the loop recovered the
    failure bit by testing ``observation.startswith(ERROR_PREFIX)`` — a typed
    value thrown away and then reconstructed by sniffing text. Two guards
    (ADR-048, ADR-049) and now the permission refusals depend on that bit
    being right, and any tool whose *successful* output happened to begin
    with ``Error:`` would have broken it. The string prefix stays for the
    model, which has no other way to see that its action failed; the boolean
    stays for the loop (ADR-058).

    Attributes:
        text: The exact observation to append to the run context.
        ok: Whether the call succeeded. ``False`` covers a tool failure, an
            unknown tool name, and a call the permission policy refused.
        error_type: *Why* it failed, as one of the bounded values in
            :mod:`harness.observability.errors`, or ``None`` on success.

            This is the second half of the argument ADR-058 started. That
            ADR observed that the loop was reconstructing a *boolean* by
            sniffing text and gave it the boolean instead. The same applies
            to the reason: a missing file, a path escape, a dead MCP server
            and a policy refusal all produce ``ok=False`` and an ``Error:``
            string, and telemetry was left to guess between them from
            wording. Set here, where the exception is still in scope, the
            reason is carried rather than inferred (ADR-094).

            Nothing in the control plane branches on it — the loop's guards
            still read ``ok`` alone — so a wrong value mislabels a graph
            and cannot change what the agent does.
    """

    text: str
    ok: bool = True
    error_type: str | None = None


def truncate(text: str, limit: int) -> str:
    """Clip ``text`` to ``limit`` characters, saying so when it clips.

    A silent truncation is worse than a loud one: the model would treat a
    partial directory listing or file as complete and reason from it. The
    appended notice tells it to narrow the request instead.

    Args:
        text: Raw tool output.
        limit: Maximum characters to keep. Values below 1 disable clipping,
            which is useful in tests that assert on full output.

    Returns:
        Either ``text`` unchanged, or a clipped prefix plus a marker.
    """
    if limit < 1 or len(text) <= limit:
        return text
    omitted = len(text) - limit
    return f"{text[:limit]}\n... [truncated {omitted} characters]"


class Tool(ABC):
    """One named capability exposed to the LLM.

    Subclasses set the four class attributes below and implement
    :meth:`_run`. They never implement :meth:`execute` — that is the shared
    template method.

    Attributes:
        name: Function name sent to the model. Must be unique in a registry.
        description: One line the model uses to pick this tool. Kept short
            and unambiguous on purpose: overlapping descriptions are the
            main cause of wrong tool selection.
        parameters: JSON Schema for the arguments object.
        risk: ``observe`` or ``mutate``.
        reach: ``local`` or ``open_world``. Defaults to ``local`` because
            every built-in tool is confined to the sandbox roots; only a
            tool that talks to something outside the process — the MCP
            adapter sets it from the server's ``openWorldHint`` — says
            otherwise.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    risk: RiskLevel
    reach: Reach = "local"

    def __init__(
        self,
        workspace_root: Path,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    ) -> None:
        """Bind the tool to a workspace root and an output budget.

        Args:
            workspace_root: Directory all paths must stay inside. Resolved
                once here so every later containment check compares against
                a stable, symlink-free root.
            max_output_chars: Per-observation character cap.
        """
        self.workspace_root = workspace_root.resolve()
        self.max_output_chars = max_output_chars

    def risk_for(self, arguments: dict[str, Any]) -> RiskLevel:
        """Return the risk of this particular call.

        The class attribute ``risk`` is what the tool *can* do; this is what
        one call with these arguments does. They are the same for every tool
        but ``delegate``, whose risk depends on which specialists it hands
        work to (ADR-119). The permission policy reads this, never a string
        test on the arguments of its own.

        Args:
            arguments: The call's decoded arguments.

        Returns:
            ``observe`` or ``mutate``. Never less than the tool can justify:
            an override that cannot tell must answer ``risk``.
        """
        return self.risk

    def invoke(self, arguments: dict[str, Any]) -> ToolOutcome:
        """Run the tool and return a typed outcome, never an exception.

        This is the template method — the one place a tool's exceptions
        become observations. Each ``except`` clause below maps a failure mode
        the LLM can actually cause to a message it can act on. Order matters:
        the specific ``OSError`` subclasses must precede the generic
        ``OSError``.

        Returning :class:`ToolOutcome` rather than a bare string is what lets
        the loop's guards read ``ok`` instead of re-deriving it from the text
        (ADR-058). :meth:`execute` is the string-only convenience on top.

        Args:
            arguments: JSON-object arguments decoded from the model's tool
                call. Assumed well-formed JSON but *not* assumed to match
                the schema — models get this wrong, so it is validated here.

        Returns:
            The final observation, whether the call succeeded, and — when it
            did not — which kind of failure it was. Each ``except`` clause
            names its own ``error_type`` because this is the last point at
            which the exception exists: one line further down it is a string
            and every reader after that is guessing (ADR-094).
        """
        error_type: str | None = None
        try:
            result = self._run(arguments)
        except PathEscapeError as exc:
            # Security refusal. Phrased as a rule, not a bug, so the model
            # retries inside the workspace instead of retrying the escape.
            result = ToolResult(str(exc), ok=False)
            error_type = errors.TOOL_PATH_ESCAPE
        except (KeyError, TypeError) as exc:
            # Missing or wrongly typed argument: the model ignored the schema.
            result = ToolResult(f"Invalid arguments for {self.name}: {exc}", ok=False)
            error_type = errors.TOOL_INVALID_ARGUMENTS
        except FileNotFoundError:
            result = ToolResult(
                f"No such file or directory: {arguments.get('path', '?')}", ok=False
            )
            error_type = errors.TOOL_NOT_FOUND
        except IsADirectoryError:
            result = ToolResult(
                f"Path is a directory, not a file: {arguments.get('path', '?')}",
                ok=False,
            )
            error_type = errors.TOOL_INVALID_ARGUMENTS
        except NotADirectoryError:
            result = ToolResult(
                f"Path is a file, not a directory: {arguments.get('path', '?')}",
                ok=False,
            )
            error_type = errors.TOOL_INVALID_ARGUMENTS
        except PermissionError:
            result = ToolResult(
                f"Permission denied by the operating system: "
                f"{arguments.get('path', '?')}",
                ok=False,
            )
            error_type = errors.TOOL_ACCESS_DENIED
        except UnicodeDecodeError:
            # Binary file. Worth its own message: the model should stop
            # retrying the read rather than assume a transient failure.
            result = ToolResult(
                f"File is not valid UTF-8 text and cannot be read as text: "
                f"{arguments.get('path', '?')}",
                ok=False,
            )
            error_type = errors.TOOL_INVALID_ARGUMENTS
        except ValueError as exc:
            # Tool-specific precondition, e.g. edit_file's "must match once".
            result = ToolResult(str(exc), ok=False)
            error_type = errors.TOOL_INVALID_ARGUMENTS
        except OSError as exc:
            # Catch-all for the rest of the file-system surface (disk full,
            # name too long, ...). Still an observation, never a crash.
            result = ToolResult(f"File system error: {exc}", ok=False)
            error_type = errors.TOOL_FAILED

        # A subclass can also return a failed result without raising —
        # `RemoteTool` does exactly that for an MCP error it caught itself.
        # Those have no exception to classify, so the text is read as a last
        # resort. `_run` may set `_last_error_type` to pre-empt that.
        if not result.ok and error_type is None:
            error_type = self._error_type_for(result)

        # Truncate last so error messages are capped too, and so subclasses
        # never have to think about the budget.
        clipped = ToolResult(truncate(result.content, self.max_output_chars), result.ok)
        return ToolOutcome(clipped.as_observation(), clipped.ok, error_type)

    def _error_type_for(self, result: ToolResult) -> str:
        """Classify a failed result a subclass returned rather than raised.

        Overridden by :class:`~harness.mcp.adapter.RemoteTool`, which knows
        its failures are remote ones and can say so precisely. The default
        falls back to reading the message, which is the weakest classifier
        in the system and is why the ``except`` clauses above do not use it.

        Args:
            result: The failed result from :meth:`_run`.

        Returns:
            An entry from
            :data:`~harness.observability.errors.ERROR_TYPES`.
        """
        return errors.classify_tool_failure(result.content)

    def execute(self, arguments: dict[str, Any]) -> str:
        """Run the tool and return only the observation text.

        Kept as the readable form of :meth:`invoke` for callers that have no
        use for the boolean — the tests, and anyone driving a single tool by
        hand. Nothing downstream reconstructs ``ok`` from this string; the
        loop calls :meth:`invoke`.

        Args:
            arguments: JSON-object arguments from the LLM.

        Returns:
            Text appended to the run context (success or error).
        """
        return self.invoke(arguments).text

    @abstractmethod
    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        """Do the actual work.

        Subclasses may raise freely: :meth:`execute` converts exceptions to
        observations. They should return ``ToolResult(..., ok=False)``
        directly only for failures that are not naturally exceptions.

        Args:
            arguments: JSON-object arguments from the LLM.

        Returns:
            The observation to hand back to the loop.
        """

    # -- Shared argument helpers -------------------------------------------
    # These exist so every tool validates arguments the same way. A model
    # that sends ``{"path": 5}`` should get one consistent message whichever
    # tool it called.

    def _string_argument(self, arguments: dict[str, Any], key: str) -> str:
        """Return ``arguments[key]`` as a string.

        Args:
            arguments: Decoded tool arguments.
            key: Required key name.

        Returns:
            The argument value.

        Raises:
            KeyError: If the key is absent.
            TypeError: If the value is not a string.
        """
        if key not in arguments:
            raise KeyError(f"missing required argument {key!r}")
        value = arguments[key]
        if not isinstance(value, str):
            raise TypeError(
                f"argument {key!r} must be a string, got {type(value).__name__}"
            )
        return value

    def _resolve(self, arguments: dict[str, Any], key: str = "path") -> Path:
        """Resolve a path argument inside the workspace.

        Every tool that touches the file system goes through here, so the
        containment check cannot be skipped by accident.

        Args:
            arguments: Decoded tool arguments.
            key: Which argument holds the path.

        Returns:
            Absolute path inside the workspace.

        Raises:
            KeyError, TypeError: Bad argument (see :meth:`_string_argument`).
            PathEscapeError: The path leaves the workspace.
        """
        return resolve_in_workspace(
            self.workspace_root, self._string_argument(arguments, key)
        )
