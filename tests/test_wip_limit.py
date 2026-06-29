"""
Тесты WIP-лимита: при достижении лимита одновременно бегущих задач новые
карточки встают в очередь (status='queued'), а reaper авто-стартует самую
старую queued по FIFO, как только освобождается слот.

subprocess.Popen замокан — реальный claude не вызывается; вместо процесса
возвращается фейк с фиксированным pid.
"""
import itertools

import pytest

import db
import services as svc


@pytest.fixture
def tmp_env(tmp_path, monkeypatch):
    """Изолированная БД и runs/; Popen замокан (без реального claude)."""
    test_db = tmp_path / "test.db"
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(db, "DB_PATH", test_db)
    monkeypatch.setattr(svc, "RUNS", runs)
    db.init_db()

    real_popen = svc.subprocess.Popen

    class FakeProc:
        def __init__(self, *a, **kw):
            self.pid = 424242

    # Мокаем ТОЛЬКО спавн claude (bash -lc ...); git rev-parse и пр. в _spawn_card
    # (head_at_start) пропускаем к реальному Popen, иначе FakeProc без communicate
    # ломает subprocess.run.
    def fake_popen(args, *a, **kw):
        if isinstance(args, (list, tuple)) and args and args[0] == "bash":
            return FakeProc(args, *a, **kw)
        return real_popen(args, *a, **kw)

    monkeypatch.setattr(svc.subprocess, "Popen", fake_popen)
    # окно НЕ переполнено — иначе start_card уводит всё в queued (зависело бы от
    # реальной 5h-квоты, делая тесты лимитов недетерминированными).
    monkeypatch.setattr(svc, "_window_util_exceeded", lambda: False)
    # wip-throttle тоже читает РЕАЛЬНОЕ окно (_effective_wip_limit→get_usage): при
    # util>60% режет лимит до 1 → тесты с 2 параллельными картами падали, когда
    # реальное 5h-окно высоко. Глушим throttle, чтобы лимит был детерминирован.
    db.set_setting("wip_throttle_util", "0")
    # выбор модели → дефолт без вызова Claude: гибрид-роутер на «серых» задачах
    # (дефолтные prompt'ы вроде "do it N") иначе сделал бы реальный Haiku-вызов.
    monkeypatch.setattr(svc, "_model_for_card", lambda card: svc._MODELS["task"])
    yield runs


_seq = itertools.count()


def _card(prompt=None, slug="-home-nel-t"):
    # уникальный prompt на каждую карточку: add_card схлопывает дубли с
    # одинаковым title+prompt в окне 10с (защита от двойного POST).
    if prompt is None:
        prompt = f"do it {next(_seq)}"
    board = db.ensure_board(slug.rsplit("-", 1)[-1], slug)
    return db.add_card(board["id"], "title", prompt, slug, column="approved")


def test_wip_limit_queues_second_card(tmp_env):
    db.set_setting("wip_limit", "1")
    c1, c2 = _card(), _card()

    svc.start_card(c1)
    svc.start_card(c2)

    a, b = db.get_card(c1["id"]), db.get_card(c2["id"])
    assert a["status"] == "running"
    # вторая упёрлась в лимит → queued, осталась в колонке approved, с пометкой
    assert b["status"] == "queued"
    assert b["column"] == "approved"
    assert "очеред" in b["result"].lower()
    assert svc.count_running() == 1


def test_freeing_slot_autostarts_queued(tmp_env):
    db.set_setting("wip_limit", "1")
    c1, c2 = _card(), _card()
    svc.start_card(c1)
    svc.start_card(c2)
    assert db.get_card(c2["id"])["status"] == "queued"

    # первая завершается успешно (rc=0) → reaper финализирует и авто-стартует вторую
    runs = tmp_env
    (runs / f"card_{c1['id']}.rc").write_text("0\n")
    (runs / f"card_{c1['id']}.out").write_text('{"type":"result","result":"ок","total_cost_usd":0.01}')

    svc.refresh_running_cards()
    svc.start_next_queued()

    a, b = db.get_card(c1["id"]), db.get_card(c2["id"])
    assert a["status"] == "done"
    assert b["status"] == "running"  # авто-старт второй
    assert svc.count_running() == 1


def test_fifo_order_of_queued(tmp_env):
    """Из нескольких queued первой стартует самая старая (по created_at)."""
    db.set_setting("wip_limit", "1")
    c1, c2, c3 = _card(), _card(), _card()
    svc.start_card(c1)          # running
    svc.start_card(c2)          # queued (раньше)
    svc.start_card(c3)          # queued (позже)

    runs = tmp_env
    (runs / f"card_{c1['id']}.rc").write_text("0\n")
    (runs / f"card_{c1['id']}.out").write_text('{"type":"result","result":"ок"}')
    svc.refresh_running_cards()
    svc.start_next_queued()

    # освободился ровно один слот → стартовала c2 (старшая), c3 ещё в очереди
    assert db.get_card(c2["id"])["status"] == "running"
    assert db.get_card(c3["id"])["status"] == "queued"


def test_under_limit_starts_immediately(tmp_env):
    """При лимите > числа бегущих карточка стартует сразу, без очереди."""
    db.set_setting("wip_limit", "3")
    c1 = _card()
    svc.start_card(c1)
    assert db.get_card(c1["id"])["status"] == "running"


def test_project_lock_one_per_slug_per_pass(tmp_env, monkeypatch):
    """Проект-лок: за один проход start_next_queued стартует МАКСИМУМ одну задачу
    на проект (slug). Очередь A1,B1,A2,B2 при пустом running и большом лимите →
    запускаются по одной из каждого проекта (A1,B1), вторые (A2,B2) ждут."""
    db.set_setting("parallelism", "project")
    db.set_setting("wip_limit", "0")  # всё в queued
    A, B = "-home-nel-a", "-home-nel-b"
    _card(slug=A); _card(slug=B); _card(slug=A); _card(slug=B)
    for c in db.list_cards():
        db.update_card(c["id"], status="queued")

    order = []
    # _spawn_card должен помечать карточку running, чтобы project_busy видел лок
    def fake_spawn(card):
        order.append(card["slug"])
        db.update_card(card["id"], status="running", pid=1)
    monkeypatch.setattr(svc, "_spawn_card", fake_spawn)
    db.set_setting("wip_limit", "99")
    svc.start_next_queued()

    # по одной на проект: A и B, но НЕ две A и не две B
    assert sorted(order) == [A, B]


def test_project_lock_blocks_second_same_project(tmp_env, monkeypatch):
    """start_card второй задачи занятого проекта → queued, не running."""
    db.set_setting("parallelism", "project")
    db.set_setting("wip_limit", "99")  # WIP не ограничивает — лочит только проект
    A = "-home-nel-a"
    c1 = _card(slug=A)
    c2 = _card(slug=A)
    svc.start_card(c1)
    svc.start_card(c2)
    assert db.get_card(c1["id"])["status"] == "running"
    assert db.get_card(c2["id"])["status"] == "queued"


def test_parallelism_off_disables_project_lock(tmp_env, monkeypatch):
    """Режим 'off': две задачи одного проекта бегут параллельно (лок снят)."""
    db.set_setting("parallelism", "off")
    db.set_setting("wip_limit", "99")
    A = "-home-nel-a"
    c1 = _card(slug=A)
    c2 = _card(slug=A)
    svc.start_card(c1)
    svc.start_card(c2)
    assert db.get_card(c1["id"])["status"] == "running"
    assert db.get_card(c2["id"])["status"] == "running"


def test_get_wip_limit_default_and_override(tmp_env):
    assert db.get_wip_limit() == db.WIP_LIMIT_DEFAULT  # 3 по умолчанию
    db.set_setting("wip_limit", "5")
    assert db.get_wip_limit() == 5
    db.set_setting("wip_limit", "мусор")  # некорректное → дефолт
    assert db.get_wip_limit() == db.WIP_LIMIT_DEFAULT
