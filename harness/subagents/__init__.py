"""Specialized sub-agents: delegation as a first-class runtime operation.

The brief (§8.4) asks for sub-agents "running into the same harness", each a
separate agent instance with its own role, context, limits and permissions,
whose executions are recorded as part of the parent run. This package is
that, and the emphasis is on *the same harness*: a child is an ordinary
:class:`~harness.loop.AgentLoop`, and ``loop.py`` contains no sub-agent
branch (ADR-084).

Reading order, if you are meeting this for the first time:

* :mod:`harness.subagents.roles` — what a specialist is: capabilities,
  limits, instructions, output contract, and the three shipped ones.
* :mod:`harness.subagents.contracts` — the typed envelope in and the typed
  result out, with the lifecycle between them.
* :mod:`harness.subagents.factory` — the runtime: build, run, validate,
  record.
* :mod:`harness.subagents.policy` — where capabilities and budgets are
  actually enforced, on the harness's one permission gate.
* :mod:`harness.subagents.tools` — the single ``delegate`` tool the model
  sees, and why delegation is exposed as a tool call.
* ``docs/subagents.md`` — the same story in prose, with worked examples of
  every message that crosses the boundary.
"""

from harness.subagents.confirm import AttributedConfirmer
from harness.subagents.contracts import (
    BatchResult,
    DelegationEnvelope,
    DelegationResult,
    DelegationStatus,
    DelegationTask,
    ExecutionBudget,
    ResearchResult,
    ReviewResult,
    RunMetadata,
    WriteResult,
)
from harness.subagents.deadline import ChildDeadlineExceeded, DeadlineLLMClient
from harness.subagents.factory import ConfigurableFactory, narrow_mode
from harness.subagents.policy import ChildPolicy
from harness.subagents.protocol import Delegator, SubAgentFactory
from harness.subagents.roles import (
    AgentCatalog,
    AgentSpec,
    Capability,
    build_catalog,
    builtin_specs,
    capability_of,
)
from harness.subagents.tools import DelegateTool

__all__ = [
    "AgentCatalog",
    "AgentSpec",
    "AttributedConfirmer",
    "BatchResult",
    "Capability",
    "ChildDeadlineExceeded",
    "ChildPolicy",
    "ConfigurableFactory",
    "DeadlineLLMClient",
    "DelegateTool",
    "DelegationEnvelope",
    "DelegationResult",
    "DelegationStatus",
    "DelegationTask",
    "Delegator",
    "ExecutionBudget",
    "ResearchResult",
    "ReviewResult",
    "RunMetadata",
    "SubAgentFactory",
    "WriteResult",
    "build_catalog",
    "builtin_specs",
    "capability_of",
    "narrow_mode",
]
