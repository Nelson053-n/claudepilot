"""
Тесты защиты от двойного запуска (инцидент #77: задача стартовала двумя
процессами claude в одну рабочую папку → 537КБ .out, мусор в result).

Корень: внешние пути (/run,/continue,/move) звали claim_card_run, но внутренние
пути reaper'а (start_next_queued/start_scheduled/resume_paused) спавнили через
_spawn_card напрямую, а тот выставлял status='running' только В КОНЦЕ (после
секундных worktree/baseline/git). В этом окне второй тик reaper'а видел карточку
не-running и спавнил дубль. Фикс: db.claim_spawn атомарно метит running в начале
_spawn_card; проигравший вызов выходит без процесса.
"""
import itertools
import threading

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


def _card(slug="-home-nel-t"):
    board = db.ensure_board("T", slug)
    return db.add_card(board["id"], "t", f"do {next(_seq)}", slug, column="approved")


# ---- атомарность claim_spawn ----------------------------------------------

def test_claim_spawn_first_wins(tmp_env):
    c = _card()
    assert db.claim_spawn(c["id"]) is True          # первый — победитель
    assert db.get_card(c["id"])["status"] == "running"
    assert db.claim_spawn(c["id"]) is False         # второй — уже running, проигрыш


def test_claim_spawn_from_queued(tmp_env):
    # карточка reaper'а приходит из queued — claim_spawn должен её застолбить
    c = _card()
    db.update_card(c["id"], status="queued")
    assert db.claim_spawn(c["id"]) is True


def test_claim_spawn_from_paused(tmp_env):
    # resume_paused: paused-карточка тоже столбится
    c = _card()
    db.update_card(c["id"], status="paused")
    assert db.claim_spawn(c["id"]) is True


# ---- _spawn_card не плодит дубли -------------------------------------------

def _mock_popen(monkeypatch, calls):
    """Popen('bash'…) → фейк-процесс + счётчик; остальное (git и пр.) к реальному."""
    real = svc.subprocess.Popen

    class Fake:
        def __init__(self): self.pid = 4242

    def fake(args, *a, **kw):
        if isinstance(args, (list, tuple)) and args and args[0] == "bash":
            calls.append(args)
            return Fake()
        return real(args, *a, **kw)

    monkeypatch.setattr(svc.subprocess, "Popen", fake)


def test_two_spawns_one_process(tmp_env, monkeypatch):
    calls = []
    _mock_popen(monkeypatch, calls)
    c = _card()

    svc._spawn_card(c)   # первый — спавнит
    svc._spawn_card(c)   # второй — claim проигрывает, процесса нет

    assert len(calls) == 1            # ровно ОДИН процесс claude
    assert db.get_card(c["id"])["status"] == "running"


def test_concurrent_spawns_one_process(tmp_env, monkeypatch):
    # настоящая гонка: N потоков зовут _spawn_card одновременно
    calls = []
    _mock_popen(monkeypatch, calls)
    c = _card()
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()           # стартуем все разом — максимизируем гонку
        svc._spawn_card(dict(c))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert len(calls) == 1   # несмотря на 8 одновременных вызовов — один процесс


def test_spawn_prep_failure_releases_lock(tmp_env, monkeypatch):
    # сбой подготовки ДО Popen (напр. project_path кинул) → карточка не зависает
    # running, а возвращается в queued (замок снят, reaper повторит).
    c = _card()

    def boom(*a, **k):
        raise RuntimeError("disk gone")
    monkeypatch.setattr(svc, "project_path", boom)

    svc._spawn_card(c)
    assert db.get_card(c["id"])["status"] == "queued"
