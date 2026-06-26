"""
Тест GET /api/schedule (app.build_schedule).

Проверяем, что обзор собирает: отложенные карточки (status=scheduled, по
scheduled_at), очередь (status=queued), регулярные процессы с интервалами и
лимитные окна. usage мокаем — без сети.
"""
import app
import db
import services as svc


def _fixed_usage():
    return {"available": True,
            "five_hour": {"util": 42, "left": "2h 0m",
                          "resets_at": "2099-01-01T00:00:00Z"},
            "seven_day": {"util": 10, "left": "5d 0h",
                          "resets_at": "2099-01-05T00:00:00Z"}}


def test_schedule_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(svc, "get_usage", _fixed_usage)
    monkeypatch.setattr(svc, "next_period_start", lambda: 4102444800.0)
    db.init_db()
    board = db.ensure_board("Alpha", "-home-nel-alpha")
    sched = db.add_card(board["id"], "отложенная", "p", "-home-nel-alpha")
    queued = db.add_card(board["id"], "в очереди", "p", "-home-nel-alpha")
    db.add_card(board["id"], "обычная", "p", "-home-nel-alpha")  # не попадёт
    db.update_card(sched["id"], status="scheduled", scheduled_at=4102444800.0)
    db.update_card(queued["id"], status="queued")

    d = app.build_schedule()

    # 1) отложенные
    assert len(d["scheduled"]) == 1
    assert d["scheduled"][0]["id"] == sched["id"]
    assert d["scheduled"][0]["scheduled_at"] == 4102444800.0
    assert d["scheduled"][0]["project"] == "alpha"

    # 2) очередь
    assert len(d["queued"]) == 1
    assert d["queued"][0]["id"] == queued["id"]

    # 3) регулярные процессы: git-бэкап с реальным интервалом + анализ «по требованию»
    names = {p["name"] for p in d["periodic"]}
    assert any("автобэкап" in n for n in names)
    assert any("reaper" in n for n in names)
    backup = next(p for p in d["periodic"] if "автобэкап" in p["name"])
    assert backup["interval"] == app.BACKUP_INTERVAL
    analysis = next(p for p in d["periodic"] if "анализ" in p["name"])
    assert analysis["interval"] is None  # крона нет — по требованию

    # 4) лимитные окна
    assert d["windows"]["available"] is True
    assert d["windows"]["five_hour"]["util"] == 42
    assert d["windows"]["next_period"] == 4102444800.0

    # версия проброшена
    assert d["version"] == app.PROF_VERSION


def test_schedule_route_via_client(client, monkeypatch):
    """Маршрут отдаёт 200 и нужные ключи (открытый GET, без токена)."""
    monkeypatch.setattr(svc, "get_usage", _fixed_usage)
    monkeypatch.setattr(svc, "next_period_start", lambda: None)
    r = client.get("/api/schedule")
    assert r.status_code == 200
    body = r.json()
    for key in ("scheduled", "queued", "periodic", "windows", "version"):
        assert key in body
