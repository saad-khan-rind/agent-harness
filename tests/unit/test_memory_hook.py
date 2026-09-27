"""The read path: what memory puts into the prompt, and what it never does.

``MemoryHook`` is the only thing that writes memory into a run, and it does
it through the lifecycle seam so ``loop.py`` never learns memory exists
(ADR-001, ADR-034). What changed with the migration is *what the first tier
is*: it used to be a file injected whole, identical on every run; it is now
the result of a query against the task the user just typed (ADR-067).

That makes three questions worth testing that the file version did not have:

* does the injected set actually depend on the request, or is it whatever
  happened to be stored;
* does the budget still hold when the content is no longer a fixed file;
* does the section still say, in the prompt, that its contents are data
  rather than instructions — the property the prompt-injection tests rest on.

The last one matters most. Memory is the one part of the context a previous
session could have poisoned, so the framing is not decoration.
"""

from pathlib import Path
from typing import Any

from harness.context import RunContext
from harness.memory.hook import MEMORY_SECTION_HEADER, MemoryHook
from harness.memory.qdrant_store import QdrantMemoryStore


def _hook(
    store: QdrantMemoryStore,
    workspace: Path,
    max_inject_chars: int = 4000,
    core_fact_limit: int = 10,
) -> MemoryHook:
    """Build the hook the CLI builds, with the budget under test."""
    return MemoryHook(
        store,
        workspace,
        "AGENTS.md",
        max_inject_chars,
        core_fact_limit,
    )


def _inject(hook: MemoryHook, task: str) -> str:
    """Run ``on_run_start`` for ``task`` and return the system prompt."""
    context = RunContext(task="")
    context.ensure_system_prompt("SYSTEM")
    context.start_turn(task)
    hook.on_run_start(context)
    return str(context.messages[0]["content"])


# --- retrieval drives the injection ----------------------------------------


def test_the_injected_facts_depend_on_what_was_asked(
    store: QdrantMemoryStore, workspace: Path
) -> None:
    """The claim the whole migration rests on, once the tier outgrows its limit.

    The file store injected the same block whatever the question. Here, with
    room for one fact, two different questions keep two different facts
    from the same collection, which is what lets memory grow past a context
    window. Below the limit every core fact is shown (ADR-118).
    """
    store.store("the harness deploys with docker compose", "core_fact", "user")
    store.store("apricots are the preferred fruit", "core_fact", "user")
    hook = _hook(store, workspace, core_fact_limit=1)

    about_deploys = _inject(hook, "how do we deploy the harness")
    about_fruit = _inject(hook, "which fruit is preferred")

    assert "docker compose" in about_deploys
    assert "apricots" not in about_deploys
    assert "apricots" in about_fruit


def test_a_core_fact_the_request_does_not_resemble_is_still_injected(
    store: QdrantMemoryStore, workspace: Path
) -> None:
    """The team was never scheduled because the team was never in the prompt.

    Found running Week2_Scenarios.md scenario 2 against the real model:
    ``/remember Alice is in Europe/Berlin, Bob is in Asia/Karachi ...`` and
    then "find a 30-minute slot that works for the whole team". The fact
    scored 0.04 against the request under the shipped embedder, below its
    0.35 floor, so the agent searched the workspace for time zones until the
    per-prompt budget stopped it. Core facts are the up-front tier; while
    there are fewer than the limit, all of them are shown (ADR-118).
    """
    store.store(
        "Alice is in Europe/Berlin, Bob is in Asia/Karachi", "core_fact", "user"
    )
    store.store("I prefer short answers with no preamble", "core_fact", "user")

    injected = _inject(
        _hook(store, workspace), "find a 30-minute slot for the whole team"
    )

    assert "Europe/Berlin" in injected
    assert "short answers" in injected


def test_the_core_fact_limit_still_bounds_the_section(
    store: QdrantMemoryStore, workspace: Path
) -> None:
    """Filling to the limit never goes past it."""
    for number in range(6):
        store.store(f"standing fact number {number}", "core_fact", "user")

    injected = _inject(_hook(store, workspace, core_fact_limit=3), "unrelated")

    assert injected.count("standing fact number") == 3


def test_only_core_facts_are_injected(
    store: QdrantMemoryStore, workspace: Path
) -> None:
    """The two-tier split, preserved.

    Notes are found with ``memory_search`` when they are needed. Injecting
    them up front would spend the budget the tiering exists to protect.
    """
    store.store("deployment detail worth keeping", "core_fact", "user")
    store.store("deployment detail in a long note", "project_note", "user")

    injected = _inject(_hook(store, workspace), "deployment detail")

    assert "worth keeping" in injected
    assert "in a long note" not in injected


def test_every_injected_fact_carries_its_provenance(
    store: QdrantMemoryStore, workspace: Path
) -> None:
    """The trust distinction the section exists to preserve.

    A fact the user stated and one the model inferred while reading a
    possibly hostile file must not look identical in the prompt (ADR-034).
    """
    store.store("stated by the person", "core_fact", "user")
    store.store("inferred while working", "core_fact", "agent")

    injected = _inject(_hook(store, workspace), "stated inferred person working")

    assert "- [user] stated by the person" in injected
    assert "- [agent] inferred while working" in injected


# --- nothing to say, so nothing said ---------------------------------------


def test_an_unavailable_database_injects_nothing_and_does_not_raise(
    workspace: Path, memory_root: Path
) -> None:
    """The degradation policy, at the point where a run would have died.

    ``on_run_start`` is called before the first LLM call of every turn. If
    it raised on an unreachable database, a missing container would mean no
    agent at all rather than an agent without memory.
    """
    from harness.memory.embed import HashingEmbedder
    from harness.memory.sessions import SessionLog

    store = QdrantMemoryStore(
        embedder=HashingEmbedder(),
        session_log=SessionLog(memory_root),
        url="http://127.0.0.1:1",
        timeout=2.0,
    )

    assert MEMORY_SECTION_HEADER not in _inject(_hook(store, workspace), "anything")


# --- the section says what it is -------------------------------------------


# --- project memory --------------------------------------------------------


def test_project_memory_is_injected_and_still_a_file(
    store: QdrantMemoryStore, workspace: Path
) -> None:
    """``AGENTS.md`` did not move into the collection, and should not.

    It is written by a human, read by a human, and versioned with the code.
    A vector helps with none of those.
    """
    (workspace / "AGENTS.md").write_text("Run ruff before committing.", "utf-8")

    injected = _inject(_hook(store, workspace), "anything at all")

    assert "Run ruff before committing." in injected


# --- the budget ------------------------------------------------------------


def test_the_whole_block_stays_within_the_budget(
    store: QdrantMemoryStore, workspace: Path
) -> None:
    """Memory competes with the conversation for one window.

    The content is no longer a fixed file, so the ceiling is the only thing
    keeping a well-stocked collection from crowding out the task.
    """
    (workspace / "AGENTS.md").write_text("project rule. " * 400, "utf-8")
    for index in range(10):
        store.store(
            f"deployment fact number {index} " + "detail " * 40, "core_fact", "user"
        )

    injected = _inject(_hook(store, workspace, max_inject_chars=600), "deployment fact")

    # Measured between the fences ``RunContext`` wraps every section in.
    # The budget is the hook's, over the content it composed; the fence is
    # fixed overhead the context adds and is not the hook's to spend.
    body = injected.split("BEGIN HARNESS DATA", 1)[1].split("END HARNESS DATA", 1)[0]

    assert len(body.strip()) <= 600


# --- the section is replaced, not appended ---------------------------------


def test_a_second_turn_replaces_the_first_turn_s_facts(
    store: QdrantMemoryStore, workspace: Path
) -> None:
    """The query changes with every turn, so the stale set must go.

    Appending would spend the budget on facts relevant to a question that
    has already been answered.
    """
    store.store("the harness deploys with docker compose", "core_fact", "user")
    store.store("apricots are the preferred fruit", "core_fact", "user")
    hook = _hook(store, workspace, core_fact_limit=1)

    context = RunContext(task="")
    context.ensure_system_prompt("SYSTEM")
    context.start_turn("how do we deploy")
    hook.on_run_start(context)
    context.start_turn("which fruit is preferred")
    hook.on_run_start(context)
    prompt = str(context.messages[0]["content"])

    assert "apricots" in prompt
    assert "docker compose" not in prompt
    assert prompt.count(MEMORY_SECTION_HEADER) == 1


# --- the run-end write trigger ---------------------------------------------


def test_the_run_is_recorded_at_run_end(
    store: QdrantMemoryStore, workspace: Path, memory_root: Path
) -> None:
    """The harness-driven trigger: it costs no LLM call (ADR-036)."""
    context = RunContext(task="")
    context.ensure_system_prompt("SYSTEM")
    context.start_turn("build the thing")
    context.add_assistant_message("built it")

    _hook(store, workspace).on_run_end(context)

    assert "build the thing" in (memory_root / "sessions.md").read_text("utf-8")


# --- slots bypass retrieval entirely ---------------------------------------


def test_a_slot_is_injected_even_when_the_query_does_not_match_it(
    store: QdrantMemoryStore, workspace: Path
) -> None:
    """The regression this test exists for, stated as the user hit it.

    ``Likes: swimming, going to the gym, and casual morning runs`` stored,
    ``what do i like?`` asked, and nothing injected — because under the
    lexical fallback embedder that pair scores 0.115 against a
    ``MIN_SIMILARITY`` of 0.20. The two texts share no trigrams:
    ``preferences`` against ``likes`` is exactly 0.0. No phrasing of the
    question a user would actually type clears the floor, so the fix is not
    a better query or a lower threshold — it is not asking a vector at all.
    """
    store.upsert_slot("Likes", "swimming, going to the gym, and casual runs", "user")

    injected = _inject(_hook(store, workspace), "what do i like?")

    assert "swimming" in injected


# --- follow-ups retrieve through the recent messages (ADR-110) --------------


def test_a_vague_follow_up_still_retrieves_what_the_conversation_is_about(
    store: QdrantMemoryStore, workspace: Path
) -> None:
    """A follow-up that matches nothing alone is answered from the turn before.

    "and on the laptop?" scores 0.0 against the fact under the lexical
    embedder, which is why the previous messages are a second query.
    """
    store.store("User edits Python code in Neovim", "core_fact", "user")
    hook = _hook(store, workspace)
    context = RunContext(task="")
    context.ensure_system_prompt("SYSTEM")
    context.start_turn("Which editor do I use for Python code?")
    context.add_assistant_message("Neovim.")
    context.start_turn("and on the laptop?")

    hook.on_run_start(context)

    assert "User edits Python code in Neovim" in str(context.messages[0]["content"])


def test_the_current_question_alone_decides_on_the_first_turn(
    store: QdrantMemoryStore, workspace: Path
) -> None:
    """With no earlier messages there is one query, not the same one twice."""
    store.store("User edits Python code in Neovim", "core_fact", "user")
    searches: list[str] = []
    original = store.search

    def _counting(query: str, limit: int, category: Any = None) -> Any:
        searches.append(query)
        return original(query, limit, category)

    store.search = _counting  # type: ignore[method-assign]

    _inject(_hook(store, workspace), "Which editor do I use for Python code?")

    assert searches == ["Which editor do I use for Python code?"]
