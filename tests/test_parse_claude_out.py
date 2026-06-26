"""
Тесты services._parse_claude_json — извлечение итога из вывода claude.

Регрессия инцидента #77: задача сама дёрнула reload и оборвала свой stream-json
без финального type:result. Старый код вернул сырой NDJSON (256КБ мусора) в
result карточки. Фикс: при отсутствии type:result берём последний text-блок
ассистента, а не дамп потока.
"""
import json

import services as svc


def _assistant(text=None, tool=False):
    content = []
    if text is not None:
        content.append({"type": "text", "text": text})
    if tool:
        content.append({"type": "tool_use", "name": "Bash", "input": {}})
    return json.dumps({"type": "assistant",
                       "message": {"content": content, "usage": {}}})


def test_final_result_event_used():
    # нормальный случай: есть type:result — берём его result + cost/turns
    stream = "\n".join([
        _assistant("работаю"),
        json.dumps({"type": "result", "result": "Готово полностью",
                    "total_cost_usd": 1.5, "num_turns": 12,
                    "usage": {"input_tokens": 100, "cache_read_input_tokens": 5000}}),
    ])
    r = svc._parse_claude_json(stream)
    assert r["text"] == "Готово полностью"
    assert r["cost_usd"] == 1.5
    assert r["num_turns"] == 12
    assert r["cache_read_tokens"] == 5000


def test_truncated_stream_falls_back_to_last_text():
    # инцидент #77: НЕТ type:result (поток оборван) → последний text-блок, не дамп
    stream = "\n".join([
        _assistant("начинаю"),
        _assistant(tool=True),               # tool_use без текста
        _assistant("Сервер перезагрузился, всё готово"),
        json.dumps({"type": "user",
                    "message": {"content": [{"type": "tool_result",
                                             "content": "ok"}]}}),
    ])
    r = svc._parse_claude_json(stream)
    assert r["text"] == "Сервер перезагрузился, всё готово"
    # сырого JSON в result быть НЕ должно
    assert '"type"' not in r["text"]
    assert r["cost_usd"] is None  # финала нет → cost неизвестен


def test_truncated_no_text_keeps_raw():
    # совсем нет ни result, ни text-блоков — деградируем к raw (нечего извлечь)
    stream = _assistant(tool=True)  # только tool_use
    r = svc._parse_claude_json(stream)
    assert r["text"] == stream.strip()


def test_old_single_json_object():
    # старый --output-format json: единый объект с result/total_cost_usd
    obj = json.dumps({"result": "старый формат", "total_cost_usd": 0.3,
                      "usage": {"output_tokens": 50}})
    r = svc._parse_claude_json(obj)
    assert r["text"] == "старый формат"
    assert r["cost_usd"] == 0.3
    assert r["output_tokens"] == 50
