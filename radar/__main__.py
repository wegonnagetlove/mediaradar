"""Командная строка.

python -m radar run --mode B            обычный запуск (по расписанию)
python -m radar run --mode A            разовый запуск
python -m radar run --mode D            утренний выпуск за ночь
python -m radar run --dry-run           всё, кроме отправки и записи памяти
python -m radar run --no-llm            только сбор: показать свежих кандидатов без нейросети
python -m radar check                   проверить каждый источник и вывести, сколько он отдаёт
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from . import config, pipeline, rotation
from .sources import google_news, instagram, rss, telegram
from .state import State


def _summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def cmd_run(args) -> int:
    manual = (os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
              and os.environ.get("RADAR_STATUS", "yes") != "no")
    rep = pipeline.run(args.mode, dry_run=args.dry_run, no_llm=args.no_llm, manual=manual,
                       ignore_seen=args.ignore_seen)
    print(rep.summary())
    print("\n" + (rep.digest or "Пусто"))
    head = "### Радар: ошибка\n\n" if rep.failed else "### Радар\n\n"
    problems = "\n".join(f"- **{w}**" for w in rep.warnings) + "\n\n" if rep.failed else ""
    _summary(head + problems + "```\n" + rep.summary() + "\n```\n\n" + (rep.digest or "Пусто"))
    if os.environ.get("GITHUB_ACTIONS") and not rep.failed:
        for w in rep.warnings:
            print("::warning title=Радар::" + w.replace("\n", " ").replace("%", "%25"))
        print("::notice title=Радар::" + rep.summary().replace("\n", " "))
        for t in rep.picked:
            print("::notice title=Отобрано::" + t.replace("\n", " "))
    if rep.failed:
        print("\nОШИБКА: " + "; ".join(rep.warnings), file=sys.stderr)
        if os.environ.get("GITHUB_ACTIONS"):
            for w in rep.warnings:  # заметки запуска видны и на странице, и через API
                print("::error title=Радар::" + w.replace("\n", " ").replace("%", "%25"))
        return 1
    return 0


def cmd_check(args) -> int:
    cfg = config.load()
    secrets = config.Secrets.from_env()
    src = cfg["sources"]
    rows: list[tuple[str, str, int]] = []
    notes: list[str] = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        jobs = []
        for ch in src.get("telegram") or []:
            ch = {"name": ch} if isinstance(ch, str) else ch
            jobs.append(("telegram", ch["name"], pool.submit(telegram.fetch, ch["name"])))
        for ch in cfg.get("competitors_telegram") or []:
            jobs.append(("конкурент", ch, pool.submit(telegram.competitor_posts, ch)))
        for feed in src.get("rss") or []:
            jobs.append(("rss", feed["label"], pool.submit(rss.fetch, feed["url"], label=feed["label"], region="intl")))
        for region, qs in (src.get("google_news") or {}).items():
            for q in qs or []:
                jobs.append((f"gnews {region}", q[:50], pool.submit(google_news.fetch, q, region, 24 * 60)))
        st = State()  # память только читается: проверка не сдвигает очередь
        ig_cfg = cfg.get("instagram") or {}
        ig, warn = instagram.collect({**ig_cfg, "min_interval_minutes": 0}, secrets, pool, st)
        for w in warn:
            rows.append(("instagram", w, 0))
        by_acc: dict[str, int] = {}
        for it in ig:
            by_acc[(it.author or "").lower()] = by_acc.get((it.author or "").lower(), 0) + 1
        all_ig = instagram.normalize_accounts(ig_cfg.get("accounts"))
        limit = instagram.per_run_limit(ig_cfg)
        batch = rotation.pick([a for a in all_ig if not a.get("feed")], State(), "ig", limit)
        for a in batch + [a for a in all_ig if a.get("feed")]:
            rows.append(("instagram", "@" + a["name"], by_acc.get(a["name"].lower(), 0)))
        tier1 = sum(1 for a in all_ig if int(a.get("tier", 2)) == 1)
        notes.append(f"Instagram: в базе {len(all_ig)} аккаунтов, tier 1: {tier1}, за запуск до {limit}; "
                     f"полный круг очереди примерно за "
                     f"{rotation.cycle_hours(len(all_ig), tier1, limit, int(ig_cfg.get('run_interval_minutes', 15)))} ч. "
                     "В таблице первая порция очереди.")
        dead = rotation.disabled_list(st, "ig")
        if dead:
            notes.append("Отключены на неделю (ошибка 3 запуска подряд): " +
                         "; ".join(f"@{n} ({e[:60]})" for n, e in dead))
        soc = cfg.get("social_accounts") or {}
        notes.append(f"Threads: {len(soc.get('threads') or [])}, X: {len(soc.get('x') or [])} аккаунтов; "
                     f"нейросеть ищет по {soc.get('per_run', 12)} из каждого списка за запуск.")
        for kind, name, fut in jobs:
            rows.append((kind, name, len(fut.result())))
    width = max(len(r[1]) for r in rows) if rows else 10
    bad = 0
    lines = []
    for kind, name, n in rows:
        mark = "OK " if n else "-- "
        bad += not n
        lines.append(f"{mark}{kind:<11} {name:<{width}}  {n}")
    out = "\n".join(lines) + f"\n\nИсточников без данных: {bad} из {len(rows)}. " \
          "Для google_news проверялось окно 24 часа; пустой ответ у узкого запроса бывает нормой.\n" + "\n".join(notes)
    print(out)
    _summary("### Проверка источников\n\n```\n" + out + "\n```")
    return 0


def cmd_reset(args) -> int:
    st = State()
    st.seen, st.sent, st.meta = {}, [], {}
    st.save()
    print("Память радара очищена.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="radar", description="Newsroom Radar")
    sub = p.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    r.add_argument("--mode", default=os.environ.get("RADAR_MODE", "B"), choices=["A", "B", "D", "a", "b", "d"])
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--no-llm", action="store_true")
    r.add_argument("--ignore-seen", action="store_true", help="диагностика: без памяти и без отправки")
    sub.add_parser("check")
    sub.add_parser("reset")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    if args.cmd == "check":
        return cmd_check(args)
    if args.cmd == "reset":
        return cmd_reset(args)
    if args.cmd is None:
        args = p.parse_args(["run", *(argv or sys.argv[1:])])
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
