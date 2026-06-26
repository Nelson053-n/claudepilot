"""
Тесты экономии квоты 5h-окна (оптимизация, не урезание):
  - _model_for_card → Sonnet по умолчанию, Opus только при маркере сложности
  - _window_util_exceeded → старт задач придерживается при высокой утилизации окна
"""
import itertools
import pytest
import db
import services as svc


@pytest.fixture
def tmp_env(tmp_path, monkeypatch):
    test_db = tmp_path / "test.db"
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(db, "DB_PATH", test_db)
    monkeypatch.setattr(svc, "RUNS", runs)
    db.init_db()
    yield runs


_seq = itertools.count()


def _card(slug="-home-nel-t", title="t", prompt=None):
    if prompt is None:
        prompt = f"do {next(_seq)}"
    board = db.ensure_board("T", slug)
    return db.add_card(board["id"], title, prompt, slug, column="approved")


def test_routing_default_sonnet(tmp_env):
    # обычная задача → Sonnet (дефолт, экономит квоту)
    c = _card(title="починить баг", prompt="поправь функцию")
    assert svc._model_for_card(c) == "claude-sonnet-4-6"


def test_routing_opus_on_marker(tmp_env):
    # явный маркер сложности → Opus
    for marker in ("[opus]", "[hard]", "[сложно]"):
        c = _card(title=f"{marker} большой рефакторинг", prompt="...")
        assert svc._model_for_card(c) == svc.OPUS
    # маркер в задании тоже срабатывает
    c = _card(title="задача", prompt="это [complex] требует Opus")
    assert svc._model_for_card(c) == svc.OPUS


def test_routing_settings_override(tmp_env):
    # settings model:task имеет высший приоритет — принудительно opus на всё
    db.set_setting("model:task", "claude-opus-4-8")
    c = _card(title="простая", prompt="мелочь")
    assert svc._model_for_card(c) == "claude-opus-4-8"


def test_window_util_guard(tmp_env, monkeypatch):
    db.set_setting("window_util_limit", "85")
    monkeypatch.setattr(svc, "get_usage", lambda: {"five_hour": {"util": 90}})
    assert svc._window_util_exceeded() is True
    monkeypatch.setattr(svc, "get_usage", lambda: {"five_hour": {"util": 50}})
    assert svc._window_util_exceeded() is False
    db.set_setting("window_util_limit", "0")  # выключено
    monkeypatch.setattr(svc, "get_usage", lambda: {"five_hour": {"util": 99}})
    assert svc._window_util_exceeded() is False


def test_start_card_queued_when_window_full(tmp_env, monkeypatch):
    db.set_setting("window_util_limit", "85")
    monkeypatch.setattr(svc, "get_usage", lambda: {"five_hour": {"util": 95}})
    spawned = []
    monkeypatch.setattr(svc, "_spawn_card", lambda c: spawned.append(c["id"]))
    c = _card()
    svc.start_card(c)
    assert not spawned  # не запущена — окно почти исчерпано
    assert db.get_card(c["id"])["status"] == "queued"
