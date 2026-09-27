"""Wrap tools discovered on an MCP server as harness ``Tool`` objects.

Everything a *remote* tool definition needs before it is fit to register
happens here: the deny-list, the server-name prefix, and the normalisation
of names, descriptions and schemas.

Normalisation is the part that is easy to underestimate. Up to now every
tool definition in this codebase was written by us and reviewed by a human.
A remote definition is written by someone else and arrives at runtime, so it
can be nameless, undescribed, wrongly cased, three paragraphs long, or share
a description with a tool from another server. ``tests/unit/test_tool_contract.py``
already encodes what a usable definition looks like — snake_case names,
bounded and distinct descriptions, every schema property typed and described
— and it runs over the production registry, so a remote tool has to meet the
same bar as a built-in one. Rather than exempt remote tools, the adapter
repairs their metadata on the way in (ADR-043).

The deny-list is the safety half. ``ADR-008`` says the agent cannot delete
anything, and up to now that was true by construction because no delete
class was ever written. A server hands us its tool list at *runtime*, so the
guarantee has to be re-established at registration time (ADR-042).

Refusal reads two signals, in this order. A tool the server *annotates*
``destructive_hint: true`` is refused on its own word, whatever it is
called; only then does the name word-list apply. Reading the protocol's own
field first is the difference between a guarantee and a spelling test — a
server offering ``sweep``, ``wipe`` or ``expire`` passes every word in
``DENIED_TOOL_WORDS`` while saying plainly in its annotations what it does
(ADR-059).
"""

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from harness.observability.errors import classify_exception, classify_tool_failure
from harness.tools.base import (
    DEFAULT_MAX_OUTPUT_CHARS,
    DENIED_TOOL_WORDS,
    Reach,
    RiskLevel,
    Tool,
    ToolResult,
)

# Separator between the server label and the remote tool name. Doubled
# underscore rather than a single one so the boundary is still visible when
# the remote name itself contains underscores, which most do.
NAME_SEPARATOR = "__"

# Names refused at registration. The list itself moved to
# ``harness.tools.base`` when the permission policy started refusing the same
# words at call time (ADR-057): one guarantee, one source of truth. It is
# re-exported here so ``harness.mcp`` keeps advertising it, since the deny
# rule is still an MCP concern at *this* boundary.

# ``Tool.__init__`` requires a filesystem root because every tool written so
# far has one: the workspace, the memory directory, or the skills folder. A
# remote tool has none — it sends a message to another process and never
# touches our disk. An unused sentinel is passed rather than making the root
# optional on the base class, which would be surgery on all eleven existing
# tools and their tests two days before a snapshot (ADR-040). The path is
# deliberately implausible, so if it is ever used by accident the traceback
# names the mistake.
_NO_LOCAL_ROOT = Path("/nonexistent/mcp-tools-have-no-local-root")

# Function-name rules the model's API expects, mirrored from the contract
# tests so a violation fails at wrap time rather than in a test run.
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_MAX_NAME_LENGTH = 64

# Description bounds from the contract tests: too short is uninformative,
# too long is re-sent on every single request. Only the maximum is
# enforced here — see _normalise_description for why the minimum needs no
# code — but both are asserted in tests/unit/test_mcp_adapter.py.
_MIN_DESCRIPTION = 15
_MAX_DESCRIPTION = 300

# A permissive schema for a server that sent something unusable. Accepting
# any object beats registering a broken schema, because the model would fail
# the call with no idea why.
_FALLBACK_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


class RemoteTool(Tool):
    """One tool living on an MCP server, callable like a built-in.

    The four contract attributes are set per instance rather than per class,
    because one class stands in for every remote tool: they differ per
    server and are only known at discovery time.

    Attributes:
        remote_name: The name as the *server* advertises it. Kept separate
            from :attr:`name`, which carries the server prefix — the server
            would not recognise the prefixed form.
        server_name: Which configured server this tool came from.
    """

    def __init__(
        self,
        name: str,
        remote_name: str,
        server_name: str,
        description: str,
        parameters: dict[str, Any],
        risk: RiskLevel,
        call: Callable[[str, dict[str, Any]], str],
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
        reach: Reach = "open_world",
    ) -> None:
        """Bind one remote tool definition to the client that can invoke it.

        Args:
            name: Prefixed, normalised name the model sees.
            remote_name: Unprefixed name to send back to the server.
            server_name: Configured server label.
            description: Normalised description.
            parameters: Normalised JSON Schema.
            risk: ``observe`` or ``mutate``, derived from the server's
                annotations where it supplied any.
            reach: ``local`` or ``open_world``, likewise from the
                annotations. Defaults to ``open_world`` — the opposite of
                :class:`Tool`'s default — because a remote tool is outside
                the sandbox by definition, and the safe assumption about
                one that did not say is that it reaches further still.
            call: Invokes ``(remote_name, arguments)`` and returns
                observation text. A plain callable rather than the client
                object, so this module does not import the client that
                imports it.
            max_output_chars: Per-observation character cap.
        """
        super().__init__(_NO_LOCAL_ROOT, max_output_chars)
        self.name = name
        self.remote_name = remote_name
        self.server_name = server_name
        self.description = description
        self.parameters = parameters
        self.risk = risk
        self.reach = reach
        self._call = call
        # Set by ``_run`` and read by ``_error_type_for`` one call later.
        # A tool instance handles one call at a time (the loop dispatches
        # sequentially), so a single slot is enough and a dict keyed by
        # call would only be somewhere for entries to leak.
        self._last_error_type: str | None = None

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        """Forward the call to the server and return what it said.

        No path resolution and no local validation: the server owns this
        tool's preconditions and reports its own errors.

        The failure mapping is done **here** rather than left to
        ``Tool.execute``. That shared handler catches the filesystem
        vocabulary the built-in tools raise — ``FileNotFoundError``,
        ``PermissionError``, ``ValueError`` and friends — and a remote
        failure is none of those: it arrives as a ``RuntimeError`` from the
        client (a timeout, a dead session, or a tool the server itself
        reported as failed). Widening the base class to catch that would
        change how every local tool behaves, so the remote-specific mapping
        stays in the remote-specific class.

        The result is the same observation shape either way, which is the
        point: the model cannot tell a local failure from a remote one and
        does not need to (ADR-006).
        """
        self._last_error_type = None
        try:
            return ToolResult(self._call(self.remote_name, arguments))
        except (RuntimeError, OSError) as exc:
            # RuntimeError covers McpConnectionError, which subclasses it;
            # OSError covers a transport that died mid-call. Both become an
            # observation the model can read and act on, never a crash that
            # ends a run the workspace may already have been changed by.
            #
            # The exception is also classified before it is discarded. One
            # step further down it is a string starting with "web MCP
            # server:", and telling a dead subprocess from a tool that
            # reported its own failure would mean guessing from wording —
            # which is how "which MCP server is responsible" became a
            # question the trace could not answer (ADR-094).
            self._last_error_type = classify_exception(exc)
            return ToolResult(f"{self.server_name} MCP server: {exc}", ok=False)

    def _error_type_for(self, result: ToolResult) -> str:
        """Return the error type this call's exception was classified as.

        Overrides the base class's text-reading fallback, because this class
        caught the exception itself and therefore knows the answer rather
        than having to infer it. Falls back to the remote-flavoured text
        classifier only for a failed result that came back *without* an
        exception — a server that returned an error payload rather than
        raising.

        Args:
            result: The failed result from :meth:`_run`.

        Returns:
            An entry from
            :data:`~harness.observability.errors.ERROR_TYPES`.
        """
        return self._last_error_type or classify_tool_failure(result.content, mcp=True)


def wrap_remote_tools(
    server_name: str,
    remote_tools: list[Any],
    call: Callable[[str, dict[str, Any]], str],
    denied_tools: frozenset[str] = frozenset(),
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
) -> list[Tool]:
    """Turn one server's advertised tools into registrable harness tools.

    Args:
        server_name: Configured label, used as the name prefix.
        remote_tools: ``mcp.Tool`` objects from ``list_tools``.
        call: Invokes a remote tool by its unprefixed name.
        denied_tools: Extra names refused for this server, on top of
            :data:`DENIED_TOOL_WORDS`.
        max_output_chars: Per-observation character cap.

    Returns:
        Wrapped tools in the order the server listed them, minus any that
        were denied or unusable.
    """
    prefix = _normalise_identifier(server_name)
    wrapped: list[Tool] = []
    # Descriptions must be distinct across the whole registry. Tracking what
    # this server has already used covers same-server collisions; the server
    # label baked into every description covers cross-server ones.
    taken_descriptions: set[str] = set()

    for remote in remote_tools:
        remote_name = str(getattr(remote, "name", "") or "")
        if not remote_name or refusal_reason(remote, denied_tools) is not None:
            continue

        name = _prefixed_name(prefix, remote_name)
        if name is None:
            # A name with nothing usable left after normalisation (all
            # punctuation, say) is skipped rather than guessed at.
            continue

        description = _normalise_description(
            raw=str(getattr(remote, "description", "") or ""),
            server_name=server_name,
            remote_name=remote_name,
            taken=taken_descriptions,
        )
        taken_descriptions.add(description)

        wrapped.append(
            RemoteTool(
                name=name,
                remote_name=remote_name,
                server_name=server_name,
                description=description,
                parameters=_normalise_schema(getattr(remote, "input_schema", None)),
                risk=_risk_of(remote),
                reach=_reach_of(remote),
                call=call,
                max_output_chars=max_output_chars,
            )
        )
    return wrapped


def denied_names(
    remote_tools: list[Any],
    denied_tools: frozenset[str] = frozenset(),
) -> list[str]:
    """Return the advertised names this harness refuses to register.

    Computed separately from :func:`wrap_remote_tools` so the CLI can *name*
    what was blocked at startup. A tool that silently is not there is the
    failure mode that costs an afternoon of debugging, and "the server
    offered it, we refused it" is exactly the sentence a user needs.

    Args:
        remote_tools: ``mcp.Tool`` objects from ``list_tools``.
        denied_tools: Extra exact names configured for this server.

    Returns:
        Refused names, in the order the server listed them.
    """
    return [name for name, _ in denied_entries(remote_tools, denied_tools)]


def denied_entries(
    remote_tools: list[Any],
    denied_tools: frozenset[str] = frozenset(),
) -> list[tuple[str, str]]:
    """Return each refused tool with the reason it was refused.

    Two reasons exist and the difference matters to whoever reads the
    startup warning. ``"annotated destructive"`` means the server said so
    itself, which is a correctly behaved server being correctly refused.
    ``"denied name"`` means we matched a word, which — on a tool that is
    *not* actually destructive — is a false positive the operator may want
    to argue with. Collapsing both into "refused" would leave them unable to
    tell those apart (ADR-059).

    Args:
        remote_tools: ``mcp.Tool`` objects from ``list_tools``.
        denied_tools: Extra exact names configured for this server.

    Returns:
        ``(name, reason)`` pairs in the order the server listed them.
    """
    entries: list[tuple[str, str]] = []
    for tool in remote_tools:
        name = str(getattr(tool, "name", "") or "")
        if not name:
            continue
        reason = refusal_reason(tool, denied_tools)
        if reason is not None:
            entries.append((name, reason))
    return entries


def refusal_reason(
    remote: Any,
    denied_tools: frozenset[str] = frozenset(),
) -> str | None:
    """Return why this remote tool must not be registered, or ``None``.

    The annotation is checked before the name. A server that declares
    ``destructive_hint: true`` has told us what the tool does in the
    protocol's own vocabulary, and believing it is both more accurate than
    a word list and the reason the field exists.

    One case is deliberately *not* refused: a tool carrying no annotations
    at all. The MCP specification's stated default for ``destructiveHint``
    is true, so reading "absent" as "destructive" would refuse nearly every
    tool on nearly every server, including the ones this repository ships.
    An unannotated tool is instead registered as ``mutate`` (:func:`_risk_of`)
    and left to the permission policy, which is the right place for "we do
    not know" (ADR-059).

    Args:
        remote: The ``mcp.Tool`` object as advertised.
        denied_tools: Extra exact names configured for this server.

    Returns:
        A short reason, or ``None`` when the tool may be registered.
    """
    if is_destructive(remote):
        return "annotated destructive"
    name = str(getattr(remote, "name", "") or "")
    if name and is_denied(name, denied_tools):
        return "denied name"
    return None


def is_destructive(remote: Any) -> bool:
    """Return whether the server annotated this tool as destructive.

    Only an explicit ``True`` counts, and only on a tool that is not also
    marked read-only: the specification says ``destructiveHint`` is
    meaningful only when ``readOnlyHint`` is false, so a server sending both
    has contradicted itself and the safer, more specific claim — that it
    only reads — is the one that survives.

    Args:
        remote: The ``mcp.Tool`` object.

    Returns:
        ``True`` when the tool declares itself destructive.
    """
    annotations = getattr(remote, "annotations", None)
    if annotations is None:
        return False
    if getattr(annotations, "read_only_hint", None) is True:
        return False
    return getattr(annotations, "destructive_hint", None) is True


def is_denied(remote_name: str, denied_tools: frozenset[str] = frozenset()) -> bool:
    """Return whether a remote tool name must be refused at registration.

    Substring matching, not equality: a server calling its tool
    ``fs_delete_path`` is offering deletion just as plainly as one calling it
    ``delete``, and the guarantee this protects is a hard constraint of the
    brief rather than a naming convention.

    Args:
        remote_name: Name as advertised by the server.
        denied_tools: Extra exact names configured for this server.

    Returns:
        ``True`` if the tool must not be registered.
    """
    lowered = remote_name.lower()
    if lowered in {denied.lower() for denied in denied_tools}:
        return True
    return any(word in lowered for word in DENIED_TOOL_WORDS)


def _prefixed_name(prefix: str, remote_name: str) -> str | None:
    """Build the model-facing name, or ``None`` when nothing usable remains.

    Prefixing is not cosmetic. ``ToolRegistry.register`` raises on a
    duplicate name, so an unprefixed server advertising ``read_file`` would
    abort startup; and two servers offering the same name with different
    behaviour would be indistinguishable to the model. The prefix solves
    both, and it makes the CLI trace say where a tool came from.
    """
    remote = _normalise_identifier(remote_name)
    if not remote:
        return None
    name = f"{prefix}{NAME_SEPARATOR}{remote}" if prefix else remote
    # The function-calling API caps names; clip rather than reject, since a
    # long name is the server's choice and not something the user can fix.
    name = name[:_MAX_NAME_LENGTH].rstrip("_")
    return name if _NAME_PATTERN.match(name) else None


def _normalise_identifier(raw: str) -> str:
    """Reduce arbitrary text to a snake_case identifier fragment.

    Args:
        raw: Server label or remote tool name.

    Returns:
        Lowercase ``[a-z0-9_]`` text with no leading digit or underscore, or
        an empty string when nothing usable remains.
    """
    lowered = re.sub(r"[^a-z0-9_]+", "_", raw.strip().lower())
    # Collapse runs so "get--time" does not become "get___time", which would
    # read as the server separator.
    collapsed = re.sub(r"_{2,}", "_", lowered).strip("_")
    # A name must start with a letter, so drop any leading digits.
    return re.sub(r"^[0-9_]+", "", collapsed)


def _normalise_description(
    raw: str,
    server_name: str,
    remote_name: str,
    taken: set[str],
) -> str:
    """Make a server's description satisfy the tool contract.

    The description is the entire basis on which the model picks one tool
    over another, so an empty or duplicated one is a correctness problem and
    not a style one.

    Every remote description ends with the server label. That is what keeps
    descriptions distinct across servers, and it is genuinely useful to the
    model besides: it says this tool is remote and which server owns it.

    Args:
        raw: Description as advertised, possibly empty.
        server_name: Configured server label.
        remote_name: Unprefixed remote name, used to break a tie.
        taken: Descriptions already used by this server, mutated by the
            caller after each tool.

    Returns:
        A description of 15 to 300 characters starting with a capital.
    """
    suffix = f" (via the {server_name} MCP server.)"
    body = " ".join(raw.split())
    if not body:
        body = f"Remote tool {remote_name}."
    # A sentence, because the contract requires an initial capital and a
    # lowercase description reads as a fragment beside the built-in tools.
    body = body[0].upper() + body[1:]

    # Clip the server's text, never the suffix: the suffix is what carries
    # the distinctness guarantee.
    room = _MAX_DESCRIPTION - len(suffix)
    if len(body) > room:
        body = body[: room - 1].rstrip() + "…"

    description = f"{body}{suffix}"
    if description in taken:
        # Two tools on one server sharing a description. The remote name is
        # unique per server, so naming it breaks the tie.
        marker = f" Tool: {remote_name}."
        room = _MAX_DESCRIPTION - len(suffix) - len(marker)
        description = f"{body[:room].rstrip()}{marker}{suffix}"

    # No minimum-length branch here on purpose. The suffix alone is longer
    # than _MIN_DESCRIPTION for any server name, and ``body`` is never empty
    # by this point, so a too-short description is unreachable. A guard for
    # it would be unreachable code pretending to be a safeguard, which is
    # worse than not having one — the assertion below is where the bound is
    # actually enforced, in the tests.
    return description[:_MAX_DESCRIPTION]


def _normalise_schema(raw: Any) -> dict[str, Any]:
    """Repair a remote input schema into one the contract accepts.

    Servers routinely omit per-property descriptions — the SDK generates
    schemas from Python signatures and emits ``title`` instead — and the
    contract requires every property to be both typed and described, because
    an undocumented argument is one the model fills in by guessing.

    Args:
        raw: The server's ``input_schema``, or ``None``.

    Returns:
        An object schema with typed, described properties and a ``required``
        list that mentions only properties that exist.
    """
    if not isinstance(raw, dict) or raw.get("type") != "object":
        return dict(_FALLBACK_SCHEMA)

    properties = raw.get("properties")
    if not isinstance(properties, dict):
        return dict(_FALLBACK_SCHEMA)

    repaired: dict[str, Any] = {}
    for prop_name, spec in properties.items():
        # A property whose spec is not an object cannot be repaired in place,
        # so it becomes a described string rather than being dropped.
        detail = dict(spec) if isinstance(spec, dict) else {}
        if "type" not in detail:
            # No type at all: a string round-trips through JSON for anything
            # the model is likely to send.
            detail["type"] = "string"
        if not detail.get("description"):
            # ``title`` is what the SDK generates in place of a description.
            detail["description"] = str(
                detail.get("title") or f"The {prop_name} argument."
            )
        repaired[prop_name] = detail

    schema: dict[str, Any] = {"type": "object", "properties": repaired}
    required = raw.get("required")
    if isinstance(required, list):
        # Drop required names with no matching property: the contract checks
        # this, and the model would otherwise be told to send an argument the
        # schema does not describe.
        kept = [name for name in required if name in repaired]
        if kept:
            schema["required"] = kept
    return schema


def _risk_of(remote: Any) -> RiskLevel:
    """Classify a remote tool as ``observe`` or ``mutate``.

    MCP has no field that means what our ``risk`` means, but it does have
    optional annotations, and ``read_only_hint`` is close enough to be worth
    honouring. Anything else — including a server that supplied no
    annotations at all — is treated as ``mutate``, because the value feeds
    Week 3's permission policy and an unknown tool should not be filed under
    the safer label by default.

    Args:
        remote: The ``mcp.Tool`` object.

    Returns:
        The risk level for the wrapped tool.
    """
    annotations = getattr(remote, "annotations", None)
    if annotations is not None and getattr(annotations, "read_only_hint", None) is True:
        return "observe"
    return "mutate"


def _reach_of(remote: Any) -> Reach:
    """Classify a remote tool as ``local`` or ``open_world``.

    ``open_world_hint`` is the annotation that says what ``read_only_hint``
    cannot: whether the tool talks to anything beyond the server's own
    state. The specification's default for it is *true*, and that default
    is honoured here — a tool that did not say, or a server that sent no
    annotations at all, is ``open_world``. This is the direction the web
    server's own docstring asked for when it noted that ``web_search`` had
    registered under the same label as ``read_file`` (ADR-055, ADR-102).

    Only an explicit ``False`` earns ``local``. The calendar and time
    servers set it, so their queries stay unprompted; a server that wants
    the same treatment says so in its annotations or gets it from a rule in
    ``config.yaml``.

    Args:
        remote: The ``mcp.Tool`` object.

    Returns:
        The reach for the wrapped tool.
    """
    annotations = getattr(remote, "annotations", None)
    if (
        annotations is not None
        and getattr(annotations, "open_world_hint", None) is False
    ):
        return "local"
    return "open_world"
