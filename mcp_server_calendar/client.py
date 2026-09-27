"""The ICS calendar client, forked from upstream's ``calendar.py``.

Forked from `adamchrabaszcz/ics-calendar-mcp` (MIT). The query methods keep
upstream's names and return shapes — :meth:`get_events_between`,
:meth:`get_events_today`, :meth:`get_event_by_uid`,
:meth:`get_recurring_events`, :meth:`search_events` — because those are what
the tool surface is built on, and a fork that renamed them would be a
rewrite wearing a NOTICE file.

Recurrence expansion is upstream's and is the single best reason to have
forked rather than written this: ``recurring_ical_events.of(cal).between()``
turns one ``RRULE:FREQ=WEEKLY;BYDAY=MO`` into an instance on every Monday in
the window. Getting that right by hand means implementing RFC 5545's
recurrence rules — ``BYDAY``, ``BYSETPOS``, ``UNTIL``, ``COUNT``, ``EXDATE``
— which is a project, not a function.

Three deviations from upstream, all in ADR-071:

**The source is a local file, not a URL.** Upstream requires
``ICS_CALENDAR_URL`` and fetches it with ``httpx``. Ours reads a path. That
removes the last credential — a private ICS feed URL *is* a secret, and
§8.2 forbids committing those — and it removes network egress from a server
that otherwise needs none, so the SSRF guard the web server carries
(ADR-056) does not have to be duplicated here.

**Synchronous.** Upstream is ``async`` because it makes an HTTP request.
Reading a file has nothing to await, and an ``async`` method that never
yields is a lie about what it does.

**No singleton.** Upstream keeps a module-level ``_client`` with
``get_client`` / ``reset_client`` / ``set_client`` around it — three
functions that exist to undo the global in tests. The server builds a client
and holds it, so the global is not needed and neither are its undo hatches.
"""

import time as time_module
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import recurring_ical_events
from icalendar import Calendar

from mcp_server_calendar.formatting import (
    DEFAULT_TIMEZONE,
    InvalidArgument,
    format_event,
    format_event_short,
    format_recurring_event,
)

# Upstream caches the fetched feed for five minutes to spare the remote
# server. Ours re-reads when the file looks different instead: the file is
# local, so reading it is cheap, and a cache would mean an event the agent
# just created is invisible for five minutes — which on a demo looks
# exactly like the write having failed.
#
# "Looks different" is modification time *and* size, and our own writes call
# ``invalidate`` rather than relying on either. See ``fetch_calendar``: on a
# filesystem whose mtime advances once a second, two writes in one tick are
# indistinguishable, and the reader keeps serving the older file (ADR-091).
CACHE_BY_MTIME = True

# Bounds, mirrored into the JSON Schema so the model is told them rather
# than discovering them through a failed call. Upstream has none: its
# ``search_events`` defaults to a 365-day window and its range query has no
# ceiling at all.
MAX_EVENTS_RETURNED = 100
MAX_RANGE_DAYS = 366

# How far either side of a new event to look for one with the same title
# before treating it as a duplicate (ADR-091). A fortnight is the span a
# "move it to next week" lands in; a quarterly review is not a duplicate of
# the last one.
DUPLICATE_WINDOW_DAYS = 14

# Ceiling on the calendar file itself. It lives on a volume that outlives
# the container and nothing here prunes it, so a read has to have an end.
MAX_FILE_BYTES = 5_000_000


class ICSCalendarClient:
    """Reads events out of one iCalendar file."""

    def __init__(
        self,
        path: Path,
        timezone: str = DEFAULT_TIMEZONE,
        max_file_bytes: int = MAX_FILE_BYTES,
    ) -> None:
        """Args:
        path: The ``.ics`` file. Created empty if absent, because an
            empty calendar is the normal state of a fresh install and
            refusing to start over it would break the server exactly
            when someone first tries it.
        timezone: Zone events are reported in and naive times are read
            as.
        max_file_bytes: Ceiling on the file read.
        """
        self.path = path
        self.timezone = timezone
        self._max_file_bytes = max_file_bytes
        self._calendar: Calendar | None = None
        self._loaded_stamp: tuple[float, int] | None = None

    # -- loading ------------------------------------------------------------

    def invalidate(self) -> None:
        """Forget the cached calendar, so the next read goes to disk.

        Called by whoever just wrote through :mod:`mcp_server_calendar.writer`.
        The cache below is a *heuristic* — it asks the filesystem whether
        anything looks different — and a writer in the same process does not
        have to guess: it knows (ADR-091).
        """
        self._calendar = None
        self._loaded_stamp = None

    def fetch_calendar(self, force: bool = False) -> Calendar:
        """Return the parsed calendar, re-reading when the file changed.

        Upstream's method name and caching intent, with the file's own state
        in place of a five-minute TTL.

        **Modification time is not enough on its own, and that is not
        theoretical.** Several filesystems advance ``st_mtime`` only once a
        second — a GitLab runner's is one, a Mac's is not, which is exactly
        the pair that makes this look like a CI-only problem. Two writes
        inside one tick then share a timestamp, and a reader that had looked
        between them keeps serving the older file forever. Size is compared
        as well because it is free and it catches the appends this
        calendar's only writer makes; :meth:`invalidate` is what actually
        guarantees correctness for our own writes (ADR-091).

        Raises:
            InvalidArgument: If the file is too large or is not parseable
                iCalendar. Upstream lets both escape as a bare exception
                that the tool converts into a successful-looking result.
        """
        if not self.path.exists():
            return Calendar()

        try:
            stat = self.path.stat()
        except OSError as exc:
            raise InvalidArgument(f"cannot read the calendar file: {exc}") from exc
        size = stat.st_size
        stamp = (stat.st_mtime, size)

        if size > self._max_file_bytes:
            raise InvalidArgument(
                f"the calendar file is {size} bytes, over the "
                f"{self._max_file_bytes}-byte limit"
            )
        if not force and self._calendar is not None and self._loaded_stamp == stamp:
            return self._calendar

        try:
            # utf-8-sig, not utf-8: RFC 5545 says UTF-8, but Outlook and
            # several Windows exporters write a byte-order mark, and a BOM
            # in front of BEGIN:VCALENDAR is enough to make the parser
            # reject an otherwise good file.
            raw = self.path.read_bytes().decode("utf-8-sig")
            # ``from_ical`` is typed as returning the base ``Component``.
            # Parsing a VCALENDAR gives a ``Calendar``; the cast records
            # that rather than widening the attribute.
            self._calendar = cast(Calendar, Calendar.from_ical(raw))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise InvalidArgument(
                f"the calendar file is not readable iCalendar: {exc}"
            ) from exc
        self._loaded_stamp = stamp
        return self._calendar

    # -- upstream's query surface ------------------------------------------

    def get_events_between(
        self,
        start: date,
        end: date,
        short_format: bool = False,
    ) -> list[dict[str, Any]]:
        """Return events between two dates, recurrences expanded.

        Upstream's signature and return shape. The expansion is
        upstream's too: ``recurring_ical_events`` turns a weekly rule into
        one instance per occurrence in the window.

        Raises:
            InvalidArgument: If the window is inverted or longer than
                :data:`MAX_RANGE_DAYS`. Upstream has no ceiling.
        """
        if end < start:
            raise InvalidArgument("end_date must be on or after start_date")
        if (end - start).days > MAX_RANGE_DAYS:
            raise InvalidArgument(
                f"the window must be at most {MAX_RANGE_DAYS} days "
                f"(got {(end - start).days})"
            )

        calendar = self.fetch_calendar()
        try:
            occurrences = recurring_ical_events.of(calendar).between(start, end)
        except Exception as exc:  # noqa: BLE001 - library errors are open-ended
            raise InvalidArgument(
                f"the calendar could not be expanded over that window: {exc}"
            ) from exc

        formatter = format_event_short if short_format else format_event
        found = [formatter(event, self.timezone) for event in occurrences]
        found.sort(key=lambda event: (event.get("start") or "", event.get("summary")))
        return found[:MAX_EVENTS_RETURNED]

    def get_events_today(self) -> list[dict[str, Any]]:
        """Return today's events. Upstream's method.

        "Today" is read in the configured zone rather than the machine's,
        which is the whole reason ``--local-timezone`` exists: the
        container's clock is UTC and a user in Berlin asking for "today"
        at 00:30 does not mean yesterday.
        """
        today = datetime.now(self._zone()).date()
        return self.get_events_between(today, today + timedelta(days=1))

    def get_event_by_uid(self, uid: str) -> dict[str, Any] | None:
        """Return one event by UID, or ``None``. Upstream's method."""
        wanted = uid.strip()
        if not wanted:
            raise InvalidArgument("event_uid must not be empty")
        for component in self.fetch_calendar().walk("VEVENT"):
            if str(component.get("UID", "")) == wanted:
                return format_event(component, self.timezone)
        return None

    def get_recurring_events(self) -> list[dict[str, Any]]:
        """Return the base recurring events, unexpanded. Upstream's method."""
        found = [
            format_recurring_event(component, self.timezone)
            for component in self.fetch_calendar().walk("VEVENT")
            if component.get("RRULE")
        ]
        found.sort(key=lambda event: str(event.get("summary", "")).lower())
        return found[:MAX_EVENTS_RETURNED]

    def search_events(
        self,
        query: str,
        start: date | None = None,
        end: date | None = None,
    ) -> list[dict[str, Any]]:
        """Return events matching ``query``. Upstream's method and defaults.

        Substring matching over summary, description and location, with the
        window defaulting to a year from today — both upstream's choices.
        """
        needle = query.strip().lower()
        if not needle:
            raise InvalidArgument("query must not be empty")
        if start is None:
            start = datetime.now(self._zone()).date()
        if end is None:
            end = start + timedelta(days=365)

        return [
            event
            for event in self.get_events_between(start, end)
            if needle in str(event.get("summary", "")).lower()
            or needle in str(event.get("description", "")).lower()
            or needle in str(event.get("location", "")).lower()
        ]

    # -- what the fork adds -------------------------------------------------

    def same_summary_near(
        self,
        summary: str,
        moment: datetime,
        window_days: int = DUPLICATE_WINDOW_DAYS,
    ) -> list[dict[str, Any]]:
        """Return events with this exact title within ``window_days``.

        The question ``create_event`` asks before writing: is this a new
        meeting, or a second copy of one that is already here (ADR-091)?

        Exact on the normalised title and nothing else. Matching loosely —
        substrings, fuzzy distance — would start refusing "Standup" because
        "Standup with Bob" exists, and a guard that blocks real work gets
        switched off. Times are deliberately *not* compared: a duplicate of
        an 11:00 meeting written at 15:00 is exactly the case this exists to
        catch, so overlapping is not the test.

        Args:
            summary: The title about to be written.
            moment: When the new event would start; the window is centred
                here rather than on today, because an event is usually
                created for a date in the future.
            window_days: How far either side of ``moment`` to look. A
                fortnight: far enough to catch "move it to next week",
                short enough that a quarterly review is not a duplicate of
                the last one.

        Returns:
            The matching events, oldest first. Empty when there are none.
        """
        wanted = _normalise_summary(summary)
        if not wanted:
            return []
        span = timedelta(days=max(1, window_days))
        found = self.get_events_between((moment - span).date(), (moment + span).date())
        return [
            event
            for event in found
            if _normalise_summary(str(event.get("summary", ""))) == wanted
        ]

    def busy_between(
        self, start: datetime, end: datetime, skip_uid: str | None = None
    ) -> list[tuple[datetime, datetime]]:
        """Return occupied intervals in ``[start, end)``, recurrences included.

        Not upstream's — it has no notion of "busy", which is precisely the
        gap ``suggest_reschedule_slots`` fills (ADR-072). Built on
        upstream's expansion so a weekly standup blocks every week rather
        than only the first one.

        Args:
            start: Window start, aware.
            end: Window end, aware.
            skip_uid: An event to leave out — the one being moved. Leaving
                it in would make its own slot look occupied.

        Returns:
            ``(start, end)`` pairs. All-day events are excluded: a
            ``VALUE=DATE`` entry has no time, so treating it as busy would
            black out a whole working day on a guess.
        """
        blocks: list[tuple[datetime, datetime]] = []
        for event in self.get_events_between(
            start.date(), end.date() + timedelta(days=1)
        ):
            if event.get("all_day"):
                continue
            if skip_uid is not None and event.get("uid") == skip_uid:
                continue
            begins = _as_datetime(event.get("start"))
            finishes = _as_datetime(event.get("end"))
            if begins is None or finishes is None or finishes <= begins:
                continue
            if begins < end and start < finishes:
                blocks.append((begins, finishes))
        return blocks

    def clashes_with(
        self, start: datetime, end: datetime, skip_uid: str
    ) -> list[dict[str, Any]]:
        """Return the events overlapping ``[start, end)``, named.

        Separate from :meth:`busy_between` because the two answer different
        questions and want different shapes. The search only needs *when*
        the hours are taken, so it gets bare intervals. A person asking why
        their meeting has to move needs to know *what* it collides with —
        "Client call" rather than "something at 11:30" — so this keeps the
        formatted event.
        """
        found: list[dict[str, Any]] = []
        for event in self.get_events_between(
            start.date(), end.date() + timedelta(days=1)
        ):
            if event.get("all_day") or event.get("uid") == skip_uid:
                continue
            begins = _as_datetime(event.get("start"))
            finishes = _as_datetime(event.get("end"))
            if begins is None or finishes is None:
                continue
            if begins < end and start < finishes:
                found.append(event)
        return found

    def occurrence_of(self, uid: str, on_or_after: datetime) -> dict[str, Any] | None:
        """Return the next occurrence of ``uid`` at or after a moment.

        Needed because of recurrence, and it is the subtlety upstream's
        ``get_event_by_uid`` hides: that method returns the *base* event,
        which for a weekly standup is the first Monday it ever happened,
        possibly years ago. Rescheduling that instance is meaningless, so
        the reschedule tool asks for the occurrence that is actually
        coming up.
        """
        horizon = (on_or_after + timedelta(days=MAX_RANGE_DAYS - 1)).date()
        for event in self.get_events_between(on_or_after.date(), horizon):
            if event.get("uid") != uid:
                continue
            begins = _as_datetime(event.get("start"))
            if begins is not None and begins >= on_or_after:
                return event
        return None

    def _zone(self) -> Any:
        """Return the configured zone."""
        from mcp_server_calendar.formatting import zone

        return zone(self.timezone)


def _normalise_summary(summary: str) -> str:
    """Return a title in the one form two spellings of it can be compared in.

    Case and runs of whitespace only. A model writing ``"Design  Review"``
    means the same meeting as ``"design review"``, and a guard that missed
    that would be trivially defeated by the shift key.
    """
    return " ".join(summary.split()).casefold()


def _as_datetime(value: Any) -> datetime | None:
    """Parse an ISO string from a formatted event back into a datetime.

    The formatted shape is upstream's and stringifies its times, so a
    caller that needs to compute with them has to parse them back. Kept
    here rather than at the call sites so there is one place that knows
    the round trip exists.
    """
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def now_in(timezone: str) -> datetime:
    """Return the current instant in ``timezone``.

    A function rather than an inline ``datetime.now`` so the server can
    substitute a fixed clock in tests and a reschedule search over a fixed
    calendar gives the same answer on any machine on any day.
    """
    from mcp_server_calendar.formatting import zone

    return datetime.fromtimestamp(time_module.time(), tz=zone(timezone))
