"""Model calls the harness makes on its own behalf, not the agent's.

Three memory features ask the model a question outside the ReAct loop:
consolidation ("does this new fact replace an old one?"), extraction
("what did the user just tell us that is worth keeping?") and conversation
summarisation. They share one rule, and this module exists so it is
written once: **an auxiliary call never fails a run.** A timeout, a 429, a
spent budget or a reply that is not the JSON that was asked for all come
back as ``None``, and every caller has a deterministic answer ready for
``None`` (ADR-107, ADR-108).

No tool schemas are sent. These are questions with a fixed answer shape,
and offering tools would let a question about memory turn into an action.
"""

import json
from typing import Any

from harness.llm.protocol import LLMClient


def ask_text(llm: LLMClient, system: str, user: str) -> str | None:
    """Send one two-message request and return the reply text.

    Args:
        llm: The chat client. In the CLI this is the budgeted, traced client,
            so the call is metered and appears on the dashboard like any
            other model request.
        system: Instructions for this one question.
        user: The material to answer about.

    Returns:
        The stripped reply, or ``None`` on any failure or an empty reply.
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    try:
        response = llm.chat(messages, [])
    except Exception:  # noqa: BLE001 - an auxiliary call must never fail a run
        return None
    content = (response.content or "").strip()
    return content or None


def ask_json(llm: LLMClient, system: str, user: str) -> dict[str, Any] | None:
    """Like :func:`ask_text`, but return the first JSON object in the reply.

    Args:
        llm: The chat client.
        system: Instructions, which should ask for JSON only.
        user: The material to answer about.

    Returns:
        The decoded object, or ``None`` when the call failed or the reply
        held no JSON object.
    """
    reply = ask_text(llm, system, user)
    return parse_json_object(reply) if reply is not None else None


def parse_json_object(text: str) -> dict[str, Any] | None:
    """Return the outermost ``{...}`` in ``text`` as a dict, or ``None``.

    Mid-sized models wrap JSON in prose or a markdown fence often enough
    that insisting on a bare object would turn a correct answer into a
    fallback. Taking the span from the first ``{`` to the last ``}`` accepts
    both, and anything that still does not decode is treated as no answer.

    Args:
        text: A model reply.

    Returns:
        The decoded object, or ``None``.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        decoded = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) else None
