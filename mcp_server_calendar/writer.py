"""Appending an event to the calendar file. Not upstream's — ours.

Upstream `ics-calendar-mcp` is **read-only**: it fetches a feed over HTTP
and offers five query tools. That is the right shape for a feed you do not
own, and the wrong shape for a demo, because there would be no way to put
anything on the calendar in the first place.

So this module is one of the two additions the fork makes (ADR-071). It is
deliberately the smallest write that is useful:

* **append only.** A new ``VEVENT`` is added to the file; nothing existing
  is touched. There is no update and no delete, which is ADR-008 reaching
  the servers we write and not only the harness — and a ``delete_event``
  would be refused at registration by the adapter on its name alone
  (ADR-042), so shipping one would mean shipping a tool that can never be
  called.
* **the UID is generated here.** Accepting one from the caller would let
  ``create_event`` overwrite an existing event, which is a delete wearing a
  different name.
* **no recurrence.** A caller cannot write an ``RRULE``. Reading them is
  upstream's strength and the fork keeps it; letting a model *author* one
  means a single call can fill the calendar to the horizon, and there is no
  delete tool to undo it with.
"""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from icalendar import Calendar, Event

from mcp_server_calendar.formatting import InvalidArgument

# Bounds, published in the JSON Schema.
MAX_SUMMARY_CHARS = 200
MAX_LOCATION_CHARS = 200
MAX_DESCRIPTION_CHARS = 2_000

# An event longer than this is almost always a mistyped year. Storing one
# would black out the calendar to the horizon and make every reschedule
# search return nothing, with no clue why.
MAX_EVENT_HOURS = 24

_PRODID = "-//agent-harness//calendar//EN"


def create_event(
    path: Path,
    summary: str,
    start: datetime,
    end: datetime,
    location: str = "",
    description: str = "",
) -> dict[str, str]:
    """Append one event to the calendar file and return what was written.

    Args:
        path: The ``.ics`` file. Created, with a ``VCALENDAR`` wrapper, if
            it does not exist yet.
        summary: One-line title.
        start: Aware start.
        end: Aware end, after ``start``.
        location: Optional.
        description: Optional.

    Returns:
        The stored event's ``uid``, ``summary``, ``start`` and ``end``.

    Raises:
        InvalidArgument: On empty or over-long text, a naive datetime, a
            non-positive duration, or an implausibly long event.
    """
    title = _require_text(summary, "summary", MAX_SUMMARY_CHARS)
    place = _optional_text(location, "location", MAX_LOCATION_CHARS)
    notes = _optional_text(description, "description", MAX_DESCRIPTION_CHARS)
    _require_aware(start, "start")
    _require_aware(end, "end")
    if end <= start:
        raise InvalidArgument("end must be after start")
    if end - start > timedelta(hours=MAX_EVENT_HOURS):
        raise InvalidArgument(
            f"an event longer than {MAX_EVENT_HOURS} hours is almost always a "
            f"mistyped date; create separate events instead"
        )

    calendar = _load_or_create(path)
    component = Event()
    uid = str(uuid.uuid4())
    component.add("uid", uid)
    component.add("summary", title)
    component.add("dtstart", start)
    component.add("dtend", end)
    # Required by RFC 5545 on a published event, and some clients refuse a
    # file without it. It records when the file was written, which is not
    # when the event happens and is never used for scheduling.
    component.add("dtstamp", datetime.now(UTC))
    if place:
        component.add("location", place)
    if notes:
        component.add("description", notes)
    calendar.add_component(component)

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_bytes(bytes(calendar.to_ical()))
    except OSError as exc:
        raise InvalidArgument(f"could not write the calendar file: {exc}") from exc

    return {
        "uid": uid,
        "summary": title,
        "start": start.isoformat(timespec="minutes"),
        "end": end.isoformat(timespec="minutes"),
        # The weekday of what was *actually written*, which is not
        # decoration. A user asks for "next Monday", the model works out a
        # date, and if it works it out wrong nothing downstream ever
        # disagrees with it — in one real session the agent wrote
        # 2026-09-15 and then told the user "Monday, September 15". It was a
        # Tuesday. Echoing the day back is the cheapest way for the model to
        # catch its own arithmetic before the user does (ADR-079).
        "day_of_week": start.strftime("%A"),
    }


def _load_or_create(path: Path) -> Calendar:
    """Return the existing calendar, or a new empty one.

    A file that exists but does not parse is *not* replaced: overwriting it
    would silently destroy whatever was in it, which is the one thing a
    server with no delete tool must not do by accident.
    """
    if not path.exists():
        calendar = Calendar()
        calendar.add("prodid", _PRODID)
        calendar.add("version", "2.0")
        return calendar
    try:
        return cast(Calendar, Calendar.from_ical(path.read_bytes().decode("utf-8-sig")))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise InvalidArgument(
            f"refusing to write: the existing calendar file is not readable "
            f"iCalendar and appending would lose it ({exc})"
        ) from exc


def _require_text(value: str, field: str, limit: int) -> str:
    """Return trimmed, non-empty text within ``limit``."""
    text = " ".join(value.split())
    if not text:
        raise InvalidArgument(f"{field} must not be empty")
    if len(text) > limit:
        raise InvalidArgument(
            f"{field} must be at most {limit} characters (got {len(text)})"
        )
    return text


def _optional_text(value: str, field: str, limit: int) -> str:
    """Return trimmed text within ``limit``, allowing empty."""
    text = " ".join(value.split())
    if len(text) > limit:
        raise InvalidArgument(
            f"{field} must be at most {limit} characters (got {len(text)})"
        )
    return text


def _require_aware(moment: datetime, field: str) -> None:
    """Reject a naive datetime.

    Every comparison in this server is between absolute instants, so a
    naive value is not a slightly worse input: it cannot be compared with
    the others without inventing a zone for it.
    """
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise InvalidArgument(f"{field} must carry a timezone offset")
