import json
from datetime import datetime, timezone
from pathlib import Path

from radar import article
from radar.sources import google_news, instagram, rss, telegram
from radar.timeutil import parse_dt

FX = Path(__file__).parent / "fixtures"


def read(name):
    return (FX / name).read_text(encoding="utf-8")


def test_parse_dt_variants():
    utc = datetime(2026, 10, 28, 18, 35, 12, tzinfo=timezone.utc)
    assert parse_dt("2026-10-28T18:35:12+0000") == utc          # Instagram Graph
    assert parse_dt("2026-10-28T18:35:12Z") == utc
    assert parse_dt("Wed, 28 Oct 2026 18:35:12 +0000") == utc   # RSS
    assert parse_dt("Wed, 28 Oct 2026 21:35:12 +0300") == utc
    assert parse_dt(int(utc.timestamp())) == utc
    assert parse_dt("вчера") is None


def test_telegram_channel():
    posts = telegram.parse_channel(read("tg_channel.html"), "testchan")
    assert [p["post"] for p in posts] == ["testchan/101", "testchan/102", "testchan/103"]
    assert posts[0]["published"] == datetime(2026, 10, 28, 18, 20, tzinfo=timezone.utc)
    assert "40 млн рублей" in posts[0]["text"]
    assert posts[1]["text"] == ""                      # пост только с фото
    assert posts[2]["forwarded_from"] == "Другой канал"
    assert posts[0]["url"] == "https://t.me/testchan/101"


def test_rss_and_atom():
    items = rss.parse_feed(read("rss.xml").encode(), label="WWD", region="intl")
    assert len(items) == 2
    assert items[0].title.startswith("Designer Named")
    assert items[0].summary == "The appointment ends a six-month search."
    assert items[0].author == "Jane Doe"
    atom = rss.parse_feed(read("atom.xml").encode(), label="Blog", region="intl")
    assert atom[0].url == "https://example.org/museum"
    assert atom[0].published == datetime(2026, 10, 28, 18, 10, tzinfo=timezone.utc)


def test_rss_garbage_does_not_raise():
    assert rss.parse_feed(b"<html>not a feed", label="x", region="intl") == []


def test_google_news_title_and_domain(monkeypatch):
    def fake_fetch(url, *, label, region, source):
        assert "when%3A1h" in url and "hl=ru" in url
        return rss.parse_feed(read("gnews.xml").encode(), label=label, region=region, source=source)
    monkeypatch.setattr(rss, "fetch", fake_fetch)
    items = google_news.fetch("site:theblueprint.ru", "ru", 60)
    assert items[0].title == "Бренд закрывает магазин на Петровке"
    assert items[0].label == "The Blueprint"
    assert items[0].extra["domain"] == "theblueprint.ru"
    assert items[0].summary == ""


def test_instagram_graph():
    items = instagram.parse_graph(json.loads(read("ig_graph.json")), "diet_prada", "intl")
    assert len(items) == 2
    first = items[0]
    assert first.label == "@diet_prada (Instagram)"
    assert first.title == "Is this the most blatant copy of the season?"
    assert first.published == datetime(2026, 10, 28, 18, 35, 12, tzinfo=timezone.utc)
    assert first.time_basis == "API"
    assert items[1].title == "Новый пост @diet_prada"


def test_instagram_graph_url():
    assert instagram.graph_url("123") == "https://graph.facebook.com/123"
    assert instagram.graph_url("123", "v23.0") == "https://graph.facebook.com/v23.0/123"


def test_instagram_apify_regions():
    items = instagram.parse_apify(json.loads(read("apify.json")), {"russian_star": "ru"})
    assert items[0].url == "https://www.instagram.com/p/CCC333/"
    assert items[1].url == "https://www.instagram.com/p/DDD444/"
    assert items[1].region == "ru"
    assert items[0].region == "intl"


def test_instagram_accounts_normalize():
    accs = instagram.normalize_accounts(["@bof", {"name": "star", "region": "ru"}, {"bad": 1}])
    assert accs == [{"name": "bof", "region": "intl"}, {"name": "star", "region": "ru"}]


def test_instagram_collect_without_token_warns():
    from concurrent.futures import ThreadPoolExecutor
    from radar.config import Secrets
    s = Secrets("", "", "", "", "", "")
    with ThreadPoolExecutor(2) as pool:
        items, warn = instagram.collect({"enabled": True, "provider": "graph", "accounts": ["bof"]}, s, pool)
    assert items == [] and "IG_ACCESS_TOKEN" in warn[0]


def test_article_meta_time_and_text():
    a = article.parse(read("article.html"), "https://wwd.com/a/1")
    assert a.published == datetime(2026, 10, 28, 18, 31, tzinfo=timezone.utc)
    assert "five years" in a.text and "Short." not in a.text and "menu" not in a.text


def test_images_from_sources():
    feed = rss.parse_feed(read("rss.xml").encode(), label="WWD", region="intl")
    assert feed[0].extra["images"] == ["https://wwd.com/img/1.jpg"]
    assert feed[1].extra["images"] == ["https://wwd.com/img/0.jpg"]
    posts = telegram.parse_channel(read("tg_channel.html"), "testchan")
    assert posts[1]["images"] == ["https://cdn4.telesco.pe/file/abc.jpg"] and posts[0]["images"] == []
    ig = instagram.parse_graph(json.loads(read("ig_graph.json")), "diet_prada", "intl")
    assert ig[0].extra["images"] == ["https://cdn.ig/1.jpg", "https://cdn.ig/v.jpg"]   # карусель, у видео обложка
    assert ig[1].extra["images"] == ["https://cdn.ig/2.jpg"]
    ap = instagram.parse_apify(json.loads(read("apify.json")), {})
    assert ap[0].extra["images"] == ["https://cdn.ig/c.jpg"] and "images" not in ap[1].extra


def test_article_og_image():
    html = read("article.html").replace("<head>", '<head><meta property="og:image" content="https://wwd.com/og.jpg">')
    assert article.parse(html, "https://wwd.com/a/1").image == "https://wwd.com/og.jpg"


def test_instagram_collect_rotation_and_failures(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from radar.config import Secrets
    from radar.state import State
    st = State(tmp_path / "s.json")
    calls = []

    def fake(account, token, uid, **k):
        calls.append(account["name"])
        if account["name"] == "dead":
            return [], "Invalid user", False
        if account["name"] == "limited":
            return [], "Application request limit reached", True
        return instagram.parse_graph(json.loads(read("ig_graph.json")), account["name"], "intl"), "", False

    monkeypatch.setattr(instagram, "fetch_graph_ex", fake)
    cfg = {"enabled": True, "provider": "graph", "max_per_run": 3,
           "accounts": ["dead", "limited", {"name": "ok", "tier": 1}, "b", "c"]}
    s = Secrets("", "", "", "tok", "uid", "")
    with ThreadPoolExecutor(4) as pool:
        for _ in range(6):
            items, warn = instagram.collect(cfg, s, pool, st)
    assert any("@dead отключён" in w for w in warn) or "dead" in st.meta["ig_fail"]
    assert st.meta["ig_fail"]["dead"]["n"] >= 3 and "limited" not in st.meta["ig_fail"]
    assert all(it.extra.get("window_minutes") for it in items)
    assert calls.count("ok") == 6   # tier 1 в каждом запуске
