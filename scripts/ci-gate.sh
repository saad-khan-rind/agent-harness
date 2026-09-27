#!/bin/sh
# The quality gate, in one place.
#
# Used by three callers that must never disagree:
#   * a developer:  ./scripts/ci-gate.sh
#   * the test image: Dockerfile.test's default CMD
#   * GitHub CI:    .github/workflows/ci.yml
#
# Keeping it as one script is the point. When the gate lived as four commands
# copy-pasted into the README, the Dockerfile and the pipeline, they drifted:
# CI would pass while a developer's machine failed, or the reverse.
#
# `set -e` so the first failing check stops the run with a non-zero exit
# status — that is what makes the pipeline red and what makes
# `docker run ... agent-harness-tests` usable in a script.
set -e

echo "==> ruff format (style)"
ruff format --check .

echo
echo "==> ruff check (lint)"
ruff check .

echo
echo "==> mypy (types, strict)"
mypy harness mcp_server_time_plus mcp_server_web_search mcp_server_calendar

echo
echo "==> pytest (unit + integration, with coverage)"
# Coverage flags and the fail_under threshold live in pyproject.toml, so
# they apply identically however pytest is invoked. Any extra arguments
# passed to this script are forwarded, which is what lets CI add
# --junitxml without duplicating the rest of the configuration.
#
# `addopts` carries -m "not e2e", so this is the unit and integration tiers.
# The end-to-end tier is handled below, on its own terms (ADR-074).
pytest "$@"

# The end-to-end tier: run it when it can actually run, say why when it
# cannot. Never silently absent — a gate that quietly drops a whole tier is
# how ADR-076's entry-point bug survived a week.
#
# Two conditions, checked in this order because the second is the slower one:
#   * .env.test exists (a real InnKube token)
#   * the Docker daemon answers
#
# AGENT_HARNESS_E2E_CONTEXT is set by the `tests` compose service, which mounts the
# real repository at /host; unset on a developer's machine, where the root is
# this script's parent.
echo
E2E_ROOT="${AGENT_HARNESS_E2E_CONTEXT:-$(cd "$(dirname "$0")/.." && pwd)}"
if [ ! -f "$E2E_ROOT/.env.test" ]; then
    echo "==> pytest (end-to-end): SKIPPED — no $E2E_ROOT/.env.test"
    echo "    The tier runs the shipped image against the real model, so it"
    echo "    needs a token. To enable it:  cp .env.example .env.test"
elif ! docker info >/dev/null 2>&1 && ! podman info >/dev/null 2>&1; then
    echo "==> pytest (end-to-end): SKIPPED — no reachable container engine"
    echo "    .env.test is present, so this is the only thing missing. Inside"
    echo "    the gate container it means the socket was not mounted or the"
    echo "    agent user cannot open it — see group_add in compose.yaml."
else
    echo "==> pytest (end-to-end: real model, throwaway volumes)"
    echo "    Each test starts a container and waits on a real model, so"
    echo "    expect a few minutes. Watch one in flight from another shell:"
    echo "      docker logs -f \$(docker ps -q --filter name=agent-harness-e2e | head -1)"
    echo
    # --no-cov: the code under test runs in another container, so this
    # process would measure nothing and then fail the 85% floor for it.
    # -p no:randomly: these cost real tokens, so a repeatable order is worth
    # more than shuffling when a rerun is expensive.
    # -v: names each test as it *starts*. The default quiet dots are printed
    # when a test finishes, so a slow test and a hung one look identical —
    # which is exactly the confusion this tier produces on a first run.
    # --durations: says afterwards which ones were worth the wait.
    pytest tests/e2e -m e2e --no-cov -p no:randomly -v --durations=10
fi

echo
echo "==> gate passed"
