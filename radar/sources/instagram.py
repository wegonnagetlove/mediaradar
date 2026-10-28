"""Instagram: посты публичных аккаунтов с точным временем публикации.

Instagram принадлежит Meta, признанной экстремистской и запрещённой в РФ;
публичные аккаунты используются только как новостные источники.

Три способа получения (config.yaml → instagram.provider):

graph   Официальный Instagram Graph API, метод Business Discovery. Бесплатно.
        Нужны свой профессиональный аккаунт Instagram, привязанный к странице Facebook,
        приложение Meta и токен (IG_ACCESS_TOKEN, IG_USER_ID). Видит только бизнес- и
        авторские аккаунты; личные закрытые и обычные личные профили недоступны.
apify   Готовый парсер публичных постов на Apify (APIFY_TOKEN). Платно за каждый пост,
        поэтому опрашивается не чаще instagram.min_interval_minutes.
feed    Любой RSS-мост (свой RSSHub и т.п.): у аккаунта в списке указывается feed: <url>.

Сторис чужих аккаунтов не отдаёт ни один из способов.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from .. import net
from ..models import Item
from ..timeutil import now_utc, parse_dt
from . import rss

log = logging.getLogger("radar.instagram")

GRAPH = "https://graph.facebook.com"


def normalize_accounts(raw: list) -> list[dict]:
    out = []
    for a in raw or []:
        if isinstance(a, str):
            out.append({"name": a.lstrip("@"), "region": "intl"})
        elif isinstance(a, dict) and a.get("name"):
            out.append({"region": "intl", **a, "name": str(a["name"]).lstrip("@")})
    return out


def _item(username: str, region: str, caption: str, url: str, ts, media_type: str = "",
          images: list[str] | None = None) -> Item | None:
    dt = parse_dt(ts)
    caption = (caption or "").strip()
    if not dt or not url:
        return None
    first = caption.split("\n", 1)[0] if caption else ""
    title = first[:200] if len(first) >= 15 else (caption[:200].replace("\n", " ") or f"Новый пост @{username}")
    return Item(
        source="instagram", label=f"@{username} (Instagram)", region=region,
        title=title, url=url, published=dt, summary=caption[:1500], author=username,
        time_basis="API",
        extra={k: v for k, v in (("media_type", media_type), ("images", [u for u in images or [] if u][:4])) if v},
    )


# ── graph ────────────────────────────────────────────────────────────────

def graph_url(ig_user_id: str, version: str = "") -> str:
    return f"{GRAPH}/{version.strip('/')}/{ig_user_id}" if version else f"{GRAPH}/{ig_user_id}"


def _graph_images(m: dict) -> list[str]:
    def one(x: dict) -> str:
        return x.get("thumbnail_url") if x.get("media_type") == "VIDEO" else (x.get("media_url") or x.get("thumbnail_url", ""))
    children = ((m.get("children") or {}).get("data")) or []
    return [one(c) for c in children] if children else [one(m)]


def parse_graph(payload: dict, username: str, region: str) -> list[Item]:
    media = (((payload or {}).get("business_discovery") or {}).get("media") or {}).get("data") or []
    items = []
    for m in media:
        it = _item(username, region, m.get("caption", ""), m.get("permalink", ""), m.get("timestamp"),
                   m.get("media_product_type") or m.get("media_type", ""), _graph_images(m))
        if it:
            items.append(it)
    return items


def fetch_graph(account: dict, token: str, ig_user_id: str, *, per_account: int = 8, version: str = "") -> list[Item]:
    u = account["name"]
    fields = (f"business_discovery.username({u})"
              f"{{username,media.limit({per_account}){{caption,timestamp,permalink,media_type,media_product_type,"
              f"media_url,thumbnail_url,children{{media_type,media_url,thumbnail_url}}}}}}")
    try:
        r = net.get(graph_url(ig_user_id, version), params={"fields": fields, "access_token": token})
    except Exception as e:  # noqa: BLE001
        log.warning("Instagram @%s: %s", u, e)
        return []
    data = r.json() if r.headers.get("content-type", "").startswith(("application/json", "text/javascript")) else {}
    if r.status_code != 200 or "error" in data:
        err = (data.get("error") or {}).get("message", f"HTTP {r.status_code}")
        log.warning("Instagram @%s: %s", u, err)
        return []
    return parse_graph(data, u, account.get("region", "intl"))


# ── apify ────────────────────────────────────────────────────────────────

def parse_apify(rows: list[dict], regions: dict[str, str]) -> list[Item]:
    items = []
    for row in rows or []:
        user = (row.get("ownerUsername") or row.get("username") or "").lstrip("@")
        url = row.get("url") or row.get("postUrl") or (
            f"https://www.instagram.com/p/{row['shortCode']}/" if row.get("shortCode") else "")
        ts = row.get("timestamp") or row.get("takenAtTimestamp") or row.get("taken_at")
        imgs = [c.get("displayUrl", "") for c in row.get("childPosts") or []] or \
            list(row.get("images") or []) or [row.get("displayUrl", "")]
        it = _item(user or "instagram", regions.get(user.lower(), "intl"),
                   row.get("caption") or row.get("text") or "", url, ts, row.get("type", ""), imgs)
        if it:
            items.append(it)
    return items


def fetch_apify(accounts: list[dict], token: str, *, actor: str, per_account: int, extra_input: dict | None = None) -> list[Item]:
    payload = {"username": [a["name"] for a in accounts], "resultsLimit": per_account, **(extra_input or {})}
    url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
    try:
        r = net.post(url, json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=280)
    except Exception as e:  # noqa: BLE001
        log.warning("Apify: %s", e)
        return []
    if r.status_code not in (200, 201):
        log.warning("Apify: HTTP %s %s", r.status_code, r.text[:200])
        return []
    regions = {a["name"].lower(): a.get("region", "intl") for a in accounts}
    return parse_apify(r.json(), regions)


# ── feed ─────────────────────────────────────────────────────────────────

def fetch_feed(account: dict) -> list[Item]:
    items = rss.fetch(account["feed"], label=f"@{account['name']} (Instagram)",
                      region=account.get("region", "intl"), source="instagram")
    for it in items:
        it.author = account["name"]
    return items


# ── точка входа ──────────────────────────────────────────────────────────

def due(cfg_ig: dict, state) -> bool:
    """Платные провайдеры опрашиваются не чаще min_interval_minutes."""
    minutes = int(cfg_ig.get("min_interval_minutes") or 0)
    if not minutes:
        return True
    last = parse_dt(state.meta.get("instagram_last_run"))
    return last is None or now_utc() - last >= timedelta(minutes=minutes) - timedelta(minutes=2)


def collect(cfg_ig: dict, secrets, pool) -> tuple[list[Item], list[str]]:
    """Возвращает (новости, предупреждения для отчёта)."""
    if not cfg_ig.get("enabled"):
        return [], []
    accounts = normalize_accounts(cfg_ig.get("accounts"))
    provider = cfg_ig.get("provider", "graph")
    per = int(cfg_ig.get("per_account", 8))
    warnings: list[str] = []
    items: list[Item] = []

    feed_accounts = [a for a in accounts if a.get("feed")]
    api_accounts = [a for a in accounts if not a.get("feed")]
    futures = [pool.submit(fetch_feed, a) for a in feed_accounts]

    if api_accounts:
        if provider == "graph":
            if secrets.ig_access_token and secrets.ig_user_id:
                futures += [pool.submit(fetch_graph, a, secrets.ig_access_token, secrets.ig_user_id,
                                        per_account=per, version=cfg_ig.get("graph_api_version", ""))
                            for a in api_accounts]
            else:
                warnings.append("Instagram: нет IG_ACCESS_TOKEN / IG_USER_ID, аккаунты без feed пропущены")
        elif provider == "apify":
            if secrets.apify_token:
                futures.append(pool.submit(fetch_apify, api_accounts, secrets.apify_token,
                                           actor=cfg_ig.get("apify_actor", "apify~instagram-post-scraper"),
                                           per_account=per, extra_input=cfg_ig.get("apify_input")))
            else:
                warnings.append("Instagram: нет APIFY_TOKEN, аккаунты без feed пропущены")
        else:
            warnings.append(f"Instagram: неизвестный provider «{provider}»")

    for f in futures:
        items.extend(f.result())
    return items, warnings
