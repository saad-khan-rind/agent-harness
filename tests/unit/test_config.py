"""Fail-fast behaviour for ``load_settings``."""

import os
from pathlib import Path

import pytest
import yaml
from harness.config import PROVIDER_DEFAULTS, Settings, load_settings
from harness.permissions import PermissionRules
from pydantic import ValidationError

_VALID_YAML = {
    "llm_model": "gemma4-31b-it",
    "temperature": 0.2,
    "max_iterations": 20,
}

# The read-only-directory tests below cannot be staged where permission bits
# do not apply: root ignores them, and Windows has no ``geteuid`` at all.
#
# Evaluated through ``getattr`` rather than calling ``os.geteuid()``
# directly, because a skip condition is evaluated at *collection* time — a
# bare call raises ``AttributeError`` on Windows and takes the whole module
# down, so the other twenty tests in this file would not run either. The
# gate itself runs in the Linux container, where the condition is real.
_CANNOT_STAGE_READ_ONLY = getattr(os, "geteuid", lambda: 0)() == 0


def _write_yaml(path: Path, payload: dict[str, object] | str) -> Path:
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def test_missing_api_key_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Startup exits when LLM_API_KEY is unset."""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    workspace = tmp_path / "ws"
    config = _write_yaml(
        tmp_path / "config.yaml",
        {**_VALID_YAML, "workspace_path": str(workspace)},
    )

    # The message names the variable and says how to supply it, because this
    # is the first thing a TA hits on a fresh checkout.
    with pytest.raises(SystemExit, match="LLM_API_KEY is not set"):
        load_settings(config_path=config, env_file=None)


def test_missing_config_file_exits(tmp_path: Path) -> None:
    """Startup exits when config.yaml cannot be read."""
    missing = tmp_path / "nope.yaml"
    with pytest.raises(SystemExit, match="Cannot read config file"):
        load_settings(config_path=missing, env_file=None)


def test_invalid_yaml_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Startup exits when config.yaml is not valid YAML."""
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    config = tmp_path / "config.yaml"
    config.write_text("llm_model: [unterminated\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="Invalid configuration"):
        load_settings(config_path=config, env_file=None)


def test_valid_settings_create_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid key and YAML produce settings and create the workspace."""
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    workspace = tmp_path / "agent-ws"
    config = _write_yaml(
        tmp_path / "config.yaml",
        {**_VALID_YAML, "workspace_path": str(workspace)},
    )

    settings = load_settings(config_path=config, env_file=None)

    assert settings.llm_api_key == "test-key"
    assert settings.llm_model == "gemma4-31b-it"
    assert settings.workspace_path == workspace.resolve()
    assert workspace.is_dir()


def test_env_overrides_yaml_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM_MODEL wins over the YAML default."""
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "qwen-agentworld-35b-a3b")
    workspace = tmp_path / "ws"
    config = _write_yaml(
        tmp_path / "config.yaml",
        {**_VALID_YAML, "workspace_path": str(workspace)},
    )

    settings = load_settings(config_path=config, env_file=None)

    assert settings.llm_model == "qwen-agentworld-35b-a3b"


def test_zero_iterations_means_no_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0 is the shipped default and must load, not fail.

    It used to be rejected, back when the cap was the only thing guaranteeing
    termination. Now it means "no fixed ceiling" and the per-tool failure
    limit carries that job (ADR-047).
    """
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    config = _write_yaml(
        tmp_path / "config.yaml",
        {**_VALID_YAML, "max_iterations": 0, "workspace_path": str(tmp_path / "ws")},
    )

    settings = load_settings(config_path=config, env_file=None)

    assert settings.max_iterations == 0


# Every field below guards a different failure mode, but they all resolve
# the same way: a startup-time SystemExit rather than a surprise mid-run.
# Merged into one table instead of one function each — each row is still
# its own collected test case (`pytest --collect-only` lists eight), so
# nothing here lost coverage; what shrank is the eight-times-repeated
# "write config.yaml, assert SystemExit" boilerplate around them.
@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param(
            "temperature",
            5.0,
            id="temperature-out-of-range",
        ),  # a bad temperature must fail at startup, not after the first task
        pytest.param(
            "max_iterations",
            -1,
            id="negative-iterations",
        ),  # a negative cap would stop the loop before its first LLM call
        pytest.param(
            "max_tool_failures",
            -2,
            id="negative-tool-failure-limit",
        ),  # 0 disables the guard; below that is a typo, not a configuration
        pytest.param(
            "max_repeated_calls",
            -1,
            id="negative-repeat-limit",
        ),  # 0 disables the guard; negative is not "more disabled"
        pytest.param(
            "max_repeated_calls",
            1,
            id="repeat-limit-of-one",
        ),  # 1 would stop on the very first tool call of any kind — always a
        # mistake, since a run of one call is trivially "repeated" at n=1
        pytest.param(
            "max_tool_output_chars",
            0,
            id="zero-output-budget",
        ),  # a zero budget would truncate every observation to nothing
        pytest.param(
            "max_context_chars",
            -1,
            id="negative-context-budget",
        ),  # a negative conversation budget is a typo, not "unlimited"
        pytest.param(
            "memory_max_inject_chars",
            0,
            id="zero-memory-budget",
        ),  # zero would inject nothing while looking configured
    ],
)
def test_an_invalid_field_exits_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: float
) -> None:
    """Each of these bounds is a startup-time guard, not a runtime surprise.

    See the ``id`` and trailing comment on each case for why that particular
    value is the one that must be rejected rather than clamped or ignored.
    """
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    config = _write_yaml(
        tmp_path / "config.yaml",
        {**_VALID_YAML, field: value, "workspace_path": str(tmp_path / "ws")},
    )

    with pytest.raises(SystemExit, match="Invalid configuration"):
        load_settings(config_path=config, env_file=None)


def test_api_key_is_never_read_from_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token committed to config.yaml must not be silently accepted.

    The brief forbids committing the token. YAML is the lowest-priority
    source, so an env var always wins; this test pins that ordering so a
    future refactor cannot quietly reverse it.
    """
    monkeypatch.setenv("LLM_API_KEY", "from-env")
    config = _write_yaml(
        tmp_path / "config.yaml",
        {
            **_VALID_YAML,
            "llm_api_key": "committed-by-mistake",
            "workspace_path": str(tmp_path / "ws"),
        },
    )

    settings = load_settings(config_path=config, env_file=None)

    assert settings.llm_api_key == "from-env"


@pytest.mark.skipif(
    _CANNOT_STAGE_READ_ONLY,
    reason="root ignores permission bits, so the read-only case cannot be staged",
)
def test_unwritable_workspace_exits_with_the_docker_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only sandbox must fail at startup, not mid-conversation.

    This is the bind-mount ownership problem: the container runs as uid 1000
    and a mounted host folder keeps the host's ownership, so another uid gets
    "Permission denied" on the first write. mkdir(exist_ok=True) succeeds on
    such a directory, so only an actual write attempt catches it.
    """
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    workspace = tmp_path / "ro-ws"
    workspace.mkdir()
    config = _write_yaml(
        tmp_path / "config.yaml",
        {**_VALID_YAML, "workspace_path": str(workspace)},
    )
    workspace.chmod(0o500)  # r-x: listable, not writable
    try:
        with pytest.raises(SystemExit) as info:
            load_settings(config_path=config, env_file=None)
        message = str(info.value)
        # The message must carry the actual remedy, not just the symptom.
        assert "not writable" in message
        assert "--user" in message
        assert ":Z" in message
    finally:
        # Restore so pytest can clean the tmp tree up.
        workspace.chmod(0o700)


def test_memory_inside_the_workspace_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The configuration mistake that silently undoes the memory sandbox.

    The file tools are confined to ``workspace_path``, so a memory
    directory inside it could be rewritten with ``write_file`` — the agent
    would be able to edit what its next session loads as trusted context.
    Every individual path check would still pass, so nothing downstream
    would report it (ADR-035).
    """
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    workspace = tmp_path / "ws"
    config = _write_yaml(
        tmp_path / "config.yaml",
        {
            **_VALID_YAML,
            "workspace_path": str(workspace),
            "memory_path": str(workspace / "memory"),
        },
    )

    with pytest.raises(SystemExit) as info:
        load_settings(config_path=config, env_file=None)

    message = str(info.value)
    assert "must not overlap" in message
    # The message must carry the reason, not just the rule: this is a
    # security property, and a bare "invalid config" invites someone to
    # work around it.
    assert "write_file" in message


@pytest.mark.parametrize("filename", ["../../etc/passwd", "  "])
def test_a_project_memory_filename_that_is_a_path_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    """A config setting must not become a path traversal.

    This value is joined onto the workspace root to find project memory.
    The loader also routes it through the resolver, but a bad config should
    fail at startup with a clear message rather than silently reading
    nothing for the rest of the project's life.
    """
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    config = _write_yaml(
        tmp_path / "config.yaml",
        {
            **_VALID_YAML,
            "workspace_path": str(tmp_path / "ws"),
            "project_memory_filename": filename,
        },
    )

    with pytest.raises(SystemExit, match="Invalid configuration"):
        load_settings(config_path=config, env_file=None)


@pytest.mark.skipif(
    _CANNOT_STAGE_READ_ONLY,
    reason="root ignores permission bits, so the read-only case cannot be staged",
)
def test_unwritable_memory_exits_with_its_own_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Memory gets the same fail-fast treatment as the workspace.

    And its own volume name in the message: telling the user to mount
    ``agent-harness-workspace`` when the memory volume is the broken one would
    send them to the wrong fix.
    """
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    memory = tmp_path / "ro-memory"
    memory.mkdir()
    config = _write_yaml(
        tmp_path / "config.yaml",
        {
            **_VALID_YAML,
            "workspace_path": str(tmp_path / "ws"),
            "memory_path": str(memory),
        },
    )
    memory.chmod(0o500)
    try:
        with pytest.raises(SystemExit) as info:
            load_settings(config_path=config, env_file=None)
        message = str(info.value)
        assert "Memory directory is not writable" in message
        assert "agent-harness-memory:/app/memory" in message
    finally:
        memory.chmod(0o700)


def test_the_harness_and_the_servers_agree_about_the_timezone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One zone, declared in three places, kept in step by this test.

    ``timezone`` tells the agent what "today" and "next Monday" mean;
    ``--local-timezone`` tells the time and calendar servers what a time with
    no offset means. If they drift, the agent books "Monday 17:00" thinking
    Berlin and the calendar stores 17:00 somewhere else — a bug that shows up
    as an event on the wrong hour and nothing anywhere saying why.

    The duplication is not avoidable: the server arguments are literal
    strings in the YAML and nothing interpolates them. So the fix is a test
    rather than a refactor (ADR-079).
    """
    # The key is stripped from every test's environment by the root
    # conftest, and load_settings refuses to build without one.
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.chdir(tmp_path)
    settings = load_settings(
        Path(__file__).resolve().parents[2] / "config.yaml", env_file=None
    )

    assert settings.timezone == "Europe/Berlin"
    for server in settings.mcp_servers:
        if "--local-timezone" not in server.args:
            continue
        declared = server.args[server.args.index("--local-timezone") + 1]
        assert declared == settings.timezone, (
            f"server {server.name!r} runs in {declared!r} but the harness "
            f"thinks in {settings.timezone!r}"
        )


def test_an_unusable_timezone_is_refused() -> None:
    """A typo fails at startup, not in the middle of a demo."""
    with pytest.raises(ValidationError, match="timezone"):
        Settings(llm_api_key="test-key", timezone="Europe/Passau")  # type: ignore[call-arg]


@pytest.mark.parametrize("port", [80, 0, 65536])
def test_a_port_the_exporter_could_never_bind_is_refused(port: int) -> None:
    """Below 1024 needs root and the container runs as uid 1000 (ADR-030).

    So a privileged port is not a preference that might work — it is a
    guaranteed failure, and failing at startup with the reason beats a
    banner line saying "port unavailable" that sends someone looking for
    the process that stole it.
    """
    with pytest.raises(ValidationError, match="metrics_port"):
        Settings(llm_api_key="test-key", metrics_port=port)  # type: ignore[call-arg]


def test_traces_are_on_by_default_and_live_under_the_memory_root() -> None:
    """The trace directory is derived, not configured twice.

    One memory root, and the traces inside it: that is what keeps them off
    the workspace (where the file tools could rewrite the record of what
    they did) and on the volume that outlives ``--rm`` (ADR-083).
    """
    settings = Settings(llm_api_key="test-key")  # type: ignore[call-arg]

    assert settings.traces_enabled is True
    assert settings.traces_dirname == "traces"
    assert settings.traces_path == settings.memory_path / "traces"


@pytest.mark.parametrize("dirname", ["", "a/b", ".."])
def test_a_traces_dirname_that_is_a_path_is_refused(dirname: str) -> None:
    """A bare name only — a separator would let config relocate the traces.

    ``../workspace`` would put the trace inside the sandbox the file tools
    can write to, which is the one place it must not be.
    """
    with pytest.raises(ValidationError, match="traces_dirname"):
        Settings(llm_api_key="test-key", traces_dirname=dirname)  # type: ignore[call-arg]


def test_the_suite_strips_every_variable_env_example_documents() -> None:
    """A leftover ``METRICS_PORT`` is how CI went red while Docker stayed green.

    Env wins over YAML. Tests write ports and flags into ``config.yaml`` and
    assume they stick; they do not if the process still has the demo's
    ``.env.example`` values. ``tests/conftest.py`` strips those names. This
    pins the two files together so a new variable cannot be documented in
    one and forgotten in the other.
    """
    root = Path(__file__).resolve().parents[2]
    example = root / ".env.example"
    if not example.is_file():
        pytest.fail(
            f"{example} is missing. Inside the test image that means "
            "Dockerfile.test does not COPY it; the file is test input, like "
            "README.md, and has to be in the image for this to run."
        )
    documented: set[str] = set()
    for line in example.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        documented.add(stripped.split("=", 1)[0])
    conftest = (root / "tests" / "conftest.py").read_text(encoding="utf-8")
    missing = sorted(name for name in documented if f'"{name}"' not in conftest)
    assert missing == [], (
        f"tests/conftest.py must strip every name in .env.example; missing {missing}"
    )


def test_the_shipped_model_is_the_one_e2e_runs() -> None:
    """Env wins over YAML, so a model in ``.env.example`` would retarget e2e.

    The shipped provider is ``config.yaml``, and with no model pinned there
    the shipped model is that provider's default. ``.env.example`` must
    leave ``LLM_MODEL`` commented out, so a copied template cannot pin a
    different one, and the e2e fixture must run the same model (ADR-099,
    ADR-121).
    """
    from tests.e2e.conftest import UNATTENDED_ENV

    root = Path(__file__).resolve().parents[2]
    loaded = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    shipped = loaded["llm_model"] or PROVIDER_DEFAULTS[loaded["llm_provider"]].model
    example = (root / ".env.example").read_text(encoding="utf-8")

    assert Settings(llm_api_key="k").llm_model == shipped  # type: ignore[call-arg]
    assert not any(line.startswith("LLM_MODEL=") for line in example.splitlines())
    assert UNATTENDED_ENV["LLM_MODEL"] == shipped


# --- providers (ADR-121) -----------------------------------------------------


@pytest.mark.parametrize("provider", sorted(PROVIDER_DEFAULTS))
def test_a_provider_fills_in_its_own_endpoint_and_model(provider: str) -> None:
    """Choosing a provider is enough; its URL and model need not be known."""
    settings = Settings(llm_api_key="k", llm_provider=provider)  # type: ignore[call-arg]

    assert settings.llm_base_url == PROVIDER_DEFAULTS[provider].base_url
    assert settings.llm_model == PROVIDER_DEFAULTS[provider].model


def test_an_explicit_model_and_url_beat_the_provider_defaults() -> None:
    """The defaults fill gaps; they never overwrite what was configured."""
    settings = Settings(  # type: ignore[call-arg]
        llm_api_key="k",
        llm_provider="gemini",
        llm_model="gemini-2.5-pro",
        llm_base_url="https://proxy.example/v1",
    )

    assert settings.llm_model == "gemini-2.5-pro"
    assert settings.llm_base_url == "https://proxy.example/v1"


def test_custom_provider_requires_its_own_url_and_model() -> None:
    """No defaults for an unknown server: guessing one would leak the key."""
    with pytest.raises(ValidationError, match="LLM_BASE_URL and LLM_MODEL"):
        Settings(llm_api_key="k", llm_provider="custom")  # type: ignore[call-arg]


def test_an_unknown_provider_is_rejected() -> None:
    """A typo in LLM_PROVIDER fails at startup, not on the first request."""
    with pytest.raises(ValidationError, match="llm_provider"):
        Settings(llm_api_key="k", llm_provider="gemni")  # type: ignore[call-arg]


def test_legacy_innkube_variables_still_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing .env with the INNKUBE_* names keeps working."""
    monkeypatch.setenv("INNKUBE_API_KEY", "legacy-key")
    monkeypatch.setenv("INNKUBE_MODEL", "gemma4-31b-it")
    config = _write_yaml(tmp_path / "config.yaml", {"workspace_path": str(tmp_path)})

    settings = load_settings(config_path=config, env_file=None)

    assert settings.llm_api_key == "legacy-key"
    assert settings.llm_model == "gemma4-31b-it"


def test_provider_switches_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM_PROVIDER in .env retargets a config.yaml that pins no model."""
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    config = _write_yaml(
        tmp_path / "config.yaml",
        {"llm_provider": "innkube", "llm_model": "", "workspace_path": str(tmp_path)},
    )

    settings = load_settings(config_path=config, env_file=None)

    assert settings.llm_provider == "gemini"
    assert settings.llm_base_url == PROVIDER_DEFAULTS["gemini"].base_url
    assert settings.llm_model == PROVIDER_DEFAULTS["gemini"].model


# --- permissions (ADR-102) ---------------------------------------------------


def test_the_shipped_permission_default_is_auto_with_no_rules() -> None:
    """``auto`` in the file; the CLI resolves it by whether stdin is a TTY."""
    settings = Settings(llm_api_key="test-key")  # type: ignore[call-arg]
    assert settings.permission_mode == "auto"
    assert settings.permission_rules.to_rules().match("anything") is None
    assert settings.server_tiers() == {}


def test_rules_and_server_tiers_parse_into_the_policy_values() -> None:
    """Three lists in, one frozen ``PermissionRules`` out, patterns stripped;
    a server's ``permission`` reaches the policy keyed by its label."""
    settings = Settings(  # type: ignore[call-arg]
        llm_api_key="test-key",
        permission_rules={"deny": [" calendar__add_* "], "ask": ["memory_*"]},
        mcp_servers=[
            {"name": "web", "command": "python", "permission": "ask"},
            {"name": "time", "command": "python"},
        ],
    )
    rules = settings.permission_rules.to_rules()
    assert rules == PermissionRules(deny=("calendar__add_*",), ask=("memory_*",))
    assert rules.match("calendar__add_event") == "deny"
    assert rules.match("MEMORY_STORE") == "ask"
    assert settings.server_tiers() == {"web": "ask"}


def test_bad_permission_values_fail_at_startup() -> None:
    """A blank pattern, an unknown mode and an unknown tier are all refused."""
    with pytest.raises(ValidationError, match="must not be empty"):
        Settings(  # type: ignore[call-arg]
            llm_api_key="test-key", permission_rules={"allow": ["web__*", ""]}
        )
    with pytest.raises(ValidationError):
        Settings(llm_api_key="test-key", permission_mode="confirm")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(  # type: ignore[call-arg]
            llm_api_key="test-key",
            mcp_servers=[{"name": "web", "command": "python", "permission": "always"}],
        )


def test_the_claim_check_is_on_by_default_and_can_be_switched_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``claim_check_enabled`` ships on, and the environment can turn it off.

    On because the failure it catches was reproduced four runs in four
    against the lab endpoint; switchable so a model that never does it need
    not pay for the regex (ADR-116).
    """
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    config = _write_yaml(
        tmp_path / "config.yaml",
        {**_VALID_YAML, "workspace_path": str(tmp_path / "ws")},
    )
    assert load_settings(config_path=config, env_file=None).claim_check_enabled

    monkeypatch.setenv("CLAIM_CHECK_ENABLED", "false")
    assert not load_settings(config_path=config, env_file=None).claim_check_enabled


def test_the_shipped_config_turns_the_claim_check_on() -> None:
    """The committed default is the one the ADR argues for."""
    root = Path(__file__).resolve().parents[2]
    shipped = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    assert shipped["claim_check_enabled"] is True


def test_the_shipped_config_plans_delegation_at_run_time() -> None:
    """Shipped on; the test suite switches it off only for scripted models.

    ``tests/conftest.py`` sets ``SUBAGENT_AUTO_PLAN=false`` for every test,
    so this reads the committed file rather than ``Settings`` (ADR-117).
    """
    root = Path(__file__).resolve().parents[2]
    shipped = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    assert shipped["subagent_auto_plan"] is True
