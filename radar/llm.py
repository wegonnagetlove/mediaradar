"""Единый вызов любой нейросети.

anthropic    Claude, нативный API; веб-поиск через серверный инструмент.
openai       ChatGPT; веб-поиск через Responses API.
openrouter   сотни моделей одним ключом; веб-поиск через суффикс :online.
gemini       Google Gemini через OpenAI-совместимый шлюз.
deepseek     DeepSeek.
custom       любой OpenAI-совместимый API (Mistral, Groq, Ollama, корпоративный шлюз): llm.base_url.
"""
from __future__ import annotations

import json
import logging
import re

from . import net

log = logging.getLogger("radar.llm")

OPENAI_COMPAT = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "deepseek": "https://api.deepseek.com",
}
WEB_CAPABLE = {"anthropic", "openai", "openrouter"}


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, cfg: dict, api_key: str):
        self.cfg = cfg
        self.provider = cfg.get("provider", "anthropic")
        self.model = cfg["model"]
        self.key = api_key
        if not api_key:
            raise LLMError("не задан LLM_API_KEY")

    @property
    def can_search(self) -> bool:
        return self.provider in WEB_CAPABLE

    # ── публичный метод ──────────────────────────────────────────────────
    def complete(self, system: str, user: str, *, web_search: bool = False, max_searches: int = 8) -> str:
        if web_search and not self.can_search:
            raise LLMError(f"провайдер {self.provider} не умеет искать в вебе")
        if self.provider == "anthropic":
            return self._anthropic(system, user, web_search, max_searches)
        if self.provider == "openai" and web_search:
            return self._openai_responses(system, user)
        return self._chat(system, user, web_search)

    # ── Anthropic ────────────────────────────────────────────────────────
    def _anthropic(self, system: str, user: str, web: bool, max_searches: int) -> str:
        body = {
            "model": self.model,
            "max_tokens": int(self.cfg.get("max_tokens", 6000)),
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if self.cfg.get("temperature") is not None and not web:
            body["temperature"] = float(self.cfg["temperature"])
        if web:
            body["tools"] = [{"type": self.cfg.get("anthropic_web_search_tool", "web_search_20250305"),
                              "name": "web_search", "max_uses": max_searches}]
        headers = {"x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        texts: list[str] = []
        for _ in range(4):  # pause_turn: длинный поиск возвращается частями
            r = net.post("https://api.anthropic.com/v1/messages", json=body, headers=headers, timeout=300)
            if r.status_code != 200:
                raise LLMError(f"Anthropic HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
            texts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
            if data.get("stop_reason") != "pause_turn":
                break
            body["messages"] = body["messages"][:1] + [{"role": "assistant", "content": data["content"]}]
        return "".join(texts).strip()

    # ── OpenAI Responses (веб-поиск) ─────────────────────────────────────
    def _openai_responses(self, system: str, user: str) -> str:
        body = {
            "model": self.model,
            "instructions": system,
            "input": user,
            "tools": [{"type": self.cfg.get("openai_web_search_tool", "web_search")}],
            "max_output_tokens": int(self.cfg.get("max_tokens", 6000)),
        }
        r = net.post(f"{OPENAI_COMPAT['openai']}/responses", json=body,
                     headers={"Authorization": f"Bearer {self.key}"}, timeout=300)
        if r.status_code != 200:
            raise LLMError(f"OpenAI HTTP {r.status_code}: {r.text[:300]}")
        out = []
        for item in r.json().get("output", []):
            if item.get("type") == "message":
                out += [c.get("text", "") for c in item.get("content", []) if c.get("type") == "output_text"]
        return "".join(out).strip()

    # ── OpenAI-совместимые chat/completions ──────────────────────────────
    def _chat(self, system: str, user: str, web: bool) -> str:
        base = (self.cfg.get("base_url") or OPENAI_COMPAT.get(self.provider, "")).rstrip("/")
        if not base:
            raise LLMError("для provider: custom укажите llm.base_url")
        model = self.model
        if web and self.provider == "openrouter" and not model.endswith(":online"):
            model += ":online"
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": int(self.cfg.get("max_tokens", 6000)),
        }
        # у reasoning-моделей OpenAI температура не настраивается
        if self.cfg.get("temperature") is not None and self.provider != "openai":
            body["temperature"] = float(self.cfg["temperature"])
        r = net.post(f"{base}/chat/completions", json=body, headers={"Authorization": f"Bearer {self.key}"}, timeout=300)
        if r.status_code != 200:
            raise LLMError(f"{self.provider} HTTP {r.status_code}: {r.text[:300]}")
        try:
            return (r.json()["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"{self.provider}: неожиданный ответ {r.text[:300]}") from e


def extract_json(text: str):
    """Достаёт JSON из ответа модели, даже если он обёрнут в ``` или в пояснения."""
    if not text:
        raise ValueError("пустой ответ модели")
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_, close in (("{", "}"), ("[", "]")):
        start, end = text.find(open_), text.rfind(close)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"в ответе модели нет JSON: {text[:200]}")
