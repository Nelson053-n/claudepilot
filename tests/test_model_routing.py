"""Авто-роутинг модели по типу работы (services._model_for)."""
import services as svc


def test_default_models(monkeypatch):
    # без settings-override все типы → sonnet (дефолт; Opus только по маркеру
    # сложности через _model_for_card, см. test_budget_guard).
    monkeypatch.setattr(svc.db, "get_setting", lambda *a, **k: None)
    assert svc._model_for("review") == "claude-sonnet-4-6"
    assert svc._model_for("analyze") == "claude-sonnet-4-6"
    assert svc._model_for("task") == "claude-sonnet-4-6"


def test_unknown_kind_falls_back_to_task(monkeypatch):
    monkeypatch.setattr(svc.db, "get_setting", lambda *a, **k: None)
    assert svc._model_for("???") == svc._MODELS["task"]


def test_settings_override(monkeypatch):
    # settings-ключ model:<kind> переопределяет дефолт
    overrides = {"model:review": "claude-haiku-4-5", "model:task": "custom-model"}
    monkeypatch.setattr(svc.db, "get_setting", lambda key, *a, **k: overrides.get(key))
    assert svc._model_for("review") == "claude-haiku-4-5"
    assert svc._model_for("task") == "custom-model"
    # analyze без override → дефолт
    assert svc._model_for("analyze") == "claude-sonnet-4-6"
