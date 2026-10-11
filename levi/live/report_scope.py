"""Explicit ISO/epoch report boundaries; no workspace or dataset reads."""

import math
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def window(since=None, before=None, timezone="UTC"):
    """Return [since, before) in epoch seconds, with an explicit naive-time zone.

    Offset-bearing ISO times are absolute. Naive DST gaps or ambiguous times
    require the caller to provide an offset; never inherit the machine zone.
    """
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError("timezone must be an IANA time zone") from exc

    def boundary(value):
        if value is None or value == "":
            return None
        if type(value) not in (str, int, float):
            raise ValueError("report boundary must be an ISO time or epoch seconds")
        try:
            stamp = float(value)
        except (ValueError, OverflowError):
            try:
                date = datetime.fromisoformat(value)
                if date.tzinfo is None:
                    early = date.replace(tzinfo=zone, fold=0)
                    late = date.replace(tzinfo=zone, fold=1)
                    if early.utcoffset() != late.utcoffset():
                        raise ValueError(
                            "ambiguous or nonexistent local time; give an offset"
                        )
                    date = early
                stamp = date.timestamp()
            except (ValueError, TypeError, OverflowError, OSError) as exc:
                raise ValueError(
                    "report boundary must be a valid unambiguous ISO time or epoch seconds"
                ) from exc
        if not math.isfinite(stamp):
            raise ValueError("report boundary must be finite")
        try:
            datetime.fromtimestamp(stamp, UTC)
        except (ValueError, OverflowError, OSError) as exc:
            raise ValueError(
                "report boundary is outside the supported date range"
            ) from exc
        return stamp

    start, end = boundary(since), boundary(before)
    if start is not None and end is not None and start >= end:
        raise ValueError("report since must be earlier than before")
    return start, end, zone.key if start is not None or end is not None else None
