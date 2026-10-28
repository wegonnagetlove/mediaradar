from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


@dataclass
class Item:
    """Одна новость-кандидат из любого источника."""

    source: str            # тип источника: rss | gnews | telegram | instagram | social
    label: str             # человекочитаемое имя: «WWD», «@diet_prada (Instagram)»
    region: str            # ru | intl
    title: str
    url: str
    published: datetime    # всегда aware, UTC
    summary: str = ""
    author: str = ""
    time_basis: str = "лента"   # откуда взято время: лента | страница | API | поиск
    extra: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        key = (self.url or self.title).strip().lower()
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]

    def age_minutes(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (now - self.published).total_seconds() / 60

    def for_llm(self, tz) -> dict:
        d = {
            "id": self.id,
            "region": self.region,
            "source": self.label,
            "type": self.source,
            "published": self.published.astimezone(tz).strftime("%d.%m %H:%M"),
            "title": self.title[:300],
            "summary": self.summary[:600],
            "url": self.url,
        }
        if self.extra.get("in_competitor_media"):
            d["in_competitor_media"] = True
        return d

    def to_json(self) -> dict:
        d = asdict(self)
        d["published"] = self.published.isoformat()
        d["id"] = self.id
        return d


@dataclass
class CompetitorPost:
    channel: str
    text: str
    published: datetime | None
    url: str = ""
