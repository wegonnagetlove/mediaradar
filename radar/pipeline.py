"""Один запуск радара: собрать → отсеять → отобрать → написать → отправить."""
from __future__ import annotations

import html
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import article, config, rotation
from .llm import LLM, LLMError, extract_json
from .models import CompetitorPost, Item
from .notify import Telegram, is_night
from .sources import google_news, instagram, rss, telegram
from .state import State
from .timeutil import now_utc, parse_dt

log = logging.getLogger("radar")

TZ_LABEL = "МСК"


@dataclass
class Report:
    mode: str
    started: datetime
    collected: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    fresh: int = 0
    candidates: int = 0
    picks: int = 0
    sent: int = 0
    digest: str = ""
    failed: bool = False

    def summary(self) -> str:
        src = ", ".join(f"{k}: {v}" for k, v in self.collected.items())
        lines = [f"Режим {self.mode}. Собрано по источникам: {src or 'ничего'}.",
                 f"Свежих и новых: {self.fresh}; отдано на отбор: {self.candidates}; отобрано: {self.picks}; отправлено: {self.sent}."]
        lines += [f"Предупреждение: {w}" for w in self.warnings]
        return "\n".join(lines)


# ── сбор ─────────────────────────────────────────────────────────────────

def collect(cfg: config.Config, secrets: config.Secrets, state: State, max_age: int, report: Report) -> list[Item]:
    src = cfg["sources"]
    items: list[Item] = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        jobs = {}
        for ch in src.get("telegram") or []:
            ch = {"name": ch} if isinstance(ch, str) else ch
            jobs[pool.submit(telegram.fetch, ch["name"], label=ch.get("label"), region=ch.get("region", "ru"))] = "telegram"
        for feed in src.get("rss") or []:
            jobs[pool.submit(rss.fetch, feed["url"], label=feed["label"], region=feed.get("region", "intl"))] = "rss"
        for region, queries in (src.get("google_news") or {}).items():
            for q in queries or []:
                jobs[pool.submit(google_news.fetch, q, region, max_age)] = "google_news"

        ig_cfg = cfg.get("instagram") or {}
        ig_due = ig_cfg.get("enabled") and instagram.due(ig_cfg, state)
        if ig_due:
            ig_items, ig_warn = instagram.collect(ig_cfg, secrets, pool, state)
            items += ig_items
            report.collected["instagram"] = len(ig_items)
            report.warnings += ig_warn
            state.meta["instagram_last_run"] = now_utc().isoformat()
            if not ig_items and not ig_warn:
                report.warnings.append("Instagram: ни одного поста, проверьте токен и имена аккаунтов")

        for fut, kind in jobs.items():
            got = fut.result()
            report.collected[kind] = report.collected.get(kind, 0) + len(got)
            items += got
    return items


def competitor_material(cfg: config.Config, state: State) -> list[CompetitorPost]:
    posts: list[CompetitorPost] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        tg = [pool.submit(telegram.competitor_posts, ch) for ch in cfg.get("competitors_telegram") or []]
        media = [pool.submit(google_news.recent_from_domain, d, "ru", 24) for d in cfg.get("competitors_media") or []]
        for f in tg:
            posts += f.result()
        for f in media:
            for it in f.result()[:15]:
                posts.append(CompetitorPost(channel=it.extra.get("domain") or it.label, text=it.title,
                                            published=it.published, url=it.url))
    for s in state.recent_sent(48):
        posts.append(CompetitorPost(channel="МЫ (уже отправлено редактору)", text=s["title"],
                                    published=parse_dt(s["at"]), url=s.get("url", "")))
    return posts


def _norm(title: str) -> str:
    return re.sub(r"\W+", " ", title.lower()).strip()[:90]


def select_candidates(items: list[Item], cfg: config.Config, state: State, max_age: int, now: datetime) -> list[Item]:
    border = now - timedelta(minutes=max_age)
    competitor_domains = {d.lower().removeprefix("www.") for d in cfg.get("competitors_media") or []}
    stop = [s.lower() for s in cfg.get("stop_topics") or []]
    seen_titles: set[str] = set()
    fresh: list[Item] = []
    for it in sorted(items, key=lambda i: i.published):  # самая ранняя копия сюжета побеждает
        own = it.extra.get("window_minutes")  # аккаунт из очереди: окно с его прошлой проверки
        item_border = now - timedelta(minutes=max(max_age, int(own))) if own else border
        if it.published < item_border or it.published > now + timedelta(minutes=10):
            continue
        if not state.is_new(it.id):
            continue
        key = _norm(it.title)
        if key in seen_titles:
            continue
        if any(s in (it.title + " " + it.summary).lower() for s in stop):
            continue
        seen_titles.add(key)
        dom = it.extra.get("domain") or google_news.host(it.url)
        if dom in competitor_domains:
            it.extra["in_competitor_media"] = True
        fresh.append(it)
    return fresh


def _social_names(raw) -> list[dict]:
    out = []
    for a in raw or []:
        if isinstance(a, str):
            out.append({"name": a.lstrip("@")})
        elif isinstance(a, dict) and a.get("name"):
            out.append({**a, "name": str(a["name"]).lstrip("@")})
    return out


def social_scan(llm: LLM, cfg: config.Config, max_age: int, now: datetime, state: State | None = None) -> list[Item]:
    raw = cfg.get("social_accounts") or {}
    per_run = int(raw.get("per_run", 12)) if isinstance(raw, dict) else 12
    accounts = {}
    for net_name in ("instagram", "threads", "x"):
        lst = _social_names(raw.get(net_name))
        if state is not None:
            lst = rotation.pick(lst, state, f"social_{net_name}", per_run, now)
        accounts[net_name] = [a["name"] for a in lst]
    prompt = config.prompt("social.md").format(
        now=now.astimezone(cfg.tz).strftime("%d.%m.%Y %H:%M"), now_utc=now.strftime("%Y-%m-%d %H:%M"),
        max_age=max_age,
        instagram=", ".join("@" + a for a in accounts.get("instagram") or []) or "нет",
        threads=", ".join("@" + a for a in accounts.get("threads") or []) or "нет",
        x=", ".join("@" + a for a in accounts.get("x") or []) or "нет",
    )
    data = extract_json(llm.complete("Ты новостной мониторинг соцсетей. Отвечаешь только JSON.", prompt,
                                     web_search=True, max_searches=10))
    out = []
    for p in data.get("posts", []) if isinstance(data, dict) else []:
        dt = parse_dt(p.get("published_utc"))
        if not (dt and p.get("url") and p.get("text")):
            continue
        platform = {"instagram": "Instagram", "threads": "Threads", "x": "X"}.get(p.get("platform", ""), p.get("platform", ""))
        out.append(Item(source="social", label=f"@{p.get('account', '').lstrip('@')} ({platform})",
                        region=p.get("region", "intl"), title=p["text"][:200], url=p["url"], published=dt,
                        summary=p["text"], time_basis="поиск" if p.get("basis") == "пост" else "пересказ"))
    return out


# ── отбор и текст ────────────────────────────────────────────────────────

def triage(llm: LLM, cfg, mode: dict, cands: list[Item], comp: list[CompetitorPost], now: datetime) -> list[dict]:
    tz = cfg.tz
    comp_json = [{"channel": c.channel, "time": c.published.astimezone(tz).strftime("%d.%m %H:%M") if c.published else "",
                  "text": c.text[:400]} for c in comp]
    prompt = config.prompt("triage.md").format(
        channels=", ".join(cfg["channels"]), now=now.astimezone(tz).strftime("%d.%m.%Y %H:%M"),
        max_age=mode["max_age_minutes"], max_posts=mode["max_posts"], min_posts=mode["min_posts"],
        topics=" ".join(str(cfg.get("topics_hint", "")).split()), stop_topics=", ".join(cfg.get("stop_topics") or []) or "нет",
        candidates=json.dumps([c.for_llm(tz) for c in cands], ensure_ascii=False),
        competitor_posts=json.dumps(comp_json, ensure_ascii=False) if comp_json else "нет данных",
    )
    data = extract_json(llm.complete("Ты выпускающий редактор. Отвечаешь только JSON.", prompt))
    picks = data.get("picks", []) if isinstance(data, dict) else []
    by_id = {c.id: c for c in cands}
    valid = [p for p in picks if p.get("id") in by_id]
    return valid[: mode["max_posts"]]


def enrich(picks: list[dict], by_id: dict[str, Item], strict: bool, max_age: int, now: datetime) -> list[dict]:
    """Открывает первоисточники: уточняет время и добирает текст для фактуры."""
    with ThreadPoolExecutor(max_workers=6) as pool:
        arts = list(pool.map(lambda p: article.fetch(by_id[p["id"]].url), picks))
    out = []
    border = now - timedelta(minutes=max_age + 15)
    for p, art in zip(picks, arts):
        it = by_id[p["id"]]
        if art and art.published:
            if art.published < border:
                log.info("отброшено, страница старше окна: %s", it.url)
                continue
            it.published, it.time_basis = art.published, "страница"
        elif strict and it.source in ("rss", "gnews"):
            log.info("отброшено, время не подтверждено страницей: %s", it.url)
            continue
        if art and art.image and not it.extra.get("images"):
            it.extra["images"] = [art.image]
        p["_item"] = it
        p["_text"] = art.text if art and art.text else ""
        out.append(p)
    return out


def write_posts(llm: LLM, cfg, picks: list[dict], now: datetime) -> dict[str, str]:
    payload = []
    for p in picks:
        it: Item = p["_item"]
        payload.append({"id": it.id, "urgent": bool(p.get("urgent")), "source": it.label, "type": it.source,
                        "url": it.url, "title": it.title, "summary": it.summary,
                        "article_text": p.get("_text", "")[:5000], "why": p.get("why", "")})
    prompt = config.prompt("writer.md").format(
        channels=", ".join(cfg["channels"]), now=now.astimezone(cfg.tz).strftime("%d.%m.%Y %H:%M"),
        picks=json.dumps(payload, ensure_ascii=False))
    data = extract_json(llm.complete("Ты автор новостного телеграм-канала. Отвечаешь только JSON.", prompt))
    return {x["id"]: x["post"].strip() for x in data.get("posts", []) if x.get("id") and x.get("post")}


def plural_news(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} новость"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} новости"
    return f"{n} новостей"


def header(it: Item, status: str, tz) -> str:
    t = it.published.astimezone(tz).strftime("%d.%m, %H:%M")
    basis = "" if it.time_basis in ("страница", "API") else f", время по: {it.time_basis}"
    return f"[Время публикации: {t}, {TZ_LABEL}{basis}] [Источник: {it.label}, {it.url}] [Статус: {status}]"


def source_footer(it: Item, p: dict) -> str:
    """Последняя строка дневного поста: ссылка на первоисточник и, если сюжет не эксклюзив, у кого он уже есть."""
    foot = f'<a href="{html.escape(it.url, quote=True)}">Источник</a>'
    status = (p.get("status") or "").strip()
    if status and not p.get("exclusive", True):
        foot += f" · {html.escape(status)}"
    return foot


def order(picks: list[dict]) -> list[dict]:
    # российские эксклюзивы первыми, внутри групп самая свежая первой
    return sorted(picks, key=lambda p: (p["_item"].region != "ru", not p.get("exclusive", False),
                                        -p["_item"].published.timestamp()))


# ── запуск ───────────────────────────────────────────────────────────────

def run(mode_name: str = "B", *, dry_run: bool = False, no_llm: bool = False, cfg=None, secrets=None,
        manual: bool = False) -> Report:
    """Обёртка: проверка связи с Telegram, сам запуск, итоговый отчёт и ошибки отправки."""
    cfg = cfg or config.load()
    secrets = secrets or config.Secrets.from_env()
    now = now_utc()
    report = Report(mode=mode_name.upper(), started=now)
    tg = Telegram(secrets.telegram_bot_token, secrets.telegram_chat_id,
                  silent=bool(cfg["notify"].get("silent_night")) and is_night(now.astimezone(cfg.tz)))
    use_tg = bool(cfg["notify"].get("telegram")) and not dry_run
    if use_tg:
        problem = tg.check()
        if problem:
            report.warnings.append(f"Telegram: {problem}")
            report.failed = True
            tg.token = ""  # дальше не пытаемся слать в заведомо неверный чат
    _run(mode_name, dry_run=dry_run, no_llm=no_llm, cfg=cfg, secrets=secrets, tg=tg, report=report, now=now)
    for e in tg.errors:
        report.warnings.append(f"Telegram: {e}")
        report.failed = True
    if manual and use_tg and tg.ready and not report.failed:
        status = (f"Радар работает. Режим {report.mode}: собрано {sum(report.collected.values())}, "
                  f"свежих {report.fresh}, отобрано {report.picks}, отправлено {report.sent}.")
        if not report.picks:
            status += " Подходящих новостей за окно не нашлось."
        other = [w for w in report.warnings if not w.startswith("Telegram")]
        if other:
            status += "\n\n" + "\n".join(f"• {w}" for w in other[:6])
        tg.send(status)
    return report


def _run(mode_name: str, *, dry_run: bool, no_llm: bool, cfg, secrets, tg: Telegram, report: Report, now) -> Report:
    mode = cfg.mode(mode_name)
    max_age = int(mode["max_age_minutes"])
    tz = cfg.tz
    state = State()
    notify_cfg = cfg["notify"]

    if cfg.get("engine") == "agent" and not no_llm:
        return _run_agent(cfg, secrets, mode_name, now, tg, dry_run, report, state)

    items = collect(cfg, secrets, state, max_age, report)
    llm = None
    if not no_llm:
        try:
            llm = LLM(cfg["llm"], secrets.llm_api_key)
        except LLMError as e:
            report.warnings.append(str(e))
    if llm and cfg.get("social_search") and llm.can_search:
        try:
            social = social_scan(llm, cfg, max_age, now, state)
            report.collected["social_search"] = len(social)
            items += social
        except (LLMError, ValueError) as e:
            report.warnings.append(f"поиск по соцсетям: {e}")

    fresh = select_candidates(items, cfg, state, max_age, now)
    report.fresh = len(fresh)
    fresh.sort(key=lambda i: (i.region != "ru", -i.published.timestamp()))
    cands = fresh[: int(mode.get("max_candidates") or cfg.get("max_candidates_to_llm", 60))]
    report.candidates = len(cands)

    if no_llm or not llm:
        report.digest = "\n\n".join(f"{c.region.upper()} | {c.published.astimezone(tz):%d.%m %H:%M} | {c.label}\n{c.title}\n{c.url}" for c in cands)
        if not dry_run:
            state.mark_seen(i.id for i in cands)
            state.save()
        return report

    if mode.get("digest"):
        return _run_digest(cfg, mode, llm, cands, now, tg, dry_run, report, state)

    posts_out: list[tuple[dict, str]] = []
    undelivered: set[str] = set()
    picks: list[dict] = []
    if cands:
        comp = competitor_material(cfg, state)
        try:
            picks = triage(llm, cfg, mode, cands, comp, now)
            by_id = {c.id: c for c in cands}
            picks = order(enrich(picks, by_id, bool(cfg.get("strict_time")), max_age, now))
            report.picks = len(picks)
            posts = write_posts(llm, cfg, picks, now) if picks else {}
        except (LLMError, ValueError) as e:
            _fail(state, tg, report, f"нейросеть: {e}", dry_run)
            return report
        for p in picks:
            it = p["_item"]
            body = posts.get(it.id) or f"{it.title}\n\n{it.summary[:500]}"
            if p.get("urgent") and not body.startswith("⚡"):
                body = "⚡️ " + body
            posts_out.append((p, body))

    state.meta["fail_streak"] = 0
    if posts_out:
        report.digest = "\n\n———\n\n".join(f"{b}\n\n{p['_item'].url}" for p, b in posts_out)
        if not dry_run:
            max_photos = int(notify_cfg.get("max_photos", 4)) if notify_cfg.get("photos", True) else 0
            for p, body in posts_out:
                it = p["_item"]
                delivered = True
                if notify_cfg.get("telegram") and tg.ready:
                    delivered = tg.send_post(body, footer=source_footer(it, p) if notify_cfg.get("source_link", True) else "",
                                             images=(it.extra.get("images") or [])[:max_photos], link=it.url)
                elif notify_cfg.get("telegram"):
                    delivered = False  # бот не настроен: новость не теряем, попробуем в следующий раз
                if delivered:
                    state.mark_sent(body.lstrip("⚡️ ").split("\n", 1)[0][:160], it.url)
                    report.sent += 1
                else:
                    undelivered.add(it.id)
    else:
        report.digest = "Пусто"
        if not dry_run and notify_cfg.get("send_empty") and tg.ready:
            tg.send(f"Пусто ({now.astimezone(tz):%H:%M})")

    if not dry_run:
        # что не влезло в лимит или не дошло до Telegram, уйдёт на отбор в следующий раз
        state.mark_seen(i.id for i in cands if i.id not in undelivered)
        state.save()
        _save_digest(cfg, report, now)
    return report


def _run_agent(cfg, secrets, mode_name, now, tg, dry_run, report, state) -> Report:
    try:
        llm = LLM(cfg["llm"], secrets.llm_api_key)
        system = config.prompt("editor.md")
        user = (f"Сейчас {now.astimezone(cfg.tz):%d.%m.%Y %H:%M} (Москва). Режим {mode_name.upper()}. "
                f"Окно свежести {cfg.mode(mode_name)['max_age_minutes']} минут. Выполни задание.")
        sent = state.recent_sent(48)
        if sent:
            user += "\nУже отправлено (не повторять): " + "; ".join(s["title"] for s in sent[-40:])
        text = llm.complete(system, user, web_search=True, max_searches=20)
    except LLMError as e:
        _fail(state, tg, report, f"нейросеть: {e}", dry_run)
        return report
    report.digest = text
    state.meta["fail_streak"] = 0
    empty = text.strip().strip(".").lower() in ("пусто", "за последний час подходящих новостей нет")
    if not dry_run:
        if not empty and tg.ready:
            tg.send(text)
            first = text.split("\n", 1)[0]
            state.mark_sent(first, "")
            report.sent = 1
        elif empty and cfg["notify"].get("send_empty") and tg.ready:
            tg.send("Пусто")
        state.save()
        _save_digest(cfg, report, now)
    return report


def _fail(state: State, tg: Telegram, report: Report, msg: str, dry_run: bool) -> None:
    log.error(msg)
    report.warnings.append(msg)
    if dry_run:
        return
    streak = int(state.meta.get("fail_streak", 0)) + 1
    state.meta["fail_streak"] = streak
    if streak == 3 and tg.ready:  # одно сообщение, а не спам каждые 15 минут
        tg.send(f"Радар: три запуска подряд с ошибкой. Последняя: {msg[:500]}")
    state.save()


def _save_digest(cfg, report: Report, now: datetime) -> None:
    if not cfg["notify"].get("save_digest") or report.digest in ("", "Пусто"):
        return
    local = now.astimezone(cfg.tz)
    path = config.ROOT / "digests" / local.strftime("%Y-%m-%d") / f"{local:%H%M}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.digest + "\n", encoding="utf-8")


# ── утренний выпуск ──────────────────────────────────────────────────────

def digest_triage(llm: LLM, cfg, mode: dict, cands: list[Item], comp: list[CompetitorPost], now: datetime) -> list[dict]:
    tz = cfg.tz
    comp_json = [{"channel": c.channel, "text": c.text[:300]} for c in comp]
    prompt = config.prompt("digest.md").format(
        channels=", ".join(cfg["channels"]), now=now.astimezone(tz).strftime("%d.%m.%Y %H:%M"),
        hours=round(mode["max_age_minutes"] / 60, 1), max_posts=mode["max_posts"],
        topics=" ".join(str(cfg.get("topics_hint", "")).split()), stop_topics=", ".join(cfg.get("stop_topics") or []) or "нет",
        candidates=json.dumps([c.for_llm(tz) for c in cands], ensure_ascii=False),
        competitor_posts=json.dumps(comp_json, ensure_ascii=False) if comp_json else "нет данных",
    )
    data = extract_json(llm.complete("Ты выпускающий редактор утреннего выпуска. Отвечаешь только JSON.", prompt))
    picks = data.get("picks", []) if isinstance(data, dict) else []
    ids = {c.id for c in cands}
    out, used = [], set()
    for p in picks:
        if p.get("id") in ids and p["id"] not in used and p.get("headline"):
            used.add(p["id"])
            out.append(p)
    return out[: mode["max_posts"]]


def digest_lines(picks: list[dict], tz=None) -> list[str]:
    """Строки утреннего выпуска: только заголовок‑ссылка, сначала Россия, потом мир."""
    ordered = [p for p in picks if p["_item"].region == "ru"] + [p for p in picks if p["_item"].region != "ru"]
    return [f'<a href="{html.escape(p["_item"].url, quote=True)}">{html.escape(p["headline"].strip())}</a>'
            for p in ordered]


def _run_digest(cfg, mode, llm, cands, now, tg, dry_run, report, state) -> Report:
    tz = cfg.tz
    local = now.astimezone(tz)
    picks: list[dict] = []
    if cands:
        comp = competitor_material(cfg, state)
        try:
            picks = digest_triage(llm, cfg, mode, cands, comp, now)
            by_id = {c.id: c for c in cands}
            picks = enrich(picks, by_id, bool(cfg.get("strict_time")), int(mode["max_age_minutes"]), now)
        except (LLMError, ValueError) as e:
            _fail(state, tg, report, f"утренний выпуск: {e}", dry_run)
            return report
    state.meta["fail_streak"] = 0
    report.picks = len(picks)
    head = "Доброе утро!" if picks else "Доброе утро! За ночь ничего важного не случилось."
    lines = digest_lines(picks)
    report.digest = head + ("\n\n" + "\n\n".join(lines) if lines else "")
    if not dry_run:
        if tg.ready:
            tg.send_list(head, lines)
            report.sent = len(picks)
        for p in picks:
            state.mark_sent(p["headline"], p["_item"].url)
        state.mark_seen(i.id for i in cands)
        state.save()
        _save_digest(cfg, report, now)
    return report
