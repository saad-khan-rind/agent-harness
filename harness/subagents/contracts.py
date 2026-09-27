"""The typed contracts a delegation is carried out under.

Delegation in this harness is not "the model writes a paragraph to another
model". It is a *runtime operation* with a declared input and a declared
output, and this module is where both are declared. Three types do the work:

* :class:`DelegationTask` — what the **parent** says. The task, the context
  it chose to hand over, and the constraints it wants honoured. This is the
  envelope, and the important word is *chose*: the child gets this and
  nothing else of the parent's conversation (ADR-084).
* :class:`RunMetadata` — what the **runtime** adds. Identifiers, role,
  depth, budget, deadline, capabilities, permission mode. The parent cannot
  write any of it, which is what stops a model talking itself into a wider
  child than the configuration allows (ADR-086).
* :class:`DelegationResult` — what comes **back**. A status, a condensed
  summary, and a *validated* payload — or a well-defined failure. An
  unvalidated child answer never enters the parent's context pretending to
  be a result (ADR-085).

Why pydantic rather than a dataclass: ``harness/config.py`` already validates
the harness's other external input with it, and the thing being validated
here has exactly the same shape of problem — text produced elsewhere that
must not be trusted until it has been checked against a schema. Using the
validation technology the project already has is cheaper than a second one,
and ``model_json_schema`` gives the child's prompt a field list that cannot
drift from the model the runtime validates against.

Nothing in this module imports the loop, the registry, or a tool. It is the
vocabulary; :mod:`harness.subagents.factory` is the machinery.
"""

import json
import re
from enum import StrEnum
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# How much of a child's raw answer is kept when it could not be validated.
# Enough for a person reading the trace to see what the model actually said,
# short enough that a failed delegation cannot flood the parent's window —
# which is the exact failure delegation exists to prevent.
INVALID_OUTPUT_PREVIEW_CHARS = 600


class DelegationStatus(StrEnum):
    """Lifecycle of one delegated child run.

    Named states rather than a boolean because the failures are genuinely
    different and the parent should be able to act on the difference: a
    ``REJECTED`` delegation never ran and is the parent's mistake to fix, a
    ``TIMED_OUT`` one ran out of budget and might succeed if narrowed, and an
    ``INVALID_OUTPUT`` one did the work but could not report it in the agreed
    shape. Collapsing those into "failed" would leave the coordinating model
    with no way to choose its next move (ADR-085).

    The three non-terminal states exist so a run that is cut off mid-flight —
    a killed process, an exception nobody caught — is distinguishable in the
    stored artifact from one that finished. A record that says ``RUNNING`` is
    a record of something that never came back.
    """

    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    INVALID_OUTPUT = "invalid_output"
    FAILED = "failed"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """Whether no further transition is expected from this state."""
        return self not in (DelegationStatus.CREATED, DelegationStatus.RUNNING)

    @property
    def is_success(self) -> bool:
        """Whether the parent may use the payload as a result."""
        return self is DelegationStatus.COMPLETED


class ExecutionBudget(BaseModel):
    """What one child run is allowed to spend.

    Every field is a *ceiling the runtime enforces*, not advice in a prompt.
    ``max_iterations`` is enforced by the loop itself, ``max_tool_calls`` and
    ``timeout_seconds`` by the budget policy the factory wraps around the
    child's permission policy — which is the one gate every tool call already
    passes through, so budget enforcement needed no new enforcement point
    (ADR-086).

    Attributes:
        max_iterations: Reason-act cycles. ``0`` means uncapped, which a
            child never gets: :meth:`narrowed_to` refuses to widen.
        max_tool_calls: Tool calls, counted across the whole child run.
            ``0`` disables the count.
        timeout_seconds: Wall-clock ceiling on the child's *actions*. It
            bounds what the child may start, not an in-flight model call —
            that is the LLM client's own request timeout. Stated plainly
            because a budget that claims to interrupt a network call it
            cannot interrupt is a budget that lies.
        max_output_chars: Ceiling on the summary handed back to the parent.
            The point of delegation is that the detail stays in the child.
    """

    model_config = ConfigDict(frozen=True)

    max_iterations: int = Field(default=8, ge=0)
    max_tool_calls: int = Field(default=20, ge=0)
    timeout_seconds: float = Field(default=120.0, ge=0)
    max_output_chars: int = Field(default=2000, ge=1)

    def narrowed_to(self, ceiling: "ExecutionBudget") -> "ExecutionBudget":
        """Return this budget, clamped so it cannot exceed ``ceiling``.

        The rule the whole budget design rests on: a child's allocation is
        never larger than what the parent has left. A role may ask for eight
        iterations and get three, and it is never the other way round.

        ``0`` means "no limit", so it loses to any real number on both sides
        — an unlimited ceiling does not lift a child's own cap, and an
        unlimited request does not escape a real ceiling.

        Args:
            ceiling: The enclosing allowance, usually the parent's remainder.

        Returns:
            A new budget no larger than either input.
        """
        return ExecutionBudget(
            max_iterations=_tighter(self.max_iterations, ceiling.max_iterations),
            max_tool_calls=_tighter(self.max_tool_calls, ceiling.max_tool_calls),
            timeout_seconds=_tighter_float(
                self.timeout_seconds, ceiling.timeout_seconds
            ),
            max_output_chars=min(self.max_output_chars, ceiling.max_output_chars),
        )


def _tighter(left: int, right: int) -> int:
    """Return the stricter of two integer limits, where ``0`` means none."""
    if left <= 0:
        return max(0, right)
    if right <= 0:
        return left
    return min(left, right)


def _tighter_float(left: float, right: float) -> float:
    """Return the stricter of two float limits, where ``0`` means none."""
    if left <= 0:
        return max(0.0, right)
    if right <= 0:
        return left
    return min(left, right)


class DelegationTask(BaseModel):
    """The envelope a parent fills in to delegate one unit of work.

    This is the *whole* of what the parent chooses to send. There is
    deliberately no field for "the conversation so far": if the child needs
    something the user said, the parent must restate it in
    :attr:`context`, and that act of restating is the isolation boundary
    made visible. A parent that forgets is a parent whose child asks for
    clarification, which is a much better failure than a child quietly
    inheriting an entire session's history (ADR-084).

    Attributes:
        role: Which registered specialist to run.
        task: What to do, written so it makes sense to someone who was not
            in the conversation. The one required piece of content.
        context: Facts, paths and prior findings the child needs. The
            parent's explicit hand-over; empty is allowed and means "this
            task is self-contained".
        constraints: Hard limits on how the work is done, in the parent's
            words (for example "do not touch files outside notes/"). Advisory
            to the model, unlike the capability and permission limits, which
            the runtime enforces regardless of what is written here.
        success_criteria: What "done" looks like. Optional, and worth
            filling in: a verification specialist with a vague criterion is
            the classic early-victory failure of multi-agent systems.
    """

    model_config = ConfigDict(frozen=True)

    role: str = Field(min_length=1)
    task: str = Field(min_length=1)
    context: str = ""
    constraints: tuple[str, ...] = ()
    success_criteria: str = ""


class RunMetadata(BaseModel):
    """What the runtime, not the model, decides about a child run.

    Kept in its own type rather than mixed into :class:`DelegationTask` so
    that the boundary is structural: a model fills in a ``DelegationTask``,
    and there is no field on it whose name a model could guess to widen its
    child's permissions. Everything here is derived by the factory from the
    agent's registered specification and the parent's own limits.

    Attributes:
        agent_id: Registered specification the child runs under.
        role: Observability label for the child run. Equal to ``agent_id``
            for the shipped specialists, kept separate because a role is a
            *label on a time series* and an id is a lookup key, and the day
            two specifications share a label is the day the distinction
            stops being pedantic.
        child_run_id: Unique id of this child run.
        parent_run_id: The run that delegated it. Never empty — a child with
            no parent is not a child.
        depth: How many delegations deep this run is. The parent is ``0``.
        permission_mode: Effective mode, already narrowed against the
            parent's.
        capabilities: Capability names granted, already intersected with
            what the parent could offer.
        tools: Tool names actually registered for the child, after the
            capability filter met the tools that exist in this session.
        budget: Effective allowance, already clamped to the parent's.
        deadline_epoch: Wall-clock instant the budget expires at, or
            ``None`` when the timeout is disabled.
    """

    model_config = ConfigDict(frozen=True)

    agent_id: str
    role: str
    child_run_id: str
    parent_run_id: str
    depth: int = Field(ge=1)
    permission_mode: str
    capabilities: tuple[str, ...]
    tools: tuple[str, ...]
    budget: ExecutionBudget
    deadline_epoch: float | None = None


class DelegationEnvelope(BaseModel):
    """Everything a child is started with: the parent's ask plus the runtime's terms.

    The two halves stay separate all the way into the child's prompt, and
    that is the point of the type. What a person reading a trace needs to
    know is not only what the child was asked to do, but what it was
    *allowed* to do while doing it, and those two facts come from different
    authorities.

    Attributes:
        task: The parent's envelope.
        runtime: The runtime's terms.
    """

    model_config = ConfigDict(frozen=True)

    task: DelegationTask
    runtime: RunMetadata


# -- role-specific result payloads -------------------------------------------
#
# One model per specialist, because "structured output" means nothing if the
# structure is the same empty bag for every role. Each of these is the thing
# that role exists to produce, and the runtime refuses a child answer that
# is not one of them (ADR-085).


class Evidence(BaseModel):
    """One supported claim from the researcher.

    ``kind`` is the field that earns this type its place. A research
    sub-agent that returns a flat list of sentences leaves the parent unable
    to tell what was found from what was inferred, and a coordinator that
    cannot tell those apart will state a guess as a finding.

    Attributes:
        claim: The statement itself.
        source: Where it came from — a URL, a file path, or ``model`` when
            it is the child's own knowledge rather than something it looked
            up.
        kind: ``fact`` when a source supports it, ``assumption`` when the
            child is reasoning past its evidence.
    """

    claim: str
    source: str = "model"
    kind: Literal["fact", "assumption"] = "fact"


class ResearchResult(BaseModel):
    """What the ``researcher`` specialist returns.

    Attributes:
        answer: The condensed finding, in prose. This is what reaches the
            parent's context, so it is the field the role's prompt tells the
            child to keep short.
        evidence: Supporting claims, facts and assumptions marked apart.
        uncertainties: What the child could not establish. Required to be
            present — an empty list is an assertion of confidence, not an
            omission — because a researcher that reports no uncertainty is
            usually one that did not look for any.
        confidence: The child's own reading of how solid the answer is.
    """

    answer: str
    evidence: list[Evidence] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "medium"


class WriteResult(BaseModel):
    """What the ``writer`` specialist returns.

    Attributes:
        summary: What was written, in one or two sentences.
        files_written: Workspace-relative paths the child created or
            changed. The parent needs the paths, not the contents — the
            contents are on disk, which is the artifact plane.
        verified: Whether the child read the file back after writing it.
            Its own claim, and treated as one: the integration test checks
            the file, not this flag.
        notes: Anything the parent should know, including what was refused.
    """

    summary: str
    files_written: list[str] = Field(default_factory=list)
    verified: bool = False
    notes: str = ""


class Finding(BaseModel):
    """One issue raised by the ``critic`` specialist.

    Attributes:
        issue: What is wrong.
        severity: How much it matters.
        where: File, section or line the issue is in, when the critic can
            point at one.
        fix: The suggested correction, if there is an obvious one.
    """

    issue: str
    severity: Literal["high", "medium", "low"] = "medium"
    where: str = ""
    fix: str = ""


class ReviewResult(BaseModel):
    """What the ``critic`` specialist returns.

    ``verdict`` is a closed set on purpose. A reviewer asked for "feedback"
    returns prose that the coordinating model then has to interpret, and
    interpreting a review is exactly the step where an agent talks itself
    into shipping something. A verdict is actionable without interpretation.

    Attributes:
        verdict: ``approve``, ``revise`` or ``reject``.
        findings: Issues, most important first.
        strengths: What is already right, so a revision does not break it.
        checked: What the critic actually looked at. The countermeasure to
            the early-victory failure: a reviewer that approves after
            reading one paragraph has to say so here.
    """

    verdict: Literal["approve", "revise", "reject"]
    findings: list[Finding] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    checked: list[str] = Field(default_factory=list)


class DelegationResult(BaseModel):
    """The outcome of one delegation, as the runtime records and returns it.

    This is the type that crosses back over the isolation boundary, and the
    reason the boundary holds. The child's messages, its tool observations
    and its intermediate reasoning are not in it; what is in it is a status,
    a short summary, a validated payload and the accounting.

    Attributes:
        status: Terminal lifecycle state.
        agent_id: Which specification ran.
        role: Its observability label.
        child_run_id: The child's run id — the same id the trace file and
            the stored artifact use, which is what makes a line in the
            parent's context traceable to a full record.
        parent_run_id: The run that delegated it.
        summary: Condensed text for the parent, already clipped to the
            budget's ``max_output_chars``.
        payload: The validated structured result as plain data, or ``None``
            when the run did not produce one.
        error: Why it failed, when it failed. ``None`` on success.
        artifact_path: Where the full record was written, when it was.
        iterations: Reason-act cycles the child used.
        tool_calls: Tool calls the child made.
        prompt_tokens: Prompt tokens the child billed.
        completion_tokens: Completion tokens the child billed.
        duration_ms: Wall-clock duration of the child run.
        exit_reason: How the child's loop terminated, from the loop's own
            account of itself rather than inferred here.
        transitions: The lifecycle states this delegation passed through, in
            order. Carried on the result rather than only in a log so the
            behaviour is *testable*: "a rejected delegation never entered
            RUNNING" is an assertion about this tuple, and a lifecycle
            nobody can assert on is documentation, not a guarantee.
    """

    status: DelegationStatus
    agent_id: str
    role: str
    child_run_id: str
    parent_run_id: str
    summary: str = ""
    payload: dict[str, Any] | None = None
    error: str | None = None
    artifact_path: str | None = None
    iterations: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0
    exit_reason: str | None = None
    transitions: tuple[DelegationStatus, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether the parent may treat this as a usable result."""
        return self.status.is_success

    def as_observation(self) -> str:
        """Render the text the parent's context actually receives.

        Deliberately a rendering rather than a JSON dump. The parent is a
        language model reading its own conversation, and a wall of JSON is
        both harder for it to use and larger than the prose it encodes. The
        JSON stays available — the payload is on this object and the whole
        record is in the artifact file — but what enters the window is the
        condensed form, which is the entire economic argument for delegation
        (ADR-084).

        Returns:
            One block of text: the outcome line and the summary.

        No path to the stored record. The record is under the memory root,
        which the parent's file tools are confined away from on purpose
        (ADR-035), so naming it would only invite a call that cannot
        succeed. The record is for people and ``/trace``, which find it by
        the child run id in the first line.
        """
        header = f"[delegation {self.status.value}] {self.role} ({self.child_run_id})"
        lines = [header]
        if self.error:
            lines.append(self.error)
        if self.summary:
            lines.append(self.summary)
        return "\n".join(lines)


class BatchResult(BaseModel):
    """What one ``delegate`` call returns, however many children it ran.

    A batch is not a result with a status of its own, and modelling it as one
    would be the first step towards losing information the parent needs: five
    children can finish five different ways, and an aggregate "partly
    succeeded" tells the parent nothing it can act on. So this is a carrier —
    the children's own results, in the order they were asked for — and every
    question about how it went is answered by asking the individual results.

    Ordered by request rather than by completion on purpose. The parent wrote
    the list; reading the answers back in a different order because one child
    happened to be quicker would make the same call produce different text on
    different runs, which is a bad property for something a model reads.

    Attributes:
        results: One result per task the parent asked for, in that order.
    """

    results: tuple[DelegationResult, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether *every* child produced a usable result.

        All rather than any, because this is what the loop's failure guard
        reads (ADR-048, ADR-058). A batch where two of three children failed
        is a turn that mostly did not work, and reporting it as success would
        hide a repeated failure from the guard that exists to stop one.
        """
        return bool(self.results) and all(result.ok for result in self.results)

    def as_observation(self) -> str:
        """Render every child's block, under a header when there are several.

        The single-task case renders exactly as it did before this was a
        batch at all — no header, no numbering — because that is what the
        existing traces, tests and prompt all describe, and a change in the
        text a model reads is a change in behaviour.

        Returns:
            One block of text for the parent's context.
        """
        if len(self.results) == 1:
            return self.results[0].as_observation()
        if not self.results:
            # Defensive: the tool refuses an empty batch before it reaches
            # here, so this is a message for a caller that bypassed it.
            return "[delegation] no tasks were given, so nothing ran."

        succeeded = sum(1 for result in self.results if result.ok)
        lines = [
            f"[delegation batch] {len(self.results)} sub-agents ran "
            f"concurrently; {succeeded} returned a usable result."
        ]
        for index, result in enumerate(self.results, start=1):
            # Numbered against the order the parent asked in, so it can match
            # an answer to the task it wrote without re-reading the roles.
            lines.append(f"\n--- {index} of {len(self.results)} ---")
            lines.append(result.as_observation())
        return "\n".join(lines)


# -- validating what a child said --------------------------------------------

# Matches a fenced JSON block, with or without the ``json`` language tag.
# Models wrap structured output in a fence far more often than not, and the
# fence is a stronger signal than brace-matching because it is what the
# prompt asked for.
_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Return the structured object a child answer carries, if it carries one.

    Two strategies, in order of how much they trust the model:

    1. The last fenced block. A model that followed the instruction produced
       one, and the *last* one is taken because a model that shows its
       working sometimes emits an illustrative object before the real one.
    2. The last balanced ``{...}`` span. This is the fallback for a model
       that emitted bare JSON, and it is scanned from the right so trailing
       prose after the object does not defeat it.

    Both can fail, and failing is a supported outcome: the caller turns it
    into :attr:`DelegationStatus.INVALID_OUTPUT` rather than guessing.

    Args:
        text: The child's final answer.

    Returns:
        The decoded object, or ``None`` when there is no valid one.
    """
    for candidate in reversed(_FENCE.findall(text)):
        decoded = _decode_object(candidate)
        if decoded is not None:
            return decoded
    return _last_balanced_object(text)


def _decode_object(candidate: str) -> dict[str, Any] | None:
    """Decode ``candidate`` if it is a JSON object, else return ``None``."""
    try:
        value = json.loads(candidate)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _last_balanced_object(text: str) -> dict[str, Any] | None:
    """Return the last *outermost* balanced ``{...}`` span in the text.

    Two properties are load-bearing and both were learned from a failing
    test rather than reasoned out in advance:

    * **Outermost, not innermost.** Every one of these contracts nests — a
      research result contains evidence objects — so a scan that looks for
      the last ``{`` and matches forward finds the last piece of *evidence*
      and reports a result with no ``answer`` field. The scan therefore
      runs left to right and records only spans that open at depth zero.
    * **Strings are tracked.** A brace inside a quoted value is not a
      brace, and quoted braces appear the moment a child quotes a code
      snippet — which is exactly what a code-reading specialist does.

    Brace counting rather than a regular expression, because JSON nests and
    a regular expression cannot count.

    Args:
        text: The child's final answer.

    Returns:
        The decoded object, or ``None``.
    """
    spans: list[tuple[int, int]] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, character in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            if depth == 0:
                start = index
            depth += 1
        elif character == "}" and depth:
            depth -= 1
            if depth == 0:
                spans.append((start, index + 1))

    for opening, closing in reversed(spans):
        decoded = _decode_object(text[opening:closing])
        if decoded is not None:
            return decoded
    return None


def validate_payload(
    model: type[BaseModel], text: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Check a child's answer against its role's declared output model.

    The gate the requirement asks for: a result that does not match the
    contract must produce a defined status, not slide into the parent's
    context looking like a finding. Both halves of the return are needed —
    the payload when it validated, and a message the *child* can act on when
    it did not, because the caller may hand that message back for one repair
    attempt (ADR-085).

    Args:
        model: The role's result model.
        text: The child's final answer.

    Returns:
        ``(payload, None)`` when the answer validated, otherwise
        ``(None, reason)``.
    """
    raw = extract_json_object(text)
    if raw is None:
        return None, (
            "No JSON object was found in the answer. The result must end with "
            "a single JSON object matching the required fields."
        )
    try:
        validated = model.model_validate(raw)
    except ValidationError as exc:
        # The first few problems only. A model handed thirty validation
        # errors rewrites everything and usually breaks what was already
        # right; three is enough to fix the shape.
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or '(root)'}: "
            f"{error['msg']}"
            for error in exc.errors()[:3]
        )
        return None, f"The JSON object did not match the required shape: {problems}."
    return validated.model_dump(mode="json"), None


def render_payload(payload: dict[str, Any]) -> str:
    """Render a validated payload as the prose the parent's context receives.

    Generic over the shape rather than written once per role, and that is a
    deliberate trade. A per-role renderer would read slightly better and
    would be a fourth place to update when a contract changes — the model,
    the example, the prompt and the renderer — which is three places too
    many for a cosmetic gain.

    What it does is the small amount of structure that matters to a reader:
    scalars inline, lists as bullets, and a list of objects flattened to
    ``value — value`` so a piece of evidence or a finding stays on one line.
    Empty fields are dropped, because "uncertainties: []" tells the parent
    nothing it could act on and costs it tokens to read.

    Args:
        payload: The validated result, as plain data.

    Returns:
        A short block of text.
    """
    lines: list[str] = []
    for key, value in payload.items():
        label = key.replace("_", " ")
        if isinstance(value, list):
            if not value:
                continue
            lines.append(f"{label}:")
            lines.extend(f"  - {_render_item(item)}" for item in value)
        elif isinstance(value, bool):
            lines.append(f"{label}: {'yes' if value else 'no'}")
        elif value not in (None, ""):
            lines.append(f"{label}: {value}")
    return "\n".join(lines)


def _render_item(item: Any) -> str:
    """Render one list element for :func:`render_payload`."""
    if isinstance(item, dict):
        return " — ".join(
            str(value) for value in item.values() if value not in (None, "", [])
        )
    return str(item)


def describe_fields(model: type[BaseModel]) -> str:
    """Render a model's fields as the short list a child's prompt shows.

    Generated from the model rather than written out beside it, so the
    contract the child is told about cannot drift from the contract the
    runtime validates against — which is the failure mode that makes
    hand-written schema documentation worse than none.

    Args:
        model: The role's result model.

    Returns:
        One line per field: name, whether it is required, and its type.
    """
    lines = []
    for name, field in model.model_fields.items():
        required = "required" if field.is_required() else "optional"
        lines.append(f"  {name} ({required}, {_describe_type(field.annotation)})")
    return "\n".join(lines)


def _describe_type(annotation: Any) -> str:
    """Render one field's type the way a model can act on it.

    ``list`` and ``Literal`` — which is most of what these contracts are made
    of — carry all their useful information in their arguments, so the bare
    type name is the one rendering that tells a model nothing. A nested model
    is flattened to its own field names rather than its class name, for the
    same reason: ``list of {claim, source, kind}`` is actionable and
    ``list[Evidence]`` is a name only this codebase knows.

    Args:
        annotation: The field's annotation.

    Returns:
        A short human- and model-readable type description.
    """
    origin = get_origin(annotation)
    if origin is Literal:
        return "one of: " + ", ".join(repr(value) for value in get_args(annotation))
    if origin in (list, tuple):
        inner = get_args(annotation)
        return f"list of {_describe_type(inner[0])}" if inner else "list"
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return "{" + ", ".join(annotation.model_fields) + "}"
    return getattr(annotation, "__name__", None) or str(annotation)
