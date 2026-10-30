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

from .. import net, rotation
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


RATE_LIMIT_CODES = {4, 17, 32, 613, 80002}


def fetch_graph(account: dict, token: str, ig_user_id: str, *, per_account: int = 8, version: str = "") -> list[Item]:
    items, _err, _rl = fetch_graph_ex(account, token, ig_user_id, per_account=per_account, version=version)
    return items


def fetch_graph_ex(account: dict, token: str, ig_user_id: str, *, per_account: int = 8,
                   version: str = "") -> tuple[list[Item], str, bool]:
    """(посты, текст ошибки или "", это ли превышение лимита / сбой сети)."""
    u = account["name"]
    fields = (f"business_discovery.username({u})"
              f"{{username,media.limit({per_account}){{caption,timestamp,permalink,media_type,media_product_type,"
              f"media_url,thumbnail_url,children{{media_type,media_url,thumbnail_url}}}}}}")
    try:
        r = net.get(graph_url(ig_user_id, version), params={"fields": fields, "access_token": token})
    except Exception as e:  # noqa: BLE001
        log.warning("Instagram @%s: %s", u, e)
        return [], str(e), True
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code != 200 or "error" in data:
        e = data.get("error") or {}
        err = e.get("message", f"HTTP {r.status_code}")
        transient = e.get("code") in RATE_LIMIT_CODES or r.status_code >= 500 or e.get("is_transient", False)
        log.warning("Instagram @%s: %s", u, err)
        return [], err, bool(transient)
    return parse_graph(data, u, account.get("region", "intl")), "", False


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

def per_run_limit(cfg_ig: dict) -> int:
    """Сколько аккаунтов за запуск, чтобы уложиться в лимит запросов в час (Business Discovery: 200)."""
    v = cfg_ig.get("max_per_run", "auto")
    if str(v).lower() != "auto":
        return int(v)
    runs_per_hour = max(1, 60 // max(1, int(cfg_ig.get("run_interval_minutes", 15))))
    return max(5, int(cfg_ig.get("hourly_limit", 180)) // runs_per_hour)


def due(cfg_ig: dict, state) -> bool:
    """Платные провайдеры опрашиваются не чаще min_interval_minutes."""
    minutes = int(cfg_ig.get("min_interval_minutes") or 0)
    if not minutes:
        return True
    last = parse_dt(state.meta.get("instagram_last_run"))
    return last is None or now_utc() - last >= timedelta(minutes=minutes) - timedelta(minutes=2)


def collect(cfg_ig: dict, secrets, pool, state=None) -> tuple[list[Item], list[str]]:
    """Возвращает (новости, предупреждения для отчёта).

    С памятью радара (state) работает очередь: tier 1 каждый запуск, остальные по кругу,
    не больше max_per_run запросов; у каждого поста помечено окно с прошлой проверки аккаунта.
    """
    if not cfg_ig.get("enabled"):
        return [], []
    accounts = normalize_accounts(cfg_ig.get("accounts"))
    provider = cfg_ig.get("provider", "graph")
    per = int(cfg_ig.get("per_account", 8))
    warnings: list[str] = []
    items: list[Item] = []
    now = now_utc()

    feed_accounts = [a for a in accounts if a.get("feed")]
    api_accounts = [a for a in accounts if not a.get("feed")]
    if state is not None:
        api_accounts = rotation.pick(api_accounts, state, "ig", per_run_limit(cfg_ig), now)
    default_window = int(cfg_ig.get("default_window_minutes", 60))
    cap = int(cfg_ig.get("max_window_minutes", 360))

    def tag(account: dict, got: list[Item]) -> list[Item]:
        if state is not None:
            w = rotation.window_minutes(state, "ig", account["name"], default_window, cap, now)
            for it in got:
                it.extra["window_minutes"] = w
                if account.get("tier"):
                    it.extra["tier"] = account["tier"]
        return got

    feed_futs = [(a, pool.submit(fetch_feed, a)) for a in feed_accounts]
    graph_futs = []
    if api_accounts:
        if provider == "graph":
            if secrets.ig_access_token and secrets.ig_user_id:
                graph_futs = [(a, pool.submit(fetch_graph_ex, a, secrets.ig_access_token, secrets.ig_user_id,
                                              per_account=per, version=cfg_ig.get("graph_api_version", "")))
                              for a in api_accounts]
            else:
                warnings.append("Instagram: нет IG_ACCESS_TOKEN / IG_USER_ID, аккаунты без feed пропущены")
        elif provider == "apify":
            if secrets.apify_token:
                got = pool.submit(fetch_apify, api_accounts, secrets.apify_token,
                                  actor=cfg_ig.get("apify_actor", "apify~instagram-post-scraper"),
                                  per_account=per, extra_input=cfg_ig.get("apify_input")).result()
                by_user: dict[str, list[Item]] = {}
                for it in got:
                    by_user.setdefault((it.author or "").lower(), []).append(it)
                for a in api_accounts:
                    items += tag(a, by_user.get(a["name"].lower(), []))
                    if state is not None:
                        rotation.polled(state, "ig", a["name"], now)
            else:
                warnings.append("Instagram: нет APIFY_TOKEN, аккаунты без feed пропущены")
        else:
            warnings.append(f"Instagram: неизвестный provider «{provider}»")

    for a, f in feed_futs:
        items += tag(a, f.result())
    rate_limited = 0
    for a, f in graph_futs:
        got, err, transient = f.result()
        if err and transient:
            rate_limited += 1
            continue  # аккаунт не проверен: окно не сдвигаем, в следующий раз возьмём шире
        items += tag(a, got)
        if state is not None:
            rotation.polled(state, "ig", a["name"], now)
            if err:
                if rotation.fail(state, "ig", a["name"], err, now):
                    warnings.append(f"Instagram: @{a['name']} отключён на неделю: {err[:120]}")
            else:
                rotation.ok(state, "ig", a["name"])
    if rate_limited:
        warnings.append(f"Instagram: {rate_limited} аккаунтов не проверено из-за лимита или сбоя сети; "
                        "уменьшите instagram.max_per_run, если повторяется")
    return items, warnings
