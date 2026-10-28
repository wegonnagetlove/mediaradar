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

from . import config, pipeline
from .sources import google_news, instagram, rss, telegram
from .state import State


def _summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def cmd_run(args) -> int:
    rep = pipeline.run(args.mode, dry_run=args.dry_run, no_llm=args.no_llm)
    print(rep.summary())
    print("\n" + (rep.digest or "Пусто"))
    _summary("### Радар\n\n```\n" + rep.summary() + "\n```\n\n" + (rep.digest or "Пусто"))
    return 0


def cmd_check(args) -> int:
    cfg = config.load()
    secrets = config.Secrets.from_env()
    src = cfg["sources"]
    rows: list[tuple[str, str, int]] = []
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
        ig, warn = instagram.collect({**(cfg.get("instagram") or {}), "min_interval_minutes": 0}, secrets, pool)
        for w in warn:
            rows.append(("instagram", w, 0))
        by_acc: dict[str, int] = {}
        for it in ig:
            by_acc[it.author] = by_acc.get(it.author, 0) + 1
        for a in instagram.normalize_accounts((cfg.get("instagram") or {}).get("accounts")):
            rows.append(("instagram", "@" + a["name"], by_acc.get(a["name"], 0)))
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
          "Для google_news проверялось окно 24 часа; пустой ответ у узкого запроса бывает нормой."
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
