"""Keeping the confirmation prompt answerable when children run concurrently.

``require_user_confirmation`` is one of the three outcomes a policy must
support, and the whole of it rests on something the rest of the harness does
not otherwise depend on: there is exactly one terminal, one person, and one
question on it at a time. Sequential delegation preserved that for free — a
child ran between the parent's ``before_tool`` and ``after_tool``, so nothing
else could be asking.

A batched ``delegate`` breaks it in two ways at once. Two children reaching a
mutation together would interleave their prompts into one unreadable line,
and even if they did not, "Allow write_file?" with three agents running names
neither the agent nor the reason, so there is no answer a person could give
honestly.

So this wrapper does two things and nothing else: it **serialises** the
prompts, so the person is asked one question at a time, and it tells the
confirmer **who is asking**, so the question can say so. It is a wrapper
rather than a change to :class:`~harness.cli.ConsoleConfirmer` because
``print`` and ``input`` stay in ``cli.py``, and because the parent's own
confirmations keep going through the unwrapped confirmer exactly as before.

**The tool it passes on is the real tool.** An earlier version of this
wrapped it in a stand-in whose ``name`` carried the role, which read well in
the prompt and was wrong: ``PermissionTiers.tier_of`` matches the deny-list
and the operator's ``permission_rules`` on exactly that string, so a renamed
tool would have been explained to the person by the wrong rule — a misleading
sentence in the one prompt that exists to be read carefully. The asker
travels beside the tool instead, through an optional method the CLI's
confirmer implements and a plain one does not, so a confirmer that has not
heard of sub-agents still gets the call it has always got.

A child that is waiting here is not a child that has stopped: its deadline is
still running. That is the honest accounting — the time really did pass — and
it is one more reason the deadline had to be enforced at the model call
rather than only on the way into a tool (see :mod:`harness.subagents.deadline`).
"""

import threading
from typing import Protocol, runtime_checkable

from harness.permissions import Confirmer
from harness.tools.base import Tool


@runtime_checkable
class AttributingConfirmer(Protocol):
    """A confirmer that can say who a call is being made on behalf of.

    Optional: :class:`AttributedConfirmer` uses it when the session's
    confirmer implements it and falls back to the plain
    :meth:`~harness.permissions.Confirmer.confirm` when it does not. Kept
    separate from ``Confirmer`` rather than added to it so that every
    existing implementation — including the ones in ``tests/fakes.py`` —
    stays valid without changing.
    """

    def confirm_for(self, asker: str, tool: Tool, arguments: dict[str, object]) -> bool:
        """Return whether the user approved this call by ``asker``."""


class AttributedConfirmer:
    """Ask on behalf of one named child, holding a shared lock while asking.

    Implements :class:`~harness.permissions.Confirmer` structurally. One
    instance per child run — it carries that child's role — over a lock that
    every child of one session shares, which is what makes the prompts queue
    instead of collide.
    """

    def __init__(self, inner: Confirmer, lock: threading.Lock, role: str) -> None:
        """Args:
        inner: The session's real confirmer, normally the CLI's console one.
        lock: Shared across every child of this session. A plain
            :class:`threading.Lock` rather than an ``RLock``: a child never
            asks twice at once, and a re-entrant lock here would hide that
            mistake rather than deadlock on it visibly.
        role: The specialist asking, used to attribute the prompt.
        """
        self._inner = inner
        self._lock = lock
        self._role = role

    def confirm(self, tool: Tool, arguments: dict[str, object]) -> bool:
        """Put one child's call to the person, one at a time.

        Args:
            tool: The tool the child wants to run, passed on unchanged.
            arguments: Its arguments, passed through untouched.

        Returns:
            Whether the person approved.
        """
        asker = f"{self._role} sub-agent"
        with self._lock:
            if isinstance(self._inner, AttributingConfirmer):
                return self._inner.confirm_for(asker, tool, arguments)
            return self._inner.confirm(tool, arguments)
