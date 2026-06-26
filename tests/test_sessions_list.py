"""
Тест sessions.list_sessions: обзор отдельных live-сессий Claude Code.

Проверяем:
  • подсчёт токенов/стоимости по одной записи на сессию;
  • cheap-probe фильтр active (mtime < 5мин ИЛИ свежий waiting-маркер);
  • инкрементальность: после дозаписи строки доливается дельта, а не
    пересчитывается весь файл (контролируем через мок _line_cost — счётчик
    вызовов растёт ровно на число НОВЫХ usage-строк).

PROF_PROJECTS_DIR читается на уровне импорта, поэтому переопределяем
sess.PROJECTS_DIR / WAITING_DIR напрямую и чистим кэши.
"""
import json
import os
import time

import sessions as sess


def _event(ts, model, inp, out, cr=0):
    return {
        "timestamp": ts,
        "cwd": "/home/nel/alpha",
        "gitBranch": "main",
        "message": {"model": model, "usage": {
            "input_tokens": inp, "output_tokens": out,
            "cache_read_input_tokens": cr}},
    }


def _append(path, rows):
    with open(path, "a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(sess, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(sess, "WAITING_DIR", tmp_path / "_waiting")
    (tmp_path / "_waiting").mkdir()
    sess._SESS_CACHE.clear()
    sess._BRANCH_CACHE.clear()
    # ветку не дёргаем живьём — пусть берётся из JSONL-хвоста
    monkeypatch.setattr(sess, "_git_branch", lambda cwd: "live-branch")


def test_token_counts_and_active_filter(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    # свежая сессия (mtime сейчас) — opus: 1М/1М = $90
    fresh = tmp_path / "-home-nel-alpha"
    fresh.mkdir()
    _append(fresh / "s-fresh.jsonl", [
        _event("2026-06-20T10:00:00Z", "claude-opus-4-8", 1_000_000, 1_000_000, cr=500_000)])
    # старая сессия — mtime далеко в прошлом → не active
    old = tmp_path / "-home-nel-beta"
    old.mkdir()
    op = old / "s-old.jsonl"
    _append(op, [_event("2026-01-01T00:00:00Z", "claude-sonnet-4-6", 2_000_000, 1_000_000)])
    old_t = time.time() - 3600
    os.utime(op, (old_t, old_t))

    active = sess.list_sessions(filter="active")
    assert [s["session_id"] for s in active] == ["s-fresh"]
    s = active[0]
    assert s["project"] == "alpha"
    assert s["total_input"] == 1_000_000
    assert s["total_output"] == 1_000_000
    assert s["total_cache_read"] == 500_000
    assert s["est_cost_usd"] == 90.0
    assert s["msg_count"] == 1
    assert s["git_branch"] == "live-branch"  # active → резолв живьём
    assert s["active"] is True

    # all → обе сессии, старая берёт ветку из JSONL (не active)
    every = {s["session_id"]: s for s in sess.list_sessions(filter="all")}
    assert set(every) == {"s-fresh", "s-old"}
    assert every["s-old"]["active"] is False
    assert every["s-old"]["git_branch"] == "main"
    assert every["s-old"]["est_cost_usd"] == 21.0


def test_waiting_marker_keeps_session_active(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    proj = tmp_path / "-home-nel-alpha"
    proj.mkdir()
    p = proj / "s-wait.jsonl"
    _append(p, [_event("2026-06-20T10:00:00Z", "claude-opus-4-8", 100, 100)])
    # файл «тихий» (mtime в прошлом), но агент ждёт ввода → свежий маркер
    old_t = time.time() - 3600
    os.utime(p, (old_t, old_t))
    (tmp_path / "_waiting" / "s-wait.json").write_text(json.dumps({"ts": time.time()}))

    active = sess.list_sessions(filter="active")
    assert [s["session_id"] for s in active] == ["s-wait"]


def test_incremental_only_reads_delta(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    proj = tmp_path / "-home-nel-alpha"
    proj.mkdir()
    p = proj / "s.jsonl"
    _append(p, [_event("2026-06-20T10:00:00Z", "claude-opus-4-8", 100, 100),
                _event("2026-06-20T10:01:00Z", "claude-opus-4-8", 100, 100)])

    # считаем сколько строк реально разобрано (вызовов _line_cost)
    calls = {"n": 0}
    orig = sess._line_cost
    monkeypatch.setattr(sess, "_line_cost", lambda u, m: (calls.__setitem__("n", calls["n"] + 1), orig(u, m))[1])

    s1 = sess.list_sessions(filter="active")[0]
    assert s1["msg_count"] == 2
    assert calls["n"] == 2  # первый проход — обе строки

    # дозаписываем третью строку
    _append(p, [_event("2026-06-20T10:02:00Z", "claude-opus-4-8", 100, 100)])
    s2 = sess.list_sessions(filter="active")[0]
    assert s2["msg_count"] == 3
    assert calls["n"] == 3  # доразобрана ТОЛЬКО новая строка (всего 2+1), не 2+3

    # без изменений — из кэша, 0 новых разборов
    sess.list_sessions(filter="active")
    assert calls["n"] == 3
