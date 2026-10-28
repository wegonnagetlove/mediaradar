"""Уведомления в Telegram через своего бота.

Каждая новость уходит отдельным сообщением, чтобы её можно было сразу переслать в канал.
"""
from __future__ import annotations

import html as _html
import json
import logging
import re
from datetime import datetime

from . import net

log = logging.getLogger("radar.notify")

LIMIT = 4000          # у Telegram 4096, оставляем запас
CAPTION_LIMIT = 1024  # подпись к фото
MAX_PHOTO_BYTES = 10 * 1024 * 1024


def split(text: str, limit: int = LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts, buf = [], ""
    for para in text.split("\n"):
        while len(para) > limit:  # абзац длиннее лимита режем жёстко
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(para[:limit])
            para = para[limit:]
        candidate = f"{buf}\n{para}" if buf else para
        if len(candidate) > limit:
            parts.append(buf)
            buf = para
        else:
            buf = candidate
    if buf:
        parts.append(buf)
    return parts


def is_night(now_local: datetime) -> bool:
    return 1 <= now_local.hour < 8


class Telegram:
    def __init__(self, token: str, chat_id: str, *, silent: bool = False):
        self.token, self.chat_id, self.silent = token, chat_id, silent

    @property
    def ready(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str, *, preview: bool = False, html: bool = False) -> bool:
        ok = True
        for part in (split(text) if not html else [text]):
            body = {
                "chat_id": self.chat_id,
                "text": part,
                "disable_notification": self.silent,
                "link_preview_options": {"is_disabled": not preview},
            }
            if html:
                body["parse_mode"] = "HTML"
            r = net.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json=body, timeout=30)
            if r.status_code != 200:
                log.error("Telegram: HTTP %s %s", r.status_code, r.text[:200])
                ok = False
        return ok

    def _api(self, method: str, *, data: dict, files: dict | None = None) -> bool:
        r = net.post(f"https://api.telegram.org/bot{self.token}/{method}", data=data, files=files, timeout=60)
        if r.status_code != 200:
            log.error("Telegram %s: HTTP %s %s", method, r.status_code, r.text[:200])
            return False
        return True

    def send_post(self, text: str, *, footer: str = "", images: list[str] | None = None, link: str = "") -> bool:
        """Дневной пост: фото с подписью; если фото нет, текст с превью ссылки на источник."""
        body = _html.escape(text.strip())
        caption = f"{body}\n\n{footer}" if footer else body
        photos = [b for b in (download_image(u) for u in (images or [])) if b]
        base = {"chat_id": self.chat_id, "disable_notification": json.dumps(self.silent)}
        fits = visible_len(caption) <= CAPTION_LIMIT

        sent_photo = False
        if photos:
            if len(photos) == 1:
                data = {**base, **({"caption": caption, "parse_mode": "HTML"} if fits else {})}
                ok = self._api("sendPhoto", data=data, files={"photo": ("photo.jpg", photos[0])})
            else:
                media = [{"type": "photo", "media": f"attach://p{i}"} for i in range(len(photos))]
                if fits:
                    media[0].update({"caption": caption, "parse_mode": "HTML"})
                ok = self._api("sendMediaGroup", data={**base, "media": json.dumps(media, ensure_ascii=False)},
                               files={f"p{i}": (f"p{i}.jpg", b) for i, b in enumerate(photos)})
            if ok and fits:
                return True
            sent_photo = ok
            if not ok:
                log.warning("фото не ушло, отправляю текстом")
        # без фото (или текст не влез в подпись): обычное сообщение, превью по ссылке на источник
        if sent_photo or not link:
            preview = {"is_disabled": True}
        else:
            preview = {"url": link, "prefer_large_media": True}
        return self._api("sendMessage", data={**base, "text": caption, "parse_mode": "HTML",
                                              "link_preview_options": json.dumps(preview)})

    def send_list(self, head: str, lines: list[str]) -> bool:
        """HTML-список: режется только между пунктами, чтобы не порвать ссылку."""
        ok = True
        for chunk in pack(head, lines):
            ok = self.send(chunk, html=True) and ok
        return ok


def pack(head: str, lines: list[str], limit: int = 3800) -> list[str]:
    chunks, buf = [], head
    for line in lines:
        candidate = f"{buf}\n\n{line}" if buf else line
        if len(candidate) > limit and buf:
            chunks.append(buf)
            buf = line
        else:
            buf = candidate
    if buf:
        chunks.append(buf)
    return chunks


def visible_len(html_text: str) -> int:
    """Длина текста так, как её считает Telegram: без HTML-разметки."""
    return len(_html.unescape(re.sub(r"<[^>]+>", "", html_text)))


def download_image(url: str) -> bytes | None:
    if not url or not url.startswith("http"):
        return None
    try:
        r = net.get(url, timeout=20)
    except Exception as e:  # noqa: BLE001
        log.info("фото %s: %s", url[:80], e)
        return None
    ctype = r.headers.get("content-type", "")
    if r.status_code != 200 or not ctype.startswith("image/") or "svg" in ctype or len(r.content) > MAX_PHOTO_BYTES:
        return None
    if len(r.content) < 5000:  # пиксели-счётчики и иконки
        return None
    return r.content
