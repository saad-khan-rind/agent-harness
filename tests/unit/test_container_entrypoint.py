"""The image must start the harness, and must pass a task through to it.

Two forms are documented in ``README.md`` and both are graded behaviour —
§7.3 requires the agent to be runnable via a single ``docker run``:

    docker run <image>                 # the REPL the brief asks for
    docker run <image> "some task"     # one task, then exit

The second only works if the image declares an ``ENTRYPOINT``. With ``CMD``
alone, Docker does not append ``"some task"`` to the command — it *replaces*
it, and tries to execute a program by that name. The image shipped that way
for a week without anyone noticing, because the only automated caller of the
one-shot form is the ``container`` job in ``.github/workflows/ci.yml``, which skips
itself on the FIM runner for want of a privileged Docker daemon (ADR-076).

So this file exists to make that specific regression loud and cheap. It
parses the ``Dockerfile`` as text rather than building anything: building
needs Docker, which CI does not reliably have, and the assertion is about a
declaration rather than about runtime behaviour. The end-to-end tier
(``tests/e2e/``) is where the behaviour itself is exercised — every test
there uses the one-shot form — but that tier needs a token, so this is the
version that runs everywhere.
"""

from pathlib import Path

import pytest

DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"


@pytest.fixture(scope="module")
def instructions() -> list[str]:
    """The Dockerfile's instruction lines, comments and blanks dropped.

    A missing file is reported as a failure that names the cause rather than
    left as a bare ``FileNotFoundError``. The suite runs both on a developer's
    checkout and inside ``agent-harness-tests``, and in the image a file only
    exists if ``Dockerfile.test`` copied it — so this exact test errored in the
    container while passing locally the day it was written. Saying so here
    costs three lines and saves reading a traceback.
    """
    if not DOCKERFILE.is_file():
        pytest.fail(
            f"{DOCKERFILE} is missing. Inside the test image that means "
            "Dockerfile.test does not COPY it; the file is test input, like "
            "compose.yaml, and has to be in the image for this to run."
        )
    lines = DOCKERFILE.read_text(encoding="utf-8").splitlines()
    return [
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_the_image_declares_an_entrypoint(instructions: list[str]) -> None:
    """Without one, the documented one-shot form silently does the wrong thing."""
    entrypoints = [line for line in instructions if line.startswith("ENTRYPOINT")]

    assert entrypoints, (
        "the Dockerfile declares no ENTRYPOINT, so "
        '`docker run <image> "some task"` replaces the command instead of '
        "passing the task to the harness"
    )
    assert len(entrypoints) == 1, f"more than one ENTRYPOINT: {entrypoints}"


def test_the_entrypoint_starts_the_harness(instructions: list[str]) -> None:
    """It has to be the module, in exec form.

    Exec form (a JSON array) rather than shell form, because shell form wraps
    the process in ``/bin/sh -c`` — which swallows SIGINT and would break the
    Ctrl-C handling ``harness/cli.py`` goes to some trouble to get right.
    """
    entrypoint = next(line for line in instructions if line.startswith("ENTRYPOINT"))

    assert entrypoint.startswith("ENTRYPOINT ["), (
        f"ENTRYPOINT must use exec form so signals reach the process: {entrypoint}"
    )
    assert '"python"' in entrypoint and '"-m"' in entrypoint, entrypoint
    assert '"harness"' in entrypoint, entrypoint


def test_no_cmd_competes_with_it(instructions: list[str]) -> None:
    """A leftover ``CMD`` would become default *arguments*, not a default command.

    ``CMD ["python", "-m", "harness"]`` under an ENTRYPOINT of the same thing
    would run ``python -m harness python -m harness`` — argparse would take
    the three extra words as a task and the agent would be handed nonsense on
    every bare run. That is a subtle enough failure to be worth one line.
    """
    assert [line for line in instructions if line.startswith("CMD")] == []
