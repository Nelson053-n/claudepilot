"""
Тесты авто-агента анализа (app.run_analysis):
- дедуп: предложение, похожее на уже стоящую карточку, не создаётся повторно;
- бюджет-гард: при five_hour.util > порога анализ не запускается (claude не зовём).

Изолированная БД во временной папке; svc.run_agent_once и svc.get_usage мокаются,
чтобы тесты не звали реальный claude/oauth.
"""
import pytest

import app as prof_app
import db
import services as svc


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    # project_path не должен зависеть от реальной ФС
    monkeypatch.setattr(svc, "project_path", lambda slug: tmp_path)
    yield


# ---------------- чистые хелперы ----------------
def test_is_dup_word_overlap():
    existing = [prof_app._norm_title("Добавить тесты для парсера usage")]
    # тот же смысл, другой порядок/пунктуация → дубль
    assert prof_app._is_dup("Тесты для парсера usage добавить!", existing)
    # совсем другая задача → не дубль
    assert not prof_app._is_dup("Починить git-бэкап при отсутствии remote", existing)


def test_is_dup_subset():
    existing = [prof_app._norm_title("Написать тесты валидации карточек")]
    # вхождение нормализованного множества → дубль
    assert prof_app._is_dup("тесты валидации", existing)


# ---------------- бюджет-гард ----------------
def test_run_analysis_skips_when_budget_high(tmp_db, monkeypatch):
    called = {"agent": False}

    def fake_agent(*a, **k):
        called["agent"] = True
        return {"text": "[]"}

    monkeypatch.setattr(svc, "get_usage", lambda: {"five_hour": {"util": 90}})
    monkeypatch.setattr(svc, "run_agent_once", fake_agent)

    res = prof_app.run_analysis("-home-nel-t")
    assert res["skipped"] is True
    assert "5h" in res["reason"]
    assert called["agent"] is False  # claude НЕ вызывался


def test_run_analysis_runs_when_budget_ok(tmp_db, monkeypatch):
    monkeypatch.setattr(svc, "get_usage", lambda: {"five_hour": {"util": 50}})
    monkeypatch.setattr(svc, "run_agent_once",
                        lambda *a, **k: {"text": '[{"title":"Новая задача","prompt":"do"}]'})
    res = prof_app.run_analysis("-home-nel-t")
    assert res.get("skipped") is not True
    assert res["suggested"] == 1


# ---------------- дедуп против существующих карточек ----------------
def test_run_analysis_dedups_existing(tmp_db, monkeypatch):
    name = prof_app._slug_name("-home-nel-t")
    board = db.ensure_board(name, "-home-nel-t")
    # уже стоящая карточка в активной колонке
    db.add_card(board["id"], "Добавить тесты для парсера usage", "p",
                "-home-nel-t", origin="agent", column="proposed")

    monkeypatch.setattr(svc, "get_usage", lambda: {"five_hour": {"util": 10}})
    # агент предлагает дубль (тот же смысл) + одну новую задачу
    suggestions = (
        '[{"title":"Тесты для парсера usage добавить","prompt":"x"},'
        '{"title":"Починить git remote backup","prompt":"y"}]'
    )
    monkeypatch.setattr(svc, "run_agent_once", lambda *a, **k: {"text": suggestions})

    res = prof_app.run_analysis("-home-nel-t")
    assert res["suggested"] == 1      # только новая создана
    assert res["skipped_dup"] == 1    # дубль отсеян

    titles = [c["title"] for c in db.list_cards(board["id"])]
    # дубль не задвоился, новая добавлена
    assert titles.count("Добавить тесты для парсера usage") == 1
    assert "Починить git remote backup" in titles


def test_run_analysis_done_rejected_not_dedup(tmp_db, monkeypatch):
    """Карточки в done/rejected НЕ участвуют в дедупе — предложить можно заново."""
    name = prof_app._slug_name("-home-nel-t")
    board = db.ensure_board(name, "-home-nel-t")
    db.add_card(board["id"], "Закрытая задача про логи", "p",
                "-home-nel-t", origin="agent", column="done")

    monkeypatch.setattr(svc, "get_usage", lambda: {"five_hour": {"util": 10}})
    monkeypatch.setattr(svc, "run_agent_once",
                        lambda *a, **k: {"text": '[{"title":"Закрытая задача про логи","prompt":"x"}]'})
    res = prof_app.run_analysis("-home-nel-t")
    assert res["suggested"] == 1
    assert res["skipped_dup"] == 0
