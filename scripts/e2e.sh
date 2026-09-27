#!/bin/sh
# The end-to-end tier on its own: the shipped image, the real model,
# throwaway volumes (ADR-074, ADR-075).
#
#   ./scripts/e2e.sh                                    # all of it
#   ./scripts/e2e.sh tests/e2e/test_agent_calendar.py   # one file
#   ./scripts/e2e.sh -k weekday                         # one test
#
# Use this when you want the tier *without* waiting for lint, types and
# 1,022 other tests first. `docker compose run --build --rm tests` runs
# everything including this; the two share one code path and cannot disagree.
#
# ---------------------------------------------------------------------------
# Why this runs through compose instead of calling pytest directly.
#
# It used to call pytest on the host and died with `pytest: command not
# found`, because nothing in this project asks you to install a Python
# toolchain on your laptop — that is the whole point of `Dockerfile.test`.
#
# And the tier needs three things that only the `tests` compose service
# supplies, which is why `docker run agent-harness-tests pytest ...` skips
# every test instead of running them:
#
#   * the host Docker socket, so the tests can start the agent's container
#   * the repository mounted read-only at /host, which is the build context
#     and where .env.test is read from (AGENT_HARNESS_E2E_CONTEXT)
#   * group_add, so the non-root `agent` user can open that socket
#
# `docker run` on the same image supplies none of them. `docker compose run`
# does, because compose.yaml says so.
# ---------------------------------------------------------------------------
#
# What it needs from you:
#   * Docker, running
#   * .env.test with a real LLM_API_KEY  (cp .env.example .env.test)
set -e

cd "$(dirname "$0")/.."

# Docker or Podman, resolved by the same file scripts/run.sh uses so the two
# cannot disagree about what "podman" means. Forced with --engine, or with
# AGENT_HARNESS_ENGINE for a CI job that has no argv to spare (ADR-104).
# shellcheck source=scripts/engine.sh
. "$(dirname "$0")/engine.sh"

WANTED="${AGENT_HARNESS_ENGINE:-}"
if [ "${1:-}" = "--engine" ]; then
    WANTED="$2"
    shift 2
fi
resolve_engine "$WANTED"
require_engine_running

# Which socket the `tests` container is handed. compose.yaml defaults to
# /var/run/docker.sock, and the path is resolved on the machine that runs the
# containers. On a Mac that is the engine's VM, where the path is the engine's
# own socket for Docker Desktop and for `podman machine` alike. On Linux it is
# this host: rootless Podman has no such file, and a host with both engines
# has Docker's socket there — so the tests would drive the wrong engine and
# not find the image built above. Ask Podman where its socket is instead.
if [ "$ENGINE" = podman ] && [ -z "${ENGINE_SOCKET:-}" ] && [ "$(uname -s)" = Linux ]; then
    socket="$(podman info --format '{{.Host.RemoteSocket.Path}}' 2>/dev/null)"
    socket="${socket#unix://}"
    if [ -S "$socket" ]; then
        export ENGINE_SOCKET="$socket"
    else
        echo "Podman's API socket is not running. Start it and try again:" >&2
        echo "    systemctl --user start podman.socket   # rootless" >&2
        echo "    sudo systemctl start podman.socket     # rootful" >&2
        exit 1
    fi
fi

if [ ! -f .env.test ]; then
    echo "no .env.test — copy .env.example and put the token in it:" >&2
    echo "    cp .env.example .env.test" >&2
    exit 1
fi

# Built here rather than inside the pytest session fixture so the progress is
# visible: a silent three-minute pause at the start of a test run looks like a
# hang. The tag is deliberately not `agent-harness`, so running the tests can
# never overwrite the image someone has a demo running on.
echo "==> $ENGINE build (agent-harness-e2e)"
"$ENGINE" build -t agent-harness-e2e .

echo
echo "==> pytest tests/e2e"
echo "    Each test starts a container and waits on a real model, so expect"
echo "    a few minutes. Watch one in flight from another shell:"
echo "      $ENGINE logs -f \$($ENGINE ps -q --filter name=agent-harness-e2e | head -1)"
echo
# --no-cov: the code under test runs in *another* container, so this process
#   would measure nothing and then fail the 85% coverage floor for it.
# -p no:randomly: these cost real tokens, so a repeatable order beats
#   shuffling when a rerun is expensive.
# -v: names each test as it starts. The default quiet dots only appear when a
#   test *finishes*, which makes a slow test and a hung one look identical.
# -e AGENT_HARNESS_E2E_IMAGE: tells the fixture the build above already happened.
"${COMPOSE[@]}" run --build --rm \
    -e AGENT_HARNESS_E2E_IMAGE=agent-harness-e2e \
    tests \
    pytest tests/e2e -m e2e --no-cov -p no:randomly -v --durations=10 "$@"

echo
echo "==> end-to-end passed"
