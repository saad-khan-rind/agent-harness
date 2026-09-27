# Tools Implementation — Agent Harness

This document explains how the built-in file tools are implemented in this
project and how the loop interacts with them. It is intended to give a
developer enough detail to understand, extend, or safely wrap the existing
capabilities.

Files of interest
- [harness/tools/__init__.py](harness/tools/__init__.py)
- [harness/tools/base.py](harness/tools/base.py)
- [harness/tools/observe.py](harness/tools/observe.py)
- [harness/tools/mutate.py](harness/tools/mutate.py)
- [harness/tools/registry.py](harness/tools/registry.py)
- [harness/tools/paths.py](harness/tools/paths.py)
- [harness/toolset.py](harness/toolset.py) — assembles the registry the agent runs with
- [harness/memory/tools.py](harness/memory/tools.py) — the memory tools (Week 2)
- [harness/memory/notes.py](harness/memory/notes.py) — `note_write`, `note_append` (Week 3)
- [harness/skills/tools.py](harness/skills/tools.py) — `skill_read` (Week 2)
- [harness/mcp/adapter.py](harness/mcp/adapter.py) — wraps a remote tool as a `Tool` (Week 2)

High-level overview
- The ReAct loop only talks to a `ToolRegistry` instance (see
  [harness/tools/registry.py](harness/tools/registry.py). The registry is the
  single indirection point: built-in tools, MCP-wrapped remote tools, or
  runtime-registered plugins all present the same interface to the loop.
- Built-in tools are created by `builtin_tools()` and registered via
  `build_registry()` in [harness/tools/__init__.py](harness/tools/__init__.py).

Design principles
- Safety-first: there is deliberately no delete tool or delete-capable class
  in the codebase. The implementation choices (no class, write guards, and
  edit semantics) enforce that constraint at the code level.
- Deterministic schemas: the registry preserves insertion order so the
  sequence of schemas exposed to the LLM is stable and reproducible.
- Single error channel: tools never raise uncaught exceptions to the loop.
  Instead they return observation strings. Failures are signalled with the
  `Error: ` prefix so the model can detect and recover.

Tool contract (how to implement a tool)
- Base contract: subclass `Tool` from
  [harness/tools/base.py](harness/tools/base.py). Key points:
  - Class attributes to set: `name`, `description`, `parameters` (JSON
    Schema), and `risk` (literal `"observe"` or `"mutate"`).
  - Implement `_run(self, arguments: dict) -> ToolResult` and return a
    `ToolResult(content: str, ok: bool = True)`; raise exceptions for
    unusual preconditions where appropriate.
  - Use the provided helpers `self._string_argument(...)` and
    `self._resolve(... )` so argument validation and path containment are
    consistent across tools.
  - Do not override `execute()`; it is a template method that wraps
    `_run()` and converts common filesystem exceptions into user-friendly
    observations.
- Truncation and budgets: `Tool.execute()` truncates returned text using
  `truncate()` to `self.max_output_chars`. Defaults are defined in the
  module but can be supplied when constructing the tool instances.

Security and sandboxing
- Path containment: every tool that accepts a path must resolve it via
  `resolve_in_workspace(workspace_root, relative)` from
  [harness/tools/paths.py](harness/tools/paths.py). That function:
  - Normalises and resolves symlinks, then checks `relative_to(root)` to
    detect escapes.
  - Treats absolute model-supplied paths as workspace-relative (leading
    slashes are stripped) to avoid accidentally exposing host paths.
  - Raises `PathEscapeError` when the requested path would leave the
    workspace; `Tool.execute()` converts this to an `Error: ...` observation.
- Displayed paths: observations present workspace-relative paths via
  `display_path(...)` so the LLM never learns absolute host paths.

Read-only (observe) tools
- Implemented in [harness/tools/observe.py](harness/tools/observe.py):
  - `ListDirectory` exposed as `list_files`: lists a directory, suffixing
    directories with `/` and returning a deterministic, sorted list.
  - `ReadFile` exposed as `read_file`: reads UTF-8 text files, includes a
    header with the line count, and refuses to read files larger than the
    `MAX_READABLE_BYTES` guard to avoid blowing the context budget.
  - `SearchText` exposed as `search_text`: simple substring search across
    files; it skips unreadable/binary files and caps hits at
    `DEFAULT_MAX_SEARCH_RESULTS`.
  - `FindFiles` exposed as `find_files`: matches file *names* by glob across
    every sub-folder, returning files only. It is the counterpart to
    `list_files`, which shows one level and is the only tool that reveals an
    empty folder (ADR-021).

Mutating tools
- Implemented in [harness/tools/mutate.py](harness/tools/mutate.py):
  - `CreateDirectory` exposed as `create_folder`: idempotent, returns
    whether the folder already existed or was created.
  - `WriteFile` exposed as `write_file`: writes full file contents,
    creates parent directories, refuses to write a path that is a
    directory, and enforces `MAX_WRITE_BYTES` as a size cap.
  - `AppendFile` exposed as `append_file`: adds text to the end of a file,
    creating it and its parents when absent. It is the only mutating tool
    that cannot destroy existing content — it never reads the file into the
    model's context and never rewrites it. A newline separator is inserted
    when the file does not already end with one, and the size cap is
    checked against the *resulting* file so repeated appends stay bounded.
    It exists because `read_file` + `write_file` was the only way to add a
    line, and that route silently loses data once the file passes the
    observation budget (ADR-050).
  - `EditFile` exposed as `edit_file`: replaces an *exact* `old_str` with
    `new_str` but only when `old_str` appears exactly once. This uniqueness
    rule is a deliberate safety measure to avoid ambiguous edits.

The other tool families (Week 2)
- Everything above describes the **file** tools, which are confined to the
  workspace. Four more families register on the same `ToolRegistry` and are
  built the same way, so the loop cannot tell them apart:
  - **Memory tools** — `memory_search`, `memory_store`,
    `memory_upsert_slot` in
    [harness/memory/tools.py](harness/memory/tools.py). Same `Tool` base and
    the same error contract, but no filesystem root of their own: since
    ADR-065 memory is records in a Qdrant collection, so `_run` reaches a
    database rather than a directory. They take **no path argument**, which
    is why the containment story that applies to the file tools does not
    apply to these — there is nothing to confine (ADR-070).

    `memory_search` finds facts by meaning, which is the capability the
    file surface could not offer: a fact is reachable by describing it
    rather than by naming its file. `memory_store` writes one, tagged
    `[agent]` — provenance is stamped by the store and a `[user]` tag in the
    text is stripped before the fact is embedded (ADR-062).
    `memory_upsert_slot` sets one of `Name` / `Likes` / `Locale` /
    `Language`, replacing the old value so a preference change does not
    stack ghosts, and refusing to overwrite a slot the user set themselves
    (ADR-063).
  - **Working notes** — `note_write`, `note_append` in
    [harness/memory/notes.py](harness/memory/notes.py). The other half of
    memory, and deliberately not the same half: these edit one fixed
    markdown file, `notes.md`, that holds the agent's state for the task in
    front of it — what is done, what is left, what it decided. There is no
    path argument and no path is ever joined, so unlike the file tools there
    is nothing to confine; unlike the memory tools there is nothing to
    query, because the whole file is injected into every request. An
    oversized write comes back as a *failed* observation naming the limit,
    so the model retries shorter instead of believing it saved a plan it did
    not (ADR-090).
  - **Skills** — `skill_read` in
    [harness/skills/tools.py](harness/skills/tools.py), rooted in a
    read-only skills directory. Names and descriptions are injected at
    startup; a body is fetched only when the model asks (ADR-038).
  - **Remote MCP tools** — `RemoteTool` in
    [harness/mcp/adapter.py](harness/mcp/adapter.py), one per tool
    discovered on a configured server, reached over stdio or HTTP depending
    on the entry's `transport` (ADR-061). It has no filesystem root: `_run`
    forwards the call to another process. Names carry a `server__` prefix.
    Registration is refused on two signals: a tool the server annotates
    `destructive_hint: true` is refused whatever it is called, and then a
    name that looks like a deletion is refused too. Each refusal is
    reported with the rule that fired (ADR-042, ADR-059).
- The registry is assembled by `build_agent_registry()` in
  [harness/toolset.py](harness/toolset.py) — `build_registry()` in
  `harness/tools/__init__.py` builds only the file tools, and is what
  `toolset.py` calls first. Registration order is the order the model sees:
  file, memory, skills, then remote.

Registry behaviour and the function-calling schema
- `ToolRegistry` (see
  [harness/tools/registry.py](harness/tools/registry.py)) provides the
  following surface used by the loop:
  - `register(tool: Tool)`: add an instance; duplicate names raise `ValueError`.
  - `schemas() -> list[dict]`: returns OpenAI-style function-calling schemas
    for every registered tool. Each item contains `name`, `description`, and
    `parameters` (the JSON schema the tool exposes).
  - `execute(name, arguments) -> str`: dispatch a tool by name and return
    the observation string. Unknown names return an `Error: unknown tool` observation
    rather than raising so the model can recover from hallucinated names.
  - `names()` and `__len__()` helpers used by the CLI and prompt-building.

Error reporting and recoverability
- Tools return `ToolResult` values; `ToolResult.as_observation()` prefixes
  the content with `Error: ` when `ok=False`. This is the only programmatic
  signal the model receives for failures — design choices in the code aim
  to make those messages human-readable and actionable.
- Common filesystem failure modes are caught centrally in `Tool.execute()`
  and mapped to deterministic messages (e.g. permission denied, not a
  directory, binary file / UnicodeDecodeError, etc.). That keeps the
  ReAct loop simple: it only sees observations, not exceptions.
- The `Error: ` marker has one definition, `ERROR_PREFIX` in
  [harness/tools/base.py](harness/tools/base.py), because the loop reads it:
  a tool whose observations start with it `max_tool_failures` times in a row
  (5 by default) is not executed again for the rest of the task, and the
  model is told it is unavailable. A success clears the streak, and the
  counters live on `RunContext`, so a Week 3 sub-agent gets its own
  (ADR-048).
- A tool that keeps returning the *same* observation to the *same* arguments
  ends the run instead: ten identical calls in a row means nothing was
  learned after the first. The number sits well above `max_tool_failures`
  so a failing tool is retried and retired on that guard's terms first. The observation is part of the comparison, so a
  tool that reports something changing — a clock, a directory being written
  to — is never mistaken for one that is stuck (ADR-049).

Extending or adding a tool (quick checklist)
1. Add a new subclass of `Tool` in the `harness.tools` package.
2. Set `name`, `description`, `parameters` (JSON Schema), and `risk`.
3. Implement `_run(self, arguments) -> ToolResult` and use helper methods
   (`_string_argument`, `_resolve`) for validation and path handling.
4. Register an instance with the `ToolRegistry` (or via
   `builtin_tools()` if it is intended as a built-in).
5. Write tests mirroring the existing style in `tests/unit` to assert the
   observed strings and error branches.

Notes and important constants
- Default truncation / budgets live in `base.py`: `DEFAULT_MAX_OUTPUT_CHARS`.
- Read/search/write limits: `MAX_READABLE_BYTES` (observe),
  `DEFAULT_MAX_SEARCH_RESULTS`, and `MAX_WRITE_BYTES` (mutate).
- The `risk` attribute is currently descriptive (`"observe"` or
  `"mutate"`) but is an explicit extension point for permission policies
  or audit logging.

Where to look for examples and tests
- Concrete behaviour and wording are covered by unit tests in `tests/unit`.
- The CLI uses `build_agent_registry()` from
  [harness/toolset.py](harness/toolset.py) to create the tool set — not
  `build_registry()`, which returns the file tools alone. See
  [harness/cli.py](harness/cli.py) for how the banner and run modes present
  tools to the user.
