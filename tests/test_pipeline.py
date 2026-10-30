import json
from datetime import timedelta

import pytest

from radar import config, llm, notify, pipeline
from radar.models import Item
from radar.state import State
from radar.timeutil import now_utc


def mk(title, minutes_ago, region="intl", url=None, source="rss", label="WWD"):
    return Item(source=source, label=label, region=region, title=title,
                url=url or f"https://x.test/{abs(hash(title))}", published=now_utc() - timedelta(minutes=minutes_ago),
                summary=title + " подробности")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    c = config.load()
    c["instagram"]["enabled"] = False
    c["social_search"] = False
    c["notify"]["save_digest"] = False
    c["schedule"] = {}  # расписание проверяется отдельно, остальные тесты от часов не зависят
    monkeypatch.setattr(pipeline, "State", lambda: State(tmp_path / "seen.json"))
    monkeypatch.setattr(pipeline.Telegram, "check", lambda self: "")  # без сети
    return c


def test_extract_json_variants():
    assert llm.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.extract_json('Вот ответ: {"picks": []} конец') == {"picks": []}
    with pytest.raises(ValueError):
        llm.extract_json("ничего")


def test_split_long_text():
    text = ("абзац " * 300 + "\n") * 5
    parts = notify.split(text, 1000)
    assert all(len(p) <= 1000 for p in parts)
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")


def test_plural():
    assert [pipeline.plural_news(n) for n in (1, 2, 5, 11, 21, 22)] == [
        "1 новость", "2 новости", "5 новостей", "11 новостей", "21 новость", "22 новости"]


def test_select_candidates_window_seen_dupes(cfg, tmp_path):
    st = State(tmp_path / "s.json")
    old = mk("Старое", 90)
    seen = mk("Уже видели", 5)
    st.mark_seen([seen.id])
    a = mk("Один сюжет", 20, url="https://a.test/1")
    b = mk("Один  сюжет!", 10, url="https://b.test/2")       # копия того же заголовка позже
    comp = mk("У конкурента", 5, url="https://theblueprint.ru/x", region="ru")
    stop = mk("Прогноз погоды на завтра", 5)
    cfg["stop_topics"] = ["погод"]
    out = pipeline.select_candidates([old, seen, a, b, comp, stop], cfg, st, 60, now_utc())
    titles = [i.title for i in out]
    assert titles == ["Один сюжет", "У конкурента"]
    assert out[1].extra["in_competitor_media"] is True


class FakeLLM:
    can_search = False

    def __init__(self, *a, **k):
        self.calls = []

    def complete(self, system, user, *, web_search=False, max_searches=8):
        self.calls.append(user)
        if "КАНДИДАТЫ" in user:
            cands = json.loads(user.split("КАНДИДАТЫ (JSON)\n", 1)[1].split("\n\nПОСЛЕДНИЕ", 1)[0])
            ru = [c for c in cands if c["region"] == "ru"][0]
            intl = [c for c in cands if c["region"] == "intl"][0]
            return json.dumps({"picks": [
                {"id": intl["id"], "region": "intl", "exclusive": True, "status": "в РФ не опубликовано", "urgent": False},
                {"id": ru["id"], "region": "ru", "exclusive": True, "status": "не опубликовано ни у кого", "urgent": True},
                {"id": "несуществующий"},
            ]})
        picks = json.loads(user.split("СЮЖЕТЫ (JSON)\n", 1)[1].split("\n\nОТВЕТ", 1)[0])
        return json.dumps({"posts": [{"id": p["id"], "post": f"Заголовок {p['source']}\n\nТекст поста."} for p in picks]})


def test_full_run_sends_posts_with_photos(cfg, monkeypatch):
    intl = mk("Designer exits house", 15)
    intl.extra["images"] = ["https://img.test/a.jpg", "https://img.test/b.jpg"]
    ru = mk("Рэпер ушёл с лейбла", 30, region="ru", source="telegram", label="@chan")
    monkeypatch.setattr(pipeline, "collect", lambda *a, **k: [intl, ru])
    monkeypatch.setattr(pipeline, "competitor_material", lambda *a, **k: [])
    monkeypatch.setattr(pipeline.article, "fetch", lambda url: pipeline.article.Article(url, None, "", "https://img.test/og.jpg"))
    monkeypatch.setattr(pipeline, "LLM", FakeLLM)
    sent = []
    monkeypatch.setattr(pipeline.Telegram, "send_post", lambda self, text, **k: sent.append((text, k)) or True)
    secrets = config.Secrets("key", "bot", "chat", "", "", "")
    rep = pipeline.run("B", cfg=cfg, secrets=secrets)
    assert rep.picks == 2 and rep.sent == 2 and len(sent) == 2
    (t1, k1), (t2, k2) = sent
    assert t1.startswith("⚡️ Заголовок @chan")                   # российский первым, без служебной шапки
    assert k1["images"] == ["https://img.test/og.jpg"]          # фото со страницы источника
    assert k2["images"] == ["https://img.test/a.jpg", "https://img.test/b.jpg"]  # фото из ленты важнее
    assert k1["footer"].startswith('<a href="https://x.test/') and ">Источник</a>" in k1["footer"]
    sent.clear()
    rep2 = pipeline.run("B", cfg=cfg, secrets=secrets)
    assert rep2.candidates == 0 and sent == []


def test_no_llm_mode_lists_candidates(cfg, monkeypatch):
    monkeypatch.setattr(pipeline, "collect", lambda *a, **k: [mk("Свежая новость", 5, region="ru")])
    rep = pipeline.run("B", cfg=cfg, secrets=config.Secrets("", "", "", "", "", ""), no_llm=True, dry_run=True)
    assert "Свежая новость" in rep.digest


def test_llm_failure_counts_streak(cfg, monkeypatch, tmp_path):
    class Broken(FakeLLM):
        def complete(self, *a, **k):
            raise llm.LLMError("HTTP 401")
    monkeypatch.setattr(pipeline, "collect", lambda *a, **k: [mk("Новость", 5)])
    monkeypatch.setattr(pipeline, "competitor_material", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "LLM", Broken)
    sent = []
    monkeypatch.setattr(pipeline.Telegram, "send", lambda self, t, **k: sent.append(t) or True)
    secrets = config.Secrets("key", "bot", "chat", "", "", "")
    for _ in range(3):
        rep = pipeline.run("B", cfg=cfg, secrets=secrets)
    assert any("401" in w for w in rep.warnings)
    assert len(sent) == 1 and "три запуска подряд" in sent[0]


def test_prompts_format_without_errors(cfg):
    config.prompt("triage.md").format(channels="", now="", max_age=60, max_posts=3, min_posts=0, topics="",
                                      stop_topics="", candidates="[]", competitor_posts="",
                                      policy=pipeline.POLICIES["aware"])
    assert config.load()["selection"] == "aware"
    config.prompt("writer.md").format(channels="", now="", picks="[]")
    config.prompt("social.md").format(now="", now_utc="", max_age=60, instagram="", threads="", x="")


class FakeDigestLLM(FakeLLM):
    def complete(self, system, user, *, web_search=False, max_searches=8):
        cands = json.loads(user.split("КАНДИДАТЫ (JSON)\n", 1)[1].split("\n\nПОСЛЕДНИЕ", 1)[0])
        picks = [{"id": c["id"], "region": c["region"], "headline": f"H&M <{c['title']}>",
                  "line": "Сделка на $5 млн.", "exclusive": i != 1, "status": "уже есть у @ksbchk" if i == 1 else "",
                  "urgent": i == 0} for i, c in enumerate(cands)]
        return json.dumps({"picks": picks + picks[:1]})  # дубль должен отсеяться


def test_morning_digest_html_list(cfg, monkeypatch):
    items = [mk(f"Ночная новость {i}", 30 + i * 20, region="ru" if i % 2 else "intl") for i in range(25)]
    monkeypatch.setattr(pipeline, "collect", lambda *a, **k: items)
    monkeypatch.setattr(pipeline, "competitor_material", lambda *a, **k: [])
    monkeypatch.setattr(pipeline.article, "fetch", lambda url: None)
    monkeypatch.setattr(pipeline, "LLM", FakeDigestLLM)
    sent = []
    monkeypatch.setattr(pipeline.Telegram, "send", lambda self, t, **k: sent.append((t, k)) or True)
    rep = pipeline.run("D", cfg=cfg, secrets=config.Secrets("key", "bot", "chat", "", "", ""))
    assert rep.picks == 20 and rep.sent == 20
    full = "\n\n".join(t for t, _ in sent)
    assert all(k.get("html") for _, k in sent)
    assert full.startswith("Доброе утро!\n\n<a href=")
    blocks = full.split("\n\n")[1:]
    assert len(blocks) == 20 and all(b.startswith('<a href="https://x.test/') and b.endswith("</a>") for b in blocks)
    assert "H&amp;M &lt;Ночная новость" in full
    assert "Сделка" not in full and "@ksbchk" not in full and "⚡" not in full   # только заголовки‑ссылки
    first_intl = next(i for i, b in enumerate(blocks) if any(it.url in b for it in items if it.region == "intl"))
    assert all(any(it.url in b for it in items if it.region == "ru") for b in blocks[:first_intl])
    assert all(len(t) <= 4000 for t, _ in sent)
    # дневной запуск после выпуска ничего не повторяет
    sent.clear()
    monkeypatch.setattr(pipeline, "LLM", FakeLLM)
    rep2 = pipeline.run("B", cfg=cfg, secrets=config.Secrets("key", "bot", "chat", "", "", ""))
    assert rep2.candidates == 0 and sent == []


def test_morning_digest_empty(cfg, monkeypatch):
    monkeypatch.setattr(pipeline, "collect", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "LLM", FakeDigestLLM)
    sent = []
    monkeypatch.setattr(pipeline.Telegram, "send", lambda self, t, **k: sent.append(t) or True)
    pipeline.run("D", cfg=cfg, secrets=config.Secrets("key", "bot", "chat", "", "", ""))
    assert sent == ["Доброе утро! За ночь ничего важного не случилось."]


def test_digest_prompt_formats():
    config.prompt("digest.md").format(channels="", now="", hours=7.5, max_posts=20, topics="", stop_topics="",
                                      candidates="[]", competitor_posts="")


def test_relay_workflow_chains_itself():
    import yaml
    wf = yaml.safe_load(open(config.ROOT / ".github/workflows/relay.yml", encoding="utf-8"))
    steps = wf["jobs"]["relay"]["steps"]
    names = [st.get("name", "") for st in steps]
    assert sum(n.startswith("Проверка ") for n in names) == 12
    assert sum(n.startswith("Сохранить память после") for n in names) == 12
    assert names.index("Запустить следующего дежурного") > names.index("Проверка 12")
    assert "* 300 ))" in steps[names.index("Ждать до проверки 12")]["run"]
    assert wf["permissions"]["actions"] == "write"
    assert "relay.yml/dispatches" in steps[names.index("Запустить следующего дежурного")]["run"]
    manual = yaml.safe_load(open(config.ROOT / ".github/workflows/radar.yml", encoding="utf-8"))
    assert "schedule" not in manual[True] and "status" in manual[True]["workflow_dispatch"]["inputs"]


def test_openai_uses_responses_api(monkeypatch):
    calls = []

    class R:
        status_code = 200
        text = ""

        def json(self):
            return {"output": [{"type": "message", "content": [{"type": "output_text", "text": '{"ok": 1}'}]}]}

    monkeypatch.setattr(llm.net, "post", lambda url, **k: calls.append((url, k["json"])) or R())
    m = llm.LLM({"provider": "openai", "model": "gpt-6-astra", "temperature": 0.6}, "key")
    assert m.complete("sys", "user") == '{"ok": 1}'
    assert m.complete("sys", "user", web_search=True) == '{"ok": 1}'
    (u1, b1), (u2, b2) = calls
    assert u1.endswith("/responses") and "tools" not in b1 and "temperature" not in b1
    assert b2["tools"] == [{"type": "web_search"}]


def test_config_uses_openai():
    c = config.load()
    assert c["llm"]["provider"] == "openai" and c["llm"]["model"] == "gpt-6-astra"


def test_accounts_base_loaded_and_unique():
    c = config.load()
    ig = c["instagram"]["accounts"]
    names = [a["name"].lower() for a in ig]
    assert len(ig) >= 400 and len(names) == len(set(names))
    assert all(isinstance(a["name"], str) and a["region"] in ("ru", "intl") for a in ig)
    total = len(ig) + len(c["social_accounts"]["threads"]) + len(c["social_accounts"]["x"])
    assert total >= 500


def test_rotation_queue(tmp_path):
    from radar import rotation
    st = State(tmp_path / "s.json")
    accs = [{"name": f"top{i}", "tier": 1} for i in range(5)] + [{"name": f"a{i}"} for i in range(20)]
    seen = set()
    for _ in range(4):  # 5 tier1 + 5 из очереди за запуск: 20 рядовых за 4 запуска
        got = rotation.pick(accs, st, "ig", 10)
        assert len(got) == 10 and {a["name"] for a in got[:5]} == {f"top{i}" for i in range(5)}
        seen |= {a["name"] for a in got[5:]}
    assert seen == {f"a{i}" for i in range(20)}
    # три ошибки подряд: аккаунт отключён и в очередь не попадает
    for _ in range(3):
        disabled_now = rotation.fail(st, "ig", "a0", "Invalid user")
    assert disabled_now and rotation.disabled(st, "ig", "a0")
    assert all(a["name"] != "a0" for _ in range(4) for a in rotation.pick(accs, st, "ig", 10))
    assert rotation.disabled_list(st, "ig")[0][0] == "a0"


def test_rotated_account_window(cfg, tmp_path):
    from radar import rotation
    st = State(tmp_path / "s.json")
    old = mk("Пост трёхчасовой давности", 180, source="instagram")
    old.extra["window_minutes"] = 240
    out = pipeline.select_candidates([old, mk("Обычный старый", 180)], cfg, st, 60, now_utc())
    assert [i.title for i in out] == ["Пост трёхчасовой давности"]
    rotation.polled(st, "ig", "x", now_utc() - timedelta(hours=3))
    assert 190 <= rotation.window_minutes(st, "ig", "x", 60, 360) <= 200
    assert rotation.window_minutes(st, "ig", "new", 60, 360) == 60



def test_telegram_problem_fails_run_with_clear_reason(cfg, monkeypatch):
    monkeypatch.setattr(pipeline.Telegram, "check", lambda self: "бот не может написать первым: нажмите «Старт»")
    monkeypatch.setattr(pipeline, "collect", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "LLM", FakeLLM)
    rep = pipeline.run("B", cfg=cfg, secrets=config.Secrets("key", "1:abc", "5", "", "", ""))
    assert rep.failed and "Старт" in rep.warnings[0]


def test_manual_run_sends_status(cfg, monkeypatch):
    monkeypatch.setattr(pipeline, "collect", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "LLM", FakeLLM)
    sent = []
    monkeypatch.setattr(pipeline.Telegram, "send", lambda self, t, **k: sent.append(t) or True)
    rep = pipeline.run("A", cfg=cfg, secrets=config.Secrets("key", "1:abc", "5", "", "", ""), manual=True)
    assert not rep.failed and sent and sent[-1].startswith("Радар работает. Режим A")
    assert "не нашлось" in sent[-1]


def test_send_errors_are_reported(cfg, monkeypatch):
    items = [mk("Designer exits house", 15)]
    monkeypatch.setattr(pipeline, "collect", lambda *a, **k: items)
    monkeypatch.setattr(pipeline, "competitor_material", lambda *a, **k: [])
    monkeypatch.setattr(pipeline.article, "fetch", lambda url: None)

    class OneLLM(FakeLLM):
        def complete(self, system, user, **k):
            if "КАНДИДАТЫ" in user:
                c = json.loads(user.split("КАНДИДАТЫ (JSON)\n", 1)[1].split("\n\nПОСЛЕДНИЕ", 1)[0])[0]
                return json.dumps({"picks": [{"id": c["id"], "exclusive": True}]})
            return super().complete(system, user, **k)
    monkeypatch.setattr(pipeline, "LLM", OneLLM)

    def broken(self, text, **k):
        self._error("sendMessage", 400, '{"ok":false,"description":"Bad Request: chat not found"}')
        return False
    monkeypatch.setattr(pipeline.Telegram, "send_post", broken)
    rep = pipeline.run("B", cfg=cfg, secrets=config.Secrets("key", "1:abc", "5", "", "", ""))
    assert rep.failed and any("TELEGRAM_CHAT_ID" in w for w in rep.warnings)
    assert rep.sent == 0
    # недоставленная новость не помечена как увиденная: следующий запуск попробует снова
    rep2 = pipeline.run("B", cfg=cfg, secrets=config.Secrets("key", "1:abc", "5", "", "", ""))
    assert rep2.candidates == 1



def test_plan_mode_by_moscow_time(tmp_path):
    from datetime import datetime, timezone
    c = config.load()
    st = State(tmp_path / "s.json")
    msk = lambda h, m: datetime(2026, 10, 30, h - 3, m, tzinfo=timezone.utc)  # noqa: E731
    assert pipeline.plan_mode("B", c, st, msk(3, 0))[0] is None            # ночь: тишина
    assert pipeline.plan_mode("B", c, st, msk(6, 40))[0] is None
    assert pipeline.plan_mode("B", c, st, msk(7, 5))[0] == "D"             # первый запуск утра
    assert pipeline.plan_mode("D", c, st, msk(6, 58))[0] == "D"
    st.meta["digest_date"] = "2026-10-30"
    assert pipeline.plan_mode("B", c, st, msk(7, 20))[0] == "B"            # выпуск уже был
    assert pipeline.plan_mode("D", c, st, msk(13, 29))[0] == "B"           # опоздавший выпуск
    st.meta["digest_date"] = "2026-10-29"
    assert pipeline.plan_mode("B", c, st, msk(13, 29))[0] == "B"           # после 11:00 выпуска нет
    assert pipeline.plan_mode("A", c, st, msk(3, 0))[0] == "A"             # ручной A в любое время


def test_digest_marks_day_and_window(cfg, monkeypatch):
    from datetime import datetime, timezone
    cfg["schedule"] = {"quiet_start": "00:00", "digest_time": "06:55", "digest_latest": "11:00"}
    fixed = datetime(2026, 10, 30, 4, 10, tzinfo=timezone.utc)  # 07:10 МСК
    monkeypatch.setattr(pipeline, "now_utc", lambda: fixed)
    seen_age = {}
    def fake_collect(cfg_, secrets_, state_, max_age, report_):
        seen_age["v"] = max_age
        return []
    monkeypatch.setattr(pipeline, "collect", fake_collect)
    monkeypatch.setattr(pipeline, "LLM", FakeDigestLLM)
    sent = []
    monkeypatch.setattr(pipeline.Telegram, "send", lambda self, t, **k: sent.append(t) or True)
    secrets = config.Secrets("key", "1:abc", "5", "", "", "")
    rep = pipeline.run("B", cfg=cfg, secrets=secrets)
    assert rep.mode == "D" and sent[0].startswith("Доброе утро")
    assert 460 <= seen_age["v"] <= 470                       # с 00:00 (−30 мин) до 07:10
    sent.clear()
    rep2 = pipeline.run("B", cfg=cfg, secrets=secrets)
    assert rep2.mode == "B" and not any(t.startswith("Доброе утро") for t in sent)


def test_five_minute_cadence_limits(tmp_path):
    from radar import rotation
    from radar.sources import instagram
    c = config.load()
    assert instagram.per_run_limit(c["instagram"]) == 15          # 180 запросов в час / 12 запусков
    st = State(tmp_path / "s.json")
    accs = [{"name": f"t{i}", "tier": 1} for i in range(20)] + [{"name": f"r{i}"} for i in range(40)]
    seen_top, seen_rest = set(), set()
    for _ in range(5):
        got = rotation.pick(accs, st, "ig", 15)
        assert len(got) == 15
        seen_top |= {a["name"] for a in got if a["name"].startswith("t")}
        seen_rest |= {a["name"] for a in got if a["name"].startswith("r")}
    assert len(seen_top) == 20 and len(seen_rest) == 40          # никто не голодает


def test_every_throttles(tmp_path):
    from datetime import timedelta
    st = State(tmp_path / "s.json")
    t0 = now_utc()
    assert pipeline.every(st, "google_news", 15, t0)
    assert not pipeline.every(st, "google_news", 15, t0 + timedelta(minutes=5))
    assert pipeline.every(st, "google_news", 15, t0 + timedelta(minutes=15))
    assert pipeline.every(st, "x", 0, t0)



def test_balance_gives_every_source_a_place():
    noisy = [mk(f"Шум {i}", i, region="ru", label="@breakingmash", source="telegram") for i in range(80)]
    wwd = [mk(f"WWD {i}", 30 + i, label="WWD") for i in range(5)]
    dezeen = [mk("Dezeen 1", 50, label="Dezeen")]
    out = pipeline.balance(noisy + wwd + dezeen, 20)
    labels = [i.label for i in out]
    assert len(out) == 20 and labels.count("WWD") == 5 and labels.count("Dezeen") == 1
    assert labels[0] == "@breakingmash"                          # российские всё равно первыми
