"""Очередь опроса большой базы аккаунтов в пределах лимитов API.

Аккаунты с tier: 1 проверяются в каждом запуске, остальные по кругу:
за запуск берётся следующая порция, указатель хранится в памяти радара.
Аккаунты, которые несколько запусков подряд отвечают ошибкой, отключаются на неделю.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .timeutil import now_utc, parse_dt

FAIL_LIMIT = 3
DISABLE_DAYS = 7


def _meta(state, key: str) -> dict:
    return state.meta.setdefault(key, {})


def disabled(state, key: str, name: str, now: datetime | None = None) -> bool:
    rec = _meta(state, f"{key}_fail").get(name.lower())
    until = parse_dt(rec.get("until")) if rec else None
    return bool(until and until > (now or now_utc()))


def pick(accounts: list[dict], state, key: str, limit: int, now: datetime | None = None) -> list[dict]:
    """tier 1 всегда, затем следующая порция по кругу; всего не больше limit."""
    now = now or now_utc()
    live = [a for a in accounts if not disabled(state, key, a["name"], now)]
    if limit <= 0 or len(live) <= limit:
        return live
    top = [a for a in live if int(a.get("tier", 2)) == 1][:limit]
    rest = [a for a in live if int(a.get("tier", 2)) != 1]
    room = limit - len(top)
    if room <= 0 or not rest:
        return top
    cursor = int(state.meta.get(f"{key}_cursor", 0)) % len(rest)
    batch = [rest[(cursor + i) % len(rest)] for i in range(min(room, len(rest)))]
    state.meta[f"{key}_cursor"] = (cursor + len(batch)) % len(rest)
    return top + batch


def window_minutes(state, key: str, name: str, default: int, cap: int, now: datetime | None = None) -> int:
    """Сколько минут назад этот аккаунт проверяли в прошлый раз (с запасом), но не больше cap."""
    last = parse_dt(_meta(state, f"{key}_polled").get(name.lower()))
    if not last:
        return default
    gap = ((now or now_utc()) - last).total_seconds() / 60 + 15
    return int(min(cap, max(default, gap)))


def polled(state, key: str, name: str, now: datetime | None = None) -> None:
    _meta(state, f"{key}_polled")[name.lower()] = (now or now_utc()).isoformat()


def ok(state, key: str, name: str) -> None:
    _meta(state, f"{key}_fail").pop(name.lower(), None)


def fail(state, key: str, name: str, error: str, now: datetime | None = None) -> bool:
    """Считает ошибку; возвращает True, если аккаунт только что отключён."""
    now = now or now_utc()
    rec = _meta(state, f"{key}_fail").setdefault(name.lower(), {"n": 0})
    rec["n"] = int(rec.get("n", 0)) + 1
    rec["err"] = error[:200]
    if rec["n"] >= FAIL_LIMIT and not rec.get("until"):
        rec["until"] = (now + timedelta(days=DISABLE_DAYS)).isoformat()
        return True
    return False


def disabled_list(state, key: str, now: datetime | None = None) -> list[tuple[str, str]]:
    now = now or now_utc()
    out = []
    for name, rec in _meta(state, f"{key}_fail").items():
        until = parse_dt(rec.get("until"))
        if until and until > now:
            out.append((name, rec.get("err", "")))
    return sorted(out)


def cycle_hours(total: int, tier1: int, limit: int, run_minutes: int = 15) -> float:
    rest = max(0, total - tier1)
    room = max(1, limit - tier1)
    if total <= limit:
        return run_minutes / 60
    return round(-(-rest // room) * run_minutes / 60, 1)
