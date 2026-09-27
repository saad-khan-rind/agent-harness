# Agent Harness — Project Guidelines

Python **3.12**. Container: `python:3.12-slim-bookworm`. If a change conflicts
with this file, update the file and log the reason in `decisions.md`.

## Python

- PEP 8, line length 88. **Ruff** formats and lints; **mypy** type-checks.
  Do not debate formatting in review.
- `snake_case` functions/modules, `PascalCase` classes, `UPPER_SNAKE_CASE`
  constants. Prefer full names (`workspace_root`) over abbreviations.
- Type hints on every public function, method, and attribute. Use `list[str]`,
  `str | None` — not `Optional`/`List` from `typing`. Always annotate
  `-> None`. Use `Protocol` / `ABC` at module boundaries (LLM, tools, memory,
  MCP, hooks).
- Keep `loop.py` free of concrete tools, MCP, and memory. New capabilities
  register through `ToolRegistry`, `Hook`, or `PermissionPolicy`.
- Every tool call is decided by a `PermissionPolicy` in `AgentLoop._execute`,
  which is the one place every call passes through (ADR-057). Do not add a
  second check anywhere else, and do not let a tool police itself — §8.4 asks
  for one consistent mechanism, not special cases. Asking a human goes
  through the `Confirmer` seam, which the CLI implements, because `print` and
  `input` stay in the terminal front end: `cli.py` (the REPL and `main`),
  `console.py` (trace, spinner, approval prompt) and `session.py` (startup
  notices). `tests/unit/test_seams.py` enforces it.
- Dispatch returns `ToolOutcome(text, ok)`. Read `outcome.ok`; never
  re-derive failure from the `Error:` prefix (ADR-058). The prefix is for the
  model, the boolean is for the code.
- The loop must stay bounded. There is no fixed iteration cap by default
  (ADR-047); a tool that fails `max_tool_failures` times in a row is retired
  for the task (ADR-048); and `max_repeated_calls` identical calls in a row —
  same tool, same arguments, same observation — end the run as making no
  progress (ADR-049). Any change that removes a guard needs another one in
  its place, and an ADR.
- CLI only. No GUI. No `print` in library code.

Before a snapshot, run the gate — one script, so it cannot drift from
what CI runs (ADR-028):

```bash
./scripts/ci-gate.sh
```

Or in the container, if you have no local Python 3.12:

```bash
docker compose run --rm tests
```

Coverage runs as part of `pytest` and the gate fails below 85% (ADR-029,
amended by ADR-101). Do not lower the threshold to make a change pass —
add the test.

## Docs, comments, and decisions

- Comment **everything that is not obvious from the name**: every module,
  class, and public function gets a Google-style docstring. Private helpers
  get a one-liner. Add short inline comments on non-trivial steps (control
  flow, safety checks, error handling, why a default was chosen).
- Comments explain **why** this code exists and what it is for, not a
  restatement of the next line — and not its history. "This used to…" and
  "the first version…" belong in the commit message and the ADR; in the
  code they go stale and bury the reason.
- **Decisions are discussed, then written down.** Do not merge an
  architectural or safety choice until the team has talked it through
  (standup, review, or chat). Then add an ADR in `decisions.md` (context,
  decision, consequences) so the oral exam and later work can reconstruct
  it.
- An ADR records a **choice between alternatives**. A bug fix is not an
  ADR: describe it in the commit message and the pull request.
- Keep `README.md` accurate. Architecture changes go in
  `docs/architecture.md` in the same change.

## Dependencies

- Runtime: strict pins in `requirements.txt` (`package==1.2.3`).
- Dev tools: `requirements-dev.txt` (`-r requirements.txt` plus Ruff, mypy,
  pytest, pytest-cov, jsonschema). `Dockerfile` installs **only**
  `requirements.txt`; `Dockerfile.test` installs the dev set (ADR-027).
- Pin the version you tested. Architectural swaps get an ADR.

## Config and secrets

| Kind | Where | Git? |
| --- | --- | --- |
| Secrets (LLM API key) | `.env` / container env | **Never** |
| Variable names + dummies | `.env.example` | Yes |
| Model, temperature, workspace | `config.yaml` | Yes |

Load with **pydantic-settings**. Env wins over YAML. Read `Settings` at
startup — do not call `os.environ.get` in tools or the loop.

Fail fast if `LLM_API_KEY` is missing/empty, `config.yaml` is unreadable,
or the workspace path cannot be created. Tests that do not need a real model must
not construct `Settings` with a live key.

Keep `.env.example` in sync with every variable the process reads.

## Security

- File tools stay inside the workspace. Never call `Path` directly in a tool:
  go through `Tool._resolve`, which uses `tools/paths.py` (ADR-007).
- No delete tools — no class, not even a stub (ADR-008). The word list in
  `harness/tools/base.py` is the single source of truth for delete-shaped
  names, and it is enforced twice: at registration by the MCP adapter
  (ADR-042) and at call time by the permission policy, in every mode
  including `allow_all` (ADR-057). A remote tool is refused on its
  `destructive_hint` annotation *before* its name is examined, because a
  word list cannot be finished (ADR-059).
- Model-facing tool names follow the assignment's wording; Python class names
  stay descriptive (ADR-005). Changing a tool `name` changes the model's
  interface — treat it as an API change and note it in an ADR.
- Tools are `observe` (list / read / search) or `mutate` (create / write /
  edit). No unrestricted shell, eval, or HTTP.
- Treat every tool observation as untrusted input: it is bytes we did not
  write, going straight into the model's context. Defences must be
  structural, not prompt text — see ADR-026 and
  `tests/integration/test_prompt_injection.py`. A change that makes the agent's
  limits depend on the model choosing to behave is not acceptable.
- Docker is the OS sandbox; path confinement is the in-process sandbox.

## Tests and git

- pytest, three tiers (ADR-074): `tests/unit/` for a component,
  `tests/integration/` for the whole harness with the model scripted, and
  `tests/e2e/` for the shipped image against the real model. New tools,
  config fields and loop guards need a unit test. The default `pytest` run
  is unit plus integration; it must stay keyless, offline and green.
- Never put a scripted model in `tests/e2e/`. That directory is the only
  evidence the agent can do the job, and a fake there makes it evidence of
  nothing. If a case needs a scripted model, it is an integration test and
  belongs one directory over.
- Test doubles live in `tests/fakes.py`, never in `harness/` — the container
  image ships production code only (ADR-013). `ScriptedLLM` records every
  request, so assert on what the loop *sent*, not just what it returned.
  Note each recorded request carries the whole history, so scanning every
  request double-counts earlier tool messages: read the last one.
- A new tool needs nothing added to `tests/unit/test_tool_contract.py` — the
  contract tests iterate the registry. If one fails, fix the tool, not the
  test.
- Integration tests drive the real `cli.main()` and replace only the model
  (ADR-014). If a change makes that impossible, the change is wrong.
- End-to-end tests run `docker run` against throwaway volumes and read
  `.env.test`; they skip without Docker or a token, and they never touch the
  demo volumes (ADR-075). Run them with `./scripts/e2e.sh`, not in CI.
- Shared ownership. No secrets, `workspace/` outputs, or `.venv` in git.
- `main` stays runnable: merge through a pull request with the gate green.
- You must be able to explain every line. AI-assisted changes still need
  a team discussion and an ADR when they are architectural.

## Extension points

Typed seams only until the capability is needed. The loop talks to
`LLMClient`, `ToolRegistry`, `RunContext`, `Hook`, `PermissionPolicy` and
`Confirmer` — never to a concrete file tool or MCP client.

| Seam | Module |
| --- | --- |
| Tools | `harness/tools/` (assembled by `harness/toolset.py`) |
| MCP client | `harness/mcp/` |
| Long-term memory | `harness/memory/` |
| Skills | `harness/skills/` |
| Hooks | `harness/hooks.py` |
| Observability | `harness/observability/` (the only import of `prometheus_client`) |
| Permissions | `harness/permissions.py` (enforced in `loop.py`) |
| Sub-agents | `harness/subagents/` (registered in `session.py`, never in `loop.py`) |
| Token budget | `harness/budget/` (wraps the `LLMClient` in `session.py`; enforced at the model call, never in `loop.py`. A rolling 24-hour window plus a per-prompt checkpoint that pauses rather than stops — ADR-088, ADR-089) |
| Claim check | `harness/llm/grounding.py` (wraps the agent's `LLMClient` in `session.py`, never in `loop.py`; re-asks once when a final answer claims an action no tool took this turn — ADR-116) |
| Short-term memory | `harness/memory/scratchpad.py` + `notes.py` (`notes.md` under the memory root, one writing session at a time, injected by a hook, never reachable by the file tools — ADR-090) and `conversation.py` (folds old REPL turns into a summary when over budget — ADR-108) |
| Memory writes | `harness/memory/consolidate.py` (`MemoryWriter`: every long-term write goes through it; corrections supersede, never delete) + `extract.py` (reads the user's message only — ADR-107) |

`mcp_server_time_plus/` is **not** a seam and not part of the harness: it is
a separate program that the agent image launches as a subprocess and reaches
only over MCP. Nothing in `harness/` may import it and it may not import
`harness/` — if either did, the integration would prove nothing. It is held
to the same gate as `harness/`: `ruff`, `mypy --strict` and the same
coverage floor (ADR-046).

## Observability

Metrics reach a run through the `Hook` seam and nowhere else. Two rules
follow from that and are worth stating before you add a counter:

- **Nothing outside `harness/observability/` imports `prometheus_client`.**
  A subsystem reports by calling a `MetricsRecorder` method named after what
  happened (`memory_operation`, `tool_finished`), or — better — by doing
  nothing at all and letting `TelemetryHook` derive it from the lifecycle
  callbacks it already receives (its spans become metrics in
  `MetricsSpanExporter` — ADR-093).
- **Metric names are frozen** (ADR-081). `tests/unit/test_span_metrics.py`
  pins the list in both directions. A new metric is a conversation with
  whoever owns the dashboard, not a one-line addition.

`loop.py` has exactly one observability line — `after_permission` — and
adding a second is a decision that needs an ADR, not a commit. It also sets
`RunContext.exit_reason` at each terminal return; that is a *value the loop
knows and a hook cannot see*, not an observability call, and there are four
of them because there are four ways the loop can stop (ADR-084). At the three
normal exits it sets `RunContext.final_answer` the same way, through one
`_finish` helper, so the console hook can print the answer before slower
run-end work such as memory extraction (ADR-110).

## Sub-agents

A sub-agent is another `AgentLoop`. There is no second runtime, `loop.py`
has no delegation branch, and a change that adds one needs an ADR explaining
why the parameters were not enough (ADR-084).

- **A specialist is data.** `AgentSpec` rows in `harness/subagents/roles.py`.
  Adding one is a registration plus a result model plus an example that
  validates — never a branch in the factory.
- **Grant capabilities, not tool names.** `capability_of` must stay total
  over every tool that can reach a registry; `tests/unit/test_subagents.py`
  asserts it. A tool nothing classifies is a tool nothing refuses.
- **A child's terms only narrow.** Permission mode is
  `min(session, specification)`, capabilities are intersected with the
  session's tools, and the budget is clamped to the parent's remainder. A
  change that lets any of those widen is a privilege escalation, not a
  feature.
- **Whether to delegate is planned, not hoped for.** `DelegationPlanner`
  (`harness/subagents/planner.py`) wraps the coordinator's client in
  `session.py` and, at the first read of a turn, asks the model one JSON
  question — does this split? — turning a yes into a `delegate` call that
  the loop dispatches like any other. It never dispatches anything itself
  (ADR-117).
- **Shared hooks must be thread-safe.** Since ADR-106 a batched `delegate`
  runs children on worker threads, and the hooks they share with the parent
  — telemetry and the console — are called from several threads at once.
  Any state such a hook keeps between callbacks must be per thread
  (`threading.local`) or behind a lock; terminal output goes through the
  session's one `Console`. The order hooks are composed in is load bearing
  too, and `tests/unit/test_hook_order.py` pins it.
- **Enforce on the permission gate.** Capability, deadline and call-count
  refusals live in `ChildPolicy`, which wraps the policy consulted in
  `AgentLoop._execute`. Do not add a second enforcement point (ADR-086).
- **Nothing crosses back but a validated result.** A child never receives
  the parent's messages and never holds a reference to parent state. An
  answer that fails its contract becomes a status, never a finding
  (ADR-085). A hook must
never raise into a run: an unmatched `after_*` records a zero, because a gap
in a graph is acceptable and a failed task because a graph was unavailable
is not.
