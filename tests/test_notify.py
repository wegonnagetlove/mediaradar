import json

from radar import notify


class Resp:
    def __init__(self, status=200, ctype="image/jpeg", content=b"x" * 6000):
        self.status_code, self.headers, self.content, self.text = status, {"content-type": ctype}, content, ""


def capture(monkeypatch, image_ok=True):
    calls = []
    monkeypatch.setattr(notify.net, "get", lambda url, **k: Resp() if image_ok else Resp(404, "text/html", b""))
    monkeypatch.setattr(notify.net, "post", lambda url, **k: calls.append((url.rsplit("/", 1)[1], k)) or Resp())
    return calls


def test_single_photo_with_caption(monkeypatch):
    calls = capture(monkeypatch)
    tg = notify.Telegram("t", "c")
    tg.send_post("Moschino предлагают сумку <за 140 тысяч>", footer='<a href="https://s">Источник</a>',
                 images=["https://img/1.jpg"], link="https://s")
    assert [c[0] for c in calls] == ["sendPhoto"]
    data = calls[0][1]["data"]
    assert data["parse_mode"] == "HTML" and "&lt;за 140 тысяч&gt;" in data["caption"]
    assert data["caption"].endswith('<a href="https://s">Источник</a>')


def test_album_caption_on_first(monkeypatch):
    calls = capture(monkeypatch)
    notify.Telegram("t", "c").send_post("Текст", images=["https://a", "https://b"], link="https://s")
    method, k = calls[0]
    media = json.loads(k["data"]["media"])
    assert method == "sendMediaGroup" and len(media) == 2
    assert media[0]["caption"] == "Текст" and "caption" not in media[1]
    assert set(k["files"]) == {"p0", "p1"}


def test_long_text_goes_after_photo(monkeypatch):
    calls = capture(monkeypatch)
    notify.Telegram("t", "c").send_post("а" * 1500, images=["https://a"], link="https://s")
    assert [c[0] for c in calls] == ["sendPhoto", "sendMessage"]
    assert "caption" not in calls[0][1]["data"]
    assert json.loads(calls[1][1]["data"]["link_preview_options"]) == {"is_disabled": True}


def test_no_photo_uses_link_preview(monkeypatch):
    calls = capture(monkeypatch, image_ok=False)
    notify.Telegram("t", "c").send_post("Текст", images=["https://broken"], link="https://s")
    assert [c[0] for c in calls] == ["sendMessage"]
    assert json.loads(calls[0][1]["data"]["link_preview_options"])["url"] == "https://s"


def test_visible_len_ignores_markup():
    assert notify.visible_len('Текст &amp; <a href="https://very/long">Источник</a>') == len("Текст & Источник")
