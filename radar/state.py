"""Память между запусками: что уже видели и что уже отправили.

Хранится в state/seen.json. В GitHub Actions файл переживает запуски через actions/cache.
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from .config import ROOT
from .timeutil import now_utc, parse_dt

KEEP_DAYS = 3
KEEP_SENT = 300


class State:
    def __init__(self, path: Path | None = None):
        self.path = path or ROOT / "state" / "seen.json"
        self.seen: dict[str, str] = {}
        self.sent: list[dict] = []
        self.meta: dict = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.seen = data.get("seen", {})
                self.sent = data.get("sent", [])
                self.meta = data.get("meta", {})
            except (json.JSONDecodeError, OSError):
                pass  # повреждённый файл: начинаем с чистого листа

    def is_new(self, item_id: str) -> bool:
        return item_id not in self.seen

    def mark_seen(self, ids) -> None:
        stamp = now_utc().isoformat()
        for i in ids:
            self.seen.setdefault(i, stamp)

    def mark_sent(self, title: str, url: str) -> None:
        self.sent.append({"title": title, "url": url, "at": now_utc().isoformat()})
        self.sent = self.sent[-KEEP_SENT:]

    def recent_sent(self, hours: int = 48) -> list[dict]:
        border = now_utc() - timedelta(hours=hours)
        return [s for s in self.sent if (parse_dt(s.get("at")) or border) > border]

    def save(self) -> None:
        border = now_utc() - timedelta(days=KEEP_DAYS)
        self.seen = {k: v for k, v in self.seen.items() if (parse_dt(v) or border) > border}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"seen": self.seen, "sent": self.sent, "meta": self.meta},
                                        ensure_ascii=False, indent=1), encoding="utf-8")
