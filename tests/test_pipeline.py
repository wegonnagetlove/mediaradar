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
    monkeypatch.setattr(pipeline, "State", lambda: State(tmp_path / "seen.json"))
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


def test_full_run_sends_ru_first(cfg, monkeypatch):
    items = [mk("Designer exits house", 15), mk("Рэпер ушёл с лейбла", 30, region="ru", source="telegram", label="@chan")]
    monkeypatch.setattr(pipeline, "collect", lambda *a, **k: items)
    monkeypatch.setattr(pipeline, "competitor_material", lambda *a, **k: [])
    monkeypatch.setattr(pipeline.article, "fetch", lambda url: None)
    monkeypatch.setattr(pipeline, "LLM", FakeLLM)
    sent = []
    monkeypatch.setattr(pipeline.Telegram, "send", lambda self, t, **k: sent.append(t) or True)
    secrets = config.Secrets("key", "bot", "chat", "", "", "")
    rep = pipeline.run("B", cfg=cfg, secrets=secrets)
    assert rep.picks == 2 and rep.sent == 2
    assert sent[0].startswith("⚡️ 2 новости: Заголовок @chan; Заголовок WWD")
    assert sent[1].startswith("[Время публикации:") and "[Статус: не опубликовано ни у кого]" in sent[1]
    assert "⚡️ Заголовок @chan" in sent[1]
    # повторный запуск: всё уже видели, ничего не отправляется
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
                                      stop_topics="", candidates="[]", competitor_posts="")
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
    assert full.startswith("<b>Утренний выпуск,")
    assert "20 новостей за ночь" in full
    assert full.index("<b>Россия</b>") < full.index("<b>Мир</b>")
    assert '<a href="https://x.test/' in full and "H&amp;M &lt;Ночная новость" in full
    assert "1. ⚡️ <a href" in full and "уже есть у @ksbchk" in full
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
    assert len(sent) == 1 and "подходящих новостей нет" in sent[0]


def test_digest_prompt_formats():
    config.prompt("digest.md").format(channels="", now="", hours=7.5, max_posts=20, topics="", stop_topics="",
                                      candidates="[]", competitor_posts="")


def test_schedule_covers_moscow_day():
    import yaml
    wf = yaml.safe_load(open(config.ROOT / ".github/workflows/radar.yml", encoding="utf-8"))
    crons = [c["cron"] for c in wf[True]["schedule"]]  # PyYAML читает ключ on как True
    assert "55 3 * * *" in crons and "*/15 5-20 * * *" in crons
