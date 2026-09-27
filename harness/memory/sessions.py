"""The run log, which stays a file.

Everything else moved into Qdrant. This did not, and the reason is worth
stating because it is the one place the migration is deliberately partial
(ADR-068).

The sessions log is not retrieved by meaning. Nobody asks memory "what did
I do that was semantically similar to deploying"; they ask "what did I run
last Tuesday", which is an ordered scan, not a nearest-neighbour query.
Embedding it would spend an embedding call per run to make a record that
the read path never queries and that would pollute the vector space with
one near-identical point per run — the worst possible neighbours for a
collection whose value is that dissimilar things are far apart.

It is also the artefact a human reads during a demo. ``docker run --rm -v
agent-harness-memory:/m alpine cat /m/sessions.md`` shows what the agent has been
doing; the equivalent for a vector collection is a dashboard and a query.

So: one append-only markdown file on the same volume the memory root always
used, trimmed to a fixed number of newest entries. The code is lifted
unchanged from the file store it used to live in, minus the parts that
served facts and notes.
"""

from datetime import UTC, datetime
from pathlib import Path

SESSIONS_FILENAME = "sessions.md"

_SESSIONS_HEADER = """# Sessions

One line per finished run, appended by the harness.
"""

# Newest entries kept when the log is trimmed. Trimming is harness
# bookkeeping on the harness's own log, not a capability offered to the
# model, so it does not weaken the no-delete rule (ADR-008) any more than
# the startup write probe does.
MAX_SESSION_LINES = 200

# How much of a task or outcome survives into one log line. The log is a
# list of what happened, not a transcript.
_LINE_CLIP = 120


class SessionLog:
    """An append-only markdown log of finished runs."""

    def __init__(self, root: Path, max_lines: int = MAX_SESSION_LINES) -> None:
        """Args:
        root: Memory root. Not created here; ``load_settings`` creates
            and write-probes it at startup so a broken mount fails
            before the first LLM call rather than at the first write.
        max_lines: Newest entries kept.
        """
        self._path = root.resolve() / SESSIONS_FILENAME
        self._max_lines = max_lines

    @property
    def path(self) -> Path:
        """Return the log file's absolute path."""
        return self._path

    def append(self, task: str, outcome: str, iterations: int) -> None:
        """Append one run, then trim. Never raises.

        Called from ``on_run_end``, which the loop invokes in a ``finally``
        block including when the run is already failing. Letting a full
        disk turn a bad run into a crashed run would replace a useful error
        message with a confusing one.
        """
        stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
        line = (
            f"- {stamp} | {_one_line(task, _LINE_CLIP)} | "
            f"{_one_line(outcome, _LINE_CLIP)} | {iterations} iteration(s)\n"
        )
        try:
            existing = self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            existing = _SESSIONS_HEADER
        try:
            self._path.write_text(
                _trim(existing, line, self._max_lines), encoding="utf-8", newline=""
            )
        except OSError:
            return

    def read(self) -> str:
        """Return the log's text, or an empty string.

        Total for the same reason every read on the old store was: a
        missing log is the normal state of a fresh install, and an
        unreadable one must not stop a run from starting.
        """
        try:
            return self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""

    def recent(self, limit: int) -> list[str]:
        """Return the newest entry lines, oldest first.

        Args:
            limit: Most lines to return.

        Returns:
            Entry lines without trailing newlines, or an empty list.
        """
        entries = [line for line in self.read().splitlines() if line.startswith("- ")]
        return entries[-limit:] if limit > 0 else []


def _one_line(text: str, limit: int) -> str:
    """Collapse whitespace and clip, so a log line stays one line."""
    flattened = " ".join(text.split())
    return flattened if len(flattened) <= limit else flattened[:limit] + "…"


def _trim(existing: str, new_line: str, max_lines: int) -> str:
    """Append ``new_line`` and keep only the newest entries.

    The log is unbounded otherwise: it gains a line per run forever, on a
    volume that outlives ``--rm``.
    """
    body = existing if existing.endswith("\n") else existing + "\n"
    lines = (body + new_line).splitlines(keepends=True)
    entries = [line for line in lines if line.startswith("- ")]
    if len(entries) <= max_lines:
        return body + new_line
    return _SESSIONS_HEADER + "".join(entries[-max_lines:])
