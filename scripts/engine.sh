#!/usr/bin/env bash
#
# Which container engine, and how it spells "compose". Sourced, never run.
#
#   . "$(dirname "$0")/engine.sh"
#   resolve_engine "$WANTED"        # "", "docker" or "podman"
#   "${COMPOSE[@]}" run --rm tests
#   "$ENGINE" build -t agent-harness .
#
# This file is the whole of the Podman support, and it is one file on
# purpose. The alternative — a second script per engine — drifts: the copy
# somebody is not looking at is the one that stops working, and there is no
# test that can tell you which copy that is (ADR-104).
#
# Three spellings exist and a machine may have more than one:
#
#   docker compose      Compose v2, a Docker CLI plugin
#   podman compose      Podman 4.7+, delegating to whatever it finds
#   podman-compose      the standalone Python implementation, which has no
#                       `--wait` — see COMPOSE_HAS_WAIT below
#
# Sets, for the caller: ENGINE, COMPOSE (an array), COMPOSE_HAS_WAIT (0/1)
# and SELINUX_SUFFIX (exported, read by compose.yaml).
#
# Two things about Podman on a Mac that look like our bugs and are not:
#
#   * `podman compose` prints ">>>> Executing external compose provider" and
#     shells out to whatever compose binary it finds, often Docker Desktop's.
#     That is Podman working as documented, not Docker being used.
#   * A build can fail with `unable to retrieve auth token: invalid
#     username/password`. Podman reads ~/.docker/config.json, and Docker
#     Desktop leaves a `credsStore` helper in it — with Desktop stopped the
#     helper returns nothing, so Podman sends empty credentials to Docker Hub
#     and is refused instead of pulling anonymously. `podman logout --all`
#     does NOT clear it — that file is not Podman's. run.sh catches this at
#     the build step and prints the three ways out.

# --- what the caller gets ----------------------------------------------
ENGINE=""
COMPOSE=()
COMPOSE_HAS_WAIT=1

_engine_die() {
  printf '\033[31m\033[1m==>\033[0m %s\n' "$*" >&2
  exit 1
}

_resolve_docker() {
  command -v docker >/dev/null 2>&1 || return 1
  docker compose version >/dev/null 2>&1 || return 1
  COMPOSE=(docker compose)
  COMPOSE_HAS_WAIT=1
}

_resolve_podman() {
  command -v podman >/dev/null 2>&1 || return 1
  if podman compose version >/dev/null 2>&1; then
    COMPOSE=(podman compose)
    COMPOSE_HAS_WAIT=1
  elif command -v podman-compose >/dev/null 2>&1; then
    COMPOSE=(podman-compose)
    # No `--wait`, and passing it fails the whole command rather than being
    # ignored. The services are not what a run depends on anyway: a missing
    # Qdrant degrades memory rather than failing the run (ADR-069).
    COMPOSE_HAS_WAIT=0
  else
    return 1
  fi
}

# SELinux relabelling for the host binds in compose.yaml. Podman honours the
# label by default on Fedora, RHEL and CentOS, and without `:Z` the container
# is denied a file that is plainly in the checkout — a failure that reads as
# "the config is missing". Docker on those hosts wants the same suffix; on a
# Mac, on Windows and with SELinux disabled it is unnecessary, so it is added
# only when the kernel says it applies.
_resolve_selinux() {
  SELINUX_SUFFIX=""
  if [[ -x /usr/sbin/selinuxenabled ]] && /usr/sbin/selinuxenabled 2>/dev/null; then
    SELINUX_SUFFIX=",Z"
  elif command -v getenforce >/dev/null 2>&1 &&
    [[ "$(getenforce 2>/dev/null || echo Disabled)" != "Disabled" ]]; then
    SELINUX_SUFFIX=",Z"
  fi
  export SELINUX_SUFFIX
}

# Can this engine actually start a container right now?
#
# Installed is not the same as usable, and on a machine with both engines
# that difference decides which one gets picked. `docker info` needs a
# running daemon; podman is daemonless, but the same probe catches a broken
# rootless setup or a stopped `podman machine`.
_engine_running() {
  "$1" info >/dev/null 2>&1
}

# Resolve the engine, optionally forced by name.
#
# Args:
#   $1: "docker", "podman", or empty to take whichever is present.
#
# A name that is asked for is never swapped for the other one: running the
# demo on an engine the user did not choose is worse than saying what is
# missing. Auto-detection is the opposite case and has to be *usable*
# rather than merely installed — a Mac with the Docker CLI on PATH and
# Docker Desktop stopped would otherwise pick Docker and report a dead
# daemon while a perfectly good Podman sat next to it (ADR-104).
resolve_engine() {
  local wanted="${1:-}"
  case "$wanted" in
    docker)
      _resolve_docker || _engine_die "docker is installed but has no working compose.
  It needs the Compose v2 plugin: https://docs.docker.com/compose/install/"
      ENGINE=docker
      ;;
    podman)
      _resolve_podman || _engine_die "podman is installed but has no working compose.
  It needs 'podman compose' (Podman 4.7+) or the podman-compose package."
      ENGINE=podman
      ;;
    "")
      local installed=""
      if _resolve_docker && _engine_running docker; then
        ENGINE=docker
      elif _resolve_podman && _engine_running podman; then
        ENGINE=podman
      else
        # Nothing is both installed and up. Name what is there but stopped,
        # because "no engine found" would be wrong and unhelpful on a
        # machine that has one a click away.
        local found=0
        _resolve_docker && {
          installed="docker"
          found=$((found + 1))
        }
        _resolve_podman && {
          installed="${installed:+$installed and }podman"
          found=$((found + 1))
        }
        if [[ $found -eq 1 ]]; then
          _engine_die "$installed is installed but not running.
  Start it — Docker Desktop, or 'podman machine start' — and try again."
        elif [[ $found -gt 1 ]]; then
          _engine_die "found $installed, but neither is running.
  Start one — Docker Desktop, or 'podman machine start' — and try again."
        fi
        _engine_die "no container engine with compose support found.
  Install Docker Desktop (or the Compose v2 plugin), or Podman 4.7+.
  Everything here also works with the plain run commands in README.md."
      fi
      ;;
    *) _engine_die "unknown engine '$wanted'. One of: docker podman" ;;
  esac
  _resolve_selinux
}

# Check the engine can actually start a container.
#
# `docker info` needs a running daemon; podman is daemonless, but the same
# probe catches a broken rootless setup — no subuid range, no user namespace
# — before anything slow happens.
require_engine_running() {
  "$ENGINE" info >/dev/null 2>&1 && return 0
  if [[ $ENGINE == docker ]]; then
    _engine_die "the Docker daemon is not running. Start Docker Desktop, or:
  sudo systemctl start docker"
  fi
  _engine_die "podman cannot start containers here. Usually a rootless setup
  problem — check 'podman info' and that /etc/subuid has an entry for you."
}
