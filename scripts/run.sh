#!/usr/bin/env bash
#
# One command to run the agent with everything switched on, on Docker or
# Podman. The engine is detected; nothing else about the invocation changes.
#
#   ./run.sh                          the agent, with memory, metrics and dashboards
#   ./run.sh --tracing                …and Jaeger, for reading one run as a flame graph
#   ./run.sh "list the files in my workspace"
#   ./run.sh --stop                   stop the supporting services
#
#   ./run.sh --permissions allow_all           run every tool, no prompts
#   ./run.sh --permissions confirm_mutations   ask before writes and web reads
#   ./run.sh --permissions deny_mutations      read-only session
#   ./run.sh --allow-all | --confirm | --read-only     the same, shorter
#
#   ./run.sh --engine podman          force Podman even if Docker is installed
#   ./run.sh --engine docker          force Docker even if Podman is installed
#
#   Without a flag the mode is whatever config.yaml says (`auto`: confirm on a
#   terminal, allow_all otherwise — ADR-102).
#
# This is a convenience wrapper and nothing may become load-bearing on it.
# The brief requires the plain `docker run` / `podman run` path to work by
# itself, and it does — see README.md. What this removes is the things that
# are easy to get wrong and silently produce a half-working demo (ADR-098):
#
#   1. Forgetting `--service-ports`, which leaves Grafana permanently empty
#      because Prometheus has nothing to scrape (ADR-092).
#   2. Forgetting to rebuild, so a code change never reaches the container
#      you are looking at.
#   3. Starting the agent before Qdrant, so the banner says memory is
#      unavailable and `/remember` is refused (ADR-069).
#   4. On Podman with SELinux, a bind mount the container cannot read —
#      which looks exactly like a missing config file (ADR-104).

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

readonly RESET=$'\033[0m' BOLD=$'\033[1m' DIM=$'\033[2m'
readonly GREEN=$'\033[32m' YELLOW=$'\033[33m' RED=$'\033[31m'

note() { printf '%s==>%s %s\n' "$GREEN$BOLD" "$RESET" "$*"; }
warn() { printf '%s==>%s %s\n' "$YELLOW$BOLD" "$RESET" "$*" >&2; }
die() {
  printf '%s==>%s %s\n' "$RED$BOLD" "$RESET" "$*" >&2
  exit 1
}

# --- what the user asked for -------------------------------------------
TRACING=0
STOP=0
PERMISSIONS=""
ENGINE_WANTED=""
TASK=()

# The modes the harness accepts, checked here so a typo fails before the
# supporting services are started rather than after.
MODES="allow_all confirm_mutations deny_mutations auto"

set_permissions() {
  case " $MODES " in
    *" $1 "*) PERMISSIONS="$1" ;;
    *) die "unknown permission mode '$1'. One of: $MODES" ;;
  esac
}

set_engine() {
  case "$1" in
    docker | podman) ENGINE_WANTED="$1" ;;
    *) die "unknown engine '$1'. One of: docker podman" ;;
  esac
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tracing) TRACING=1 ;;
    --stop | --down) STOP=1 ;;
    --permissions | -p)
      [[ $# -ge 2 ]] || die "--permissions needs a mode. One of: $MODES"
      set_permissions "$2"
      shift
      ;;
    --permissions=*) set_permissions "${1#--permissions=}" ;;
    --allow-all) set_permissions allow_all ;;
    --confirm) set_permissions confirm_mutations ;;
    --read-only) set_permissions deny_mutations ;;
    --engine | -e)
      [[ $# -ge 2 ]] || die "--engine needs a name. One of: docker podman"
      set_engine "$2"
      shift
      ;;
    --engine=*) set_engine "${1#--engine=}" ;;
    --podman) set_engine podman ;;
    --docker) set_engine docker ;;
    -h | --help)
      sed -n '3,34p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) TASK+=("$1") ;;
  esac
  shift
done

# --- which engine, and how it spells "compose" -------------------------
# One resolver, shared with scripts/e2e.sh, so the two cannot disagree about
# what "podman" means. It sets ENGINE, COMPOSE, COMPOSE_HAS_WAIT and exports
# SELINUX_SUFFIX for compose.yaml (ADR-104).
# shellcheck source=scripts/engine.sh
. "$(dirname "${BASH_SOURCE[0]}")/engine.sh"

resolve_engine "$ENGINE_WANTED"
require_engine_running

if [[ $STOP -eq 1 ]]; then
  note "Stopping the supporting services… ($ENGINE)"
  # No -v. That flag deletes the named volumes, which is the agent's
  # workspace and its long-term memory — never something a stop command
  # should do on its own (ADR-054).
  "${COMPOSE[@]}" --profile tracing down
  note "Stopped. Your workspace and memory volumes are untouched."
  exit 0
fi

# --- the other engine --------------------------------------------------
# A machine with both engines can have this stack up on one of them while
# the user asks for the other. Both publish the same host ports, so the
# second start failed half-way with "bind: address already in use" on 6333,
# after a long build, and said nothing about why. Checked by container name,
# which both engines report the same way and which the compose project name
# pins (ADR-054). The e2e and test containers publish nothing, so only the
# services are looked for.
if [[ $ENGINE == docker ]]; then OTHER=podman; else OTHER=docker; fi
if command -v "$OTHER" >/dev/null 2>&1 && "$OTHER" info >/dev/null 2>&1; then
  if "$OTHER" ps --format '{{.Names}}' 2>/dev/null |
    grep -qE '^agent-harness-(qdrant|prometheus|grafana|jaeger|harness-run)'; then
    die "the supporting services are already running on $OTHER, and they hold
  the same ports ($ENGINE cannot publish 3000, 6333, 9090 or 9091 while they
  do). Stop them first — your workspace and memory are kept:

    ./run.sh --stop --engine $OTHER"
  fi
fi

# --- the token ---------------------------------------------------------
if [[ ! -f .env ]]; then
  cp .env.example .env
  warn "Created .env from .env.example."
  die "Choose a provider and put its API key in it first:
  LLM_PROVIDER=gemini   # or innkube, openai, groq, openrouter, mistral, ollama
  LLM_API_KEY=..."
fi

if grep -qE '^LLM_API_KEY=(your_api_key_here)?$' .env; then
  die ".env still has the placeholder token. Set LLM_API_KEY=... in .env"
fi

# --- the supporting services -------------------------------------------
# Every service without a profile: Qdrant for long-term memory, Prometheus
# for the metrics it scrapes, Grafana for the dashboard. The agent itself is
# behind the `cli` profile because it is an interactive REPL.
SERVICES=(qdrant prometheus grafana)
if [[ $TRACING -eq 1 ]]; then
  SERVICES+=(jaeger)
  export OTLP_ENABLED=true
  export OTLP_ENDPOINT=http://jaeger:4318
fi

note "Starting ${SERVICES[*]}… ($ENGINE)"
# --wait is Compose v2 and podman-compose does not implement it. Detected
# rather than assumed: passing it to an implementation that does not know it
# fails the whole command, and the services are not what the run depends on
# anyway — a missing Qdrant degrades memory rather than failing the run.
up=("${COMPOSE[@]}" --profile tracing up -d)
if [[ $COMPOSE_HAS_WAIT -eq 1 ]]; then
  up+=(--wait --wait-timeout 90)
fi
"${up[@]}" "${SERVICES[@]}" ||
  warn "Some services did not report healthy. The agent runs anyway — a
  missing Qdrant degrades memory rather than failing the run (ADR-069)."


# --- the image ---------------------------------------------------------
# Built with the engine directly rather than by `compose run --build`, which
# is what ADR-098 used to rely on. Same guarantee — a code change reaches the
# container you are about to look at — from one layer less.
#
# The layer that went: on Podman, `podman compose` shells out to an external
# compose provider (often Docker Desktop's `docker-compose` binary), and a
# build driven through it reports registry failures with no useful
# provenance. `scripts/e2e.sh` already built this way; now both do (ADR-104).
note "Building the image… ($ENGINE)"
if ! "$ENGINE" build -t agent-harness .; then
  if [[ $ENGINE == podman ]]; then
    die "the image did not build.

  If that ended in 'unable to retrieve auth token: invalid username/password',
  it is a credentials problem and not a Docker Hub outage. Podman falls back
  to ~/.docker/config.json, where Docker Desktop leaves a credsStore helper;
  with Desktop stopped the helper returns nothing, so Podman sends empty
  credentials instead of pulling anonymously. 'podman logout --all' does not
  clear it, because that file is not Podman's. Either:

    printf '{}' > /tmp/empty-auth.json
    REGISTRY_AUTH_FILE=/tmp/empty-auth.json ./run.sh --engine podman

  or remove the \"credsStore\" line from ~/.docker/config.json, or start
  Docker Desktop so its helper answers."
  fi
  die "the image did not build. The error is above."
fi

# --- where everything is -----------------------------------------------
# Printed here, after the build, rather than when the services come up. The
# build prints hundreds of lines, and a list printed before it was scrolled
# out of sight by the time anyone looked for it (ADR-111).
printf '\n'
note "Ready. Open these in a browser:"
cat <<'URLS'
  Grafana      http://localhost:3000    (admin / admin, dashboard is the home page)
  Prometheus   http://localhost:9090    (Status -> Targets, and the Alerts tab)
  Metrics      http://localhost:9091/metrics
  Qdrant       http://localhost:6333/dashboard
URLS
if [[ $TRACING -eq 1 ]]; then
  printf '  Jaeger       http://localhost:16686   (each answer prints a link to its trace)\n'
else
  printf '%s  Jaeger       off — start with ./run.sh --tracing%s\n' "$DIM" "$RESET"
fi
printf '\n'

# --- the agent ---------------------------------------------------------
# --service-ports so 9091 is published and Prometheus has something to
# scrape; without it the dashboard stays empty and nothing says why
# (ADR-092). --rm because the state worth keeping is on the volumes, not in
# the container. No --build: the image was just built, above.
if [[ -n $PERMISSIONS ]]; then
  note "Starting the agent with --permissions $PERMISSIONS. /help lists the commands, /exit quits."
else
  note "Starting the agent. /help lists the commands, /exit quits."
fi
printf '\n'
# bash 3.2 (what macOS ships) treats "${empty[@]}" as unbound under `set -u`.
# The documented default is no extra arguments, so TASK is often empty —
# build the argv and only append it when there is something to pass.
agent_cmd=("${COMPOSE[@]}" run --rm --service-ports harness)
if [[ -n $PERMISSIONS ]]; then
  agent_cmd+=(--permissions "$PERMISSIONS")
fi
if [[ ${#TASK[@]} -gt 0 ]]; then
  agent_cmd+=("${TASK[@]}")
fi
exec "${agent_cmd[@]}"
