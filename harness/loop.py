"""ReAct control plane.

This module is the *Loop* of the brief (Section 2) and the "Planner / Main
Agent" box of our architecture diagram: a state machine that alternates
between a reasoning state (an LLM call) and an acting state (a tool call)
until a terminal state.

    reason -> act -> observe -> reason -> ... -> final answer

Terminal states, all three of which are guarded so the machine always stops:

* The model replies with text and no tool calls -> final answer.
* The same call repeats with the same result -> the run stops, no progress.
* The iteration cap, when one is configured, is reached -> cut off, says so.
* The LLM endpoint fails -> :class:`LLMError` propagates to the CLI.

Every tool call also passes a permission check before it runs. That check
lives here, and only here, because the brief (§8.4) asks for permission
management applied consistently rather than as isolated special cases: a
file tool, a memory tool, a skill tool and a tool discovered on an MCP
server are all dispatched through this one method, so policing that method
polices all of them, and none of them has to know (ADR-057).

Dependency rule (from ``AGENTS.md``): this module imports ``LLMClient``,
``ToolRegistry``, ``RunContext``, ``Hook``, ``PermissionPolicy`` and
``Confirmer`` — never a concrete file tool, MCP client, or memory store.
That is what keeps Weeks 2 and 3 additive. Every one of those imports is a
protocol or a registry, so this file does not change when capabilities are
added.
"""

import json
from itertools import count

from harness.context import RunContext
from harness.hooks import CompositeHook, Hook, NoOpHook
from harness.llm.protocol import LLMClient, LLMResponse, ToolCall
from harness.observability.errors import (
    GUARD_TOOL_RETIRED,
    PERMISSION_DECLINED,
    PERMISSION_DENIED,
    TOOL_INVALID_ARGUMENTS,
)
from harness.permissions import (
    AllowAllPolicy,
    Confirmer,
    DenyingConfirmer,
    PermissionPolicy,
)
from harness.prompts import AGENT_SYSTEM_PROMPT
from harness.tools.base import ERROR_PREFIX, ToolOutcome
from harness.tools.registry import ToolRegistry

# How a run ended, recorded on the context at each terminal return. The
# observability layer needs this as a *value* because a hook is handed the
# context and never the loop's return value, so the alternative is inferring
# the ending from what a hook happened to see — which cannot tell a
# repetition stop from a failure, and files a working guard under "errors"
# (ADR-081, ADR-084). Named constants rather than literals because the
# metrics recorder's `RunStatus` labels are the same four words and the two
# must not drift.
EXIT_SUCCESS = "success"
EXIT_CAPPED = "capped"
EXIT_NO_PROGRESS = "no_progress"
EXIT_ERROR = "error"


class AgentLoop:
    """Reason-act-observe cycle, bounded by guards rather than a fixed cap."""

    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        hook: Hook | None = None,
        max_iterations: int = 0,
        max_tool_failures: int = 5,
        max_repeated_calls: int = 10,
        system_prompt: str = AGENT_SYSTEM_PROMPT,
        max_context_chars: int = 24_000,
        policy: PermissionPolicy | None = None,
        confirmer: Confirmer | None = None,
    ) -> None:
        """Store collaborators. Does not call the LLM.

        Args:
            llm: Chat client (InnKube or a test fake).
            registry: Tools the model may call. An empty registry is
                still a supported configuration — the client then omits the
                ``tools`` field entirely and the loop terminates on the
                model's first reply — but the harness no longer ships a mode
                that uses one (ADR-024).
            hook: Optional lifecycle hook; defaults to :class:`NoOpHook`.
                This is the seam Week 3's observability layer attaches to.
            max_iterations: Optional ceiling on reason-act cycles. ``0``,
                the shipped default, means no fixed cap: a long task is not
                cut off at an arbitrary turn, and the count is still tracked
                and displayed (ADR-047).
            max_tool_failures: Consecutive failures of one tool before the
                loop stops calling it and reports it unavailable for the
                rest of the task. This is the guard that actually bounds the
                common runaway the brief names — a model that calls a broken
                tool, reads the error and calls it again unchanged. ``0``
                disables it (ADR-048).
            max_repeated_calls: Identical calls in a row — same tool, same
                arguments, and the same observation back — before the run
                is stopped as making no progress. The failure limit above
                only catches a tool that *errors*; this catches the other
                half, a model that keeps re-reading the same file or
                re-running the same successful search and never concludes.
                Set above ``max_tool_failures`` so a failing tool is
                retried and retired on that guard's terms rather than
                being cut off mid-recovery by this one. ``0`` disables it
                (ADR-049).
            system_prompt: Instructions seeded once per session.
            max_context_chars: Conversation character budget. The loop
                calls :meth:`RunContext.compact` before each LLM call so
                old tool dumps cannot crowd out the system prompt. ``0``
                disables compaction.
            policy: Permission policy consulted before every tool call.
                Defaults to :class:`AllowAllPolicy`, which is what keeps an
                unattended run behaving as it always has; the CLI builds a
                stricter one from ``permission_mode`` (ADR-057).
            confirmer: How to reach the user when the policy answers
                ``require_user_confirmation``. Defaults to
                :class:`DenyingConfirmer` — no way to ask means the answer
                is no, never a silent yes.
        """
        self._llm = llm
        self._registry = registry
        # Always wrapped, even when it is already one hook.
        #
        # Defaulting keeps the attribute non-optional, so the loop needs no
        # None check at each of the seven call sites below — that part is
        # unchanged. What is new is the *wrapping*, and it is what lets a
        # lifecycle seam grow without breaking its implementors.
        # ``CompositeHook`` knows how to call a hook that predates
        # ``on_llm_error`` and one whose ``after_tool`` takes two arguments
        # rather than three (ADR-094). Routing every hook through it means
        # that knowledge lives in exactly one place instead of as a
        # ``getattr`` at each call site here, which is where it would rot.
        self._hook: Hook = CompositeHook(hook if hook is not None else NoOpHook())
        self._max_iterations = max_iterations
        self._max_tool_failures = max_tool_failures
        self._max_repeated_calls = max_repeated_calls
        self._system_prompt = system_prompt
        self._max_context_chars = max_context_chars
        # Defaulted the same way as the hook, and for the same reason: the
        # attributes stay non-optional, so ``_execute`` never needs a None
        # check on the two things it consults for every single tool call.
        self._policy: PermissionPolicy = (
            policy if policy is not None else AllowAllPolicy()
        )
        self._confirmer: Confirmer = (
            confirmer if confirmer is not None else DenyingConfirmer()
        )

    def run(self, context: RunContext) -> str:
        """Execute the loop until a final answer or the iteration cap.

        The context is passed in rather than created here so a caller can
        keep one across REPL turns (multi-turn conversation) or hand a fresh
        one to a sub-agent (Week 3) without the loop knowing the difference.

        Args:
            context: In-run messages and the current task. The task must
                already have been recorded with ``context.start_turn``.

        Returns:
            Final assistant text for the CLI.

        Raises:
            LLMError: If the model endpoint fails. Deliberately *not* caught:
                a tool error is something the agent can recover from, but a
                dead endpoint is something the user must be told about.
        """
        self._hook.on_run_start(context)
        try:
            # Schemas are read once per run, not per iteration: the tool set
            # is fixed for the duration of a run, and re-serialising it every
            # turn would be pure overhead.
            schemas = self._registry.schemas()

            for iteration in count(1):
                # The cap is optional. When it is set the loop stops at it;
                # when it is 0 the counter still advances, because the CLI
                # shows it and Week 3 metrics record it (ADR-047).
                if self._max_iterations and iteration > self._max_iterations:
                    break
                context.iterations_used = iteration

                # Stub old tool dumps before the request so the window the
                # model sees is the compacted one (ADR-032).
                context.compact(self._max_context_chars)

                # --- Reason -------------------------------------------------
                self._hook.before_llm(context)
                try:
                    response = self._llm.chat(context.messages, schemas)
                except Exception as exc:
                    # Reported, then re-raised unchanged. The loop's contract
                    # is unchanged — an endpoint failure still ends the run
                    # and still reaches the CLI — but until now it ended the
                    # run with *no* record of the model call at all, because
                    # ``after_llm`` fires only on success. A run that died on
                    # a 429 and a run that died on a bad model name were the
                    # same two lines in the trace (ADR-094).
                    #
                    # ``except Exception`` rather than ``except LLMError``:
                    # a client that raises something else is exactly the case
                    # where the evidence is most needed, and the exception
                    # leaves this block either way.
                    self._hook.on_llm_error(context, exc)
                    raise
                # Recorded before the hook so ``after_llm`` and ``on_run_end``
                # both see session totals that include this call (ADR-031).
                context.record_usage(response.prompt_tokens, response.completion_tokens)
                self._hook.after_llm(context, response)

                context.add_assistant_message(
                    response.content, self._as_provider_tool_calls(response)
                )

                # The first assistant text of a session is treated as the
                # plan (see the prompt, which asks for one). Captured for
                # display and for future plan-vs-progress checking.
                if context.plan is None and response.content:
                    context.plan = response.content.strip()

                # --- Terminal check -----------------------------------------
                # No tool calls means the model is done acting. This is the
                # only success exit, and it is why ``tool_choice`` is never
                # forced in the client.
                if not response.tool_calls:
                    return _finish(context, EXIT_SUCCESS, self._final_text(response))

                # --- Act and observe ----------------------------------------
                # Iterating over the whole list handles models that request
                # several calls in one turn. They run sequentially, and this
                # is the exact place where a future DAG scheduler would run
                # independent calls in parallel instead (ADR-020).
                for call in response.tool_calls:
                    observation = self._execute(call, context)
                    context.add_tool_result(call.id, call.name, observation)

                    # --- No-progress check ------------------------------
                    # Same tool, same arguments, same observation, N times
                    # in a row: nothing has been learned since the first of
                    # them, so the run is stopped rather than left to spend
                    # tokens repeating itself. This is the guard that makes
                    # termination hold once the iteration cap is off
                    # (ADR-049).
                    repeats = context.record_call_signature(
                        call.name, call.arguments, observation
                    )
                    if self._max_repeated_calls and repeats >= self._max_repeated_calls:
                        return _finish(
                            context,
                            EXIT_NO_PROGRESS,
                            self._no_progress(call.name, repeats),
                        )

            # --- Cap reached ------------------------------------------------
            # Reported as a normal (if unsatisfying) answer rather than an
            # exception: the workspace may well have been changed already,
            # and the user needs to be told the run was cut off, not handed
            # a stack trace.
            # Only reachable when a cap is configured; with max_iterations
            # at 0 the loop above never breaks out.
            return _finish(
                context,
                EXIT_CAPPED,
                f"Stopped after the maximum of {self._max_iterations} iterations "
                f"without a final answer. Increase max_iterations in config.yaml "
                f"or give me a smaller task.",
            )
        finally:
            # Anything that reaches here without a reason left by one of the
            # three returns above did not terminate normally: an ``LLMError``
            # on its way out, or a ``KeyboardInterrupt``. Recorded before the
            # hook fires, because ``on_run_end`` is where the observability
            # layer reads it and a reason set afterwards would never be seen
            # (ADR-084).
            if context.exit_reason is None:
                context.exit_reason = EXIT_ERROR
            # ``finally`` so the hook fires on the cap, on a clean answer,
            # and on an LLMError alike. Week 3's metrics depend on run-end
            # firing exactly once per run, including failed ones.
            self._hook.on_run_end(context)

    def _execute(self, call: ToolCall, context: RunContext) -> str:
        """Check permission, run one tool call, and return its observation.

        Never raises. ``ToolRegistry.invoke`` already converts unknown
        names and tool exceptions into outcomes, so a failure here becomes
        context the model can act on rather than the end of the run — the
        brief's "errors must be communicated upstream effectively".

        Three things happen in order, and the order is the design:

        1. A tool that has already failed ``max_tool_failures`` times in a
           row is not run at all. A model that cannot fix its arguments will
           otherwise repeat the same call until something else stops it, and
           with no iteration cap there may be nothing else. The observation
           says so plainly, so the model can change approach instead of
           guessing why (ADR-048).
        2. The permission policy decides. This is the single enforcement
           point for §8.4 — every tool the model can reach is dispatched
           here (ADR-057).
        3. Only then does the tool run.

        A refusal — by policy, or by a user who said no — is recorded as a
        failure for that tool. That is not a judgement about the tool; it is
        what keeps the loop bounded. Without it a model could call a denied
        tool forever with slightly different arguments, tripping neither the
        failure guard nor the repetition guard, and ``AGENTS.md`` requires
        every path through this loop to terminate. Instead the tool is
        retired after ``max_tool_failures`` refusals, exactly as a broken
        one would be.

        Args:
            call: The tool call requested by the model.
            context: Run state holding this task's failure streaks.

        Returns:
            Observation text to append to the context.
        """
        limit = self._max_tool_failures
        if context.tool_is_exhausted(call.name, limit):
            # Not executed at all: the point is to stop spending time and
            # tokens on something that has already failed `limit` times.
            observation = self._unavailable(call.name, limit)
            self._hook.before_tool(call.name, call.arguments)
            # A synthesised outcome, marked as the guard rather than as a
            # tool failure. The distinction is the whole reason ``guard.*``
            # error types exist: this call never ran, so counting it as
            # another failure of that tool would inflate the tool's failure
            # rate every time the guard did its job (ADR-094).
            self._hook.after_tool(
                call.name,
                observation,
                ToolOutcome(observation, ok=False, error_type=GUARD_TOOL_RETIRED),
            )
            return observation

        # Fired before the permission check so the trace shows *what* is
        # being asked about before the confirmation prompt appears.
        self._hook.before_tool(call.name, call.arguments)

        # Arguments that did not parse are answered here: after the
        # retirement guard, which must still be able to retire a tool the
        # model keeps mis-calling, and before the permission policy, which
        # has nothing to police — the call never became a call.
        #
        # Running the tool with ``{}`` instead would produce "missing
        # required argument 'text'" for a model that *did* send the text and
        # watched its JSON get truncated. That is a correction for a
        # different mistake, and following it produces the same broken call
        # again; it is how one malformed call became a repeating one.
        if call.decode_error is not None:
            observation = f"{ERROR_PREFIX} {call.name}: {call.decode_error}"
            self._hook.after_tool(
                call.name,
                observation,
                ToolOutcome(observation, ok=False, error_type=TOOL_INVALID_ARGUMENTS),
            )
            # Counted against this tool's failure streak, so a model that
            # cannot emit valid JSON for it reaches the retirement guard
            # above rather than looping until the iteration cap.
            context.record_tool_outcome(call.name, ok=False)
            return observation

        decision, refusal = self._permission_refusal(call)
        # Reported as a value, immediately after the decision and before the
        # tool runs. The alternative — inferring it later from the
        # observation text — cannot tell a policy denial from a tool that
        # failed by itself, and a permission panel that confuses those two
        # is worse than no panel (ADR-058, ADR-081).
        self._hook.after_permission(call.name, decision)
        outcome = (
            refusal
            if refusal is not None
            else self._registry.invoke(call.name, call.arguments)
        )

        # ``outcome.ok`` rather than a prefix test on the text: the failure
        # bit is carried, not reconstructed, so a tool whose successful
        # output happens to start with "Error:" cannot poison the guards
        # (ADR-058).
        streak = context.record_tool_outcome(call.name, ok=outcome.ok)
        observation = outcome.text
        if not outcome.ok and limit and streak >= limit:
            # Appended rather than replacing the error: the model needs the
            # last reason it failed *and* the instruction to stop.
            observation = f"{observation}\n{self._unavailable(call.name, limit)}"
        # The outcome goes with the text. It carries ``ok`` — which a hook
        # used to recover by testing the ``Error:`` prefix — and now also
        # ``error_type``, set where the exception was still in scope. The
        # observation is passed too because the console trace prints it and
        # should not have to reach into a dataclass to do that.
        self._hook.after_tool(call.name, observation, outcome)
        return observation

    def _permission_refusal(self, call: ToolCall) -> tuple[str, ToolOutcome | None]:
        """Decide this call, and return what was decided and any refusal.

        An unknown tool name is passed straight through. The policy has no
        tool to judge, and the registry's own "unknown tool, here are the
        real ones" message is far more useful to the model than a permission
        error would be.

        Args:
            call: The tool call requested by the model.

        Returns:
            ``(decision, refusal)``. The decision is ``allow``, ``deny``,
            ``confirmed`` or ``declined`` — the four *observable* outcomes,
            which are not quite the policy's three: ``confirmed`` and
            ``declined`` split ``require_user_confirmation`` by what the
            person actually said. A gate nobody answers and a gate somebody
            approves are different events, and the panel needs both.
            ``refusal`` is a failed outcome when the call must not run, and
            ``None`` when it may.
        """
        try:
            tool = self._registry.get(call.name)
        except KeyError:
            # Not "allow" in any meaningful sense, but the policy was never
            # consulted, so reporting a decision it did not make would be a
            # lie in the metric. The registry refuses it a moment later.
            return "allow", None

        decision = self._policy.decide(tool, dict(call.arguments))
        if decision == "allow":
            return "allow", None
        if decision == "require_user_confirmation":
            # The loop asks but does not know how the asking happens: the
            # CLI injects a terminal prompt, tests inject a script, and
            # nothing here prints (ADR-057).
            if self._confirmer.confirm(tool, dict(call.arguments)):
                return "confirmed", None
            return "declined", ToolOutcome(
                self._declined(call.name), ok=False, error_type=PERMISSION_DECLINED
            )
        return "deny", ToolOutcome(
            self._denied(call.name), ok=False, error_type=PERMISSION_DENIED
        )

    @staticmethod
    def _denied(name: str) -> str:
        """Return the observation for a call the policy refused outright."""
        return (
            f"{ERROR_PREFIX} {name} is not permitted by the active permission "
            f"policy and was not run. Do not call it again - use a different "
            f"tool, or tell the user what you could not do and why."
        )

    @staticmethod
    def _declined(name: str) -> str:
        """Return the observation for a call the user was asked about and refused.

        Worded differently from :meth:`_denied` on purpose. A policy denial
        is permanent and the model should route around it; a declined
        confirmation is a person's decision about *this* call, and the useful
        next move is usually to ask them what they want instead.
        """
        return (
            f"{ERROR_PREFIX} the user was asked to approve this call to {name} "
            f"and declined, so it was not run. Do not retry it. Ask the user "
            f"what they would prefer, or continue without it."
        )

    @staticmethod
    def _no_progress(name: str, repeats: int) -> str:
        """Return the answer shown when the run is stopping for repetition.

        Plain text rather than an exception, for the same reason the
        iteration cap is: the workspace may already have been changed, and
        the user needs to be told what happened rather than handed a stack
        trace. Returning from inside ``try`` also leaves ``on_run_end``
        firing exactly once, as ADR-017 requires.
        """
        return (
            f"Stopped: the same call to {name} was made {repeats} times in a "
            f"row with identical arguments and an identical result, so the "
            f"run was making no progress. Nothing further was attempted. "
            f"Try a more specific task, or check that tool's output above."
        )

    @staticmethod
    def _unavailable(name: str, limit: int) -> str:
        """Return the observation shown once a tool is out of service."""
        return (
            f"{ERROR_PREFIX} {name} failed {limit} times in a row and is "
            f"treated as not working for the rest of this task. Do not call "
            f"it again - use a different tool, or tell the user what you "
            f"could not do."
        )

    @staticmethod
    def _as_provider_tool_calls(response: LLMResponse) -> list[dict[str, object]]:
        """Rebuild provider-shaped tool calls for the message history.

        The normalised :class:`ToolCall` is convenient for the loop but is
        not what the API expects back. Re-serialising here (rather than
        stashing the raw SDK object) keeps :mod:`harness.llm.protocol` as the
        only provider-shaped surface in the codebase.

        Note the arguments are re-encoded as a JSON string, because that is
        the wire format even though the loop works with a dict.

        Args:
            response: The normalised model response.

        Returns:
            Tool call dicts, or an empty list when the model only spoke.
        """
        return [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments),
                },
            }
            for call in response.tool_calls
        ]

    @staticmethod
    def _final_text(response: LLMResponse) -> str:
        """Extract the user-facing answer from a terminal response.

        Some models end a run with an empty content field. Returning an
        empty string would look to the user like the harness had crashed
        silently, so a placeholder is substituted.

        Args:
            response: A response with no tool calls.

        Returns:
            Text to show the user.
        """
        text = (response.content or "").strip()
        return text or "(the model finished without producing any text)"


def _finish(context: RunContext, reason: str, answer: str) -> str:
    """Record how and with what the run ended, then return the answer.

    Both values are written onto the context *before* the ``finally`` that
    fires ``on_run_end``, because a hook receives the context and never the
    loop's return value. ``exit_reason`` exists for that reason (ADR-084);
    ``final_answer`` is the same idea for the text: it lets the console hook
    show the answer at run end, ahead of any slower run-end work such as
    memory extraction, instead of after it (ADR-110).

    Args:
        context: The run's state.
        reason: One of the ``EXIT_*`` constants.
        answer: What the user is shown.

    Returns:
        ``answer``, unchanged.
    """
    context.exit_reason = reason
    context.final_answer = answer
    return answer
