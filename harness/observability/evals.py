"""Three kinds of success, and somewhere to put the third one.

A run of this harness can succeed in three different senses, and conflating
any two of them produces a dashboard that lies:

1. **System success.** Every request returned, every tool ran, the MCP
   servers stayed up. Visible as spans with no ``error`` status.
2. **Agent success.** The loop reached a final answer rather than a guard.
   Visible as ``harness.exit_reason == "success"`` on the run span.
3. **Task success.** The answer was *right*. Not visible anywhere, because
   nothing in the harness knows what the user wanted.

The first two are already measured, and they are the ones that mislead.
Every HTTP request can return 200 and every tool can succeed while the agent
writes the wrong file, which is precisely the case the brief means by "do
not assume an HTTP 200 response means the agent succeeded". So this module
does two things (ADR-096):

* **Derives signals** from a finished trace — the mechanical facts that
  correlate with a bad run: repeated identical calls, retry storms, a tool
  that failed three times, a truncated final answer, a run that reported
  success while carrying errors. None of these *is* task success. All of
  them are cheap, honest, and computable from evidence the harness already
  has.
* **Stores a verdict** against a ``run_id``, so an evaluation performed
  later — by a person, by a test fixture asserting on a known task, by a
  future scoring job — attaches to the exact trace that produced it. That
  association is the architectural requirement; the source of the verdict is
  deliberately left open.

**There is no LLM judge here, and that is a decision rather than an
omission.** A model scoring its own harness's output would add a second
inference call per run, its own failure modes, and its own cost, in exchange
for a number nobody could check. The brief says not to invent one unless it
is actually useful, and for a lab harness with a fixed set of demonstration
tasks it is not: the end-to-end tests already know the right answer for the
tasks they run, so they are a better judge than a model would be, and
:meth:`QualityStore.record` is how they say so. The seam is built so that a
judge *could* be added later — it would be one more writer of
:class:`RunQuality` — without anything else changing.

**Signals are warnings, never verdicts.** :func:`derive_signals` never sets
``task_success``. A run can trip every heuristic here and still be correct,
and a run can trip none and be completely wrong. Reporting a heuristic as
ground truth is how an evaluation system becomes worse than no evaluation
system.
"""

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from harness.observability.errors import (
    LAYER_MCP,
    LAYER_MODEL,
    LAYER_TOOL,
    layer_for,
)

# Where verdicts live, under the traces directory so a trace and its
# evaluation travel together: copy the volume off a dead container and you
# have both, or neither. A subdirectory rather than a suffix so `ls traces/`
# still lists runs.
QUALITY_DIRNAME = "quality"

# --- thresholds -------------------------------------------------------------
# Every one of these is a judgement call, so each is named, defaulted here,
# and overridable. A magic number buried in an `if` is a threshold nobody can
# tune and nobody can defend in a review.

# Identical calls in a row before it is worth mentioning. Below the loop's
# own `max_repeated_calls` guard (10), because the point of a signal is to
# fire while the run is still salvageable — a signal that only lights up
# once a guard has already stopped the run tells you nothing new (ADR-049).
THRASH_THRESHOLD = 3

# Retries across a whole run before the endpoint is the story. Three is one
# full budget of `max_retries`; more than that and the run spent longer
# waiting than thinking.
RETRY_STORM_THRESHOLD = 4

# Failures of one tool before it is called unreliable. Matches the loop's
# `max_tool_failures` default, so this fires exactly when the guard retires
# the tool and the two agree about what "broken" means (ADR-048).
UNRELIABLE_TOOL_THRESHOLD = 3

# Reasons a verdict can carry, as a bounded vocabulary — it becomes a
# Prometheus label. `unspecified` exists so a verdict is never blocked on
# choosing a reason.
REASON_UNSPECIFIED = "unspecified"
REASON_WRONG_OUTPUT = "wrong_output"
REASON_INCOMPLETE = "incomplete"
REASON_TOOL_FAILURE = "tool_failure"
REASON_PERMISSION = "permission"
REASON_INFRASTRUCTURE = "infrastructure"
REASON_TIMEOUT = "timeout"
REASON_GUARD = "guard_stopped"

FAILURE_REASONS = frozenset(
    {
        REASON_UNSPECIFIED,
        REASON_WRONG_OUTPUT,
        REASON_INCOMPLETE,
        REASON_TOOL_FAILURE,
        REASON_PERMISSION,
        REASON_INFRASTRUCTURE,
        REASON_TIMEOUT,
        REASON_GUARD,
    }
)


@dataclass
class RunSignals:
    """Mechanical facts about one finished run, computed from its spans.

    Every field is a count or a flag derived from evidence, with no
    interpretation applied. :attr:`warnings` is the closest this gets to a
    judgement, and even that is a list of things a person should look at
    rather than a score.

    Attributes:
        iterations: Reason-act cycles the run used.
        llm_calls: Model requests that returned.
        llm_errors: Model requests that failed.
        llm_retries: Transient failures the client retried through.
        tool_calls: Tool calls dispatched.
        tool_failures: Tool calls that failed, for any reason.
        repeated_calls: Calls made with byte-identical arguments to the one
            before them.
        permission_refusals: Calls the policy denied or a person declined.
        subagent_runs: Delegated child runs.
        memory_operations: Long-term memory reads and writes.
        prompt_tokens: Tokens spent on input across the whole run.
        completion_tokens: Tokens spent on output.
        compactions: Times the context was compacted to fit its budget.
        error_layers: Which layers produced failures, counted.
        worst_tool: The tool that failed most, and how often.
        exit_reason: How the loop said it ended.
        answered: Whether the final model call produced an answer rather
            than another tool request.
        finish_reason: The last model call's finish reason, when the
            endpoint supplied one.
        wall_seconds: The run span's duration.
        model_seconds: How much of it was spent waiting on the model.
        tool_seconds: How much was spent inside tools.
        warnings: Named heuristics that fired. See the module docstring:
            these are prompts to look, never verdicts.
    """

    iterations: int = 0
    llm_calls: int = 0
    llm_errors: int = 0
    llm_retries: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    repeated_calls: int = 0
    permission_refusals: int = 0
    subagent_runs: int = 0
    memory_operations: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    compactions: int = 0
    error_layers: dict[str, int] = field(default_factory=dict)
    worst_tool: tuple[str, int] | None = None
    exit_reason: str | None = None
    answered: bool = False
    finish_reason: str | None = None
    wall_seconds: float = 0.0
    model_seconds: float = 0.0
    tool_seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def system_ok(self) -> bool:
        """Whether the infrastructure held up.

        Three layers count as infrastructure: the model endpoint, the local
        tools, and the MCP servers. Each is something that was supposed to
        work and did not.

        Three deliberately do not:

        * **policy** — a denial is the permission layer *working*. Counting
          it would make a correctly-defended harness look broken, which is
          the same mistake ADR-047 avoids for guards.
        * **guard** — the iteration cap and the repetition stop are the
          harness doing its job (ADR-047, ADR-049).
        * **memory** — a missing Qdrant degrades a run rather than failing
          it, by explicit decision (ADR-069). Counting it would mean every
          run on a laptop with no database reported an infrastructure
          failure, and a signal that fires on every run is a signal nobody
          reads.

        The **agent** layer is excluded too, and that exclusion is the point
        of the whole property: a run where the model chose the wrong tool is
        a run where the infrastructure worked perfectly (ADR-096).
        """
        return not any(
            self.error_layers.get(layer)
            for layer in (LAYER_MODEL, LAYER_TOOL, LAYER_MCP)
        )

    @property
    def agent_ok(self) -> bool:
        """Whether the loop reached a final answer."""
        return self.exit_reason == "success"

    @property
    def overhead_seconds(self) -> float:
        """Time in the run that was neither the model nor a tool.

        The harness's own cost: permission checks, memory, compaction,
        serialisation, and this telemetry layer. Worth having as a number
        rather than an assumption — "observability is low overhead" is a
        claim, and this is the measurement that supports or refutes it.
        """
        return max(0.0, self.wall_seconds - self.model_seconds - self.tool_seconds)


@dataclass
class RunQuality:
    """A task-success verdict attached to one run.

    Attributes:
        run_id: The run this verdict is about. The join key to everything
            else — the trace file is named after it, and the spans carry it.
        task_success: Whether the run did what was asked. ``None`` means
            nobody has judged it, which is a third state and not a failure:
            most runs are never evaluated and reporting them as failed would
            be worse than reporting nothing.
        failure_reason: One of :data:`FAILURE_REASONS`, when it failed.
        source: Who decided — ``human``, ``test``, ``heuristic``, or the
            name of a future scorer. Recorded because a verdict's weight
            depends entirely on where it came from, and a store that mixed
            them without saying so would be unusable.
        note: Free text for a person. Never a metric label.
        recorded_at: Unix seconds.
        signals: The derived facts, kept alongside the verdict so a stored
            evaluation is self-contained — an analysis of a hundred verdicts
            does not have to re-read a hundred trace files.
    """

    run_id: str
    task_success: bool | None = None
    failure_reason: str = REASON_UNSPECIFIED
    source: str = "human"
    note: str = ""
    recorded_at: float = field(default_factory=time.time)
    signals: RunSignals | None = None

    @property
    def outcome(self) -> str:
        """The verdict as a bounded label: ``success``, ``failure``, ``unjudged``."""
        if self.task_success is None:
            return "unjudged"
        return "success" if self.task_success else "failure"

    def as_dict(self) -> dict[str, Any]:
        """Render for storage, with the signals inlined."""
        payload = asdict(self)
        payload["outcome"] = self.outcome
        return payload


def derive_signals(spans: list[dict[str, Any]]) -> RunSignals:
    """Compute the mechanical facts about a run from its trace records.

    Takes the JSONL records rather than live :class:`~harness.observability.api.Span`
    objects, so the same function serves a run that just finished, a trace
    file copied off a dead container, and a test fixture built by hand. The
    trace is the interface.

    Only spans belonging to the *root* run and its descendants are counted,
    which is everything in the file: a child's spans share the parent's
    trace id by construction, so delegated work is included in the parent's
    totals. That is deliberate — a run that spent ninety seconds inside a
    sub-agent did spend ninety seconds.

    Args:
        spans: Records as written by
            :class:`~harness.observability.exporters.jsonl.JsonlSpanExporter`.

    Returns:
        The derived signals, including any warnings that fired.
    """
    signals = RunSignals()
    tool_failures: dict[str, int] = {}
    last_llm: dict[str, Any] | None = None
    root: dict[str, Any] | None = None

    for record in spans:
        name = str(record.get("span") or record.get("event") or "")
        attributes = record.get("attributes") or {}
        seconds = _seconds_of(record)
        failed = record.get("status") == "error" or record.get("ok") is False

        if name in ("agent.run", "run_end") and not record.get("parent_span_id"):
            root = record
        elif name == "subagent.run":
            signals.subagent_runs += 1
        elif name == "agent.iteration":
            signals.iterations += 1
            signals.compactions += _int(attributes.get("harness.context.compactions"))
        elif name in ("llm.call", "llm"):
            last_llm = record
            if failed:
                signals.llm_errors += 1
            else:
                signals.llm_calls += 1
                signals.model_seconds += seconds
            signals.llm_retries += _int(attributes.get("harness.llm.retries"))
            signals.prompt_tokens += _int(record.get("tokens_prompt"))
            signals.completion_tokens += _int(record.get("tokens_completion"))
        elif name in ("tool.call", "tool"):
            signals.tool_calls += 1
            tool = str(record.get("tool") or "unknown")
            # `delegate` is excluded from tool time because its duration is
            # the child run's whole duration, which is already counted as
            # that child's model and tool spans. Counting both would report
            # a research-then-write task's research twice (ADR-087).
            if tool != "delegate":
                signals.tool_seconds += seconds
            if failed:
                signals.tool_failures += 1
                tool_failures[tool] = tool_failures.get(tool, 0) + 1
            if _int(attributes.get("harness.tool.failure_streak")) > 1:
                signals.repeated_calls += 1
        elif name in ("permission.check", "permission"):
            if record.get("permission") in ("deny", "declined"):
                signals.permission_refusals += 1
        elif name.startswith("memory."):
            signals.memory_operations += 1

        error_type = record.get("error_type")
        if error_type:
            layer = record.get("error_layer") or layer_for(str(error_type))
            key = str(layer)
            signals.error_layers[key] = signals.error_layers.get(key, 0) + 1

    if root is not None:
        signals.exit_reason = root.get("exit_reason")
        signals.wall_seconds = _seconds_of(root)
        root_attributes = root.get("attributes") or {}
        signals.iterations = signals.iterations or _int(root.get("iteration"))
        signals.repeated_calls = max(
            signals.repeated_calls,
            _int(root_attributes.get("harness.count.repeated_calls")),
        )
    if last_llm is not None:
        last_attributes = last_llm.get("attributes") or {}
        signals.answered = bool(last_attributes.get("harness.llm.final_answer"))
        reason = last_attributes.get("gen_ai.response.finish_reasons")
        signals.finish_reason = str(reason) if reason else None
    if tool_failures:
        worst = max(tool_failures.items(), key=lambda item: item[1])
        signals.worst_tool = worst

    signals.warnings = _warnings(signals)
    return signals


def _warnings(signals: RunSignals) -> list[str]:
    """Name the heuristics that fired, in the order a reader should check.

    Each one is a *question*, not an answer. The names are short because
    they appear in a trace summary; what each means is here:

    ``silent_failure``
        The run reported success while carrying failures underneath it.
        The most valuable signal in the list, because it is the shape of a
        run that looks healthy on every aggregate and did the wrong thing.
    ``no_tools_used``
        The agent answered without acting. Sometimes correct — a question
        that needed no tool — and sometimes a model answering from memory a
        question that required looking (ADR-096).
    ``answer_truncated``
        The final response hit the token ceiling, so the answer stops
        mid-sentence. Indistinguishable from success on every metric.
    ``thrash``
        Identical calls repeated. The early warning for the no-progress
        guard.
    ``retry_storm``
        The endpoint made the run wait rather than think.
    ``unreliable_tool``
        One tool accounted for most of the failures.
    ``context_pressure``
        The window was compacted, so the model stopped being able to see
        what a tool told it earlier — an invisible cause of a wrong answer
        (ADR-114).
    ``permission_blocked``
        The policy, not the agent, is why work did not happen.
    """
    warnings: list[str] = []
    if signals.agent_ok and not signals.system_ok:
        warnings.append("silent_failure")
    if signals.agent_ok and signals.tool_calls == 0:
        warnings.append("no_tools_used")
    if signals.finish_reason == "length":
        warnings.append("answer_truncated")
    if signals.repeated_calls >= THRASH_THRESHOLD:
        warnings.append("thrash")
    if signals.llm_retries >= RETRY_STORM_THRESHOLD:
        warnings.append("retry_storm")
    if signals.worst_tool and signals.worst_tool[1] >= UNRELIABLE_TOOL_THRESHOLD:
        warnings.append("unreliable_tool")
    if signals.compactions:
        warnings.append("context_pressure")
    if signals.permission_refusals:
        warnings.append("permission_blocked")
    return warnings


def suggest_reason(signals: RunSignals) -> str:
    """Propose a failure reason from the signals, for a run that failed.

    A convenience for whoever is recording a verdict, not a verdict itself:
    it fills in the most likely value so a person pressing ``/eval fail``
    gets a useful label instead of ``unspecified``. They can always override
    it, and :func:`derive_signals` never calls this.

    Ordered by how conclusive the evidence is — an endpoint that never
    answered explains a failure completely, whereas a tool failure is a
    reason only if nothing more fundamental went wrong.

    Args:
        signals: Facts derived from the run's trace.

    Returns:
        One of :data:`FAILURE_REASONS`.
    """
    if signals.llm_errors:
        return REASON_INFRASTRUCTURE
    if signals.error_layers.get("mcp"):
        return REASON_INFRASTRUCTURE
    if signals.exit_reason in ("capped", "no_progress"):
        return REASON_GUARD
    if signals.permission_refusals and not signals.answered:
        return REASON_PERMISSION
    if signals.tool_failures:
        return REASON_TOOL_FAILURE
    if not signals.answered:
        return REASON_INCOMPLETE
    return REASON_WRONG_OUTPUT


class QualityStore:
    """Verdicts on disk, one JSON file per run, beside the traces.

    Deliberately not a database. A verdict is written once, read rarely, and
    has to survive a container being removed — which is exactly what the
    memory volume already does for traces. A file per run also means two
    processes evaluating two different runs cannot conflict, and that
    copying one run's evidence somewhere is ``cp`` of two files.
    """

    def __init__(self, traces_dir: Path) -> None:
        """Args:
        traces_dir: The directory trace files live in. Verdicts go in a
            ``quality`` subdirectory of it.
        """
        self._root = traces_dir / QUALITY_DIRNAME

    @property
    def root(self) -> Path:
        """Where verdicts are written."""
        return self._root

    def path_for(self, run_id: str) -> Path:
        """Return the file a run's verdict is (or would be) stored in."""
        return self._root / f"{run_id}.json"

    def record(self, quality: RunQuality) -> bool:
        """Store one verdict, overwriting any earlier one for that run.

        Overwriting rather than appending: a later judgement of the same run
        supersedes an earlier one, and a history of somebody changing their
        mind is not what this is for.

        Args:
            quality: The verdict.

        Returns:
            Whether it was written. ``False`` for a directory that cannot be
            created or a file that cannot be written — the same rule as the
            trace exporter, because an evaluation system that can fail a run
            is worse than one that loses a verdict (ADR-069).
        """
        if quality.failure_reason not in FAILURE_REASONS:
            quality.failure_reason = REASON_UNSPECIFIED
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            self.path_for(quality.run_id).write_text(
                json.dumps(quality.as_dict(), indent=2, default=str) + "\n",
                encoding="utf-8",
            )
        except OSError:
            return False
        return True

    def load(self, run_id: str) -> RunQuality | None:
        """Read one run's verdict, or ``None`` if it has never been judged."""
        path = self.path_for(run_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        signals_payload = payload.get("signals")
        return RunQuality(
            run_id=str(payload.get("run_id") or run_id),
            task_success=payload.get("task_success"),
            failure_reason=str(payload.get("failure_reason") or REASON_UNSPECIFIED),
            source=str(payload.get("source") or "human"),
            note=str(payload.get("note") or ""),
            recorded_at=float(payload.get("recorded_at") or 0.0),
            signals=RunSignals(**signals_payload) if signals_payload else None,
        )

    def all(self) -> list[RunQuality]:
        """Every stored verdict, newest first.

        Returns:
            The verdicts that could be read. A corrupt file is skipped
            rather than raised: a directory of a hundred verdicts should not
            be unreadable because one write was interrupted.
        """
        if not self._root.is_dir():
            return []
        loaded = [self.load(path.stem) for path in self._root.glob("*.json")]
        found = [item for item in loaded if item is not None]
        return sorted(found, key=lambda item: item.recorded_at, reverse=True)


def _int(value: Any) -> int:
    """Read an integer from a trace field, defaulting to zero."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _seconds_of(record: dict[str, Any]) -> float:
    """Return a span's duration in seconds, at the best resolution available.

    ``duration_ns`` when the record has it, ``duration_ms`` otherwise.
    Milliseconds are lossy at exactly the scale that matters: a permission
    check, a memory injection and a compaction are all sub-millisecond, so a
    reader that only had ``duration_ms`` reported every one of them as zero
    — and the time split of a fast run as "not recorded" (ADR-094).

    A v1 trace has only ``latency_ms``, which the upgrade step promotes.
    """
    nanoseconds = record.get("duration_ns")
    if isinstance(nanoseconds, int) and not isinstance(nanoseconds, bool):
        return nanoseconds / 1_000_000_000
    return float(record.get("duration_ms") or 0) / 1000.0
