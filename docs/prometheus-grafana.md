# Prometheus and Grafana — what they are, and how this harness uses them

This note is for someone who has not used either product before. It
explains the software in general, then maps every piece onto *this*
repository, so you can talk through the Week 3 dashboard in an oral exam
or a demo without treating Grafana as a black box that “just shows graphs”.

The assignment’s §8.4 asks for **monitoring**: throughput, latency, success
and failure rates, LLM usage, tool usage, permission decisions, sub-agent
activity. We split that into two halves:

| Question | Tool | Lives in |
| --- | --- | --- |
| *How is the agent doing overall?* | **Metrics** (Prometheus + Grafana) | numbers that accumulate over time |
| *What did this one run do, step by step?* | **Traces** (JSONL files, `/trace`) | one file per run on the memory volume |

Grafana only draws the first half. Traces are a different file format and
a different UI (`/trace` in the REPL). Do not mix them up in the exam:
Grafana never opens a trace file.

---

## 1. The idea in one picture

Imagine the agent is a machine on a factory floor. You want a wall of
dials: how many tasks finished, how long InnKube took, how often a tool
failed. You do **not** want the machine to phone home. You want a
clipboard that walks over every few seconds, copies the numbers off the
machine’s display, and files them. A second person then draws charts from
the filing cabinet.

That is the whole stack:

```
  Agent process                         Clipboard              Filing cabinet         Wall of charts
  ─────────────                         ─────────              ──────────────         ──────────────
  does work                             Prometheus             Prometheus's           Grafana
  keeps running totals                  (pulls /metrics        own time-series        (reads Prometheus
  serves them at :9091/metrics           every 5 seconds)       database)              and draws panels)
```

Three programs, three jobs:

1. **The harness** *produces* numbers. It does not know Grafana exists.
2. **Prometheus** *collects and stores* those numbers over time.
3. **Grafana** *displays* them. It does not store the metrics itself.

If Prometheus is down, Grafana still opens — the panels are empty. If the
harness is not running, Prometheus still runs — it just records “target
down”. If Grafana is down, the numbers are still being stored; you can
look at them in Prometheus’s own UI at `http://localhost:9090`.

The harness is deliberately **not** dependent on either of them.
`docker run` works with both stopped. Observability is how you *watch* the
agent, not part of the work (ADR-081, ADR-083).

---

## 2. What Prometheus is

[Prometheus](https://prometheus.io/) is an **open-source monitoring
system**. It was built at SoundCloud, is now a CNCF project, and is the
default way a lot of cloud software is watched.

It does four things:

1. **Scrapes** HTTP endpoints that speak a simple text format.
2. **Stores** each number as a *time series* (a named value that changes
   over time, with optional labels).
3. **Lets you query** those series with a language called **PromQL**.
4. Optionally **alerts** when a query is true for too long. We do not use
   alerting in this lab.

### 2.1 Pull, not push

This is the design choice people trip over.

Most logging systems wait for the application to *push* a line (“I just
did X”). Prometheus does the opposite: **it pulls**. Your application
opens a small HTTP server and, when asked, dumps its current counters.
Prometheus, on a timer, does the equivalent of:

```bash
curl http://harness:9091/metrics
```

…and files whatever came back, stamped with “now”.

Consequences that matter here:

- The agent needs **no network egress** to be observable. It does not call
  Prometheus. It does not call Grafana. It just answers on port 9091.
- If nobody scrapes, the numbers still exist *inside the process* — they
  just vanish when the process exits. Prometheus is the memory that
  survives a container restart.
- A finished one-shot `docker run` that already exited has **nothing left
  to scrape**. That is why Grafana only fills while the REPL is up, and
  why the e2e tests hold a session open and scrape *during* the task.

You can see the raw dump yourself (harness running, port published):

```bash
curl -s localhost:9091/metrics | grep harness_
```

It looks like this:

```
# HELP harness_runs_total Agent runs completed, by how they ended.
# TYPE harness_runs_total counter
harness_runs_total{role="main",status="success"} 3.0
```

That is Prometheus’s **exposition format**. Grafana never reads this URL.
Only Prometheus does. Grafana then asks Prometheus, in PromQL, “what is
the rate of `harness_runs_total` over the last few minutes?”

### 2.2 Metric types (the three we use)

Prometheus has a small vocabulary. We use three types.

| Type | Everyday meaning | Example in this repo |
| --- | --- | --- |
| **Counter** | A number that only goes **up** (or resets to 0 when the process restarts). “How many times has X happened since start?” | `harness_runs_total`, `harness_llm_tokens_total` |
| **Gauge** | A number that can go **up and down**. “What is the value *right now*?” | `harness_active_runs` (1 while a task runs, 0 between tasks) |
| **Histogram** | Not one number: a set of **buckets**. “Of all the run durations, how many were under 1s? under 5s? under 30s?” | `harness_run_duration_seconds`, `harness_llm_latency_seconds` |

A counter is the wrong type for “currently active runs”: that would only
ever climb. A gauge is the wrong type for “total tokens billed”: you would
lose the history of how fast they were spent. A histogram is what you
need for latency, because “average latency” hides the slow calls; we graph
the **p95** (95th percentile) instead.

Default histogram buckets in `prometheus_client` top out at **10 seconds**.
A single InnKube call regularly takes longer than that, so every run would
land in `+Inf` and the graph would be useless. Our buckets go up to five
minutes: `0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300` seconds
(`harness/observability/recorder.py`).

### 2.3 Labels (and why `run_id` is forbidden)

A metric name alone is not enough. `harness_runs_total` is split by
**labels**: extra key/value tags that create separate series.

```
harness_runs_total{role="main", status="success"}  3
harness_runs_total{role="main", status="error"}    0
harness_runs_total{role="researcher", status="success"} 1
```

Those are three time series, not one. Grafana can then draw “success vs
error” or “main vs researcher” without us inventing extra metric names.

**Cardinality** is the catch. Prometheus stores *one series per unique
combination of labels*. If we labelled every metric with `run_id=abc-123`,
every task would mint a new series forever. The database would grow
without bound and become useless within a day. That is why **`run_id` is
never a label** (ADR-081). Aggregates live in Prometheus; the story of one
run lives in the JSONL trace.

### 2.4 PromQL, very briefly

Prometheus’s query language is **PromQL**. Grafana panels are just PromQL
saved as JSON.

A few shapes we actually use:

| PromQL | Meaning |
| --- | --- |
| `sum(harness_active_runs)` | add every series of that gauge (all roles) |
| `rate(harness_runs_total[5s])` | how fast that counter is climbing, per second |
| `sum(rate(...[$__rate_interval])) by (role) * 60` | same idea, per minute, split by role (`$__rate_interval` is Grafana filling in the graph’s step) |
| `histogram_quantile(0.95, sum(rate(harness_llm_latency_seconds_bucket[$__rate_interval])) by (le, job))` | “95% of LLM calls were faster than this” |

You do not need to write PromQL by hand. The queries are generated in
`scripts/build_dashboard.py`. You *do* need to know that empty panels
usually mean “Prometheus has no samples”, not “the query is fancy”.

### 2.5 What Prometheus looks like in the browser

Open **http://localhost:9090** (no login).

Useful pages during a demo:

- **Status → Target health** (or `/targets`): is `harness` **UP** or
  **DOWN**? If DOWN, Grafana will be empty. The error we hit in the lab
  was `lookup harness: no such host` — Prometheus could not even open
  `/metrics`.
- **Graph**: type `harness_runs_total` and Execute. You should see the
  same numbers `curl` showed, now with a timeline.

Prometheus’s UI is ugly on purpose. It is an engineer’s debugger.
Grafana is the thing you show a TA.

---

## 3. What Grafana is

[Grafana](https://grafana.com/) is an **open-source dashboarding**
application. It does not collect metrics. It does not store them (in our
setup). It is a web app that:

1. Knows how to talk to a **datasource** (here: Prometheus).
2. Draws **panels** (a number, a line chart, a bar chart, …) from queries.
3. Groups panels into a **dashboard**.
4. Refreshes on a timer so the wall of charts moves while you watch.

Analogy: Prometheus is the spreadsheet. Grafana is the chart view. If you
change the spreadsheet, the chart updates. If you delete Grafana, the
spreadsheet is still there.

### 3.1 Datasource

A datasource is “where Grafana should ask for numbers”. Ours is defined
in git, not clicked together in the UI:

`observability/grafana/provisioning/datasources/prometheus.yml`

```yaml
datasources:
  - name: Prometheus
    uid: prometheus          # the dashboard JSON refers to this exact uid
    type: prometheus
    access: proxy            # Grafana's server queries Prometheus, not your browser
    url: http://prometheus:9090
```

`access: proxy` means: your browser talks only to Grafana
(`localhost:3000`). Grafana’s container then talks to Prometheus on the
Docker network as `http://prometheus:9090`. You never have to expose
Prometheus for the dashboard to work — we publish 9090 anyway so you can
debug targets.

The `uid` is **fixed**. If Grafana minted a random uid on first start,
every panel would point at a datasource that does not exist, and you
would see “No data” for a completely different reason than a scrape
failure.

### 3.2 Provisioning (“dashboards as code”)

You *can* build a dashboard by clicking in the Grafana UI and exporting
JSON. We do not. A hand-clicked export is two thousand lines in which a
typo in a metric name is invisible. Instead:

1. `scripts/build_dashboard.py` describes each panel in Python (title,
   PromQL, description).
2. It writes `observability/grafana/provisioning/dashboards/harness.json`.
3. Grafana, at container start, **loads every JSON file** in that folder
   (`dashboards.yml` tells it to).
4. `tests/unit/test_observability_stack.py` fails if the committed JSON
   is not exactly what the script produces, or if a query names a metric
   that is not on the frozen list.

So a TA gets the seven panel groups with **no Import dialog**. Edit the
script, run it, commit the JSON. Do not edit `harness.json` by hand.

Login is `admin` / `admin`. That is a lab default on a laptop loopback;
do not publish port 3000 beyond that. Grafana lands on this dashboard
because compose sets:

```
GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH=.../harness.json
```

Refresh is **5 seconds**, matching Prometheus’s scrape interval, so the
graphs move during a live demo.

### 3.3 Panel types we use

- **Stat** — one number (Active runs, Success ratio).
- **Time series** — a line over the last 15 minutes (the default window).
- **Row** — a collapsible header. One row per §8.4 bullet, so a marker
  can skip what they are not asking about.

Each panel has a **description** (the `i` icon). That text is the “why
would I look at this”, not a restatement of the query.

---

## 4. How we wire them in *this* repository

### 4.1 Three containers, one laptop

`compose.yaml` defines the stack. Images are **pinned** (not `:latest`),
same rule as Qdrant: demo day must be the versions we tested.

| Compose service | Image | Host port | Job |
| --- | --- | --- | --- |
| `harness` | `agent-harness` (our image) | **9091** | agent + `/metrics` exporter |
| `prometheus` | `prom/prometheus:v3.2.1` | **9090** | scrape and store |
| `grafana` | `grafana/grafana:11.6.0` | **3000** | dashboards |
| `qdrant` | `qdrant/qdrant:v1.15.4` | 6333 | memory (unrelated to graphs) |

Grafana `depends_on` Prometheus. The harness **does not** depend on
either. `docker compose run harness` must not wait for Grafana.

Start it:

```bash
docker compose up -d qdrant prometheus grafana
docker compose run --build --rm --service-ports harness
```

`--service-ports` matters. `docker compose run` **ignores** the `ports:`
block unless you pass that flag. Without it, `localhost:9091` is closed
on your Mac and Prometheus (which we pointed at the host — see §6)
cannot scrape.

### 4.2 The data path, hop by hop

```
  you type a task
        │
        ▼
  AgentLoop  ──lifecycle callbacks──►  TelemetryHook (opens spans)
        │                                    │
        │                                    ▼
        │                             Tracer ──► JSONL / OTLP exporters
        │                                    │
        │                                    ▼
        │                             MetricsSpanExporter
        │                                    │
        │                                    ▼
        │                             PrometheusRecorder
        │                             (counters / gauges / histograms
        │                              in process memory)
        │                                    │
        │                                    ▼
        │                             HTTP :9091/metrics
        │                             (daemon thread)
        │                                    ▲
        │                                    │  scrape every 5s
        │                                    │
        │                             Prometheus
        │                             (stores samples)
        │                                    ▲
        │                                    │  PromQL
        │                                    │
        ▼                             Grafana :3000
  you see the answer                  you see the graphs
```

Nothing in `loop.py` knows the word Prometheus. The loop already fires
hook callbacks (`on_run_start`, `before_llm`, `after_tool`, …).
`TelemetryHook` implements that same `Hook` protocol and opens a span for
each phase; when a span closes, the `Tracer` hands it to every exporter, and
`MetricsSpanExporter` turns it into counter and histogram updates. The trace
file and the counters are fed from the same span, so they cannot disagree
about a run (ADR-093). The only extra line in the loop is `after_permission`,
because a denial inferred from observation text cannot be told apart
from a tool that failed by itself (ADR-081).

`harness/observability/` is the **only** package that imports
`prometheus_client`. Memory, MCP, sub-agents, the loop — none of them
talk to Prometheus.

### 4.3 Files that must agree

Four files, four programs, no compiler between them. If they drift, the
symptom is an empty panel, not a crash. Tests pin the wiring
(`tests/unit/test_observability_stack.py`, `tests/unit/test_span_metrics.py`).

| File | Who reads it | What it must match |
| --- | --- | --- |
| `config.yaml` → `metrics_port: 9091` | harness | the listen port |
| `compose.yaml` → `9091:9091` on `harness` | Docker | publishes that port |
| `observability/prometheus.yml` → `harness:9091` | Prometheus | scrape target |
| `scripts/build_dashboard.py` → metric names | Grafana (via generated JSON) | frozen names in `recorder.py` |

### 4.4 Inside the harness: recorder, hook, server

The pieces, one job each (ADR-081, ADR-093):

**`MetricsRecorder`** — a Protocol of methods named after *events*, not
after counters: `run_started`, `llm_call`, `tool_finished`. Callers never
see a `Counter` object.

**`PrometheusRecorder`** — the real implementation. It owns a
`CollectorRegistry` so tests (and a second session in one process) do not
share numbers. Prometheus refuses to register the same metric name twice;
a module-level global would make the second test crash on import.

**`NullRecorder`** — same methods, empty bodies. `metrics_enabled: false`
swaps this in. The hook has **no `if metrics:` branch**. A hook that
checks a flag on every call is a hook that will eventually check it
wrong.

**`TelemetryHook`** — implements `Hook`. It opens a span in `before_*`
and closes it in `after_*`; the span carries its own start and end, so
there are no stopwatches anywhere else. If an `after_llm` arrives without a
matching `before_llm`, it records duration 0 rather than raising: a gap
in a graph is acceptable, a failed task because a graph was unavailable
is not. Its open-span stack is per thread, because concurrent sub-agents
share it (ADR-106).

**`MetricsSpanExporter`** — receives each finished span and calls the
recorder method named after it. The only place spans become counters.

**`serve_metrics`** — `prometheus_client.start_http_server` on a **daemon
thread**. A busy port is a **banner line**, not a crash:

```
Metrics:   http://localhost:9091/metrics
Metrics:   port 9091 unavailable (Address already in use)
Metrics:   disabled
```

Same degradation rule as a missing Qdrant (ADR-069). The agent still
answers questions.

### 4.5 Frozen metric names

Twenty-five names: the fourteen frozen in Week 3, below, and eleven added
with the span model (ADR-094), listed after them. The dashboard queries them as **strings**, so a rename
that compiles and passes every Python test still blanks a panel. They are
pinned in both directions: every name present, no name nobody agreed to.

| Metric | Type | Labels | What it answers |
| --- | --- | --- | --- |
| `harness_active_runs` | gauge | `role` | is it busy right now |
| `harness_runs_total` | counter | `status`, `role` | throughput, success vs failure |
| `harness_run_duration_seconds` | histogram | `role` | how long a task takes |
| `harness_llm_requests_total` | counter | `role` | model calls |
| `harness_llm_tokens_total` | counter | `direction` | token consumption (`prompt` / `completion`) |
| `harness_llm_latency_seconds` | histogram | — | where the wall-clock time goes |
| `harness_tool_calls_total` | counter | `tool`, `ok`, `kind` | which tools, and do they work |
| `harness_tool_latency_seconds` | histogram | `kind` | builtin vs MCP vs memory vs skill vs delegate |
| `harness_permission_decisions_total` | counter | `outcome` | how often the gate says no |
| `harness_memory_ops_total` | counter | `op` | memory reads and writes |
| `harness_context_chars` | histogram | `role` | how close to the context budget |
| `harness_context_compactions_total` | counter | `role` | when old observations were dropped |
| `harness_subagent_runs_total` | counter | `role`, `status` | delegation activity |
| `harness_subagent_duration_seconds` | histogram | `role` | delegation cost |

Added with the span model:

| Metric | Type | Labels | What it answers |
| --- | --- | --- | --- |
| `harness_llm_errors_total` | counter | `error_type`, `role` | model calls that failed instead of returning |
| `harness_llm_retries_total` | counter | `error_type` | transient failures the client absorbed |
| `harness_errors_total` | counter | `layer`, `error_type`, `component` | every failure, by the layer responsible (dashboard row 0) |
| `harness_mcp_calls_total` | counter | `server`, `ok` | which MCP server, and does it work |
| `harness_memory_operation_seconds` | histogram | `op` | time inside one memory operation |
| `harness_memory_items_returned` | histogram | `op` | how much one retrieval returned |
| `harness_run_iterations` | histogram | `role` | reason-act cycles per finished run |
| `harness_run_tool_calls` | histogram | `role` | tool calls per finished run |
| `harness_repeated_tool_calls_total` | counter | `tool` | byte-identical repeats, the no-progress signal |
| `harness_task_outcomes_total` | counter | `outcome`, `reason` | quality verdicts, separate from run success (ADR-096) |
| `harness_telemetry_spans_dropped` | gauge | — | spans an exporter lost; non-zero means the graphs undercount |

Label values worth memorising:

- `kind`: `builtin` \| `mcp` \| `memory` \| `skill` \| `delegate`
- `status`: `success` \| `error` \| `no_progress` \| `capped`  
  `capped` and `no_progress` are **guards working**, not the agent
  failing. Filing them under “errors” would misreport the first number a
  TA reads.
- `outcome`: `allow` \| `deny` \| `confirmed` \| `declined`  
  Four values for the policy’s three, because “a person said yes” and “a
  person said no” are different events from a silent deny.
- `role`: `main`, or a specialist (`researcher`, `writer`, `critic`).

`classify_tool` in `harness/observability/telemetry.py` derives `kind` from the name: `delegate` is
exact, `memory_*` / `skill_*` by prefix, MCP by the `__` separator
(ADR-042), everything else `builtin`.

---

## 5. What the dashboard actually shows

Rows 1–7 are the brief’s seven groups, **in the brief’s order**. Row 0
comes first because it is the panel you open when something is wrong, and
rows 8–11 are extensions, numbered outside 1–7 so the mapping stays
checkable.

0. **Is anything wrong?** — failures by responsible layer
   (`harness_errors_total`): the model, a tool, an MCP server, memory, the
   permission gate, or the harness itself.

1. **Throughput and active runs** — `Active runs` (gauge, should return
   to 0 between tasks) and runs completed per minute by role.
2. **Latency** — p95 of the whole run, of InnKube, of tools by kind, of
   sub-agent runs. If run p95 is high, check LLM latency first: that is
   usually where the time went (you saw this live when `hello?` sat on
   “thinking”).
3. **Success and failure rates** — success ratio over 1h, and runs split
   by `status` so `capped` is visible next to `error`, not folded into it.
4. **LLM usage and tokens** — requests per minute, tokens per minute by
   `prompt` vs `completion`. Prompt usually dominates; that is context,
   not the model’s own words.
5. **Tool usage** — by kind, by tool name, failures (`ok="false"`), and
   memory operations. A permission *refusal* is **not** a failed tool; it
   is counted in group 6.
6. **Permission decisions** — allow / deny / confirmed / declined, plus a
   1h count of denied-or-declined. In `allow_all` this stays quiet; in
   `confirm_mutations` a healthy session *does* deny things on purpose.
7. **Sub-agent activity** — delegated runs by role and outcome, and how
   many child loops are running now.
8. **Failure diagnosis** (ours) — error types, the model request failure
   ratio, LLM failures by kind and retries per minute.
9. **Context window** (ours) — character budget p95, and compactions per
   minute. Compaction is otherwise invisible: it removes evidence without
   failing anything (ADR-114).
10. **Long-term memory** (ours) — operations, latency p95 and how many
    records a read returned.
11. **Task quality** (ours) — verdicts recorded against runs, kept apart
    from run success: a run can answer and still not do what was asked
    (ADR-096). This row also shows dropped telemetry spans.

---

## 6. A full walk-through of one task

You type `create a folder called notes`.

1. The REPL calls `AgentLoop`. `TelemetryHook.on_run_start` opens the
   `agent.run` span →
   `active_runs{role="main"}` becomes **1**. Grafana’s “Active runs” flips
   from 0 to 1 on the next 5-second refresh.
2. `before_llm` prints `… thinking (iteration 1)` (or animates that line
   on a TTY) and stamps the time.
3. InnKube answers. `after_llm` observes the duration and token counts →
   `harness_llm_requests_total`, `harness_llm_tokens_total`,
   `harness_llm_latency_seconds`, plus context-size / compaction metrics.
4. The model calls `create_folder`. The permission policy says `allow`
   (`after_permission`). Then the tool runs. `after_tool` increments
   `harness_tool_calls_total{tool="create_folder", ok="true", kind="builtin"}`
   and records latency under `kind="builtin"`.
5. The model returns a final answer. `on_run_end` sets
   `active_runs` back to **0**, increments
   `harness_runs_total{status="success", role="main"}`, observes run
   duration.

Meanwhile, every 5 seconds Prometheus has been `GET /metrics` and storing
samples. Grafana’s queries over `now-15m` now have points to draw.

If you `/exit`, the process dies, the exporter dies, Prometheus’s target
goes **DOWN**, and the counters in *process memory* are gone. The samples
Prometheus already stored remain until that container is removed (we do
not mount a Prometheus volume, so `docker compose down` forgets history —
fine for a demo).

---

## 7. Why Grafana was empty, and how scrape actually works here

Prometheus is configured with one job:

```yaml
# observability/prometheus.yml
scrape_configs:
  - job_name: harness
    static_configs:
      - targets: ["harness:9091"]
```

It therefore looks up the hostname **`harness`**. That name exists for
`docker compose up harness`. It does **not** exist for the documented
demo command `docker compose run harness`: Compose names the one-off
container `agent-harness-harness-run-<id>` and does not register the service
alias unless you pass `--use-aliases`.

Symptom:

```
Error scraping target: lookup harness on 127.0.0.11:53: no such host
```

Target health DOWN → PromQL returns nothing → Grafana panels blank, even
though `curl localhost:9091/metrics` already showed real numbers.

The fix (ADR-092) is on the **Prometheus** service, not the scrape file:

```yaml
extra_hosts:
  - "harness:host-gateway"
```

That puts `harness` in Prometheus’s `/etc/hosts`, pointing at the Docker
host. Combined with `--service-ports` (which publishes 9091 on the Mac),
the scrape becomes: Prometheus → host:9091 → harness container. One
target, no second job that would *double* every dashboard query.

Checklist when a panel is empty:

1. Is the harness still running? (`… thinking` or `You:` in the terminal.)
2. `curl -s localhost:9091/metrics | grep harness_` — if this fails, the
   exporter is not published (`--service-ports` missing, or port in use).
3. http://localhost:9090/targets — is `harness` **UP**?
4. Have you actually finished a task? Gauges move during a run; many
   counters only increment on `on_run_end`.
5. Grafana time range is last 15 minutes; a scrape that just started
   needs one interval (5s) before the first point exists.

---

## 8. Metrics vs traces (do not confuse them in the exam)

| | Metrics (Prometheus / Grafana) | Traces (`/trace`, JSONL) |
| --- | --- | --- |
| Question | How is it doing *in aggregate*? | What did *this run* do? |
| Identity | no `run_id` | one file per `run_id` |
| Where | port 9091 → Prometheus → Grafana | `{memory}/traces/<id>.jsonl` |
| Survives `/exit` | only samples already scraped | yes (memory volume) |
| Shows tool arguments | no | no (on purpose — would copy the workspace onto the memory volume) |
| Shows permission vs tool failure | `outcome` vs `ok="false"` as separate metrics | consecutive `permission` and `tool` lines, paired by the reader |

Both attach through the **same `Hook` seam**. Neither is a second runtime.
`loop.py` did not grow a “metrics branch” or a “trace branch”.

---

## 9. What you should be able to say out loud

A short version that is still accurate:

> The harness exposes Prometheus metrics on port 9091. It does not push
> anywhere — Prometheus scrapes `/metrics` every five seconds, so the
> agent needs no network egress to be observable. Twenty-five metric
> names are frozen; `run_id` is never a label, because that would explode
> cardinality. Grafana does not collect data: it is provisioned from git
> with Prometheus as its datasource, and the dashboard JSON is generated
> from those frozen names. Rows 1–7 match the seven bullets of §8.4, in
> order. The stack is two compose services the harness does not depend
> on. `TelemetryHook` sits on the existing lifecycle seam and opens spans;
> `MetricsSpanExporter` turns finished spans into counters;
> `prometheus_client` is imported only under `harness/observability/`.

If they ask “what if Grafana is down?”: the agent is unchanged; Prometheus
still stores samples; you can query `localhost:9090`.

If they ask “what if Prometheus is down?”: Grafana opens with empty
panels; `curl :9091/metrics` still works.

If they ask “why not push to Grafana?”: Grafana is not a metrics store in
this setup, and push would force the agent to know about the dashboard —
the opposite of a decoupled hook.

---

## 10. Commands cheat sheet

```bash
# stack
docker compose up -d prometheus grafana

# agent with the metrics port published
docker compose run --build --rm --service-ports harness

# raw numbers (harness must be up)
curl -s localhost:9091/metrics | grep harness_

# UIs
open http://localhost:3000          # Grafana  admin / admin
open http://localhost:9090/targets  # Prometheus scrape health

# regenerate dashboard after editing panels
python scripts/build_dashboard.py
```

Related code, if you want to read the source in this order:

1. `harness/observability/recorder.py` — what a metric *is*
2. `harness/observability/hook.py` — when it is incremented
3. `harness/observability/server.py` — how Prometheus fetches it
4. `observability/prometheus.yml` — scrape job
5. `scripts/build_dashboard.py` — what Grafana draws
6. ADR-081 (metrics), ADR-083 (dashboard as code), ADR-092 (scrape via
   the published port) in `decisions.md`
