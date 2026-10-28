from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def parse_dt(value: str | None) -> datetime | None:
    """RFC 822 (RSS), ISO 8601 (Atom, JSON-LD, API) или unix-время → aware UTC."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        return datetime.fromtimestamp(int(s), tz=timezone.utc)
    dt = None
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        pass
    if dt is None:
        iso = s.replace("Z", "+00:00")
        # Instagram Graph отдаёт +0000 без двоеточия
        if len(iso) > 5 and iso[-5] in "+-" and iso[-4:].isdigit() and iso[-3] != ":":
            iso = iso[:-2] + ":" + iso[-2:]
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
