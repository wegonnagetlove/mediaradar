from __future__ import annotations

import os
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
            telegram_bot_token=g("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=g("TELEGRAM_CHAT_ID"),
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
