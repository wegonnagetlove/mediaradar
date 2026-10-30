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


def explain(status: int, text: str) -> str:
    """Ошибка Telegram человеческими словами: что именно поправить."""
    low = (text or "").lower()
    if status == 401 or "unauthorized" in low:
        return "Telegram не принял TELEGRAM_BOT_TOKEN: токен отозван или скопирован не полностью. Скопируйте его заново в BotFather (кнопка Copy) и обновите секрет"
    if "chat not found" in low:
        return "Telegram не нашёл чат TELEGRAM_CHAT_ID: проверьте число в секрете (ваш Id из @userinfobot, а не номер бота) и нажмите «Старт» у своего бота"
    if status == 403 or "blocked" in low or "initiate conversation" in low:
        return "бот не может написать первым: откройте своего бота в Telegram и нажмите «Старт»"
    if "parse entities" in low:
        return "Telegram не разобрал разметку сообщения"
    return f"Telegram ответил HTTP {status}: {text[:200]}"


class Telegram:
    def __init__(self, token: str, chat_id: str, *, silent: bool = False):
        self.token, self.chat_id, self.silent = token, chat_id, silent
        self.errors: list[str] = []
        self.sent = 0

    def _error(self, method: str, status: int, text: str) -> None:
        msg = explain(status, text)
        log.error("Telegram %s: %s", method, msg)
        if msg not in self.errors:
            self.errors.append(msg)

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
            try:
                r = net.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json=body, timeout=30)
            except Exception as e:  # noqa: BLE001
                self._error("sendMessage", 0, str(e))
                ok = False
                continue
            if r.status_code != 200 and html and "parse entities" in r.text.lower():
                body.pop("parse_mode")  # разметка не разобралась: шлём как обычный текст
                r = net.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json=body, timeout=30)
            if r.status_code != 200:
                self._error("sendMessage", r.status_code, r.text)
                ok = False
            else:
                self.sent += 1
        return ok

    def _api(self, method: str, *, data: dict, files: dict | None = None) -> bool:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        try:
            r = net.post(url, data=data, files=files, timeout=60)
            if r.status_code != 200 and data.get("parse_mode") and "parse entities" in r.text.lower():
                data = {k: v for k, v in data.items() if k != "parse_mode"}
                r = net.post(url, data=data, files=files, timeout=60)
        except Exception as e:  # noqa: BLE001
            self._error(method, 0, str(e))
            return False
        if r.status_code != 200:
            self._error(method, r.status_code, r.text)
            return False
        self.sent += 1
        return True

    def check(self) -> str:
        """Проверка связи до запуска: '' если всё в порядке, иначе понятное объяснение."""
        if not self.token:
            return "нет секрета TELEGRAM_BOT_TOKEN"
        if not self.chat_id:
            return "нет секрета TELEGRAM_CHAT_ID"
        if self.token.split(":", 1)[0] == self.chat_id.lstrip("-"):
            return "в TELEGRAM_CHAT_ID записан номер самого бота; нужен ваш Id из @userinfobot"
        try:
            r = net.post(f"https://api.telegram.org/bot{self.token}/getChat", data={"chat_id": self.chat_id}, timeout=30)
        except Exception as e:  # noqa: BLE001
            return f"нет связи с Telegram: {e}"
        return "" if r.status_code == 200 else explain(r.status_code, r.text)

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
