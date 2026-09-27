#!/usr/bin/env python3
"""Read-only FastMCP server for public SuperSaaS resource schedules."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import unescape
from typing import Annotated
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from fastmcp import FastMCP
from pydantic import Field

MAX_DAYS = 366
CHUNK_DAYS = 28
TIMEOUT = 20
MAX_BYTES = 5_000_000
WEEKDAYS = 7
MINUTES_PER_DAY = 1440
SECONDS_PER_DAY = 86400
DAILY_ROUNDING = SECONDS_PER_DAY
SCHEDULE_TLDS = ("nl", "com")
SCHEDULE_SUBDOMAINS = ("www", "m", "d")
SCHEDULE_HOSTS = {"supersaas.nl", "supersaas.com"} | {
    f"{sub}.supersaas.{tld}" for sub in SCHEDULE_SUBDOMAINS for tld in SCHEDULE_TLDS
}
SCHEDULE_LINK = re.compile(
    r"(?:https?:)?//(?:(?:www|m|d)\.)?supersaas\.(nl|com)(/schedule/[^\s\"'<>()?#\[]*)",
    re.IGNORECASE,
)
SCHEDULE_ASSET = re.compile(r"/assets/(resource|capacity|service)-[0-9a-f]{8,}\.js")
TRAILING_PUNCTUATION = ".,;:!?'\""
mcp = FastMCP("supersaas-slots", version="0.2.1")


class ScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class Schedule:
    page_url: str
    resource_id: int
    rp_id: int
    token: int
    bit_prefs: int
    open_times: tuple[int | None, ...]
    starts: tuple[tuple[int, ...], ...]
    duration_seconds: int
    buffer_seconds: int
    add_limit: int
    early_limit: int
    date_only: bool = False
    checkin_minute: int = 0
    checkout_minute: int = 0


def _number(source: str, name: str) -> int:
    match = re.search(r"\b" + re.escape(name) + r"\s*=\s*(\d+)\b", source)
    if not match:
        raise ScheduleError(f"Schedule page does not expose {name}.")
    return int(match.group(1))


def _validate_entry_url(url: str) -> str:
    parts = urlsplit(url)
    if (parts.scheme != "https" or not parts.hostname or parts.username
            or parts.password or parts.port not in (None, 443)):
        raise ScheduleError("Use a public HTTPS URL without credentials or a custom port.")
    return url


def _validate_schedule_url(url: str) -> str:
    _validate_entry_url(url)
    parts = urlsplit(url)
    if parts.hostname not in SCHEDULE_HOSTS:
        raise ScheduleError("Schedule pages live on supersaas.nl or supersaas.com.")
    if not parts.path.startswith("/schedule/"):
        raise ScheduleError("URL must point to a SuperSaaS /schedule/ page.")
    return url


def _get(url: str) -> tuple[bytes, str]:
    req = Request(url, headers={"User-Agent": "supersaas-slots-mcp/0.1", "Accept": "text/html, application/json"})
    try:
        with urlopen(req, timeout=TIMEOUT) as response:
            final_url = response.geturl()
            final = urlsplit(final_url)
            if final.scheme != "https" or final.username or final.password or final.port not in (None, 443):
                raise ScheduleError("The page redirected to an unsafe URL.")
            data = response.read(MAX_BYTES + 1)
    except OSError as exc:
        raise ScheduleError(f"Could not fetch {url}: {exc}") from exc
    if len(data) > MAX_BYTES:
        raise ScheduleError(f"Response exceeded {MAX_BYTES // 1000000} MB.")
    return data, final_url


def _canonical_schedule_url(url: str) -> str:
    """Reduce any SuperSaaS host to the ``www`` form and drop view parameters.

    ``www``, ``m`` (mobile) and ``d`` serve the same configuration, so one
    schedule can be written several ways. Canonicalising keeps a page that
    links to the same schedule twice from looking ambiguous.
    """
    parts = urlsplit(url)
    tld = parts.hostname.rsplit(".", 1)[1]
    path = parts.path
    if len(path) > len("/schedule/") and path.endswith("/"):
        path = path.rstrip("/")
    return f"https://www.supersaas.{tld}{path}"


def find_schedule_links(html: str) -> tuple[str, ...]:
    """Collect distinct SuperSaaS schedule URLs mentioned in a page."""
    links = []
    for tld, path in SCHEDULE_LINK.findall(unescape(html)):
        url = _canonical_schedule_url(
            f"https://www.supersaas.{tld.lower()}{path.rstrip(TRAILING_PUNCTUATION)}")
        if url not in links:
            links.append(url)
    return tuple(links)


def resolve_schedule_url(url: str) -> tuple[str, str | None]:
    """Return the schedule URL for a SuperSaaS or third-party page.

    Third-party pages such as a salon website are fetched and scanned for a
    link to a SuperSaaS schedule. The second result is that page's URL when a
    link was followed, otherwise ``None``.
    """
    _validate_entry_url(url)
    if urlsplit(url).hostname in SCHEDULE_HOSTS:
        return _canonical_schedule_url(_validate_schedule_url(url)), None
    body, page_url = _get(url)
    links = find_schedule_links(body.decode("utf-8", "replace"))
    if not links:
        raise ScheduleError("No link to a supersaas.nl or supersaas.com /schedule/ page here.")
    if len(links) > 1:
        raise ScheduleError(
            "Page links to several schedules; pass the intended schedule URL directly: "
            + ", ".join(links)
        )
    return _canonical_schedule_url(_validate_schedule_url(links[0])), page_url


def load_schedule(url: str) -> tuple[Schedule, str]:
    _validate_schedule_url(url)
    body, final_url = _get(url)
    if urlsplit(final_url).hostname not in SCHEDULE_HOSTS:
        raise ScheduleError("SuperSaaS redirected to an unexpected host.")
    return parse_schedule(body.decode("utf-8"), final_url), final_url


def _weekday(day: date) -> int:
    """SuperSaaS indexes weekdays and ``bit_prefs`` bits from Sunday = 0."""
    return (day.weekday() + 1) % 7


def _constraint_list(text: str) -> tuple[int, ...]:
    """Expand one whitespace-separated start-time list.

    SuperSaaS writes a positive token as a minute-of-day offset and a zero or
    negative token as a repeating grid: ``0`` is hourly, ``-30`` every half
    hour past the half hour.
    """
    minutes = []
    for token in text.split():
        if not re.fullmatch(r"-?\d+", token) or abs(int(token)) > MINUTES_PER_DAY:
            raise ScheduleError(f"Unparseable start-time constraint {token!r}.")
        value = int(token)
        if value > 0:
            minutes.append(value % MINUTES_PER_DAY)
        else:
            minutes.extend(-value + 60 * offset for offset in range(24))
    return tuple(sorted(set(minutes)))


def parse_constraints(text: str, rounding: int) -> tuple[tuple[int, ...], ...]:
    """Port of the SuperSaaS ``precalc_constraints()`` page script.

    Returns one start-minute tuple per weekday, indexed like ``open_times`` and
    ``bit_prefs`` so element 0 is Sunday. The wire format is
    ``"<base>[:<mask>=<list> ...]"`` where ``<mask>`` is a weekday bitmask.
    """
    base_text, *overrides = text.split(":")
    base = _constraint_list(base_text)
    if not base:
        raise ScheduleError("Schedule declares no start times.")
    if rounding == DAILY_ROUNDING:
        return tuple(base[1:2] or base[:1] for _ in range(WEEKDAYS))
    days = [base] * WEEKDAYS
    for override in overrides:
        mask_text, separator, list_text = override.partition("=")
        if not separator or not re.fullmatch(r"\d+", mask_text.strip()):
            raise ScheduleError("Malformed per-weekday start-time constraint.")
        values = _constraint_list(list_text)
        mask, position = int(mask_text), 0
        while mask:
            if position >= WEEKDAYS:
                raise ScheduleError("Per-weekday start-time mask spans more than 7 days.")
            if mask & 1:
                days[position] = values
            mask >>= 1
            position += 1
    return tuple(days)


def _clock(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _night_times(text: str) -> tuple[int, int]:
    """Return (check-in, check-out) minutes of day for a date-only schedule.

    SuperSaaS stores both in the two start-time constraints of a daily-rounded
    page: the later value is check-in and the earlier one is next-morning
    check-out, so ``'720 840'`` means arrive 14:00 and leave 12:00. A single
    value makes the unit a full 24 hours.
    """
    values = _constraint_list(text)
    if len(values) == 1:
        return values[0], values[0]
    if len(values) != 2:
        raise ScheduleError(
            f"Cannot derive check-in and check-out from {len(values)} start times; "
            "a date-only schedule declares one or two."
        )
    return values[1], values[0]


def _missing_constraints_error(source: str) -> ScheduleError:
    kinds = set(SCHEDULE_ASSET.findall(source))
    if kinds and "resource" not in kinds:
        label = "capacity" if "capacity" in kinds else "service"
        what = ("seats per class or group event" if label == "capacity"
                else "several services booked across shared staff")
        return ScheduleError(
            f"This is a {label} schedule, which tracks {what} through a different "
            "endpoint than resource schedules; only resource schedules are supported."
        )
    if not re.search(r"\bopen_times\s*=", source):
        return ScheduleError(
            "This schedule publishes no availability; SuperSaaS keeps it behind a login."
        )
    return ScheduleError(
        "This schedule publishes opening hours but no start times; only resource "
        "schedules with explicit start times are supported."
    )


def parse_schedule(html: str, page_url: str) -> Schedule:
    _validate_schedule_url(page_url)
    source = unescape(html)
    start_match = re.search(r"\bstart\s*=\s*precalc_constraints\(\s*(['\"])(.*?)\1\s*\)", source, re.S)
    if not start_match:
        raise _missing_constraints_error(source)
    rounding = re.search(r"\brounding\s*=\s*(\d+)", source)
    rounding = int(rounding.group(1)) if rounding else 0
    constraint_text = start_match.group(2)
    if not constraint_text.strip():
        raise ScheduleError(
            "This resource schedule declares no start-time grid; SuperSaaS derives its "
            "slots from the times the visitor picks, which the server cannot replay."
        )
    starts = parse_constraints(constraint_text, rounding)
    open_match = re.search(r"\bopen_times\s*=\s*(\[[^\]]*\])", source, re.S)
    if not open_match:
        raise ScheduleError("Schedule page does not expose open_times.")
    try:
        open_times = json.loads(open_match.group(1))
    except json.JSONDecodeError as exc:
        raise ScheduleError("Cannot parse open_times.") from exc
    if not isinstance(open_times, list) or len(open_times) < 14 or len(open_times) > 28:
        raise ScheduleError("Unsupported open_times layout.")
    if any(x is not None and (type(x) is not int or x < 0 or x > MINUTES_PER_DAY) for x in open_times):
        raise ScheduleError("Invalid open_times value.")
    open_times += [None] * (28 - len(open_times))
    if re.search(r"\bcomplex\s*=\s*[1-9]", source) or re.search(r"\bsync\s*=\s*true", source):
        raise ScheduleError("Linked or synchronized schedules are not supported.")
    if re.search(r"\bcluster\s*=\s*[1-9]", source):
        raise ScheduleError("Cluster booking is not supported.")
    resource_id = _number(source, "filter")
    if resource_id == 0:
        raise ScheduleError(
            "This page asks the visitor to pick a resource; pass one resource URL instead."
        )
    if not re.search(r"resource\[" + str(resource_id) + r"\]", source):
        raise ScheduleError("Could not identify a single resource on this page.")
    duration = _number(source, "default_length")
    date_only = rounding == DAILY_ROUNDING and duration >= DAILY_ROUNDING
    if date_only:
        checkin, checkout = _night_times(constraint_text)
        starts = ((checkin,),) * WEEKDAYS
    elif duration >= DAILY_ROUNDING:
        raise ScheduleError(
            "This schedule uses 24-hour units without daily rounding, which "
            "SuperSaaS does not combine; only sub-day durations are supported."
        )
    if duration <= 0:
        raise ScheduleError("Unsupported appointment duration.")
    # On the observed resource page `buffer` is in minutes.
    buffer_minutes = _number(source, "buffer")
    if buffer_minutes < 0 or buffer_minutes > MINUTES_PER_DAY:
        raise ScheduleError("Invalid buffer.")
    return Schedule(
        page_url=page_url, resource_id=resource_id,
        rp_id=_number(source, "rp_id"), token=_number(source, "token"),
        bit_prefs=_number(source, "bit_prefs"), open_times=tuple(open_times),
        starts=starts, duration_seconds=duration, buffer_seconds=buffer_minutes * 60,
        add_limit=_number(source, "add_limit"), early_limit=_number(source, "early_limit"),
        date_only=date_only, checkin_minute=checkin if date_only else 0,
        checkout_minute=checkout if date_only else 0,
    )


def ajax_url(schedule: Schedule, start: date, stop: date) -> str:
    values = {
        "v": "12", "token": str(schedule.token),
        "afrom": start.isoformat() + " 00:00", "ato": stop.isoformat() + " 00:00",
        # SuperSaaS selects exceptions by their START date, even when a blocked
        # range began before `start` and still overlaps the requested dates.
        "ad": "r", "efrom": "1970-01-01", "eto": stop.isoformat(), "ed": "r",
    }
    origin = urlsplit(schedule.page_url)
    return f"{origin.scheme}://{origin.netloc}/ajax/resource/{schedule.rp_id}?{urlencode(values)}"


def _periods(schedule: Schedule, day: date, exceptions: list[list]) -> list[tuple[int, int]]:
    day_start = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    if any(not isinstance(row, list) or len(row) < 3 for row in exceptions):
        raise ScheduleError("Malformed exception in AJAX response.")
    relevant = [row for row in exceptions if row[0] < day_start + SECONDS_PER_DAY and row[1] > day_start]
    if any(row[2] not in (0, 1) or (row[2] == 1 and len(row) < 5) for row in relevant):
        raise ScheduleError(f"Unsupported exception on {day}.")
    if any(row[2] == 0 for row in relevant):
        return []
    weekday = _weekday(day)
    periods = []
    if schedule.bit_prefs & (1 << weekday):
        for offset in (0, 14):
            a = schedule.open_times[offset + weekday]
            b = schedule.open_times[offset + 7 + weekday]
            if a is not None and b is not None and a < b:
                periods.append((a, b))
    for row in relevant:
        if row[2] == 1:
            a, b = row[3], row[4]
            if type(a) is not int or type(b) is not int or not (0 <= a < b <= MINUTES_PER_DAY):
                raise ScheduleError(f"Invalid opening exception on {day}.")
            periods.append((a, b))
    return periods


def calculate_slots(schedule: Schedule, data: dict, start: date, stop: date,
                    *, now: datetime | None = None, respect_booking_window: bool = True) -> list[dict]:
    """Calculate slots in [start, stop), interpreting page epochs as UTC wall time."""
    apps = data.get("app", [])
    exceptions = data.get("exc", [])
    if not isinstance(apps, list) or not isinstance(exceptions, list):
        raise ScheduleError("AJAX response lacks appointment or exception arrays.")
    if any(not isinstance(row, list) or len(row) < 3 for row in apps):
        raise ScheduleError("Malformed appointment in AJAX response.")
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ScheduleError("now must be timezone-aware.")
    now_s = now.timestamp()
    slots = []
    day = start
    while day < stop:
        midnight = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
        periods = _periods(schedule, day, exceptions)
        for minute in schedule.starts[_weekday(day)]:
            begin = midnight + minute * 60
            if schedule.date_only:
                # A night runs from check-in on this date to check-out the next,
                # so it only needs the check-in date to be an open day.
                if not periods:
                    continue
                finish = midnight + SECONDS_PER_DAY + schedule.checkout_minute * 60
            else:
                finish = begin + schedule.duration_seconds
                end_minute = minute + schedule.duration_seconds / 60
                if not any(a <= minute and end_minute <= b for a, b in periods):
                    continue
            if respect_booking_window:
                # SuperSaaS treats a zero limit as "no limit"; see the truthiness
                # guards around add_limit and early_limit in its own page script.
                if schedule.add_limit and begin < now_s + schedule.add_limit:
                    continue
                if schedule.early_limit and begin > now_s + schedule.early_limit:
                    continue
            if any(len(row) >= 3 and row[2] == schedule.resource_id and
                   begin < row[1] + schedule.buffer_seconds and
                   finish + schedule.buffer_seconds > row[0] for row in apps):
                continue
            slots.append({
                "start": datetime.fromtimestamp(begin, timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "end": datetime.fromtimestamp(finish, timezone.utc).strftime("%Y-%m-%d %H:%M"),
            })
        day += timedelta(days=1)
    return slots


def merge_stays(slots: list[dict]) -> list[dict]:
    """Collapse consecutive free nights into bookable stays.

    Nights are contiguous when the next check-in date equals the current
    check-out date, so four free nights become one stay of four nights.
    """
    stays: list[dict] = []
    for slot in slots:
        check_in, check_out = slot["start"][:10], slot["end"][:10]
        if stays and stays[-1]["end"] == check_in:
            stays[-1]["end"] = check_out
            stays[-1]["nights"] += 1
        else:
            stays.append({"start": check_in, "end": check_out, "nights": 1})
    return stays


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def find_available_slots(
    schedule_url: Annotated[str, Field(
        description="Public HTTPS SuperSaaS /schedule/ URL, or a business page linking to one"
    )],
    from_date: Annotated[str, Field(description="First date, YYYY-MM-DD")],
    through_date: Annotated[str, Field(description="Last date, inclusive, YYYY-MM-DD")],
    max_results: Annotated[int, Field(description="Maximum number of slots returned", ge=1, le=2000)] = 500,
    respect_booking_window: Annotated[bool, Field(description="Apply minimum and maximum advance-booking limits")] = True,
) -> dict:
    """List available slots in a public, single-resource SuperSaaS schedule.

    Accepts a SuperSaaS schedule URL directly, or any public HTTPS page that
    links to exactly one schedule on supersaas.nl or supersaas.com, such as a
    salon's own booking page. Supports explicit, repeating-grid, and per-weekday
    start times, fixed duration, weekly opening hours, exceptions, booked
    appointments, buffer time, and booking-window limits. Date-only schedules
    such as nightly rentals report whole nights from check-in to check-out and
    add a `stays` summary of merged consecutive nights. Unsupported schedule
    rules are reported as errors.
    """
    try:
        start = date.fromisoformat(from_date)
        through = date.fromisoformat(through_date)
    except ValueError as exc:
        raise ScheduleError("Dates must be YYYY-MM-DD.") from exc
    stop = through + timedelta(days=1)
    days = (stop - start).days
    if not 1 <= days <= MAX_DAYS:
        raise ScheduleError(f"Date range must contain 1 to {MAX_DAYS} days.")
    if type(max_results) is not int or not 1 <= max_results <= 2000:
        raise ScheduleError("max_results must be an integer from 1 to 2000.")
    resolved_url, linked_from = resolve_schedule_url(schedule_url)
    schedule, final_url = load_schedule(resolved_url)
    all_slots = []
    cursor = start
    now = datetime.now(timezone.utc)
    while cursor < stop:
        chunk_end = min(cursor + timedelta(days=CHUNK_DAYS), stop)
        body, _ = _get(ajax_url(schedule, cursor, chunk_end))
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ScheduleError("SuperSaaS AJAX response was not JSON.") from exc
        all_slots.extend(calculate_slots(schedule, data, cursor, chunk_end,
                                         now=now, respect_booking_window=respect_booking_window))
        cursor = chunk_end
    result = {
        "schedule_url": final_url, "from_date": from_date, "through_date": through_date,
        "time_basis": "schedule wall time; epoch values interpreted as UTC",
        "unit": "night" if schedule.date_only else "slot",
        "duration_minutes": schedule.duration_seconds // 60,
        "count": len(all_slots), "truncated": len(all_slots) > max_results,
        "slots": all_slots[:max_results],
    }
    if schedule.date_only:
        result["check_in"] = _clock(schedule.checkin_minute)
        result["check_out"] = _clock(schedule.checkout_minute)
        result["stays"] = merge_stays(all_slots)
    if linked_from:
        result["linked_from"] = linked_from
    return result


def main() -> None:
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
