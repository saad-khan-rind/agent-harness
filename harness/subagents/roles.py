"""What a specialist *is*: capabilities, limits, instructions, output contract.

A sub-agent in this harness is not a branch in the runtime. It is a row in a
catalog — an :class:`AgentSpec` — and the runtime knows only how to run a
specification, never which specifications exist. That is the difference
between "we added two sub-agents" and "we added sub-agents", and it is why
the three shipped specialists are defined at the bottom of this module as
ordinary registrations rather than inside :mod:`harness.subagents.factory`
(ADR-084).

**Capabilities, not tool lists.** A specification grants
:class:`Capability` values, and the tool names a child actually gets are
*derived* by intersecting those capabilities with the tools this session
happens to have. Three things fall out of that and each of them matters:

* A session with no MCP servers still builds a working researcher, with one
  fewer capability rather than a crash or a tool that fails when called.
* A tool added next term — another file tool, another memory verb — is
  classified by :func:`capability_of` and lands in the right children with
  no edit here.
* The grant is auditable as a sentence a person can check ("the researcher
  may read files, search memory and search the web"), which a list of eleven
  tool names is not.

**Classification is total.** Every tool that can reach a registry maps to
exactly one capability, for the same reason
:class:`~harness.permissions.RiskBasedPolicy` keys off the risk level every
tool must declare: a specification that has no opinion about some tool is a
hole, and a hole in a capability system is not a gap in a document, it is a
tool a restricted agent can call.
"""

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum

from pydantic import BaseModel

from harness.permissions import PermissionMode
from harness.subagents.contracts import (
    ExecutionBudget,
    ResearchResult,
    ReviewResult,
    WriteResult,
)
from harness.tools.base import Tool

# The separator the MCP adapter puts between a server name and a remote tool
# name (ADR-042). Duplicated from the metrics hook's constant rather than
# imported from it, because importing observability into the sub-agent
# package would point the dependency at a layer that is meant to observe
# this one, not to be depended on by it.
_MCP_SEPARATOR = "__"

# Server names whose tools count as web research. A tuple rather than a
# single string because a second search server is a configuration change, not
# a code change, and because `web__` is a name chosen in `config.yaml` that
# this module should not pretend is a law.
DEFAULT_WEB_SERVERS: tuple[str, ...] = ("web",)

# The model-facing name of the delegation tool. Lives here, next to the
# capability that governs it, so the recursion guard and the tool cannot
# drift apart: a child never holds DELEGATE, so a child registry never
# contains this name (ADR-084).
DELEGATE_TOOL_NAME = "delegate"

# The one memory tool that only reads. Everything else under `memory_` writes,
# and a child that can write to long-term memory can change what *later*
# sessions believe, which is a far larger blast radius than its own run.
_MEMORY_READ_TOOLS = frozenset({"memory_search"})


class Capability(StrEnum):
    """A class of action an agent may be granted.

    Coarser than a tool name and finer than a risk level. The risk level
    answers "could this change something"; a capability answers "which part
    of the world" — and that second question is what separates a researcher
    that may read the web from a writer that may change the workspace, even
    though both are made of tools the parent already had.
    """

    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    MEMORY_READ = "memory_read"
    MEMORY_WRITE = "memory_write"
    SKILLS = "skills"
    NOTES = "notes"
    WEB_SEARCH = "web_search"
    REMOTE_TOOLS = "remote_tools"
    DELEGATE = "delegate"


def capability_of(
    tool: Tool, web_servers: Iterable[str] = DEFAULT_WEB_SERVERS
) -> Capability:
    """Return the one capability a tool belongs to.

    Order is the design. The delegation tool is checked first because it is
    the recursion guard and must not be reachable through any other rule;
    memory, skills and notes are checked by prefix before the remote separator,
    because those are local tools whose names the harness controls; remote
    tools are split by server; and everything left is a file tool, sorted by
    the risk level it already declares.

    Args:
        tool: A registered tool.
        web_servers: Server names whose remote tools count as web research.

    Returns:
        The capability that governs this tool.
    """
    name = tool.name
    if name == DELEGATE_TOOL_NAME:
        return Capability.DELEGATE
    if name in _MEMORY_READ_TOOLS:
        return Capability.MEMORY_READ
    if name.startswith("memory_"):
        return Capability.MEMORY_WRITE
    if name.startswith("skill_"):
        return Capability.SKILLS
    # Its own capability, and no shipped role grants it. The scratchpad is
    # the *parent's* working state for the task it was given; a child that
    # could write to it would be editing the notes its caller is relying on
    # to know what is left to do. A child that needs notes of its own would
    # need its own scratchpad, which is a separate decision (ADR-090).
    if name.startswith("note_"):
        return Capability.NOTES
    if _MCP_SEPARATOR in name:
        server = name.split(_MCP_SEPARATOR, 1)[0]
        if server in tuple(web_servers):
            return Capability.WEB_SEARCH
        return Capability.REMOTE_TOOLS
    return Capability.FILE_READ if tool.risk == "observe" else Capability.FILE_WRITE


@dataclass(frozen=True)
class AgentSpec:
    """One registered specialist: who it is, what it may do, what it returns.

    Frozen because a specification is configuration, and configuration that
    a running agent can edit is not a limit. :meth:`with_budget` returns a
    new specification rather than mutating this one, which is how
    ``config.yaml`` tightens the shipped defaults without anything at
    runtime being able to loosen them.

    Attributes:
        agent_id: Lookup key, and the value the model passes as ``role``.
        role: Observability label for the child's runs. Equal to
            ``agent_id`` for the shipped specialists.
        description: One line telling the *parent* when this specialist is
            worth delegating to. This text goes into the parent's tool
            schema, so it is written for a dispatcher choosing between
            specialists, not for a person reading documentation.
        instructions: The child's system prompt body. Its whole prompt is
            this plus the runtime preamble and the output contract, and it
            contains none of the parent's prompt — a specialist that
            inherits "you are a helpful assistant with eleven tools" is not
            a specialist.
        capabilities: What the child may do. The tool list is derived from
            this, and the permission policy enforces it a second time.
        permission_mode: The strictest mode this specialist runs under. The
            factory narrows it further against the parent's, never wider.
        budget: The allowance this specialist asks for, before the parent's
            remainder clamps it.
        result_model: The contract the child's answer is validated against.
        output_example: A filled-in example of that contract. Generated
            field lists tell a model the shape; an example tells it the
            register — how long an ``answer`` should be, what a ``source``
            looks like — and medium-sized models follow the example far
            more reliably than the schema.
    """

    agent_id: str
    role: str
    description: str
    instructions: str
    capabilities: frozenset[Capability]
    permission_mode: PermissionMode
    budget: ExecutionBudget
    result_model: type[BaseModel]
    output_example: str = ""

    def with_budget(self, budget: ExecutionBudget) -> "AgentSpec":
        """Return a copy of this specification under a different allowance.

        Args:
            budget: The replacement allowance.

        Returns:
            A new specification; this one is unchanged.
        """
        return replace(self, budget=budget)

    def grants(self, capability: Capability) -> bool:
        """Whether this specialist holds ``capability``.

        Args:
            capability: The capability to test.
        """
        return capability in self.capabilities


@dataclass
class AgentCatalog:
    """The registered specialists, by id.

    A catalog rather than a module-level dict so a test, a future
    configuration file, or a second session can hold a different set without
    reaching into global state — and so the CLI can build one catalog from
    ``config.yaml`` and hand it to the factory, which is the only consumer.

    Attributes:
        specs: Registered specifications, in registration order. Order is
            what the model sees in the ``delegate`` schema and in the
            catalog block of its system prompt, so it is kept stable.
    """

    specs: dict[str, AgentSpec] = field(default_factory=dict)

    def register(self, spec: AgentSpec) -> None:
        """Add a specification, refusing a duplicate id.

        A hard error rather than a silent overwrite, for the reason
        :class:`~harness.tools.registry.ToolRegistry` refuses duplicate tool
        names: two specifications sharing an id means one of them is
        unreachable, and the unreachable one is usually the stricter.

        Args:
            spec: The specification to register.

        Raises:
            ValueError: If ``spec.agent_id`` is already registered.
        """
        if spec.agent_id in self.specs:
            raise ValueError(f"Duplicate sub-agent id: {spec.agent_id}")
        self.specs[spec.agent_id] = spec

    def get(self, agent_id: str) -> AgentSpec | None:
        """Return a specification, or ``None`` when the id is unknown.

        ``None`` rather than a raise: an unknown role is something the
        *model* gets wrong, and the runtime answers a model's mistake with a
        failed outcome naming the real ids, exactly as the tool registry
        answers a hallucinated tool name (ADR-084).

        Args:
            agent_id: The id the parent asked for.
        """
        return self.specs.get(agent_id)

    def names(self) -> list[str]:
        """Return the registered ids, in registration order."""
        return list(self.specs)

    def describe(self) -> str:
        """Render the catalog as the block the parent's prompt shows.

        Args:
            None.

        Returns:
            One line per specialist: its id and what it is for.
        """
        return "\n".join(
            f"- {spec.agent_id}: {spec.description}" for spec in self.specs.values()
        )

    def __iter__(self) -> Iterator[AgentSpec]:
        """Iterate the registered specifications in registration order."""
        return iter(self.specs.values())

    def __len__(self) -> int:
        """Return how many specialists are registered."""
        return len(self.specs)


# -- the shipped specialists -------------------------------------------------
#
# Three, and the third is not padding. Two would satisfy the brief, but the
# three below are chosen so that each one demonstrates a *different* reason
# delegation exists, which is the claim the design has to survive being
# asked about:
#
#   researcher — context isolation. Search results and fetched pages are the
#                bulkiest, least reusable tokens in any run. They belong in a
#                window that is thrown away.
#   writer     — permission narrowing. The only specialist that may change
#                the workspace, and the only one a `confirm_mutations`
#                session gates.
#   critic     — verification. The cheapest delegation there is: it needs
#                almost no context to transfer, and an independent reader is
#                the one thing a single agent cannot be for its own output.

_RESEARCHER_INSTRUCTIONS = """You are a research specialist working for another agent.

You investigate one question and report what you found. You do not write
files, you do not change anything, and you do not carry out the wider task
the question came from - another agent is doing that.

How to work:
1. Decide in one sentence what would answer the question.
2. Gather evidence with the tools you have. Prefer a source you can point
   at over something you already believe.
3. Separate what a source supports from what you are inferring. An inference
   is useful and must be labelled as one.
4. Say what you could not establish. "I could not find X" is a finding.

Keep the answer short. Everything you read stays in this context and is
discarded; only what you write in the result reaches the agent that asked,
so a long answer wastes the very thing you were called to protect."""

_WRITER_INSTRUCTIONS = """You are a writing specialist working for another agent.

You produce or update files in the workspace from material you are given.
You do not research: if a fact is missing from your task, say so in the
result rather than inventing it or going to look for it.

How to work:
1. If the file exists, read it before changing it, so old_str matches byte
   for byte.
2. Write the content you were given. Follow the constraints in the task.
3. Read the file back afterwards and confirm what is in it.
4. Report the paths you touched.

Write only the files the task names. A file you were not asked about is not
yours to change, whatever you think it needs."""

_CRITIC_INSTRUCTIONS = """You are a review specialist working for another agent.

You examine work that is already done and report what is wrong with it. You
change nothing - you have no tools that can. Your value is that you are a
reader who was not in the conversation that produced this, so state what the
text or the file actually says, not what it was probably meant to say.

How to work:
1. Read what you are asked to review. Read all of it before judging any of it.
2. Check it against the criteria in the task. If no criteria were given,
   check it for internal contradiction, unsupported claims, and missing
   pieces the task implies.
3. List issues most serious first. Point at where each one is.
4. Give a verdict: approve, revise, or reject.

Say what you actually looked at. A review that approves after reading one
paragraph is worse than no review, because it will be believed."""


def builtin_specs(
    budget: ExecutionBudget | None = None,
) -> tuple[AgentSpec, ...]:
    """Return the three shipped specialists.

    A function rather than a module constant so the caller supplies the
    allowance. ``config.yaml`` owns the limits — the brief requires
    configuration to be persistent and file-based — and a module constant
    would have frozen them at import time, before settings were read.

    Args:
        budget: The default allowance for every shipped specialist, or
            ``None`` for the conservative built-in one. Per-role differences
            are expressed below as deliberate narrowings of this.

    Returns:
        The researcher, the writer and the critic, in the order the model
        sees them.
    """
    base = budget if budget is not None else ExecutionBudget()
    return (
        AgentSpec(
            agent_id="researcher",
            role="researcher",
            description=(
                "Investigates one question, file or subject and returns a "
                "short evidence-backed finding. Reads files, long-term memory "
                "and the web; cannot change anything. Use one researcher per "
                "independent question - several run at once - and whenever "
                "answering needs material you do not want in your own context."
            ),
            instructions=_RESEARCHER_INSTRUCTIONS,
            capabilities=frozenset(
                {
                    Capability.FILE_READ,
                    Capability.MEMORY_READ,
                    Capability.WEB_SEARCH,
                }
            ),
            # Not `confirm_mutations`: there is nothing to confirm, because
            # this specialist holds no capability that can mutate. Stating
            # the mode as `deny_mutations` anyway is belt and braces — the
            # tools are absent *and* the policy would refuse them (ADR-086).
            permission_mode="deny_mutations",
            budget=base,
            result_model=ResearchResult,
            output_example=(
                '{"answer": "Qdrant stores vectors in named collections; the '
                'harness uses one collection per store.", "evidence": '
                '[{"claim": "The store creates the collection on first write", '
                '"source": "harness/memory/qdrant_store.py", "kind": "fact"}], '
                '"uncertainties": ["Whether the collection survives a volume '
                'reset was not checked"], "confidence": "medium"}'
            ),
        ),
        AgentSpec(
            agent_id="writer",
            role="writer",
            description=(
                "Writes or updates files in the workspace from material you "
                "supply. Cannot research or reach the network. Use it when a "
                "task ends in a file and you already have the content."
            ),
            instructions=_WRITER_INSTRUCTIONS,
            capabilities=frozenset({Capability.FILE_READ, Capability.FILE_WRITE}),
            # The one specialist that can change the workspace, and the one
            # whose mode is therefore inherited rather than narrowed. A
            # specification states the *widest* mode it could need; the
            # session's own mode is what actually decides, because the person
            # who chose `confirm_mutations` for their session did not choose
            # it only for the work the top-level agent happens to do itself.
            # So a `confirm_mutations` session still gets its prompt — the
            # child shares the parent's Confirmer, so the same person is
            # asked about the child's writes on the same terminal — and an
            # `allow_all` session is not surprised by a sub-agent that asks
            # anyway. Narrowing still works in the direction that matters:
            # the other two specialists sit *below* any session mode.
            permission_mode="allow_all",
            budget=base,
            result_model=WriteResult,
            output_example=(
                '{"summary": "Wrote the three-section brief.", '
                '"files_written": ["notes/brief.md"], "verified": true, '
                '"notes": "The file already existed and was replaced."}'
            ),
        ),
        AgentSpec(
            agent_id="critic",
            role="critic",
            description=(
                "Reviews finished work and returns a verdict with specific "
                "findings. Reads files; changes nothing and has no memory or "
                "network. Use it before telling the user something is done."
            ),
            instructions=_CRITIC_INSTRUCTIONS,
            capabilities=frozenset({Capability.FILE_READ}),
            permission_mode="deny_mutations",
            # A reviewer needs less room than a researcher: it is reading a
            # bounded artifact, not searching. Half the steps, and the cap is
            # what stops a model that has nothing to say from re-reading the
            # same file until a guard fires.
            budget=base.narrowed_to(
                ExecutionBudget(
                    max_iterations=max(1, base.max_iterations // 2),
                    max_tool_calls=max(1, base.max_tool_calls // 2),
                    timeout_seconds=base.timeout_seconds,
                    max_output_chars=base.max_output_chars,
                )
            ),
            result_model=ReviewResult,
            output_example=(
                '{"verdict": "revise", "findings": [{"issue": "The summary '
                'claims three sources but lists two", "severity": "high", '
                '"where": "notes/brief.md, first paragraph", "fix": "Cite the '
                'third source or say two"}], "strengths": ["Structure matches '
                'the request"], "checked": ["notes/brief.md"]}'
            ),
        ),
    )


def build_catalog(
    budget: ExecutionBudget | None = None,
    specs: Iterable[AgentSpec] | None = None,
) -> AgentCatalog:
    """Assemble the catalog the factory runs from.

    Args:
        budget: Default allowance for the shipped specialists.
        specs: Replacement specifications, for a test or a future
            configuration file. ``None`` means the shipped three.

    Returns:
        A catalog with each specification registered once.
    """
    catalog = AgentCatalog()
    for spec in specs if specs is not None else builtin_specs(budget):
        catalog.register(spec)
    return catalog


def capability_report(spec: AgentSpec) -> str:
    """Render a specification's grant as one readable line.

    Used in the child's own prompt and in the stored artifact. A child that
    is told what it may do stops trying what it may not, which turns a
    permission denial from a wasted turn into a turn that never happens —
    and a reader of the artifact can check the grant without reading code.

    Args:
        spec: The specification.

    Returns:
        A comma-separated list of capability names, or a plain statement
        that the specialist holds none.
    """
    if not spec.capabilities:
        return "none"
    return ", ".join(sorted(capability.value for capability in spec.capabilities))


def tools_for(
    spec: AgentSpec,
    tools: Mapping[str, Tool],
    web_servers: Iterable[str] = DEFAULT_WEB_SERVERS,
) -> list[str]:
    """Return the names a specification may hold, out of the tools that exist.

    The intersection, and only the intersection. A capability naming a tool
    this session never registered contributes nothing — an MCP server that
    did not start costs the researcher its web capability and nothing else,
    which is the same degradation rule the rest of the harness follows for a
    missing backend (ADR-069).

    Args:
        spec: The specification being built.
        tools: Every tool the parent holds, by name.
        web_servers: Server names whose remote tools count as web research.

    Returns:
        Tool names, in the parent registry's order so the child's schema
        order is stable across runs.
    """
    servers = tuple(web_servers)
    return [
        name
        for name, tool in tools.items()
        if spec.grants(capability_of(tool, servers))
    ]
