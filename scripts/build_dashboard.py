"""Generate the Grafana dashboard JSON from the frozen metric names.

Run from the repository root:

    python scripts/build_dashboard.py

The committed file is ``observability/grafana/provisioning/dashboards/
harness.json``. It is generated rather than hand-edited because a dashboard
is twenty-odd panels of near-identical JSON, and the only things that vary —
a title, a PromQL query, a unit — are exactly the things that should be
readable in one place. A hand-edited export from the Grafana UI is 2000
lines in which a typo in a metric name is invisible; this file is 200 in
which it is not (ADR-083).

The dashboard is in **two halves**, because it answers two different
questions and mixing them is what produces a wall of graphs nobody reads:

* **Is anything wrong?** (rows 0-2) Throughput, success rate, and — the
  panel added with the span rewrite — *which layer* is failing. A person
  looking at this half is deciding whether to look further, so it is three
  rows and no more.
* **Why?** (rows 3-9) Latency broken down by component, LLM usage, tools and
  MCP servers, permissions, sub-agents, memory, context pressure and task
  quality. A person is here because the first half sent them.

Every query names a metric from the pinned list in
``harness/observability/recorder.py`` and nothing else;
``tests/unit/test_observability_stack.py`` checks both directions.

**One panel, one question.** A metric that is available is not a reason for
a panel; the brief is explicit about this and it is the rule that keeps the
thing usable. Each description below states the question its panel answers,
and a panel that cannot be given one does not ship.
"""

import json
from pathlib import Path
from typing import Any

OUTPUT = (
    Path(__file__).resolve().parents[1]
    / "observability"
    / "grafana"
    / "provisioning"
    / "dashboards"
    / "harness.json"
)

# Must match `uid` in provisioning/datasources/prometheus.yml.
DATASOURCE = {"type": "prometheus", "uid": "prometheus"}

# Grafana lays panels on a 24-column grid. Two panels per row is wide enough
# to read a legend and narrow enough that the seven groups fit on one screen
# at demo resolution.
WIDTH = 12
HEIGHT = 8


def panel(
    title: str,
    query: str,
    kind: str = "timeseries",
    legend: str = "",
    unit: str | None = None,
    description: str = "",
    thresholds: list[tuple[str, float | None]] | None = None,
    extra: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Build one panel. ``gridPos`` is filled in by :func:`layout`.

    Args:
        title: Panel heading.
        query: PromQL.
        kind: Grafana panel type.
        legend: Legend template, e.g. ``{{role}}``.
        unit: Grafana unit id (``s``, ``percentunit``, ``short``…).
        description: Shown on the panel's ``i`` icon. Every panel gets one —
            a query and a legend answer "what is this", not "why would I
            look at it", and that second question is what makes a dashboard
            usable by someone who did not write the queries.
        thresholds: ``(color, value)`` steps, lowest first, ``value=None``
            for the base colour. Reserved for the handful of stat panels
            where a colour actually encodes "is this good or bad" — most
            panels are a shape over time, not a single number to judge.
        extra: Additional ``(query, legend)`` series on the same axes, for
            the few panels where the *comparison* is the point and two
            adjacent panels would make a reader do the arithmetic by eye.
            Used sparingly: most questions are one series.
    """
    field_config: dict[str, Any] = {"defaults": {}, "overrides": []}
    if unit is not None:
        field_config["defaults"]["unit"] = unit
    if thresholds is not None:
        field_config["defaults"]["thresholds"] = {
            "mode": "absolute",
            "steps": [{"color": color, "value": value} for color, value in thresholds],
        }
    return {
        "type": kind,
        "title": title,
        "description": description,
        "datasource": DATASOURCE,
        "fieldConfig": field_config,
        "options": {},
        "targets": [
            {
                "datasource": DATASOURCE,
                "expr": expression,
                "legendFormat": series_legend or "__auto",
                # Grafana identifies a series by refId; "A", "B", "C"… is
                # what its own editor produces, so a panel opened in the UI
                # looks like one somebody made there.
                "refId": chr(ord("A") + index),
            }
            for index, (expression, series_legend) in enumerate(
                [(query, legend), *(extra or [])]
            )
        ],
    }


def stat(
    title: str,
    query: str,
    unit: str | None = None,
    description: str = "",
    thresholds: list[tuple[str, float | None]] | None = None,
) -> dict[str, Any]:
    """A single-number panel."""
    built = panel(
        title,
        query,
        kind="stat",
        unit=unit,
        description=description,
        thresholds=thresholds,
    )
    built["options"] = {
        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        "colorMode": "value",
        "graphMode": "none",
    }
    return built


def quantile(metric: str, by: str, q: float = 0.95) -> str:
    """PromQL for a latency quantile of a histogram, grouped by ``by``."""
    return (
        f"histogram_quantile({q}, "
        f"sum(rate({metric}_bucket[$__rate_interval])) by (le, {by}))"
    )


def per_minute(metric: str, by: str, selector: str = "") -> str:
    """PromQL for a counter's rate per minute, grouped by ``by``."""
    return f"sum(rate({metric}{selector}[$__rate_interval])) by ({by}) * 60"


def increase(metric: str, by: str, selector: str = "") -> str:
    """PromQL for a counter's increase over the graph interval, grouped."""
    return f"sum(increase({metric}{selector}[$__rate_interval])) by ({by})"


def ratio(numerator: str, denominator: str, window: str = "1h") -> str:
    """PromQL for one counter's share of another over ``window``.

    ``increase`` on both sides rather than ``rate``, so the number reads as
    "of the runs in the last hour" rather than as a per-second figure nobody
    can sanity-check by counting.
    """
    return (
        f"sum(increase({numerator}[{window}])) / sum(increase({denominator}[{window}]))"
    )


def average(metric: str, by: str) -> str:
    """PromQL for a histogram's mean, grouped by ``by``.

    Used only where the *typical* value is the question — "how many tool
    calls does an average run make". Where the tail is the question, use
    :func:`quantile` instead: a mean latency hides the one run that took
    five minutes, which is the run worth finding.
    """
    return (
        f"sum(rate({metric}_sum[$__rate_interval])) by ({by}) / "
        f"sum(rate({metric}_count[$__rate_interval])) by ({by})"
    )


# The dashboard, in two halves. See the module docstring.
GROUPS: list[tuple[str, list[dict[str, Any]]]] = [
    (
        "0 · Is anything wrong?",
        [
            panel(
                "Failures by layer",
                increase("harness_errors_total", "layer"),
                legend="{{layer}}",
                unit="short",
                description="THE panel. Every failure anywhere in a run, "
                "attributed to the layer a person should go and look at: "
                "model, tool, mcp, policy, memory, subagent, guard or agent. "
                "Nine legend entries instead of a wall of error strings, "
                "because the first question is never 'what was the "
                "exception' but 'is this my agent, my tools, my policy, or "
                "the model'. Rising 'agent' with everything else flat is the "
                "hardest case and the one no other panel shows: nothing "
                "broke and the answers are still wrong.",
            ),
            stat(
                "Success ratio (1h)",
                ratio('harness_runs_total{status="success"}', "harness_runs_total"),
                unit="percentunit",
                description="Runs that reached a final answer. Note what this "
                "is NOT: it says nothing about whether the answer was "
                "right. That is the Task quality row, and the gap between "
                "the two numbers is the most interesting thing on this "
                "dashboard.",
                thresholds=[("red", None), ("orange", 0.7), ("green", 0.9)],
            ),
            stat(
                "Active runs",
                "sum(harness_active_runs)",
                description="Runs executing right now, all roles. Returns to "
                "zero between tasks; a value that sticks is a hook that did "
                "not fire on_run_end.",
            ),
            panel(
                "Runs by outcome",
                increase("harness_runs_total", "status"),
                legend="{{status}}",
                unit="short",
                description="success / error / capped / no_progress. The last "
                "two are guards working as designed (ADR-047, ADR-049), kept "
                "apart from 'error' on purpose: a dashboard that filed a "
                "working safety net under failures would raise a false alarm "
                "every time the harness protected a run.",
            ),
        ],
    ),
    (
        "1 · Throughput and active runs",
        [
            panel(
                "Runs completed per minute",
                per_minute("harness_runs_total", "role"),
                legend="{{role}}",
                unit="short",
                description="Runs finishing, by role. Flat at zero while "
                "Active runs is nonzero means work is in flight but nothing "
                "is finishing — check Run duration p95 next.",
            ),
            stat(
                "Active runs",
                "sum(harness_active_runs)",
                description="Runs executing right now, all roles. Returns to "
                "zero between tasks; a value that sticks is a hook that did "
                "not fire on_run_end.",
            ),
        ],
    ),
    (
        "2 · Latency (run and components)",
        [
            panel(
                "Run duration p95",
                quantile("harness_run_duration_seconds", "role"),
                legend="{{role}}",
                unit="s",
                description="Wall-clock time from task to final answer, 95th "
                "percentile. The number the rest of this row exists to "
                "explain when it is high.",
            ),
            panel(
                "LLM request latency p95",
                quantile("harness_llm_latency_seconds", "job"),
                legend="p95",
                unit="s",
                description="Time InnKube took to answer one chat call. "
                "Usually most of a run's duration lives here, not in tool "
                "execution — check this before suspecting a tool.",
            ),
            panel(
                "Tool latency p95 by kind",
                quantile("harness_tool_latency_seconds", "kind"),
                legend="{{kind}}",
                unit="s",
                description="Time a tool call takes once the model has "
                "decided to make it, split by kind (builtin, mcp, memory, "
                "skill, delegate) so a slow MCP server does not hide inside "
                "an undifferentiated 'tools are slow'.",
            ),
            panel(
                "Sub-agent duration p95",
                quantile("harness_subagent_duration_seconds", "role"),
                legend="{{role}}",
                unit="s",
                description="Wall-clock time of a delegated child run. "
                "Compare against Run duration p95 to see whether delegating "
                "is actually saving the parent time or just adding a hop.",
            ),
        ],
    ),
    (
        "3 · Success and failure rates",
        [
            stat(
                "Success ratio (1h)",
                ratio('harness_runs_total{status="success"}', "harness_runs_total"),
                unit="percentunit",
                description="Runs that reached a final answer, over all runs. "
                "capped and no_progress are the loop guards working, not "
                "errors, and are counted separately beside this. Note what "
                "this is NOT: it says nothing about whether the answer was "
                "right — that is row 11, and the gap between the two numbers "
                "is the most interesting thing on this dashboard.",
                thresholds=[("red", None), ("orange", 0.8), ("green", 0.95)],
            ),
            panel(
                "Runs by outcome",
                increase("harness_runs_total", "status"),
                legend="{{status}}",
                unit="short",
                description="Every value the status label can take, so a "
                "spike in capped or no_progress (the loop guards working, "
                "not errors) is visible next to failed rather than folded "
                "into it.",
            ),
        ],
    ),
    (
        "4 · LLM usage and tokens",
        [
            panel(
                "Model requests per minute",
                per_minute("harness_llm_requests_total", "role"),
                legend="{{role}}",
                unit="short",
                description="How often the loop calls the LLM, by role. "
                "Several requests per completed run is normal — ReAct "
                "iterates — so compare against Runs completed per minute "
                "rather than expecting a 1:1 ratio.",
            ),
            panel(
                "Tokens per minute by direction",
                per_minute("harness_llm_tokens_total", "direction"),
                legend="{{direction}}",
                unit="short",
                description="Prompt vs completion tokens billed. Prompt "
                "usually dominates — that is context, not the model's own "
                "output — and a rising share of it over a long session is "
                "context rot building up.",
            ),
        ],
    ),
    (
        "5 · Tool usage (built-in, MCP, memory, skills, delegate)",
        [
            panel(
                "Tool calls by kind",
                increase("harness_tool_calls_total", "kind"),
                legend="{{kind}}",
                unit="short",
                description="kind separates what the harness ships (builtin) "
                "from what an MCP server executes (mcp), memory and skill "
                "tools, and delegation hops.",
            ),
            panel(
                "Tool calls by tool",
                increase("harness_tool_calls_total", "tool"),
                legend="{{tool}}",
                unit="short",
                description="The same calls as 'Tool calls by kind', broken "
                "down to the individual tool name — the panel that answers "
                "'which tool does the agent actually reach for'.",
            ),
            panel(
                "Failed tool calls",
                increase("harness_tool_calls_total", "tool", '{ok="false"}'),
                legend="{{tool}}",
                unit="short",
                description="Calls where the tool itself reported an error "
                '(ok="false"), by tool. Not a denial: a permission refusal '
                "is counted under Decisions by outcome, not here.",
            ),
            panel(
                "MCP calls by server",
                increase("harness_mcp_calls_total", "server"),
                legend="{{server}}",
                unit="short",
                description="'Which MCP server is responsible for a failure' "
                "as one query. The server was always recoverable from the "
                "tool-name prefix and was never readable; recording it as "
                "its own dimension makes it a legend entry. Safe as a label "
                "because server names come from config.yaml — bounded by a "
                "file somebody edits, not by what a model invents.",
            ),
            panel(
                "MCP failures by server",
                increase("harness_mcp_calls_total", "server", '{ok="false"}'),
                legend="{{server}}",
                unit="short",
                description="One server failing while the others are flat is "
                "a server to restart; all of them failing together is the "
                "harness or the network.",
            ),
            panel(
                "Repeated identical calls",
                increase("harness_repeated_tool_calls_total", "tool"),
                legend="{{tool}}",
                unit="short",
                description="'Are agents repeatedly calling the same failing "
                "tool?' — counted by fingerprinting the arguments, so it is "
                "visible without recording what was passed. Rises before the "
                "no-progress guard fires, which makes it a warning rather "
                "than a postmortem, and usually means a tool description or "
                "a prompt needs work rather than the tool itself.",
            ),
            panel(
                "Work per run: iterations and tool calls",
                average("harness_run_tool_calls", "role"),
                legend="{{role}} tool calls (mean)",
                unit="short",
                description="'How many tools does an average run invoke?' A "
                "mean is the right statistic here because the question is "
                "about the typical run; the outlier that called forty shows "
                "up in Runs by outcome as capped or no_progress. Plotted "
                "beside iterations so the ratio between them is readable: "
                "many tool calls per iteration is a model batching its work, "
                "and one per iteration with a rising count is a model going "
                "round in circles.",
                extra=[
                    (
                        average("harness_run_iterations", "role"),
                        "{{role}} iterations (mean)",
                    ),
                ],
            ),
        ],
    ),
    (
        "6 · Permission decisions",
        [
            panel(
                "Decisions by outcome",
                increase("harness_permission_decisions_total", "outcome"),
                legend="{{outcome}}",
                unit="short",
                description="allow and deny are the policy alone; confirmed "
                "and declined are the two halves of require_user_confirmation "
                "— whether the person said yes (ADR-081).",
            ),
            stat(
                "Denied or declined (1h)",
                'sum(increase(harness_permission_decisions_total{outcome=~"deny|declined"}[1h]))',
                unit="short",
                description="Calls the policy refused, or a person said no "
                "to, in the last hour. Not itself a failure signal — a "
                "healthy confirm_mutations session denies things on "
                "purpose.",
            ),
        ],
    ),
    (
        "7 · Sub-agent activity",
        [
            panel(
                "Delegated runs by role and outcome",
                increase("harness_subagent_runs_total", "role, status"),
                legend="{{role}} / {{status}}",
                unit="short",
                description="Every sub-agent run, split by which specialist "
                "ran and how it ended — the panel that shows whether "
                "delegation is actually being used, not just configured.",
            ),
            stat(
                "Active sub-agents",
                'sum(harness_active_runs{role!="main"})',
                description="Child loops executing right now. Nested inside "
                "a main run, so this is at most the number of delegate calls "
                "in flight.",
            ),
        ],
    ),
    (
        "8 · Failure diagnosis",
        [
            panel(
                "Error types",
                increase("harness_errors_total", "error_type"),
                legend="{{error_type}}",
                unit="short",
                description="The drill-down from row 0: once a layer is "
                "named, this says which failure inside it. Bounded to the "
                "vocabulary in harness/observability/errors.py, so the "
                "legend stays readable and the label stays safe.",
            ),
            # `ratio()` wraps one series in `increase(...)`. The denominator
            # here is two counters (failures do not increment requests —
            # see exporters/metrics.py), and PromQL cannot
            # `increase(a + b[1h])`: the range selector binds to one
            # series. Increase each, then add. `or vector(0)` so a
            # cluster that has never failed still shows 0%, not "No data".
            stat(
                "Model request failure ratio (1h)",
                "(sum(increase(harness_llm_errors_total[1h])) or vector(0)) / "
                "((sum(increase(harness_llm_errors_total[1h])) or vector(0)) + "
                "(sum(increase(harness_llm_requests_total[1h])) or vector(0)))",
                unit="percentunit",
                description="Requests that failed, over all requests made. "
                "The brief's 'do not assume an HTTP 200 means the agent "
                "succeeded', read the other way round: before the span "
                "rewrite a failed request incremented nothing at all, so a "
                "run killed by a 429 and one killed by a bad prompt were the "
                "same evidence. Nonzero here with a green success ratio "
                "means the harness is absorbing endpoint trouble.",
                thresholds=[("green", None), ("orange", 0.05), ("red", 0.2)],
            ),
            panel(
                "LLM failures by kind",
                increase("harness_llm_errors_total", "error_type"),
                legend="{{error_type}}",
                unit="short",
                description="rate_limited is capacity, timeout is often a "
                "context too large, auth is a person's problem no retry will "
                "fix. Different responses, so different series.",
            ),
            panel(
                "Retries per minute",
                per_minute("harness_llm_retries_total", "error_type"),
                legend="{{error_type}}",
                unit="short",
                description="Retries happen inside the client, below the "
                "loop, so without this a rate-limited run and a slow run are "
                "the same number of seconds on the same graph. Rising here "
                "with flat LLM failures means the cluster is busy and the "
                "harness is absorbing it.",
            ),
        ],
    ),
    # Rows 1-7 are the brief's seven bullets in its order. This one is ours,
    # and it is numbered outside that range so the mapping stays checkable
    # (ADR-114).
    (
        "9 · Context window",
        [
            panel(
                "Context size p95",
                quantile("harness_context_chars", "role"),
                legend="{{role}}",
                unit="none",
                description="Characters sent to the model, not tokens: the "
                "endpoint often omits usage, so the harness compacts on a "
                "character budget and this measures the thing that fires "
                "(ADR-032).",
            ),
            panel(
                "Compactions per minute",
                per_minute("harness_context_compactions_total", "role"),
                legend="{{role}}",
                unit="short",
                description="Each one dropped an old tool observation the "
                "model can no longer see. Harmless in ones; a run that "
                "compacts repeatedly is losing the evidence it was working "
                "from, and that is invisible in every other panel.",
            ),
        ],
    ),
    (
        "10 · Long-term memory",
        [
            panel(
                "Memory operations",
                increase("harness_memory_ops_total", "op"),
                legend="{{op}}",
                unit="short",
                description="Reads and writes by operation. 'inject' is the "
                "retrieval the harness performs on every single run without "
                "a tool call — the most frequent memory operation in the "
                "system, and the one nothing measured until memory got its "
                "own spans (ADR-094).",
            ),
            panel(
                "Memory latency p95",
                quantile("harness_memory_operation_seconds", "op"),
                legend="{{op}}",
                unit="s",
                description="Qdrant is a container on the same network, so "
                "anything above a few hundred milliseconds is already a "
                "problem. Rising here with flat run duration means memory is "
                "slow but not yet the bottleneck.",
            ),
            panel(
                "Records retrieved per read",
                average("harness_memory_items_returned", "op"),
                legend="{{op}} (mean)",
                unit="short",
                description="Zero on every read means the collection is "
                "empty or the embedder changed dimension — both of which "
                "look exactly like 'memory is working' on every other panel, "
                "because the calls succeed and return nothing.",
            ),
        ],
    ),
    (
        "11 · Task quality (not the same as run success)",
        [
            panel(
                "Task verdicts",
                increase("harness_task_outcomes_total", "outcome"),
                legend="{{outcome}}",
                unit="short",
                description="Whether a run did what was asked, recorded by "
                "whoever knows — a person with /eval, or a test with a known "
                "answer. Nothing in the harness can derive this, which is "
                "exactly why it is a separate metric rather than a "
                "refinement of the success rate (ADR-096).",
            ),
            stat(
                "Task success ratio (1h)",
                ratio(
                    'harness_task_outcomes_total{outcome="success"}',
                    "harness_task_outcomes_total",
                ),
                unit="percentunit",
                description="Compare with 'Success ratio' in row 0. That one "
                "says the loop reached an answer; this one says the answer "
                "was right. The gap between them is agent quality, and it is "
                "the number a dashboard of infrastructure metrics can never "
                "show. Only counts runs somebody judged.",
                thresholds=[("red", None), ("orange", 0.7), ("green", 0.9)],
            ),
            panel(
                "Why tasks failed",
                increase(
                    "harness_task_outcomes_total", "reason", '{outcome="failure"}'
                ),
                legend="{{reason}}",
                unit="short",
                description="A bounded vocabulary, so it stays a legend: "
                "wrong_output, incomplete, tool_failure, permission, "
                "infrastructure, timeout, guard_stopped. wrong_output rising "
                "while row 0 stays green is the case worth acting on.",
            ),
            stat(
                "Telemetry spans dropped",
                "sum(harness_telemetry_spans_dropped)",
                unit="short",
                description="Spans an exporter refused or lost. Should be "
                "zero. Published rather than swallowed because a telemetry "
                "layer that quietly stops recording is worse than one that "
                "is visibly off — every panel above would keep looking "
                "healthy.",
                thresholds=[("green", None), ("orange", 1.0)],
            ),
        ],
    ),
]


def layout(groups: list[tuple[str, list[dict[str, Any]]]]) -> list[dict[str, Any]]:
    """Assign ids and grid positions: a row header, then its panels two abreast."""
    panels: list[dict[str, Any]] = []
    next_id = 1
    y = 0
    for title, members in groups:
        panels.append(
            {
                "type": "row",
                "title": title,
                "id": next_id,
                "collapsed": False,
                "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
                "panels": [],
            }
        )
        next_id += 1
        y += 1
        for index, member in enumerate(members):
            column = index % 2
            if index and column == 0:
                y += HEIGHT
            member["id"] = next_id
            member["gridPos"] = {"h": HEIGHT, "w": WIDTH, "x": column * WIDTH, "y": y}
            panels.append(member)
            next_id += 1
        y += HEIGHT
    return panels


def build() -> dict[str, Any]:
    """Assemble the dashboard document."""
    return {
        "uid": "agent-harness",
        "title": "Agent Harness",
        "description": "Week 3 observability (§8.4): one group per required "
        "bullet. Generated by scripts/build_dashboard.py from the frozen "
        "metric names; edit that script, not this file.",
        "tags": ["harness", "week3"],
        "timezone": "browser",
        "editable": False,
        "graphTooltip": 1,
        "refresh": "5s",
        "time": {"from": "now-15m", "to": "now"},
        "schemaVersion": 39,
        "version": 1,
        "panels": layout(GROUPS),
        "templating": {"list": []},
        "annotations": {"list": []},
    }


if __name__ == "__main__":
    OUTPUT.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT}")
