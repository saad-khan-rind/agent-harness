"""Permission modes against a model that genuinely wants to do the thing.

ADR-057 puts one decision point in front of every tool call, keyed on the
tool's declared ``risk``. The deterministic version of this is
``tests/integration/test_permission_modes.py``, where the scripted model
asks for a write and the policy refuses it.

What that version cannot show is the interesting half. A refusal is only
worth anything if the agent then *tells the user* rather than claiming
success, and whether it does is a property of the error text and the system
prompt — a scripted model says whatever its script says. So the assertions
here are of two kinds: the file did not change, and the reply did not
pretend otherwise.
"""

import pytest
from tests.e2e.conftest import WORKSPACE, Stack

pytestmark = pytest.mark.e2e


def test_deny_mutations_makes_the_run_read_only(stack: Stack) -> None:
    """The read-only tier, with the agent asked squarely to write.

    ``deny_mutations`` is the mode for showing someone the agent without
    letting it touch anything, so the property that matters is absolute: no
    file changed, none created, whatever route the model tried.
    """
    stack.write(f"{WORKSPACE}/notes/plan.md", "# Plan\noriginal\n")

    run = stack.run(
        "Add a line saying 'ship it' to notes/plan.md.",
        permissions="deny_mutations",
    )
    run.assert_completed()

    assert stack.read(f"{WORKSPACE}/notes/plan.md") == "# Plan\noriginal\n"
    assert stack.find_text(WORKSPACE, "ship it") == [], (
        f"the text was written somewhere despite deny_mutations; tree:\n{stack.tree()}"
    )


def test_a_denied_agent_says_so_instead_of_claiming_success(stack: Stack) -> None:
    """The half only a real model can demonstrate.

    A refusal the agent hides is worse than no refusal at all: the user
    walks away believing the edit landed. The permission error is phrased
    for a model to read and relay, and this asserts that it was relayed —
    loosely, because there are many honest ways to say "I was not allowed
    to", and none of them is prescribed.
    """
    stack.write(f"{WORKSPACE}/notes/plan.md", "# Plan\noriginal\n")

    run = stack.run(
        "Add a line saying 'ship it' to notes/plan.md.",
        permissions="deny_mutations",
    )
    run.assert_completed()

    admitted = any(
        word in run.answer
        for word in ("not allow", "denied", "permission", "cannot", "can't", "unable",
                     "read-only", "read only", "refused", "blocked")
    )  # fmt: skip
    assert admitted, (
        f"the agent did not tell the user the write was refused: {run.answer!r}"
    )


def test_reading_still_works_when_writing_does_not(stack: Stack) -> None:
    """The mode is read-*only*, not off.

    Worth its own test because the plausible over-correction is a policy
    that refuses anything it is unsure about, which would make the mode
    useless: an agent that cannot read cannot answer a question either, and
    nobody would run a demo in it.
    """
    stack.write(f"{WORKSPACE}/notes/plan.md", "# Plan\nship the harness on Friday\n")

    run = stack.run(
        "What does notes/plan.md say?",
        permissions="deny_mutations",
    )
    run.assert_completed()

    assert "read_file" in run.tools_called, (
        f"reading was refused too, which is not what deny_mutations means: "
        f"{run.tools_called}"
    )
    assert "friday" in run.answer, (
        f"the file's content never reached the reply: {run.answer!r}"
    )


def test_confirm_mutations_refuses_rather_than_hanging_with_no_terminal(
    stack: Stack,
) -> None:
    """The unattended case, which is what a container run always is.

    ``confirm_mutations`` needs somebody to ask. A one-shot ``docker run``
    has no terminal attached, so the honest outcomes are two: refuse, or
    block forever waiting for input nobody can give. The harness refuses and
    names the flag to use instead.

    This test exists because the second outcome is the kind of bug that only
    shows up in front of an audience.
    """
    stack.write(f"{WORKSPACE}/notes/plan.md", "# Plan\noriginal\n")

    run = stack.run(
        "Add a line saying 'ship it' to notes/plan.md.",
        permissions="confirm_mutations",
    )
    run.assert_completed()

    assert stack.read(f"{WORKSPACE}/notes/plan.md") == "# Plan\noriginal\n"
    assert "no terminal to ask" in run.stdout, (
        f"the refusal did not explain itself:\n{run.stdout}"
    )


def test_the_default_mode_lets_the_work_happen(stack: Stack) -> None:
    """The control for the three tests above.

    Without this, every assertion in this file is satisfied by an agent that
    can do nothing at all. Same task, same file, default permissions: the
    line lands.
    """
    stack.write(f"{WORKSPACE}/notes/plan.md", "# Plan\noriginal\n")

    run = stack.run("Add a line saying 'ship it' to notes/plan.md.")
    run.assert_completed()

    body = stack.read(f"{WORKSPACE}/notes/plan.md")
    assert "ship it" in body.lower(), f"the edit did not land:\n{body}"
    assert "original" in body, f"the existing content was lost:\n{body}"
