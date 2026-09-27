# Observability

This document is the reference for the harness's telemetry: what the
architecture is, what every field means, what every metric answers, and how
to take a failed run and find out what happened.

`docs/prometheus-grafana.md` is the companion piece and explains what
Prometheus and Grafana *are* for someone who has not used them. This one
assumes you know and tells you what we built.

---

## 1. The shape of it

```
   the agent                       one instrumentation point        several sinks
   ─────────                       ─────────────────────────        ─────────────
   AgentLoop ──┐
   MemoryHook ─┼── lifecycle ────► TelemetryHook ──► Tracer ──┬──► JSONL file    (one per run)
   factory ────┘    callbacks                                 ├──► Prometheus    (/metrics :9091)
                                                              └──► OTLP          (optional, off)
```

One hook opens spans. Every exporter reads the same finished span. That is
the whole architecture, and the reason for it is that Week 3 had **two**
observers — one for metrics, one for the trace file — each with its own
stopwatches, each deriving tokens the same way, and each needing to be
edited to record a new field. The second copy was always one commit behind
(ADR-093).

Three properties fall out of the shape:

- **A new telemetry field is one attribute in one place.** Three exporters
  get it.
- **A dashboard and a trace file cannot disagree about a run**, because they
  read the same object.
- **Observability is decoupled from the loop.** `loop.py` gained two calls
  in this release: one to report a failed model request, one to pass the
  typed tool outcome it already had. Nothing else.

### Files

| File | What it is |
| --- | --- |
| `harness/observability/api.py` | `Span`, `Tracer`, the `Attr` vocabulary. **Stdlib only** — that is what lets `harness/tools/` and `harness/memory/` open a span without acquiring a metrics library (ADR-034). |
| `harness/observability/errors.py` | The bounded `error.type` vocabulary and the layer each one blames. |
| `harness/observability/redaction.py` | What is safe to record about a tool call. |
| `harness/observability/telemetry.py` | `TelemetryHook` — lifecycle callbacks in, spans out. |
| `harness/observability/exporters/` | `jsonl.py`, `metrics.py`, `otlp.py`. |
| `harness/observability/recorder.py` | The metric definitions; the only `prometheus_client` import in the harness. |
| `harness/observability/evals.py` | Task-quality verdicts and the signals derived from a trace. |
| `harness/observability/replay.py` | Reads a trace back as a tree. `/trace` and `python -m …replay`. |

---

## 2. The trace model

A run produces one tree. Here is a real one, from
`tests/integration/test_observability_e2e.py`, rendered by `/trace`:

```
run e488241e  main  2026-09-16 23:00:51  answered in 5ms

       5ms    agent.run [main] answered
       0ms      memory.read
       0ms      iteration 1  context 39 chars
       0ms        llm  1 tool call  120+20 tokens
       0ms        read_ledger  path=str[10]
       0ms          permission: allow
       0ms      iteration 2  context 176 chars
       0ms        llm  1 tool call  120+20 tokens
!      0ms        web__web_search via web  query=str[16]  [mcp.connection]
       0ms          permission: allow
       0ms      iteration 3  context 350 chars
       0ms        llm  1 tool call  120+20 tokens
!      0ms        write_brief  body=str[26]  [permission.denied]
       0ms          permission: deny (not run)
       2ms      iteration 4  context 652 chars
       0ms        llm  1 tool call  120+20 tokens  2 retries
       1ms        delegate  role=str[10] task=str[19]
       0ms          permission: allow
       1ms          subagent.run [researcher] answered
       0ms            iteration 1  context 39 chars
       0ms              llm  1 tool call  120+20 tokens
       0ms              read_ledger  path=str[10]
       0ms                permission: allow
       0ms            iteration 2  context 180 chars
       0ms              llm  final answer  90+18 tokens
       0ms      iteration 5  context 849 chars
       0ms        llm  final answer  200+40 tokens
       0ms      memory.write

  7 iterations, 7 model calls, 5 tool calls (2 failed)
  1,048 tokens (890 in, 158 out)
  time: model 0ms (3%), tools 1ms (11%), harness 5ms (86%)

diagnosis:
  failures by layer: mcp (1), policy (1)
  ! silent_failure: reported success while carrying failures underneath
  ! retry_storm: the endpoint was retried repeatedly
  ! permission_blocked: the policy refused work the agent tried to do
```

Read the gutter, not the indentation: the `!` column is fixed on the left so
failures are scannable without following the tree.

### Span names

| Span | When | Kind |
| --- | --- | --- |
| `agent.run` | One per top-level run. Its `trace_id` *is* the run id. | internal |
| `subagent.run` | One per delegated child, nested inside the parent's `tool.call`. | internal |
| `agent.iteration` | One per reason-act cycle. Derived from `before_llm`, so the loop was not touched. | internal |
| `llm.call` | One per model request, successful or failed. | **client** |
| `tool.call` | One per dispatched tool call. | client for MCP, internal otherwise |
| `permission.check` | One per decision, **inside** the call it governs, backdated to when dispatch began. | internal |
| `memory.read` / `memory.write` | Long-term memory, including the retrieval that goes through no tool call. | client |
| `agent.finalize` | A sub-agent's contract validation and repair. Only where it is real work. | internal |

**Why `agent.iteration` exists.** Without it a tool call sits *beside* a
reasoning step rather than inside one, so "it went wrong on iteration 7" is
an exercise in counting lines. With it, it is a subtree.

**Why `permission.check` is a child.** The Week 3 reader paired a permission
event with the tool event after it. That is correct until a model requests
two tool calls in one turn, at which point it attributes a decision to the
wrong call — silently. Now it is parentage.

---

## 3. Every telemetry field

Attributes are named in `harness/observability/api.py` as `Attr.*`
constants. Two families: `gen_ai.*` and `error.*` come from the
OpenTelemetry semantic conventions, so a viewer that understands LLM spans
renders ours without configuration; `harness.*` is everything the
conventions have no name for.

### Identity — on every span (inherited from the parent)

| Field | Meaning |
| --- | --- |
| `harness.run_id` | The run this span belongs to. Inherited, so `grep <run_id> traces/*.jsonl` returns the whole run. |
| `harness.parent_run_id` | The run that delegated this one; `null` for the top-level agent. |
| `harness.role` | `main`, or a specialist's role. |
| `harness.depth` | Delegation depth. `0` for the parent. |
| `harness.iteration` | Which reason-act cycle this happened in. |
| `harness.component` | Which subsystem opened the span: `agent`, `loop`, `llm`, `tool`, `permissions`, `memory`, `subagent`. |

### Model — on `llm.call`

| Field | Meaning |
| --- | --- |
| `gen_ai.system` | `innkube`. |
| `gen_ai.operation.name` | `chat`. |
| `gen_ai.request.model` | The configured model. Bounded, so it is safe as a label — and it is what makes "did p95 move because we switched models" answerable. |
| `gen_ai.request.temperature` | A comparison between two runs is meaningless without it. |
| `gen_ai.response.finish_reasons` | `stop`, `tool_calls`, `length`… **`length` means the answer was cut off mid-sentence**, which is indistinguishable from success on every other signal. |
| `gen_ai.usage.input_tokens` | Prompt tokens for *this* call — a difference from the context's cumulative totals, which is the one place usage is normalised across endpoints (ADR-031). `0` means the endpoint reported nothing, not that the call was free. |
| `gen_ai.usage.output_tokens` | Completion tokens, same convention. |
| `harness.llm.tool_calls` | How many tools this response asked for. |
| `harness.llm.final_answer` | `true` when the response carried no tool calls. **This is the field that separates system success from agent success**: a request returning 200 sets it `false` every time the model wanted another tool, which is normal; a *run* whose last call never sets it `true` never answered, however healthy each request was. |
| `harness.llm.retries` | Attempts the client retried through. Without it, three rate-limited attempts and one slow response are the same forty seconds. |

### Tools — on `tool.call`

| Field | Meaning |
| --- | --- |
| `harness.tool.name` | As the model called it, prefix included. |
| `harness.tool.kind` | `builtin`, `mcp`, `memory`, `skill`, `delegate`. Lets a panel ask "how much of this run was MCP" without naming every tool. |
| `harness.tool.args` | The **redacted digest** — `path=str[18] content=str[4213]`. Never values at the default setting. |
| `harness.tool.args_fingerprint` | A 12-character hash of the normalised arguments. Gives equality and nothing else, which is exactly what "is it calling the same thing over and over" needs. |
| `harness.tool.result_chars` | The observation's *length*, never its content. Answers "did it return anything" and "is this what blew up the context". |
| `harness.tool.failure_streak` | How many times this exact call has repeated in a row. |
| `harness.mcp.server` | Which configured server. Bounded by `config.yaml`, so safe as a label — this is what makes "which MCP server is responsible" one query instead of string surgery. |
| `harness.mcp.tool` | The unprefixed name the server itself advertises. |

### Permissions — on `permission.check`

| Field | Meaning |
| --- | --- |
| `harness.permission.decision` | `allow`, `deny`, `confirmed`, `declined`. Four values for the policy's three outcomes: `require_user_confirmation` splits by what the person said. |
| `harness.permission.mode` | The active mode, so a denial reads as policy rather than mystery. |
| `harness.permission.user_confirmed` | `true`/`false` only when somebody was asked. **Absent is not `false`** — "we never asked" and "they said no" are different facts. |

The span itself **succeeds** even for a denial: the policy worked. The
`tool.call` it refused is the thing that fails.

### Memory — on `memory.read` / `memory.write`

| Field | Meaning |
| --- | --- |
| `harness.memory.operation` | `inject`, `append`, `search`, `store`. |
| `harness.memory.items` | Records returned. Zero on every read means an empty collection or a changed embedder dimension — both of which look like "memory works" everywhere else, because the calls succeed. |
| `harness.memory.backend` | Which store and which embedder, so a silent fallback is visible. |

### Sub-agents — on `subagent.run` / `agent.finalize`

| Field | Meaning |
| --- | --- |
| `harness.subagent.role` | Which specialist. |
| `harness.subagent.result_status` | `valid` / `invalid` against the declared output contract. |
| `harness.subagent.repair_attempts` | How many restatements were spent. |

### Outcome — on `agent.run` / `subagent.run`

| Field | Meaning |
| --- | --- |
| `harness.exit_reason` | `success`, `capped`, `no_progress`, `error`. Set by the loop at each terminal return, so telemetry never has to infer it (ADR-084). |
| `harness.count.*` | Run totals: `iterations`, `llm_calls`, `tool_calls`, `tool_failures`, `repeated_calls`, `permission_refusals`, `llm_retries`, `compactions`. |
| `error.type` | Why it failed, from the bounded vocabulary. |
| `harness.error.message` | Short free text, truncated to 200 characters. **No metric reads this.** |

### Context — on `agent.iteration`

`harness.context.chars` and `harness.context.compactions`. Measured at
`before_llm` because that is the one moment the context *is* what is about
to be sent — the loop compacts immediately before (ADR-032, ADR-114).

---

## 4. Error types and layers

Every failure carries an `error.type` from a closed list, and every type
maps to a **responsible layer**. The layer is the one that earns its keep:
the question is never "what was the exception" but "is this my agent, my
tools, my policy, or the model".

| Layer | Error types | What it means |
| --- | --- | --- |
| `model` | `llm.timeout`, `llm.rate_limited`, `llm.unavailable`, `llm.bad_request`, `llm.auth`, `llm.malformed_response`, `llm.error` | The endpoint. |
| `tool` | `tool.not_found`, `tool.path_escape`, `tool.access_denied`, `tool.timeout`, `tool.failed` | A local tool, or what the world did to it. |
| `mcp` | `mcp.connection`, `mcp.tool_error`, `mcp.timeout` | A remote server. |
| `policy` | `permission.denied`, `permission.declined` | The permission layer **working**. |
| `memory` | `memory.unavailable`, `memory.embedding`, `memory.failed` | Long-term memory. Degrades a run rather than failing it (ADR-069). |
| `subagent` | `subagent.invalid_output`, `subagent.failed` | A child's result did not meet its contract. |
| `guard` | `guard.iteration_cap`, `guard.no_progress`, `guard.tool_retired` | The harness protecting itself. **Not a fault.** |
| `agent` | `tool.unknown`, `tool.invalid_arguments`, `subagent.unknown_role`, `agent.incomplete` | The model's reasoning. |
| `harness` | `harness.internal` | Us. |

Two attributions look wrong at first and are deliberate:

- **`tool.unknown` and `tool.invalid_arguments` blame the agent.** The tool
  layer behaved correctly by refusing a name that does not exist or
  arguments that do not fit the schema. The model chose badly.
- **`guard.*` is excluded from "failures by layer" in the diagnosis.** The
  iteration cap doing its job is not somebody's fault, and counting it would
  answer "whose fault" with "ours, for having a safety net".

---

## 5. Metrics

Twenty-five, on `:9091/metrics`. The first fourteen are frozen from Week 3
and the dashboard queries them by name; the rest arrived with the span
rewrite. Each exists because a question could not be answered without it —
`tests/unit/test_span_metrics.py` pins the whole list in both directions.

### The originals (frozen, ADR-081)

| Metric | Type | Labels | Question |
| --- | --- | --- | --- |
| `harness_active_runs` | gauge | role | How many runs are in flight? |
| `harness_runs_total` | counter | status, role | Throughput, and how runs end. |
| `harness_run_duration_seconds` | histogram | role | How long does a run take? |
| `harness_llm_requests_total` | counter | role | How often do we call the model? |
| `harness_llm_tokens_total` | counter | direction | What are we spending? |
| `harness_llm_latency_seconds` | histogram | — | How slow is the endpoint? |
| `harness_tool_calls_total` | counter | tool, ok, kind | Which tools, and do they work? |
| `harness_tool_latency_seconds` | histogram | kind | Which *kind* of tool is slow? |
| `harness_permission_decisions_total` | counter | outcome | What is the policy doing? |
| `harness_memory_ops_total` | counter | op | Is memory being used? |
| `harness_context_chars` | histogram | role | How close to the budget do runs get? |
| `harness_context_compactions_total` | counter | role | How often does the model lose evidence? |
| `harness_subagent_runs_total` | counter | role, status | Is delegation used, and does it work? |
| `harness_subagent_duration_seconds` | histogram | role | Is delegating saving time or adding a hop? |

### Added with the span rewrite

| Metric | Type | Labels | Question |
| --- | --- | --- | --- |
| `harness_errors_total` | counter | layer, error_type, component | **Which layer is failing?** The most useful metric here. |
| `harness_llm_errors_total` | counter | error_type, role | Requests that never returned. Before this, they incremented nothing. |
| `harness_llm_retries_total` | counter | error_type | Is the endpoint making runs wait rather than think? |
| `harness_mcp_calls_total` | counter | server, ok | Which MCP server is responsible for a failure? |
| `harness_memory_operation_seconds` | histogram | op | Is memory slow? |
| `harness_memory_items_returned` | histogram | op | Is retrieval returning anything? |
| `harness_run_iterations` | histogram | role | How much thinking does a run take? |
| `harness_run_tool_calls` | histogram | role | How many tools does an average run invoke? |
| `harness_repeated_tool_calls_total` | counter | tool | Are agents repeatedly calling the same failing tool? |
| `harness_task_outcomes_total` | counter | outcome, reason | Did the run do what was asked? |
| `harness_telemetry_spans_dropped` | gauge | — | Is the telemetry layer itself losing data? |

### Cardinality

`run_id` is **never** a label, and neither is a path, a prompt, a message or
a fingerprint. Prometheus keeps one time series per label combination, so
any one of those makes the database unusable within a day — and the damage
is in the storage by the time anyone notices. Per-run detail lives in the
trace, which is a different tool for a different question.

Every label value comes from a bounded vocabulary: the role catalogue, five
tool kinds, the configured server names, the error types above.
`MetricsSpanExporter._label` clips anything to 64 characters as a last
defence, and `test_span_metrics.py` asserts the forbidden names never appear.

---

## 6. Debugging a failed run

The workflow, in the order you would actually do it.

### Step 1 — which layer?

Open **row 0, "Failures by layer"**. Nine legend entries. If the answer is
`model` or `mcp`, it is infrastructure and rows 3 and 5 have the detail. If
it is `agent`, no component failed and the problem is reasoning — no amount
of infrastructure work will help.

### Step 2 — which run?

```bash
python -m harness.observability.replay --failures
```

Runs that did not answer, or that answered while carrying failures
underneath. That second case is the valuable one and nothing else surfaces
it.

### Step 3 — what happened?

```bash
python -m harness.observability.replay 9b06 --slowest 5
```

Or `/trace 9b06` in the REPL. You get the tree above, the totals, the time
split, and the diagnosis block.

### Step 4 — read the diagnosis

The distinction §9 of the brief asks for is made explicitly:

**Something broke.**

```
diagnosis:
  failures by layer: mcp (1), policy (1)
  most failures: web__web_search (3)
  ! unreliable_tool: one tool accounted for most failures
```

A layer is named. Go and fix it.

**Nothing broke, and the task still failed.**

```
diagnosis:
  no component reported a failure — the run ended without an
  answer despite every call succeeding
  the run was stopped by a guard, not by an error
```

Every call succeeded. This is a prompt or tool-description problem.

**The dangerous middle case.**

```
diagnosis:
  failures by layer: mcp (1)
  ! silent_failure: reported success while carrying failures underneath
```

The agent answered *anyway*, so every aggregate says success. This is the
run worth finding, and `silent_failure` is the only thing that points at it.

### Step 5 — record the verdict

```
/eval fail permission the brief was never written: write_brief was denied
```

Stored against the run id, beside the trace, and counted in
`harness_task_outcomes_total`.

### The cases, distinguished

| What the trace shows | Diagnosis |
| --- | --- |
| `llm.call` failed, `error.type: llm.rate_limited` | Endpoint capacity. Check retries. |
| `llm.call` succeeded, `tool.call` failed `tool.invalid_arguments` | Malformed tool call — the *model*, not the tool. |
| `tool.call` failed `mcp.connection`, `harness.mcp.server: web` | That server died. |
| `permission.check` `deny` → `tool.call` `permission.denied` | The policy, not a failure. |
| Same `args_fingerprint` repeatedly, `failure_streak` rising | Thrash. The model cannot tell its call failed. |
| `guard.tool_retired` | A tool failed `max_tool_failures` times and was retired. |
| Everything `ok`, `exit_reason: no_progress` | The agent chose badly. |
| Everything `ok`, `finish_reason: length` | The answer was **truncated**. Looks like success everywhere else. |

---

## 7. Running the stack

```bash
# Metrics and dashboards
docker compose up -d prometheus grafana
docker compose run --rm --service-ports harness

# http://localhost:3000   Grafana (admin/admin), dashboard is the home page
# http://localhost:9090   Prometheus, including the Alerts tab
# http://localhost:9091/metrics   the raw exposition
```

Traces need nothing running:

```bash
python -m harness.observability.replay                    # list
python -m harness.observability.replay last --slowest 5   # the last run
python -m harness.observability.replay --failures         # only the bad ones
python -m harness.observability.replay --dir ./copied-traces --json
```

Optional trace viewer:

```bash
./run.sh --tracing        # or: docker compose --profile tracing up -d jaeger, OTLP_ENABLED=true
# every answer then prints:  trace: http://localhost:16686/trace/<run id>
```

Settings, all in `config.yaml`:

| Setting | Default | Effect |
| --- | --- | --- |
| `metrics_enabled` / `metrics_port` | `true` / `9091` | The `/metrics` exporter. A closed port is a banner line, never a crash. |
| `traces_enabled` / `traces_dirname` | `true` / `traces` | The JSONL files, under `memory_path`. |
| `telemetry_redaction` | `shapes` | `strict`, `shapes`, or `values`. The banner says when it is not the default. |
| `otlp_enabled` / `otlp_endpoint` | `false` / `:4318` | OTLP export. Off three ways over. Inside compose the endpoint defaults to `http://jaeger:4318`. |
| `otlp_ui_url` | `http://localhost:16686` | Where the browser opens the viewer; the per-answer trace link is built from it. |

---

## 8. What is deliberately not here

- **No LLM-as-a-judge.** A model scoring its own harness adds an inference
  call, its own failure modes and its own cost, for a number nobody can
  check. The end-to-end tests already know the right answer for the tasks
  they run, so they are the better judge (ADR-096).
- **No Langfuse or Phoenix integration.** Both accept OTLP, so anyone who
  wants one points `otlp_endpoint` at it and changes no code. Adding a
  vendor SDK for a capability we already have would be coupling for nothing
  (ADR-095).
- **No sampling.** An agent run is rare, expensive and individually
  interesting. Dropping one in ten is how the run somebody wants to debug
  goes missing.
- **No prompts or tool output in telemetry, at any setting.** Sizes and
  shapes only.
- **No `run_id` label on any metric.** See §5.

## 9. Tests

| File | What it defends |
| --- | --- |
| `tests/unit/test_span_trace.py` | Tree shape, nesting, failures, redaction, and that an observer can never fail a run. |
| `tests/unit/test_span_metrics.py` | The frozen names, label cardinality, and the span→counter translation. |
| `tests/unit/test_trace_reader.py` | The tree is read not guessed; v1 traces still open; the quality store. |
| `tests/unit/test_error_taxonomy.py` | Every branch of the classifier — each one is an attribution claim. |
| `tests/unit/test_otlp_export.py` | The translation, and the three ways it degrades. |
| `tests/unit/test_replay_cli.py` | Reading a trace with no agent running. |
| `tests/unit/test_observability_stack.py` | compose / prometheus.yml / dashboard agree. |
| `tests/integration/test_observability_e2e.py` | **One run, every mechanism**, reconstructed from the file alone. |
