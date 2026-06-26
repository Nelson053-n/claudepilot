"""
Тест prof_cli: интеграция add → list через реальные эндпоинты app.

prof_cli._req по умолчанию ходит urllib'ом на 127.0.0.1:7777. В тесте
переопределяем его на TestClient (ASGI, in-process) с изолированной БД —
так проверяется настоящая цепочка CLI → POST /api/cards → GET /api/cards,
включая ensure_board по slug, без сети и без реального сервера.
"""
import json

import pytest

import db
import prof_cli
from conftest import TOKEN


@pytest.fixture
def cli(tmp_path, monkeypatch, client):
    # изолированная БД (init_db уже вызван при импорте app, но на боевой prof.db)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()

    # _req CLI → TestClient вместо urllib
    def fake_req(method, path, body=None):
        headers = {"Authorization": f"Bearer {TOKEN}"}
        r = client.request(method, path, json=body, headers=headers)
        assert r.status_code == 200, f"{method} {path}: {r.status_code} {r.text}"
        return r.json() if r.text else {}

    monkeypatch.setattr(prof_cli, "_req", fake_req)
    monkeypatch.setattr(prof_cli, "_req_raw", fake_req)  # status зовёт его через _try
    yield


def test_add_then_list_roundtrip(cli, capsys):
    slug = "-home-nel-demo"
    prof_cli.main(["add", f"--board={slug}", "--title", "Тестовая задача",
                   "--prompt", "сделай что-то"])
    out = capsys.readouterr().out
    assert "Тестовая задача" in out
    assert "proposed" in out

    # та же доска (по slug) в list возвращает созданную карточку
    prof_cli.main(["--json", "list", f"--board={slug}"])
    listed = capsys.readouterr().out
    cards = json.loads(listed)
    assert any(c["title"] == "Тестовая задача" and c["column"] == "proposed"
               for c in cards)


def test_add_creates_board_by_slug(cli):
    slug = "-home-nel-newproj"
    # доски ещё нет — add должен её создать через ensure_board
    prof_cli.main(["add", f"--board={slug}", "--title", "Первая"])
    boards = db.list_boards()
    assert any(b["slug"] == slug for b in boards)
    # карточка унаследовала slug проекта
    cards = db.list_cards()
    card = next(c for c in cards if c["title"] == "Первая")
    assert card["slug"] == slug
    assert card["origin"] == "agent"


def test_status_summary(cli, capsys):
    prof_cli.main(["add", "--board=-home-nel-demo", "--title", "З1"])
    capsys.readouterr()  # сбросить вывод add, чтобы захватить только status
    prof_cli.main(["--json", "status"])
    data = json.loads(capsys.readouterr().out)
    assert data["cards"]["total"] >= 1
    assert data["cards"]["proposed"] >= 1
    assert "total_cost_usd" in data["cost"]
    assert "by_project" in data["cost"]
