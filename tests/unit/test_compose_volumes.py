"""Compose and ``docker run`` must mount the same two named volumes.

Compose prefixes undeclared volume names with the project name. Without an
explicit ``name:`` pin, ``docker compose run`` would write to
``agent-harness_agent-harness-workspace`` (the project prefix, from the
folder) while ``docker run -v agent-harness-workspace:…`` wrote to
``agent-harness-workspace`` — two disks, and persistence that only holds if you
always start the agent the same way (ADR-048). This file parses the
committed ``compose.yaml`` so dropping the pin fails the gate.
"""

import re
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_PATH = _REPO_ROOT / "compose.yaml"

# What ``Dockerfile.test`` copies, named once. Two tests read it: one checks
# the COPY line still names each file, the other that ``.dockerignore`` does
# not strip it from the build context before the COPY can see it.
_TEST_IMAGE_FILES = (
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "config.yaml",
    "compose.yaml",
    "Dockerfile",
    "Dockerfile.test",
    "README.md",
    ".env.example",
    "run.sh",
)
_TEST_IMAGE_DIRS = (
    "harness",
    "tests",
    "scripts",
    "observability",
    "skills",
    "mcp_server_time_plus",
    "mcp_server_web_search",
    "mcp_server_calendar",
)
_TEST_IMAGE_NEEDS = _TEST_IMAGE_FILES + _TEST_IMAGE_DIRS

# The identifiers the README ``docker run -v`` flags use. Compose must
# resolve to exactly these, not a project-prefixed variant.
_WORKSPACE_VOLUME = "agent-harness-workspace"
_MEMORY_VOLUME = "agent-harness-memory"


def _compose() -> dict[str, Any]:
    """Load the committed compose file as a mapping."""
    loaded = yaml.safe_load(_COMPOSE_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), "compose.yaml must be a mapping"
    return cast(dict[str, Any], loaded)


def test_project_name_is_pinned() -> None:
    """The project name must not follow the folder.

    The default is the directory name. This repository is currently
    ``agent-harness``; a rename would mint a new volume prefix and look like
    data loss.
    """
    assert _compose()["name"] == "agent-harness"


def test_data_volumes_use_the_docker_run_names() -> None:
    """Each declared volume's Docker identity is the unprefixed name."""
    volumes = _compose()["volumes"]

    for volume_name in (_WORKSPACE_VOLUME, _MEMORY_VOLUME):
        assert volume_name in volumes, volume_name
        declaration = volumes[volume_name]
        assert isinstance(declaration, dict), (
            f"{volume_name} must set name: explicitly, not be a bare key "
            f"(a bare key is prefixed with the project name)"
        )
        assert declaration.get("name") == volume_name
        assert declaration.get("external") is not True, (
            f"{volume_name} must be created if missing, not required to exist already"
        )


def test_harness_mounts_both_named_volumes() -> None:
    """The agent service always attaches the workspace and memory volumes."""
    mounts = _compose()["services"]["harness"]["volumes"]
    sources = {str(mount).split(":", 1)[0] for mount in mounts}

    assert _WORKSPACE_VOLUME in sources
    assert _MEMORY_VOLUME in sources
    assert f"{_WORKSPACE_VOLUME}:/app/workspace" in mounts
    assert f"{_MEMORY_VOLUME}:/app/memory" in mounts


def test_tests_service_does_not_mount_agent_volumes() -> None:
    """The gate must not be able to read or corrupt a demo's workspace or
    memory.
    """
    mounts = _compose()["services"]["tests"].get("volumes") or []
    joined = " ".join(str(mount) for mount in mounts)

    assert _WORKSPACE_VOLUME not in joined
    assert _MEMORY_VOLUME not in joined
    assert "/app/workspace" not in joined
    assert "/app/memory" not in joined


# --- the Qdrant service (ADR-065) ------------------------------------------
#
# Memory needs a second container now, and the three things below are the
# ones a wrong compose file gets wrong *silently*: the harness starts, the
# banner says memory is unavailable, and a demo proceeds without it. Parsing
# the committed file is the only way that becomes a test failure rather than
# a surprise on Thursday.


def test_the_qdrant_service_exists_on_a_pinned_image() -> None:
    """Qdrant, and a version rather than ``:latest``.

    ``:latest`` drifted to 1.19 while ``requirements.txt`` pins
    ``qdrant-client==1.15.1``, and the client prints a loud version-mismatch
    warning when the server's minor version is more than one ahead — so the
    first thing a demo showed was a warning above the banner. A pin also
    makes the database identical on every machine, which is what the
    container is for (ADR-079).

    Asserted as "not latest" plus a version shape rather than as one exact
    tag, so bumping the pin alongside the client does not need this test
    edited. What must not happen is drifting back to a floating tag.
    """
    image = _compose()["services"]["qdrant"]["image"]

    assert image.startswith("qdrant/qdrant:")
    tag = image.split(":", 1)[1]
    assert tag != "latest", "a floating tag puts a version warning in the demo"
    assert re.fullmatch(r"v?\d+\.\d+(\.\d+)?", tag), (
        f"expected a pinned version tag, got {tag!r}"
    )


def test_qdrant_storage_is_persisted_to_the_host() -> None:
    """Without this the collection lives in the container and dies with it.

    A host bind rather than a named volume, unlike the agent's own roots
    (ADR-030): the reason those need volumes — our non-root uid cannot write
    a host folder owned by someone else — does not apply, because the
    official Qdrant image runs as root. What the bind buys is a database
    visible in the checkout and inspectable without a throwaway container
    (ADR-069).
    """
    mounts = _compose()["services"]["qdrant"]["volumes"]

    # The SELinux suffix is interpolated on the end and is empty everywhere
    # but Fedora/RHEL, so the assertion is on the mount rather than on the
    # exact string (ADR-104).
    assert any(
        mount.startswith("./agent_memory_db:/qdrant/storage") for mount in mounts
    ), mounts


def test_the_harness_points_at_the_service_name_not_localhost() -> None:
    """Inside the compose network the database is ``qdrant``, not localhost.

    ``config.yaml`` defaults to localhost so a developer running the harness
    outside a container works with no edit; the containerised path needs the
    override, and its absence is invisible — memory simply never connects.

    Read from the *list* form, which the block moved to so the telemetry
    variables could be passed through as bare names (ADR-098). The mapping
    form always assigns a value, and assigning a default is exactly what
    those variables must not have.
    """
    environment = _compose()["services"]["harness"]["environment"]

    assert "MEMORY_QDRANT_URL=http://qdrant:6333" in environment


def test_qdrant_healthcheck_does_not_need_tools_the_image_lacks() -> None:
    """``python3`` is not in ``qdrant/qdrant:v1.15.4``, so that probe never passed.

    The check exists so ``docker compose ps`` tells you before a demo
    whether memory will work. A probe that cannot even start reports
    unhealthy while the process is serving ``/readyz`` (ADR-069).
    """
    check = _compose()["services"]["qdrant"]["healthcheck"]["test"]
    joined = " ".join(check) if isinstance(check, list) else str(check)
    assert "python3" not in joined
    assert "curl" not in joined
    assert "wget" not in joined
    assert "/readyz" in joined


def test_the_test_service_has_no_memory_backend() -> None:
    """The suite must pass with no Qdrant, and this is what proves we mean it.

    ``.github/workflows/ci.yml`` has no Docker, so the gate has to run against the
    in-process engine regardless. A ``depends_on`` here would hide a test
    that had quietly started needing a server.

    This used to assert that the service had no ``environment`` block at all,
    which was a cheap proxy and worked right up until the service needed one
    for an unrelated reason (``AGENT_HARNESS_E2E_CONTEXT``, ADR-077). The proxy is
    replaced by the thing it stood for: no backend URL, and no token.
    """
    tests = _compose()["services"]["tests"]

    assert "depends_on" not in tests
    environment = tests.get("environment", {})
    assert "MEMORY_QDRANT_URL" not in environment, (
        "the gate must run against the in-process engine, not a server"
    )
    # The suite must pass with no key, so the service must not be a route for
    # one to arrive by. `.env.test` reaches the *agent* containers the
    # end-to-end tier starts, through `docker run --env-file`, never this one.
    assert "LLM_API_KEY" not in environment
    assert "env_file" not in tests


def test_an_e2e_answer_does_not_include_the_budget_footer() -> None:
    """The budget line carries a clock that is not the agent's.

    Recovery tests search ``Run.answer`` for a time of day. If the harness
    footer ``resets 09:44 CEST`` stays in that string, a model that said
    nothing still "states a time" and the test fails it for the footer.
    """
    from tests.e2e.conftest import Run

    run = Run(
        task="t",
        exit_code=0,
        stdout=(
            "Agent: (the model finished without producing any text)\n"
            "  tokens: 8,553 prompt + 0 completion (session 8,553)\n"
            "  budget: 8,553 / 1,000,000 tokens in 24h (0%) · "
            "991,447 left · resets 09:44 CEST\n"
        ),
        stderr="",
    )

    assert "09:44" not in run.answer
    assert "budget:" not in run.answer
    assert "tokens:" not in run.answer


# --- profiles: what a bare `docker compose up -d` is allowed to start -------
# Added with ADR-098. Before profiles existed, the most obvious command in the
# file was the most wrong one: `docker compose up -d` would have started the
# interactive REPL detached with nobody on its stdin *and* run the whole test
# suite. These pin which services a bare `up` may touch.

# Services that must come up with no profile flag: the things the agent talks
# to, and nothing else.
_SUPPORTING_SERVICES = frozenset({"qdrant", "prometheus", "grafana"})

# Services that must be behind a profile, and which one.
_PROFILED_SERVICES = {
    "harness": "cli",
    "jaeger": "tracing",
    "tests": "test",
}


def _profiles_of(service: str) -> list[str]:
    """Return the profiles a service declares, or an empty list."""
    definition = _compose()["services"][service]
    return list(definition.get("profiles") or [])


def test_the_stop_command_cannot_delete_the_data_volumes() -> None:
    """``down -v`` deletes the workspace and long-term memory.

    No stop command should be able to do that by accident, so the script's
    ``--stop`` path must never pass it (ADR-054, ADR-098).
    """
    script = (_REPO_ROOT / "scripts" / "run.sh").read_text(encoding="utf-8")
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "compose" not in stripped:
            continue
        assert " down -v" not in stripped, stripped
        assert not stripped.endswith("down -v"), stripped


# --- both engines, one code path (ADR-104) ----------------------------------


def _script(name: str) -> str:
    """Return one committed script under ``scripts/``."""
    return (_REPO_ROOT / "scripts" / name).read_text(encoding="utf-8")


def _run_script() -> str:
    """Return the committed ``scripts/run.sh``."""
    return _script("run.sh")


def test_the_run_script_never_names_an_engine_in_a_command() -> None:
    """Podman support is one resolver, not a second copy of the script.

    Every command below the resolver runs ``"${COMPOSE[@]}"``. A literal
    ``docker compose`` or ``podman compose`` further down would be the
    beginning of a Docker-only path that works until somebody tries the
    other engine — which is how the two would drift apart.
    """
    # An engine named in *command position* — at the start of a line, after
    # a shell operator, or after `exec`. Prose inside an error message is
    # allowed to name both engines, and does.
    invoked = re.compile(r"(?:^|[;&|(]\s*|\bexec\s+)(docker compose|podman[ -]compose)")
    offenders = []
    for number, line in enumerate(_run_script().splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#") or "compose" not in stripped:
            continue
        # The resolver itself is where the names are allowed to appear.
        if "COMPOSE=(" in stripped or "version >/dev/null" in stripped:
            continue
        if invoked.search(stripped):
            offenders.append(f"{number}: {stripped}")
    assert offenders == [], offenders


def test_the_resolver_knows_all_three_compose_spellings() -> None:
    """``docker compose``, ``podman compose`` and ``podman-compose``.

    The third is a separate Python implementation with its own gaps — no
    ``--wait``, notably — so a resolver that assumed Compose v2 everywhere
    would fail on the Podman setup that is most common on older hosts.
    """
    resolver = _script("engine.sh")
    for spelling in ("docker compose", "podman compose", "podman-compose"):
        assert spelling in resolver, spelling
    assert "COMPOSE_HAS_WAIT" in resolver


@pytest.mark.parametrize("script", ["run.sh", "e2e.sh"])
def test_every_script_uses_the_shared_resolver(script: str) -> None:
    """One file decides what "podman" means, and both scripts source it.

    A second copy drifts, and the copy nobody is looking at is the one that
    stops working.
    """
    body = _script(script)
    assert "engine.sh" in body
    assert "resolve_engine" in body


def test_an_engine_asked_for_by_name_is_not_silently_swapped() -> None:
    """``--engine podman`` must not fall back to Docker.

    Falling back would run the demo on an engine the user did not choose,
    which is worse than saying what is missing.
    """
    resolver = _script("engine.sh")
    # Each named branch dies on its own resolver rather than trying the other.
    assert "_resolve_docker || _engine_die" in resolver
    assert "_resolve_podman || _engine_die" in resolver


def test_the_host_binds_carry_the_selinux_suffix() -> None:
    """Both of them, or Podman on Fedora denies a file that is plainly there.

    The failure mode is the reason this is pinned: the container reports a
    missing config, the file is in the checkout, and nothing connects the
    two.
    """
    services = _compose()["services"]
    binds = [
        *services["qdrant"]["volumes"],
        *[mount for mount in services["harness"]["volumes"] if mount.startswith("./")],
    ]
    host_binds = [mount for mount in binds if mount.startswith("./")]
    assert host_binds, "expected at least the qdrant storage and config binds"
    for mount in host_binds:
        assert mount.endswith("${SELINUX_SUFFIX:-}"), mount


def test_the_test_service_socket_is_overridable_for_podman() -> None:
    """Podman is daemonless but ships a compatible socket at another path.

    The default has to stay Docker's, so nothing needs setting to use
    Docker, and the container side has to stay ``/var/run/docker.sock``
    because that is where the docker CLI in the test image looks.
    """
    mounts = _compose()["services"]["tests"]["volumes"]
    socket = [mount for mount in mounts if mount.endswith(":/var/run/docker.sock")]

    assert socket == ["${ENGINE_SOCKET:-/var/run/docker.sock}:/var/run/docker.sock"]


def test_the_root_entry_point_forwards_rather_than_duplicates() -> None:
    """``./run.sh`` is what the README tells people to type.

    It has to be a forwarder: a second copy of the argument parsing is a
    second thing to keep correct, and the one that gets forgotten is
    whichever one the reader is not looking at.
    """
    entry = _REPO_ROOT / "run.sh"
    if not entry.is_file():
        pytest.fail(
            f"{entry} is missing. Inside the test image that means "
            "Dockerfile.test does not COPY it; the file is test input, like "
            "README.md, and has to be in the image for this to run."
        )
    root = entry.read_text(encoding="utf-8")

    assert "scripts/run.sh" in root
    assert root.count("exec ") == 1
    # Short enough that it is obviously a forwarder and not a fork of it.
    assert len(root.splitlines()) < 15


def test_auto_detection_requires_a_running_engine_not_just_an_installed_one() -> None:
    """A Mac with the Docker CLI on PATH and Desktop stopped has Podman too.

    Choosing on "is it installed" picked Docker there and then reported a
    dead daemon, while a working Podman sat next to it. Only a *forced*
    engine is allowed to fail that way — that is the point of forcing one.
    """
    resolver = _script("engine.sh")

    assert "_engine_running" in resolver
    # The auto branch checks both halves; the forced branches do not.
    assert "_resolve_docker && _engine_running docker" in resolver
    assert "_resolve_podman && _engine_running podman" in resolver


def test_every_repo_file_a_test_reads_is_in_the_test_image() -> None:
    """The mistake that has now been made five times.

    A test that reads a committed file passes on a developer's machine and
    errors inside ``agent-harness-tests``, because the image is a *copy* of
    the repository and only what ``Dockerfile.test`` names is in it. The
    entrypoint test, the README port check, the observability stack tests,
    the ``.env.example`` pin and ``run.sh`` all learned this the same way.

    So the COPY line is asserted here rather than rediscovered there.
    """
    path = _REPO_ROOT / "Dockerfile.test"
    if not path.is_file():
        pytest.fail(
            f"{path} is missing. Inside the test image that means it is not "
            "on its own COPY line — which is the very mistake this test is "
            "here to catch."
        )
    dockerfile = path.read_text(encoding="utf-8")
    copied = ""
    for line in dockerfile.splitlines():
        if line.startswith("COPY ") and line.rstrip().endswith(" ./"):
            copied += " " + line
    for needed in _TEST_IMAGE_FILES:
        assert f" {needed} " in copied, (
            f"{needed} is read by a test but not copied into the test image"
        )


@pytest.mark.parametrize("name", [".dockerignore", "Dockerfile.test.dockerignore"])
def test_an_ignore_file_never_strips_what_the_test_image_copies(name: str) -> None:
    """Either file may be the one this image is built with, so both must work.

    BuildKit prefers `Dockerfile.test.dockerignore`; Buildah has no
    per-Dockerfile ignore file and falls back to `.dockerignore` for every
    build. `.dockerignore` excluding `*.md` and `tests` therefore made the
    test image unbuildable on Podman: `COPY … README.md` failed on a file
    plainly in the checkout. Re-including them with `!` did not fix it — the
    negations had no effect on that path — so both files now exclude nothing
    the image copies, and there is nothing left to negate (ADR-104).

    Asserted as a property rather than as literal lines, because the lines
    are what kept changing. Note that an ignore file is not itself build
    input: BuildKit strips it, so inside the test image both are absent and
    the successful build is the stronger evidence anyway.
    """
    ignore = _REPO_ROOT / name
    if not ignore.is_file():
        # Running inside agent-harness-tests. The build got this far, which
        # means nothing on the COPY line was excluded — check they arrived.
        for needed in _TEST_IMAGE_NEEDS:
            assert (_REPO_ROOT / needed).exists(), needed
        return

    patterns = [
        stripped
        for line in ignore.read_text(encoding="utf-8").splitlines()
        if (stripped := line.strip()) and not stripped.startswith("#")
    ]
    assert not [p for p in patterns if p.startswith("!")], (
        "no `!` re-includes: they did not take effect on Podman, which is "
        "what this file exists to survive. Narrow the exclusion instead."
    )
    for needed in _TEST_IMAGE_NEEDS:
        for pattern in patterns:
            assert not fnmatch(needed, pattern), (
                f"{needed!r} is copied into the test image, but {name} "
                f"excludes it from the build context with {pattern!r}"
            )


def test_a_wider_build_context_cannot_widen_the_shipped_image() -> None:
    """Why the ignore files above are allowed to be permissive.

    What keeps `tests/` and the daily reports out of the shipped image is
    `Dockerfile`, not `.dockerignore`: there is no `COPY . .`, so every path
    in the image is one somebody wrote down. Lose that and the ignore files
    become load-bearing again for something they demonstrably cannot carry
    across two engines (ADR-104).
    """
    shipped = (_REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    for line in shipped.splitlines():
        assert not line.startswith("COPY . "), line
        assert line.strip() != "COPY . .", line
    assert "COPY tests" not in shipped


def test_traces_go_to_jaeger_by_its_service_name_inside_compose() -> None:
    """``localhost:4318`` inside the harness container is the container itself.

    config.yaml's default is right for a harness on the host and sent every
    span nowhere inside compose, silently, unless the session came in
    through ``run.sh --tracing`` (ADR-111). The default here fixes that and
    still lets the shell or ``.env`` override it.
    """
    environment = _compose()["services"]["harness"]["environment"]

    assert "OTLP_ENDPOINT=${OTLP_ENDPOINT:-http://jaeger:4318}" in environment


def test_the_tests_service_can_open_the_socket_under_selinux() -> None:
    """The end-to-end tier has to reach Podman's socket, not only Docker's.

    Podman enforces SELinux, including in the VM `podman machine` runs on a
    Mac, and the socket's label refuses a confined container whatever groups
    it holds. Without this option every e2e test failed on Podman with
    "permission denied" on the socket while passing on Docker (ADR-115).
    """
    tests = _compose()["services"]["tests"]
    assert "label=disable" in tests.get("security_opt", [])
    # Only the service that already holds the socket. The agent's own
    # container stays fully confined.
    assert "security_opt" not in _compose()["services"]["harness"]


def test_e2e_script_finds_podmans_own_socket_on_linux() -> None:
    """On Linux the default socket path may be Docker's, or missing (ADR-115)."""
    script = _script("e2e.sh")
    assert "RemoteSocket.Path" in script
    assert "export ENGINE_SOCKET" in script


def test_run_script_refuses_to_start_over_the_other_engines_stack() -> None:
    """Both engines publish the same host ports, so only one stack can run.

    Starting on Docker while the stack was up on Podman (or the reverse)
    failed after the build with a bare "address already in use"; the script
    now checks the other engine first and names the stop command.
    """
    script = _run_script()
    assert "already running on $OTHER" in script
    assert "./run.sh --stop --engine $OTHER" in script
    # Before the token check and the build, so the refusal is immediate.
    assert script.index("already running on $OTHER") < script.index('$ENGINE" build')


def test_run_script_help_is_portable_to_bsd_sed() -> None:
    """macOS ships BSD sed, which has no ``\\s`` or ``\\?``: --help printed
    every line with its ``#`` still on."""
    script = _run_script()
    assert "sed 's/^#\\s" not in script
    assert "sed 's/^# \\{0,1\\}//'" in script
