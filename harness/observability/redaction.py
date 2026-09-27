"""What is safe to put in telemetry, decided once.

The Week 3 trace hook solved this problem by recording nothing: no tool
arguments, no observations, no prompts. That was the right default and it is
still the default here — a ``write_file`` body on the memory volume is a
second copy of the workspace that nobody asked for, and a prompt in a span
is a prompt in whatever backend the span is exported to. But "record
nothing" also threw away the single most useful debugging question about
arguments, which is not *what* the agent passed but *whether it kept passing
the same thing* (ADR-094).

So there are three modes, and the middle one is the default:

``strict``
    Nothing derived from arguments at all. What Week 3 did. Right for a
    deployment where even the *shape* of a call is sensitive.

``shapes`` (default)
    Key names, value types and value *sizes* — ``path=str[18]
    content=str[4213]`` — plus a fingerprint. No value ever leaves the
    process. This answers "was it writing a big file or a small one", "did
    it pass the argument at all", and, via the fingerprint, "is it calling
    the same thing over and over" without disclosing a single byte.

``values``
    Truncated values as well, with secret-looking keys still removed. For
    local debugging only. It is not the default and the banner says when it
    is on, because a mode that quietly starts logging file contents is a
    mode that will eventually log something it should not.

**The fingerprint is the good idea here.** It is a short hash of the
normalised arguments, so two calls with identical arguments produce
identical fingerprints and nothing else can be recovered from it. That is
exactly enough to see a model thrashing — the pattern §9 calls "are agents
repeatedly calling the same failing tool" — and it is the same normalisation
:meth:`~harness.context.RunContext.record_call_signature` already uses for
the no-progress guard, so the trace and the guard agree about what "the same
call" means.

**Key names are themselves filtered.** Even in ``shapes`` mode a key called
``api_key`` is replaced rather than named, because the *presence* of a
credential argument is worth hiding and because a key name is one typo away
from being a value (a caller passing ``{"token": ...}`` is common; a caller
passing ``{token_value: ...}`` as a key is not unheard of).
"""

import hashlib
import json
from typing import Any, Final, Literal

RedactionMode = Literal["strict", "shapes", "values"]

# Substrings that make a key name suspicious. Matched case-insensitively
# against the whole key, so `api_key`, `X-Auth-Token` and `userPassword` are
# all caught. Deliberately broad: a false positive costs one redacted field
# in a debug view, a false negative costs a credential in a trace backend.
_SECRET_KEY_MARKERS: Final = (
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "auth",
    "apikey",
    "api_key",
    "access_key",
    "private",
    "cookie",
    "session",
    "bearer",
    "signature",
    "salt",
    "cert",
)

# What a redacted key or value is replaced with. Visible rather than
# omitted: a reader should be able to tell "this field was removed on
# purpose" from "this field was never sent", and an omission cannot say
# which.
REDACTED: Final = "[redacted]"

# Ceiling on one value in `values` mode.
MAX_VALUE_CHARS: Final = 120

# Ceiling on the whole digest string. A tool called with forty arguments is
# not a thing this harness has, but a remote tool on somebody else's server
# might be, and an unbounded attribute is an unbounded span.
MAX_DIGEST_CHARS: Final = 500

# Length of the argument fingerprint. Twelve hex characters is 48 bits: far
# more than enough to avoid collisions within one run, and short enough to
# read off a terminal and compare by eye.
FINGERPRINT_CHARS: Final = 12


def is_secret_key(key: str) -> bool:
    """Whether a key name suggests it holds a credential.

    Args:
        key: The argument name as the model supplied it.

    Returns:
        ``True`` if the name contains any marker in
        :data:`_SECRET_KEY_MARKERS`.
    """
    lowered = key.lower().replace("-", "_")
    return any(marker in lowered for marker in _SECRET_KEY_MARKERS)


def _type_name(value: Any) -> str:
    """Return a short, stable name for a value's type."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, (list, tuple)):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return type(value).__name__


def _size_of(value: Any) -> int | None:
    """Return a meaningful size for a value, or ``None`` if it has none.

    Characters for a string, elements for a collection, nothing for a
    scalar — a size of ``4`` on the integer ``4000`` would be noise.
    """
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (list, tuple, dict)):
        return len(value)
    return None


def _shape(value: Any) -> str:
    """Render one value as ``type`` or ``type[size]``."""
    kind = _type_name(value)
    size = _size_of(value)
    return f"{kind}[{size}]" if size is not None else kind


def _clip(value: Any) -> str:
    """Render one value truncated, for ``values`` mode."""
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return str(value)
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = text.replace("\n", "\\n")
    if len(text) > MAX_VALUE_CHARS:
        return f"{text[:MAX_VALUE_CHARS]}…({len(text)} chars)"
    return text


def argument_digest(
    arguments: dict[str, Any], mode: RedactionMode = "shapes"
) -> str | None:
    """Describe a tool call's arguments without disclosing them.

    Args:
        arguments: What the model passed.
        mode: ``strict``, ``shapes`` or ``values``. See the module
            docstring.

    Returns:
        A single-line digest such as ``path=str[18] content=str[4213]``, or
        ``None`` in ``strict`` mode and for a call with no arguments. A
        ``None`` return is what :meth:`~harness.observability.api.Span.set`
        drops, so the attribute is simply absent.
    """
    if mode == "strict" or not arguments:
        return None

    parts: list[str] = []
    for key in sorted(arguments):
        value = arguments[key]
        if is_secret_key(key):
            # The key name is replaced too, not just the value: which
            # credential a call carried is itself a detail worth not having
            # in a trace backend.
            parts.append(f"{REDACTED}={REDACTED}")
            continue
        rendered = _shape(value) if mode == "shapes" else _clip(value)
        parts.append(f"{key}={rendered}")

    digest = " ".join(parts)
    if len(digest) > MAX_DIGEST_CHARS:
        digest = digest[:MAX_DIGEST_CHARS] + "…"
    return digest or None


def argument_fingerprint(arguments: dict[str, Any]) -> str | None:
    """Return a short stable hash of a call's arguments.

    Safe in every mode, including ``strict``, because a hash of a value is
    not the value: nothing can be recovered from it, and it cannot be
    reversed by anyone who does not already know what they are looking for.
    What it *can* do is equality, which is the whole point — two calls with
    the same fingerprint are the same call, and a column of identical
    fingerprints in a trace is a model going in circles.

    Normalisation matches
    :meth:`~harness.context.RunContext.record_call_signature`: JSON with
    sorted keys, ``default=str`` for anything unserialisable. That is
    deliberate. The no-progress guard and the trace must agree about what
    "the same call" means, or a run stopped for repetition will show a
    trace whose fingerprints all differ.

    Args:
        arguments: What the model passed.

    Returns:
        Twelve hex characters, or ``None`` for a call with no arguments —
        every no-argument call would otherwise share one fingerprint and
        look like thrashing.
    """
    if not arguments:
        return None
    try:
        encoded = json.dumps(arguments, sort_keys=True, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        encoded = repr(sorted(arguments.items()))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:FINGERPRINT_CHARS]


def result_summary(observation: str, mode: RedactionMode = "shapes") -> int:
    """Return a tool observation's length, and never its content.

    Not mode-dependent, and not a mistake: a *size* is safe in every mode
    and is the one thing about an observation worth putting in a span. It
    answers "did this call return anything", "is this the call that blew up
    the context window", and — compared against the tool's truncation
    ceiling — "was the result cut off, so is the model reasoning from a
    fragment".

    The content itself is never recorded at any setting. ``values`` mode
    widens what is kept about *inputs*, which a person chose to send; an
    observation can contain a whole file the person never looked at.

    Args:
        observation: The tool's observation text.
        mode: Accepted and ignored, so the call site reads consistently
            with the other two functions here.

    Returns:
        Character count.
    """
    return len(observation)


def describe_mode(mode: RedactionMode) -> str:
    """One line for the startup banner, so the setting is never a surprise.

    Args:
        mode: The configured mode.

    Returns:
        Text for the banner.
    """
    return {
        "strict": "telemetry records no argument detail",
        "shapes": "telemetry records argument shapes and fingerprints, no values",
        "values": "telemetry records truncated argument VALUES (debug setting)",
    }.get(mode, f"telemetry redaction: {mode}")
