"""The one tool that starts a delegation.

Three ways of exposing delegation to a model were considered and the choice
is worth stating before the code, because it is the architectural decision
of this slice (ADR-084):

* **Agent as tool** — the parent calls ``delegate(...)`` and gets a result.
* **First-class child run** — the runtime creates a linked child run with
  its own identity, context, permissions, budget and lifecycle.
* **A workflow graph** — a fixed research → write → review pipeline.

The third was rejected because it decides at build time what the model
should decide at run time; a coordinator that cannot skip the researcher
when it already knows the answer is a pipeline wearing an agent's clothes.
The first two are not alternatives, and treating them as alternatives is the
usual mistake. This harness does **both**: the child *is* a first-class run
— :mod:`harness.subagents.factory` gives it an id, a parent, an isolated
context, its own policy and budget, its own lifecycle and its own place in
the trace — and the *model's* way of asking for one is a tool call, because
a tool call is the only thing a model can do in a ReAct loop.

The consequence that makes it worth doing this way: ``loop.py`` needs no
sub-agent branch. Delegation arrives at ``AgentLoop._execute`` as an
ordinary dispatch, passes the same permission gate as every other call
(ADR-057), is counted by the same metrics, and appears in the same trace.
A second control path would have had to re-earn every one of those.

``risk`` is ``mutate``, and that is not a formality. A writer child can
change the workspace, so in a ``confirm_mutations`` session the person at
the terminal is asked before the delegation starts — one prompt about the
whole hand-over, in addition to the prompts the child's own writes raise.
Declaring it ``observe`` because "the tool itself only calls a function"
would be exactly the kind of special case §8.4 asks us not to make.
"""

from pathlib import Path
from typing import Any, Literal

from harness.subagents.contracts import DelegationTask
from harness.subagents.factory import ConfigurableFactory
from harness.subagents.roles import DELEGATE_TOOL_NAME, Capability
from harness.tools.base import Tool, ToolResult

# Ceiling on each free-text argument before the envelope is even built. A
# parent that pastes half its context window into `context` has defeated the
# point of delegating, so the limit is enforced rather than requested: the
# text is clipped and the child is told it was clipped.
MAX_FIELD_CHARS = 4000


class DelegateTool(Tool):
    """Hand one task to a registered specialist and return its result.

    The schema is built from the catalog at construction time, so the roles
    the model is offered are the roles that exist. A hard-coded enum would
    be a second list to keep in step with the registrations, and the first
    one to go stale.
    """

    name = DELEGATE_TOOL_NAME
    risk = "mutate"

    def __init__(
        self,
        factory: ConfigurableFactory,
        workspace_root: Path,
        max_output_chars: int = 4000,
    ) -> None:
        """Args:
        factory: The delegation runtime. It holds the parent's context, so
            this tool needs no reference to the run it is called from —
            which is what let delegation work without changing
            ``Tool.invoke`` to take a context no other tool has ever
            needed.
        workspace_root: The sandbox root, as for every other tool. This
            one touches no file, but it is bound to the same root so the
            attribute is true and ``Tool._resolve`` stays correct if a
            future argument ever names a path.
        max_output_chars: Per-observation cap, applied like any other
            tool's. The result is already clipped to the child's own
            budget; this is the outer bound every observation has.
        """
        super().__init__(workspace_root, max_output_chars)
        self._factory = factory
        roles = factory.catalog.names()
        self._max_tasks = factory.max_concurrency
        fan_out = (
            ""
            if self._max_tasks < 2
            else (
                f"\n\nWhen a request has two or more INDEPENDENT parts - "
                f"several files, subjects or sections - put up to "
                f"{self._max_tasks} of them in 'tasks' in this one call "
                f"instead of filling in 'role' and 'task'. They run "
                f"concurrently, so a batch costs about as long as its slowest "
                f"member rather than the sum of all of them. Only batch work "
                f"where no task needs another's answer: they cannot see each "
                f"other, and a task that depends on a sibling's finding must "
                f"wait for a later call. A writer task always goes alone, "
                f"because the others would read files it is still changing."
            )
        )
        self.description = (
            "Hand self-contained sub-tasks to specialist sub-agents and get "
            "back a structured result from each. Use it on your own "
            "initiative whenever the work splits into independent parts or "
            "needs reading you will not need again - the user will not ask "
            "for it by name. Each sub-agent runs in its own context with its "
            "own tools and permissions and sees ONLY what you put in this "
            "call. Available roles:\n" + factory.catalog.describe() + fan_out
        )
        # One task's fields, reused for the flat single-task form and for
        # each entry of `tasks`, so the two shapes cannot drift apart.
        task_properties: dict[str, Any] = {
            "role": {
                "type": "string",
                "enum": roles,
                "description": "Which specialist to run.",
            },
            "task": {
                "type": "string",
                "description": (
                    "What the specialist must do, written so it makes "
                    "sense to someone who has not seen this "
                    "conversation."
                ),
            },
            "context": {
                "type": "string",
                "description": (
                    "Facts, paths and earlier findings the specialist "
                    "needs. It cannot see your conversation, so anything "
                    "missing here is missing for it."
                ),
            },
            "constraints": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Limits on how the work is done.",
            },
            "success_criteria": {
                "type": "string",
                "description": "What finished looks like.",
            },
        }
        properties = dict(task_properties)
        if self._max_tasks > 1:
            properties["tasks"] = {
                "type": "array",
                "maxItems": self._max_tasks,
                "items": {
                    "type": "object",
                    "properties": task_properties,
                    "required": ["role", "task"],
                },
                "description": (
                    "Several independent sub-tasks to run at the same time. "
                    "Use this INSTEAD of 'role' and 'task', not as well."
                ),
            }
        self.parameters: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            # Not `required` any more when batching is available, because a
            # batched call legitimately has neither. The check moved into
            # `_run`, where it can say which of the two shapes was expected
            # rather than failing schema validation with a bare field name.
            **({} if self._max_tasks > 1 else {"required": ["role", "task"]}),
        }

    def risk_for(self, arguments: dict[str, Any]) -> Literal["observe", "mutate"]:
        """``observe`` when every requested specialist is read-only by spec.

        A child's mode is ``min(session, specification)`` (ADR-086), so a
        researcher or a critic — both specified ``deny_mutations`` — cannot
        change anything whatever the session allows. Asking "run it?" before
        handing them a reading task protects nothing, and in the default
        interactive mode it put a prompt in front of every run-time fan-out
        (ADR-117). Anything this cannot resolve — an unknown role, a
        malformed ``tasks`` list, the writer — is ``mutate``, as before
        (ADR-119).

        Args:
            arguments: The call's decoded arguments, in either shape.

        Returns:
            ``observe`` or ``mutate``.
        """
        tasks = arguments.get("tasks")
        entries = tasks if isinstance(tasks, list) and tasks else [arguments]
        for entry in entries:
            if not isinstance(entry, dict):
                return "mutate"
            spec = self._factory.catalog.get(str(entry.get("role", "")))
            if spec is None or spec.permission_mode != "deny_mutations":
                return "mutate"
        return "observe"

    def _run(self, arguments: dict[str, Any]) -> ToolResult:
        """Validate the envelopes, run the delegation, return the summary.

        Args:
            arguments: The model's arguments, in either the single-task
                shape or the batched one.

        Returns:
            The rendered result. ``ok`` is false unless *every* child
            completed, so a failed delegation counts toward the loop's
            failure guard exactly like a failed tool call — which is what
            stops a model delegating the same impossible task until
            something else stops it (ADR-048).
        """
        tasks, error = self._envelopes(arguments)
        if error is not None:
            return ToolResult(error, ok=False)

        batch = self._factory.delegate_batch(tasks)
        # The observation carries the error text itself, so `ok=False` here
        # would prefix it with a second "Error:". The failure bit is what the
        # loop reads; the prefix is already in the message the factory wrote.
        return ToolResult(batch.as_observation(), ok=batch.ok)

    def _envelopes(
        self, arguments: dict[str, Any]
    ) -> tuple[list[DelegationTask], str | None]:
        """Turn the model's arguments into envelopes, or say what was wrong.

        Accepts either shape and refuses the ambiguous middle: a call that
        fills in both ``tasks`` and the flat fields has said two things, and
        guessing which one it meant would run work the model did not ask for.

        Args:
            arguments: The model's arguments.

        Returns:
            ``(tasks, None)`` when they are usable, or ``([], reason)``.
        """
        roles = ", ".join(self._factory.catalog.names())
        raw = arguments.get("tasks")
        # Read leniently: with batching available neither shape's fields are
        # schema-required, so an absent `role` is one of the two legitimate
        # calls rather than a malformed one.
        flat = _optional(arguments, "role")

        if raw is not None and not isinstance(raw, list):
            return [], "delegate's 'tasks' must be a list of sub-task objects."

        if isinstance(raw, list) and raw:
            if flat:
                return [], (
                    "delegate takes either 'tasks' or a single 'role' and "
                    "'task', not both. Send one shape or the other."
                )
            if len(raw) > self._max_tasks:
                return [], (
                    f"delegate runs at most {self._max_tasks} sub-agents at "
                    f"once and {len(raw)} were given. Send the most important "
                    f"{self._max_tasks} now and the rest in a later call."
                )
            tasks: list[DelegationTask] = []
            for position, item in enumerate(raw, start=1):
                if not isinstance(item, dict):
                    return [], (
                        f"entry {position} of 'tasks' is not an object with "
                        f"'role' and 'task'."
                    )
                built, reason = _envelope(item, roles, position)
                if built is None:
                    return [], reason or ""
                tasks.append(built)
            conflict = self._writes_alongside_others(tasks)
            if conflict is not None:
                return [], conflict
            return tasks, None

        built, reason = _envelope(arguments, roles, None)
        if built is None:
            return [], reason or ""
        return [built], None

    def _writes_alongside_others(self, tasks: list[DelegationTask]) -> str | None:
        """Refuse a batch where one task can change the workspace, or say why.

        Batched tasks run at the same time and must be independent. A task
        whose specialist holds ``FILE_WRITE`` is never independent of the
        others: they read the workspace while it is being changed. Observed
        on the real model: asked to "have the writer make summary.md, then
        have the critic check it", the coordinator batched the two, the
        critic looked before the file existed and rejected work it never
        saw. Decided on the capability rather than on role names, so a
        fourth specialist that writes is covered too (ADR-120).

        Args:
            tasks: The validated envelopes of one batched call.

        Returns:
            The refusal for the model, or ``None`` when the batch may run.
        """
        if len(tasks) < 2:
            return None
        for task in tasks:
            spec = self._factory.catalog.get(task.role)
            if spec is not None and Capability.FILE_WRITE in spec.capabilities:
                return (
                    f"the {task.role} changes the workspace, so it cannot run in "
                    f"the same batch as other tasks: they would read the "
                    f"workspace while it is being written. Delegate the "
                    f"{task.role} on its own first, and send the other tasks "
                    f"(a review of what it wrote, for example) in a later call "
                    f"once it has finished."
                )
        return None


def _envelope(
    source: dict[str, Any], roles: str, position: int | None
) -> tuple[DelegationTask | None, str | None]:
    """Build one envelope from one task object, or say why it cannot be built.

    Shared by both argument shapes so a batched entry is validated exactly
    as a single call is — the child cannot tell which shape it came from, so
    neither should the checks.

    Args:
        source: The object holding one task's fields.
        roles: The registered role names, for the refusal message.
        position: Which entry of ``tasks`` this is, or ``None`` for the flat
            single-task form. Only used to make the message locatable.

    Returns:
        ``(task, None)`` or ``(None, reason)``.
    """
    role = _optional(source, "role")
    task = _optional(source, "task")
    if not role or not task:
        where = "" if position is None else f"entry {position} of 'tasks': "
        return None, (
            f"{where}delegate needs a non-empty 'role' and 'task'. "
            f"Available roles: {roles}."
        )
    return (
        DelegationTask(
            role=role,
            task=_clip(task),
            context=_clip(_optional(source, "context")),
            constraints=tuple(_constraints(source)),
            success_criteria=_clip(_optional(source, "success_criteria")),
        ),
        None,
    )


def _optional(arguments: dict[str, Any], key: str) -> str:
    """Return a string argument that the model may have omitted."""
    value = arguments.get(key)
    return value.strip() if isinstance(value, str) else ""


def _constraints(arguments: dict[str, Any]) -> list[str]:
    """Return the constraints list, tolerating a model that sent a string.

    Models send a single string where an array was asked for often enough
    that refusing it would waste a turn on a difference the child cannot
    see. Anything else non-list is dropped rather than stringified, because
    a constraint the parent cannot read back is worse than none.

    Args:
        arguments: The model's arguments.

    Returns:
        Constraint lines, clipped.
    """
    value = arguments.get("constraints")
    if isinstance(value, str):
        return [_clip(value)] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [_clip(str(item)) for item in value if str(item).strip()]


def _clip(text: str) -> str:
    """Clip one envelope field, saying so when it clips."""
    if len(text) <= MAX_FIELD_CHARS:
        return text
    return text[:MAX_FIELD_CHARS] + "\n... [truncated by the delegation runtime]"
