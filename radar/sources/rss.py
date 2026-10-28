"""RSS 2.0, Atom и RDF без внешних зависимостей."""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

from .. import net
from ..models import Item
from ..timeutil import parse_dt

log = logging.getLogger("radar.rss")

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "rss1": "http://purl.org/rss/1.0/",
    "media": "http://search.yahoo.com/mrss/",
}


def _images(e, html_parts: list[str]) -> list[str]:
    """Картинка записи: media:content, media:thumbnail, enclosure или первый <img> в тексте."""
    urls = []
    for tag in ("media:content", "media:group/media:content", "media:thumbnail"):
        for m in e.findall(tag, NS):
            u = m.get("url", "")
            kind = (m.get("medium") or m.get("type") or "image").lower()
            if u and ("image" in kind or tag.endswith("thumbnail")):
                urls.append(u)
    for enc in e.findall("enclosure"):
        if enc.get("type", "").startswith("image/") and enc.get("url"):
            urls.append(enc.get("url"))
    if not urls:
        for part in html_parts:
            if part and "<img" in part:
                img = BeautifulSoup(part, "html.parser").find("img", src=True)
                if img and img["src"].startswith("http"):
                    urls.append(img["src"])
                    break
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:4]


def _text(el, path: str) -> str:
    if el is None:
        return ""
    found = el.find(path, NS)
    return (found.text or "").strip() if found is not None and found.text else ""


def _clean(html: str, limit: int = 800) -> str:
    if not html:
        return ""
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    return " ".join(text.split())[:limit]


def parse_feed(xml: bytes | str, *, label: str, region: str, source: str = "rss") -> list[Item]:
    try:
        root = ET.fromstring(xml if isinstance(xml, bytes) else xml.encode("utf-8"))
    except ET.ParseError as e:
        log.warning("%s: не XML (%s)", label, e)
        return []

    items: list[Item] = []
    entries = root.findall(".//item") or root.findall(".//rss1:item", NS)
    if entries:
        for e in entries:
            title = _text(e, "title") or _text(e, "rss1:title")
            link = _text(e, "link") or _text(e, "rss1:link")
            date = _text(e, "pubDate") or _text(e, "dc:date")
            desc = _text(e, "description") or _text(e, "content:encoded")
            author = _text(e, "dc:creator") or _text(e, "author")
            src = e.find("source")
            src_name = (src.text or "").strip() if src is not None and src.text else ""
            dt = parse_dt(date)
            if not (title and link and dt):
                continue
            it = Item(source=source, label=src_name or label, region=region, title=_clean(title, 300),
                      url=link, published=dt, summary=_clean(desc), author=author)
            if src is not None and src.get("url"):
                it.extra["source_url"] = src.get("url")
            imgs = _images(e, [_text(e, "description"), _text(e, "content:encoded")])
            if imgs:
                it.extra["images"] = imgs
            items.append(it)
        return items

    for e in root.findall("atom:entry", NS):
        title = _text(e, "atom:title")
        # у Element без детей bool() == False, поэтому только явная проверка на None
        link_el = e.find("atom:link[@rel='alternate']", NS)
        if link_el is None:
            link_el = e.find("atom:link", NS)
        link = link_el.get("href", "") if link_el is not None else ""
        dt = parse_dt(_text(e, "atom:published") or _text(e, "atom:updated"))
        desc = _text(e, "atom:summary") or _text(e, "atom:content")
        author = _text(e, "atom:author/atom:name")
        if title and link and dt:
            items.append(Item(source=source, label=label, region=region, title=_clean(title, 300),
                              url=link, published=dt, summary=_clean(desc), author=author))
    return items


def fetch(url: str, *, label: str, region: str, source: str = "rss") -> list[Item]:
    try:
        r = net.get(url, headers={"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5"})
    except Exception as e:  # noqa: BLE001
        log.warning("%s: %s", label, e)
        return []
    if r.status_code != 200:
        log.warning("%s: HTTP %s", label, r.status_code)
        return []
    return parse_feed(r.content, label=label, region=region, source=source)
