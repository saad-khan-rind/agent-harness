#!/usr/bin/env python3
"""Measure the right ``min_similarity`` for the embedder on THIS machine.

Why this is a script and not a constant
---------------------------------------
The similarity floor decides which retrieved facts reach the prompt. It is
the one number in the memory subsystem that cannot be chosen from theory:
every embedding model puts unrelated text at a different baseline, so a
value tuned for one is meaningless for another. The lexical fallback needs
0.20. A sentence-transformers model needs roughly twice that. Copying the
wrong one does not raise an error — it silently stops filtering, and every
query starts returning the whole collection.

This repository can measure the lexical floor because the lexical embedder
ships in it. It cannot measure the local model's, because CI has no network
and no model to download. So the measurement moves to the machine that has
one, and runs against that machine's own facts rather than a fixture.

What it does
------------
Embeds the pairs in ``PAIRS`` below, prints the two distributions, and
reports whether they separate and where the boundary is. Then either write
the suggested value into ``config.yaml`` as ``memory_min_similarity``, or —
better — replace ``PAIRS`` with questions and facts from your own demo
first, because a floor tuned on someone else's examples is the thing this
script exists to stop.

Usage::

    python scripts/tune_memory_floor.py
    python scripts/tune_memory_floor.py --model sentence-transformers/all-MiniLM-L6-v2
    python scripts/tune_memory_floor.py --lexical    # no download, checks the fallback

Exit status is 0 when the distributions separate, 1 when they overlap. An
overlap is not a crash: it is the finding, and it means no single threshold
can do this job for those pairs with that model.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.memory.embed import (  # noqa: E402
    Embedder,
    HashingEmbedder,
    load_sentence_transformer,
)

# (query, fact, should_match)
#
# Replace these with your own. The defaults are the pairs that exposed the
# original bug — "what do i like?" against a stored Likes fact scored 0.115
# on the lexical embedder against a floor of 0.20, so the fact was never
# injected and the agent said it knew nothing.
PAIRS: list[tuple[str, str, bool]] = [
    ("what do i like?", "Likes: swimming, going to the gym, and casual runs", True),
    ("what are my hobbies", "Likes: swimming, going to the gym, and casual runs", True),
    ("do i like swimming", "Likes: swimming, going to the gym, and casual runs", True),
    ("my interests", "Likes: swimming, going to the gym, and casual runs", True),
    ("how do we deploy", "the harness deploys with docker compose", True),
    ("what is my name", "Name: Carol", True),
    ("what food do i enjoy", "The user's favourite food is biryani.", True),
    ("which fruit is preferred", "apricots are the preferred fruit", True),
    (
        "the weather in Lisbon tomorrow",
        "the harness deploys with docker compose",
        False,
    ),
    (
        "the weather in Lisbon tomorrow",
        "Likes: swimming, going to the gym, and casual runs",
        False,
    ),
    ("what is my name", "the harness deploys with docker compose", False),
    ("how do we deploy", "Likes: swimming, going to the gym, and casual runs", False),
    ("how do we deploy", "Name: Carol", False),
    ("quantum chromodynamics notes", "The user's favourite food is biryani.", False),
    ("what food do i enjoy", "the harness deploys with docker compose", False),
]


def cosine(embedder: Embedder, left: str, right: str) -> float:
    """Return the cosine similarity of two texts under ``embedder``.

    A plain dot product, because every embedder in this package returns
    L2-normalised vectors — the same assumption the store's scoring makes.
    """
    a = embedder.embed(left)
    b = embedder.embed(right)
    return sum(x * y for x, y in zip(a, b, strict=True))


def main() -> int:
    """Measure and report. See the module docstring."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default="sentence-transformers/all-MiniLM-L6-v2",
        help="sentence-transformers checkpoint to measure",
    )
    parser.add_argument(
        "--lexical",
        action="store_true",
        help="measure the offline hashing embedder instead (no download)",
    )
    args = parser.parse_args()

    if args.lexical:
        embedder: Embedder | None = HashingEmbedder()
    else:
        print(f"loading {args.model} (first run downloads it)...")
        embedder = load_sentence_transformer(args.model)
        if embedder is None:
            print(
                f"could not load {args.model!r}.\n"
                f"  pip install sentence-transformers\n"
                f"and make sure this machine can reach huggingface.co.",
                file=sys.stderr,
            )
            return 2

    print(f"embedder: {embedder.name}")
    print(f"declared floor: {getattr(embedder, 'min_similarity', 'n/a')}\n")

    genuine: list[tuple[float, str]] = []
    false: list[tuple[float, str]] = []
    for query, fact, should_match in PAIRS:
        score = cosine(embedder, query, fact)
        label = f"{query!r} ~ {fact[:44]!r}"
        (genuine if should_match else false).append((score, label))

    print("SHOULD match (want these above the floor):")
    for score, label in sorted(genuine):
        print(f"  {score:.3f}  {label}")
    print("\nSHOULD NOT match (want these below the floor):")
    for score, label in sorted(false, reverse=True):
        print(f"  {score:.3f}  {label}")

    weakest = min(score for score, _ in genuine)
    strongest = max(score for score, _ in false)
    print(f"\nweakest genuine match : {weakest:.3f}")
    print(f"strongest false match : {strongest:.3f}")

    if weakest > strongest:
        suggested = round((weakest + strongest) / 2, 2)
        print(f"\nSEPARATED by {weakest - strongest:.3f}")
        print(f"set memory_min_similarity: {suggested}  in config.yaml")
        return 0

    print(
        f"\nOVERLAP of {strongest - weakest:.3f} — no single floor separates these.\n"
        f"Every threshold either drops a real match or admits a false one.\n"
        f"That is the finding, not a failure of this script: it is what the\n"
        f"lexical embedder does, and the reason to use a real model.\n"
        f"If you are already using one, your pairs may be genuinely\n"
        f"ambiguous — rewrite PAIRS with examples from your own demo."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
