"""Общая HTTP-сессия: один User-Agent, таймауты, повтор при сетевом сбое."""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger("radar.net")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36 NewsroomRadar/2.0"
)

_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"})


def get(url: str, *, params: dict | None = None, timeout: float = 20, retries: int = 1, headers: dict | None = None) -> requests.Response:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = _session.get(url, params=params, timeout=timeout, headers=headers)
            if r.status_code >= 500 and attempt < retries:
                time.sleep(1.5)
                continue
            return r
        except requests.RequestException as e:  # сеть, DNS, таймаут
            last = e
            if attempt < retries:
                time.sleep(1.5)
    raise last  # type: ignore[misc]


def post(url: str, *, json: dict | None = None, data: dict | None = None, headers: dict | None = None, timeout: float = 120) -> requests.Response:
    return _session.post(url, json=json, data=data, headers=headers, timeout=timeout)
