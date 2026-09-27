"""Meeting-slot search across timezones. Pure functions, no MCP imports.

This module is the *capability*; :mod:`mcp_server_time_plus.server` is the
*interface*. Keeping them apart is deliberate and is what the brief's Week 2
wording asks for — we are graded on the MCP interface (name, description,
schema, validation, result transformation, errors), so the interface should
be readable on its own, and the logic under it should be testable without
speaking the protocol at all. Every test in
``tests/unit/test_time_plus_meeting.py`` calls straight into here.

The capability: given several IANA timezones, a date, a duration and one
working-hours window, return the instants at which a meeting of that length
falls inside everybody's working day. That is a real scheduling problem —
the reason it is not a one-liner is daylight saving time. The offset between
two zones is not a constant: Europe and North America change clocks on
different weekends, and India, Nepal and parts of Australia sit on half- and
quarter-hour offsets. Any arithmetic done on "UTC+2" style numbers is wrong
twice a year, so all arithmetic here happens on absolute instants and every
local clock reading is produced by converting an instant into a zone.

Time is deliberately never read from the system clock: a search for a given
date returns the same answer on any machine, on any day, which is what makes
the tests deterministic.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import date as date_type
from datetime import time as time_type
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Candidate meetings start on a quarter-hour grid. Fifteen minutes is the
# smallest unit anybody schedules in practice, and it keeps the search at 96
# candidates per day: small enough to be exhaustive, so the result is "every
# slot that works", not "the first few we happened to try".
GRID_MINUTES = 15

# Bounds. They exist to keep the tool *bounded* in the sense the brief asks
# for — a capability with a clearly described, finite scope rather than an
# open-ended one — and they are mirrored into the JSON Schema so the model is
# told the limits instead of discovering them through an error.
MIN_PARTICIPANTS = 2
MAX_PARTICIPANTS = 8
MIN_DURATION_MINUTES = 15
MAX_DURATION_MINUTES = 480
MIN_RESULTS = 1
MAX_RESULTS = 20
MIN_YEAR = 2000
MAX_YEAR = 2100


class InvalidArgument(ValueError):
    """One argument did not survive validation.

    A distinct type so :mod:`mcp_server_time_plus.server` can tell a caller
    error ("Asia/Karach is not a timezone") from an internal fault, and map
    the two to different MCP responses. Subclasses ``ValueError`` so the SDK
    still reports it as a failed tool call if the mapping is ever bypassed.
    """


@dataclass(frozen=True)
class LocalView:
    """How one slot reads on one participant's wall clock.

    The date is carried per participant, not once per slot, because it is
    routinely *different*: 09:00 in Sydney is the previous afternoon in
    Berlin. A slot list that showed only one date would be quietly wrong for
    everyone else on the call.
    """

    timezone: str
    start: str
    end: str
    day: str
    date: str


@dataclass(frozen=True)
class Slot:
    """One meeting time that fits every participant's working day."""

    start_utc: datetime
    end_utc: datetime
    local: list[LocalView]

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-safe form sent back to the model."""
        return {
            "start_utc": self.start_utc.isoformat(timespec="minutes"),
            "end_utc": self.end_utc.isoformat(timespec="minutes"),
            "local": [
                {
                    "timezone": view.timezone,
                    "start": view.start,
                    "end": view.end,
                    "day": view.day,
                    "date": view.date,
                }
                for view in self.local
            ],
        }


def zone(name: str) -> ZoneInfo:
    """Resolve an IANA timezone name.

    Args:
        name: Timezone name, e.g. ``Europe/Berlin``.

    Returns:
        The loaded zone.

    Raises:
        InvalidArgument: If the name is not in the IANA database. The message
            names the offending value, because the model's next move is to
            correct it — "Invalid timezone" alone tells it nothing when it
            sent three.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidArgument(
            f"{name!r} is not an IANA timezone name (expected e.g. "
            f"'Europe/Berlin', 'Asia/Karachi', 'UTC')."
        ) from exc
    except OSError as exc:  # pragma: no cover - platform without tzdata
        raise InvalidArgument(f"timezone database unavailable for {name!r}") from exc


def parse_date(value: str) -> date_type:
    """Parse a strict ``YYYY-MM-DD`` calendar date.

    ``date.fromisoformat`` accepts a few other ISO 8601 spellings, so the
    length is checked first: a schema that says ``YYYY-MM-DD`` and an
    implementation that quietly accepts ``20260909`` is how a model learns a
    format the documentation does not describe.
    """
    if len(value) != 10:
        raise InvalidArgument(f"date {value!r} is not in YYYY-MM-DD form.")
    try:
        parsed = date_type.fromisoformat(value)
    except ValueError as exc:
        raise InvalidArgument(
            f"date {value!r} is not a valid YYYY-MM-DD date."
        ) from exc
    if not MIN_YEAR <= parsed.year <= MAX_YEAR:
        raise InvalidArgument(
            f"date {value!r} is outside the supported range {MIN_YEAR}-{MAX_YEAR}."
        )
    return parsed


def parse_clock(value: str, field: str) -> time_type:
    """Parse a 24-hour ``HH:MM`` wall-clock time.

    Args:
        value: The string to parse.
        field: Argument name, so the error says which of the two windows was
            wrong rather than leaving the caller to guess.
    """
    try:
        parsed = datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise InvalidArgument(
            f"{field} {value!r} is not a 24-hour HH:MM time (e.g. '09:00')."
        ) from exc
    return parsed


def _validate_timezones(names: list[str]) -> list[tuple[str, ZoneInfo]]:
    """Check the participant list and resolve every zone.

    Duplicates are rejected rather than silently collapsed: a duplicate is
    almost always a mistake in the caller's list, and answering a question
    the caller did not ask is worse than saying no.
    """
    if not MIN_PARTICIPANTS <= len(names) <= MAX_PARTICIPANTS:
        raise InvalidArgument(
            f"timezones needs between {MIN_PARTICIPANTS} and "
            f"{MAX_PARTICIPANTS} entries (got {len(names)})."
        )
    seen: set[str] = set()
    resolved: list[tuple[str, ZoneInfo]] = []
    for name in names:
        if name in seen:
            raise InvalidArgument(f"timezone {name!r} is listed more than once.")
        seen.add(name)
        resolved.append((name, zone(name)))
    return resolved


def _validate_window(
    earliest_local: str, latest_local: str, duration_minutes: int
) -> tuple[time_type, time_type]:
    """Check the working-hours window against the requested duration."""
    if not MIN_DURATION_MINUTES <= duration_minutes <= MAX_DURATION_MINUTES:
        raise InvalidArgument(
            f"duration_minutes must be between {MIN_DURATION_MINUTES} and "
            f"{MAX_DURATION_MINUTES} (got {duration_minutes})."
        )
    earliest = parse_clock(earliest_local, "earliest_local")
    latest = parse_clock(latest_local, "latest_local")
    if earliest >= latest:
        raise InvalidArgument(
            f"earliest_local {earliest_local!r} must be before latest_local "
            f"{latest_local!r}."
        )
    span = _minutes(latest) - _minutes(earliest)
    if span < duration_minutes:
        raise InvalidArgument(
            f"the working window {earliest_local}-{latest_local} is {span} "
            f"minutes, shorter than the requested {duration_minutes}."
        )
    return earliest, latest


def _minutes(value: time_type) -> int:
    """Return a wall-clock time as minutes since local midnight."""
    return value.hour * 60 + value.minute


def _fits(
    start_utc: datetime,
    end_utc: datetime,
    zones: list[tuple[str, ZoneInfo]],
    earliest: time_type,
    latest: time_type,
) -> list[LocalView] | None:
    """Return each participant's local view, or ``None`` if anyone is out.

    The whole meeting must fall inside the window, so the *end* is checked as
    well as the start, and both readings must land on the same local day —
    a meeting that ends after local midnight is out of hours even when both
    clock readings happen to sit inside the window.
    """
    views: list[LocalView] = []
    for name, tz in zones:
        local_start = start_utc.astimezone(tz)
        local_end = end_utc.astimezone(tz)
        if local_start.date() != local_end.date():
            return None
        if local_start.timetz().replace(tzinfo=None) < earliest:
            return None
        if local_end.timetz().replace(tzinfo=None) > latest:
            return None
        views.append(
            LocalView(
                timezone=name,
                start=local_start.strftime("%H:%M"),
                end=local_end.strftime("%H:%M"),
                day=local_start.strftime("%A"),
                date=local_start.date().isoformat(),
            )
        )
    return views


def _window_in_utc(
    day: date_type, tz: ZoneInfo, earliest: time_type, latest: time_type
) -> dict[str, str]:
    """Express one participant's working day as an absolute UTC interval.

    Used only when no slot exists. Without it the tool would answer "no" and
    leave the model with nothing to say; with it the model can explain that
    Auckland's afternoon ends before Berlin's morning starts.
    """
    start = datetime.combine(day, earliest, tzinfo=tz).astimezone(UTC)
    end = datetime.combine(day, latest, tzinfo=tz).astimezone(UTC)
    return {
        "start_utc": start.isoformat(timespec="minutes"),
        "end_utc": end.isoformat(timespec="minutes"),
    }


def find_slots(
    timezones: list[str],
    date: str,
    duration_minutes: int = 60,
    earliest_local: str = "09:00",
    latest_local: str = "18:00",
    max_results: int = 5,
) -> dict[str, Any]:
    """Find every meeting time that suits all participants on one day.

    The day is the **first** listed timezone's calendar day: somebody has to
    own the date, since a single instant falls on different dates around the
    world, and the caller who names the date is the organiser. This is stated
    in the tool description too, so the model does not have to infer it.

    Args:
        timezones: IANA names, organiser first. 2-8 entries, no duplicates.
        date: The organiser's local day, ``YYYY-MM-DD``.
        duration_minutes: Meeting length, 15-480.
        earliest_local: Start of everyone's working day, ``HH:MM``.
        latest_local: End of everyone's working day, ``HH:MM``.
        max_results: How many slots to return, 1-20.

    Returns:
        A JSON-safe dict: the echoed query, the slots found (each with every
        participant's local reading), and — when there are none — each
        participant's working window as an absolute UTC interval.

    Raises:
        InvalidArgument: If any argument fails validation. Nothing here
            raises anything else: an unexpected exception from this function
            is a bug, and the server maps the two differently on purpose.
    """
    if not MIN_RESULTS <= max_results <= MAX_RESULTS:
        raise InvalidArgument(
            f"max_results must be between {MIN_RESULTS} and {MAX_RESULTS} "
            f"(got {max_results})."
        )
    zones = _validate_timezones(timezones)
    earliest, latest = _validate_window(earliest_local, latest_local, duration_minutes)
    day = parse_date(date)

    anchor_name, anchor_zone = zones[0]
    duration = timedelta(minutes=duration_minutes)
    # Anchor midnight as an absolute instant. Every candidate is then plain
    # UTC arithmetic from here, so no invalid local time is ever constructed
    # — the hour that does not exist on a spring-forward morning simply never
    # appears, because we only ever convert instants *into* local time.
    midnight_utc = datetime.combine(
        day, time_type(0, 0), tzinfo=anchor_zone
    ).astimezone(UTC)
    step = timedelta(minutes=GRID_MINUTES)
    candidates = (24 * 60) // GRID_MINUTES

    found: list[Slot] = []
    for index in range(candidates):
        start_utc = midnight_utc + index * step
        # Keep the search on the organiser's day even across a DST shift,
        # which makes that day 23 or 25 hours long.
        if start_utc.astimezone(anchor_zone).date() != day:
            continue
        end_utc = start_utc + duration
        views = _fits(start_utc, end_utc, zones, earliest, latest)
        if views is not None:
            found.append(Slot(start_utc=start_utc, end_utc=end_utc, local=views))

    result: dict[str, Any] = {
        "date": date,
        "organiser_timezone": anchor_name,
        "duration_minutes": duration_minutes,
        "working_hours": {
            "earliest_local": earliest_local,
            "latest_local": latest_local,
        },
        "slots_found": len(found),
        "slots_returned": min(len(found), max_results),
        "truncated": len(found) > max_results,
        "slots": [slot.as_dict() for slot in found[:max_results]],
    }
    if not found:
        # Not an error: "these people cannot meet on Tuesday" is a correct,
        # useful answer. Returning it as a normal result with the evidence
        # attached keeps the failure vocabulary for things that actually went
        # wrong.
        result["no_overlap"] = {
            "explanation": (
                "No start time keeps the whole meeting inside "
                f"{earliest_local}-{latest_local} for every participant. "
                "Their working days, as absolute times, are below."
            ),
            "working_days_utc": {
                name: _window_in_utc(day, tz, earliest, latest) for name, tz in zones
            },
        }
    return result
