"""Build and validate the inline <schedule> XML Tableau Cloud expects when
creating a subscription or extract-refresh task.

Tableau Cloud has no server-wide schedules (unlike Tableau Server) - the
frequency is defined inline on the create request. The valid shapes per
frequency, per the Tableau REST API reference:

  Hourly  - one interval: hours="1" or minutes="60"; optional weekDay
            intervals; start/end required.
  Daily   - one interval: hours in {2, 4, 6, 8, 12, 24}; optional weekDay
            intervals; end required unless hours == 24.
  Weekly  - exactly one weekDay interval; no start/end.
  Monthly - one interval: either monthDay (1-31 or "LastDay"), or an
            occurrence pairing like monthDay="Third" + weekDay="Thursday".

start/end are "HH:MM:SS", must fall on 5-minute boundaries, and the
difference between them must be a multiple of 60 minutes.
"""

from __future__ import annotations

from datetime import datetime

VALID_FREQUENCIES = {"Hourly", "Daily", "Weekly", "Monthly"}
VALID_DAILY_HOURS = {2, 4, 6, 8, 12, 24}
VALID_WEEKDAYS = {
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
}
VALID_MONTH_OCCURRENCES = {"First", "Second", "Third", "Fourth", "Last"}


class ScheduleError(ValueError):
    pass


def _parse_time(value: str, field: str) -> datetime:
    try:
        return datetime.strptime(value, "%H:%M:%S")
    except (TypeError, ValueError):
        raise ScheduleError(f'{field} must be "HH:MM:SS", got {value!r}')


def _check_five_minute_boundary(t: datetime, field: str) -> None:
    if t.minute % 5 != 0 or t.second != 0:
        raise ScheduleError(f"{field} must be on a 5-minute boundary")


def build_schedule_xml(payload: dict) -> str:
    """Validate `payload` and return the `<schedule>...</schedule>` XML string.

    Expected shape (fields depend on frequency):
      {
        "frequency": "Hourly" | "Daily" | "Weekly" | "Monthly",
        "start": "HH:MM:SS",            # Hourly, Daily
        "end": "HH:MM:SS",               # Hourly, Daily (<24h)
        "hours": 4,                      # Hourly (1) or Daily (2/4/6/8/12/24)
        "minutes": 60,                    # Hourly, alternative to hours=1
        "week_days": ["Monday", ...],    # Hourly, Daily (optional extra intervals)
        "week_day": "Monday",             # Weekly; or Monthly occurrence pairing
        "month_day": "15" | "LastDay",   # Monthly
        "month_occurrence": "Third",      # Monthly, paired with week_day
      }
    """
    frequency = payload.get("frequency")
    if frequency not in VALID_FREQUENCIES:
        raise ScheduleError(f"frequency must be one of {sorted(VALID_FREQUENCIES)}")

    intervals_xml = ""
    attrs = ""

    if frequency == "Hourly":
        start, end = _require_start_end(payload, require_end=True)
        attrs = f'start="{start}" end="{end}"'
        intervals_xml = _hourly_intervals(payload)
    elif frequency == "Daily":
        hours = payload.get("hours")
        if hours not in VALID_DAILY_HOURS:
            raise ScheduleError(f"Daily hours must be one of {sorted(VALID_DAILY_HOURS)}")
        if hours == 24:
            start, _ = _require_start_end(payload, require_end=False)
            attrs = f'start="{start}"'
        else:
            start, end = _require_start_end(payload, require_end=True)
            attrs = f'start="{start}" end="{end}"'
        intervals_xml = f'<interval hours="{hours}"/>' + _weekday_intervals(payload.get("week_days"))
    elif frequency == "Weekly":
        week_day = payload.get("week_day")
        if week_day not in VALID_WEEKDAYS:
            raise ScheduleError(f"Weekly week_day must be one of {sorted(VALID_WEEKDAYS)}")
        intervals_xml = f'<interval weekDay="{week_day}"/>'
    elif frequency == "Monthly":
        intervals_xml = _monthly_interval(payload)

    frequency_details = (
        f"<frequencyDetails {attrs}><intervals>{intervals_xml}</intervals></frequencyDetails>"
        if attrs
        else f"<frequencyDetails><intervals>{intervals_xml}</intervals></frequencyDetails>"
    )
    return f'<schedule frequency="{frequency}">{frequency_details}</schedule>'


def _require_start_end(payload: dict, *, require_end: bool) -> tuple[str, str | None]:
    start = payload.get("start")
    end = payload.get("end")
    if not start:
        raise ScheduleError("start is required for this frequency")
    start_t = _parse_time(start, "start")
    _check_five_minute_boundary(start_t, "start")

    if require_end:
        if not end:
            raise ScheduleError("end is required for this frequency")
        end_t = _parse_time(end, "end")
        _check_five_minute_boundary(end_t, "end")
        diff_minutes = (end_t - start_t).total_seconds() / 60
        if diff_minutes <= 0 or diff_minutes % 60 != 0:
            raise ScheduleError("end minus start must be a positive multiple of 60 minutes")
    return start, end


def _hourly_intervals(payload: dict) -> str:
    hours = payload.get("hours")
    minutes = payload.get("minutes")
    if hours == 1:
        base = '<interval hours="1"/>'
    elif minutes == 60:
        base = '<interval minutes="60"/>'
    else:
        raise ScheduleError('Hourly requires hours=1 or minutes=60')
    return base + _weekday_intervals(payload.get("week_days"))


def _weekday_intervals(week_days: list[str] | None) -> str:
    if not week_days:
        return ""
    for day in week_days:
        if day not in VALID_WEEKDAYS:
            raise ScheduleError(f"Invalid weekDay: {day!r}")
    return "".join(f'<interval weekDay="{d}"/>' for d in week_days)


def _monthly_interval(payload: dict) -> str:
    month_day = payload.get("month_day")
    occurrence = payload.get("month_occurrence")
    week_day = payload.get("week_day")

    if month_day:
        if month_day != "LastDay":
            try:
                day_num = int(month_day)
            except ValueError:
                raise ScheduleError('month_day must be 1-31 or "LastDay"')
            if not (1 <= day_num <= 31):
                raise ScheduleError('month_day must be 1-31 or "LastDay"')
        return f'<interval monthDay="{month_day}"/>'

    if occurrence and week_day:
        if occurrence not in VALID_MONTH_OCCURRENCES:
            raise ScheduleError(f"month_occurrence must be one of {sorted(VALID_MONTH_OCCURRENCES)}")
        if week_day not in VALID_WEEKDAYS:
            raise ScheduleError(f"week_day must be one of {sorted(VALID_WEEKDAYS)}")
        return f'<interval monthDay="{occurrence}" weekDay="{week_day}"/>'

    raise ScheduleError(
        "Monthly requires either month_day, or month_occurrence together with week_day"
    )
