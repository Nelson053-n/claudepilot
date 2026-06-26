"""
Тесты авто-паузы раздувшихся задач (инцидент #74: 159 turns / 18M cache_read
в одном прогоне выжгли 5h-окно и уронили соседей в session-limit).

Логика: если бегущая задача вышла за порог раздувания (turns/cache_read из её
.out) И 5h-окно почти исчерпано → pause_card (мягко глушит, status='paused',
сохраняет контекст). Когда окно освободилось → resume_paused доделывает через
--resume (start_card_continue). Здоровые задачи и свободное окно не трогаем.
"""
import itertools
import json

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
    yield runs


_seq = itertools.count()


def _card(slug="-home-nel-t", title="t", prompt=None):
    if prompt is None:
        prompt = f"do {next(_seq)}"
    board = db.ensure_board("T", slug)
    return db.add_card(board["id"], title, prompt, slug, column="approved")


def _write_out(runs, cid, turns, cache_read_each=100_000):
    """Пишет card_<cid>.out с `turns` assistant-сообщений (каждое с usage)."""
    with (runs / f"card_{cid}.out").open("w") as f:
        for _ in range(turns):
            f.write(json.dumps({
                "type": "assistant",
                "message": {"usage": {"cache_read_input_tokens": cache_read_each}},
            }) + "\n")


# ---- детектор раздувания --------------------------------------------------

def test_live_load_counts_turns_and_cache(tmp_env):
    _write_out(tmp_env, 1, turns=10, cache_read_each=50_000)
    load = svc._live_task_load(1)
    assert load["turns"] == 10
    assert load["cache_read"] == 500_000


def test_bloated_by_turns(tmp_env):
    db.set_setting("pause_turns", "80")
    db.set_setting("pause_cache_read_m", "0")  # только turns-критерий
    _write_out(tmp_env, 1, turns=85, cache_read_each=1)
    b = svc._task_bloated(1)
    assert b and "turns" in b["reason"]


def test_bloated_by_cache_read(tmp_env):
    db.set_setting("pause_turns", "0")
    db.set_setting("pause_cache_read_m", "8")  # 8M
    _write_out(tmp_env, 1, turns=20, cache_read_each=500_000)  # 10M > 8M
    b = svc._task_bloated(1)
    assert b and "cache_read" in b["reason"]


def test_not_bloated_under_thresholds(tmp_env):
    db.set_setting("pause_turns", "80")
    db.set_setting("pause_cache_read_m", "8")
    _write_out(tmp_env, 1, turns=29, cache_read_each=100_000)  # 29 turns / 2.9M
    assert svc._task_bloated(1) is None


def test_thresholds_zero_disables(tmp_env):
    db.set_setting("pause_turns", "0")
    db.set_setting("pause_cache_read_m", "0")
    _write_out(tmp_env, 1, turns=999, cache_read_each=999_999)
    assert svc._task_bloated(1) is None  # оба порога выкл → никогда не раздута


# ---- пауза в реапере ------------------------------------------------------

def test_reaper_pauses_bloated_when_window_full(tmp_env, monkeypatch):
    db.set_setting("pause_turns", "80")
    db.set_setting("window_util_limit", "85")
    # окно почти исчерпано
    monkeypatch.setattr(svc, "get_usage", lambda: {"five_hour": {"util": 95}})
    paused = []
    monkeypatch.setattr(svc, "pause_card", lambda c, r: paused.append((c["id"], r)))

    c = _card()
    db.update_card(c["id"], status="running", pid=1)
    _write_out(tmp_env, c["id"], turns=120)  # раздулась, .rc нет → ещё бежит

    svc.refresh_running_cards()
    assert paused and paused[0][0] == c["id"]


def test_reaper_keeps_bloated_when_window_free(tmp_env, monkeypatch):
    db.set_setting("pause_turns", "80")
    db.set_setting("window_util_limit", "85")
    # окно свободно → даже раздутую задачу НЕ трогаем (она доработает)
    monkeypatch.setattr(svc, "get_usage", lambda: {"five_hour": {"util": 40}})
    paused = []
    monkeypatch.setattr(svc, "pause_card", lambda c, r: paused.append(c["id"]))

    c = _card()
    db.update_card(c["id"], status="running", pid=1)
    _write_out(tmp_env, c["id"], turns=120)

    svc.refresh_running_cards()
    assert not paused


def test_reaper_skips_finished_card(tmp_env, monkeypatch):
    # есть .rc → задача завершена, пауза неприменима (финализируется как обычно)
    db.set_setting("pause_turns", "80")
    db.set_setting("window_util_limit", "85")
    monkeypatch.setattr(svc, "get_usage", lambda: {"five_hour": {"util": 95}})
    paused = []
    monkeypatch.setattr(svc, "pause_card", lambda c, r: paused.append(c["id"]))

    c = _card()
    db.update_card(c["id"], status="running", pid=1)
    _write_out(tmp_env, c["id"], turns=120)
    (tmp_env / f"card_{c['id']}.rc").write_text("0\n")  # завершена

    svc.refresh_running_cards()
    assert not paused  # .rc есть → пауза пропущена


# ---- авто-resume ----------------------------------------------------------

def test_resume_paused_when_window_free(tmp_env, monkeypatch):
    db.set_setting("window_util_limit", "85")
    monkeypatch.setattr(svc, "get_usage", lambda: {"five_hour": {"util": 30}})
    resumed = []
    monkeypatch.setattr(svc, "start_card_continue",
                        lambda c, **k: resumed.append(c["id"]))

    c = _card()
    db.update_card(c["id"], status="paused", result="частичный отчёт")

    n = svc.resume_paused()
    assert n == 1 and resumed == [c["id"]]


def test_resume_paused_waits_while_window_full(tmp_env, monkeypatch):
    db.set_setting("window_util_limit", "85")
    monkeypatch.setattr(svc, "get_usage", lambda: {"five_hour": {"util": 95}})
    resumed = []
    monkeypatch.setattr(svc, "start_card_continue",
                        lambda c, **k: resumed.append(c["id"]))

    c = _card()
    db.update_card(c["id"], status="paused", result="x")

    assert svc.resume_paused() == 0  # окно ещё забито → не будим
    assert not resumed


def test_pause_card_sets_status_and_clears_files(tmp_env):
    # интеграционный: pause_card без живого pid (pid=None) корректно метит paused,
    # сохраняет result и чистит .out/.rc
    c = _card()
    db.update_card(c["id"], status="running", pid=None)
    _write_out(tmp_env, c["id"], turns=5)
    (tmp_env / f"card_{c['id']}.rc").write_text("0\n")

    svc.pause_card(db.get_card(c["id"]), "120 turns (порог 80)")

    fresh = db.get_card(c["id"])
    assert fresh["status"] == "paused"
    assert "пауз" in fresh["result"].lower()
    assert not (tmp_env / f"card_{c['id']}.out").exists()
    assert not (tmp_env / f"card_{c['id']}.rc").exists()
