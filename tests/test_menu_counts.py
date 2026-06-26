"""
Тест /api/menu-counts — лёгкий эндпоинт для фоновых бейджей бокового меню.

Проверяем: корректные подсчёты (projects/todos/services_ok/services_total/mcp)
и in-memory кеш с TTL (повторный запрос не пересчитывает, пока TTL не истёк).
"""
import app
import db


def _make_project(root, slug, files):
    """files: {filename: content}. MEMORY.md не считается проектным файлом."""
    mem = root / slug / "memory"
    mem.mkdir(parents=True)
    for name, txt in files.items():
        (mem / name).write_text(txt)


def test_menu_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    projects = tmp_path / "projects"
    monkeypatch.setattr(app, "PROJECTS_DIR", projects)
    # сбросить кеш между тестами
    app._MENU_CACHE["data"] = None
    app._MENU_CACHE["ts"] = 0.0

    # 2 проекта; во втором есть TODO-строка → 1 todo
    _make_project(projects, "-home-nel-alpha", {"a.md": "просто заметка"})
    _make_project(projects, "-home-nel-beta",
                  {"b.md": "TODO: дописать парсер usage", "MEMORY.md": "индекс"})

    # 2 сервиса: один up (200), один down (503)
    s1 = db.add_service("up", "http://up")
    s2 = db.add_service("down", "http://down")
    db.update_service(s1["id"], last_status=200)
    db.update_service(s2["id"], last_status=503)

    db.add_mcp("m1", command="echo")

    c = app.r_menu_counts()
    assert c == {"projects": 2, "todos": 1, "services_ok": 1,
                 "services_total": 2, "mcp": 1}


def test_menu_counts_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    projects = tmp_path / "projects"
    monkeypatch.setattr(app, "PROJECTS_DIR", projects)
    app._MENU_CACHE["data"] = None
    app._MENU_CACHE["ts"] = 0.0

    _make_project(projects, "-home-nel-alpha", {"a.md": "заметка"})
    first = app.r_menu_counts()
    assert first["projects"] == 1

    # добавляем второй проект — но кеш ещё свежий, значение не меняется
    _make_project(projects, "-home-nel-beta", {"b.md": "заметка"})
    assert app.r_menu_counts()["projects"] == 1

    # TTL истёк → пересчёт
    app._MENU_CACHE["ts"] = 0.0
    assert app.r_menu_counts()["projects"] == 2
