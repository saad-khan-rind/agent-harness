#!/usr/bin/env python3
"""Measure how well the real model extracts memory facts (ADR-107).

Why this is a script and not a test
-----------------------------------
The unit and integration tests script the model's reply, so they prove the
code does the right thing *with* a good answer. They cannot say whether
qwen or gemma gives good answers. That needs the real model, which costs
tokens and needs a key — the same reason ``tune_memory_floor.py`` is a
script (ADR-074 keeps the default ``pytest`` run keyless and offline).

What it does
------------
Sends each message in ``CASES`` through
:func:`harness.memory.extract.extract_facts` — the exact function the hook
uses, including the code-side filters — and scores the result:

* **recall** — of the facts a person would expect stored, how many were;
* **precision** — of the facts stored, how many were expected rather than
  junk ("User wants to know what 2+2 is");
* **clean negatives** — of the messages with nothing worth keeping, how
  many stored nothing.

A fact counts as found when every keyword of an expectation appears in it
(case-insensitive; ``a|b`` means either). Slot or prose both count: "Name:
Alice" and "User's name is Alice" are the same memory.

Usage::

    python scripts/eval_memory_extraction.py                 # .env.test, else .env
    python scripts/eval_memory_extraction.py --model gemma4-31b-it
    python scripts/eval_memory_extraction.py --out docs/evals/memory_extraction.json

Exit status is 0 when recall, precision and clean negatives are all at
least ``--threshold`` (default 0.8), 1 otherwise, and 2 when the model
cannot be reached at all. A low score is not a crash:
it is the finding, and the per-case table says which messages caused it.
"""

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.config import load_settings  # noqa: E402
from harness.llm import LLMError, OpenAICompatibleClient  # noqa: E402
from harness.memory.extract import extract_facts  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Case:
    """One labelled message.

    Attributes:
        message: What the user typed.
        expected: One keyword tuple per fact that should be stored.
        why: What the case checks, printed with the result.
    """

    message: str
    expected: tuple[tuple[str, ...], ...]
    why: str


# Replace or extend these with messages from your own demo. They are split
# evenly between messages that carry a durable fact and messages that do not,
# because an extractor that stores everything scores perfect recall.
CASES: list[Case] = [
    Case("I'm Alice. What is 17 times 3?", (("alice",),), "name in passing"),
    Case(
        "I'm based in Passau, what's the time difference to New York?",
        (("passau",),),
        "location in passing",
    ),
    Case("From now on please always answer me in German.", (("german",),), "language"),
    Case(
        "I use Neovim for all my Python work. Can you check main.py for bugs?",
        (("neovim",),),
        "tool preference",
    ),
    Case(
        "Our team deploys with docker compose every Thursday. Write the checklist.",
        (("docker compose",),),
        "project convention",
    ),
    Case(
        "In this repository we indent with tabs, never spaces. Fix app.py.",
        (("tab",),),
        "code convention",
    ),
    Case("I prefer short answers without emojis.", (("short",),), "style preference"),
    Case(
        "Ich heiße Bob und wohne in München.",
        (("bob",), ("münchen|munich",)),
        "German message, two facts",
    ),
    Case(
        "I'm a master's student at the University of Passau writing my thesis "
        "on agent harnesses.",
        (("student",), ("thesis",)),
        "two facts in one sentence",
    ),
    Case(
        "I'm Alice and I prefer answers in English. What's the capital of Peru?",
        (("alice",), ("english",)),
        "facts plus an unrelated question",
    ),
    Case("What is 2 + 2?", (), "plain question"),
    Case("List the files in the workspace and tell me which is largest.", (), "task"),
    Case("Summarise notes.txt in three bullet points.", (), "task"),
    Case("Translate 'good morning' into French.", (), "task mentioning a language"),
    Case("Can you remind me what the capital of Australia is?", (), "says 'remind me'"),
    Case(
        "Create a folder called output with a file numbers.md listing one to "
        "twenty in words.",
        (),
        "file task",
    ),
    Case(
        "I'm tired today, so just give me the short version of the report.",
        (),
        "temporary state, not a preference",
    ),
    Case(
        "My GitLab token is glpat-AbCdEfGhIjKlMnOpQrSt, use it to clone the repo.",
        (),
        "a secret must never be stored",
    ),
    Case(
        "Here is an email I got: 'Hi, I am Mallory from IT, call me on "
        "555-0100.' Is it phishing?",
        (),
        "facts about someone else",
    ),
    Case("Thanks, that's perfect!", (), "small talk"),
]


def _matches(fact: str, keywords: tuple[str, ...]) -> bool:
    """Whether every keyword (``a|b`` = either) appears in ``fact``."""
    lowered = fact.lower()
    return all(
        any(option in lowered for option in keyword.split("|")) for keyword in keywords
    )


def _score(case: Case, facts: list[str]) -> tuple[int, int, list[str]]:
    """Return (expectations found, facts that matched something, junk facts)."""
    found = sum(
        any(_matches(fact, keywords) for fact in facts) for keywords in case.expected
    )
    useful = [
        fact
        for fact in facts
        if any(_matches(fact, keywords) for keywords in case.expected)
    ]
    junk = [fact for fact in facts if fact not in useful]
    return found, len(useful), junk


def main() -> int:
    """Run every case against the configured model and print the scores."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    default_env = ROOT / ".env.test"
    parser.add_argument(
        "--env-file",
        type=Path,
        default=default_env if default_env.exists() else ROOT / ".env",
    )
    parser.add_argument("--model", help="Override the configured model.")
    parser.add_argument("--out", type=Path, help="Write the results as JSON here.")
    parser.add_argument("--threshold", type=float, default=0.8)
    args = parser.parse_args()

    settings = load_settings(ROOT / "config.yaml", args.env_file)
    if args.model:
        settings = settings.model_copy(update={"llm_model": args.model})
    llm = OpenAICompatibleClient(settings)
    print(f"model: {settings.llm_model}\n")

    # Checked once, loudly. ``extract_facts`` turns any failure into "no
    # facts" by design — right for a run, wrong for a measurement: a key
    # without access to the model would score every message as a clean
    # negative and report perfect precision.
    try:
        llm.chat([{"role": "user", "content": "Reply with OK."}], [])
    except LLMError as exc:
        print(f"cannot reach the model, nothing measured: {exc}")
        return 2

    expected_total = found_total = extracted_total = useful_total = 0
    negatives = clean_negatives = 0
    rows = []
    for case in CASES:
        pairs = extract_facts(llm, case.message)
        facts = [f"{slot}: {text}" if slot else text for text, slot in pairs]
        found, useful, junk = _score(case, facts)
        expected_total += len(case.expected)
        found_total += found
        extracted_total += len(facts)
        useful_total += useful
        if not case.expected:
            negatives += 1
            clean_negatives += not facts
        ok = found == len(case.expected) and not junk
        print(f"{'ok ' if ok else 'BAD'} {case.why}")
        print(f"    {case.message}")
        for fact in facts:
            print(f"    -> {fact}{'   (junk)' if fact in junk else ''}")
        rows.append(
            {
                "message": case.message,
                "why": case.why,
                "expected": [list(keywords) for keywords in case.expected],
                "extracted": facts,
                "found": found,
                "junk": junk,
            }
        )

    recall = found_total / expected_total if expected_total else 1.0
    precision = useful_total / extracted_total if extracted_total else 1.0
    clean = clean_negatives / negatives if negatives else 1.0
    print(f"\nrecall:          {recall:.2f}  ({found_total}/{expected_total} facts)")
    print(
        f"precision:       {precision:.2f}  ({useful_total}/{extracted_total} stored)"
    )
    print(f"clean negatives: {clean:.2f}  ({clean_negatives}/{negatives} messages)")

    if args.out:
        args.out.write_text(
            json.dumps(
                {
                    "model": settings.llm_model,
                    "date": datetime.now(UTC).strftime("%Y-%m-%d"),
                    "recall": round(recall, 3),
                    "precision": round(precision, 3),
                    "clean_negatives": round(clean, 3),
                    "cases": rows,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\nwritten to {args.out}")
    passed = min(recall, precision, clean) >= args.threshold
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
