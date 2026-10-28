"""Google News RSS: поиск по запросу с окном свежести when:Nh.

Даёт издания без собственного RSS (BoF, Vogue Business, The Cut, Puck…)
и российскую повестку по ключевым словам. Время берётся из pubDate ленты.
"""
from __future__ import annotations

import math
from urllib.parse import urlencode, urlparse

from ..models import Item
from . import rss

LOCALES = {
    "ru": {"hl": "ru", "gl": "RU", "ceid": "RU:ru"},
    "intl": {"hl": "en-US", "gl": "US", "ceid": "US:en"},
}


def build_url(query: str, region: str, max_age_minutes: int) -> str:
    hours = max(1, math.ceil(max_age_minutes / 60))
    q = query if "when:" in query else f"{query} when:{hours}h"
    return "https://news.google.com/rss/search?" + urlencode({"q": q, **LOCALES[region]})


def _domain_of_title(title: str) -> str:
    # Google News добавляет « - Издание» в конец заголовка
    return title.rsplit(" - ", 1)[-1] if " - " in title else ""


def fetch(query: str, region: str, max_age_minutes: int) -> list[Item]:
    items = rss.fetch(build_url(query, region, max_age_minutes), label="Google News", region=region, source="gnews")
    for it in items:
        outlet = _domain_of_title(it.title)
        if outlet and it.title.endswith(" - " + outlet):
            it.title = it.title[: -(len(outlet) + 3)]
            if it.label == "Google News":
                it.label = outlet
        if it.extra.get("source_url"):
            it.extra["domain"] = host(it.extra["source_url"])
        it.extra["query"] = query
        # в Google News описание повторяет заголовок, оно бесполезно
        if it.summary.startswith(it.title[:40]):
            it.summary = ""
    return items


def recent_from_domain(domain: str, region: str = "ru", hours: int = 24) -> list[Item]:
    """Свежие заголовки конкретного издания (для проверки «уже есть у…»)."""
    items = fetch(f"site:{domain} when:{hours}h", region, hours * 60)
    for it in items:
        it.extra["domain"] = domain
    return items


def host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")
