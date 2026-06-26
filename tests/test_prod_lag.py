"""
Тесты прод-аудита services.prod_check / refresh_prod_lag — детект «запушено в
origin, но не задеплоено на сервер». Закрывает дыру, которую _deploy_not_done не
видит: код в GitHub есть, но прод (git pull на сервере) отстал.

Команда deploy_remote_cmd:<slug> печатает HEAD прода; prod_check сравнивает его
с origin/<branch>. Чистый git/ssh — без LLM. Здесь «прод» эмулируем echo'ем
нужного коммита (вместо ssh), а origin — реальным bare-remote.
"""
import subprocess

import pytest

import db
import services as svc


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


def _head(r):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(r),
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


@pytest.fixture
def repo_with_remote(tmp_path, monkeypatch):
    """Локальный репо с веткой main, привязанной к bare-remote (origin), + 1 коммит
    запушен. project_path('proj') замокан на этот репо."""
    bare = tmp_path / "bare.git"
    bare.mkdir()
    _git(["init", "--bare", "-b", "main"], bare)
    r = tmp_path / "proj"
    r.mkdir()
    _git(["init", "-b", "main"], r)
    _git(["config", "user.email", "t@t"], r)
    _git(["config", "user.name", "t"], r)
    _git(["remote", "add", "origin", str(bare)], r)
    (r / "a.txt").write_text("v1")
    _git(["add", "-A"], r)
    _git(["commit", "-m", "c1"], r)
    _git(["push", "-u", "origin", "main"], r)
    monkeypatch.setattr(svc, "project_path", lambda slug: r)
    return r


def test_no_cmd_skips(tmp_db, repo_with_remote):
    # команда не задана → проверка пропускается (error=no_cmd, ok=False)
    r = svc.prod_check("proj")
    assert r["ok"] is False and r["error"] == "no_cmd"


def test_prod_in_sync_lag_zero(tmp_db, repo_with_remote):
    # прод на том же коммите, что origin → lag=0
    db.set_setting("deploy_remote_cmd:proj", f"echo {_head(repo_with_remote)}")
    r = svc.prod_check("proj")
    assert r["ok"] is True and r["lag"] == 0


def test_prod_behind(tmp_db, repo_with_remote):
    # прод застрял на c1; пушим c2 в origin → прод отстаёт на 1 коммит
    old = _head(repo_with_remote)
    db.set_setting("deploy_remote_cmd:proj", f"echo {old}")
    (repo_with_remote / "a.txt").write_text("v2")
    _git(["add", "-A"], repo_with_remote)
    _git(["commit", "-m", "c2"], repo_with_remote)
    _git(["push", "origin", "main"], repo_with_remote)
    r = svc.prod_check("proj")
    assert r["ok"] is True and r["lag"] == 1
    assert r["prod_head"] == old


def test_prod_unknown_commit_lag_minus_one(tmp_db, repo_with_remote):
    # прод печатает коммит, которого нет в репо → lag=-1 (расхождение, но не посчитать)
    db.set_setting("deploy_remote_cmd:proj", "echo deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
    r = svc.prod_check("proj")
    assert r["ok"] is True and r["lag"] == -1


def test_cmd_failure_reported(tmp_db, repo_with_remote):
    # команда падает (rc≠0) → ok=False, error содержит cmd_rc
    db.set_setting("deploy_remote_cmd:proj", "exit 7")
    r = svc.prod_check("proj")
    assert r["ok"] is False and "cmd_rc7" in (r["error"] or "")


def test_bad_prod_head(tmp_db, repo_with_remote):
    # команда печатает мусор вместо хэша → ok=False
    db.set_setting("deploy_remote_cmd:proj", "echo nope")
    r = svc.prod_check("proj")
    assert r["ok"] is False and "bad_prod_head" in (r["error"] or "")


def test_refresh_writes_setting(tmp_db, repo_with_remote, monkeypatch):
    # refresh_prod_lag(force) пишет prod_lag:<slug> для проектов с командой.
    import json
    db.ensure_board("Proj", "proj")
    db.set_setting("deploy_remote_cmd:proj", f"echo {_head(repo_with_remote)}")
    out = svc.refresh_prod_lag(force=True)
    assert out["proj"]["lag"] == 0
    saved = json.loads(db.get_setting("prod_lag:proj"))
    assert saved["lag"] == 0
