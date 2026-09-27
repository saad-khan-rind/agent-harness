"""A failing tool is an observation, not the end of the run.

ADR-025 makes every tool failure a message the model reads and can act on,
and ADR-048 stops a tool that keeps failing from looping forever. Both are
claims about a *conversation* — the model reads the error, changes what it
is doing, and finishes the task — which is the one thing a scripted model
cannot demonstrate, because the script decides what it does next.

Two tests, and the difference between them is worth stating because it
changed how they were written.

The first **guarantees** the error. A file either exists or it does not, and
a model cannot know which without asking, so the failing call is certain to
happen.

The second cannot guarantee it, and it has now taught the same lesson twice.
It first asked for ``Asia/Tokio`` — an obvious typo — and asserted the tool
was called twice; `gemma4-31b-it` silently corrected the spelling and called
once, which is *better* behaviour, and the test failed it for that. Rewritten
to require only that the clock was read at all, it then failed a run where
the model answered "that is not a timezone" without calling anything —
better again, since the question has no correct clock reading.

The lesson is that there is no route to assert here, not that the last route
was the wrong one. What is left is what a real run actually owes us: it
reaches an answer, and it does not state a time it never read. The route
itself is forced one tier down, with a scripted model, where it can be.
"""

import re

import pytest
from tests.e2e.conftest import WORKSPACE, Stack

pytestmark = pytest.mark.e2e


def test_a_missing_file_is_an_error_the_agent_recovers_from(stack: Stack) -> None:
    """The deterministic one: the error is certain, so the assertion can be hard.

    Nothing in the workspace exists yet, so ``read_file`` must fail — there
    is no prompt wording and no model cleverness that avoids it. What is
    under test is everything after: the message reaching the model as a
    failure, the model understanding it, and the run finishing the job
    anyway, all without a human.
    """
    run = stack.run(
        "Read notes/summary.md and tell me what it says. If it is not there, "
        "create it containing the single line: no summary yet."
    )
    run.assert_completed()

    assert run.tool_errors, (
        f"reading a missing file did not surface as an error:\n{run.stdout}"
    )
    assert "read_file" in run.tools_called, run.tools_called
    body = stack.read(f"{WORKSPACE}/notes/summary.md")
    assert "no summary yet" in body.lower(), (
        f"the agent read the error but never finished the task:\n{body}"
    )


# A clock reading as a model writes one: 14:30, 2:05 pm, 09:00 CEST. Matched
# rather than searching for ``":"`` because an answer may legitimately contain
# a colon while claiming no time at all ("Note: Passau uses Europe/Berlin").
_A_TIME = re.compile(r"\b\d{1,2}:\d{2}\b")


def test_a_rejected_timezone_does_not_end_the_run(stack: Stack) -> None:
    """The opportunistic one: assert the contract, not the route.

    ``Europe/Passau`` is well-formed, plausible, and not an IANA zone —
    Passau has a university, not a timezone. A model that passes it gets the
    server's rejection naming the argument, and should then reach for
    ``Europe/Berlin``.

    A model that skips the mistake has done better, not worse — and a model
    that answers *"that is not a timezone"* without calling anything has
    done better still, because the question has no correct clock reading.
    An earlier version of this test required the clock call and failed a run
    that did exactly that, which is the same mistake it was written to fix
    one paragraph up. The route is the agent's business.

    So the claim is narrowed to what a real run genuinely owes us, and it is
    still not vacuous: **the agent may not state a time it never read.** If
    it called the clock, the answer should carry a reading; if it called
    nothing, the answer must not invent one. Either way it has to reach an
    answer at all, which ``assert_completed`` is.

    Retrying with ``Europe/Berlin`` is not required either, for the same
    reason. A model that reads the rejection and then explains that the zone
    does not exist has recovered from it — it understood the error and
    answered — and a test that demanded a second call would be back to
    grading the route.

    The forced version of the recovery — the rejection arriving as an
    ``Error:`` observation the next turn can act on, which is what upstream's
    contract got wrong by returning a *successful* call carrying
    ``{"error": ...}`` — is
    ``tests/integration/test_mcp_meeting_agent.py::test_a_mistyped_zone_on_an_inherited_tool_is_recoverable``.
    That tier can script the mistake; this one cannot (ADR-071, ADR-074).
    """
    run = stack.run("What time is it right now in Europe/Passau?")
    run.assert_completed()

    clock_calls = [n for n in run.tools_called if n == "time_plus__get_current_time"]

    if clock_calls:
        # It read a clock, so it has a reading to report.
        assert _A_TIME.search(run.answer), (
            f"the clock was read but the answer carries no time: {run.answer!r}"
        )
    else:
        # It read nothing, so there is no time it could honestly give.
        assert not _A_TIME.search(run.answer), (
            "the agent stated a time without reading a clock: "
            f"{run.answer!r} (tools called: {run.tools_called})"
        )


def test_a_request_to_report_back_still_gets_the_work_done(stack: Stack) -> None:
    """The file exists, not only the sentence saying it does (ADR-116).

    Worded exactly as the failing case was found: the trailing "then tell me
    you did" made the lab model reply "I have created the file" four runs
    out of four with no tool call and an empty workspace. The claim check
    sends that reply back once, and the model then writes the file.
    """
    run = stack.run(
        "Create a file called engine-check.md containing the word podman, "
        "then tell me you did.",
        permissions="allow_all",
    )
    run.assert_completed()

    assert stack.exists(f"{WORKSPACE}/engine-check.md"), (
        f"the answer claims a file the workspace does not have: {run.answer!r}\n"
        f"{stack.tree()}"
    )
    assert "podman" in stack.read(f"{WORKSPACE}/engine-check.md")
