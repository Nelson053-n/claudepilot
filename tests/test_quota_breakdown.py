"""
Тест sessions.quota_breakdown: дашборд «кто жрёт квоту 5h-окна».

Проверяем:
  - классификацию сессий task (session_id ∈ cards.session_dir) vs interactive;
  - разделение токенов и проценты interactive_pct/task_pct;
  - флаг bloated (пик контекста > порога) только у интерактивных;
  - совет /compact указывает на самую раздутую интерактивную сессию.

PROF_PROJECTS_DIR читается на импорте — переопределяем sess.PROJECTS_DIR и
чистим кэши. «Сейчас» фиксируем близко к событиям, чтобы они попали в 5h-окно.
"""
import json

import sessions as sess


def _write_jsonl(path, rows):
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _event(ts, model, inp, out, cr=0, cc=0):
    return {
        "timestamp": ts,
        "message": {
            "model": model,
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_read_input_tokens": cr,
                "cache_creation_input_tokens": cc,
            },
        },
    }


def test_quota_breakdown_split_and_advice(tmp_path, monkeypatch):
    now_iso = "2026-06-22T12:00:00Z"
    near = "2026-06-22T11:00:00Z"  # внутри 5h-окна

    # интерактивная раздутая сессия: огромный cache_read, пик ctx > порога
    inter = tmp_path / "-home-nel-mvp-bonds"
    inter.mkdir()
    _write_jsonl(inter / "chat-session.jsonl", [
        _event(near, "claude-opus-4-8", 1000, 2000, cr=300_000),  # ctx=300K > 200K → bloated
    ])
    # задача-коворкинг: её session_id есть в cards.session_dir
    task = tmp_path / "-home-nel-trade"
    task.mkdir()
    _write_jsonl(task / "task-sid-123.jsonl", [
        _event(near, "claude-sonnet-4-6", 500, 500, cr=10_000),  # ctx=10K, малая нагрузка
    ])
    # старая сессия вне окна — не должна попасть
    old = tmp_path / "-home-nel-old"
    old.mkdir()
    _write_jsonl(old / "stale.jsonl", [
        _event("2026-06-20T00:00:00Z", "claude-opus-4-8", 9_000_000, 9_000_000),
    ])

    monkeypatch.setattr(sess, "PROJECTS_DIR", tmp_path)
    sess._SESS_CACHE.clear()
    monkeypatch.setattr(sess.time, "time", lambda: sess._parse_ts(now_iso))

    out = sess.quota_breakdown(task_session_ids={"task-sid-123"}, hours=5.0)
    t = out["totals"]

    # обе свежие сессии попали, старая — нет
    assert t["sessions"] == 2
    assert t["interactive_sessions"] == 1
    assert t["task_sessions"] == 1

    inter_tok = 1000 + 2000 + 300_000           # in+out+cr
    task_tok = 500 + 500 + 10_000
    assert t["interactive_tokens"] == inter_tok
    assert t["task_tokens"] == task_tok
    # интерактив доминирует → почти весь процент у него
    assert t["interactive_pct"] + t["task_pct"] == 100
    assert t["interactive_pct"] > t["task_pct"]

    # сессии отсортированы по токенам убыв., интерактив сверху и помечен bloated
    sessions = out["sessions"]
    assert sessions[0]["kind"] == "interactive"
    assert sessions[0]["bloated"] is True
    assert sessions[0]["peak_ctx"] == 300_000
    # задача не bloated (и не интерактив)
    task_row = next(s for s in sessions if s["kind"] == "task")
    assert task_row["bloated"] is False
    assert task_row["model"] == "sonnet"

    # совет указывает на раздутую интерактивную сессию проекта mvp-bonds
    assert out["advice"] is not None
    assert out["advice"]["session_id"] == "chat-session"
    assert out["advice"]["project"] == "mvp-bonds"
    assert "/compact" in out["advice"]["message"]


def test_quota_breakdown_no_bloat_no_advice(tmp_path, monkeypatch):
    now_iso = "2026-06-22T12:00:00Z"
    near = "2026-06-22T11:30:00Z"
    p = tmp_path / "-home-nel-x"
    p.mkdir()
    # маленькая интерактивная сессия — ниже порога раздутости
    _write_jsonl(p / "small.jsonl", [
        _event(near, "claude-sonnet-4-6", 1000, 1000, cr=5000),
    ])
    monkeypatch.setattr(sess, "PROJECTS_DIR", tmp_path)
    sess._SESS_CACHE.clear()
    monkeypatch.setattr(sess.time, "time", lambda: sess._parse_ts(now_iso))

    out = sess.quota_breakdown(task_session_ids=set(), hours=5.0)
    assert out["advice"] is None
    assert out["totals"]["interactive_pct"] == 100
    assert out["sessions"][0]["bloated"] is False
