"""
Тесты чистых парсеров app.py (без сети/claude/БД):
- _parse_fm — frontmatter с кавычками / без блока / с пустыми значениями
- parse_suggestions — JSON-массив из ответа агента (валидный/невалидный/markdown)
"""
import app as prof_app


# ---------------- _parse_fm ----------------
def test_parse_fm_basic():
    txt = "---\nname: foo\ndescription: hello\n---\n\nтело документа"
    meta, body = prof_app._parse_fm(txt)
    assert meta == {"name": "foo", "description": "hello"}
    assert body == "тело документа"


def test_parse_fm_strips_quotes():
    # двойные и одинарные кавычки вокруг значения снимаются
    txt = '---\nname: "quoted"\ntitle: \'single\'\n---\nbody'
    meta, _ = prof_app._parse_fm(txt)
    assert meta["name"] == "quoted"
    assert meta["title"] == "single"


def test_parse_fm_no_block():
    # нет frontmatter-блока → meta пустой, тело = весь текст (обрезанный)
    txt = "просто текст без блока\nвторая строка"
    meta, body = prof_app._parse_fm(txt)
    assert meta == {}
    assert body == "просто текст без блока\nвторая строка"


def test_parse_fm_empty_values_skipped():
    # ключи с пустым значением игнорируются (m.group(2).strip() пустой)
    txt = "---\nname: kept\nempty:\nblank:   \n---\nbody"
    meta, _ = prof_app._parse_fm(txt)
    assert meta == {"name": "kept"}
    assert "empty" not in meta
    assert "blank" not in meta


def test_parse_fm_unterminated_block():
    # открыли ---, но не закрыли → блок не распознан, всё уходит в тело
    txt = "---\nname: foo\nбез закрывающего разделителя"
    meta, body = prof_app._parse_fm(txt)
    assert meta == {}
    assert body.startswith("---")


# ---------------- parse_suggestions ----------------
def test_parse_suggestions_valid():
    text = '[{"title":"A","prompt":"do a"},{"title":"B","prompt":"do b"}]'
    out = prof_app.parse_suggestions(text)
    assert len(out) == 2
    assert out[0]["title"] == "A"


def test_parse_suggestions_markdown_wrapped():
    # агент обернул JSON в ```json … ``` и добавил пояснения вокруг
    text = (
        "Вот результат анализа:\n```json\n"
        '[{"title":"Тест","prompt":"написать тест"}]\n'
        "```\nГотово."
    )
    out = prof_app.parse_suggestions(text)
    assert out == [{"title": "Тест", "prompt": "написать тест"}]


def test_parse_suggestions_invalid_json():
    # есть скобки, но внутри не JSON → пустой список, без исключения
    text = "[это не json, просто текст в скобках]"
    assert prof_app.parse_suggestions(text) == []


def test_parse_suggestions_no_array():
    assert prof_app.parse_suggestions("совсем нет массива") == []
    assert prof_app.parse_suggestions("") == []


def test_parse_suggestions_non_list_json():
    # валидный JSON, но не массив (объект) → вернуть пустой список
    # (re.search r'\[.*\]' не найдёт скобок у голого объекта)
    assert prof_app.parse_suggestions('{"title":"x"}') == []
