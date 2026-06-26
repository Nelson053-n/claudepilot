"""
Тест app._cards_by_model и поля by_model в /api/cards/cost.

Разбивка prof-задач по тарифному классу модели (opus/sonnet/haiku) через
sessions._tier_name. Карты без проставленной model (запуски до роутинга) идут
в класс 'unknown' — отдельно, чтобы фронт мог показать секцию только при наличии
реальной модели.
"""
import app
import db


def test_cards_by_model_aggregation():
    cards = [
        {"model": "claude-opus-4-8", "cost_usd": 1.00, "input_tokens": 100, "output_tokens": 50},
        {"model": "claude-opus-4-8", "cost_usd": 0.50, "input_tokens": 20, "output_tokens": 10},
        {"model": "claude-sonnet-4-6", "cost_usd": 0.20, "input_tokens": 80, "output_tokens": 40},
        {"model": "claude-haiku-4-5", "cost_usd": 0.05, "input_tokens": 30, "output_tokens": 15},
        {"model": None, "cost_usd": 0.90, "input_tokens": 60, "output_tokens": 30},  # до роутинга
    ]
    bm = app._cards_by_model(cards)
    assert set(bm) == {"opus", "sonnet", "haiku", "unknown"}
    # opus: две карты сложены
    assert bm["opus"] == {"cost": 1.50, "input": 120, "output": 60, "tasks": 2}
    assert bm["sonnet"] == {"cost": 0.20, "input": 80, "output": 40, "tasks": 1}
    assert bm["haiku"] == {"cost": 0.05, "input": 30, "output": 15, "tasks": 1}
    # карта без model → unknown, не теряется
    assert bm["unknown"] == {"cost": 0.90, "input": 60, "output": 30, "tasks": 1}


def test_cards_by_model_all_unknown():
    # ни одной карты с моделью (колонка есть, роутинг ещё не пишет) → только unknown
    cards = [{"model": None, "cost_usd": 0.10, "input_tokens": 10, "output_tokens": 5}]
    bm = app._cards_by_model(cards)
    assert set(bm) == {"unknown"}
    assert bm["unknown"]["tasks"] == 1


def test_cards_cost_endpoint_by_model(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    board = db.ensure_board("Alpha", "-home-nel-alpha")
    c1 = db.add_card(board["id"], "t1", "p", "-home-nel-alpha")
    c2 = db.add_card(board["id"], "t2", "p", "-home-nel-alpha")
    db.update_card(c1["id"], cost_usd=1.00, model="claude-opus-4-8")
    db.update_card(c2["id"], cost_usd=0.20, model="claude-sonnet-4-6")

    data = app.r_cards_cost()
    bm = data["by_model"]
    assert bm["opus"]["cost"] == 1.00 and bm["opus"]["tasks"] == 1
    assert bm["sonnet"]["cost"] == 0.20 and bm["sonnet"]["tasks"] == 1
    assert "unknown" not in bm  # обе карты с моделью
