"""Event formatting, carried from upstream's ``utils.py``.

Forked from `adamchrabaszcz/ics-calendar-mcp` (MIT). These functions decide
the **shape of every event this server returns**, so they are the part of
the fork that must not drift: a client written against upstream's
`get_events_range` gets the same keys from ours (``uid``, ``summary``,
``start``, ``end``, ``all_day``, and the optional ``location``,
``description``, ``status``, ``organizer``).

Two deliberate deviations from upstream, both recorded in ADR-071:

**`pytz` is gone.** Upstream uses ``pytz.timezone(...).localize(...)``, which
is the pre-3.9 idiom. This repository already depends on ``tzdata`` and uses
``zoneinfo`` everywhere else (ADR-044), and mixing two timezone libraries in
one image is how you get two answers to the same question. The behaviour is
unchanged: a naive datetime is read as local, an aware one is converted, and
a date becomes local midnight.

**A bad timezone raises instead of silently defaulting.** Upstream's
``parse_datetime`` falls through to ``tz.localize(datetime.now())`` when it
is handed something it does not recognise — an event whose time could not be
read becomes *now*, which for a tool that finds free time is the worst
possible wrong answer: it moves a mystery event on top of the current hour.
Ours returns ``None`` and the caller drops the event.
"""

from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ``Component``, not ``Event``: ``Calendar.walk("VEVENT")`` and
# ``recurring_ical_events`` both hand back the base class, and every field
# these helpers read is a ``Component`` field. Narrowing the annotation to
# ``Event`` would buy nothing and cost a cast at every call site.
from icalendar import Component

# Upstream defaults to Europe/Warsaw, its author's zone. Ours is passed in
# from ``--local-timezone`` and defaults to UTC, for the same reason
# ADR-044 gave: the container's clock is UTC, and a default that guesses
# what the caller can state is not worth shipping.
DEFAULT_TIMEZONE = "UTC"


class InvalidArgument(ValueError):
    """One argument did not survive validation.

    A distinct type so :mod:`mcp_server_calendar.server` can tell a caller
    error from an internal fault and map the two to different MCP
    responses. Upstream has no equivalent — it returns ``{"error": ...}``
    from a *successful* tool call, which is the defect ADR-071 fixes.
    """


def zone(name: str) -> ZoneInfo:
    """Return the IANA zone called ``name``.

    Raises:
        InvalidArgument: If the zone is unknown, naming the offender. The
            same message ``mcp_server_time_plus`` gives, so a model that
            mistypes a zone gets the same correctable answer from either
            server.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise InvalidArgument(
            f"{name!r} is not an IANA timezone name (try 'Europe/Berlin')"
        ) from exc


def parse_date(date_str: str) -> date:
    """Parse an ISO ``YYYY-MM-DD`` string. Upstream's signature.

    Raises:
        InvalidArgument: On anything else. Upstream lets ``ValueError``
            escape to the tool, which catches it and returns the string
            ``"Invalid date format: ..."`` inside a successful result.
    """
    try:
        return datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError) as exc:
        raise InvalidArgument(
            f"date must be ISO format YYYY-MM-DD, e.g. '2026-09-14' (got {date_str!r})"
        ) from exc


def parse_datetime(dt_value: Any, timezone: str = DEFAULT_TIMEZONE) -> datetime | None:
    """Convert an icalendar date or datetime into an aware datetime.

    Upstream's function, with ``zoneinfo`` in place of ``pytz`` and
    ``None`` in place of the silent fall-through to "now".

    Args:
        dt_value: What ``icalendar`` decoded from ``DTSTART`` / ``DTEND``.
        timezone: Zone a naive value is read as, and the zone an aware
            value is reported in.

    Returns:
        An aware datetime, or ``None`` when the value is not a date at all.
    """
    tz = zone(timezone)
    if isinstance(dt_value, datetime):
        if dt_value.tzinfo is None:
            # A floating time is legal iCalendar and means "local wherever
            # you are". There is no such place inside a server, so the
            # configured zone stands in and the assumption is stated here.
            return dt_value.replace(tzinfo=tz)
        return dt_value.astimezone(tz)
    if isinstance(dt_value, date):
        return datetime.combine(dt_value, time.min, tzinfo=tz)
    return None


def format_event(event: Component, timezone: str = DEFAULT_TIMEZONE) -> dict[str, Any]:
    """Convert an icalendar event to upstream's dictionary shape.

    The keys are upstream's and are not ours to change: they are what makes
    this a fork rather than a rewrite, and
    ``tests/unit/test_calendar_upstream.py`` pins them.
    """
    dtstart = event.get("DTSTART")
    dtend = event.get("DTEND")

    start_dt = parse_datetime(dtstart.dt, timezone) if dtstart else None
    end_dt = parse_datetime(dtend.dt, timezone) if dtend else None

    is_all_day = bool(
        dtstart
        and isinstance(dtstart.dt, date)
        and not isinstance(dtstart.dt, datetime)
    )

    result: dict[str, Any] = {
        "uid": str(event.get("UID", "")),
        "summary": str(event.get("SUMMARY", "No title")),
        "start": start_dt.isoformat() if start_dt else None,
        "end": end_dt.isoformat() if end_dt else None,
        "all_day": is_all_day,
    }
    if event.get("LOCATION"):
        result["location"] = str(event.get("LOCATION"))
    if event.get("DESCRIPTION"):
        result["description"] = str(event.get("DESCRIPTION"))
    if event.get("STATUS"):
        result["status"] = str(event.get("STATUS"))
    if event.get("ORGANIZER"):
        result["organizer"] = str(event.get("ORGANIZER")).replace("mailto:", "")
    return result


def format_event_short(
    event: Component, timezone: str = DEFAULT_TIMEZONE
) -> dict[str, Any]:
    """Upstream's compact shape for list views: clock times, not ISO."""
    dtstart = event.get("DTSTART")
    dtend = event.get("DTEND")

    start_dt = parse_datetime(dtstart.dt, timezone) if dtstart else None
    end_dt = parse_datetime(dtend.dt, timezone) if dtend else None

    is_all_day = bool(
        dtstart
        and isinstance(dtstart.dt, date)
        and not isinstance(dtstart.dt, datetime)
    )

    result: dict[str, Any] = {
        "uid": str(event.get("UID", "")),
        "summary": str(event.get("SUMMARY", "No title")),
        "start": start_dt.strftime("%H:%M")
        if start_dt and not is_all_day
        else "All day",
        "end": end_dt.strftime("%H:%M") if end_dt and not is_all_day else None,
    }
    if event.get("LOCATION"):
        result["location"] = str(event.get("LOCATION"))
    return result


def _get_rrule_value(rule: dict[str, Any], key: str, default: Any = None) -> Any:
    """Read one RRULE field, which icalendar may give as a list or a scalar.

    Upstream's helper, unchanged. ``FREQ=WEEKLY`` decodes to ``['WEEKLY']``
    while ``INTERVAL=2`` may decode to ``2``, so every read has to cope
    with both.
    """
    value = rule.get(key, default)
    if isinstance(value, list):
        return value[0] if value else default
    return value


def format_recurring_event(
    event: Component, timezone: str = DEFAULT_TIMEZONE
) -> dict[str, Any]:
    """Upstream's recurring shape: the event plus a readable rule summary.

    The human-readable line ("weekly on Monday, Wednesday until
    2026-12-31") is upstream's idea and worth keeping: an RRULE is not
    something a mid-sized model reads reliably, and this is the field that
    makes ``list_recurring_events`` useful rather than raw.
    """
    result = format_event(event, timezone)

    rrule = event.get("RRULE")
    if not rrule:
        return result

    rule = dict(rrule)
    freq = _get_rrule_value(rule, "FREQ")
    interval = _get_rrule_value(rule, "INTERVAL", 1)
    until = _get_rrule_value(rule, "UNTIL")
    byday = rule.get("BYDAY", [])

    result["recurrence"] = {
        "frequency": freq,
        "interval": interval,
        "until": until.isoformat() if until else None,
        "days": list(byday) if byday else None,
        "description": _describe_rule(freq, interval, until, byday),
    }
    return result


_FREQUENCY_WORDS = {
    "DAILY": "daily",
    "WEEKLY": "weekly",
    "MONTHLY": "monthly",
    "YEARLY": "yearly",
}

_DAY_WORDS = {
    "MO": "Monday",
    "TU": "Tuesday",
    "WE": "Wednesday",
    "TH": "Thursday",
    "FR": "Friday",
    "SA": "Saturday",
    "SU": "Sunday",
}


def _describe_rule(freq: Any, interval: Any, until: Any, byday: Any) -> str:
    """Render an RRULE as a sentence. Upstream's wording, kept."""
    description = _FREQUENCY_WORDS.get(str(freq), str(freq))
    if isinstance(interval, int) and interval > 1:
        description = f"every {interval} {description.rstrip('ly')}s"
    if byday:
        days = [_DAY_WORDS.get(str(day), str(day)) for day in byday]
        description += f" on {', '.join(days)}"
    if until is not None:
        description += f" until {until.strftime('%Y-%m-%d')}"
    return description
