#!/usr/bin/env bash
#
# The entry point, at the root where it is findable: `./run.sh`.
#
# Everything lives in scripts/run.sh — this forwards to it unchanged, so
# there is one script to read and one to keep correct. Run `./run.sh --help`
# for the full list, or read "Run it" at the top of README.md.
exec "$(dirname "${BASH_SOURCE[0]}")/scripts/run.sh" "$@"
