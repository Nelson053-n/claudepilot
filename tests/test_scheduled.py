"""
Тесты отложенного старта (status=scheduled, scheduled_at).

start_scheduled (вызывается reaper'ом) запускает scheduled-карточку, только когда
наступило scheduled_at И есть свободный слот под WIP-лимитом. До срока — не трогает.
unschedule снимает отложку (idle, scheduled_at=NULL).

Popen замокан — реальный claude не вызывается.
"""
import itertools

import pytest

import db
import services as svc


@pytest.fixture
def tmp_env(tmp_path, monkeypatch):
    test_db = tmp_path / "test.db"
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(db, "DB_PATH", test_db)
    monkeypatch.setattr(svc, "RUNS", runs)
    db.init_db()

    class FakeProc:
        def __init__(self, *a, **kw):
            self.pid = 424242

    monkeypatch.setattr(svc.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(svc, "_window_util_exceeded", lambda: False)  # окно не блокирует старт
    # выбор модели → дефолт без вызова Claude (гибрид-роутер иначе зовёт Haiku на
    # «серых» задачах — реальный claude в тестах не нужен).
    monkeypatch.setattr(svc, "_model_for_card", lambda card: svc._MODELS["task"])
    yield runs


_seq = itertools.count()


def _card(slug="-home-nel-t"):
    # уникальный prompt: add_card схлопывает дубли title+prompt в окне 10с.
    board = db.ensure_board(slug.rsplit("-", 1)[-1], slug)
    return db.add_card(board["id"], "title", f"do it {next(_seq)}", slug, column="approved")


def test_future_scheduled_does_not_start(tmp_env):
    c = _card()
    db.update_card(c["id"], status="scheduled", column="approved",
                   scheduled_at=db.now() + 3600)  # через час

    svc.start_scheduled()

    got = db.get_card(c["id"])
    assert got["status"] == "scheduled"  # время не наступило — не запускаем
    assert svc.count_running() == 0


def test_due_scheduled_starts_when_slot_free(tmp_env):
    db.set_setting("wip_limit", "3")
    c = _card()
    db.update_card(c["id"], status="scheduled", column="approved",
                   scheduled_at=db.now() - 1)  # уже наступило

    n = svc.start_scheduled()

    got = db.get_card(c["id"])
    assert n == 1
    assert got["status"] == "running"
    assert got["scheduled_at"] is None  # отложка снята при старте
    assert svc.count_running() == 1


def test_due_scheduled_waits_when_no_slot(tmp_env):
    """Срок наступил, но WIP-лимит исчерпан → карточка остаётся scheduled (не
    стартует, не теряет scheduled_at). Следующий тик reaper'а повторит, когда
    освободится слот."""
    db.set_setting("wip_limit", "1")
    busy = _card()
    svc.start_card(busy)  # занимает единственный слот → running
    assert svc.count_running() == 1

    c = _card()
    sched_at = db.now() - 1
    db.update_card(c["id"], status="scheduled", column="approved",
                   scheduled_at=sched_at)

    n = svc.start_scheduled()

    got = db.get_card(c["id"])
    assert n == 0
    assert got["status"] == "scheduled"  # ждёт слот, не стартует
    assert got["scheduled_at"] == sched_at  # отложка не снята


def test_unschedule_clears(tmp_env):
    c = _card()
    db.update_card(c["id"], status="scheduled", column="approved",
                   scheduled_at=db.now() + 3600)

    db.update_card(c["id"], status="idle", scheduled_at=None)  # как делает эндпоинт

    got = db.get_card(c["id"])
    assert got["status"] == "idle"
    assert got["scheduled_at"] is None
    svc.start_scheduled()
    assert db.get_card(c["id"])["status"] == "idle"  # снятая — не стартует


def test_scheduled_continue_uses_continue(tmp_env, monkeypatch):
    """sched_continue=1 → start_scheduled зовёт start_card_continue (доделать),
    БЕЗ флага → start_card (с нуля). Мокаем обе, фиксируем какая вызвана."""
    called = []
    monkeypatch.setattr(svc, "start_card", lambda c: called.append(("fresh", c["id"])))
    monkeypatch.setattr(svc, "start_card_continue",
                        lambda c: called.append(("cont", c["id"])))

    c1 = _card()
    db.update_card(c1["id"], status="scheduled", scheduled_at=db.now() - 1,
                   sched_continue=1)
    c2 = _card()
    db.update_card(c2["id"], status="scheduled", scheduled_at=db.now() - 1)

    svc.start_scheduled()

    assert ("cont", c1["id"]) in called   # с флагом → continue
    assert ("fresh", c2["id"]) in called  # без флага → с нуля
    # флаг снят после старта (не продолжать повторно)
    assert db.get_card(c1["id"])["sched_continue"] in (None, 0)


def test_next_period_start_from_resets_at(tmp_env, monkeypatch):
    """next_period_start = resets_at + 5мин из usage; None если usage недоступен."""
    from datetime import datetime, timezone, timedelta
    reset = datetime.now(timezone.utc) + timedelta(hours=2)
    monkeypatch.setattr(svc, "get_usage",
                        lambda: {"five_hour": {"resets_at": reset.isoformat()}})
    ts = svc.next_period_start()
    assert ts == pytest.approx(reset.timestamp() + 5 * 60, abs=1)

    monkeypatch.setattr(svc, "get_usage", lambda: {"available": False})
    assert svc.next_period_start() is None
