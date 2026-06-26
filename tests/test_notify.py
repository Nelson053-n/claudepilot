"""
Тесты Telegram-уведомлений (services.notify_telegram + переходы статусов).

urllib мокается, реального сетевого вызова нет. Проверяем:
  • не настроено (нет token/chat_id) → тихий no-op, urlopen не зовётся;
  • переход в done в refresh_running_cards → notify с нужным текстом;
  • повтор того же (cid, event) дедуплицируется (urlopen не зовётся второй раз);
  • переход в failed → ❌-текст; детект waiting → ⏳-текст.
"""
import json

import pytest

import db
import services as svc


class _FakeResp:
    def __enter__(self): return self
    def __exit__(self, *a): return False


@pytest.fixture
def tmp_env(tmp_path, monkeypatch):
    """Изолированная БД и runs/ (+waiting); настроенный TG через env; чистый дедуп.

    sent — список текстов реально «отправленных» сообщений (перехват urlopen).
    """
    test_db = tmp_path / "test.db"
    runs = tmp_path / "runs"
    waiting = runs / "waiting"
    waiting.mkdir(parents=True)
    monkeypatch.setattr(db, "DB_PATH", test_db)
    monkeypatch.setattr(svc, "RUNS", runs)
    monkeypatch.setattr(svc, "WAITING", waiting)
    monkeypatch.setenv("PROF_TG_BOT_TOKEN", "T")
    monkeypatch.setenv("PROF_TG_CHAT_ID", "42")
    svc._TG_SENT.clear()
    db.init_db()

    sent = []

    def fake_urlopen(req, timeout=None):
        # req.data — JSON-тело sendMessage
        sent.append(json.loads(req.data.decode())["text"])
        return _FakeResp()

    monkeypatch.setattr(svc.urllib.request, "urlopen", fake_urlopen)
    yield runs, sent


def _running_card(sid=None, *, prompt="do it"):
    board = db.ensure_board("T", "-home-nel-t")
    card = db.add_card(board["id"], "Моя задача", prompt, "-home-nel-t", column="in_progress")
    db.update_card(card["id"], status="running", pid=None, session_dir=sid)
    return card["id"]


def test_noop_when_not_configured(tmp_path, monkeypatch):
    """Нет token/chat_id (env пуст, settings пуст) → no-op, urlopen не зовётся."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.delenv("PROF_TG_BOT_TOKEN", raising=False)
    monkeypatch.delenv("PROF_TG_CHAT_ID", raising=False)
    db.init_db()
    svc._TG_SENT.clear()
    called = []
    monkeypatch.setattr(svc.urllib.request, "urlopen",
                        lambda *a, **k: called.append(1))
    assert svc.notify_telegram("привет") is False
    assert called == []


def test_done_sends_notification(tmp_env):
    runs, sent = tmp_env
    cid = _running_card()
    (runs / f"card_{cid}.rc").write_text("0\n")
    (runs / f"card_{cid}.out").write_text('{"result":"готово","total_cost_usd":0.05}')

    svc.refresh_running_cards()

    assert db.get_card(cid)["status"] == "done"
    assert len(sent) == 1
    msg = sent[0]
    assert "✅" in msg and "Моя задача" in msg and "$0.05" in msg


def test_done_notification_deduped(tmp_env):
    """Повторный refresh с тем же (cid,'done') не шлёт второе сообщение."""
    runs, sent = tmp_env
    cid = _running_card()
    (runs / f"card_{cid}.rc").write_text("0\n")
    (runs / f"card_{cid}.out").write_text('{"result":"готово","total_cost_usd":0.05}')

    svc.refresh_running_cards()
    # карточка уже done, но переведём обратно в running и финализируем снова —
    # дедуп по (cid,'done') должен подавить повторное уведомление
    db.update_card(cid, status="running")
    svc.refresh_running_cards()

    assert len(sent) == 1


def test_failed_sends_notification(tmp_env):
    runs, sent = tmp_env
    cid = _running_card()
    (runs / f"card_{cid}.rc").write_text("1\n")
    (runs / f"card_{cid}.out").write_text("Traceback: упало")

    svc.refresh_running_cards()

    assert db.get_card(cid)["status"] == "failed"
    assert len(sent) == 1
    assert "❌" in sent[0] and "rc=1" in sent[0]


def test_waiting_sends_notification(tmp_env):
    runs, sent = tmp_env
    sid = "sess-abc"
    cid = _running_card(sid)
    (runs / "waiting" / f"{sid}.json").write_text(json.dumps({"ts": db.now()}))

    res = svc.waiting_cards()
    assert res and res[0]["card_id"] == cid
    assert len(sent) == 1
    assert "⏳" in sent[0] and "Моя задача" in sent[0]

    # повторный poll (reaper/GET) — дедуп по (cid,'waiting'), без второго сообщения
    svc.waiting_cards()
    assert len(sent) == 1
