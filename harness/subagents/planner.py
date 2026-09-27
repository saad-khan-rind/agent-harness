"""Decide at run time whether a request should fan out to sub-agents.

The ``delegate`` tool, its roles and a system prompt that explains when to
use it were not enough. Measured against the lab model on five tasks — three
that split naturally into independent parts, two that do not — the agent
delegated on its own **zero** times out of six on the three that should, with
the old prompt and again with a rewritten one that spelled out when to split
and gave worked examples. It read every file itself, one after another. It
delegated only when a user named the sub-agent, which is not a decision.

Choosing one tool out of twenty-five, mid-task, while also doing the work, is
the hard version of the question. The easy version is asked on its own, with
nothing else to choose between: *does this request split into independent
parts, and if so, what are they?* That is the lead agent's planning step in
the plan-then-execute (orchestrator-worker) pattern, and a mid-sized model
answers it reliably.

So this wrapper, at the model-call seam where the budget and the claim check
already sit, asks that question once per turn — at the moment the agent is
about to start reading (a file, a search, a web lookup), when it knows what
the request is and has seen any listing it made first. If the answer is a
split, the reply the loop receives is a ``delegate`` call carrying the
planned tasks instead of the single read. That call then goes through
``AgentLoop._execute`` like any other: the permission gate decides it, the
hooks see it, the trace records it and the children run in parallel under
the factory's limits. Nothing is dispatched from here (ADR-057, ADR-117).

The model still makes the decision, and it still does everything after it:
it reads the children's results, combines them, and writes whatever the task
ends in. What changed is only that the decision is put to it as a question
it can answer.
"""

import json
import threading
import uuid
from collections.abc import Callable
from typing import Any

from harness.llm.protocol import LLMClient, LLMResponse, ToolCall
from harness.memory.auxiliary import ask_json
from harness.subagents.roles import DELEGATE_TOOL_NAME

# Tools that mean "the agent has started reading material". Matched on the
# part after an MCP server prefix, so ``web__web_search`` counts. Listing and
# finding files are deliberately absent: they are how the agent learns what
# the parts *are*, so planning waits until they have run.
READING_TOOLS = frozenset(
    {"read_file", "search_text", "web_search", "web_fetch", "memory_search"}
)

# The only role the planner fans out to. Parallel work in this harness is
# reading and looking things up; writing is done once, by the coordinator,
# from what comes back, and review needs the finished thing. Keeping the
# planner to read-only children also means it can never be the reason a
# file changed.
PLANNED_ROLE = "researcher"

# Longest the rendered turn-so-far may be. The planner needs the request and
# the shape of what has been found — file names from a listing — not whole
# files, and a short question keeps the extra call cheap.
_TRANSCRIPT_MAX = 3000
_RESULT_CLIP = 600

_PLANNER_SYSTEM = """You plan work for an agent that can run sub-agents in
parallel. Decide whether the user's request splits into INDEPENDENT parts
that are each worth giving to a separate sub-agent, and if so, write them.

The agent is about to start reading or searching, so assume the material
has to be looked up - even facts that seem well known.

Split when there are two or more parts and each part needs its own reading,
searching or looking up: several files to read, several subjects to look up,
several sections that come from different material. A comparison of several
named things splits into one part per thing. Parts are independent when none
needs another's answer.

Do not split when there is only one part, when the parts are tiny (one short
file, one number), or when a later part needs an earlier part's result.

Each task goes to a sub-agent that can read files and search the web but
cannot see this conversation. So every task must name exactly what to read
or look up (file paths exactly as written, subject names), say what to
report back, and make sense on its own. At most {max_tasks} tasks. Do not
include writing the final answer or any file: the coordinator does that
after the results come back.

Answer with one JSON object and nothing else:
{{"split": true, "reason": "<one sentence>",
  "tasks": [{{"task": "...", "context": "...", "success_criteria": "..."}}]}}
or
{{"split": false, "reason": "<one sentence>"}}"""


def _reading_tool(name: str) -> bool:
    """Whether a tool name is one of :data:`READING_TOOLS`, prefix or not."""
    return name.rsplit("__", 1)[-1] in READING_TOOLS


def _turn_start(messages: list[dict[str, Any]]) -> int:
    """Index of the last user message, or ``-1`` when there is none."""
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            return index
    return -1


def _delegated_this_turn(messages: list[dict[str, Any]], start: int) -> bool:
    """Whether any assistant message since ``start`` called ``delegate``."""
    for message in messages[start + 1 :]:
        for call in message.get("tool_calls") or []:
            if (call.get("function") or {}).get("name") == DELEGATE_TOOL_NAME:
                return True
    return False


def render_turn(messages: list[dict[str, Any]], start: int) -> str:
    """Render the user's request and what this turn has found so far.

    Args:
        messages: The conversation as the loop is about to send it.
        start: Index of the turn's user message.

    Returns:
        Plain text for the planner: the request, then one block per tool
        call and the head of its result.
    """
    request = str(messages[start].get("content") or "") if start >= 0 else ""
    lines = [f"USER REQUEST:\n{request}"]
    for message in messages[start + 1 :]:
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            lines.append(f"CALLED {function.get('name')}({function.get('arguments')})")
        if message.get("role") == "tool":
            text = str(message.get("content") or "")
            lines.append(f"RESULT:\n{text[:_RESULT_CLIP]}")
    return "\n\n".join(lines)[:_TRANSCRIPT_MAX]


def planned_tasks(plan: dict[str, Any] | None, max_tasks: int) -> list[dict[str, str]]:
    """Turn the planner's answer into ``delegate`` task entries, or none.

    Anything short of a clear, well-formed split is "no split": a planner
    that is unsure must leave the agent to work as it was going to.

    Args:
        plan: The decoded JSON answer, or ``None``.
        max_tasks: The most tasks one ``delegate`` call accepts.

    Returns:
        Between two and ``max_tasks`` task dicts, or an empty list.
    """
    if not plan or plan.get("split") is not True:
        return []
    raw = plan.get("tasks")
    if not isinstance(raw, list):
        return []
    tasks: list[dict[str, str]] = []
    for entry in raw[:max_tasks]:
        if not isinstance(entry, dict):
            continue
        task = str(entry.get("task") or "").strip()
        if not task:
            continue
        tasks.append(
            {
                "role": PLANNED_ROLE,
                "task": task,
                "context": str(entry.get("context") or "").strip(),
                "success_criteria": str(entry.get("success_criteria") or "").strip(),
            }
        )
    return tasks if len(tasks) >= 2 else []


class DelegationPlanner:
    """An :class:`~harness.llm.protocol.LLMClient` that plans a fan-out once per turn.

    Wraps the agent's client. Satisfies the protocol structurally, and only
    ever acts on the coordinator: a child's tool list has no ``delegate``, so
    for a child every call passes straight through.
    """

    def __init__(
        self,
        inner: LLMClient,
        planner: LLMClient,
        max_tasks: int,
        enabled: Callable[[], bool] = lambda: True,
    ) -> None:
        """Args:
        inner: The client the agent's own requests go to.
        planner: The client the planning question goes to — the budgeted,
            traced one, so the call is metered and shows up as its own
            ``llm.call`` span with ``purpose=delegation.plan``.
        max_tasks: The most tasks one ``delegate`` call accepts
            (``subagent_max_concurrency``). Below two, nothing can run in
            parallel, and the planner never asks.
        enabled: Consulted per turn. The session answers ``False`` in a
            mode where ``delegate`` would be refused anyway, so no call is
            spent planning work the gate will not allow.
        """
        self._inner = inner
        self._planner = planner
        self._max_tasks = max_tasks
        self._enabled = enabled
        # The turns already planned for, keyed by the user message's object
        # identity and text — so the question is asked at most once per turn
        # whatever it answered. Only the coordinator's calls reach here with
        # ``delegate`` offered, but a lock costs nothing and the children
        # share this instance on worker threads.
        self._asked: set[tuple[int, str]] = set()
        self._lock = threading.Lock()
        # How many turns were fanned out, for the session and the tests.
        self.splits = 0

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        """Forward the request; at the first read of a turn, ask whether to split.

        Args:
            messages: OpenAI-style chat messages.
            tools: Function-calling schemas.

        Returns:
            The agent's own reply, or — when the planner split the work — a
            ``delegate`` call carrying the planned tasks, with both calls'
            tokens on it.
        """
        response = self._inner.chat(messages, tools)
        if not self._should_ask(messages, tools, response):
            return response

        start = _turn_start(messages)
        plan = ask_json(
            self._planner,
            _PLANNER_SYSTEM.format(max_tasks=self._max_tasks),
            render_turn(messages, start),
        )
        tasks = planned_tasks(plan, self._max_tasks)
        if not tasks:
            return response

        self.splits += 1
        reason = str((plan or {}).get("reason") or "").strip()
        return LLMResponse(
            content=(
                f"This splits into {len(tasks)} independent parts, so I am "
                f"handing them to sub-agents to work on in parallel."
                + (f" {reason}" if reason else "")
            ),
            tool_calls=[
                ToolCall(
                    id=f"plan-{uuid.uuid4().hex[:12]}",
                    name=DELEGATE_TOOL_NAME,
                    arguments={"tasks": tasks},
                )
            ],
            # The planner's own call is counted on its span by the traced
            # client; the agent's discarded reply is counted here, so the
            # loop's usage still covers every token spent this step.
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            finish_reason=response.finish_reason,
        )

    def _should_ask(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        response: LLMResponse,
    ) -> bool:
        """Whether this is the moment to put the question, and mark it asked."""
        if self._max_tasks < 2 or not response.tool_calls:
            return False
        offered = {(tool.get("function") or {}).get("name") for tool in tools}
        if DELEGATE_TOOL_NAME not in offered:
            return False
        if not _reading_tool(response.tool_calls[0].name):
            return False
        start = _turn_start(messages)
        if start < 0 or _delegated_this_turn(messages, start):
            return False
        key = (id(messages[start]), json.dumps(messages[start].get("content")))
        with self._lock:
            if key in self._asked:
                return False
            self._asked.add(key)
        return self._enabled()
