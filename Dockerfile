# Base image is fixed by the brief (Section 7.3): python:3.12-slim-bookworm.
# Pinning to the Debian codename rather than "3.12-slim" keeps the OS stable
# across rebuilds, which is the reproducibility half of why we containerise.
FROM python:3.12-slim-bookworm

# PYTHONDONTWRITEBYTECODE: no .pyc files in the image or the mounted volume.
# PYTHONUNBUFFERED: the REPL prompt and the tool trace must appear
#   immediately; without this, Python block-buffers stdout when it is not a
#   TTY and a piped one-shot run looks like it has hung.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies are copied and installed before the source so that editing a
# .py file does not invalidate the pip layer. Runtime requirements only —
# ruff, mypy and pytest are not shipped to the TAs (AGENTS.md §3).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the local embedding model into the image.
#
# This is the difference between a container that starts and one that waits
# on huggingface.co in front of an audience. Downloading it at *run* time
# would mean a first run needs network egress the graded `docker run` has no
# reason to have, would repeat on every fresh container, and would fail
# closed — the harness falls back to the lexical embedder, which is the
# silent degradation this whole change exists to remove.
#
# Pulled by the same function the harness uses, so a build that produces an
# image has already proved the model loads. A failure here fails the build,
# which is where an unavailable model should be discovered.
ENV FASTEMBED_CACHE_PATH=/opt/models
RUN python -c "\
from fastembed import TextEmbedding; \
m = TextEmbedding(model_name='sentence-transformers/all-MiniLM-L6-v2', \
cache_dir='/opt/models'); \
print('embedding model cached, dims =', len(next(iter(m.embed(['ping'])))))" \
    && chmod -R a+rX /opt/models

# Application code and the non-secret defaults. The .env file is explicitly
# NOT copied: secrets enter at runtime via --env-file, and .dockerignore
# blocks it from the build context so it cannot be added by accident.
COPY harness ./harness
COPY config.yaml .

# The MCP server the harness launches. It ships *inside* this image on
# purpose: config.yaml starts it as a stdio subprocess, so a bare
# `docker run` has the remote tools too. A separate compose service would
# have meant the graded single-command path had no MCP tools at all
# (ADR-046).
COPY mcp_server_time_plus ./mcp_server_time_plus

# The web research server. Ships in the same image and for the same
# reason: config.yaml launches it as a stdio subprocess, so a bare
# `docker run` has web_search and web_fetch too (ADR-046, ADR-047).
COPY mcp_server_web_search ./mcp_server_web_search

# The calendar server: a fork of adamchrabaszcz/ics-calendar-mcp (MIT).
# Ships in the same image for the same reason as the other two, and needs no
# credentials to start because the fork reads an iCalendar file on a volume
# rather than fetching upstream's ICS_CALENDAR_URL — a private feed URL is a
# secret, and §8.2 forbids committing those (ADR-071).
COPY mcp_server_calendar ./mcp_server_calendar

# Skills ship *in the image*, not on a volume. They are instructions the
# agent loads into its own prompt, so a writable skills directory would be a
# persistent injection channel: text written during one run would steer
# every later run (ADR-038).
COPY skills ./skills

# Create the two writable roots inside the image and hand them to a non-root
# user; make the third root read-only even to that user.
#
# Every part of this matters. Baking the folders in means a bare `docker run`
# with no volumes works out of the box. Running as a non-root user means that
# if the agent is ever talked into writing somewhere it should not, it is
# writing as an unprivileged account — a second, OS-level line of defence
# behind the in-process path confinement in harness/tools/paths.py. And
# stripping write permission from /app/skills makes "skills are read-only"
# an OS guarantee rather than a consequence of us not having written a
# write tool.
#
# There is no Dockerfile VOLUME instruction. VOLUME would attach an
# anonymous volume whenever `-v` was omitted, which survives `--rm` as a
# nameless hash and piles up; persistence is an explicit named-volume flag
# instead (ADR-054).
RUN useradd --create-home --uid 1000 agent \
    && mkdir -p /app/workspace /app/memory /app/calendar \
    && chown -R agent:agent /app \
    && chown -R root:root /app/skills \
    && chmod -R a-w /app/skills
USER agent

# ENTRYPOINT, not CMD, and the difference is not cosmetic. With `CMD` alone,
# `docker run <image> "some task"` does not pass the task to the harness — it
# *replaces* the command, and Docker tries to execute a program called
# "some task". This shipped as `CMD` for a week and looked fine, because the
# only caller that exercises the one-shot form is the `container` CI job, and
# that job skips itself on the FIM runner for want of a privileged Docker.
# The end-to-end tier runs the one-shot form on every test, which is how it
# surfaced (ADR-076).
#
# With ENTRYPOINT and no CMD, both documented forms work and mean what the
# README says they mean:
#   docker run <image>                  -> python -m harness          (REPL)
#   docker run <image> "some task"      -> python -m harness "…"      (one shot)
#   docker run <image> --permissions …  -> flags reach argparse too
# `--entrypoint` still overrides it for the rare case that wants a shell.
ENTRYPOINT ["python", "-m", "harness"]
