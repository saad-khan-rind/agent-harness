"""Week 2's MCP requirement, with the model choosing the tools.

The brief asks for "at least one end-to-end test covering runtime MCP tool
discovery, invocation, and result handling". All three happen here in one
container run and none of them is arranged:

* **Discovery** — nothing in the image knows the tool names ahead of time.
  ``config.yaml`` names three programs; the client launches each as a
  subprocess, completes the handshake and asks what it offers. The names
  that come back are the ones the model then sees.
* **Invocation** — the model picks. The prompts below never mention a tool,
  a server or a prefix, because the interesting question is whether a
  mid-sized model can work out that "what time is it in Tokyo" is a job for
  a remote tool rather than for its own guess.
* **Result handling** — the tool's answer goes into the context and the
  model's final reply has to reflect it, which is what the assertions read.

The deterministic counterpart is ``tests/integration/test_mcp_tools.py``
(a toy server) and ``test_mcp_time_plus.py`` (the shipped one). Those prove
the transport works. This proves the feature does (ADR-074).
"""

import pytest
from tests.e2e.conftest import WORKSPACE, Stack

pytestmark = pytest.mark.e2e


def test_a_remote_tool_is_discovered_invoked_and_its_result_used(
    stack: Stack,
) -> None:
    """The brief's sentence, in one run: discovery, invocation, result handling.

    The prompt names a city, not a tool. Getting this right means the model
    saw ``time_plus__get_current_time`` in a list that was assembled at
    runtime from a subprocess, understood the prefix belonged to a server,
    called it, and then used what came back — four things, only the last of
    which a unit test can see.

    The assertion on the answer is deliberately weak: a timezone name, not a
    time. The actual clock reading changes every second and asserting on it
    would be asserting that the test runs fast.
    """
    run = stack.run("What time is it right now in Tokyo? Use your tools.")
    run.assert_completed()

    called = [name for name in run.tools_called if name.startswith("time_plus__")]
    assert called, f"no remote tool was reached at all: {run.tools_called}"
    assert "time_plus__get_current_time" in run.tools_called, (
        f"the clock was not read from the server: {run.tools_called}"
    )
    assert "tokyo" in run.answer or "japan" in run.answer or ":" in run.answer, (
        f"the tool result never reached the reply: {run.answer!r}"
    )


def test_the_new_tool_the_fork_added_is_reachable_by_a_real_agent(
    stack: Stack,
) -> None:
    """``find_meeting_slot`` — the capability upstream's time server lacks.

    ADR-044's claim is that the fork adds something real, and the way to
    test a claim like that is to ask for the thing upstream could not do.
    Upstream can convert 14:30 Berlin to Tokyo; it cannot answer "when can
    these three people meet", which needs working hours intersected across
    zones.

    No assertion on *which* slot: the right answer depends on the working
    hours the model chooses to pass, and there are several defensible sets.
    What is asserted is that it asked the server instead of doing timezone
    arithmetic in its head, which is the part it gets wrong.
    """
    run = stack.run(
        "Find an hour on 14 September 2026 that works for people in "
        "Europe/Berlin, Asia/Karachi and Australia/Sydney."
    )
    run.assert_completed()

    assert "time_plus__find_meeting_slot" in run.tools_called, (
        f"the meeting-slot tool was never called: {run.tools_called}"
    )


def test_a_remote_result_can_be_carried_into_a_local_tool(stack: Stack) -> None:
    """The composition the whole integration exists for.

    A remote tool and a built-in one, in one task, with the output of the
    first becoming the input of the second. This is the thing that would
    still be broken if remote tools were registered in a separate list, or
    if their results were printed instead of appended to the context — and
    it is invisible to any test that exercises one tool at a time.
    """
    run = stack.run(
        "How many working days are there from Monday 2 March 2026 to "
        "Friday 18 December 2026, counting both of those days themselves, "
        "if 6 April, 1 May and 25 May 2026 are public holidays? "
        "Save just the number in a file called report/days.md."
    )
    run.assert_completed()

    assert "time_plus__count_working_days" in run.tools_called, (
        f"the count was not computed by the server: {run.tools_called}"
    )
    body = stack.read(f"{WORKSPACE}/report/days.md")
    # Mon 2 March to Fri 18 December 2026, both included, is 210 weekdays;
    # the three holidays all fall on weekdays, leaving 207.
    #
    # A long range with holidays, not two weeks, and that is the fix for a
    # real failure: asked about 14–25 September, the model counted ten days
    # in its head, wrote the right number, and never called the server — so
    # the test failed a correct answer for the route it took (ADR-078). Ten
    # months less three holidays is arithmetic nobody does unaided.
    #
    # "counting both of those days themselves" is not padding. The first
    # version of this prompt said "between 2026-09-14 and 2026-09-25", and
    # the agent answered 9 — it set `include_end_date` to false, which is a
    # perfectly reasonable reading of "between" and made the test an
    # argument about English rather than about the harness. The tool's
    # description now states its default for the same reason (ADR-078).
    assert "207" in body, f"the working-day count did not reach the file:\n{body}"
