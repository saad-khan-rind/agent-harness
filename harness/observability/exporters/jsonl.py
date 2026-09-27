"""One JSONL file per run, now a span tree rather than a flat list.

The file is still ``{traces_dir}/{run_id}.jsonl`` and a child's spans still
land in its parent's file, because "inspect one agent run" has to include
the runs it delegated (ADR-083). What changed is what a line *is*.

**A line is a finished span.** It carries ``span_id`` and
``parent_span_id``, so the reader reconstructs a tree instead of guessing
one from adjacency. That is the whole point of ADR-093, and it is why the
Week 3 pairing of a ``permission`` line with the ``tool`` line after it —
correct until a model requested two tools in one turn — is gone.

**The schema is additive.** Every v1 key is still present and still means
what it meant: ``ts``, ``run_id``, ``parent_run_id``, ``role``, ``event``,
``tool``, ``permission``, ``ok``, ``latency_ms``, ``tokens_prompt``,
``tokens_completion``, ``exit_reason``. A script somebody wrote against the
old files keeps working. The new keys sit after them, and
``schema_version`` says which shape a reader is holding so it never has to
infer that from which fields happen to be present (ADR-094).

**Spans are written when they close**, which is how every tracing system
works and which has one consequence worth stating: a parent's line comes
*after* its children's, because it finishes last. Nothing reads the file in
line order — :mod:`harness.observability.replay` sorts by start time and
nests by ``parent_span_id`` — but a person running ``tail -f`` should know
that the last line of a run is its summary rather than its latest step.

**A hook must never break a run.** A directory that cannot be created or a
file that cannot be written retires the trace for that run and the run
continues. A missing trace is a gap in the evidence; a failed task because
a log file was read-only is a harness with its priorities backwards
(ADR-069, ADR-081).
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness.observability.api import (
    SPAN_AGENT_RUN,
    SPAN_LLM_CALL,
    SPAN_PERMISSION,
    SPAN_SUBAGENT_RUN,
    SPAN_TOOL_CALL,
    Attr,
    Span,
)
from harness.observability.errors import layer_for

# Bumped when the shape of a line changes in a way a reader must know about.
# v1 was the Week 3 flat event log; v2 is the span tree.
SCHEMA_VERSION = 2

_SUFFIX = ".jsonl"

# The v1 key order, preserved exactly. Order is not cosmetic here: it keeps a
# raw `cat` of the file readable and it keeps a `cut -d, -f3` somebody wrote
# in week 3 pointing at the same column.
_V1_KEYS: tuple[str, ...] = (
    "ts",
    "run_id",
    "parent_run_id",
    "role",
    "event",
    "tool",
    "permission",
    "ok",
    "latency_ms",
    "tokens_prompt",
    "tokens_completion",
    "exit_reason",
)

# v1 called these events; v2 calls them spans. The mapping keeps `event`
# meaning what it meant, so a v1 reader still sees `llm`, `tool` and
# `permission` lines and can still count them.
_EVENT_NAMES: dict[str, str] = {
    SPAN_AGENT_RUN: "run_end",
    SPAN_SUBAGENT_RUN: "run_end",
    SPAN_LLM_CALL: "llm",
    SPAN_TOOL_CALL: "tool",
    SPAN_PERMISSION: "permission",
}

# Attributes already promoted to a top-level v1 key. Left out of the nested
# `attributes` object so the same fact is not stored twice on every line —
# a trace file is written once per event and read by a person, and duplicate
# fields make it longer without making it clearer.
_PROMOTED = frozenset(
    {
        Attr.RUN_ID,
        Attr.PARENT_RUN_ID,
        Attr.ROLE,
        Attr.TOOL_NAME,
        Attr.PERMISSION_DECISION,
        Attr.EXIT_REASON,
        Attr.GEN_AI_USAGE_INPUT_TOKENS,
        Attr.GEN_AI_USAGE_OUTPUT_TOKENS,
        Attr.ERROR_TYPE,
        Attr.ITERATION,
    }
)


class JsonlSpanExporter:
    """Append one JSON line per finished span, to the run's own file.

    One instance per session. It holds no per-run state at all — the file a
    span belongs to is derived from ``span.trace_id``, which every span in
    a run shares including a delegated child's — so there is nothing for one
    REPL turn to leave behind for the next.
    """

    def __init__(self, traces_dir: Path) -> None:
        """Args:
        traces_dir: Directory to write into. Created on first use; if it
            cannot be, tracing is off for that run and nothing is raised.
        """
        self._traces_dir = traces_dir
        self._broken: set[str] = set()

    @property
    def traces_dir(self) -> Path:
        """Where trace files are written."""
        return self._traces_dir

    def path_for(self, run_id: str) -> Path:
        """Return the file a run is (or would be) written to.

        Args:
            run_id: A top-level run's identifier. A child's spans are in its
                parent's file, so pass the parent's id for those.

        Returns:
            The JSONL path, whether or not it exists yet.
        """
        return self._traces_dir / f"{run_id}{_SUFFIX}"

    def export(self, span: Span) -> None:
        """Append one span. A file that cannot be written is given up on."""
        trace_id = span.trace_id
        if trace_id in self._broken:
            return
        line = json.dumps(self.as_record(span), ensure_ascii=False) + "\n"
        path = self.path_for(trace_id)
        try:
            self._traces_dir.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            # One failure retires the trace for this run. Retrying every span
            # against a full disk or a read-only mount would turn a missing
            # log into a slow agent.
            self._broken.add(trace_id)

    def shutdown(self) -> None:
        """Nothing is held open: each span opens and closes the file.

        Deliberate rather than lazy. A parent and its child write to the
        same path, the process may be killed between spans, and a run that
        was interrupted should still be readable up to its last complete
        line. A held handle would buy some syscalls and cost all three.
        """

    @staticmethod
    def as_record(span: Span) -> dict[str, Any]:
        """Render one span as the JSON object written to the file.

        Static and pure, so the tests assert on the record rather than on
        the contents of a temporary directory, and so
        :mod:`harness.observability.replay` can be checked against the same
        function that produces its input.

        Args:
            span: A finished span.

        Returns:
            A dict whose first twelve keys are the v1 schema.
        """
        attributes = span.attributes
        record: dict[str, Any] = dict.fromkeys(_V1_KEYS)
        record.update(
            ts=_iso(span.start_wall_ns),
            run_id=attributes.get(Attr.RUN_ID),
            parent_run_id=attributes.get(Attr.PARENT_RUN_ID),
            role=attributes.get(Attr.ROLE),
            event=_EVENT_NAMES.get(span.name, span.name),
            tool=attributes.get(Attr.TOOL_NAME),
            permission=attributes.get(Attr.PERMISSION_DECISION),
            # `ok` keeps its v1 meaning — "did this succeed" — and gains a
            # precision it never had: a span whose status is still `unset`
            # is now `null` rather than being rounded to one of the two
            # answers it does not have.
            ok=None if span.status == "unset" else span.status == "ok",
            latency_ms=span.duration_ms,
            tokens_prompt=attributes.get(Attr.GEN_AI_USAGE_INPUT_TOKENS),
            tokens_completion=attributes.get(Attr.GEN_AI_USAGE_OUTPUT_TOKENS),
            exit_reason=attributes.get(Attr.EXIT_REASON),
        )

        error_type = span.error_type
        record.update(
            schema_version=SCHEMA_VERSION,
            span=span.name,
            trace_id=span.trace_id,
            span_id=span.span_id,
            parent_span_id=span.parent_span_id,
            kind=span.kind,
            status=span.status,
            iteration=attributes.get(Attr.ITERATION),
            error_type=error_type,
            # Stored rather than derived on read, so a person grepping the
            # raw file gets the same attribution the dashboard shows and the
            # two cannot drift (ADR-094).
            error_layer=layer_for(error_type) if error_type else None,
            start_unix_ns=span.start_wall_ns,
            duration_ms=span.duration_ms,
            # Also in nanoseconds, because milliseconds are lossy at exactly
            # the scale that matters here. A permission check, a memory
            # injection and a compaction are all sub-millisecond, so a
            # trace that only carried ``duration_ms`` reported them as 0 —
            # and a *whole run* of fast operations as 0 too, which makes the
            # time split ("where did the run go?") unanswerable for any run
            # that did not call a real model. ``duration_ms`` stays because
            # it is a v1 key and it is what a person wants to read.
            duration_ns=span.duration_ns,
            attributes={
                key: value
                for key, value in sorted(attributes.items())
                if key not in _PROMOTED
            },
        )
        if span.events:
            record["events"] = [
                {"ts": _iso(when), "name": name, **fields}
                for when, name, fields in span.events
            ]
        return record


def _iso(unix_ns: int) -> str:
    """UTC timestamp with millisecond precision, ISO 8601 with a ``Z``.

    Same format as v1, so a reader comparing an old file with a new one is
    comparing the same strings.
    """
    moment = datetime.fromtimestamp(unix_ns / 1_000_000_000, UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
