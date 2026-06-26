"""
Тесты services.refresh_running_cards через временную БД и фейковые runs/*.rc.

refresh_running_cards финализирует карточки в статусе 'running', определяя исход
по файлам runs/card_<id>.rc (код возврата) и runs/card_<id>.out (вывод):
  rc == 0                       → done / review
  rc is None и пустой вывод     → interrupted / approved (убит на старте)
  есть вывод, но rc≠0/нет rc    → failed / in_progress
"""
import importlib
import os

import pytest

import db
import services as svc


def _age_out(runs, cid, secs):
    """Состарить mtime card_<cid>.out на secs секунд назад (имитация молчащего
    процесса: reaper финализирует orphan только если .out молчит > _OUT_STALE)."""
    f = runs / f"card_{cid}.out"
    t = db.now() - secs
    os.utime(f, (t, t))


@pytest.fixture
def tmp_env(tmp_path, monkeypatch):
    """Изолированная БД и runs/ во временной папке.

    db.DB_PATH читается внутри _conn() при каждом вызове, а svc.RUNS — внутри
    refresh_running_cards, поэтому monkeypatch.setattr достаточно (без reimport).
    """
    test_db = tmp_path / "test.db"
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(db, "DB_PATH", test_db)
    monkeypatch.setattr(svc, "RUNS", runs)
    db.init_db()
    yield runs


def _running_card(runs, *, prompt="do it"):
    """Создаёт карточку и переводит в running с pid=None (без живого процесса)."""
    board = db.ensure_board("T", "-home-nel-t")
    card = db.add_card(board["id"], "title", prompt, "-home-nel-t", column="in_progress")
    db.update_card(card["id"], status="running", pid=None)
    return card["id"]


def test_done_on_rc_zero(tmp_env):
    runs = tmp_env
    cid = _running_card(runs)
    (runs / f"card_{cid}.rc").write_text("0\n")
    (runs / f"card_{cid}.out").write_text('{"result":"всё готово","total_cost_usd":0.01}')

    svc.refresh_running_cards()

    card = db.get_card(cid)
    assert card["status"] == "done"
    assert card["column"] == "review"
    assert card["return_code"] == 0
    assert card["result"] == "всё готово"
    assert card["cost_usd"] == 0.01


def test_failed_on_nonzero_rc_with_output(tmp_env):
    runs = tmp_env
    cid = _running_card(runs)
    (runs / f"card_{cid}.rc").write_text("1\n")
    (runs / f"card_{cid}.out").write_text("Traceback: что-то упало")

    svc.refresh_running_cards()

    card = db.get_card(cid)
    assert card["status"] == "failed"
    assert card["column"] == "in_progress"
    assert card["return_code"] == 1
    assert "упало" in card["result"]


def test_interrupted_when_no_rc_and_empty_output(tmp_env):
    runs = tmp_env
    cid = _running_card(runs)
    # нет .rc, pid=None, started_at за пределами grace, вывод пустой и .out молчит
    # дольше _OUT_STALE → процесс реально оборван.
    (runs / f"card_{cid}.out").write_text("")
    db.update_card(cid, started_at=db.now() - 10000)
    _age_out(runs, cid, svc._OUT_STALE + 60)

    svc.refresh_running_cards()

    card = db.get_card(cid)
    assert card["status"] == "interrupted"
    assert card["column"] == "approved"
    assert card["return_code"] is None
    assert "прервано" in card["result"].lower()


def test_orphaned_after_grace_no_rc(tmp_env):
    # pid мёртв, .rc нет, started_at старше grace → задача реально оборвана
    # (рестарт/краш) → interrupted, возвращается на доработку. БЕЗ grace остаётся
    # running (чтобы пережившая рестарт задача успела дописать .rc).
    runs = tmp_env
    cid = _running_card(runs)
    (runs / f"card_{cid}.out").write_text("частичный вывод без кода возврата")
    # отматываем started_at в прошлое за пределы grace-периода + .out молчит
    db.update_card(cid, started_at=db.now() - 10000)
    _age_out(runs, cid, svc._OUT_STALE + 60)

    svc.refresh_running_cards()

    card = db.get_card(cid)
    assert card["status"] == "interrupted"
    assert card["column"] == "approved"


def test_stays_running_within_grace_no_rc(tmp_env):
    # свежая задача (started_at недавно), pid мёртв, .rc ещё нет → НЕ хороним,
    # оставляем running: claude мог пережить рестарт и вот-вот допишет .rc.
    runs = tmp_env
    cid = _running_card(runs)
    db.update_card(cid, started_at=db.now())  # только что стартовала
    (runs / f"card_{cid}.out").write_text("вывод ещё пишется")

    svc.refresh_running_cards()

    assert db.get_card(cid)["status"] == "running"


def test_stays_running_when_out_fresh_despite_old_start(tmp_env):
    # КОРЕНЬ «дёрганья»: started_at старый (за пределами grace), .rc нет, pid
    # «мёртв» — НО .out писался только что (процесс жив, claude пишет). Раньше
    # такую ЖИВУЮ задачу reaper ошибочно метил interrupted. Теперь .out свежий →
    # out_silent=False → остаётся running.
    runs = tmp_env
    cid = _running_card(runs)
    db.update_card(cid, started_at=db.now() - 10000)  # давно стартовала
    (runs / f"card_{cid}.out").write_text("процесс активно пишет прямо сейчас")
    # .out свежий (только что записан) — НЕ состариваем

    svc.refresh_running_cards()

    assert db.get_card(cid)["status"] == "running"


def test_unfinished_card_untouched(tmp_env):
    """Карточка с живым дочерним процессом и без .rc остаётся running."""
    import subprocess

    runs = tmp_env
    board = db.ensure_board("T", "-home-nel-t")
    card = db.add_card(board["id"], "live", "p", "-home-nel-t", column="in_progress")
    # настоящий дочерний процесс: waitpid(pid, WNOHANG) → (0,0) (не завершён),
    # os.kill(pid,0) не упадёт → pid_dead=False → карточка не финализируется.
    # (os.getpid() не годится: для waitpid он не наш ребёнок → ChildProcessError.)
    proc = subprocess.Popen(["sleep", "30"])
    try:
        db.update_card(card["id"], status="running", pid=proc.pid)
        svc.refresh_running_cards()
        assert db.get_card(card["id"])["status"] == "running"
    finally:
        proc.kill()
        proc.wait()


def test_non_running_cards_skipped(tmp_env):
    """Карточки не в статусе running игнорируются даже при наличии .rc."""
    runs = tmp_env
    board = db.ensure_board("T", "-home-nel-t")
    card = db.add_card(board["id"], "idle one", "p", "-home-nel-t")
    (runs / f"card_{card['id']}.rc").write_text("0\n")

    svc.refresh_running_cards()

    # статус остался idle (по умолчанию), не стал done
    assert db.get_card(card["id"])["status"] == "idle"
