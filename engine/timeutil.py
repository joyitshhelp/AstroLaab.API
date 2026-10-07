"""Local birth time -> UTC with explicit handling of daylight-saving edge cases."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SUPPORTED_MIN_YEAR = 1950
SUPPORTED_MAX_YEAR = 2050


class TimeInputError(ValueError):
    """The supplied birth date/time/timezone cannot be interpreted."""


def _offset_minutes(dt: datetime) -> float:
    return dt.utcoffset().total_seconds() / 60.0


def resolve_local_time(year, month, day, hour, minute, tz_name="Asia/Kolkata", utc_offset_minutes=None, second=0):
    """Return (local_dt, utc_dt, warnings).

    - The supported range is judged on the LOCAL calendar year.
    - A wall-clock time that never existed (DST gap) raises TimeInputError.
    - A wall-clock time that occurred twice (DST overlap) uses the first
      occurrence and returns an 'ambiguous_local_time' warning.
    - An explicit utc_offset_minutes wins; if it contradicts the timezone
      database a 'utc_offset_override' warning is returned.
    """
    if not (SUPPORTED_MIN_YEAR <= year <= SUPPORTED_MAX_YEAR):
        raise TimeInputError(f"birth year outside supported range {SUPPORTED_MIN_YEAR}-{SUPPORTED_MAX_YEAR}")
    try:
        naive = datetime(year, month, day, hour, minute, second)
    except ValueError as exc:
        raise TimeInputError("invalid calendar date/time") from exc
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise TimeInputError("invalid IANA timezone") from exc

    warnings = []
    first = naive.replace(tzinfo=tz, fold=0)
    second = naive.replace(tzinfo=tz, fold=1)
    gap = overlap = False
    if _offset_minutes(first) != _offset_minutes(second):
        round_trip = first.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)
        if round_trip != naive:
            gap = True
        else:
            overlap = True

    if utc_offset_minutes is not None:
        if abs(utc_offset_minutes) > 14 * 60:
            raise TimeInputError("utc_offset_minutes must be between -840 and 840")
        local = naive.replace(tzinfo=timezone(timedelta(minutes=utc_offset_minutes)))
        valid = {_offset_minutes(first)} | ({_offset_minutes(second)} if overlap else set())
        if gap or utc_offset_minutes not in valid:
            warnings.append({
                "code": "utc_offset_override",
                "message": "utc_offset_minutes differs from the offset the timezone database gives for this local time; the supplied offset was used.",
            })
        return local, local.astimezone(timezone.utc), warnings

    if gap:
        raise TimeInputError(
            "This local time does not exist in the given timezone (daylight-saving gap). "
            "Check the birth time, or send utc_offset_minutes."
        )
    if overlap:
        warnings.append({
            "code": "ambiguous_local_time",
            "message": "This local time occurred twice (daylight-saving overlap). The first occurrence was used; send utc_offset_minutes to choose.",
        })
    return first, first.astimezone(timezone.utc), warnings
