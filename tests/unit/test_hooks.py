"""``CompositeHook``: one lifecycle, several observers.

The loop takes a single hook. From Week 2 there are three independent
concerns — memory injects and records, skills advertise, the CLI traces —
so something has to fan the lifecycle out without ``AgentLoop`` learning how
many observers exist.
"""

from typing import Any

import pytest
from harness.context import RunContext
from harness.hooks import CompositeHook, Hook, NoOpHook
from harness.tools.base import ToolOutcome

# Every callback on the protocol. Listed once so a new lifecycle event
# cannot be added to Hook and forgotten in CompositeHook: the fan-out test
# below iterates this, and a missing method would raise AttributeError.
_CALLBACKS = (
    "on_run_start",
    "before_llm",
    "after_llm",
    # Added with the span rewrite: `after_llm` fires only on success, so a
    # failed request produced no model-call telemetry at all (ADR-094).
    "on_llm_error",
    "before_tool",
    "after_permission",
    "after_tool",
    "on_run_end",
)


class _Recorder:
    """A hook that records which callbacks it received, in order."""

    def __init__(self, label: str, log: list[str]) -> None:
        self.label = label
        self._log = log

    def on_run_start(self, context: RunContext) -> None:
        self._log.append(f"{self.label}:on_run_start")

    def before_llm(self, context: RunContext) -> None:
        self._log.append(f"{self.label}:before_llm")

    def after_llm(self, context: RunContext, response: Any) -> None:
        self._log.append(f"{self.label}:after_llm")

    def on_llm_error(self, context: RunContext, error: BaseException) -> None:
        self._log.append(f"{self.label}:on_llm_error")

    def before_tool(self, name: str, arguments: dict[str, Any]) -> None:
        self._log.append(f"{self.label}:before_tool")

    def after_permission(self, name: str, outcome: str) -> None:
        self._log.append(f"{self.label}:after_permission")

    def after_tool(
        self, name: str, result: str, outcome: ToolOutcome | None = None
    ) -> None:
        self._log.append(f"{self.label}:after_tool")

    def on_run_end(self, context: RunContext) -> None:
        self._log.append(f"{self.label}:on_run_end")


def test_composite_satisfies_the_hook_protocol() -> None:
    """It has to be usable wherever a single hook was.

    Checked member by member rather than with ``isinstance``: ``Hook`` is a
    plain ``Protocol``, not ``@runtime_checkable``, and it stays that way on
    purpose — the seam is a static contract that mypy enforces, and making
    it runtime-checkable would only verify that the method *names* exist.
    """
    for callback in _CALLBACKS:
        assert callable(getattr(CompositeHook, callback, None)), callback
    # Every member of the protocol is covered, so the list above cannot go
    # stale without this failing.
    assert set(_CALLBACKS) == {
        member for member in vars(Hook) if not member.startswith("_")
    }


def test_every_callback_reaches_every_hook() -> None:
    """Every callback fans out; none is silently dropped.

    A dropped ``on_run_start`` would mean a run with no memory injected,
    which produces a plausible-looking answer and no error at all — the
    worst kind of failure to debug. ``after_permission`` is in the list for
    the same reason with a sharper edge: it is the only record that a tool
    call was refused, and losing it makes a denied run look like a run that
    never asked (ADR-081).
    """
    log: list[str] = []
    composite = CompositeHook(_Recorder("a", log), _Recorder("b", log))
    context = RunContext(task="a task")

    composite.on_run_start(context)
    composite.before_llm(context)
    composite.after_llm(context, None)
    composite.on_llm_error(context, RuntimeError("boom"))
    composite.before_tool("read_file", {})
    composite.after_permission("read_file", "allow")
    composite.after_tool("read_file", "ok")
    composite.on_run_end(context)

    for callback in _CALLBACKS:
        assert f"a:{callback}" in log
        assert f"b:{callback}" in log


def test_a_raising_hook_is_not_swallowed() -> None:
    """A broken hook is a harness defect, not something the model did.

    Swallowing it would produce runs that quietly lack memory. Hooks with a
    reason to tolerate failure handle it themselves, close to the failure
    they expect — the sessions-log write is the example.
    """

    class _Broken(NoOpHook):
        def on_run_start(self, context: RunContext) -> None:
            raise RuntimeError("boom")

    composite = CompositeHook(_Broken())

    with pytest.raises(RuntimeError, match="boom"):
        composite.on_run_start(RunContext(task="a task"))
