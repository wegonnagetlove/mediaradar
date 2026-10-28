"""Уведомления в Telegram через своего бота.

Каждая новость уходит отдельным сообщением, чтобы её можно было сразу переслать в канал.
"""
from __future__ import annotations

import logging
from datetime import datetime

from . import net

log = logging.getLogger("radar.notify")

LIMIT = 4000  # у Telegram 4096, оставляем запас


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

    def send(self, text: str, *, preview: bool = False) -> bool:
        ok = True
        for part in split(text):
            r = net.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json={
                "chat_id": self.chat_id,
                "text": part,
                "disable_notification": self.silent,
                "link_preview_options": {"is_disabled": not preview},
            }, timeout=30)
            if r.status_code != 200:
                log.error("Telegram: HTTP %s %s", r.status_code, r.text[:200])
                ok = False
        return ok
