"""
Тесты services._deploy_not_done — ДЕТЕРМИНИРОВАННЫЙ детект «не выкачено», без опоры
на текст отчёта агента (тот мог промолчать про неудачу). Сравнивает git HEAD на
старте задачи (head_at_start) с HEAD после: коммита не появилось → результат не
закоммичен → на прод/тест не попадёт → not_deployed.

Проверка OPT-IN (настройка deploy_check:<slug>) и консервативна: worktree-режим и
отсутствие head_at_start её отключают.
"""
import subprocess

import pytest

import db
import services as svc


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Изолированная БД, чтобы set_setting/get_setting не трогали prod-базу."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


@pytest.fixture
def repo(tmp_path):
    """Реальный git-репозиторий с одним коммитом."""
    r = tmp_path / "proj"
    r.mkdir()
    _git(["init", "-b", "main"], r)
    _git(["config", "user.email", "t@t"], r)
    _git(["config", "user.name", "t"], r)
    (r / "a.txt").write_text("v1")
    _git(["add", "-A"], r)
    _git(["commit", "-m", "init"], r)
    return r


def _head(r):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(r),
                          capture_output=True, text=True).stdout.strip()


def test_no_commit_since_start_is_not_done(tmp_db, repo):
    # opt-in включён, HEAD на старте зафиксирован, нового коммита нет → not_deployed.
    db.set_setting("deploy_check:proj", "1")
    card = {"slug": "proj", "head_at_start": _head(repo)}
    # агент наработал правки, но НЕ закоммитил (рабочее дерево грязное, HEAD тот же)
    (repo / "a.txt").write_text("v2-uncommitted")
    assert svc._deploy_not_done(card, repo) is True


def test_new_commit_means_deployed(tmp_db, repo):
    db.set_setting("deploy_check:proj", "1")
    start = _head(repo)
    card = {"slug": "proj", "head_at_start": start}
    # агент закоммитил → HEAD сдвинулся → НЕ not_deployed
    (repo / "a.txt").write_text("v2")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "work"], repo)
    assert _head(repo) != start
    assert svc._deploy_not_done(card, repo) is False


def test_disabled_by_default(tmp_db, repo):
    # без настройки deploy_check:<slug> проверка молчит (opt-in), даже если коммита нет.
    card = {"slug": "proj", "head_at_start": _head(repo)}
    (repo / "a.txt").write_text("v2-uncommitted")
    assert svc._deploy_not_done(card, repo) is False


def test_no_head_at_start_skips(tmp_db, repo):
    # HEAD на старте не зафиксирован — сравнивать не с чем, проверка пропускается.
    db.set_setting("deploy_check:proj", "1")
    card = {"slug": "proj", "head_at_start": None}
    assert svc._deploy_not_done(card, repo) is False


def test_worktree_mode_skips(tmp_db, repo):
    # worktree-режим: коммит/мердж делает финализатор позже — проверка тут не лезет.
    db.set_setting("deploy_check:proj", "1")
    card = {"slug": "proj", "head_at_start": _head(repo),
            "worktree_path": str(repo)}
    assert svc._deploy_not_done(card, repo) is False


@pytest.fixture
def repo_with_remote(repo, tmp_path):
    """Репо с bare-remote и отслеживаемым upstream (main → origin/main, синхронны)."""
    bare = tmp_path / "remote.git"
    _git(["init", "--bare", "-b", "main", str(bare)], tmp_path)
    _git(["remote", "add", "origin", str(bare)], repo)
    _git(["push", "-u", "origin", "main"], repo)
    return repo


def test_committed_but_not_pushed_is_not_done(tmp_db, repo_with_remote):
    # агент закоммитил (HEAD сдвинулся), но push не прошёл (пароль не подошёл) →
    # локальная ветка опережает origin/main → результат на remote/прод не уехал.
    db.set_setting("deploy_check:proj", "1")
    r = repo_with_remote
    card = {"slug": "proj", "head_at_start": _head(r)}
    (r / "a.txt").write_text("v2")
    _git(["add", "-A"], r)
    _git(["commit", "-m", "work"], r)  # коммит есть, push НЕ делаем
    assert svc._deploy_not_done(card, r) is True


def test_committed_and_pushed_is_done(tmp_db, repo_with_remote):
    # закоммитил И запушил → upstream догнал HEAD → выкачено, не not_deployed.
    db.set_setting("deploy_check:proj", "1")
    r = repo_with_remote
    card = {"slug": "proj", "head_at_start": _head(r)}
    (r / "a.txt").write_text("v2")
    _git(["add", "-A"], r)
    _git(["commit", "-m", "work"], r)
    _git(["push"], r)
    assert svc._deploy_not_done(card, r) is False
