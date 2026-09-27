"""Lifecycle hooks for monitoring and gates.

The default is :class:`NoOpHook`. Prometheus does not live here: metrics are
an *implementation* of this protocol in :mod:`harness.observability`, so this
module keeps no dependency on a metrics library and the loop keeps no
mention of one (ADR-018, ADR-081).

A hook receives the live :class:`RunContext`, so the seam is not read-only:
it is also how harness-supplied material enters a run. Long-term memory uses
``on_run_start`` to inject and ``on_run_end`` to record, which is what keeps
``loop.py`` free of any mention of memory (ADR-034).
"""

import inspect
from typing import Any, Protocol

from harness.context import RunContext
from harness.tools.base import ToolOutcome


class Hook(Protocol):
    """Callbacks around loop and tool steps.

    Implementations must not raise on no-op; a missing hook is equivalent
    to doing nothing.
    """

    def on_run_start(self, context: RunContext) -> None:
        """Called once when a user task begins."""

    def before_llm(self, context: RunContext) -> None:
        """Called immediately before an LLM request."""

    def after_llm(self, context: RunContext, response: Any) -> None:
        """Called after an LLM response is received."""

    def on_llm_error(self, context: RunContext, error: BaseException) -> None:
        """Called when an LLM request failed instead of returning.

        The gap this closes: ``after_llm`` only fires on success, so until
        now a dead endpoint produced *no* model-call telemetry at all. The
        run was marked failed and the reason — 429, timeout, bad model name
        — existed nowhere, which is precisely the case §2 of our brief means
        by "do not assume an HTTP 200 means the agent succeeded", read in
        the other direction (ADR-094).

        Fires once per failed request, after the client's own retries are
        exhausted, and the loop re-raises immediately afterwards. A hook
        here must therefore be quick and must not swallow anything.

        Args:
            context: The run that was making the request.
            error: The exception about to propagate, normally
                :class:`~harness.llm.protocol.LLMError`.
        """

    def before_tool(self, name: str, arguments: dict[str, Any]) -> None:
        """Called before ``ToolRegistry.execute``."""

    def after_permission(self, name: str, outcome: str) -> None:
        """Called once per tool call, after the permission policy decided.

        The decision is reported as a value, not inferred later from an
        ``Error:`` string in the observation. A parsed message cannot tell a
        policy denial from a tool that failed on its own, and a dashboard
        that confuses the two is worse than no dashboard (ADR-058, ADR-081).

        Args:
            name: Tool the model asked for.
            outcome: ``allow``, ``deny``, ``confirmed`` or ``declined``.
                The last two are the halves of
                ``require_user_confirmation`` — whether the person said yes.
                "We asked" and "they agreed" are different facts and the
                permission panel needs both.
        """

    def after_tool(
        self, name: str, result: str, outcome: ToolOutcome | None = None
    ) -> None:
        """Called after a tool returns an observation.

        Args:
            name: Tool the model called.
            result: The observation appended to the context. Still the first
                positional argument, because every existing hook prints or
                measures it and none of them should have to change.
            outcome: The typed result the registry produced, when the caller
                has one. It carries ``ok`` and ``error_type``, so a hook can
                say *why* a call failed instead of inferring it from the
                text.

                Optional, and the default is not laziness. ``after_tool``
                also fires for a call the loop never dispatched — a tool
                retired by the failure guard (ADR-048) — where there is no
                outcome to pass, and inventing one would be a hook told a
                tool ran when it did not.
        """

    def on_run_end(self, context: RunContext) -> None:
        """Called when the loop terminates (answer, error, or cap)."""


class NoOpHook:
    """Default hook that records nothing."""

    def on_run_start(self, context: RunContext) -> None:
        """Ignore run start."""

    def before_llm(self, context: RunContext) -> None:
        """Ignore LLM start."""

    def after_llm(self, context: RunContext, response: Any) -> None:
        """Ignore LLM completion."""

    def on_llm_error(self, context: RunContext, error: BaseException) -> None:
        """Ignore a failed LLM request."""

    def before_tool(self, name: str, arguments: dict[str, Any]) -> None:
        """Ignore tool start."""

    def after_permission(self, name: str, outcome: str) -> None:
        """Ignore the permission decision."""

    def after_tool(
        self, name: str, result: str, outcome: ToolOutcome | None = None
    ) -> None:
        """Ignore tool completion."""

    def on_run_end(self, context: RunContext) -> None:
        """Ignore run end."""


class CompositeHook:
    """Fan one lifecycle out to several hooks, in order.

    The loop takes a single hook, which was enough while the only hook was
    the CLI's trace. From Week 2 there are two independent concerns — the
    trace prints, memory injects and records — and Week 3 adds metrics.
    Composing them here keeps ``AgentLoop`` unaware of how many observers
    exist, which is the point of the seam.

    Order is the order given, so a hook that mutates the context (memory)
    can be placed before one that only reports on it.

    Exceptions are **not** swallowed. A hook that raises is a defect in the
    harness rather than something the model did, and hiding it would produce
    runs that silently lack memory. Individual hooks that have a reason to
    tolerate failure handle it themselves, close to the failure they expect.
    """

    def __init__(self, *hooks: Hook) -> None:
        """Args:
        *hooks: Hooks to notify, in call order.
        """
        self._hooks = hooks
        # Which hooks accept the third argument of ``after_tool``. Decided
        # once, here, rather than guessed per call from a ``TypeError``:
        # catching ``TypeError`` around a hook call would also catch one
        # raised *inside* it and silently retry with fewer arguments, which
        # turns a real bug in somebody's hook into a mysterious double
        # invocation. Reading the signature asks the question directly.
        #
        # This exists because ``after_tool`` grew a parameter (ADR-094) and
        # a lifecycle seam is a published interface: hooks written against
        # the older two-argument shape — a fixture, an open branch,
        # anything not in this repository — keep working unchanged.
        self._takes_outcome = tuple(_accepts_outcome(hook) for hook in hooks)

    @property
    def hooks(self) -> tuple[Hook, ...]:
        """The composed hooks, in call order (``on_run_end`` runs them reversed)."""
        return tuple(self._hooks)

    def on_run_start(self, context: RunContext) -> None:
        """Notify every hook that a run began."""
        for hook in self._hooks:
            hook.on_run_start(context)

    def before_llm(self, context: RunContext) -> None:
        """Notify every hook before an LLM request."""
        for hook in self._hooks:
            hook.before_llm(context)

    def after_llm(self, context: RunContext, response: Any) -> None:
        """Notify every hook after an LLM response."""
        for hook in self._hooks:
            hook.after_llm(context, response)

    def on_llm_error(self, context: RunContext, error: BaseException) -> None:
        """Notify every hook that an LLM request failed.

        ``getattr`` rather than a direct call, and for the same reason as
        the arity check above: this callback is newer than the protocol, and
        a hook that predates it should stay silent rather than raise
        ``AttributeError`` into a loop that is already handling a failure.
        Adding a callback must not be able to turn one error into two.
        """
        for hook in self._hooks:
            callback = getattr(hook, "on_llm_error", None)
            if callback is not None:
                callback(context, error)

    def before_tool(self, name: str, arguments: dict[str, Any]) -> None:
        """Notify every hook before a tool call."""
        for hook in self._hooks:
            hook.before_tool(name, arguments)

    def after_permission(self, name: str, outcome: str) -> None:
        """Notify every hook of the permission decision."""
        for hook in self._hooks:
            hook.after_permission(name, outcome)

    def after_tool(
        self, name: str, result: str, outcome: "ToolOutcome | None" = None
    ) -> None:
        """Notify every hook after a tool observation.

        Hooks that accept the typed outcome get it; hooks written against
        the two-argument shape are called the way they expect.
        """
        for hook, takes_outcome in zip(self._hooks, self._takes_outcome, strict=True):
            if takes_outcome:
                hook.after_tool(name, result, outcome)
            else:
                hook.after_tool(name, result)

    def on_run_end(self, context: RunContext) -> None:
        """Notify every hook that a run ended, in **reverse** order.

        The one callback that does not run in composition order, and the
        reason is the same one that makes ``finally`` blocks unwind
        backwards: hooks set up in order, so they must tear down in the
        opposite one.

        It became load-bearing with the span rewrite. The telemetry hook
        opens the ``agent.run`` span in ``on_run_start`` and must therefore
        be *first*, so that every other hook's work happens inside it — the
        memory hook's retrieval included, which otherwise opened a span with
        nothing on the tracer's stack, became a root span of its own, and
        wrote a second trace file for the same run. Closing in the same
        order would then close the run span while memory's write span was
        still open, which is the same bug from the other end (ADR-093).

        Reverse order fixes both and costs nothing: no hook here depends on
        another having ended first.
        """
        for hook in reversed(self._hooks):
            hook.on_run_end(context)


def _accepts_outcome(hook: Hook) -> bool:
    """Whether ``hook.after_tool`` takes the typed outcome.

    Args:
        hook: Any hook implementation.

    Returns:
        ``True`` when the method accepts a third positional argument (or
        ``**kwargs``). A hook whose signature cannot be read — a C
        callable, a mock — is assumed to accept it, because every hook in
        this repository does and a mock will not mind either way.
    """
    callback = getattr(hook, "after_tool", None)
    if callback is None:
        return False
    try:
        parameters = inspect.signature(callback).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        return True
    if any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        or parameter.kind is inspect.Parameter.VAR_POSITIONAL
        for parameter in parameters.values()
    ):
        return True
    positional = [
        parameter
        for parameter in parameters.values()
        if parameter.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    return len(positional) >= 3
