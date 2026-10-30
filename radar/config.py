from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "channels": [],
    "timezone": "Europe/Moscow",
    "modes": {
        "A": {"max_age_minutes": 60, "min_posts": 3, "max_posts": 5},
        "B": {"max_age_minutes": 60, "min_posts": 0, "max_posts": 3},
    },
    "engine": "hybrid",
    "social_search": False,
    "strict_time": False,
    "llm": {"provider": "anthropic", "model": "claude-sonnet-5-5", "temperature": 0.6, "max_tokens": 6000},
    "notify": {"telegram": True, "send_empty": False, "silent_night": False, "save_digest": True},
    "competitors_telegram": [],
    "competitors_media": [],
    "sources": {"telegram": [], "rss": [], "google_news": {"ru": [], "intl": []}},
    "instagram": {"enabled": False, "provider": "graph", "accounts": []},
    "social_accounts": {"instagram": [], "x": []},
    "topics_hint": "",
    "stop_topics": [],
    "max_candidates_to_llm": 60,
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


TOKEN_RE = re.compile(r"(\d{6,}):([A-Za-z0-9_-]{30,})")


def clean_token(raw: str) -> str:
    """Токен бота из чего угодно: с приставкой bot, ссылкой, фразой BotFather, невидимыми символами."""
    raw = (raw or "").replace("\u200b", "").replace("\ufeff", "").replace("\u00a0", " ")
    m = TOKEN_RE.search(raw)
    return f"{m.group(1)}:{m.group(2)}" if m else raw.strip()


def describe_token(raw: str) -> str:
    """Форма секрета без раскрытия самого токена: длина, номер бота, лишние символы."""
    raw = raw or ""
    if not raw.strip():
        return "секрет пустой"
    parts = []
    m = TOKEN_RE.search(raw)
    if m:
        parts.append(f"номер бота {m.group(1)}, длина части после двоеточия {len(m.group(2))} (обычно 35)")
        extra = len(raw) - len(m.group(0))
        if extra:
            parts.append(f"вокруг токена {extra} лишних символов (их радар уже отбрасывает)")
    else:
        parts.append(f"не похоже на токен: длина {len(raw)}, двоеточие {'есть' if ':' in raw else 'нет'}")
        if any(ord(c) > 127 for c in raw):
            parts.append("есть русские или особые символы")
    return "; ".join(parts)


@dataclass
class Secrets:
    llm_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    ig_access_token: str
    ig_user_id: str
    apify_token: str

    @classmethod
    def from_env(cls) -> "Secrets":
        g = lambda k: os.environ.get(k, "").strip()  # noqa: E731
        return cls(
            llm_api_key=g("LLM_API_KEY"),
            telegram_bot_token=clean_token(os.environ.get("TELEGRAM_BOT_TOKEN", "")),
            telegram_chat_id=re.sub(r"[^\d-]", "", g("TELEGRAM_CHAT_ID")),
            ig_access_token=g("IG_ACCESS_TOKEN"),
            ig_user_id=g("IG_USER_ID"),
            apify_token=g("APIFY_TOKEN"),
        )


class Config(dict):
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self["timezone"])

    def mode(self, name: str) -> dict:
        return self["modes"][name.upper()]


def _name(a) -> str:
    return str(a if isinstance(a, str) else (a or {}).get("name", "")).lstrip("@").lower()


def _append_unique(base: list, extra: list) -> list:
    seen = {_name(a) for a in base}
    out = list(base)
    for a in extra or []:
        n = _name(a)
        if n and n not in seen:
            seen.add(n)
            out.append(a)
    return out


def merge_accounts(cfg: dict, accounts: dict) -> dict:
    """База аккаунтов из accounts.yaml дописывается к спискам из config.yaml без повторов."""
    ig = cfg.setdefault("instagram", {})
    ig["accounts"] = _append_unique(ig.get("accounts") or [], accounts.get("instagram"))
    social = cfg.setdefault("social_accounts", {})
    for net in ("threads", "x"):
        social[net] = _append_unique(social.get(net) or [], accounts.get(net))
    return cfg


def load(path: str | Path | None = None) -> Config:
    path = Path(path or os.environ.get("RADAR_CONFIG") or ROOT / "config.yaml")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    cfg = _merge(DEFAULTS, data)
    acc_path = path.parent / str(cfg.get("accounts_file") or "accounts.yaml")
    if acc_path.exists():
        with open(acc_path, encoding="utf-8") as f:
            merge_accounts(cfg, yaml.safe_load(f) or {})
    return Config(cfg)


def prompt(name: str) -> str:
    return (ROOT / "prompts" / name).read_text(encoding="utf-8")
