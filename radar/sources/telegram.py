"""Публичные телеграм-каналы через веб-превью t.me/s/<канал>.

Без бота и без API: страница отдаёт ~20 последних постов с точным временем (атрибут datetime).
Используется и как источник новостей, и для проверки конкурентов.
"""
from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from .. import net
from ..models import CompetitorPost, Item
from ..timeutil import parse_dt

log = logging.getLogger("radar.telegram")

_BG = re.compile(r"background-image:\s*url\(['\"]?([^'\")]+)['\"]?\)")


def _photos(msg) -> list[str]:
    urls = []
    for el in msg.select(".tgme_widget_message_photo_wrap, .tgme_widget_message_video_thumb"):
        m = _BG.search(el.get("style", ""))
        if m and m.group(1).startswith("http"):
            urls.append(m.group(1))
    return urls[:4]


def parse_channel(html: str, channel: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    posts = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        text_el = msg.select_one(".tgme_widget_message_text")
        text = text_el.get_text("\n", strip=True) if text_el else ""
        time_el = msg.select_one("a.tgme_widget_message_date time[datetime]") or msg.select_one("time[datetime]")
        link_el = msg.select_one("a.tgme_widget_message_date")
        fwd = msg.select_one(".tgme_widget_message_forwarded_from_name")
        posts.append({
            "post": msg["data-post"],
            "text": text,
            "published": parse_dt(time_el["datetime"]) if time_el else None,
            "url": link_el["href"] if link_el and link_el.has_attr("href") else f"https://t.me/{msg['data-post']}",
            "forwarded_from": fwd.get_text(strip=True) if fwd else "",
            "images": _photos(msg),
        })
    return posts


def _fetch_html(channel: str) -> str | None:
    try:
        r = net.get(f"https://t.me/s/{channel}")
    except Exception as e:  # noqa: BLE001
        log.warning("t.me/s/%s: %s", channel, e)
        return None
    if r.status_code != 200 or "tgme_widget_message" not in r.text:
        log.warning("t.me/s/%s: канал закрыт для веб-превью или не найден (HTTP %s)", channel, r.status_code)
        return None
    return r.text


def _title_from(text: str) -> str:
    first = text.strip().split("\n", 1)[0]
    return first[:200] if len(first) >= 15 else text[:200].replace("\n", " ")


def fetch(channel: str, *, label: str | None = None, region: str = "ru") -> list[Item]:
    html = _fetch_html(channel)
    if not html:
        return []
    items = []
    for p in parse_channel(html, channel):
        if not p["text"] or not p["published"]:
            continue  # пост без текста (только фото) отбора не пройдёт
        items.append(Item(
            source="telegram", label=label or f"@{channel}", region=region,
            title=_title_from(p["text"]), url=p["url"], published=p["published"],
            summary=p["text"][:1200], time_basis="API",
            extra={k: v for k, v in (("forwarded_from", p["forwarded_from"]), ("images", p["images"])) if v},
        ))
    return items


def competitor_posts(channel: str, limit: int = 20) -> list[CompetitorPost]:
    html = _fetch_html(channel)
    if not html:
        return []
    posts = [CompetitorPost(channel=f"@{channel}", text=p["text"][:500], published=p["published"], url=p["url"])
             for p in parse_channel(html, channel) if p["text"]]
    return posts[-limit:]
