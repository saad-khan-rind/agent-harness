"""The end-to-end tier: the shipped container, a real model, throwaway volumes.

Everything else in this repository tests the harness with the model replaced.
This directory does not. Every test here starts the image that would be
graded, hands it a task in English, lets **the model ``config.yaml`` ships**
decide which tools to call, and then asserts on what is actually on disk
afterwards. Nothing is scripted, nothing is stubbed, and a test failing here
means an agent could not do the job — not that a wire came loose
(ADR-074, ADR-099).

That is the tier the brief describes. Section 7.4 says "all tasks will be
tested using the LLMs provided through the InnKube API", and its own two
worked examples assert things like *"Assistant reports 5 lines inside
notes.txt"* — a claim about what a model said, which a scripted model can
only ever confirm about itself. The deterministic versions of these same
cases still exist, one directory over in ``tests/integration/``; neither
replaces the other.

## Isolation

Each test gets its own set of Docker volumes, named after a fresh UUID, and
they are removed in the fixture's teardown whether the test passed, failed or
raised. Nothing is shared between tests and nothing survives one: a test
cannot see another's files, cannot be poisoned by another's memory, and
cannot leave anything behind on the machine that ran it. The named volumes
the demo uses — ``agent-harness-workspace`` and friends — are never touched, so
running this suite cannot destroy a demo that was set up by hand (ADR-075).

The same goes for the token. It is read from ``.env.test`` — gitignored, and
separate from the ``.env`` a demo uses so that a test run and a demo cannot
reconfigure each other. It holds only what cannot be committed or differs per
person: the provider and its token. The model is not a secret — it lives in
``config.yaml``,
and ``UNATTENDED_ENV`` injects it so a leftover ``LLM_MODEL`` in this
file cannot silently retarget the tier onto a model nobody ships
(ADR-077, ADR-099). ``.env.example`` is the template to copy the token from.

## Running them

Nothing extra to type. ``scripts/ci-gate.sh`` runs this tier when it can and
prints why when it cannot, so the usual command covers it:

    docker compose run --build --rm tests   # lint, types, unit, integration, e2e
    ./scripts/ci-gate.sh                    # the same, on the host
    ./scripts/e2e.sh                        # this tier alone

Two things have to be true, and the gate names whichever is missing: a
``.env.test`` with a real token, and a reachable Docker daemon. Without
either the tier skips — loudly, with a reason — and the gate carries on,
which is what CI and a TA with no key see.

The default ``pytest`` deselects the tier (``-m "not e2e"`` in
``pyproject.toml``), so ``pytest`` alone stays fast, free and keyless. Note
that ``pytest tests/e2e`` therefore runs *nothing*: naming the directory
selects these tests and the marker filter then deselects them. ``-m`` takes
one value and the last wins, so ``pytest tests/e2e -m e2e`` is the form that
works.
"""

import contextlib
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

import pytest
import yaml
from harness.config import PROVIDER_DEFAULTS

# The repository root: the build context for the image under test, and where
# `.env.test` lives. Found from this file rather than from the working
# directory, because pytest can be invoked from anywhere.
#
# `AGENT_HARNESS_E2E_CONTEXT` overrides it, and exists for one caller: the gate
# container (`docker compose run --rm tests`), which mounts the real
# repository read-only at /host and points this there. Inside that container
# the code under `/app` is a *copy* made by `Dockerfile.test`, with no
# `.dockerignore` and no `.env.test` — building from it would produce a
# near-enough image from a subtly different context, which is the kind of
# difference that makes an end-to-end result mean less than it looks
# (ADR-077).
PROJECT_ROOT = Path(
    os.environ.get("AGENT_HARNESS_E2E_CONTEXT") or Path(__file__).resolve().parents[2]
)

ENV_FILE = PROJECT_ROOT / ".env.test"

# Built once per session unless a tag is supplied. A separate tag from the
# demo image on purpose: building here must never overwrite the image someone
# has running.
IMAGE = os.environ.get("AGENT_HARNESS_E2E_IMAGE", "agent-harness-e2e")

# Mount points inside the image. These are the paths `config.yaml` resolves
# to, and the paths the Dockerfile creates and chowns to uid 1000, which is
# why the volumes come up writable for the non-root agent (ADR-030).
WORKSPACE = "/app/workspace"
MEMORY = "/app/memory"
CALENDAR = "/app/calendar"

# One task, one model, one container. Generous because the lab models are
# slow and a multi-step task is several completions deep; a test that times
# out tells you nothing about the harness.
#
# Not *unboundedly* generous, though. `MAX_ITERATIONS` is 15 and a healthy
# run finishes well inside a minute, so five minutes is already far outside
# normal — and a ceiling that is too high turns a wedged run into a quarter
# of an hour of silence rather than a failure you can read (ADR-079).
RUN_TIMEOUT_SECONDS = 300

# Docker's own calls — create a volume, remove one, read a file back. These
# are local and instant; a hang here is a broken daemon, not a slow model.
DOCKER_TIMEOUT_SECONDS = 120

# Stamped on every container a stack starts, so teardown can find them by
# label. `--rm` only removes a container that exits by itself; one killed by
# Ctrl-C, or left running by an interrupted pytest, survives and keeps its
# volume pinned. The label is what makes the sweep possible (ADR-080).
STACK_LABEL = "agent-harness-e2e-stack"

# The same pin `compose.yaml` uses, and for the same reason: `:latest` drifted
# to a version `qdrant-client` 1.15.1 warns about, and a floating tag makes
# the store a different database on every machine (ADR-079). Kept in step by
# `tests/unit/test_compose_volumes.py`.
QDRANT_IMAGE = "qdrant/qdrant:v1.15.4"


def _env_file_value(name: str) -> str:
    """Return one non-secret variable from ``.env.test``, or ``""``."""
    if not ENV_FILE.is_file():
        return ""
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip()
    return ""


def _shipped_llm_model() -> str:
    """Return the model this tier runs, or ``AGENT_HARNESS_E2E_MODEL`` if set.

    The provider comes from ``.env.test`` (it decides where the token is
    valid), falling back to ``config.yaml``. The model is then the one
    ``config.yaml`` pins, or that provider's default — never a leftover
    ``LLM_MODEL`` in ``.env.test``, which would silently retarget every run
    onto a model nobody ships. ``AGENT_HARNESS_E2E_MODEL`` is the deliberate
    override; a stale dotenv file is not (ADR-099, ADR-121).
    """
    override = os.environ.get("AGENT_HARNESS_E2E_MODEL", "").strip()
    if override:
        return override
    loaded = yaml.safe_load((PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8"))
    pinned = str(loaded.get("llm_model") or "").strip()
    if pinned:
        return pinned
    provider = _env_file_value("LLM_PROVIDER") or str(loaded["llm_provider"])
    defaults = PROVIDER_DEFAULTS.get(provider)
    # `custom` has no default model; its own LLM_MODEL is the only answer.
    return defaults.model if defaults else _env_file_value("LLM_MODEL")


# The only settings these tests override. `config.yaml` ships
# `max_iterations: 0` — no cap — which is right for a person at a terminal:
# they can see a run going nowhere and press Ctrl-C. A test cannot, so
# without a ceiling "the model is confused" becomes a fifteen-minute hang
# instead of a failed assertion (ADR-047).
#
# The model is the other one. Env wins over YAML, and `.env.test` is handed
# to `docker run --env-file`, so a leftover `LLM_MODEL=gemma4-31b-it`
# would make this tier evidence of a model nobody ships. `--env` after
# `--env-file` is what lets this dict win (ADR-077, ADR-099).
UNATTENDED_ENV = {
    "MAX_ITERATIONS": "15",
    "LLM_MODEL": _shipped_llm_model(),
}

# The port `config.yaml` serves metrics on inside the container. Reached by
# container name on this stack's network rather than published to the host —
# see `Session` for why that distinction cost an afternoon (ADR-081).
METRICS_PORT = 9091

# The harness's own wording when the policy refuses a call
# (`AgentLoop._denied`). Matched rather than reconstructed because it is the
# only thing that tells a refused call apart from an executed one in the
# printed trace — and it is written by the harness, never by a tool, which
# is what makes it safe to match on here (ADR-057).
PERMISSION_REFUSAL = "is not permitted by the active permission policy"

# How long an interactive session waits for a line it expects. The same
# ceiling as a one-shot run, for the same reason: it is a real model.
SESSION_TIMEOUT_SECONDS = RUN_TIMEOUT_SECONDS


# Every name a delete-shaped tool could plausibly have. If one of these ever
# turns up in a trace, something registered a capability this project says it
# does not have — ADR-008 failing, not a model misbehaving.
DESTRUCTIVE_NAMES = ("delete", "remove", "rm_", "unlink", "destroy", "purge", "drop")


def parse_trace(output: str) -> list[tuple[str, bool]]:
    """Read a printed trace into ``(tool, refused_by_policy)`` pairs.

    **The distinction this draws is the reason it is a function.** The
    ``→ name(args)`` line is printed *before* the permission check —
    deliberately, so a person watching sees what is being asked about before
    a confirmation prompt appears — which means a refused call looks exactly
    like an executed one to anything that reads only those lines. A test
    that counted them would report a working permission gate as a violation,
    which is precisely backwards and is a mistake this suite has made once
    already (ADR-082).

    So each request line is paired with the observation that follows it, and
    the call is marked refused when that observation is the harness's own
    denial. The wording belongs to the harness, never to a tool, which is
    what makes matching on it safe here (ADR-057).

    Shared by :class:`Run` and :class:`Session` rather than written twice:
    one is a finished run and the other is a growing buffer, but the trace
    format is the same and so is the trap.

    Args:
        output: Everything the harness printed, or as much as has arrived.

    Returns:
        One entry per call, in order.
    """
    calls: list[tuple[str, bool]] = []
    pending: str | None = None
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("→ ") and "(" in stripped:
            if pending is not None:
                # A request with no observation before it: the result has
                # not been printed yet. Counted as having run, because the
                # gate is consulted before the tool and a refusal would
                # already be on screen.
                calls.append((pending, False))
            pending = stripped[2:].split("(", 1)[0].strip()
        elif stripped.startswith("←") and pending is not None:
            calls.append((pending, PERMISSION_REFUSAL in stripped))
            pending = None
    if pending is not None:
        calls.append((pending, False))
    return calls


def no_destructive_tool_was_called(tools: list[str]) -> bool:
    """Whether a trace is free of anything delete-shaped.

    Lives here rather than in one test file because two of them need it, and
    a test importing a private name out of another test module is a worse
    kind of coupling than a shared helper.
    """
    return not [name for name in tools if any(w in name for w in DESTRUCTIVE_NAMES)]


def _docker_available() -> str | None:
    """Return why Docker is unusable, or ``None`` when it is fine."""
    if shutil.which("docker") is None:
        return "docker is not installed on this machine"
    probe = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        text=True,
        timeout=DOCKER_TIMEOUT_SECONDS,
        check=False,
    )
    if probe.returncode != 0:
        return "the docker daemon is not reachable"
    return None


def _token_available() -> str | None:
    """Return why the token is unusable, or ``None`` when it is fine.

    Read rather than parsed into the environment: the value is only ever
    handed to ``docker run --env-file``, so this process never holds it and
    it cannot leak into a traceback or a captured-output dump.
    """
    if not ENV_FILE.is_file():
        return f"{ENV_FILE.name} is missing (copy .env.example and put the token in it)"
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition("=")
        if name.strip() in ("LLM_API_KEY", "INNKUBE_API_KEY") and value.strip():
            if value.strip() == "your_api_key_here":
                return f"{ENV_FILE.name} still holds the placeholder token"
            return None
    return f"{ENV_FILE.name} sets no LLM_API_KEY"


@pytest.fixture(scope="session")
def e2e_image() -> str:
    """Build the shipped image once, and skip the whole tier if we cannot.

    The image is the deliverable, so it is built from the real ``Dockerfile``
    with the repository as context — not a test variant. ``Dockerfile.test``
    is a different thing entirely: it runs the *suite* inside a container,
    whereas this runs the *agent* inside one.
    """
    for reason in (_docker_available(), _token_available()):
        if reason is not None:
            pytest.skip(f"end-to-end tier needs a real run: {reason}")

    if os.environ.get("AGENT_HARNESS_E2E_IMAGE"):
        # Supplied by the caller — CI, or a developer who has just built it.
        # Trust it rather than spending three minutes rebuilding.
        return IMAGE

    build = subprocess.run(
        ["docker", "build", "-t", IMAGE, "."],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT_SECONDS,
        check=False,
    )
    if build.returncode != 0:
        pytest.fail(f"could not build {IMAGE}:\n{build.stderr[-4000:]}")
    return IMAGE


@dataclass
class Run:
    """What one container run produced.

    Attributes:
        task: The English task the agent was given.
        exit_code: The container's exit status.
        stdout: Everything printed, which is the tool trace and the final
            ``Agent:`` line.
        stderr: Anything on the error stream.
    """

    task: str
    exit_code: int
    stdout: str
    stderr: str

    @property
    def answer(self) -> str:
        """The agent's final reply, lowercased, or ``""`` if it never got there.

        The reply runs from the ``Agent: `` line to the end of the output,
        because a model's answer is very often several lines long and taking
        only the first would silently truncate exactly the part a test is
        looking for. The token-usage and budget lines the trace appends are
        dropped: they are harness footers, and the budget line carries a
        clock (``resets 09:44 CEST``) that is not the agent's.

        Lowercased because every assertion on it is a tolerant one — a model
        that writes "Five" instead of "five" has still answered correctly,
        and a test that disagrees with it is testing capitalisation.
        """
        lines = self.stdout.splitlines()
        for index, line in enumerate(lines):
            if line.startswith("Agent: "):
                reply = [line[len("Agent: ") :], *lines[index + 1 :]]
                kept = [
                    one
                    for one in reply
                    if not one.startswith(("  tokens: ", "  budget: "))
                ]
                return "\n".join(kept).strip().lower()
        return ""

    @property
    def trace(self) -> list[tuple[str, bool]]:
        """Each call, as ``(name, refused)``. See :func:`parse_trace`."""
        return parse_trace(self.stdout)

    @property
    def tools_called(self) -> list[str]:
        """Every tool the model *asked for*, in order, refused or not.

        Note what this does not say: that the tool ran. Reading it back is
        how these tests assert on *behaviour* — that the agent reached for
        the calendar server rather than inventing an answer — without
        constraining the exact arguments, which are the model's business.
        For anything about permissions use :attr:`tools_that_ran`, because a
        refused call appears here too.
        """
        return [name for name, _ in self.trace]

    @property
    def tools_that_ran(self) -> list[str]:
        """Every tool the policy actually let through, in order."""
        return [name for name, refused in self.trace if not refused]

    @property
    def tool_errors(self) -> list[str]:
        """Every failed tool observation, as the model saw it.

        ``harness/cli.py`` marks a failure with ``← !`` and a success with
        ``←``, which is the only place a test can tell the two apart from
        outside the process. Used by the recovery tests: "did the agent hit
        an error" is not something a prompt can guarantee, so the assertion
        has to be able to ask.
        """
        found = []
        for line in self.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("← !"):
                found.append(stripped[3:].strip())
        return found

    def assert_completed(self) -> None:
        """Fail with the transcript when the run did not reach an answer.

        One-shot mode returns 0 even when the endpoint refuses, printing
        ``LLM error:`` instead of an answer (``harness/cli.py``), so the exit
        code alone is not evidence of anything. Checked in one place because
        every test needs it and none of them should have to remember.
        """
        assert self.exit_code == 0, (
            f"container exited {self.exit_code} for task {self.task!r}\n"
            f"--- stdout ---\n{self.stdout}\n--- stderr ---\n{self.stderr}"
        )
        assert "LLM error:" not in self.stdout, (
            f"the endpoint refused the run for task {self.task!r}\n{self.stdout}"
        )
        assert self.answer, (
            f"the agent never produced a final answer for task {self.task!r}\n"
            f"{self.stdout}"
        )


@dataclass
class Session:
    """A REPL held open in the container, so something can be observed mid-run.

    Every other test here is one-shot: hand the container a task, wait for
    the process to exit, look at the volumes. That cannot work for metrics.
    The exporter lives and dies with the process, so by the time a one-shot
    run has exited there is nothing left to scrape — the numbers were real
    and are now gone.

    So this keeps the session alive: the REPL reads tasks on stdin and the
    test scrapes the exporter *while* the agent is running, which is also
    what a person sees during the demo (ADR-081).

    **The scrape goes over the Docker network, not a published port**, and
    that is a correction rather than a preference. Publishing binds on the
    *host*, but the gate itself runs inside a container that talks to the
    host's daemon through a mounted socket (``docker compose run tests``) —
    so the published port appeared on the host's loopback while the test
    looked at its own, found nothing, and retried until it timed out. Going
    through the daemon works from either place.

    It is also the better test. Nothing in production reaches this endpoint
    over a published port: Prometheus scrapes ``harness:9091`` across the
    compose network, which is exactly the path a sibling container takes
    here. The published port is a convenience for a human with ``curl``, and
    ``tests/unit/test_compose_volumes.py`` already pins that it is declared.

    Attributes:
        container: The ``--name``, which is also the hostname a sibling
            container resolves it by.
        network: The Docker network the agent and the probes share.
        image: The agent image, reused for the probes — it is already local
            and already has Python.
        stack: The owning stack's name, for the teardown label.
        process: The ``docker run`` client process.
        chunks: Everything printed so far, appended by a reader thread.
            Read :attr:`output` instead of this.
    """

    container: str
    network: str
    image: str
    stack: str
    process: "subprocess.Popen[str]"
    chunks: list[str] = field(default_factory=list)

    @property
    def output(self) -> str:
        """Everything the session has printed so far, as one string."""
        return "".join(self.chunks)

    # -- driving it --------------------------------------------------------

    def ask(self, task: str) -> None:
        """Type one task at the ``You:`` prompt.

        Args:
            task: Plain English, exactly as a user would type it.
        """
        stdin = self.process.stdin
        assert stdin is not None, "the session was started without a stdin pipe"
        stdin.write(f"{task}\n")
        stdin.flush()

    def wait_for_prompt(
        self, count: int, timeout: float = SESSION_TIMEOUT_SECONDS
    ) -> None:
        """Block until the ``You:`` prompt has been printed ``count`` times.

        The prompt coming back is the only outside sign that a turn has
        finished — its trace file closed, its run-end hooks done. Counting
        rather than waiting for the text, because the first prompt is on
        screen before any task is typed.

        Args:
            count: How many prompts to wait for; ``2`` is "the first task
                has finished".
            timeout: Seconds before giving up.

        Raises:
            AssertionError: If it never happens, with the transcript.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.output.count("You:") >= count:
                return
            if self.process.poll() is not None:
                raise AssertionError(
                    f"the session exited before prompt {count}\n{self.transcript}"
                )
            time.sleep(0.5)
        raise AssertionError(
            f"waited {timeout:.0f}s for prompt {count} and it never came\n"
            f"{self.transcript}"
        )

    def wait_for(self, needle: str, timeout: float = SESSION_TIMEOUT_SECONDS) -> str:
        """Block until the output contains ``needle``, and return that line.

        Searches the whole buffer rather than a list of completed lines,
        because the REPL's prompt is ``input("You: ")`` and writes no
        newline. A line-based wait would sit there until something *else*
        finished the line, waiting for output that had already arrived.

        Args:
            needle: Substring to wait for.
            timeout: Seconds before giving up.

        Returns:
            The last line containing ``needle``.

        Raises:
            AssertionError: If it never arrives, with the transcript
                attached — a timeout with no output is a failure nobody can
                diagnose.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            seen = self.output
            if needle in seen:
                matched = [line for line in seen.splitlines() if needle in line]
                return matched[-1] if matched else needle
            if self.process.poll() is not None:
                raise AssertionError(
                    f"the session exited before printing {needle!r}\n{self.transcript}"
                )
            time.sleep(0.5)
        raise AssertionError(
            f"waited {timeout:.0f}s for a line containing {needle!r} and it "
            f"never came\n{self.transcript}"
        )

    # -- looking at it -----------------------------------------------------

    def scrape(self, timeout: float = 30.0) -> str:
        """Fetch the exposition text once, from a container on the network.

        Args:
            timeout: Seconds allowed for the whole attempt.

        Returns:
            The raw ``/metrics`` body.
        """
        return self.scrape_until("", timeout=timeout)

    def scrape_until(
        self, needle: str, timeout: float = SESSION_TIMEOUT_SECONDS
    ) -> str:
        """Poll ``/metrics`` until it contains ``needle``, and return the body.

        Polling rather than scraping once: a counter appears when the run
        reaches the point that increments it, and a test that scraped
        immediately would be racing a real model over the network.

        The polling happens **inside one probe container** rather than as a
        loop out here that starts a container per attempt. Same reason
        :meth:`Stack._wait_for_memory` does it that way: a container per
        half-second would cost more than the thing being waited for, and
        would make a slow model look like a daemon under load.

        Args:
            needle: Substring to wait for in the exposition text. Empty
                matches the first successful fetch.
            timeout: Seconds before giving up.

        Returns:
            The body that first contained ``needle``.

        Raises:
            AssertionError: If it never appears, with the last body and the
                session transcript attached.
        """
        script = (
            "import sys,time,urllib.request\n"
            "url, needle, limit = sys.argv[1], sys.argv[2], float(sys.argv[3])\n"
            "deadline = time.monotonic() + limit\n"
            "last = ''\n"
            "while time.monotonic() < deadline:\n"
            "    try:\n"
            "        last = urllib.request.urlopen(url, timeout=5).read().decode()\n"
            "    except Exception:\n"
            # A refused connection in the first second is the exporter not
            # being up yet, which is normal; in the thirtieth it is not.
            "        time.sleep(0.5)\n"
            "        continue\n"
            "    if needle in last:\n"
            "        sys.stdout.write(last)\n"
            "        sys.exit(0)\n"
            "    time.sleep(0.5)\n"
            "sys.stderr.write(last[-2000:])\n"
            "sys.exit(1)\n"
        )
        url = f"http://{self.container}:{METRICS_PORT}/metrics"
        prober = f"{self.stack}-scrape-{uuid.uuid4().hex[:6]}"
        probe = _docker(
            "run", "--rm",
            "--name", prober,
            "--label", f"{STACK_LABEL}={self.stack}",
            "--network", self.network,
            "--entrypoint", "python",
            self.image,
            "-c", script, url, needle, str(timeout),
            check=False,
            # The probe stops itself at `timeout`; this is the outer bound
            # for the container that holds it, not a second deadline.
            timeout=int(timeout) + 60,
            container=prober,
        )  # fmt: skip
        if probe.returncode != 0:
            raise AssertionError(
                f"waited {timeout:.0f}s for {needle!r} at {url} and it never "
                f"appeared\n--- last body ---\n{probe.stderr[-2000:]}\n"
                f"{self.transcript}"
            )
        return probe.stdout

    @property
    def transcript(self) -> str:
        """Everything printed so far, for a failure message."""
        return "--- session output ---\n" + self.output

    @property
    def trace(self) -> list[tuple[str, bool]]:
        """Each call so far, as ``(name, refused)``. See :func:`parse_trace`."""
        return parse_trace(self.output)

    @property
    def tools_called(self) -> list[str]:
        """Every tool the model *asked for*, in order, refused or not.

        Note what this does not say: that the tool ran. Use :attr:`trace`
        when the difference matters, which it does for anything about
        permissions.
        """
        return [name for name, _ in self.trace]

    @property
    def tools_that_ran(self) -> list[str]:
        """Every tool the policy actually let through, in order."""
        return [name for name, refused in self.trace if not refused]

    # -- shutting it down --------------------------------------------------

    def close(self) -> None:
        """Ask the REPL to exit, and remove the container if it will not.

        ``/exit`` first because that exercises the shutdown path the agent
        actually has — MCP subprocesses stopped, Qdrant client closed,
        metrics server shut down. A force-remove after a grace period,
        because a test must not be able to leave a container running whatever
        state the agent got itself into (ADR-080).
        """
        with contextlib.suppress(OSError, ValueError):
            stdin = self.process.stdin
            if stdin is not None and not stdin.closed:
                stdin.write("/exit\n")
                stdin.flush()
                stdin.close()
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            _docker("rm", "--force", self.container, check=False, timeout=30)
            self.process.kill()
            self.process.wait(timeout=30)


@dataclass
class Stack:
    """One test's throwaway container world.

    A name, a set of volumes, an optional network with a Qdrant on it, and
    the bookkeeping to tear all of it down again. Created by the ``stack``
    fixture; no test builds one itself.
    """

    image: str
    name: str
    volumes: dict[str, str] = field(default_factory=dict)
    network: str | None = None
    containers: list[str] = field(default_factory=list)
    extra_env: dict[str, str] = field(default_factory=lambda: dict(UNATTENDED_ENV))

    # -- lifecycle ---------------------------------------------------------

    def volume(self, role: str) -> str:
        """Create this stack's volume for ``role`` if it does not exist yet.

        Args:
            role: ``workspace``, ``memory``, ``calendar`` or ``qdrant``.

        Returns:
            The Docker volume name, which carries the stack's UUID so it can
            never collide with the demo volumes or with another test's.
        """
        if role not in self.volumes:
            created = f"{self.name}-{role}"
            _docker("volume", "create", created)
            self.volumes[role] = created
        return self.volumes[role]

    def ensure_network(self) -> str:
        """Create this stack's network if it does not exist yet, and name it.

        One network per stack, shared by the agent, a throwaway Qdrant and
        any probe container. Containers on a user-defined network resolve
        each other by name, which is what lets a probe reach the agent at
        ``http://<container>:9091`` without a published port — and therefore
        what makes the metrics tests work when the gate is itself running
        inside a container (ADR-081).
        """
        if self.network is None:
            self.network = f"{self.name}-net"
            _docker("network", "create", self.network)
        return self.network

    def with_memory(self) -> str:
        """Start a throwaway Qdrant and return the URL the agent should use.

        Long-term memory is a second service, so testing it end-to-end means
        running one. It gets its own volume and its own network, both named
        after this stack and both destroyed with it, so a memory test cannot
        read what an earlier memory test stored — which is the whole point,
        given that these tests assert on what the agent remembered.
        """
        network = self.ensure_network()
        container = f"{self.name}-qdrant"
        if container not in self.containers:
            _docker(
                "run", "--detach", "--rm",
                "--name", container,
                "--label", f"{STACK_LABEL}={self.name}",
                "--network", network,
                "--volume", f"{self.volume('qdrant')}:/qdrant/storage",
                QDRANT_IMAGE,
                container=container,
            )  # fmt: skip
            self.containers.append(container)
            self._wait_for_memory(container)
        return f"http://{container}:6333"

    def restart_memory(self) -> None:
        """Stop the store and bring it back on the same volume.

        Not a convenience: it is the difference between memory that persists
        and memory that has merely not been interrupted. The container is
        removed rather than restarted because it runs with ``--rm``, so a
        stop and a start race with Docker's own cleanup; a new container on
        the same volume is the same thing without the race.
        """
        container = f"{self.name}-qdrant"
        if container not in self.containers:
            return
        _docker("rm", "--force", container, check=False)
        self.containers.remove(container)
        self.with_memory()

    def _wait_for_memory(self, container: str) -> None:
        """Block until the store answers, because a late start looks like no store.

        The harness is built to carry on when memory is unreachable
        (ADR-069), which is right in production and treacherous here: a test
        that raced the database would not fail, it would quietly assert
        nothing and pass. So readiness is waited for rather than assumed.

        Probed from inside the network using the agent image, which is
        already local and already has Python — the Qdrant image has no shell
        tools worth relying on.
        """
        script = (
            "import sys,time,urllib.request\n"
            "for _ in range(60):\n"
            "    try:\n"
            "        urllib.request.urlopen(sys.argv[1], timeout=2).read()\n"
            "        sys.exit(0)\n"
            "    except Exception:\n"
            "        time.sleep(1)\n"
            "sys.exit(1)\n"
        )
        prober = f"{self.name}-probe-{uuid.uuid4().hex[:6]}"
        probe = _docker(
            "run", "--rm",
            "--name", prober,
            "--label", f"{STACK_LABEL}={self.name}",
            "--network", str(self.network),
            "--entrypoint", "python",
            self.image,
            "-c", script, f"http://{container}:6333/readyz",
            check=False,
            container=prober,
        )  # fmt: skip
        if probe.returncode != 0:
            raise AssertionError(
                f"the throwaway Qdrant never became ready: {probe.stderr.strip()}"
            )

    def destroy(self) -> None:
        """Remove everything this stack created, best effort, in order.

        Containers first, then the network they were attached to, then the
        volumes — a volume still held by a container refuses to go. Failures
        are swallowed on purpose: teardown runs after a failed assertion too,
        and a cleanup error there would replace a real diagnosis with a
        docker message.
        """
        # By label first, then by the names we remember. The label catches
        # what `--rm` does not: a container killed by Ctrl-C, or left behind
        # by an interrupted run, which would otherwise keep its volume pinned
        # and make the removals below fail (ADR-080).
        listed = _docker(
            "ps", "--all", "--quiet", "--filter", f"label={STACK_LABEL}={self.name}",
            check=False,
        )  # fmt: skip
        for found in listed.stdout.split():
            _docker("rm", "--force", found, check=False)
        for container in self.containers:
            _docker("rm", "--force", container, check=False)
        if self.network is not None:
            _docker("network", "rm", self.network, check=False)
        for volume in self.volumes.values():
            _docker("volume", "rm", "--force", volume, check=False)

    # -- using it ----------------------------------------------------------

    def run(
        self,
        task: str,
        *,
        permissions: str | None = None,
        memory: bool = False,
    ) -> Run:
        """Give the agent one task in the container, and return what happened.

        Args:
            task: Plain English, exactly as a user would type it. Never a
                tool name and never a hint about which tool to use — what is
                under test includes whether the model can work that out.
            permissions: ``--permissions`` for this run, or ``None`` for the
                configured default.
            memory: Start a Qdrant for this run and point the agent at it.

        Returns:
            The finished :class:`Run`.
        """
        # A name so the timeout path has something to remove, and a label so
        # teardown can sweep anything a Ctrl-C left behind — `--rm` only
        # cleans up a container that exits on its own (ADR-080).
        agent = f"{self.name}-agent-{uuid.uuid4().hex[:6]}"
        command = [
            "run", "--rm",
            "--name", agent,
            "--label", f"{STACK_LABEL}={self.name}",
            "--env-file", str(ENV_FILE),
            "--volume", f"{self.volume('workspace')}:{WORKSPACE}",
            "--volume", f"{self.volume('memory')}:{MEMORY}",
            "--volume", f"{self.volume('calendar')}:{CALENDAR}",
        ]  # fmt: skip
        if memory:
            url = self.with_memory()
            command += [
                "--network",
                str(self.network),
                "--env",
                f"MEMORY_QDRANT_URL={url}",
            ]
        for key, value in self.extra_env.items():
            command += ["--env", f"{key}={value}"]
        command.append(self.image)
        if permissions is not None:
            command += ["--permissions", permissions]
        command.append(task)

        finished = _docker(
            *command, check=False, timeout=RUN_TIMEOUT_SECONDS, container=agent
        )
        return Run(
            task=task,
            exit_code=finished.returncode,
            stdout=finished.stdout,
            stderr=finished.stderr,
        )

    @contextlib.contextmanager
    def session(
        self,
        *,
        permissions: str | None = None,
        env: dict[str, str] | None = None,
    ) -> Iterator[Session]:
        """Hold a REPL open in the container, and close it however the test ends.

        The interactive mode rather than one-shot, because two things can
        only be observed while the process is alive: the ``/metrics``
        endpoint, and the banner. Both are things a person sees during the
        demo, so both are worth an end-to-end test rather than an
        integration one.

        The container always joins this stack's network, whether or not the
        test intends to scrape. It costs nothing — the network already
        exists for memory tests — and it is what makes
        :meth:`Session.scrape_until` work from a sibling container instead
        of a published port, which is the only form that works when the gate
        itself is running inside a container (ADR-081).

        Args:
            permissions: ``--permissions`` for this session, or ``None`` for
                the configured default.
            env: Extra environment for this session, on top of
                ``UNATTENDED_ENV``. This is how a test switches on something
                that ships off, such as skill-scoped permissions.

        Yields:
            The live :class:`Session`.
        """
        name = f"{self.name}-repl-{uuid.uuid4().hex[:6]}"
        network = self.ensure_network()
        command = [
            "docker", "run", "--rm", "--interactive",
            "--name", name,
            "--label", f"{STACK_LABEL}={self.name}",
            "--network", network,
            "--env-file", str(ENV_FILE),
            "--volume", f"{self.volume('workspace')}:{WORKSPACE}",
            "--volume", f"{self.volume('memory')}:{MEMORY}",
            "--volume", f"{self.volume('calendar')}:{CALENDAR}",
        ]  # fmt: skip
        for key, value in {**self.extra_env, **(env or {})}.items():
            command += ["--env", f"{key}={value}"]
        command.append(self.image)
        if permissions is not None:
            command += ["--permissions", permissions]

        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        live = Session(
            container=name,
            network=network,
            image=self.image,
            stack=self.name,
            process=process,
        )
        # Drained on a thread, and this is not optional: the REPL prints a
        # trace line per tool call, and a full pipe would block the agent
        # mid-run. The test would then see a hang and blame the model.
        reader = threading.Thread(
            target=_drain, args=(process.stdout, live.chunks), daemon=True
        )
        reader.start()
        try:
            yield live
        finally:
            live.close()

    # -- looking at the result --------------------------------------------

    def read(self, path: str) -> str:
        """Return the text of one file in a volume, or fail saying it is absent.

        Args:
            path: Absolute path inside the container, e.g.
                ``/app/workspace/output/numbers.md``.
        """
        found = self._inspect(path)
        if found["kind"] != "file":
            pytest.fail(
                f"{path} is not a file (it is {found['kind']}); tree:\n{self.tree()}"
            )
        return str(found["text"])

    def exists(self, path: str) -> bool:
        """Whether anything exists at ``path`` inside the volumes."""
        return bool(self._inspect(path)["kind"] != "missing")

    def is_dir(self, path: str) -> bool:
        """Whether ``path`` is a directory inside the volumes."""
        return bool(self._inspect(path)["kind"] == "directory")

    def listing(self, root: str = WORKSPACE) -> list[str]:
        """Every path under ``root`` as a list — the machine-readable :meth:`tree`.

        :meth:`tree` formats for a failure message and says ``(empty)`` when
        there is nothing there, which is helpful to read and awkward to
        assert on. This returns ``[]`` for the same case.
        """
        script = (
            "import json,os,sys\n"
            "out=[]\n"
            "for base,dirs,files in os.walk(sys.argv[1]):\n"
            "    for name in sorted(dirs): out.append(os.path.join(base,name)+'/')\n"
            "    for name in sorted(files): out.append(os.path.join(base,name))\n"
            "print(json.dumps(sorted(out)))\n"
        )
        paths: list[str] = json.loads(self._python(script, root).stdout)
        return paths

    def find_text(self, root: str, needle: str) -> list[str]:
        """Every file under ``root`` whose *contents* contain ``needle``.

        :meth:`tree` lists paths, which answers "did a file appear"; this
        answers "did this text get written anywhere", which is the question
        the containment tests actually ask. A model told to plant something
        outside its sandbox will pick its own filename, so searching for the
        payload is the only assertion that holds whatever it names the file.
        """
        script = (
            "import json,os,sys\n"
            "root, needle = sys.argv[1], sys.argv[2]\n"
            "hits=[]\n"
            "for base,_dirs,files in os.walk(root):\n"
            "    for name in files:\n"
            "        full=os.path.join(base,name)\n"
            "        try:\n"
            "            body=open(full, encoding='utf-8', errors='replace').read()\n"
            "        except OSError:\n"
            "            continue\n"
            "        if needle.lower() in body.lower(): hits.append(full)\n"
            "print(json.dumps(sorted(hits)))\n"
        )
        found: list[str] = json.loads(self._python(script, root, needle).stdout)
        return found

    def write(self, path: str, content: str) -> None:
        """Pre-populate a file before the agent runs.

        The brief's ``find_and_inspect`` case says "pre-populate the
        following tree before running the test", and this is that. Written
        through a container so the file lands with the volume's ownership,
        exactly as one the agent wrote itself would.
        """
        script = (
            "import os,sys,pathlib\n"
            "p=pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True, exist_ok=True)\n"
            "p.write_text(sys.argv[2], encoding='utf-8')\n"
        )
        self._python(script, path, content)

    def mkdir(self, path: str) -> None:
        """Pre-create an empty directory before the agent runs."""
        script = (
            "import sys,pathlib\n"
            "pathlib.Path(sys.argv[1]).mkdir(parents=True, exist_ok=True)\n"
        )
        self._python(script, path)

    def tree(self, root: str = WORKSPACE) -> str:
        """Every path under ``root``, one per line — for failure messages.

        A real model puts files where it thinks they go, so an assertion that
        fails needs to say what *is* there. Without this a failure reads
        "assert False" and tells the reader nothing.
        """
        script = (
            "import os,sys\n"
            "out=[]\n"
            "for base,dirs,files in os.walk(sys.argv[1]):\n"
            "    for name in sorted(dirs): out.append(os.path.join(base,name)+'/')\n"
            "    for name in sorted(files): out.append(os.path.join(base,name))\n"
            "print('\\n'.join(sorted(out)))\n"
        )
        return self._python(script, root).stdout.strip() or "(empty)"

    # -- the plumbing under those ------------------------------------------

    def _inspect(self, path: str) -> dict[str, object]:
        """Return ``{"kind": ..., "text": ...}`` for one path in the volumes."""
        script = (
            "import json,sys,pathlib\n"
            "p=pathlib.Path(sys.argv[1])\n"
            "kind='missing'\n"
            "text=''\n"
            "if p.is_dir(): kind='directory'\n"
            "elif p.is_file():\n"
            "    kind='file'\n"
            "    text=p.read_text(encoding='utf-8', errors='replace')\n"
            "print(json.dumps({'kind': kind, 'text': text}))\n"
        )
        result = self._python(script, path)
        parsed: dict[str, object] = json.loads(result.stdout)
        return parsed

    def _python(self, script: str, *args: str) -> subprocess.CompletedProcess[str]:
        """Run a short Python program against this stack's volumes.

        The agent image is reused rather than pulling a second one: it
        already has Python, it is already local, and mounting the volumes at
        their real paths keeps the ownership right.
        """
        reader = f"{self.name}-read-{uuid.uuid4().hex[:6]}"
        command = [
            "run", "--rm",
            "--name", reader,
            "--label", f"{STACK_LABEL}={self.name}",
            "--volume", f"{self.volume('workspace')}:{WORKSPACE}",
            "--volume", f"{self.volume('memory')}:{MEMORY}",
            "--volume", f"{self.volume('calendar')}:{CALENDAR}",
            "--entrypoint", "python",
            self.image,
            "-c", script, *args,
        ]  # fmt: skip
        return _docker(*command, container=reader)


def _drain(stream: "IO[str] | None", into: list[str]) -> None:
    """Copy a live stream into a list, in chunks, until it closes.

    Chunks rather than lines, and the difference is not cosmetic: the REPL's
    prompt is ``input("You: ")``, which writes no newline. Iterating the
    stream by line would hold that prompt inside the iterator until something
    *else* completed the line, so a test waiting for the prompt would be
    waiting for output that had already been written.

    The image sets ``PYTHONUNBUFFERED=1``, so a one-character read returns as
    soon as the agent writes anything. A blocking read on a thread is what
    keeps the pipe drained; a full pipe would stop the agent mid-run and the
    test would see a hang and blame the model.
    """
    if stream is None:
        return
    with contextlib.suppress(ValueError, OSError):
        while True:
            chunk = stream.read(1)
            if not chunk:
                return
            into.append(chunk)


def _docker(
    *args: str,
    check: bool = True,
    timeout: int = DOCKER_TIMEOUT_SECONDS,
    container: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one ``docker`` command, with the failure made readable.

    Args:
        args: Arguments after ``docker``.
        check: Raise on a non-zero exit. ``False`` for teardown and for the
            agent run itself, whose exit code is something a test asserts on.
        timeout: Seconds before the call is abandoned.
        container: The ``--name`` this command starts, when it starts one.
            Required for anything that runs a container, and the reason is
            not obvious — see below.

    Raises:
        AssertionError: On a non-zero exit when ``check``, or when the
            command outlived ``timeout``.
    """
    process = subprocess.Popen(
        ["docker", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # This branch exists because of a real twenty-minute hang, and the
        # mechanism is worth writing down.
        #
        # `subprocess.run(timeout=...)` kills the process it started — here
        # the docker *client*. It does not stop the container, because the
        # container is a child of the daemon, not of the client. The
        # container therefore keeps the stdout and stderr pipes open, and
        # Python's own drain-after-kill then blocks on a read that will never
        # end. A 900-second ceiling became an indefinite wait, and a test
        # that should have failed in fifteen minutes sat there instead.
        #
        # So: remove the container *first*. That closes the pipes, and the
        # drain below returns at once (ADR-080).
        if container is not None:
            _docker("rm", "--force", container, check=False, timeout=30)
        process.kill()
        out, err = process.communicate()
        raise AssertionError(
            f"docker {' '.join(args[:3])} did not finish within {timeout}s "
            f"and was killed.\n--- stdout so far ---\n{out}\n"
            f"--- stderr ---\n{err}"
        ) from None

    finished = subprocess.CompletedProcess(list(args), process.returncode, out, err)
    if check and finished.returncode != 0:
        raise AssertionError(
            f"docker {' '.join(args[:3])} failed ({finished.returncode}): "
            f"{finished.stderr.strip()}"
        )
    return finished


@pytest.fixture
def stack(e2e_image: str) -> Iterator[Stack]:
    """One throwaway container world per test, destroyed however the test ends.

    ``yield`` inside no try/finally on purpose: pytest runs the teardown half
    of a yield fixture even when the test fails or errors, which is the
    guarantee that matters here — a failed assertion must not leave a volume
    behind.
    """
    built = Stack(image=e2e_image, name=f"agent-harness-e2e-{uuid.uuid4().hex[:10]}")
    yield built
    built.destroy()


@pytest.fixture
def agent(stack: Stack) -> Callable[..., Run]:
    """Shorthand for ``stack.run`` — most tests want nothing else."""
    return stack.run
