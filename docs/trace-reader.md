# Trace reader (`/trace`) — what we did and why

**Date:** 2026-09-15 · **ADR:** [ADR-087](../decisions.md)

## The problem

Every agent run already writes a trace file: `memory/traces/<run_id>.jsonl`,
one JSON line per event (model call, permission decision, tool call, run
end). That is the record §8.4 of the brief asks for.

But the only way to *read* it was `cat` and `jq` on the memory volume. A raw
file does not answer the questions a person actually has after a run:

1. **Where did the time go?** Each line has a latency, nothing adds them up.
2. **Was that failed `write_file` a broken tool or a person saying "no"?**
   In the file both look like `"ok": false`. The line before it (the
   permission decision) tells them apart, but you had to notice that yourself.
3. **What did the delegated sub-agent do?** Its lines are mixed into the
   parent's file and hard to follow by eye.
4. **Why did the run stop?** The file only said whether the model answered.
   A safety guard stopping the run and the endpoint crashing looked identical.

## What we built

### 1. A twelfth key in the trace: `exit_reason`

The `run_end` line now carries the loop's own verdict — `success`, `capped`,
`no_progress` or `error`. The loop already had this value; the hook just
did not write it. Old trace files without the key still read fine.

### 2. A reader: `harness/observability/replay.py`

It parses a trace file and turns it into a timeline:

```
Run 9b0602a0  main  2026-09-14 19:20:38 UTC  answered  5.0s

  19:20:38.195  run_start
  19:20:40.155  llm                              1,958 ms   6,089 → 55 tok
  19:20:41.820  perm   write_file      declined
  19:20:41.821  tool   write_file      DECLINED               waited 1,665 ms for the user, then did not run
  19:20:43.210  llm                              1,388 ms   6,194 → 59 tok
  19:20:43.213  run_end                          5,018 ms   12,283 → 114 tok  answered

  2 llm calls · 1 tool call · 1 refused · 12,283 prompt / 114 completion tokens
  time: llm 3,346 ms (66.7%) · waiting on user 1,665 ms (33.2%) · other 7 ms
```

What it does for you:

- **Time split** — how much of the run was the model, the tools, a
  delegated child, or a person reading a confirmation prompt.
- **Pairs each tool call with its permission decision** — so a declined
  confirmation is shown as `DECLINED … waited N ms for the user`, a policy
  block as `DENIED`, a tool the loop gave up on as `RETIRED`, and only a tool
  that actually ran and broke as `FAIL`.
- **Nests delegated children** inside the parent, at the point where they
  ran, with their own counts.
- **Survives a torn file** — a run killed mid-write is still readable up to
  the last complete line.
- **Never shows tool arguments or file contents** — the trace does not
  store them (on purpose: workspace content must not land on the memory
  volume), so the reader cannot either.

### 3. Two ways to use it

**In the REPL** (no config needed):

| Command | What it shows |
| --- | --- |
| `/trace` | The run that just finished (or the list, before any run) |
| `/trace list` | Every recorded run, newest first |
| `/trace 9b06` | One run, by any unique prefix of its id |

**From the command line**, without starting the agent — also works on a
traces folder copied off a container that no longer exists:

```bash
python -m harness.observability.replay                    # list
python -m harness.observability.replay 9b06               # one run
python -m harness.observability.replay 9b06 --slowest 5   # + the 5 slowest calls
python -m harness.observability.replay --dir ./copied --json
```

## Why this way

- **No change to `loop.py`.** The team rule is that the loop stays thin;
  the reader only reads a file and the hook change is one extra field.
- **Read from the file, not from memory.** `/trace` shows what was
  *recorded*, so a bug in the hook shows up in `/trace` instead of being
  hidden by the process remembering the run differently.
- **File order, not timestamps, to place a child.** Timestamps have
  millisecond resolution and parent/child events often share one; the line
  order never ties.
- **`delegate` is its own time bucket.** Its latency *is* the child's whole
  duration, so counting it as tool time would count the child twice.

## Files changed

| File | Change |
| --- | --- |
| `harness/observability/replay.py` | **new** — parser, summary, renderer, CLI entry point |
| `harness/observability/trace.py` | writes `exit_reason` on `run_end` |
| `harness/observability/__init__.py` | exports the reader |
| `harness/cli.py` | `/trace` command, `/help` and banner rows |
| `tests/unit/test_trace_replay.py` | **new** — 38 tests, incl. a real nested child run through the real hook |
| `tests/unit/test_jsonl_trace.py` | schema pin is now twelve keys; 3 tests for `exit_reason` |
| `tests/unit/test_cli.py` | 5 tests for `/trace` |
| `tests/e2e/test_agent_traces.py` | schema pin updated |
| `README.md`, `docs/architecture.md`, `docs/subagents.md` | docs |
| `decisions.md` | ADR-087 |

## How it was tested

- `./scripts/ci-gate.sh` on 2026-09-15, when this was written: ruff, mypy
  strict, **1338 tests passed, 96.1 % coverage** (`replay.py` at 97 %). The
  current totals are in the README's **Tests** section.
- Rendered against the three real traces already on this machine
  (`memory/traces/`); the example above is one of them.
- Two failures in `tests/unit/test_memory_store.py` are **pre-existing on
  `develop`** (checked on a clean checkout) and unrelated to this change.

## Team note

The twelfth key is additive — every existing reader looks keys up by name —
but "every line has the same N keys" is a team contract, so it goes on the
standup agenda before merge, per `AGENTS.md`.
