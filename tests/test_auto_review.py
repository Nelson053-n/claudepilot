"""
Тесты авто-ревью после финализации задачи:
  (a) задача в review → авто-вызов agent_review, вердикт сохранён в review_verdict
  (b) при исчерпанном 5h-окне ревью откладывается (review_verdict пуст)
  (c) 'на доработку' предзаполняет answer вердиктом в /continue
  (d) ярлычок проверена/доработка по вердикту (через CARD_FIELDS и поле в БД)
"""
import itertools
import threading
import time

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
    yield tmp_path, runs


_seq = itertools.count()


def _review_card(runs):
    """Карточка в колонке review, status=done."""
    board = db.ensure_board("T", f"slug-{next(_seq)}")
    card = db.add_card(board["id"], "task", "do it", board["slug"], column="review")
    db.update_card(card["id"], status="done", result="агент сделал", finished_at=db.now())
    return db.get_card(card["id"])


def _running_card(runs):
    """Карточка status=running (имитирует выполнение)."""
    board = db.ensure_board("T2", f"slug2-{next(_seq)}")
    card = db.add_card(board["id"], "task", f"work {next(_seq)}", board["slug"], column="in_progress")
    db.update_card(card["id"], status="running", pid=None, started_at=db.now())
    return db.get_card(card["id"])


# ---- (a) авто-ревью сохраняет вердикт ----

def test_auto_review_saves_done_verdict(tmp_env, monkeypatch):
    """_auto_review_if_needed сохраняет DONE-вердикт в review_verdict."""
    _, runs = tmp_env
    card = _review_card(runs)

    monkeypatch.setattr(svc, "_window_util_exceeded", lambda: False)
    monkeypatch.setattr(svc, "agent_review",
                        lambda c: {"verdict": "done", "text": "всё хорошо", "cost_usd": 0.01})

    svc._auto_review_if_needed(card)

    updated = db.get_card(card["id"])
    assert updated["review_verdict"] is not None
    assert updated["review_verdict"].startswith("DONE:")
    assert "всё хорошо" in updated["review_verdict"]
    assert updated["review_checked_at"] is not None
    assert updated["review_cost_usd"] == pytest.approx(0.01)


def test_auto_review_saves_rework_verdict(tmp_env, monkeypatch):
    """_auto_review_if_needed сохраняет REWORK-вердикт."""
    _, runs = tmp_env
    card = _review_card(runs)

    monkeypatch.setattr(svc, "_window_util_exceeded", lambda: False)
    monkeypatch.setattr(svc, "agent_review",
                        lambda c: {"verdict": "rework", "text": "не хватает тестов", "cost_usd": 0.02})

    svc._auto_review_if_needed(card)

    updated = db.get_card(card["id"])
    assert updated["review_verdict"].startswith("REWORK:")
    assert "не хватает тестов" in updated["review_verdict"]


# ---- (b) при исчерпанном 5h-окне ревью откладывается ----

def test_auto_review_skipped_when_window_exceeded(tmp_env, monkeypatch):
    """Если 5h-окно исчерпано — ревью не запускается, review_verdict остаётся пустым."""
    _, runs = tmp_env
    card = _review_card(runs)

    review_called = []
    monkeypatch.setattr(svc, "_window_util_exceeded", lambda: True)
    monkeypatch.setattr(svc, "agent_review", lambda c: review_called.append(1) or {})

    svc._auto_review_if_needed(card)

    assert len(review_called) == 0
    updated = db.get_card(card["id"])
    assert updated["review_verdict"] is None


# ---- идемпотентность: уже проверена → не перепроверяем ----

def test_auto_review_skips_already_reviewed(tmp_env, monkeypatch):
    """Если review_verdict уже выставлен — повторный вызов не трогает его."""
    _, runs = tmp_env
    card = _review_card(runs)
    db.update_card(card["id"], review_verdict="DONE: уже проверено")
    card = db.get_card(card["id"])

    review_called = []
    monkeypatch.setattr(svc, "_window_util_exceeded", lambda: False)
    monkeypatch.setattr(svc, "agent_review", lambda c: review_called.append(1) or {})

    svc._auto_review_if_needed(card)

    assert len(review_called) == 0
    assert db.get_card(card["id"])["review_verdict"] == "DONE: уже проверено"


# ---- auto_review disabled ----

def test_auto_review_disabled_by_setting(tmp_env, monkeypatch):
    """settings auto_review=0 полностью отключает авто-ревью."""
    _, runs = tmp_env
    db.set_setting("auto_review", "0")
    card = _review_card(runs)

    review_called = []
    monkeypatch.setattr(svc, "agent_review", lambda c: review_called.append(1) or {})

    svc._auto_review_if_needed(card)

    assert len(review_called) == 0


# ---- (c) /continue принимает answer с вердиктом ----

def test_continue_with_verdict_answer(tmp_env, monkeypatch):
    """start_card_continue прокидывает answer (вердикт) в промпт агента."""
    _, runs = tmp_env
    board = db.ensure_board("B", f"slug3-{next(_seq)}")
    card = db.add_card(board["id"], "task", "make it", board["slug"], column="review")
    db.update_card(card["id"], status="done", result="готово",
                   review_verdict="REWORK: не хватает тестов для edge-case")
    card = db.get_card(card["id"])

    spawned = []

    def fake_spawn(c):
        spawned.append(c["prompt"])

    monkeypatch.setattr(svc, "_window_util_exceeded", lambda: False)
    monkeypatch.setattr(svc, "count_running", lambda: 0)
    monkeypatch.setattr(svc, "project_busy", lambda slug, cid: False)
    monkeypatch.setattr(svc, "_spawn_card", fake_spawn)
    monkeypatch.setattr(db, "claim_card_run", lambda cid: True)

    verdict_comment = "не хватает тестов для edge-case"
    svc.start_card_continue(card, answer=verdict_comment)

    assert len(spawned) == 1
    assert verdict_comment in spawned[0]


# ---- (d) поля review_verdict / review_checked_at доступны в CARD_FIELDS ----

def test_review_fields_in_card_fields():
    """review_verdict и review_checked_at присутствуют в CARD_FIELDS."""
    assert "review_verdict" in db.CARD_FIELDS
    assert "review_checked_at" in db.CARD_FIELDS


def test_review_fields_persisted_in_db(tmp_env):
    """review_verdict и review_checked_at реально сохраняются и читаются из БД."""
    _, _ = tmp_env
    board = db.ensure_board("C", f"slug4-{next(_seq)}")
    card = db.add_card(board["id"], "t", "p", board["slug"])
    ts = db.now()
    db.update_card(card["id"], review_verdict="DONE: проверено", review_checked_at=ts)
    updated = db.get_card(card["id"])
    assert updated["review_verdict"] == "DONE: проверено"
    assert abs(updated["review_checked_at"] - ts) < 1.0


# ---- (e) детерминированный override REWORK→DONE для read-only «нет изменений» ----
# (закрывает зацикливание ручной «доработки» read-only карты, как #103)

def test_formal_no_changes_rework_overridden():
    """REWORK сугубо про «нет изменений/коммитов» → форсим DONE (read-only норма)."""
    assert svc._rework_is_only_no_changes(
        "REWORK: задача read-only, файлы не изменялись, коммитов нет. Ответ дан.")


def test_content_rework_not_overridden():
    """REWORK по существу (неверно/ошибка) НЕ переопределяется, даже если есть 'коммитов нет'."""
    assert not svc._rework_is_only_no_changes(
        "REWORK: ответ неверный, посчитано неправильно, коммитов нет.")


def test_no_changes_marker_absent():
    """Нет маркера про изменения вовсе → не наш случай (не форсим)."""
    assert not svc._rework_is_only_no_changes("REWORK: тема не раскрыта.")
