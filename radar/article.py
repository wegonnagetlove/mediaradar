"""Страница первоисточника: точное время публикации и текст для фактуры поста."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from . import net
from .timeutil import parse_dt

log = logging.getLogger("radar.article")

SKIP_HOSTS = ("news.google.com", "instagram.com", "t.me", "threads.net", "threads.com", "x.com", "twitter.com")

META_TIME = [
    ("property", "article:published_time"),
    ("property", "og:article:published_time"),
    ("name", "article:published_time"),
    ("itemprop", "datePublished"),
    ("name", "pubdate"),
    ("name", "publish-date"),
    ("name", "parsely-pub-date"),
    ("name", "sailthru.date"),
    ("name", "date"),
]


@dataclass
class Article:
    url: str
    published: datetime | None
    text: str


def _jsonld_dates(soup: BeautifulSoup) -> list[str]:
    out = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if node.get("datePublished"):
                    out.append(node["datePublished"])
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
            elif isinstance(node, list):
                stack.extend(node)
    return out


def parse(html: str, url: str) -> Article:
    soup = BeautifulSoup(html, "html.parser")
    published = None
    for attr, val in META_TIME:
        tag = soup.find("meta", attrs={attr: val})
        if tag and tag.get("content"):
            published = parse_dt(tag["content"])
            if published:
                break
    if not published:
        for d in _jsonld_dates(soup):
            published = parse_dt(d)
            if published:
                break
    if not published:
        t = soup.find("time", attrs={"datetime": True})
        published = parse_dt(t["datetime"]) if t else None

    for junk in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
        junk.decompose()
    body = soup.find("article") or soup.find("main") or soup.body or soup
    paras = [p.get_text(" ", strip=True) for p in body.find_all("p")]
    text = "\n".join(p for p in paras if len(p) > 40)
    return Article(url=url, published=published, text=text[:6000])


def fetch(url: str) -> Article | None:
    host = urlparse(url).netloc.lower()
    if any(host.endswith(h) for h in SKIP_HOSTS):
        return None
    try:
        r = net.get(url, timeout=15)
    except Exception as e:  # noqa: BLE001
        log.info("статья %s: %s", url, e)
        return None
    if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
        return None
    if any(urlparse(r.url).netloc.lower().endswith(h) for h in SKIP_HOSTS):
        return None
    return parse(r.text, r.url)
