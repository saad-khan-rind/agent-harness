# Attribution

The five query tools — `get_events_today`, `get_events_range`,
`get_event_details`, `list_recurring_events` and `search_events` — together
with their argument names, result keys and the event-formatting helpers
behind them, are taken from
[`adamchrabaszcz/ics-calendar-mcp`](https://github.com/adamchrabaszcz/ics-calendar-mcp)
(`ics-calendar-mcp`, version 0.1.0), published under the **MIT licence**,
Copyright (c) Adam Chrabaszcz.

The licence is declared in the project's `pyproject.toml` as
`license = { text = "MIT" }` together with the OSI classifier
`License :: OSI Approved :: MIT License`. There is no separate `LICENSE`
file in the repository; the metadata grant is the licence.

This is the same relationship `mcp_server_time_plus` has with the reference
**time** server (ADR-044). Both of this project's extension servers are
forks: one of an Anthropic reference implementation, one of a community
server.

## Why this one, and not the others

Five open-source calendar MCP servers were surveyed in September 2026. Four
of them cannot be used here, and the reasons are worth writing down rather
than leaving as "we picked this one".

| Server                          | Language   | Licence                | Credentials it needs                            | Why not                                                                 |
| ------------------------------- | ---------- | ---------------------- | ----------------------------------------------- | ----------------------------------------------------------------------- |
| `madbonez/caldav-mcp`           | Python     | **none on any branch** | `CALDAV_URL`, `CALDAV_USERNAME`, `CALDAV_PASSWORD` | No licence and no licence metadata is all-rights-reserved by default. Copying it into this repository is not ours to decide. |
| `dominik1001/caldav-mcp`        | TypeScript | MIT                    | the same three                                  | The brief pins `python:3.12-slim-bookworm`. A Node runtime in the image for one server is a large cost for no capability. |
| `amattas/calendar-mcp`          | Python     | "personal use" only    | an API key                                      | Not an open-source licence.                                             |
| `guinacio/mcp-google-calendar`  | Python     | MIT                    | an OAuth `credentials.json`                     | Needs a Google account to issue the file before the server will start.  |
| **`adamchrabaszcz/ics-calendar-mcp`** | **Python** | **MIT**          | `ICS_CALENDAR_URL`                              | **Chosen.** The URL is the only blocker, and it is a source argument rather than an architecture — the fork reads a local file instead. |

The common thread in the four rejects is credentials, and §8.2 of the brief
forbids committing secrets: a server a TA cannot run without first creating
an account somewhere is not a demonstration of this project. Running
Radicale ourselves with `auth type = none` was considered and rejected in
ADR-071 — a fourth container, three transitive dependencies, and a
`CALDAV_URL`/`USERNAME`/`PASSWORD` triple with dummy values, which gives
back the property the survey was looking for.

## What we changed, and why

Recorded in ADR-071.

- **Ported from `FastMCP` to `MCPServer`.** Upstream requires `mcp>=1.0.0`
  and is written against the 1.x `FastMCP` class. This repository pins
  `mcp==2.2.0` because the harness client uses the 2.x `Client` API
  (ADR-039), and the two cannot be installed together. The SDK says so
  itself — importing `mcp.server.fastmcp` under 2.x raises *"This is mcp
  2.x, where FastMCP was renamed to MCPServer … or pin 'mcp<2' to keep
  running v1 code."* Same forced port, same reason, as ADR-044.
- **The calendar source is a local file, not a URL.** Upstream requires
  `ICS_CALENDAR_URL` and fetches it with `httpx`. A private calendar feed
  URL *is* a secret, and §8.2 of the brief forbids committing secrets; a
  local file also removes network egress from a server that otherwise needs
  none, so the SSRF guard the web server carries (ADR-056) does not have to
  be duplicated here. `httpx` is dropped as a dependency.
- **Synchronous, not `async`.** Upstream is `async` because it makes an HTTP
  request. Reading a file has nothing to await.
- **`zoneinfo` in place of `pytz`.** This repository already depends on
  `tzdata` and uses `zoneinfo` everywhere else (ADR-044); two timezone
  libraries in one image is how you get two answers to the same question.
  The default zone moves from `Europe/Warsaw` — upstream's author's — to
  UTC, set by `--local-timezone`.
- **No module-level singleton.** Upstream keeps a global `_client` with
  `get_client` / `reset_client` / `set_client` around it. The server holds
  its client, so the global and its three undo hatches are unnecessary.
- **Failures are MCP errors.** Every upstream tool ends in
  `except Exception as e: return {"error": str(e)}`, which returns a failure
  as a *successful* call: the client sees `is_error` false, the harness
  counts a success, and ADR-048's per-tool failure guard never fires on a
  tool that is failing every time. The fork raises `ToolError` instead, so
  the message reaches the model as a recoverable observation.
- **Bounds are published.** Upstream has none — its range query has no
  ceiling and its search defaults to a year. The fork publishes limits in
  the JSON Schema so the model is told them rather than discovering them by
  failing.

Everything upstream's tools returned, they still return.
`tests/unit/test_calendar_upstream.py` pins that: the tool names, the
argument names, the result envelopes (`events` / `count`), and the event
keys (`uid`, `summary`, `start`, `end`, `all_day`, plus `location`,
`description`, `status`, `organizer`). If one of those assertions has to
change, the honest move is to update this file and stop calling it a fork.

## What we added

- **`suggest_reschedule_slots`** — the new capability (ADR-072). Upstream
  can list and search events and expand their recurrences; it has no notion
  of *busy* time, so it cannot answer the question those queries are usually
  asked in service of: when am I free? Ranked by soonness and time-of-day
  affinity, one suggestion per day, with the event's own slot excluded.
- **`create_event`** — upstream is read-only, which is right for a feed you
  do not own and leaves no way to put anything on the calendar. Append only:
  no update, no delete, and the UID is generated here so a caller cannot
  overwrite an existing event (ADR-008).
- **`busy_between`, `clashes_with` and `occurrence_of`** on the client, all
  built on upstream's recurrence expansion.

## Why upstream's recurrence handling is the reason to fork

`recurring_ical_events.of(calendar).between(start, end)` turns one
`RRULE:FREQ=WEEKLY;BYDAY=MO` into an instance on every Monday in the window.
Doing that by hand means implementing RFC 5545's recurrence rules — `BYDAY`,
`BYSETPOS`, `UNTIL`, `COUNT`, `EXDATE` — which is a project, not a function.

It also matters to the tool we added rather than only to a viewer: without
expansion, a weekly standup would block one Monday and leave every later
Monday looking free, and `suggest_reschedule_slots` would confidently
propose a slot straight on top of a standing meeting.
`tests/unit/test_calendar_upstream.py::test_a_recurring_event_blocks_every_occurrence`
is that case.

MIT licence text: <https://opensource.org/license/mit>
