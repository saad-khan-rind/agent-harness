"""Keeping ``require_user_confirmation`` answerable with children in flight.

§8.4 requires a permission policy to support three outcomes, and the third
one rests on an assumption nothing else in the harness depends on: one
terminal, one person, one question at a time. Sequential delegation gave that
for free. A batched ``delegate`` does not, and a wrapper that only *claimed*
to serialise would leave the requirement broken in exactly the arrangement
nobody tests by hand.

So the tests here are about contention rather than about wiring. The lock is
checked by holding it and observing that a second caller waits; the
attribution is checked on what the confirmer was actually told; and the
fallback is checked against a confirmer that has never heard of sub-agents,
because ``tests/fakes.py`` and every existing test double is one.
"""

import threading
from pathlib import Path

import pytest
from harness.subagents.confirm import AttributedConfirmer, AttributingConfirmer
from harness.tools import build_registry
from harness.tools.base import Tool


@pytest.fixture
def write_file(workspace: Path) -> Tool:
    """A real mutating tool, the kind that raises a confirmation."""
    return build_registry(workspace).get("write_file")


class _Plain:
    """A confirmer that knows nothing about sub-agents, as fakes do."""

    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.asked: list[str] = []

    def confirm(self, tool: Tool, arguments: dict[str, object]) -> bool:
        """Record the tool's real name and answer."""
        self.asked.append(tool.name)
        return self.answer


class _Attributing:
    """A confirmer that implements the optional attribution seam."""

    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.asked: list[tuple[str, str]] = []

    def confirm(self, tool: Tool, arguments: dict[str, object]) -> bool:
        """Unused: the wrapper prefers :meth:`confirm_for`."""
        raise AssertionError("confirm_for should have been chosen")

    def confirm_for(self, asker: str, tool: Tool, arguments: dict[str, object]) -> bool:
        """Record who asked and about what."""
        self.asked.append((asker, tool.name))
        return self.answer


def test_the_prompt_names_the_sub_agent_that_is_asking(write_file: Tool) -> None:
    """ "Allow write_file?" with three agents running is unanswerable."""
    inner = _Attributing()
    wrapper = AttributedConfirmer(inner, threading.Lock(), "writer")

    assert wrapper.confirm(write_file, {"path": "out.md"}) is True
    assert inner.asked == [("writer sub-agent", "write_file")]


def test_the_tool_itself_is_passed_through_unchanged(write_file: Tool) -> None:
    """Attribution must not travel inside ``tool.name``.

    ``PermissionTiers.tier_of`` matches the deny-list and the operator's
    ``permission_rules`` on that exact string, so a decorated name would be
    explained to the person by the wrong rule — a misleading sentence in the
    one prompt that exists to be read carefully.
    """
    inner = _Attributing()
    AttributedConfirmer(inner, threading.Lock(), "writer").confirm(write_file, {})

    (_asker, name) = inner.asked[0]
    assert name == "write_file"


def test_a_confirmer_without_the_seam_still_gets_asked(write_file: Tool) -> None:
    """The fallback, which is what every existing test double takes.

    The attribution seam is optional on purpose: adding it to ``Confirmer``
    would have invalidated every implementation that already exists.
    """
    inner = _Plain(answer=False)
    wrapper = AttributedConfirmer(inner, threading.Lock(), "researcher")

    assert wrapper.confirm(write_file, {}) is False
    assert inner.asked == ["write_file"]


def test_a_refusal_is_carried_back_unchanged(write_file: Tool) -> None:
    """The wrapper decides nothing; it only sequences and labels."""
    inner = _Attributing(answer=False)

    assert (
        AttributedConfirmer(inner, threading.Lock(), "writer").confirm(write_file, {})
        is False
    )


def test_two_children_cannot_be_asked_at_the_same_time(write_file: Tool) -> None:
    """The property the lock exists for, observed rather than assumed.

    One child is held inside the confirmer. A second child on another thread
    must not get in while it is there — if it did, two ``input()`` prompts
    would be interleaved on one terminal and neither could be answered
    honestly.
    """
    inside = threading.Event()
    release = threading.Event()
    overlapped = threading.Event()
    order: list[str] = []

    class _Slow:
        """A confirmer that blocks until it is released."""

        def confirm(self, tool: Tool, arguments: dict[str, object]) -> bool:
            """Block on the first call, so the second has time to collide."""
            if not inside.is_set():
                inside.set()
                order.append("first-in")
                # If the lock is not held, the second thread reaches here and
                # sets the flag before this one is released.
                if release.wait(timeout=2.0):
                    order.append("first-out")
                return True
            overlapped.set()
            order.append("second-in")
            return True

    lock = threading.Lock()
    inner = _Slow()
    first = AttributedConfirmer(inner, lock, "writer")
    second = AttributedConfirmer(inner, lock, "researcher")

    thread = threading.Thread(target=lambda: first.confirm(write_file, {}))
    thread.start()
    assert inside.wait(timeout=2.0), "the first child never reached the confirmer"

    other = threading.Thread(target=lambda: second.confirm(write_file, {}))
    other.start()
    # Give the second thread a real chance to barge in.
    assert not overlapped.wait(timeout=0.3), "two children were asked at once"

    release.set()
    thread.join(timeout=2.0)
    other.join(timeout=2.0)

    assert order == ["first-in", "first-out", "second-in"]


def test_the_attribution_seam_is_recognised_structurally() -> None:
    """The check is ``isinstance`` against a runtime protocol, not a flag.

    Worth pinning: if the protocol stopped being ``runtime_checkable`` the
    wrapper would silently take the fallback path for every confirmer, and
    every prompt would lose its attribution without a test failing.
    """
    assert isinstance(_Attributing(), AttributingConfirmer)
    assert not isinstance(_Plain(), AttributingConfirmer)
