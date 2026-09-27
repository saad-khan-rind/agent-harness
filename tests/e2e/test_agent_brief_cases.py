"""The two worked examples from the brief, run for real.

Section 8.2 prints these under the heading *"E2E test case examples, your
tests must cover realistic use cases"*, gives each a name, a prompt and a
list of assertions, and this file is that list — the prompts copied
verbatim, the assertions kept as they are written.

They are the reason this tier exists. ``numbers_in_words`` asks for a file
*"that contains a list with the numbers from 1 to 20 in words"*, and the
assertion is that ``one`` and ``twenty`` are in it byte-exact. Nothing
about which tool writes it, in what order, or with what markup — that is
the model's business, and a test that dictated it would be testing our
script instead of the agent. ``find_and_inspect`` goes further and asserts
on what the *assistant said*: "reports 5 lines inside notes.txt". A
scripted model can only confirm that about itself.

The same two cases, with the model scripted, are in
``tests/integration/test_filesystem_tasks.py``. That version pins the exact
tool arguments and runs in CI with no token; this one runs a real agent and
accepts any route to the right answer (ADR-074).
"""

import pytest
from tests.e2e.conftest import WORKSPACE, Stack

pytestmark = pytest.mark.e2e


# --- numbers_in_words ------------------------------------------------------

# The brief's prompt, character for character. Not paraphrased and not
# "improved" with a hint about create_folder: the case is only evidence if
# the agent gets what a user would actually type.
NUMBERS_PROMPT = (
    "Create a folder called output and inside it a file called numbers.md "
    "that contains the a list with the numbers from 1 to 20 in words. "
    "At least, make sure the first and last numbers exist."
)


def test_numbers_in_words(stack: Stack) -> None:
    """The brief's first case: folder, file, and the two words that must be in it.

    Three assertions, and they are the brief's three. The byte-exact ones
    are ``one`` and ``twenty``, which is what "make sure the first and last
    numbers exist" means and all it means — a model that writes ``1. one``
    passes, and so does one that writes ``- one``, because the brief asks
    for the numbers in words and not for a format.

    The tool assertion is the softest thing here on purpose: it checks that
    a file was written through the tool layer rather than hallucinated into
    the reply, which is the failure this case is really guarding against.
    """
    run = stack.run(NUMBERS_PROMPT)
    run.assert_completed()

    assert stack.is_dir(f"{WORKSPACE}/output"), (
        f"no output/ directory was created; tree:\n{stack.tree()}"
    )
    body = stack.read(f"{WORKSPACE}/output/numbers.md")
    assert "one" in body, f"'one' is missing from numbers.md:\n{body}"
    assert "twenty" in body, f"'twenty' is missing from numbers.md:\n{body}"

    # Any write tool, not `write_file` specifically. `write_file` and
    # `append_file` both legitimately produce this file, and which one a model
    # reaches for is its business — the failure being guarded against is a
    # file that was never written at all, not one written the other way
    # (ADR-078).
    wrote = {"write_file", "append_file", "edit_file"} & set(run.tools_called)
    assert wrote, f"the file was not written through a tool: {run.tools_called}"


def test_numbers_in_words_is_acknowledged_in_the_reply(stack: Stack) -> None:
    """The brief's third assertion: "Final agent turn acknowledges".

    Separate from the file assertions because it fails for a different
    reason. A run where the file is right and the reply is empty means the
    loop ended without a final message — a harness bug. A run where the
    reply is chatty and the file is missing means the model answered in
    prose instead of acting — a prompt bug. Merging them into one test
    would leave the failure ambiguous.
    """
    run = stack.run(NUMBERS_PROMPT)
    run.assert_completed()

    assert "numbers.md" in run.answer or "numbers" in run.answer, (
        f"the final turn does not acknowledge the file it wrote: {run.answer!r}"
    )


# --- find_and_inspect ------------------------------------------------------

# The brief's tree, including the escaped newlines. The file is written with
# real newlines, which is what makes the answer five lines rather than one.
NOTES = "This\nfile\nhas\none\nline"
README = "Kartoffelsalat"

FIND_PROMPT = (
    "Tell me what is inside path_ABC/. If notes.txt exists, read it and "
    "tell me how many lines it has."
)


@pytest.fixture
def path_abc(stack: Stack) -> Stack:
    """Pre-populate the brief's tree, exactly as its Setup section specifies.

    ``path_ABC/data/notes.txt``, ``path_ABC/data/readme.md`` and an empty
    ``path_ABC/empty/``. The empty directory is not decoration: it is there
    to see whether the agent reports a directory it cannot read anything
    out of, which is the kind of thing a model quietly drops.
    """
    stack.write(f"{WORKSPACE}/path_ABC/data/notes.txt", NOTES)
    stack.write(f"{WORKSPACE}/path_ABC/data/readme.md", README)
    stack.mkdir(f"{WORKSPACE}/path_ABC/empty")
    return stack


def test_find_and_inspect_lists_both_files(path_abc: Stack) -> None:
    """The brief's first assertion: both files are mentioned in the listing.

    Both, not either. Listing ``notes.txt`` alone is the plausible failure —
    the prompt names it, so a model that pattern-matches on the second
    sentence and skips the first will find it and never look at the rest of
    the tree.
    """
    run = path_abc.run(FIND_PROMPT)
    run.assert_completed()

    assert "notes.txt" in run.answer, f"notes.txt is not mentioned: {run.answer!r}"
    assert "readme.md" in run.answer, f"readme.md is not mentioned: {run.answer!r}"


def test_find_and_inspect_reports_five_lines(path_abc: Stack) -> None:
    """The brief's second assertion: "Assistant reports 5 lines inside notes.txt".

    This is the assertion that cannot be faked. The number has to come from
    a model that read a real file through a real tool and counted, and the
    only reason it is five is that the content has four newlines in it.

    Both spellings are accepted because both are correct English and the
    brief does not ask for one. What is *not* accepted is a nearby number:
    "4 lines" and "6 lines" both fail, which is what makes this a real
    check rather than a formality.
    """
    run = path_abc.run(FIND_PROMPT)
    run.assert_completed()

    assert "5" in run.answer or "five" in run.answer, (
        f"the agent did not report a line count: {run.answer!r}"
    )
    for wrong in ("4 lines", "6 lines", "four lines", "six lines"):
        assert wrong not in run.answer, (
            f"the agent counted wrong ({wrong}): {run.answer!r}"
        )


def test_find_and_inspect_actually_opens_the_file(path_abc: Stack) -> None:
    """The route, not just the answer: it listed, then it read.

    A model can produce "5" by guessing, and a test that only reads the
    reply would accept that. The trace says whether it earned the number:
    ``list_files`` to see what is there, ``read_file`` to open the one the
    prompt named conditionally — "*if* notes.txt exists".
    """
    run = path_abc.run(FIND_PROMPT)
    run.assert_completed()

    assert "read_file" in run.tools_called, (
        f"the line count was not read from the file: {run.tools_called}"
    )
    # `list_files` or `find_files`: both answer "what is inside path_ABC/",
    # and the prompt does not care which. Requiring one exact name would make
    # this a test of the model's taste rather than of the harness.
    listed = {"list_files", "find_files"} & set(run.tools_called)
    assert listed, f"the directory was never listed: {run.tools_called}"
