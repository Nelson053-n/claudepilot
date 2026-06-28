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


# ---- гибрид-классификатор (_model_for_card + _classify_model_for_card) ----
# Эвристика по ключевым словам решает явные случаи БЕЗ вызова Claude; Haiku-вызов
# (run_agent_once kind="route") — только в серой зоне. Так пачка карт не даёт скачок
# cache_creation (раньше Haiku звался на КАЖДУЮ карту).

def _settings(monkeypatch, **vals):
    """db.get_setting(key, default=None) → vals.get(key, default). Имитирует БД
    настроек: smart_router по умолчанию включён (как в коде), если не задан иначе."""
    def get(key, default=None, *a, **k):
        return vals.get(key, default)
    monkeypatch.setattr(svc.db, "get_setting", get)


def _no_haiku(monkeypatch):
    """Гарантия, что Haiku НЕ зовётся (явный сигнал должен решаться эвристикой)."""
    def boom(*a, **k):
        raise AssertionError("Haiku-классификатор не должен вызываться на явном сигнале")
    monkeypatch.setattr(svc, "run_agent_once", boom)


def _mock_haiku(monkeypatch, label):
    """run_agent_once → фейк-вердикт Haiku с меткой; реальный claude не зовём."""
    def fake(prompt, cwd, out_name, *a, **k):
        return {"text": f"MODEL: {label}\nобоснование", "cost_usd": 0.0001}
    monkeypatch.setattr(svc, "run_agent_once", fake)
    monkeypatch.setattr(svc, "project_path", lambda slug: svc.PROF)
    monkeypatch.setattr(svc.db, "update_card", lambda *a, **k: None)


def test_classifier_opus_on_complex(monkeypatch):
    # (a) сильный сигнал сложности (_OPUS_KEYWORDS) → OPUS эвристикой, без Haiku
    _settings(monkeypatch)                       # smart_router вкл по умолчанию
    _no_haiku(monkeypatch)
    card = {"id": 1, "title": "Рефакторинг подсистемы", "prompt": "переписать архитектуру"}
    assert svc._model_for_card(card) == svc.OPUS


def test_classifier_sonnet_on_trivial(monkeypatch):
    # (a) сигнал тривиальности (_SONNET_KEYWORDS: опечатка) → Sonnet, без Haiku
    _settings(monkeypatch)
    _no_haiku(monkeypatch)
    card = {"id": 2, "title": "Поправить опечатку", "prompt": "в тексте кнопки"}
    assert svc._model_for_card(card) == svc._MODELS["task"]


def test_classifier_opus_keywords(monkeypatch):
    # (a) разные сигналы сложности из _OPUS_KEYWORDS → OPUS, без Haiku
    _settings(monkeypatch)
    _no_haiku(monkeypatch)
    for kw in ("webhook", "шифрование токенов", "race condition", "миграция схемы"):
        card = {"id": 9, "title": kw, "prompt": ""}
        assert svc._model_for_card(card) == svc.OPUS, kw


def test_grey_zone_calls_haiku_opus(monkeypatch):
    # (a) серая зона (нет ни сильного, ни тривиального сигнала) → Haiku; вердикт OPUS
    _settings(monkeypatch)
    _mock_haiku(monkeypatch, "OPUS")
    card = {"id": 10, "title": "Доработать обработку очереди заданий", "prompt": "по описанию"}
    assert svc._model_for_card(card) == svc.OPUS


def test_grey_zone_calls_haiku_sonnet(monkeypatch):
    # (a) серая зона; вердикт Haiku SONNET → дефолт Sonnet
    _settings(monkeypatch)
    _mock_haiku(monkeypatch, "SONNET")
    card = {"id": 11, "title": "Доработать обработку очереди заданий", "prompt": "по описанию"}
    assert svc._model_for_card(card) == svc._MODELS["task"]


def test_grey_zone_haiku_failure_falls_back(monkeypatch):
    # (a) серая зона; сбой/таймаут Haiku → дефолт Sonnet, запуск не падает
    _settings(monkeypatch)
    def boom(*a, **k):
        raise RuntimeError("claude недоступен / таймаут")
    monkeypatch.setattr(svc, "run_agent_once", boom)
    monkeypatch.setattr(svc, "project_path", lambda slug: svc.PROF)
    card = {"id": 12, "title": "Доработать обработку очереди заданий", "prompt": "по описанию"}
    assert svc._model_for_card(card) == svc._MODELS["task"]


def test_manual_opus_marker_forces_opus(monkeypatch):
    # (b) ручной [opus]-маркер форсит Opus (даже на тривиальной задаче), без Haiku
    _settings(monkeypatch)
    _no_haiku(monkeypatch)
    card = {"id": 3, "title": "[opus] мелкая правка", "prompt": "тривиально"}
    assert svc._model_for_card(card) == svc.OPUS


def test_settings_task_override_highest_priority(monkeypatch):
    # (c) settings model:task — высший приоритет, классификатор не применяется
    _settings(monkeypatch, **{"model:task": "forced-model"})
    _no_haiku(monkeypatch)
    card = {"id": 4, "title": "архитектура", "prompt": "сложно"}
    assert svc._model_for_card(card) == "forced-model"


def test_smart_router_disabled_falls_back(monkeypatch):
    # (d) smart_router=0 отключает классификатор → дефолт Sonnet (без ручного маркера),
    # даже если в тексте есть слова-сигналы; Haiku не зовётся
    _settings(monkeypatch, smart_router="0")
    _no_haiku(monkeypatch)
    card = {"id": 5, "title": "архитектура рефакторинг", "prompt": "webhook"}
    assert svc._model_for_card(card) == svc._MODELS["task"]
