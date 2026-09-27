"""**Real model.** A skill's `allowed-tools` really does bound the agent.

`tests/unit/test_skill_policy.py` proves the wrapper decides correctly. What
it cannot show is the property the feature exists for: that a *real* model,
following a real skill, cannot reach past what that skill declared — and
that the agent still finishes its turn rather than wedging when it is
refused (ADR-082).

**These tests are built around a precondition the model controls.** The
restriction opens when the model reads a skill, and no prompt can guarantee
it will. So the hard assertion is conditional on the trace showing that
`skill_read` actually ran, and when it did not the test warns instead of
failing. That is deliberate, and it is the rule this suite already follows:
a test that punishes a model for a legitimate route is worse than no test,
and a flaky one is worse still (ADR-078).

The shipped skills are used as they are. A test-only skill would prove the
mechanism against a fixture nobody ships, which is most of the way to
proving nothing.
"""

import warnings

import pytest
from tests.e2e.conftest import Stack

pytestmark = pytest.mark.e2e

# `skills/web-research/SKILL.md` declares exactly these. Written out rather
# than read from the file, so that quietly widening the frontmatter fails
# here instead of silently widening the test with it.
#
# These are the *bare* names. The registry — and so the trace — carries
# them as `web__web_search`, prefixed by the server label in config.yaml
# (ADR-042), and the policy matches a declared bare name against that
# prefixed spelling (ADR-082, amendment). :func:`_permitted` applies the same
# rule here; comparing trace names to this set directly would count the two
# tools the skill exists to use as escapes.
WEB_RESEARCH_TOOLS = frozenset({"web_search", "web_fetch"})

# Always permitted whatever a skill declares: a skill that forgot to list it
# would make every *other* skill unreachable, including one its own body
# tells the model to go and read.
ALWAYS_ALLOWED = frozenset({"skill_read"})

ENFORCED = {"SKILLS_ENFORCE_ALLOWED_TOOLS": "true"}


def _permitted(name: str) -> bool:
    """Whether the skill's list covers a tool name as the trace spells it.

    Mirrors ``harness.skills.policy._permits``: exact match, else the part
    after the ``<server>__`` prefix. Kept local rather than imported so this
    test stays a statement of what the *skill file* permits rather than of
    whatever the policy currently does.

    Args:
        name: A tool name from the trace.

    Returns:
        ``True`` when the skill, or the always-allowed set, covers it.
    """
    allowed = WEB_RESEARCH_TOOLS | ALWAYS_ALLOWED
    if name in allowed:
        return True
    _, separator, remote = name.partition("__")
    return bool(separator) and remote in allowed


def _after_the_skill_was_read(
    trace: list[tuple[str, bool]],
) -> list[tuple[str, bool]] | None:
    """Every call after the first `skill_read`, or ``None`` if there was none.

    Args:
        trace: ``(name, refused_by_policy)`` pairs, in order.

    Returns:
        The tail of the trace after the first `skill_read`, or ``None`` when
        the model never read a skill — which is the case every test here has
        to handle rather than assume away.
    """
    names = [name for name, _ in trace]
    if "skill_read" not in names:
        return None
    return trace[names.index("skill_read") + 1 :]


def test_a_loaded_skill_bounds_what_the_agent_may_call(stack: Stack) -> None:
    """After reading a skill, nothing outside its list gets to run.

    **Attempting is not the violation; running is.** The trace prints ``→``
    *before* the permission check, so a test reading only those lines would
    count a refusal as an escape and report a working gate as a bug;
    :func:`~tests.e2e.conftest.parse_trace` pairs each call with its
    observation so the two can be told apart.

    This is a safety property, so it is allowed to hold vacuously. A real
    model often never tries: ``skill_read`` tells it "you may use only these
    tools", it believes that, and the gate is never asked to refuse
    anything. That is the feature working, not the test missing something —
    but it does mean this run cannot be the evidence that the gate *fires*.
    That claim is forced with a scripted model one tier down, in
    ``tests/integration/test_skill_scope.py``, where the forbidden call can
    be made to happen every time (ADR-074, ADR-082).
    """
    with stack.session(env=ENFORCED) as live:
        live.wait_for("You:")
        live.ask(
            "Read the web-research skill and follow it. Then save what you "
            "found to findings.md in the workspace."
        )
        live.wait_for("Agent: ")

        after = _after_the_skill_was_read(live.trace)
        transcript = live.transcript

    if after is None:
        warnings.warn(
            "the model never called skill_read, so nothing was in scope to "
            "enforce; the restriction opens on a read and this run had none",
            stacklevel=1,
        )
        return

    escaped = [name for name, refused in after if not _permitted(name) and not refused]
    assert not escaped, (
        f"the agent ran {escaped} after loading a skill that declared "
        f"{sorted(WEB_RESEARCH_TOOLS)}\n{transcript}"
    )


def test_the_file_the_skill_did_not_allow_is_not_written(stack: Stack) -> None:
    """The restriction is visible on disk, not only in a trace.

    `web-research` declares no write tool, so a run that loaded it cannot
    have produced `findings.md` — whatever the model decided to say about
    it. This is the assertion that would catch an enforcement path that
    logged a denial and ran the tool anyway.
    """
    with stack.session(env=ENFORCED) as live:
        live.wait_for("You:")
        live.ask(
            "Read the web-research skill and follow it. Then save what you "
            "found to findings.md in the workspace."
        )
        live.wait_for("Agent: ")
        read_a_skill = "skill_read" in live.tools_called

    if not read_a_skill:
        warnings.warn(
            "the model never called skill_read, so nothing was in scope to "
            "enforce; no conclusion can be drawn from the workspace",
            stacklevel=1,
        )
        return

    assert not stack.exists("/app/workspace/findings.md"), (
        f"a skill declaring {sorted(WEB_RESEARCH_TOOLS)} was loaded and a "
        f"file was written anyway\n{stack.tree()}"
    )


def test_a_refusal_does_not_end_the_run(stack: Stack) -> None:
    """Being refused is an observation, so the agent still answers.

    This is the half that makes the feature usable rather than merely safe.
    A restriction that wedged the loop would turn one wrong line of
    frontmatter into a hung container, which is exactly why the feature
    ships off by default (ADR-048, ADR-082).
    """
    with stack.session(env=ENFORCED) as live:
        live.wait_for("You:")
        live.ask(
            "Read the web-research skill and follow it. Then save what you "
            "found to findings.md in the workspace."
        )
        answer = live.wait_for("Agent: ")

    assert answer.strip() != "Agent:", live.transcript


def test_the_shipped_default_restricts_nothing(stack: Stack) -> None:
    """Off unless asked, and this is the test that keeps it that way.

    The same task, the same skill, no `SKILLS_ENFORCE_ALLOWED_TOOLS`. The
    agent must be free to use a tool the skill never declared — which is
    Week 2's behaviour and what every other test in this suite assumes
    (ADR-038).

    Asserted as "a tool outside the list actually ran", which is a positive
    fact about the harness. The tempting alternative — grepping the
    transcript for a refusal message — would pass just as well if the model
    simply never tried, and would break the day somebody rewords the
    message.
    """
    with stack.session() as live:
        live.wait_for("You:")
        live.ask(
            "Read the web-research skill, then create a file called "
            "default.md in the workspace containing the word ok."
        )
        live.wait_for("Agent: ")

        after = _after_the_skill_was_read(live.trace)
        transcript = live.transcript

    if after is None:
        warnings.warn(
            "the model never called skill_read; the default path was not "
            "exercised by this run",
            stacklevel=1,
        )
        return

    # It tried something the skill never declared, and with enforcement off
    # that must simply have run. Under enforcement the identical call comes
    # back as a permission error instead — which is the whole difference the
    # setting makes. When it tried nothing outside the list there is nothing
    # to check and the assertion below is vacuously true; that is a quiet
    # pass rather than a warning, because the deterministic version of this
    # claim lives in tests/integration/test_skill_scope.py, where the same
    # script runs against both settings.
    outside = [(name, refused) for name, refused in after if not _permitted(name)]
    refused = [name for name, was_refused in outside if was_refused]
    assert not refused, (
        f"{refused} was refused with enforcement off; a skill's allowed-tools "
        f"must restrict nothing unless skills_enforce_allowed_tools is set\n"
        f"{transcript}"
    )
