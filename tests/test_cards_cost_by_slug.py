"""
Тест app._avg_by_slug и поля by_slug в /api/cards/cost.

Средняя стоимость считается ПО КОНКРЕТНОМУ slug (а не по всем проектам),
чтобы оценка перед запуском была точной. Карточки без cost_usd не учитываются.
"""
import app
import db


def test_avg_by_slug_per_project():
    cards = [
        {"slug": "-home-nel-alpha", "cost_usd": 0.10},
        {"slug": "-home-nel-alpha", "cost_usd": 0.30},   # alpha avg = 0.20
        {"slug": "-home-nel-beta", "cost_usd": 1.00},    # beta avg = 1.00
        {"slug": "-home-nel-beta", "cost_usd": None},    # без cost — не считается
    ]
    out = app._avg_by_slug([c for c in cards if c.get("cost_usd") is not None])
    assert out["-home-nel-alpha"] == {"avg_cost_usd": 0.20, "tasks": 2}
    assert out["-home-nel-beta"] == {"avg_cost_usd": 1.00, "tasks": 1}


def test_cards_cost_endpoint_by_slug(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    board = db.ensure_board("Alpha", "-home-nel-alpha")
    c1 = db.add_card(board["id"], "t1", "p", "-home-nel-alpha")
    c2 = db.add_card(board["id"], "t2", "p", "-home-nel-alpha")
    db.update_card(c1["id"], cost_usd=0.10)
    db.update_card(c2["id"], cost_usd=0.30)

    data = app.r_cards_cost()
    assert data["by_slug"]["-home-nel-alpha"] == {"avg_cost_usd": 0.20, "tasks": 2}
    # средняя по проекту совпадает с общей здесь (один проект), но поле существует отдельно
    assert data["avg_cost_usd"] == 0.20
