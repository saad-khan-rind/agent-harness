"""Load process settings from YAML and the environment.

Environment variables always override ``config.yaml``. A missing or empty
LLM API key, unreadable YAML, or an unusable workspace path exits the
process before the REPL or any LLM call starts.
"""

import re
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AliasChoices,
    BaseModel,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)
from yaml import YAMLError

from harness.permissions import (
    ConfiguredPermissionMode,
    PermissionRules,
    PermissionTier,
)

DEFAULT_CONFIG_PATH = Path("config.yaml")
DEFAULT_ENV_FILE = Path(".env")

# Every provider the harness knows how to reach. All of them serve the
# OpenAI chat-completions wire format, which is why one client covers them
# and choosing a provider is a configuration value rather than a class
# (ADR-121). ``custom`` is any other OpenAI-compatible server: it has no
# defaults, so it must name its own base URL and model.
LlmProvider = Literal[
    "innkube",
    "gemini",
    "openai",
    "groq",
    "openrouter",
    "mistral",
    "ollama",
    "custom",
]


class ProviderDefaults(BaseModel):
    """Endpoint and model a provider gets when the configuration names neither."""

    base_url: str
    model: str


# Filled in only where ``llm_base_url`` / ``llm_model`` are left empty, so a
# deployment can switch provider with one variable and still override either
# half. The models are sensible tool-calling defaults, not recommendations
# frozen in time: set ``LLM_MODEL`` to pin the one you tested.
PROVIDER_DEFAULTS: dict[str, ProviderDefaults] = {
    "innkube": ProviderDefaults(
        base_url="https://llms.innkube.fim.uni-passau.de",
        model="qwen-agentworld-35b-a3b",
    ),
    "gemini": ProviderDefaults(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        model="gemini-2.5-flash",
    ),
    "openai": ProviderDefaults(
        base_url="https://api.openai.com/v1",
        model="gpt-4.1-mini",
    ),
    "groq": ProviderDefaults(
        base_url="https://api.groq.com/openai/v1",
        model="llama-3.3-70b-versatile",
    ),
    "openrouter": ProviderDefaults(
        base_url="https://openrouter.ai/api/v1",
        model="openai/gpt-4.1-mini",
    ),
    "mistral": ProviderDefaults(
        base_url="https://api.mistral.ai/v1",
        model="mistral-large-latest",
    ),
    # The harness normally runs in a container, where localhost is the
    # container itself; host.docker.internal is the machine Ollama runs on.
    "ollama": ProviderDefaults(
        base_url="http://host.docker.internal:11434/v1",
        model="qwen2.5:14b",
    ),
}


class McpServerSettings(BaseModel):
    """One MCP server the harness may launch and discover tools from.

    A nested model rather than a loose dict so a typo in ``config.yaml``
    fails at startup with a field name attached, exactly like every other
    setting. Servers are described in configuration and never in code: the
    point of the integration is that adding a server is a config change
    (ADR-039).
    """

    # Label, not an identifier the server knows about. It becomes the prefix
    # on every tool this server advertises, so it reaches the model and the
    # CLI trace, and it is what keeps two servers offering the same tool name
    # distinguishable (ADR-042).
    name: str

    # How to reach the server, and what to reach.
    #
    # ``stdio`` launches ``command`` as a subprocess and speaks over its
    # pipes — the transport ADR-039 shipped, and the one the two servers in
    # this repository use. ``http`` connects to an already-running server at
    # ``url`` over Streamable HTTP, which is how a server someone else
    # operates joins the tool list without being launched, packaged or
    # trusted with our process (ADR-061).
    #
    # Exactly one of ``command`` / ``url`` applies, enforced below, because a
    # server entry carrying both is a configuration whose meaning nobody can
    # read off the page.
    transport: Literal["stdio", "http"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None

    # Extra environment for the child process. Secrets belong in the
    # process environment and are referenced from there, never written here:
    # config.yaml is committed.
    env: dict[str, str] = Field(default_factory=dict)

    # Seconds allowed for one tool call on this server. Per-server because a
    # weather lookup and a local computation do not deserve the same patience.
    timeout: float = 30.0

    # Off without deleting the entry, which is what a developer wants while
    # debugging one server out of several.
    enabled: bool = True

    # Names refused for this server on top of the global word list. Exact
    # names, for a specific tool this deployment does not want offered.
    denied_tools: list[str] = Field(default_factory=list)

    # Default permission tier for every tool this server offers, consulted
    # when no ``permission_rules`` pattern matched the tool's name. Unset
    # means the tool's own annotations decide (read-only and closed-world
    # runs freely; anything else asks). ``ask`` is the right first line for
    # a server nobody has reviewed yet; ``allow`` is for one whose tools
    # have all been walked through the four questions in the README
    # (ADR-102).
    permission: PermissionTier | None = None

    @field_validator("name")
    @classmethod
    def _usable_server_name(cls, value: str) -> str:
        """Reject a label that cannot become part of a tool name.

        The name is prefixed onto every discovered tool, and the model's
        function-calling format wants snake_case. Enforcing it here means the
        failure names the offending config key instead of surfacing later as
        a tool that silently did not register.
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("mcp server name must not be empty")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", stripped):
            raise ValueError(f"mcp server name must be snake_case (got {value!r})")
        return stripped

    @field_validator("command")
    @classmethod
    def _non_empty_command(cls, value: str | None) -> str | None:
        """Reject an empty command, which would fail only at launch time."""
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("mcp server command must not be empty")
        return stripped

    @field_validator("url")
    @classmethod
    def _usable_url(cls, value: str | None) -> str | None:
        """Reject a URL that is not an absolute http(s) address.

        Checked at startup rather than at connect time so a typo names the
        config key instead of surfacing as an unreachable server twenty
        seconds into a launch.
        """
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("mcp server url must not be empty")
        if not stripped.startswith(("http://", "https://")):
            raise ValueError(
                f"mcp server url must start with http:// or https:// (got {value!r})"
            )
        return stripped

    @model_validator(mode="after")
    def _transport_matches_target(self) -> "McpServerSettings":
        """Require exactly the fields the chosen transport uses.

        Silently ignoring a ``url`` on a stdio entry is how somebody spends
        an afternoon wondering why their edit had no effect, so the unused
        field is an error rather than a no-op.
        """
        if self.transport == "stdio":
            if not self.command:
                raise ValueError("a stdio mcp server needs a command")
            if self.url is not None:
                raise ValueError(
                    "a stdio mcp server must not set url; use transport: http"
                )
        else:
            if not self.url:
                raise ValueError("an http mcp server needs a url")
            if self.command is not None or self.args:
                raise ValueError(
                    "an http mcp server must not set command or args: it is "
                    "connected to, not launched"
                )
            if self.env:
                raise ValueError(
                    "an http mcp server must not set env: there is no child "
                    "process to give it to"
                )
        return self

    @field_validator("timeout")
    @classmethod
    def _positive_timeout(cls, value: float) -> float:
        """Reject a non-positive timeout, which would fail every call."""
        if value <= 0:
            raise ValueError("mcp server timeout must be greater than zero")
        return value


class PermissionRulesSettings(BaseModel):
    """The ``permission_rules`` block: name patterns per tier.

    Three lists rather than one list of ``{pattern, tier}`` objects, because
    the precedence between them — deny, then ask, then allow — is the whole
    point, and three named lists make it visible on the page. A pattern is
    a shell-style glob over the full registered tool name: ``web__*``,
    ``calendar__add_*``, ``memory_*``. See
    :class:`harness.permissions.PermissionRules` for the matching rules.
    """

    deny: list[str] = Field(default_factory=list)
    ask: list[str] = Field(default_factory=list)
    allow: list[str] = Field(default_factory=list)

    @field_validator("deny", "ask", "allow")
    @classmethod
    def _non_empty_patterns(cls, value: list[str]) -> list[str]:
        """Reject a blank pattern, which would match nothing and hide a typo."""
        cleaned = [pattern.strip() for pattern in value]
        if any(not pattern for pattern in cleaned):
            raise ValueError("permission_rules patterns must not be empty")
        return cleaned

    def to_rules(self) -> PermissionRules:
        """Return the frozen value the policy consumes."""
        return PermissionRules(
            deny=tuple(self.deny), ask=tuple(self.ask), allow=tuple(self.allow)
        )


class Settings(BaseSettings):
    """Non-secret defaults plus secrets supplied by the environment."""

    model_config = SettingsConfigDict(
        extra="ignore",
        env_file_encoding="utf-8",
    )

    # Which OpenAI-compatible provider to talk to. Decides the default
    # endpoint and model (``PROVIDER_DEFAULTS``); everything else about the
    # client is the same for every provider (ADR-121).
    llm_provider: LlmProvider = "innkube"

    # Secret. Has no default on purpose: pydantic then fails validation when
    # it is absent, which is the "fail fast if LLM_API_KEY is missing" rule
    # from AGENTS.md. It is never committed, so it can only ever arrive from
    # the environment or .env — never config.yaml. The ``INNKUBE_*`` names
    # are accepted as aliases so an existing .env keeps working.
    llm_api_key: str = Field(
        min_length=1,
        validation_alias=AliasChoices("llm_api_key", "innkube_api_key"),
    )

    # Endpoint and model. Empty means "the provider's default", filled in by
    # ``_apply_provider_defaults`` — so switching provider does not also
    # require knowing its URL.
    llm_base_url: str = Field(
        default="",
        validation_alias=AliasChoices("llm_base_url", "innkube_base_url"),
    )
    llm_model: str = Field(
        default="",
        validation_alias=AliasChoices("llm_model", "innkube_model"),
    )
    llm_timeout: float = Field(
        default=600.0,
        validation_alias=AliasChoices("llm_timeout", "innkube_timeout"),
    )

    # Observability (Week 3, §8.4). On by default: a harness nobody can see
    # inside is the thing this week exists to fix, and a demo where somebody
    # has to remember a flag is a demo that shows nothing.
    #
    # A closed port is NOT fatal. Same argument as a missing Qdrant
    # (ADR-069): monitoring is how you watch the agent work, not part of the
    # work, so the banner says the exporter is off and the run continues
    # (ADR-081).
    metrics_enabled: bool = True
    metrics_port: int = 9091

    # The other half of observability: one JSONL file per agent run, so a
    # single run can be inspected step by step. Metrics answer "how is it
    # doing"; the trace answers "what did it do". Written under
    # ``memory_path`` — a sibling of the workspace, which is what keeps it
    # out of the file tools' reach and on the volume that outlives ``--rm``
    # (ADR-035, ADR-083). ``traces_dirname`` is a bare name, never a path:
    # the location is decided by ``memory_path``, and a value that could
    # point elsewhere would turn a config setting into a way out of the root.
    traces_enabled: bool = True
    traces_dirname: str = "traces"

    # What telemetry may record about a tool call's arguments.
    #
    #   strict   nothing derived from arguments at all.
    #   shapes   key names, value types and sizes, plus a fingerprint. The
    #            default. No value ever leaves the process.
    #   values   truncated values too, with secret-looking keys still
    #            removed. A debugging setting, and the banner says when it
    #            is on — a mode that quietly starts logging file contents is
    #            a mode somebody will forget is enabled (ADR-094).
    #
    # The fingerprint is available in every mode, including `strict`: it is
    # a hash, so it discloses nothing, and it is what makes a model
    # repeating the same call visible without recording what the call was.
    telemetry_redaction: Literal["strict", "shapes", "values"] = "shapes"

    # Optional OpenTelemetry export, so the same spans can be opened in
    # Jaeger or Tempo. Off by default and off in three independent ways:
    # this flag, whether the packages are installed, and whether the
    # collector answers. None of the three can fail a run (ADR-095).
    #
    # Prometheus is *not* replaced by this. The brief requires
    # Prometheus-compatible metrics and we keep them; this is about reading
    # one run, which is a different question from watching all of them.
    otlp_enabled: bool = False
    otlp_endpoint: str = "http://localhost:4318"
    # Where a person opens the trace viewer, from their browser. Not the
    # endpoint above: that is where the *harness* sends spans, which inside
    # compose is `jaeger:4318` and means nothing on the host. The banner and
    # the per-answer trace link are built from this (ADR-111).
    otlp_ui_url: str = "http://localhost:16686"
    otlp_service_name: str = "agent-harness"

    # Daily token budget (ADR-088). One number, enforced as a hard stop at
    # the model call: once the day's prompt-plus-completion total reaches it,
    # no further calls are made until midnight in ``timezone``. Counted
    # across every session sharing the memory volume, sub-agents included,
    # because they all go through the same client. On by default, because a
    # budget that has to be remembered is a budget that is not there when
    # a runaway happens. The total is kept in ``budget.json`` under
    # ``memory_path`` — the same argument as the traces: off the workspace
    # and on the volume that outlives ``--rm``.
    #
    # The default is sized from the traces: about 6k prompt tokens per call
    # on this cluster, 15-25k per ordinary turn, ~112k for the heaviest demo
    # scenario. A million covers a full demo day with headroom and still
    # stops a stuck session from spending a week.
    budget_enabled: bool = True
    budget_daily_tokens: int = 1_000_000
    # Share of the allowance past which the after-answer line turns into a
    # warning. Display only; the limit is what is enforced.
    budget_warn_fraction: float = 0.8
    # The most one instruction may spend before the harness stops and asks.
    # Not a ceiling — the user types `continue` and the same task carries on
    # with a fresh allowance — so it is sized to be generous for ordinary
    # work and to interrupt a runaway well before the day is gone: roughly
    # ten heavy turns, or a fifth of the window (ADR-089). `0` switches the
    # checkpoint off and restores the single-allowance behaviour.
    budget_prompt_tokens: int = 400_000

    # Short-term memory: one markdown file the agent keeps its own task
    # state in, injected whole at the start of every run (ADR-090). On by
    # default — it is the thing that makes a long task survivable once
    # compaction starts eating the observations it was working from — and
    # sized as a share of the context budget, since it is sent every time.
    notes_enabled: bool = True
    notes_max_chars: int = 4_000

    # Specialized sub-agents (§8.4). On by default: unlike skill scoping
    # below, delegation adds a capability rather than removing one, and a
    # parent that never calls `delegate` pays exactly one extra tool schema
    # for having it available (ADR-084).
    subagents_enabled: bool = True

    # A child's own reason-act ceiling. Unlike `max_iterations` above, which
    # defaults to 0 (no cap) because a user's task may legitimately be long,
    # a *delegated* task is one bounded piece of somebody else's plan, and an
    # unbounded child is a runaway nobody is watching. A real number is
    # therefore the default here and the absence of one is not offered: a
    # child's budget is also clamped to what the parent has left (ADR-086).
    subagent_max_iterations: int = 8

    # A child's tool-call allowance for its whole run, enforced on the
    # permission gate. Separate from the iteration cap because one iteration
    # can request several calls, and a researcher fetching forty pages in
    # eight steps is exactly the runaway this bounds.
    subagent_max_tool_calls: int = 20

    # Wall-clock ceiling on a child's *actions*, in seconds. It bounds what
    # a child may start; it cannot interrupt a model call already in flight,
    # which is the LLM client's own `llm_timeout`. Stated plainly
    # because a limit that claims more than it can do is worse than none.
    # ``0`` disables it.
    subagent_timeout_seconds: float = 120.0

    # How deep delegation may go. ``1`` means the top-level agent may
    # delegate and a child may not. The primary recursion guard is
    # structural — a child's registry has no `delegate` tool — and this is
    # the second lock on the same door, the one that produces a readable
    # refusal rather than an unknown-tool error (ADR-084).
    subagent_max_depth: int = 1

    # How many times a child whose result did not match its declared output
    # contract is asked to restate it. One is worth it: the common failures
    # are packaging, not substance, and throwing away a correct
    # investigation over its formatting costs more than one model call.
    # ``0`` disables the repair (ADR-085).
    subagent_repair_attempts: int = 1

    # Which MCP servers count as *web research* when a specialist's
    # capabilities are resolved into tool names. Named rather than inferred
    # because "it has a server prefix" is not the same claim as "it searches
    # the web": the calendar server is remote too, and a researcher granted
    # WEB_SEARCH must not acquire the ability to write calendar entries as a
    # side effect of how a name happens to be spelled (ADR-086).
    subagent_web_servers: list[str] = Field(default_factory=lambda: ["web"])

    # Ceiling on the summary a child hands back. The whole economic argument
    # for delegation is that the detail stays in the child, so this is the
    # number that makes the argument true rather than aspirational.
    subagent_max_result_chars: int = 2000

    # How many children one `delegate` call may run at once, and therefore
    # the longest batch the tool accepts. ``1`` reproduces the sequential
    # behaviour exactly and is the rollback if concurrency misbehaves.
    #
    # Three rather than the ten a commercial harness uses: every child here
    # shares one InnKube cluster that already rate-limits us into retries,
    # and a fan-out wide enough to trip that spends wall-clock rather than
    # saving it. It also matches what the published orchestrator-worker
    # systems settle on for a typical query (ADR-106).
    subagent_max_concurrency: int = 3

    # Whether the coordinator is asked, once per turn at its first read,
    # "does this split into independent parts?" — and fans out when it does.
    # Without it the lab model never chose to delegate on its own (ADR-117).
    subagent_auto_plan: bool = True

    # Confine the model to a skill's declared `allowed-tools` while that
    # skill is in context. Off by default and deliberately so: it is a real
    # restriction on a running agent, and one wrong entry in a skill's
    # frontmatter turns into a tool the model cannot call for reasons the
    # transcript does not explain. Opt in per demo (ADR-082).
    skills_enforce_allowed_tools: bool = False

    # The zone the agent thinks in. It is what "today" and "next Monday"
    # mean in the system prompt, so it has to match the `--local-timezone`
    # passed to the time and calendar servers in `mcp_servers` below — three
    # places, one answer. `tests/unit/test_config.py` asserts the shipped
    # config.yaml keeps them in step, which is what makes the duplication
    # safe rather than a trap (ADR-079).
    #
    # UTC as the default because that is the container's own clock: a
    # harness started with no configuration is then consistent with itself
    # rather than quietly an hour out.
    timezone: str = "UTC"

    # Sampling. Low by default: this agent picks tool calls, where
    # determinism is worth more than variety.
    temperature: float = 0.2

    # Optional loop guard. ``0`` means no fixed cap: a task runs until the
    # model answers, and the iteration count is still displayed and recorded
    # (ADR-047). Set any positive number to restore a hard ceiling.
    max_iterations: int = 0

    # Consecutive failures of one tool before the loop stops calling it and
    # reports it as unavailable for the rest of the task. A success resets
    # the count. ``0`` disables the guard (ADR-048).
    max_tool_failures: int = 5

    # Identical calls in a row — same tool, same arguments, same observation —
    # before the run is stopped as making no progress. Deliberately well
    # above ``max_tool_failures`` so the failure limit above gets to do its
    # job first: a broken tool is retried, then retired with an explanation
    # the model can act on, and only a model that ignores that explanation
    # reaches this backstop. ``0`` disables the guard; ``1`` is meaningless
    # and rejected (ADR-049).
    max_repeated_calls: int = 10

    # Send back, once, a final answer that claims an action when no tool has
    # run since the user's message. The model then acts or corrects itself;
    # costs at most one extra call per turn, and only on such a reply
    # (ADR-116).
    claim_check_enabled: bool = True

    # Retry policy for transient InnKube failures (ADR-025). The cluster is
    # shared by the whole class, so 429s are expected rather than
    # exceptional. Attempts total max_retries + 1.
    max_retries: int = 3

    # First backoff ceiling in seconds; doubles per attempt.
    retry_base_delay: float = 1.0

    # Upper bound on any single backoff wait, so a Retry-After header of
    # "3600" cannot silently park the agent for an hour.
    retry_max_delay: float = 30.0

    # Which permission policy the loop enforces on every tool call (§8.4,
    # ADR-057, ADR-102). ``auto`` — the shipped default — is
    # ``confirm_mutations`` when stdin is a terminal and ``allow_all``
    # otherwise, resolved once by the CLI. The split exists because the two
    # ways the harness is run want opposite defaults: a person at a REPL
    # should be asked before the agent writes or reaches the internet, while
    # a one-shot run, an E2E test or a `docker run` without a TTY has nobody
    # to ask and a prompt there resolves to "no". Name a mode explicitly to
    # pin it regardless of where the harness runs.
    permission_mode: ConfiguredPermissionMode = "auto"

    # Name patterns that set a tool's tier ahead of its metadata: ``deny``
    # beats ``ask`` beats ``allow``, and the deny-list beats all three. Empty
    # by default, so a tool's tier comes from its risk and reach (ADR-102).
    permission_rules: PermissionRulesSettings = Field(
        default_factory=PermissionRulesSettings
    )

    # Exact tool names refused whatever the mode. Empty by default: the
    # word-based deny-list in ``harness.tools.base`` already covers the
    # ADR-008 guarantee, and this is for naming one specific tool.
    denied_tools: list[str] = Field(default_factory=list)

    # Sandbox root for every file tool.
    workspace_path: Path = Path("workspace")

    # Context budget. Caps one tool observation so a large file or a broad
    # search cannot crowd out the conversation (Section 2, context rot).
    max_tool_output_chars: int = 4000

    # Hit limit for search_text, for the same reason.
    max_search_results: int = 50

    # Conversation-wide budget. Caps the *sum* of messages, not one
    # observation. ``0`` disables compaction. InnKube often omits usage, so
    # this is characters, not tokens (ADR-032).
    max_context_chars: int = 64_000

    # How many earlier REPL turns stay verbatim when an over-budget
    # conversation is folded into a summary at the start of a turn. The
    # current turn always stays. Folding itself is not optional: it is what
    # keeps a long chat inside ``max_context_chars`` once there is no tool
    # output left to stub (ADR-108).
    conversation_keep_turns: int = 2

    # Ceiling on that summary, which is sent with every request.
    conversation_summary_max_chars: int = 2_000

    # Whether the model writes the summary. Off, a deterministic one-line-
    # per-turn summary is used instead. Off by default in code, on in the
    # shipped config.yaml, for the same reason as the two memory switches
    # below: a feature that spends extra model calls must be asked for, so
    # a scripted test client or a tight budget never sees calls it did not
    # expect (ADR-108).
    conversation_summary_llm_enabled: bool = False

    # Second sandbox root: long-term memory (ADR-035). Deliberately a
    # *sibling* of the workspace rather than a folder inside it. The file
    # tools are confined to workspace_path, so keeping memory outside is
    # what stops the agent rewriting its own memory with write_file, and
    # stops a hostile file read in one session from editing what the next
    # session loads. ``load_settings`` rejects a nested pair.
    memory_path: Path = Path("memory")

    # Kill switch. Tests and anyone debugging a prompt want a run with no
    # memory loaded, injected, or written at all.
    memory_enabled: bool = True

    # The harness-driven write trigger: one extra model call at the end of
    # each top-level run, reading only the user's message, that extracts
    # durable facts and stores them with ``harness`` provenance (ADR-107).
    memory_extraction_enabled: bool = False

    # Whether a new fact is checked against its nearest stored neighbours by
    # a model judge, which can file it as a duplicate or as a correction
    # that supersedes an older fact. Off, exact repeats are still caught and
    # everything else is added (ADR-107).
    memory_consolidation_enabled: bool = False

    # Budget for the whole injected memory block, including the project
    # memory file and the note index. Memory competes with the conversation
    # for the same window, so it gets an explicit ceiling rather than
    # whatever the files happen to contain.
    memory_max_inject_chars: int = 4000

    # How many core facts the always-injected first tier may carry. A
    # ceiling, not a target: a query matching three facts injects three
    # (ADR-067).
    memory_core_fact_limit: int = 10

    # Where the vector database listens. A URL rather than a host and port
    # because the client takes one, and because it makes the difference
    # between a compose service and a remote cluster a config edit
    # (ADR-065).
    memory_qdrant_url: str = "http://localhost:6333"

    # Seconds allowed for one Qdrant request. Short: it is a local
    # container on the compose network, and a memory operation that hangs
    # for a minute is worse for a run than one that fails and degrades.
    memory_qdrant_timeout: float = 10.0

    # Embedding model requested from the OpenAI-compatible endpoint. If the
    # deployment does not serve embeddings the harness falls back to a
    # local lexical embedder and says so in the banner (ADR-066).
    memory_embedding_model: str = "text-embedding-3-small"

    # Set false to skip the embedding probe entirely and always use the
    # local embedder. Useful for a demo with no network and for keeping a
    # collection at a stable width.
    memory_remote_embeddings: bool = True

    # Sentence-transformers model used when the remote endpoint serves no
    # embeddings. It is tried before the lexical fallback, because the
    # fallback measures word overlap rather than meaning and that is not a
    # property anyone wants their memory to have: "what do I like" and
    # "Likes: swimming" share no trigrams and score 0.0 against each other.
    #
    # Local embedding model, used when the endpoint serves no embeddings.
    # Loaded through fastembed when that is installed (onnxruntime, ~24 MB)
    # and sentence-transformers otherwise (torch, ~1.4 GB) — same checkpoint
    # and same vectors either way, so this name does not change with the
    # backend.
    #
    # all-MiniLM-L6-v2 rather than a BGE checkpoint, deliberately. Both are
    # 384-dimensional and both run on CPU, but MiniLM is symmetric — it
    # embeds a question and a fact the same way — while the BGE family
    # expects a "Represent this sentence for searching relevant passages:"
    # prefix on queries only, and scores almost any two English sentences
    # above 0.6 without it. A model that needs a prefix the seam has no way
    # to pass, and whose similarities cluster too high to threshold, is the
    # wrong default for a subsystem whose failure mode is already an
    # untrustworthy floor. ~90 MB, downloaded once.
    #
    # Set to "" to skip the local rung and go straight to the lexical one.
    memory_local_embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"

    # Where the local model is cached. The Dockerfile populates this path at
    # build time and sets this variable, so a container runs with no network
    # and no first-run download — which is the difference between a demo
    # that starts and one that waits on huggingface.co in front of an
    # audience. Empty uses the library's own default, which is right outside
    # a container.
    memory_local_embedding_cache: str = ""

    # Override the active embedder's own similarity floor. ``None`` uses the
    # embedder's value, which is the right default: the floor belongs to the
    # vector space, not to a deployment. Set it after measuring with
    # ``scripts/tune_memory_floor.py`` on the machine that will run this,
    # against that machine's own stored facts. A number copied from
    # somewhere else is how the floor became wrong in the first place.
    memory_min_similarity: float | None = None

    # User-authored project memory, looked up *inside* the workspace. The
    # cross-vendor name rather than a single vendor's, and the same one this
    # repository already uses for team rules.
    project_memory_filename: str = "AGENTS.md"

    # Third sandbox root: skills. Read-only and shipped in the image, so it
    # is resolved but never created and never written to (ADR-038).
    skills_path: Path = Path("skills")

    skills_enabled: bool = True

    # Budget for the injected skill index (names and descriptions only).
    skills_max_inject_chars: int = 2000

    # Kill switch for the whole MCP integration. On by default with an
    # empty server list, so a bare `docker run` behaves exactly as it did
    # before and enabling a server is a config edit, not a rebuild.
    mcp_enabled: bool = True

    # Servers to launch at startup. Empty by default (ADR-039).
    mcp_servers: list[McpServerSettings] = Field(default_factory=list)

    # Seconds allowed for a server to launch and finish its handshake.
    # Separate from the per-call timeout: a slow start is a one-off cost,
    # while a slow call happens on every turn.
    mcp_connect_timeout: float = 30.0

    @field_validator("llm_api_key")
    @classmethod
    def _reject_blank_api_key(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("LLM_API_KEY must not be empty")
        return stripped

    @model_validator(mode="after")
    def _apply_provider_defaults(self) -> "Settings":
        """Fill an empty base URL or model from the provider's defaults.

        ``custom`` has no defaults, so it must name both itself: guessing an
        endpoint for an unknown server would send the API key somewhere the
        user never chose.
        """
        self.llm_base_url = self.llm_base_url.strip()
        self.llm_model = self.llm_model.strip()
        defaults = PROVIDER_DEFAULTS.get(self.llm_provider)
        if defaults is None:
            if not self.llm_base_url or not self.llm_model:
                raise ValueError(
                    "llm_provider 'custom' needs both LLM_BASE_URL and LLM_MODEL"
                )
            return self
        if not self.llm_base_url:
            self.llm_base_url = defaults.base_url
        if not self.llm_model:
            self.llm_model = defaults.model
        return self

    @field_validator("metrics_port")
    @classmethod
    def _usable_metrics_port(cls, value: int) -> int:
        """Reject a port the exporter could never bind.

        Below 1024 needs root, and the container runs as an unprivileged
        user by design (ADR-030) — so a privileged port is not a preference
        that might work, it is a guaranteed failure at startup.
        """
        if not 1024 <= value <= 65535:
            raise ValueError(
                f"metrics_port must be between 1024 and 65535 (got {value}); "
                f"the container runs as a non-root user and cannot bind a "
                f"privileged port"
            )
        return value

    @field_validator("timezone")
    @classmethod
    def _usable_timezone(cls, value: str) -> str:
        """Reject a zone the system prompt could not render.

        Fails at startup rather than at the first dated task: a bad zone
        would otherwise surface as a traceback in the middle of a demo, and
        the name is only ever wrong because somebody typed it.
        """
        name = value.strip()
        if not name:
            raise ValueError("timezone must not be empty")
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"timezone must be an IANA name such as 'Europe/Berlin' (got {value!r})"
            ) from exc
        return name

    @field_validator("max_iterations")
    @classmethod
    def _iteration_cap(cls, value: int) -> int:
        """Accept a positive cap or ``0`` for none; reject anything negative.

        ``0`` is the shipped default and means the loop is not cut off at a
        fixed number of turns (ADR-047). A negative value is always a
        mistake — it would stop the run before its first LLM call — so it
        fails at startup rather than producing an agent that answers nothing.
        """
        if value < 0:
            raise ValueError("max_iterations must be 0 (no cap) or a positive number")
        return value

    @field_validator("subagent_max_iterations", "subagent_max_tool_calls")
    @classmethod
    def _subagent_step_limit(cls, value: int) -> int:
        """Require a real ceiling on a child's steps and calls.

        ``0`` is rejected here although it is accepted for the parent's
        ``max_iterations``, and the asymmetry is the point. A person running
        the REPL decides when their own task has gone on too long and can
        press Ctrl-C; a delegated child has no such person, so an unbounded
        one is a runaway with nobody watching (ADR-086).
        """
        if value < 1:
            raise ValueError(
                "subagent step and call limits must be at least 1; a child "
                "always runs under a real ceiling"
            )
        return value

    @field_validator("subagent_max_depth")
    @classmethod
    def _subagent_depth(cls, value: int) -> int:
        """Accept a depth of at least one.

        Zero would register a ``delegate`` tool that refuses every call,
        which is a worse way of switching sub-agents off than
        ``subagents_enabled: false`` — the model would see the tool, try it,
        and be refused, once per role it could think of.
        """
        if value < 1:
            raise ValueError(
                "subagent_max_depth must be at least 1; set subagents_enabled "
                "to false to switch delegation off"
            )
        return value

    @field_validator("subagent_timeout_seconds")
    @classmethod
    def _subagent_timeout(cls, value: float) -> float:
        """Accept a positive timeout or ``0`` to disable the clock."""
        if value < 0:
            raise ValueError(
                "subagent_timeout_seconds must be 0 (disabled) or positive"
            )
        return value

    @field_validator("subagent_repair_attempts")
    @classmethod
    def _subagent_repairs(cls, value: int) -> int:
        """Accept ``0`` to disable the repair, or a small positive number.

        Capped at three because each attempt is a model call spent on
        formatting rather than on work, and a child that cannot produce the
        shape in three tries is not going to on the fourth.
        """
        if not 0 <= value <= 3:
            raise ValueError("subagent_repair_attempts must be between 0 and 3")
        return value

    @field_validator("subagent_max_result_chars")
    @classmethod
    def _subagent_result_chars(cls, value: int) -> int:
        """Require a positive ceiling on what a child hands back."""
        if value < 1:
            raise ValueError("subagent_max_result_chars must be a positive number")
        return value

    @field_validator("subagent_max_concurrency")
    @classmethod
    def _subagent_concurrency(cls, value: int) -> int:
        """Require at least one worker, and refuse an implausible fan-out.

        ``0`` would mean a `delegate` tool that can never run anything, which
        is a confusing way to spell ``subagents_enabled: false``. The upper
        bound is a judgement rather than a hard limit of the design: past a
        handful of concurrent children the shared InnKube cluster answers
        with rate limits, so a wider fan-out buys retries instead of speed,
        and one person cannot answer that many confirmation prompts anyway.
        """
        if value < 1:
            raise ValueError(
                "subagent_max_concurrency must be at least 1; set "
                "subagents_enabled to false to switch delegation off"
            )
        if value > 8:
            raise ValueError(
                "subagent_max_concurrency above 8 is not supported: the "
                "shared model endpoint rate-limits a fan-out that wide, so "
                "it costs wall-clock rather than saving it"
            )
        return value

    @field_validator("max_tool_failures")
    @classmethod
    def _tool_failure_limit(cls, value: int) -> int:
        """Accept a positive limit or ``0`` to disable the guard."""
        if value < 0:
            raise ValueError(
                "max_tool_failures must be 0 (disabled) or a positive number"
            )
        return value

    @field_validator("max_repeated_calls")
    @classmethod
    def _repeat_limit(cls, value: int) -> int:
        """Accept ``0`` (disabled) or two or more.

        A limit of ``1`` would stop the run on the first tool call of any
        kind, since one call is trivially a run of one. Rejecting it at
        startup beats shipping an agent that answers nothing.
        """
        if value < 0 or value == 1:
            raise ValueError("max_repeated_calls must be 0 (disabled) or at least 2")
        return value

    @field_validator("temperature")
    @classmethod
    def _sane_temperature(cls, value: float) -> float:
        """Reject values outside the range OpenAI-compatible servers accept.

        Caught here rather than at request time so a typo in ``config.yaml``
        fails at startup instead of after the user has typed a task and
        waited for the endpoint to reject it.
        """
        if not 0.0 <= value <= 2.0:
            raise ValueError("temperature must be between 0.0 and 2.0")
        return value

    @field_validator("max_retries")
    @classmethod
    def _non_negative_retries(cls, value: int) -> int:
        """Reject a negative retry budget.

        Zero is valid and means "try once, never retry" — the setting tests
        and anyone debugging a failing endpoint want that.
        """
        if value < 0:
            raise ValueError("max_retries must be zero or greater")
        return value

    @field_validator("retry_base_delay", "retry_max_delay")
    @classmethod
    def _positive_delay(cls, value: float) -> float:
        """Reject a non-positive backoff delay.

        Zero would busy-loop against a rate-limited server, which is the
        precise behaviour the retry policy exists to avoid.
        """
        if value <= 0:
            raise ValueError("retry delays must be greater than zero")
        return value

    @field_validator(
        "max_tool_output_chars",
        "max_search_results",
        "memory_max_inject_chars",
        "memory_core_fact_limit",
        "skills_max_inject_chars",
    )
    @classmethod
    def _positive_budget(cls, value: int) -> int:
        """Reject non-positive context budgets.

        Zero would truncate every observation to nothing, which produces an
        agent that fails in a way that looks like a model problem rather
        than a config problem.
        """
        if value < 1:
            raise ValueError("context budgets must be at least 1")
        return value

    @field_validator("memory_qdrant_url")
    @classmethod
    def _usable_qdrant_url(cls, value: str) -> str:
        """Reject a URL the client cannot dial.

        The client accepts a bare host and then fails at the first request
        with a message about the *collection*, which sends whoever is
        debugging it to the wrong file. Requiring a scheme here turns that
        into a startup error naming the setting.
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("memory_qdrant_url must not be empty")
        if not stripped.startswith(("http://", "https://")):
            raise ValueError(
                f"memory_qdrant_url must start with http:// or https:// (got {value!r})"
            )
        return stripped

    @field_validator("memory_qdrant_timeout")
    @classmethod
    def _positive_qdrant_timeout(cls, value: float) -> float:
        """Reject a non-positive Qdrant timeout.

        Zero would abandon every request before it was sent, producing a
        harness whose memory is silently always empty.
        """
        if value <= 0:
            raise ValueError("memory_qdrant_timeout must be greater than zero")
        return value

    @field_validator("project_memory_filename")
    @classmethod
    def _plain_filename(cls, value: str) -> str:
        """Reject anything but a bare filename.

        This value is joined onto the workspace root to find the project
        memory file. A value like ``../../etc/passwd`` would turn a config
        setting into a path traversal, so separators are refused here rather
        than relied upon being caught downstream. The loader also routes the
        lookup through ``resolve_in_workspace``, but a bad config should
        fail at startup with a clear message instead of silently reading
        nothing.
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("project_memory_filename must not be empty")
        if "/" in stripped or "\\" in stripped or stripped in {".", ".."}:
            raise ValueError(
                f"project_memory_filename must be a bare filename, "
                f"not a path (got {value!r})"
            )
        return stripped

    @field_validator("otlp_endpoint", "otlp_ui_url")
    @classmethod
    def _http_endpoint(cls, value: str, info: ValidationInfo) -> str:
        """Require an ``http://`` or ``https://`` base URL.

        Checked at startup rather than at the first export, because the
        export happens on a background thread where a bad URL becomes a
        dropped span and a silence. A typo in a config file should be a
        message before the banner, not a trace backend that stays
        mysteriously empty (ADR-095).
        """
        stripped = value.strip().rstrip("/")
        if not stripped.startswith(("http://", "https://")):
            raise ValueError(
                f"{info.field_name} must start with http:// or https:// (got {value!r})"
            )
        return stripped

    @field_validator("traces_dirname")
    @classmethod
    def _plain_dirname(cls, value: str) -> str:
        """Reject anything but a bare directory name.

        Same argument as ``project_memory_filename``: the value is joined
        onto a root, so a separator or ``..`` in it would relocate the trace
        directory — possibly into the workspace, where the file tools could
        read and overwrite the very record of what they did (ADR-083).
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("traces_dirname must not be empty")
        if "/" in stripped or "\\" in stripped or stripped in {".", ".."}:
            raise ValueError(
                f"traces_dirname must be a bare directory name, "
                f"not a path (got {value!r})"
            )
        return stripped

    @property
    def traces_path(self) -> Path:
        """Where run traces are written: ``memory_path / traces_dirname``.

        A property rather than a second configured path so the two cannot
        disagree — there is one memory root, and the traces live inside it.
        """
        return self.memory_path / self.traces_dirname

    @field_validator("mcp_connect_timeout")
    @classmethod
    def _positive_connect_timeout(cls, value: float) -> float:
        """Reject a non-positive connect timeout.

        Zero would abandon every server before it had finished starting,
        producing a harness that silently never has MCP tools.
        """
        if value <= 0:
            raise ValueError("mcp_connect_timeout must be greater than zero")
        return value

    def server_tiers(self) -> dict[str, PermissionTier]:
        """Return each server's configured default tier, keyed by label.

        Only servers that set ``permission`` appear, so the policy can tell
        "this server said nothing" from "this server said allow".
        """
        return {
            server.name: server.permission
            for server in self.mcp_servers
            if server.permission is not None
        }

    @field_validator("mcp_servers")
    @classmethod
    def _unique_server_names(
        cls, value: list[McpServerSettings]
    ) -> list[McpServerSettings]:
        """Reject two servers sharing a name.

        The name is the tool prefix, so duplicates would collide in the
        registry and abort startup with a message about tools rather than
        about the configuration that caused it.
        """
        names = [server.name for server in value]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ValueError(
                f"duplicate mcp server names: {', '.join(sorted(duplicates))}"
            )
        return value

    @field_validator("budget_daily_tokens")
    @classmethod
    def _positive_daily_budget(cls, value: int) -> int:
        """Reject a daily budget that could never allow a call.

        ``0`` is not "unlimited" here, unlike the loop guards: switching the
        budget off is ``budget_enabled: false``, and a limit of zero would
        be a harness that refuses every model call, which nobody means.
        """
        if value < 1:
            raise ValueError(
                "budget_daily_tokens must be at least 1; use budget_enabled: "
                "false to switch the budget off"
            )
        return value

    @field_validator("budget_prompt_tokens")
    @classmethod
    def _sane_prompt_budget(cls, value: int) -> int:
        """Reject a negative per-prompt allowance.

        ``0`` *is* meaningful here, unlike ``budget_daily_tokens``: it means
        "do not check per instruction", which is the behaviour before
        ADR-089 and a reasonable choice for an unattended run where there is
        nobody to type ``continue``.
        """
        if value < 0:
            raise ValueError(
                "budget_prompt_tokens must be 0 (no per-prompt checkpoint) "
                "or a positive number of tokens"
            )
        return value

    @field_validator("notes_max_chars")
    @classmethod
    def _usable_notes_budget(cls, value: int) -> int:
        """Reject a notes ceiling too small to hold anything useful.

        The floor is judgement rather than arithmetic: under a few hundred
        characters the model cannot record the state of a long task, so a
        scratchpad that small is a feature that looks present and is not.
        """
        if value < 500:
            raise ValueError(
                "notes_max_chars must be at least 500; use notes_enabled: "
                "false to switch short-term memory off"
            )
        return value

    @field_validator("budget_warn_fraction")
    @classmethod
    def _warn_fraction_in_range(cls, value: float) -> float:
        """Reject a warning threshold outside (0, 1].

        ``1.0`` means "warn only when exhausted", which is legal if odd;
        anything above it could never fire, and zero or below would warn
        on the first call of the day.
        """
        if not 0 < value <= 1:
            raise ValueError(
                "budget_warn_fraction must be greater than 0 and at most 1"
            )
        return value

    @field_validator("conversation_keep_turns")
    @classmethod
    def _non_negative_keep_turns(cls, value: int) -> int:
        """Reject a negative number of kept turns.

        Zero is valid: fold everything but the current turn.
        """
        if value < 0:
            raise ValueError("conversation_keep_turns must be zero or greater")
        return value

    @field_validator("conversation_summary_max_chars")
    @classmethod
    def _usable_summary_budget(cls, value: int) -> int:
        """Reject a summary ceiling too small to say anything.

        Same judgement as ``notes_max_chars``: below a few hundred
        characters a summary of several turns is a heading and nothing else.
        """
        if value < 200:
            raise ValueError("conversation_summary_max_chars must be at least 200")
        return value

    @field_validator("max_context_chars")
    @classmethod
    def _non_negative_context_chars(cls, value: int) -> int:
        """Reject a negative conversation budget.

        Zero is valid and means "never compact", which tests and anyone
        debugging a prompt want.
        """
        if value < 0:
            raise ValueError("max_context_chars must be zero or greater")
        return value


def load_settings(
    config_path: Path = DEFAULT_CONFIG_PATH,
    env_file: Path | None = DEFAULT_ENV_FILE,
) -> Settings:
    """Load YAML and env, then ensure the workspace directory exists.

    Args:
        config_path: Path to the committed YAML defaults.
        env_file: Optional dotenv file. ``None`` skips dotenv (tests).

    Returns:
        Validated settings with ``workspace_path`` resolved and created.

    Raises:
        SystemExit: If configuration cannot be read or is invalid.
    """
    if not config_path.is_file():
        raise SystemExit(f"Cannot read config file: {config_path.resolve()}")
    try:
        config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(
            f"Cannot read config file: {config_path.resolve()} ({exc})"
        ) from exc

    class _LoadedSettings(Settings):
        """Settings bound to a specific YAML / dotenv pair."""

        model_config = SettingsConfigDict(
            extra="ignore",
            env_file=env_file,
            env_file_encoding="utf-8",
            yaml_file=config_path,
        )

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            yaml_source = YamlConfigSettingsSource(
                settings_cls,
                yaml_file=config_path,
                yaml_file_encoding="utf-8",
            )
            return (
                init_settings,
                env_settings,
                dotenv_settings,
                yaml_source,
                file_secret_settings,
            )

    try:
        settings = _LoadedSettings()
    except (ValidationError, YAMLError, OSError, ValueError) as exc:
        # A missing token is by far the most common startup failure — it is
        # what happens on a TA's first run — so it gets a message that says
        # how to fix it rather than a raw pydantic traceback.
        if "llm_api_key" in str(exc):
            raise SystemExit(
                "LLM_API_KEY is not set.\n"
                "The key is a secret and is never committed, so it must come "
                "from the environment:\n"
                "  cp .env.example .env   # then set LLM_PROVIDER and LLM_API_KEY\n"
                "  docker run --rm -it --env-file .env ... agent-harness"
            ) from exc
        raise SystemExit(f"Invalid configuration:\n{exc}") from exc

    workspace = _absolute(settings.workspace_path)
    _create_directory(workspace, "workspace")
    _require_writable(workspace)

    # The second root. Checked for nesting *before* it is created, so a
    # misconfigured pair fails with an explanation rather than quietly
    # creating a memory folder the file tools can reach.
    memory = _absolute(settings.memory_path)
    reject_nested_roots(workspace, memory)
    if settings.memory_enabled:
        _create_directory(memory, "memory")
        _require_writable(
            memory,
            label="Memory directory",
            container_path="/app/memory",
            volume_name="agent-harness-memory",
        )

    # The third root is read-only and ships inside the image, so it is
    # neither created nor probed. A missing skills directory is not an
    # error: it means no skills are available, which the loader reports
    # through an empty index rather than by refusing to start.
    skills = _absolute(settings.skills_path)

    updates: dict[str, Any] = {
        "workspace_path": workspace.resolve(),
        "memory_path": memory.resolve(),
        "skills_path": skills.resolve(),
    }
    return settings.model_copy(update=updates)


def _absolute(path: Path) -> Path:
    """Anchor a possibly relative configured path to the process directory.

    Relative paths in ``config.yaml`` are resolved against the working
    directory, which is ``/app`` inside the container.

    Args:
        path: Configured path, absolute or relative.

    Returns:
        An absolute path, not yet resolved for symlinks.
    """
    return path if path.is_absolute() else Path.cwd() / path


def _create_directory(directory: Path, label: str) -> None:
    """Create one of the harness roots, failing fast if it cannot exist.

    Args:
        directory: Absolute path to create.
        label: Word used in the error message, e.g. ``workspace``.

    Raises:
        SystemExit: If the directory cannot be created.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SystemExit(f"Cannot create {label} path {directory}: {exc}") from exc


def reject_nested_roots(workspace: Path, memory: Path) -> None:
    """Refuse a configuration where the two writable roots overlap.

    The whole security argument for long-term memory is that the file tools
    cannot reach it: they are confined to ``workspace_path`` and the memory
    tools are confined to ``memory_path``, both by the same resolver
    (ADR-035). Nesting one inside the other silently deletes that guarantee
    — ``write_file`` would be able to rewrite the agent's own memory — and
    nothing downstream would report it, because every individual path check
    would still pass.

    Checked with ``relative_to`` on resolved paths rather than string
    prefixes, for the reasons given in :mod:`harness.tools.paths`.

    Args:
        workspace: Absolute workspace root.
        memory: Absolute memory root.

    Raises:
        SystemExit: If either root contains the other, or they are equal.
    """
    # ``resolve`` on a path that does not exist yet is fine and still
    # normalises ``..`` segments, which is what makes this check meaningful
    # before the directories are created.
    resolved_workspace = workspace.resolve()
    resolved_memory = memory.resolve()

    pairs = (
        (resolved_memory, resolved_workspace),
        (resolved_workspace, resolved_memory),
    )
    nested = resolved_memory == resolved_workspace
    for inner, outer in pairs:
        if nested:
            break
        try:
            inner.relative_to(outer)
        except ValueError:
            continue
        nested = True

    if nested:
        raise SystemExit(
            f"memory_path and workspace_path must not overlap.\n"
            f"  workspace_path: {resolved_workspace}\n"
            f"  memory_path:    {resolved_memory}\n"
            f"\n"
            f"Long-term memory is kept outside the workspace on purpose: the\n"
            f"file tools are confined to the workspace, so a memory directory\n"
            f"inside it could be rewritten by the agent with write_file.\n"
            f"Point memory_path at a sibling directory instead."
        )


def _require_writable(
    directory: Path,
    label: str = "Workspace",
    container_path: str = "/app/workspace",
    volume_name: str = "agent-harness-workspace",
) -> None:
    """Fail fast when a root exists but cannot be written to.

    ``mkdir(exist_ok=True)`` succeeds on a directory that is present but
    read-only, so creating it proves nothing. Without this check the run
    starts happily and the failure only appears mid-conversation, as a
    "Permission denied" observation from the first ``write_file`` — by which
    point the user has spent a model call and has no idea the cause is
    ownership rather than the agent.

    The usual cause is a bind mount: the container runs as uid 1000, but a
    mounted host folder keeps the host's ownership inside the container, so
    any other host uid is denied. That is a run-time flag problem, not
    something the image can fix, so the message says which flag.

    ``os.access`` is deliberately not used: it consults the real uid and
    ignores ACLs and read-only mounts, so it reports writable for paths that
    then fail. Actually writing a file is the only reliable probe.

    Args:
        directory: The resolved root to probe.
        label: Human-readable name of the root, used in the message.
        container_path: Where this root lives inside the image, for the
            example commands.
        volume_name: The named volume the README recommends for this root.

    Raises:
        SystemExit: If a file cannot be created inside ``directory``.
    """
    probe = directory / ".harness-write-check"
    try:
        probe.touch()
        # Removing our own probe file. This is harness startup code, not a
        # tool, so it is unrelated to the "no delete tool" rule (ADR-008) —
        # the agent still has no way to remove anything.
        probe.unlink()
    except OSError as exc:
        raise SystemExit(
            f"{label} is not writable: {directory}\n"
            f"({exc})\n"
            f"\n"
            f"The container runs as a non-root user (uid 1000). A bind-mounted\n"
            f"host folder keeps its host ownership inside the container, so a\n"
            f"host user with a different uid cannot write to it.\n"
            f"\n"
            f"The simplest fix is a named volume, which Docker creates with the\n"
            f"image's ownership:\n"
            f"  -v {volume_name}:{container_path}\n"
            f"\n"
            f"To keep the host folder instead, run as yourself on Linux:\n"
            f'  docker run --rm -it --user "$(id -u):$(id -g)" --env-file .env \\\n'
            f'    -v "$PWD/{Path(container_path).name}:{container_path}" '
            f"agent-harness\n"
            f"\n"
            f"On SELinux systems (Fedora, RHEL), relabel the mount:\n"
            f'  -v "$PWD/{Path(container_path).name}:{container_path}:Z"\n'
            f"\n"
            f"Or drop -v entirely and use the directory inside the image."
        ) from exc
