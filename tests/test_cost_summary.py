"""
Тест sessions.cost_summary: 2 jsonl с известными usage и разными моделями.
Проверяем тарифную арифметику (_line_cost = вход*in + выход*out / 1M),
группировку by_project и by_day.

PROF_PROJECTS_DIR читается в sessions.py на уровне импорта, поэтому в тесте
переопределяем sess.PROJECTS_DIR напрямую и чистим файловый кэш.
"""
import json

import sessions as sess


def _write_jsonl(path, rows):
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _event(ts, model, inp, out, cr=0):
    return {
        "timestamp": ts,
        "message": {
            "model": model,
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_read_input_tokens": cr,
            },
        },
    }


def test_cost_summary_pricing_and_grouping(tmp_path, monkeypatch):
    # проект A (opus): 1М вход + 1М выход = $15 + $75 = $90
    proj_a = tmp_path / "-home-nel-alpha"
    proj_a.mkdir()
    _write_jsonl(proj_a / "s1.jsonl", [
        _event("2026-06-20T10:00:00Z", "claude-opus-4-8", 1_000_000, 1_000_000, cr=500_000),
        _event("2026-06-21T12:00:00Z", "claude-opus-4-8", 0, 0),  # пустое усилие
    ])
    # проект B (sonnet): 2М вход + 1М выход = $6 + $15 = $21
    # + haiku: 1М вход + 1М выход = $0.8 + $4 = $4.8
    proj_b = tmp_path / "-home-nel-beta"
    proj_b.mkdir()
    _write_jsonl(proj_b / "s2.jsonl", [
        _event("2026-06-20T08:00:00Z", "claude-sonnet-4-6", 2_000_000, 1_000_000),
        _event("2026-06-20T09:00:00Z", "claude-haiku-4-5", 1_000_000, 1_000_000),
    ])

    monkeypatch.setattr(sess, "PROJECTS_DIR", tmp_path)
    sess._FILE_CACHE.clear()

    # фиксируем «сейчас» далеко после событий, окно days большое чтобы всё попало
    monkeypatch.setattr(sess.time, "time", lambda: sess._parse_ts("2026-06-22T00:00:00Z"))
    out = sess.cost_summary(days=7)

    # --- by_project: arithmetic + сортировка по cost desc ---
    by_proj = {p["project"]: p for p in out["by_project"]}
    assert set(by_proj) == {"alpha", "beta"}
    assert by_proj["alpha"]["cost"] == 90.0
    assert by_proj["beta"]["cost"] == 25.8  # sonnet $21 + haiku $4.8
    assert by_proj["alpha"]["cache_read"] == 500_000
    assert by_proj["alpha"]["input"] == 1_000_000
    assert by_proj["alpha"]["output"] == 1_000_000
    assert by_proj["alpha"]["messages"] == 2  # пустое событие тоже считается сообщением
    # отсортировано по убыванию стоимости
    assert [p["project"] for p in out["by_project"]] == ["alpha", "beta"]

    # --- by_day: разбивка cost/input/output/messages по дню ---
    d20, d21 = out["by_day"]["2026-06-20"], out["by_day"]["2026-06-21"]
    # alpha+beta за 20-е = 90 + 21 + 4.8(haiku) = 115.8
    assert d20["cost"] == 115.8
    assert d20["input"] == 1_000_000 + 2_000_000 + 1_000_000   # alpha + sonnet + haiku
    assert d20["output"] == 1_000_000 + 1_000_000 + 1_000_000  # alpha + sonnet + haiku
    assert d20["messages"] == 3                     # alpha + sonnet + haiku
    # alpha за 21-е — пустое событие: $0, без токенов, но считается сообщением
    assert d21["cost"] == 0.0
    assert d21["input"] == 0 and d21["output"] == 0
    assert d21["messages"] == 1
    # отсортировано по дате
    assert list(out["by_day"]) == sorted(out["by_day"])

    # --- period total ---
    assert out["period"]["cost_usd"] == 115.8
    assert out["period"]["input"] == 4_000_000
    assert out["period"]["output"] == 3_000_000
    assert out["period_days"] == 7

    # --- by_model: агрегация по тарифным классам opus/sonnet/haiku ---
    bm = out["by_model"]
    assert set(bm) == {"opus", "sonnet", "haiku"}
    # opus: alpha (1М/1М) + пустое событие = $90, 2 сообщения
    assert bm["opus"]["cost"] == 90.0
    assert bm["opus"]["input"] == 1_000_000
    assert bm["opus"]["output"] == 1_000_000
    assert bm["opus"]["cache_read"] == 500_000
    assert bm["opus"]["messages"] == 2
    # sonnet: 2М/1М = $21
    assert bm["sonnet"]["cost"] == 21.0
    assert bm["sonnet"]["messages"] == 1
    # haiku: 1М/1М = $4.8
    assert bm["haiku"]["cost"] == 4.8
    assert bm["haiku"]["messages"] == 1
    # сумма по моделям = period total
    assert round(sum(m["cost"] for m in bm.values()), 2) == out["period"]["cost_usd"]
