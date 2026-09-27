"""Allow ``python -m harness`` to start the CLI.

Kept to three lines on purpose: this is the container's entry point, and
anything that runs here runs before configuration has been validated.
"""

import sys

from harness.cli import main

if __name__ == "__main__":
    # Propagate the CLI's exit code so a one-shot container run can be used
    # in a script or CI step and actually fail when the harness fails.
    sys.exit(main())
