"""
Тест app._merge_cost_by_project и эндпоинта /api/cost/combined.

Объединяет разбивку по проектам из карточек (prof-задачи + проверки) и из
live-сессий Claude Code (консоль). Матчинг — по нормализованному имени проекта
(поле `project`). Проект может быть только в карточках, только в консоли или в обоих.
Итог = задачи + проверки + консоль.
"""
import app
import db


def test_merge_matches_by_project_name():
    # одинаковое нормализованное имя проекта в обоих источниках → одна строка
    cards = [{"project": "alpha", "cost_usd": 1.00, "review_cost_usd": 0.20,
              "total_usd": 1.20, "input": 100, "output": 50, "tasks": 2}]
    sess = [{"project": "alpha", "cost": 3.00, "input": 9000, "output": 4000, "messages": 7}]
    out = app._merge_cost_by_project(cards, sess)
    assert len(out) == 1
    r = out[0]
    assert r["project"] == "alpha"
    assert r["cost_usd"] == 1.00
    assert r["review_cost_usd"] == 0.20
    assert r["console_cost_usd"] == 3.00
    assert r["console_input"] == 9000 and r["console_output"] == 4000
    # итого = задачи + проверки + консоль
    assert r["total_usd"] == 4.20


def test_merge_project_only_in_cards_or_console():
    cards = [{"project": "alpha", "cost_usd": 1.00, "review_cost_usd": 0.0,
              "total_usd": 1.00, "input": 0, "output": 0, "tasks": 1}]
    sess = [{"project": "beta", "cost": 2.50, "input": 5000, "output": 1000, "messages": 3}]
    out = app._merge_cost_by_project(cards, sess)
    by = {r["project"]: r for r in out}
    # alpha: только карточки, консоль = 0
    assert by["alpha"]["console_cost_usd"] == 0.0
    assert by["alpha"]["total_usd"] == 1.00
    # beta: только консоль, задач 0
    assert by["beta"]["tasks"] == 0
    assert by["beta"]["cost_usd"] == 0.0
    assert by["beta"]["console_cost_usd"] == 2.50
    assert by["beta"]["total_usd"] == 2.50


def test_merge_sorted_by_total_desc():
    cards = [{"project": "small", "cost_usd": 0.10, "review_cost_usd": 0.0,
              "total_usd": 0.10, "input": 0, "output": 0, "tasks": 1}]
    sess = [{"project": "big", "cost": 9.00, "input": 0, "output": 0, "messages": 1}]
    out = app._merge_cost_by_project(cards, sess)
    assert [r["project"] for r in out] == ["big", "small"]


def test_combined_endpoint_merges_cards_and_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    board = db.ensure_board("Alpha", "-home-nel-alpha")
    c1 = db.add_card(board["id"], "t1", "p", "-home-nel-alpha")
    db.update_card(c1["id"], cost_usd=0.50, review_cost_usd=0.10)

    # подменяем live-сессии: консольный расход по тому же проекту "alpha"
    monkeypatch.setattr(app.sess, "cost_summary", lambda days=7: {
        "by_project": [{"project": "alpha", "cost": 2.00,
                        "input": 1000, "output": 500, "messages": 4}]})

    data = app.r_cost_combined(days=7)
    by = {r["project"]: r for r in data["by_project"]}
    assert "alpha" in by
    r = by["alpha"]
    # карточки matched с консолью по нормализованному имени "alpha"
    assert r["cost_usd"] == 0.50
    assert r["review_cost_usd"] == 0.10
    assert r["console_cost_usd"] == 2.00
    assert r["console_input"] == 1000
    assert r["total_usd"] == 2.60
