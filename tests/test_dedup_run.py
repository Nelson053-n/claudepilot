"""
Защита от дублей при «создать и сразу стартовать задачу несколько раз».

Два слоя:
1) claim_card_run — атомарный захват карточки под запуск. Гонка двойного
   POST /run спавнит ровно один процесс claude (победитель UPDATE ... WHERE).
2) дедуп авто-запуска в POST /api/cards (column='approved') — двойной сабмит
   с тем же title+prompt возвращает ту же карточку, новой не создаёт.
"""
import pytest

import app as prof_app
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

    spawned = []
    real_popen = svc.subprocess.Popen

    class FakeProc:
        def __init__(self, *a, **kw):
            self.pid = 4242
            spawned.append(1)

    # Мокаем ТОЛЬКО спавн claude (bash -lc ...). Прочие Popen (git rev-parse в
    # _spawn_card для head_at_start и т.п.) пропускаем к реальному — иначе они
    # ложно увеличат счётчик «спавнов процесса».
    def fake_popen(args, *a, **kw):
        if isinstance(args, (list, tuple)) and args and args[0] == "bash":
            return FakeProc(args, *a, **kw)
        return real_popen(args, *a, **kw)

    monkeypatch.setattr(svc.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(svc, "_window_util_exceeded", lambda: False)  # окно не блокирует старт
    yield spawned


def _card(column="approved"):
    board = db.ensure_board("t", "-home-nel-t")
    return db.add_card(board["id"], "title", "do it", "-home-nel-t", column=column)


def test_claim_card_run_wins_once(tmp_env):
    """Из двух захватов карточки выигрывает ровно один."""
    c = _card()
    assert db.claim_card_run(c["id"]) is True
    assert db.claim_card_run(c["id"]) is False  # уже starting → проигрыш


def test_double_run_spawns_one_process(tmp_env):
    """Два POST /api/cards/{id}/run подряд → один процесс claude."""
    spawned = tmp_env
    from fastapi.testclient import TestClient
    import os
    headers = {"Authorization": "Bearer " + os.environ["PROF_TOKEN"]}
    c = _card(column="approved")
    db.update_card(c["id"], status="idle")
    with TestClient(prof_app.app) as client:
        client.post(f"/api/cards/{c['id']}/run", headers=headers)
        client.post(f"/api/cards/{c['id']}/run", headers=headers)
    assert len(spawned) == 1


def test_auto_create_dedup(tmp_env):
    """Двойной POST /api/cards с column='approved' и тем же title+prompt → одна карточка."""
    from fastapi.testclient import TestClient
    import os
    headers = {"Authorization": "Bearer " + os.environ["PROF_TOKEN"]}
    board = db.ensure_board("t", "-home-nel-t")
    body = {"board_id": board["id"], "title": "T", "prompt": "P", "column": "approved"}
    with TestClient(prof_app.app) as client:
        r1 = client.post("/api/cards", json=body, headers=headers).json()
        r2 = client.post("/api/cards", json=body, headers=headers).json()
    assert r1["id"] == r2["id"]
    assert len(db.list_cards(board["id"])) == 1


def test_proposed_create_not_deduped(tmp_env):
    """Ручное создание в 'proposed' дубли НЕ блокирует — пользователь видит карточки."""
    from fastapi.testclient import TestClient
    import os
    headers = {"Authorization": "Bearer " + os.environ["PROF_TOKEN"]}
    board = db.ensure_board("t", "-home-nel-t")
    body = {"board_id": board["id"], "title": "T", "prompt": "P", "column": "proposed"}
    with TestClient(prof_app.app) as client:
        r1 = client.post("/api/cards", json=body, headers=headers).json()
        r2 = client.post("/api/cards", json=body, headers=headers).json()
    assert r1["id"] != r2["id"]
