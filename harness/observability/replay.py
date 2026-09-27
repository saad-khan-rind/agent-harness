"""Read one run's trace back as a span tree.

:mod:`harness.observability.exporters.jsonl` writes the record; this module
reads it. The questions it answers are the ones §9 of our brief calls root
cause analysis, and they are all *structural*: which of the five seconds was
the model, whether that failed ``write_file`` was the tool or a person
saying no, where a delegated child started and stopped, and — the hard one —
whether anything failed at all or the agent simply chose wrong.

**A tree, not a list.** The Week 3 reader had a flat event log and rebuilt
relationships by adjacency: a ``permission`` line was assumed to belong to
the ``tool`` line after it. That is a guess, and it stops being true the
first time a model requests two tool calls in one turn. Every record now
carries ``span_id`` and ``parent_span_id``, so the nesting is read rather
than inferred (ADR-093).

**Old files still replay.** A v1 trace has none of those fields, so
:func:`summarise` detects the schema and falls back to the adjacency
pairing the old reader used. That is not generosity to a hypothetical user:
the memory volume in the container we demo from has v1 traces on it, and a
reader that could not open them would have thrown away the evidence from
every run before this week (ADR-094).

**Nothing here imports the loop, a hook, or ``prometheus_client``.** A trace
file is the whole input, which is what lets ``python -m
harness.observability.replay`` run against a volume copied off a container
that is no longer there.

**No arguments, no observations.** The trace deliberately carries neither —
a ``write_file`` body would put workspace content on the memory volume — so
this reader can say *``read_file`` was called three times with the same
arguments* and not *which file*. The repetition is visible because the
fingerprint is recorded; the content is not, because it is not (ADR-094).
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from harness.observability.evals import QualityStore, RunSignals, derive_signals

# The tool whose latency is a child run's whole duration. Counted under its
# own heading in the time split rather than as tool time, or a
# research-then-write task would report its research twice: once as the
# child's model calls and once as the parent's ``delegate`` call.
DELEGATE_TOOL = "delegate"

# Permission outcomes that mean the tool did not run.
_REFUSED = frozenset({"deny", "declined"})

_SUFFIX = ".jsonl"

# How much of a run id to show. Eight hex characters is what ``git`` shows
# and it is enough to be unique in any directory of traces a person will
# ever list by hand; the full id is on every line of the file.
_SHORT_ID = 8

# Span names, mirrored rather than imported: this module reads files and
# must not acquire the rest of the package to do it.
_SPAN_RUN = "agent.run"
_SPAN_SUBRUN = "subagent.run"
_SPAN_ITERATION = "agent.iteration"
_SPAN_LLM = "llm.call"
_SPAN_TOOL = "tool.call"
_SPAN_PERMISSION = "permission.check"
_SPAN_FINALIZE = "agent.finalize"

# v1 event names, for a trace written before the span rewrite.
_V1_RUN_START = "run_start"
_V1_RUN_END = "run_end"
_V1_LLM = "llm"
_V1_TOOL = "tool"
_V1_PERMISSION = "permission"

# Wording for the ``exit_reason`` values the loop writes (ADR-084).
_EXIT_WORDS = {
    "success": "answered",
    "capped": "hit the iteration cap",
    "no_progress": "stopped: no progress",
    "error": "failed",
}

# One line per warning, so a reader does not have to know what a name means.
# Kept here rather than in evals.py because this is the module that renders
# for a person; evals.py only decides which ones fired.
_WARNING_WORDS = {
    "silent_failure": "reported success while carrying failures underneath",
    "no_tools_used": "answered without calling any tool",
    "answer_truncated": "final answer hit the token ceiling and was cut off",
    "thrash": "repeated identical calls",
    "retry_storm": "the endpoint was retried repeatedly",
    "unreliable_tool": "one tool accounted for most failures",
    "context_pressure": "the context was compacted mid-run",
    "permission_blocked": "the policy refused work the agent tried to do",
}

_INDENT = "  "


class TraceNotFoundError(LookupError):
    """No trace matched, or more than one did."""


@dataclass
class SpanNode:
    """One span and the spans inside it.

    Attributes:
        record: The raw JSONL record, so a renderer can reach any attribute
            without this class having to mirror the whole schema.
        children: Spans whose ``parent_span_id`` is this one's ``span_id``,
            in start order.
    """

    record: dict[str, Any]
    children: list["SpanNode"] = field(default_factory=list)

    @property
    def name(self) -> str:
        """The span name, falling back to the v1 event name."""
        return str(self.record.get("span") or self.record.get("event") or "?")

    @property
    def span_id(self) -> str | None:
        """This span's id, or ``None`` in a v1 trace."""
        return _optional_str(self.record.get("span_id"))

    @property
    def duration_ms(self) -> int:
        """How long it took, in whole milliseconds, for display."""
        return _int(self.record.get("duration_ms") or self.record.get("latency_ms"))

    @property
    def duration_us(self) -> int:
        """How long it took, in microseconds, for ordering and comparison.

        Milliseconds round a permission check or a memory injection to zero,
        which makes a "slowest operations" table a list of ties (ADR-094).
        """
        nanoseconds = self.record.get("duration_ns")
        if isinstance(nanoseconds, int) and not isinstance(nanoseconds, bool):
            return nanoseconds // 1000
        return self.duration_ms * 1000

    @property
    def failed(self) -> bool:
        """Whether it failed."""
        return self.record.get("status") == "error" or self.record.get("ok") is False

    @property
    def error_type(self) -> str | None:
        """The taxonomy entry for how it failed, if it did."""
        return _optional_str(self.record.get("error_type"))

    @property
    def attributes(self) -> dict[str, Any]:
        """The span's structured detail."""
        value = self.record.get("attributes")
        return value if isinstance(value, dict) else {}

    def walk(self) -> "list[SpanNode]":
        """This node and every node beneath it, depth first."""
        found = [self]
        for child in self.children:
            found.extend(child.walk())
        return found


@dataclass
class RunSummary:
    """One run, its spans, and the totals computed once.

    Attributes:
        run_id: The run's identifier.
        parent_run_id: The run that delegated it, or ``None``.
        role: ``main`` or a specialist's role.
        started: ISO timestamp of the run's first span.
        root: The run's own span, with its tree beneath it. ``None`` for a
            v1 trace, which has no spans to build one from.
        events: Every record belonging to this run, in start order. Kept
            because the v1 path has nothing else, and because a flat list is
            the right input for :func:`~harness.observability.evals.derive_signals`.
        children: Delegated runs, nested.
        signals: Derived facts about the run — see
            :mod:`harness.observability.evals`.
        quality: A stored task-success verdict, when one exists.
        schema_version: 1 for a Week 3 trace, 2 for a span trace.
    """

    run_id: str
    parent_run_id: str | None = None
    role: str = "main"
    started: str | None = None
    root: SpanNode | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    children: list["RunSummary"] = field(default_factory=list)
    signals: RunSignals = field(default_factory=RunSignals)
    quality: Any = None
    schema_version: int = 1

    @property
    def short_id(self) -> str:
        """The first eight characters of the run id."""
        return self.run_id[:_SHORT_ID]

    @property
    def exit_reason(self) -> str | None:
        """How the loop said the run ended."""
        return self.signals.exit_reason

    @property
    def ok(self) -> bool:
        """Whether the run reached a final answer."""
        return self.signals.agent_ok

    @property
    def duration_ms(self) -> int:
        """Wall-clock duration in milliseconds."""
        return int(self.signals.wall_seconds * 1000)

    @property
    def total_tokens(self) -> int:
        """Tokens spent, both directions."""
        return self.signals.prompt_tokens + self.signals.completion_tokens


# --- reading ----------------------------------------------------------------


def parse_lines(lines: list[str]) -> list[dict[str, Any]]:
    """Turn JSONL text into records, skipping anything unreadable.

    A torn last line is skipped rather than refused, because a run killed
    mid-write is exactly the run somebody wants to read.

    Args:
        lines: Raw lines from a trace file.

    Returns:
        The records that parsed, in file order.
    """
    records: list[dict[str, Any]] = []
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            record = json.loads(text)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def load_events(path: Path) -> list[dict[str, Any]]:
    """Read one trace file into records.

    Args:
        path: The ``.jsonl`` file.

    Returns:
        Parsed records in file order.

    Raises:
        OSError: If the file cannot be read.
    """
    return parse_lines(path.read_text(encoding="utf-8").splitlines())


def summarise(events: list[dict[str, Any]]) -> list[RunSummary]:
    """Group records into runs, nest them, and compute their totals.

    Args:
        events: Records from one trace file, which may contain a parent run
            and every run it delegated.

    Returns:
        Top-level runs, each with its children nested. Usually one.
    """
    if not events:
        return []

    by_run: dict[str, list[dict[str, Any]]] = {}
    for record in events:
        run_id = _optional_str(record.get("run_id")) or _optional_str(
            record.get("trace_id")
        )
        if run_id is None:
            continue
        by_run.setdefault(run_id, []).append(record)

    summaries: dict[str, RunSummary] = {}
    for run_id, records in by_run.items():
        ordered = sorted(records, key=_sort_key)
        version = _int(ordered[0].get("schema_version")) or 1
        summary = RunSummary(
            run_id=run_id,
            parent_run_id=_parent_of(ordered),
            role=_role_of(ordered),
            started=_optional_str(ordered[0].get("ts")),
            events=ordered,
            schema_version=version,
            signals=derive_signals(ordered if version >= 2 else _upgrade(ordered)),
        )
        summaries[run_id] = summary

    roots: list[RunSummary] = []
    for summary in summaries.values():
        parent = summaries.get(summary.parent_run_id or "")
        if parent is not None and parent is not summary:
            parent.children.append(summary)
        else:
            roots.append(summary)

    for summary in summaries.values():
        summary.children.sort(key=lambda child: child.started or "")
    roots.sort(key=lambda run: run.started or "")

    # The root run's tree and totals are built over **every** record in the
    # file, not only its own. Both halves of that are deliberate.
    #
    # The *tree*, because a child's spans hang off the parent's `tool.call`
    # for the delegation that caused them (ADR-093). Building the parent's
    # tree from the parent's records alone would stop it at the delegation
    # boundary and produce exactly the trace this rewrite was meant to
    # replace: one that says work happened elsewhere instead of showing it.
    #
    # The *totals*, because a run that spent ninety seconds inside a
    # sub-agent did spend ninety seconds. The parent's summary is what a
    # person opens, so it has to account for the whole run; each child keeps
    # its own signals for when a reader descends into it.
    ordered_all = sorted(events, key=_sort_key)
    for root in roots:
        if root.schema_version >= 2 and root.parent_run_id is None:
            root.root = _build_tree(ordered_all)
            root.signals = derive_signals(ordered_all)
    return roots


def _build_tree(records: list[dict[str, Any]]) -> SpanNode | None:
    """Assemble one run's records into a span tree.

    The run's own span is the one whose ``parent_span_id`` names no span in
    this run — either ``None`` for the top-level agent, or the parent's
    ``tool.call`` for a delegated child, which lives in a different run's
    records and so is not found here. Both cases are "has no parent among
    my own spans", which is why one rule handles them.

    Args:
        records: One run's records, in start order.

    Returns:
        The root node, or ``None`` if the records have no span ids.
    """
    nodes: dict[str, SpanNode] = {}
    for record in records:
        span_id = _optional_str(record.get("span_id"))
        if span_id is not None:
            nodes[span_id] = SpanNode(record=record)
    if not nodes:
        return None

    root: SpanNode | None = None
    for node in nodes.values():
        parent_id = _optional_str(node.record.get("parent_span_id"))
        parent = nodes.get(parent_id or "")
        if parent is not None:
            parent.children.append(node)
        elif root is None or _sort_key(node.record) < _sort_key(root.record):
            # More than one parentless span means a run whose file is
            # incomplete — a process killed before the outer spans closed.
            # The earliest one is the best available root, which keeps a
            # partial trace readable rather than refusing it.
            root = node

    for node in nodes.values():
        node.children.sort(key=lambda child: _sort_key(child.record))
    return root


def _upgrade(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Make v1 records legible to the v2 signal derivation.

    The v1 log wrote a ``run_start`` and a ``run_end`` for each run and had
    no ``duration_ms``, no ``status`` and no ``error_type``. This fills in
    the equivalents so one code path computes signals for both schemas —
    the alternative being two derivations that gradually disagree about what
    a tool failure is.

    Args:
        records: v1 records for one run.

    Returns:
        Records with the v2 fields a reader needs, derived from v1 ones.
    """
    upgraded: list[dict[str, Any]] = []
    for record in records:
        event = record.get("event")
        if event == _V1_RUN_START:
            # v1's run_start carried nothing the totals need; its run_end
            # carried them all. Dropping it avoids counting the run twice.
            continue
        promoted = dict(record)
        promoted.setdefault("duration_ms", record.get("latency_ms"))
        promoted.setdefault("status", "error" if record.get("ok") is False else "ok")
        if event == _V1_RUN_END:
            delegated = bool(record.get("parent_run_id"))
            promoted["span"] = _SPAN_SUBRUN if delegated else _SPAN_RUN
            promoted.setdefault("parent_span_id", None)
        elif event == _V1_LLM:
            promoted["span"] = _SPAN_LLM
        elif event == _V1_TOOL:
            promoted["span"] = _SPAN_TOOL
        elif event == _V1_PERMISSION:
            promoted["span"] = _SPAN_PERMISSION
        upgraded.append(promoted)

    # v1 had no iteration spans, so the count has to come from somewhere.
    # The number of model calls is exactly the number of iterations the loop
    # ran, because the loop makes one call per cycle.
    return upgraded


def load_trace(path: Path, quality: QualityStore | None = None) -> RunSummary:
    """Read one trace file as a single run.

    Args:
        path: The ``.jsonl`` file.
        quality: Optional store to look a task-success verdict up in.

    Returns:
        The top-level run, with its delegated children nested.

    Raises:
        TraceNotFoundError: If the file holds no readable run.
        OSError: If the file cannot be read.
    """
    runs = summarise(load_events(path))
    if not runs:
        raise TraceNotFoundError(f"{path.name} contains no readable run")
    run = runs[0]
    if quality is not None:
        run.quality = quality.load(run.run_id)
    return run


def find_trace(traces_dir: Path, reference: str) -> Path:
    """Resolve a run reference to a trace file.

    Args:
        traces_dir: Directory of ``.jsonl`` traces.
        reference: A full run id, any unique prefix of one, or ``last`` for
            the most recent.

    Returns:
        The matching path.

    Raises:
        TraceNotFoundError: If nothing matched, or more than one did.
    """
    if not traces_dir.is_dir():
        raise TraceNotFoundError(f"no traces directory at {traces_dir}")
    files = sorted(
        traces_dir.glob(f"*{_SUFFIX}"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not files:
        raise TraceNotFoundError(f"no traces in {traces_dir}")
    if not reference or reference == "last":
        return files[0]

    matches = [path for path in files if path.stem.startswith(reference)]
    if not matches:
        raise TraceNotFoundError(f"no run id starts with {reference!r}")
    if len(matches) > 1:
        names = ", ".join(path.stem[:_SHORT_ID] for path in matches[:5])
        raise TraceNotFoundError(f"{reference!r} matches several runs: {names}")
    return matches[0]


def list_traces(
    traces_dir: Path, quality: QualityStore | None = None
) -> list[RunSummary]:
    """Summarise every trace in a directory, newest first.

    Args:
        traces_dir: Directory of ``.jsonl`` traces.
        quality: Optional store to look verdicts up in.

    Returns:
        One summary per readable file. Unreadable files are skipped: a
        directory of a hundred runs should not fail to list because one
        write was interrupted.
    """
    if not traces_dir.is_dir():
        return []
    runs: list[RunSummary] = []
    for path in sorted(
        traces_dir.glob(f"*{_SUFFIX}"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    ):
        try:
            runs.append(load_trace(path, quality))
        except (TraceNotFoundError, OSError):
            continue
    return runs


# --- rendering --------------------------------------------------------------


def render_run(run: RunSummary, slowest: int = 0) -> str:
    """Render one run as a readable timeline.

    Args:
        run: The run to show.
        slowest: How many slowest operations to list, or ``0`` for none.

    Returns:
        Text, ready to print.
    """
    lines = [_header(run), ""]
    if run.root is not None:
        lines.extend(_tree(run.root, 1))
    else:
        lines.extend(_flat(run, 1))
    lines.append("")
    lines.extend(_totals(run))
    diagnosis = _diagnosis(run)
    if diagnosis:
        lines.append("")
        lines.extend(diagnosis)
    if slowest:
        lines.append("")
        lines.extend(_slowest(run, slowest))
    return "\n".join(lines)


def render_list(runs: list[RunSummary]) -> str:
    """Render a directory of runs as one line each.

    Args:
        runs: Summaries, newest first.

    Returns:
        Text, ready to print.
    """
    if not runs:
        return "No runs recorded yet."
    lines = [
        f"{'run':<10} {'when':<20} {'role':<11} {'outcome':<22} "
        f"{'time':>8} {'steps':>6} {'tools':>6} {'tokens':>8}"
    ]
    for run in runs:
        outcome = _EXIT_WORDS.get(run.exit_reason or "", "unknown")
        if run.quality is not None and run.quality.task_success is not None:
            outcome = f"{outcome} / task {run.quality.outcome}"
        elif run.signals.warnings:
            outcome = f"{outcome} ({len(run.signals.warnings)} warn)"
        lines.append(
            f"{run.short_id:<10} {_when(run.started):<20} {run.role:<11} "
            f"{outcome:<22} {_seconds(run.duration_ms):>8} "
            f"{run.signals.iterations:>6} {run.signals.tool_calls:>6} "
            f"{run.total_tokens:>8}"
        )
    return "\n".join(lines)


def _header(run: RunSummary) -> str:
    """One line naming the run and how it ended."""
    outcome = _EXIT_WORDS.get(run.exit_reason or "", "unknown")
    schema = "" if run.schema_version >= 2 else "  [v1 trace]"
    return (
        f"run {run.short_id}  {run.role}  {_when(run.started)}  "
        f"{outcome} in {_seconds(run.duration_ms)}{schema}"
    )


def _tree(node: SpanNode, depth: int) -> list[str]:
    """Render a span and everything inside it.

    Indentation *is* the parentage, which is the whole difference from the
    Week 3 reader: a permission decision sits under the call it decided
    because that is where the record says it belongs, not because it
    happened to be printed next to it.

    The failure marker and the duration live in fixed columns to the *left*
    of the indentation rather than inside it. A tree whose ``!`` moves right
    as it nests is a tree you have to read to find the failures in; one with
    a gutter is a tree you can scan.
    """
    lines = [_gutter(node) + _INDENT * depth + _span_body(node)]
    for child in node.children:
        lines.extend(_tree(child, depth + 1))
    return lines


def _gutter(node: SpanNode) -> str:
    """The fixed left columns: a failure marker and a duration."""
    mark = "!" if node.failed else " "
    return f"{mark} {node.duration_ms:>6}ms  "


def _span_body(node: SpanNode) -> str:
    """The part of a span's line that describes what it was."""
    record = node.record
    attributes = node.attributes

    if node.name in (_SPAN_RUN, _SPAN_SUBRUN):
        role = record.get("role") or "main"
        reason = _EXIT_WORDS.get(str(record.get("exit_reason") or ""), "unknown")
        body = f"{node.name} [{role}] {reason}"
    elif node.name == _SPAN_ITERATION:
        chars = _int(attributes.get("harness.context.chars"))
        extra = f"  context {chars:,} chars" if chars else ""
        body = f"iteration {record.get('iteration')}{extra}"
    elif node.name == _SPAN_LLM:
        tokens = _tokens(record)
        answer = attributes.get("harness.llm.final_answer")
        calls = _int(attributes.get("harness.llm.tool_calls"))
        shape = "final answer" if answer else f"{calls} tool call{_s(calls)}"
        retries = _int(attributes.get("harness.llm.retries"))
        retry = f"  {retries} retr{'y' if retries == 1 else 'ies'}" if retries else ""
        body = f"llm  {shape}{tokens}{retry}"
    elif node.name == _SPAN_TOOL:
        digest = attributes.get("harness.tool.args")
        server = attributes.get("harness.mcp.server")
        where = f" via {server}" if server else ""
        detail = f"  {digest}" if digest else ""
        body = f"{record.get('tool')}{where}{detail}"
    elif node.name == _SPAN_PERMISSION:
        decision = str(record.get("permission") or "?")
        note = " (not run)" if decision in _REFUSED else ""
        body = f"permission: {decision}{note}"
    elif node.name == _SPAN_FINALIZE:
        status = attributes.get("harness.subagent.result_status") or ""
        body = f"finalize {status}".strip()
    else:
        body = node.name

    reason = f"  [{node.error_type}]" if node.error_type else ""
    return f"{body}{reason}"


def _flat(run: RunSummary, depth: int) -> list[str]:
    """Render a v1 trace, which has no tree to render.

    Kept for the traces already on the memory volume. Each line is an
    event in file order, which is all a v1 file supports.
    """
    lines: list[str] = []
    for record in run.events:
        event = record.get("event")
        if event == _V1_RUN_START:
            continue
        mark = "!" if record.get("ok") is False else " "
        timing = f"{_int(record.get('latency_ms')):>6}ms"
        if event == _V1_LLM:
            body = f"llm{_tokens(record)}"
        elif event == _V1_TOOL:
            body = str(record.get("tool"))
        elif event == _V1_PERMISSION:
            body = f"permission: {record.get('permission')}"
        else:
            body = str(event)
        lines.append(f"{mark} {timing}  " + _INDENT * depth + body)
    for child in run.children:
        lines.append(f"{' ':>11}  " + _INDENT * depth + f"delegated to {child.role}")
        lines.extend(_flat(child, depth + 1))
    return lines


def _totals(run: RunSummary) -> list[str]:
    """The numbers a reader wants without counting lines."""
    signals = run.signals
    lines = [
        f"  {signals.iterations} iteration{_s(signals.iterations)}, "
        f"{signals.llm_calls} model call{_s(signals.llm_calls)}"
        + (f" ({signals.llm_errors} failed)" if signals.llm_errors else "")
        + f", {signals.tool_calls} tool call{_s(signals.tool_calls)}"
        + (f" ({signals.tool_failures} failed)" if signals.tool_failures else ""),
        f"  {run.total_tokens:,} tokens "
        f"({signals.prompt_tokens:,} in, {signals.completion_tokens:,} out)",
        f"  time: {_split(signals)}",
    ]
    if run.children:
        names = ", ".join(f"{child.role} ({child.short_id})" for child in run.children)
        lines.append(f"  delegated: {names}")
    if run.quality is not None and run.quality.task_success is not None:
        note = f" — {run.quality.note}" if run.quality.note else ""
        lines.append(
            f"  task: {run.quality.outcome} [{run.quality.failure_reason}] "
            f"by {run.quality.source}{note}"
        )
    return lines


def _split(signals: RunSignals) -> str:
    """Where the wall-clock time went.

    The three numbers that matter, and the third is the one nobody
    instruments: everything that was neither the model nor a tool is the
    harness's own cost, and printing it is how "observability is low
    overhead" stops being a claim and becomes a measurement (ADR-093).
    """
    total = signals.wall_seconds or 0.0
    if total <= 0:
        return "not recorded"
    parts = [
        f"model {_duration(signals.model_seconds)} "
        f"({_share(signals.model_seconds, total)})",
        f"tools {_duration(signals.tool_seconds)} "
        f"({_share(signals.tool_seconds, total)})",
        f"harness {_duration(signals.overhead_seconds)} "
        f"({_share(signals.overhead_seconds, total)})",
    ]
    return ", ".join(parts)


def _diagnosis(run: RunSummary) -> list[str]:
    """What to look at, when the trace suggests something.

    This is the section §9 of the brief is asking for. It does not claim to
    know whether the task succeeded — nothing in the harness can — but it
    does separate the two shapes of failure that look identical in
    aggregate: *something broke* (a layer is named, go and fix it) and
    *nothing broke and the answer is still wrong* (the agent chose badly,
    which is a prompt or tool-description problem and no amount of
    infrastructure work will help).
    """
    signals = run.signals
    lines: list[str] = []

    # Guards are excluded from "failures by layer" on purpose. The iteration
    # cap and the repetition stop are the harness working as designed
    # (ADR-047, ADR-049), and listing them as failures would answer "whose
    # fault" with "ours, for having a safety net" — while hiding the far
    # more interesting fact that *nothing else* went wrong.
    component_failures = {
        layer: count
        for layer, count in signals.error_layers.items()
        if layer != "guard"
    }

    if component_failures:
        blame = ", ".join(
            f"{layer} ({count})"
            for layer, count in sorted(component_failures.items(), key=lambda i: -i[1])
        )
        lines.append(f"  failures by layer: {blame}")
    elif not signals.agent_ok:
        # The residual case §9 cares most about, and the one no aggregate
        # can surface: every call succeeded and the task still is not done.
        # That is a reasoning problem, so saying it plainly is what stops
        # somebody spending an afternoon on infrastructure.
        lines.append(
            "  no component reported a failure — the run ended without an "
            "answer despite every call succeeding"
        )
        if signals.error_layers.get("guard"):
            lines.append("  the run was stopped by a guard, not by an error")

    if signals.worst_tool and signals.worst_tool[1] > 1:
        tool, count = signals.worst_tool
        lines.append(f"  most failures: {tool} ({count})")

    for warning in signals.warnings:
        lines.append(f"  ! {warning}: {_WARNING_WORDS.get(warning, warning)}")

    return ["diagnosis:", *lines] if lines else []


def _slowest(run: RunSummary, limit: int) -> list[str]:
    """The slowest individual operations in the run and its children."""
    timed: list[tuple[int, str]] = []
    _collect_timed(run, timed, "")
    if not timed:
        return []
    timed.sort(reverse=True)
    lines = [f"slowest {min(limit, len(timed))}:"]
    for microseconds, label in timed[:limit]:
        lines.append(f"  {_us(microseconds):>9}  {label}")
    return lines


def _collect_timed(run: RunSummary, into: list[tuple[int, str]], prefix: str) -> None:
    """Gather every timed operation, labelled with which run it was in."""
    source = run.root.walk() if run.root is not None else []
    if source:
        for node in source:
            # Runs and iterations are containers: their duration is the sum
            # of what is inside them, so listing them would fill the whole
            # table with the run itself.
            if node.name in (_SPAN_RUN, _SPAN_SUBRUN, _SPAN_ITERATION):
                continue
            # `delegate`'s duration is the child run's whole duration, which
            # is already itemised as that child's own spans below.
            if node.name == _SPAN_TOOL and node.record.get("tool") == DELEGATE_TOOL:
                continue
            into.append((node.duration_us, f"{prefix}{_operation_label(node)}"))
    else:
        for record in run.events:
            if record.get("event") in (_V1_LLM, _V1_TOOL):
                label = record.get("tool") or "llm"
                into.append((_int(record.get("latency_ms")) * 1000, f"{prefix}{label}"))
    for child in run.children:
        _collect_timed(child, into, f"{prefix}{child.role}/")


def _operation_label(node: SpanNode) -> str:
    """Name one operation for the slowest-first table.

    A ``permission.check`` carries the tool name it decided, so labelling
    every span by ``record["tool"]`` made a slow confirmation prompt appear
    as a slow tool — the exact confusion ADR-087 introduced the split to
    avoid, reintroduced one layer up. The decision is named as a decision.
    """
    if node.name == _SPAN_PERMISSION:
        return f"permission: {node.record.get('tool')}"
    if node.name == _SPAN_TOOL:
        return str(node.record.get("tool") or _SPAN_TOOL)
    return node.name


# --- small helpers ----------------------------------------------------------


def _sort_key(record: dict[str, Any]) -> tuple[int, str]:
    """Order records by when the span *started*.

    Necessary because spans are written when they close, so file order is
    completion order and a parent's line follows its children's. Sorting on
    ``start_unix_ns`` restores the order things happened in, which is the
    order a person reading a timeline expects. The timestamp string is the
    tiebreaker, and the v1 fallback.
    """
    return (_int(record.get("start_unix_ns")), str(record.get("ts") or ""))


def _parent_of(records: list[dict[str, Any]]) -> str | None:
    """The parent run id these records carry, if any."""
    for record in records:
        parent = _optional_str(record.get("parent_run_id"))
        if parent:
            return parent
    return None


def _role_of(records: list[dict[str, Any]]) -> str:
    """The role these records carry."""
    for record in records:
        role = _optional_str(record.get("role"))
        if role:
            return role
    return "main"


def _tokens(record: dict[str, Any]) -> str:
    """Render a record's token counts, or nothing when it has none."""
    prompt = _int(record.get("tokens_prompt"))
    completion = _int(record.get("tokens_completion"))
    if not prompt and not completion:
        return ""
    return f"  {prompt}+{completion} tokens"


def _optional_str(value: Any) -> str | None:
    """Coerce a field to a non-empty string, or ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value: Any) -> int:
    """Coerce a field to an int, defaulting to zero."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _when(stamp: str | None) -> str:
    """Render a timestamp for a listing, in local time."""
    if not stamp:
        return "?"
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return stamp[:19]
    return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _us(value: int) -> str:
    """Render a microsecond duration at whatever scale reads best.

    Three scales because the operations in one trace span five orders of
    magnitude: a permission check is tens of microseconds and a model call
    is tens of seconds, and one unit cannot show both without printing
    either "0ms" or "0.00003s".
    """
    if value < 1000:
        return f"{value}µs"
    if value < 1_000_000:
        return f"{value / 1000:.0f}ms"
    return f"{value / 1_000_000:.1f}s"


def _seconds(milliseconds: int) -> str:
    """Render a duration in seconds, or milliseconds when that reads better.

    A run against the real endpoint takes tens of seconds and a scripted
    one takes five milliseconds, and "0.0s" is a useless way to report the
    second. One unit cannot serve both.
    """
    if milliseconds < 1000:
        return f"{milliseconds}ms"
    return f"{milliseconds / 1000:.1f}s"


def _duration(seconds: float) -> str:
    """Render a fractional-second duration at a readable scale."""
    if seconds < 1.0:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds:.1f}s"


def _share(part: float, total: float) -> str:
    """Render a share as a percentage."""
    return f"{(part / total * 100):.0f}%" if total else "0%"


def _s(count: int) -> str:
    """Return a plural ``s`` when ``count`` is not one."""
    return "" if count == 1 else "s"


# --- command line -----------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse arguments for ``python -m harness.observability.replay``."""
    parser = argparse.ArgumentParser(
        prog="python -m harness.observability.replay",
        description=(
            "Read agent run traces. With no run id, lists every recorded run."
        ),
    )
    parser.add_argument(
        "run",
        nargs="?",
        help="Run id, any unique prefix of one, or 'last'.",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=None,
        help="Traces directory. Defaults to memory/traces next to the harness.",
    )
    parser.add_argument(
        "--slowest",
        type=int,
        default=0,
        metavar="N",
        help="Also list the N slowest operations in the run.",
    )
    parser.add_argument(
        "--failures",
        action="store_true",
        help=(
            "List only runs that failed: no final answer, or a failure in "
            "the model, tool or MCP layer. Includes runs that answered while "
            "carrying failures underneath, which is the case worth finding."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the derived signals as JSON instead of a timeline.",
    )
    return parser.parse_args(argv)


def _default_dir() -> Path:
    """Where traces live when no directory is given."""
    return Path("memory") / "traces"


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m harness.observability.replay``.

    Args:
        argv: Arguments, or ``None`` to read ``sys.argv``.

    Returns:
        Process exit status.
    """
    args = _parse_args(argv)
    traces_dir = args.dir or _default_dir()
    quality = QualityStore(traces_dir)

    if args.run:
        try:
            run = load_trace(find_trace(traces_dir, args.run), quality)
        except (TraceNotFoundError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if args.json:
            from dataclasses import asdict

            print(json.dumps(asdict(run.signals), indent=2, default=str))
        else:
            print(render_run(run, args.slowest))
        return 0

    runs = list_traces(traces_dir, quality)
    if args.failures:
        # Deliberately not "anything with a warning". A warning is a prompt
        # to look, and on a healthy directory most runs carry one — a filter
        # that matched them all would be the unfiltered listing with extra
        # steps. What this selects is a run that did not answer, or one
        # whose infrastructure failed underneath an answer (ADR-096).
        runs = [run for run in runs if not run.ok or not run.signals.system_ok]
    if args.json:
        from dataclasses import asdict

        print(
            json.dumps(
                [
                    {"run_id": run.run_id, "role": run.role, **asdict(run.signals)}
                    for run in runs
                ],
                indent=2,
                default=str,
            )
        )
        return 0
    print(render_list(runs))
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
