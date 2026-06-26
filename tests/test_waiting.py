"""
Тесты services.waiting_cards — индикатор «агент ждёт ввода/разрешения».

waiting_cards() матчит маркеры runs/waiting/<session_id>.json (пишет хук
prof_waiting.sh) на running-карточки по cards.session_dir == session_id.
Self-heal: протухший (>30 мин) маркер или появление runs/card_<cid>.rc
(карточка завершена) → маркер снимается, карточка не считается ждущей.
"""
import json

import pytest

import db
import services as svc


@pytest.fixture
def tmp_env(tmp_path, monkeypatch):
    """Изолированная БД и runs/ (+runs/waiting) во временной папке."""
    test_db = tmp_path / "test.db"
    runs = tmp_path / "runs"
    waiting = runs / "waiting"
    waiting.mkdir(parents=True)
    monkeypatch.setattr(db, "DB_PATH", test_db)
    monkeypatch.setattr(svc, "RUNS", runs)
    monkeypatch.setattr(svc, "WAITING", waiting)
    db.init_db()
    yield runs, waiting


def _running_card(sid):
    """running-карточка с заданным session_dir (session_id), pid=None."""
    board = db.ensure_board("T", "-home-nel-t")
    card = db.add_card(board["id"], "title", "do it", "-home-nel-t", column="in_progress")
    db.update_card(card["id"], status="running", pid=None, session_dir=sid)
    return card["id"]


def _marker(waiting, sid, ts, event="Notification"):
    (waiting / f"{sid}.json").write_text(json.dumps({"ts": ts, "event": event}))


def test_waiting_card_found(tmp_env):
    runs, waiting = tmp_env
    cid = _running_card("sid-1")
    _marker(waiting, "sid-1", db.now())

    res = svc.waiting_cards()

    assert len(res) == 1
    assert res[0]["card_id"] == cid
    assert res[0]["waiting_since"] > 0


def test_selfheal_removes_marker_when_rc_present(tmp_env):
    """Карточка завершилась (.rc есть) → маркер снимается, не считается ждущей."""
    runs, waiting = tmp_env
    cid = _running_card("sid-2")
    _marker(waiting, "sid-2", db.now())
    (runs / f"card_{cid}.rc").write_text("0\n")

    res = svc.waiting_cards()

    assert res == []
    assert not (waiting / "sid-2.json").exists()  # маркер подчищен


def test_stale_marker_dropped(tmp_env):
    """Маркер старше 30 мин протух → снимается, карточка не ждущая."""
    runs, waiting = tmp_env
    _running_card("sid-3")
    _marker(waiting, "sid-3", db.now() - svc._WAITING_FRESH - 1)

    res = svc.waiting_cards()

    assert res == []
    assert not (waiting / "sid-3.json").exists()


def test_unmatched_marker_ignored(tmp_env):
    """Маркер без соответствующей running-карточки (чужая сессия) игнорируется."""
    runs, waiting = tmp_env
    _running_card("sid-mine")
    _marker(waiting, "sid-someone-else", db.now())

    res = svc.waiting_cards()

    assert res == []
