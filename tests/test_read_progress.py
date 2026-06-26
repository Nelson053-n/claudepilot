"""
Тесты services.read_progress — инкрементального дельта-чтения NDJSON-прогресса
(card_<id>.out в формате --output-format stream-json --verbose).

Проверяем: два вызова с разным since_offset не дублируют события (append-only
дельта), извлечение last_text из assistant и running_cost/токенов из result,
а также substring fast-reject строк без "type".
"""
import json

import pytest

import services as svc


@pytest.fixture
def runs(tmp_path, monkeypatch):
    d = tmp_path / "runs"
    d.mkdir()
    monkeypatch.setattr(svc, "RUNS", d)
    return d


def _line(obj):
    return json.dumps(obj, ensure_ascii=False) + "\n"


def _assistant(text, **usage):
    return _line({"type": "assistant",
                  "message": {"role": "assistant",
                              "content": [{"type": "text", "text": text}],
                              "usage": usage}})


def _result(text, cost, **usage):
    return _line({"type": "result", "subtype": "success",
                  "result": text, "total_cost_usd": cost,
                  "duration_ms": 3400, "num_turns": 1, "usage": usage})


def test_incremental_no_duplicate_events(runs):
    cid = 7
    out = runs / f"card_{cid}.out"
    out.write_text(
        _line({"type": "system", "subtype": "init"})
        + _assistant("работаю над задачей", input_tokens=100, output_tokens=5)
    )

    p1 = svc.read_progress(cid, 0)
    assert len(p1["events"]) == 2  # system + assistant
    assert p1["last_text"] == "работаю над задачей"
    assert p1["offset"] == out.stat().st_size

    # допись новых строк (append-only)
    with out.open("a") as f:
        f.write(_assistant("почти готово", input_tokens=120, output_tokens=42))
        f.write(_result("готово!", 0.0153, input_tokens=120,
                        output_tokens=42, cache_read_input_tokens=33000,
                        cache_creation_input_tokens=3800))

    # второй вызов с offset из первого — только новые события, без дублей
    p2 = svc.read_progress(cid, p1["offset"])
    assert len(p2["events"]) == 2  # второй assistant + result, system НЕ повторился
    types = [e["type"] for e in p2["events"]]
    assert types == ["assistant", "result"]
    assert p2["last_text"] == "готово!"          # result.result перекрыл assistant
    assert p2["running_cost"] == 0.0153
    assert p2["input_tokens"] == 120
    assert p2["output_tokens"] == 42
    assert p2["offset"] == out.stat().st_size

    # повторный вызов с конца — пусто, offset не двигается
    p3 = svc.read_progress(cid, p2["offset"])
    assert p3["events"] == []
    assert p3["offset"] == p2["offset"]


def test_partial_trailing_line_held(runs):
    """Недописанная последняя строка (без \\n) не парсится и переносится на потом:
    offset останавливается на её начале, следующий тик дочитывает её целиком."""
    cid = 9
    out = runs / f"card_{cid}.out"
    full = _assistant("первое сообщение", output_tokens=3)
    partial = '{"type":"assistant","message":{"content":[{"type":"text","text":"ещё пишет'
    out.write_text(full + partial)  # без завершающего \n

    p1 = svc.read_progress(cid, 0)
    assert len(p1["events"]) == 1
    assert p1["last_text"] == "первое сообщение"
    assert p1["offset"] == len(full.encode())  # хвост НЕ потреблён

    # дописали хвост строки
    with out.open("a") as f:
        f.write('"}],"usage":{"output_tokens":9}}}\n')

    p2 = svc.read_progress(cid, p1["offset"])
    assert len(p2["events"]) == 1
    assert p2["last_text"] == "ещё пишет"
    assert p2["output_tokens"] == 9


def test_fast_reject_non_type_lines(runs):
    """Строки без "type" (мусор/частичные) пропускаются без падения."""
    cid = 3
    out = runs / f"card_{cid}.out"
    out.write_text(
        "просто текст без json\n"
        + '{"foo":"bar"}\n'             # валидный JSON, но без "type" → reject
        + _assistant("ок", output_tokens=1)
    )
    p = svc.read_progress(cid, 0)
    assert len(p["events"]) == 1
    assert p["events"][0]["type"] == "assistant"
    assert p["last_text"] == "ок"


def test_missing_file(runs):
    p = svc.read_progress(404, 0)
    assert p["events"] == []
    assert p["offset"] == 0
    assert p["running_cost"] is None
