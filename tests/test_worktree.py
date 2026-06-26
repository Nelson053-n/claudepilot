"""
Тесты parallelism=worktree: изоляция задач одного проекта через git-worktree
и мердж результата в основную ветку.

Структура:
  • unit на project_busy/_serializes_project/_worktree_enabled (логика режима);
  • один ИНТЕГРАЦИОННЫЙ тест на реальном временном git-репо: worktree реально
    создаётся, мердж без конфликта вливается, конфликт → merge_conflict.
"""
import subprocess

import pytest

import db
import services as svc


# --------------------------- фикстуры ---------------------------
@pytest.fixture
def tmp_env(tmp_path, monkeypatch):
    """Изолированная БД и runs/; Popen замокан (без реального claude)."""
    test_db = tmp_path / "test.db"
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(db, "DB_PATH", test_db)
    monkeypatch.setattr(svc, "RUNS", runs)
    monkeypatch.setattr(svc, "_WORKTREES", runs / "worktrees")
    db.init_db()

    # Мокаем ТОЛЬКО запуск claude (_spawn_card: bash -lc ...), а реальные git-
    # вызовы (_run/_git через subprocess.run, тоже использует Popen) пропускаем
    # к настоящему Popen — иначе сломаем интеграцию с git.
    real_popen = svc.subprocess.Popen

    class FakeProc:
        def __init__(self, *a, **kw):
            self.pid = 424242

    def fake_popen(args, *a, **kw):
        if isinstance(args, (list, tuple)) and args and args[0] == "bash":
            return FakeProc()
        return real_popen(args, *a, **kw)

    monkeypatch.setattr(svc.subprocess, "Popen", fake_popen)
    # окно не переполнено — иначе start_card уводит карточки в queued (зависимость
    # от реальной 5h-квоты делала бы worktree-тесты недетерминированными).
    monkeypatch.setattr(svc, "_window_util_exceeded", lambda: False)
    yield runs


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """Реальный git-репозиторий с одним коммитом (ветка main)."""
    r = tmp_path / "proj"
    r.mkdir()
    _git(["init", "-b", "main"], r)
    _git(["config", "user.email", "t@t"], r)
    _git(["config", "user.name", "t"], r)
    (r / "f.txt").write_text("base\n")
    _git(["add", "-A"], r)
    _git(["commit", "-m", "init"], r)
    return r


def _project(repo_path):
    """Регистрирует repo как проект prof и возвращает slug."""
    slug = svc.path_to_slug(repo_path)
    db.upsert_project(slug, "proj", str(repo_path))
    return slug


# --------------------------- логика режима ---------------------------
def test_project_busy_false_in_worktree_mode(tmp_env):
    """worktree: project_busy=False (задачи изолированы, лок снят)."""
    db.set_setting("parallelism", "worktree")
    slug = "-home-nel-other"
    board = db.ensure_board("o", slug)
    c1 = db.add_card(board["id"], "t", "p1", slug, column="approved")
    db.update_card(c1["id"], status="running", pid=1)
    c2 = db.add_card(board["id"], "t", "p2", slug, column="approved")
    assert svc.project_busy(slug, c2["id"]) is False


def test_project_busy_true_for_self_prof_in_worktree(tmp_env):
    """worktree + self-prof: лок остаётся (worktree для prof отключён)."""
    db.set_setting("parallelism", "worktree")
    slug = svc.SELF_SLUG
    board = db.ensure_board("prof", slug)
    c1 = db.add_card(board["id"], "t", "p1", slug, column="approved")
    db.update_card(c1["id"], status="running", pid=1)
    c2 = db.add_card(board["id"], "t", "p2", slug, column="approved")
    assert svc.project_busy(slug, c2["id"]) is True


def test_serializes_project_matrix(tmp_env):
    other, self_ = "-home-nel-x", svc.SELF_SLUG
    db.set_setting("parallelism", "project")
    assert svc._serializes_project(other) is True
    db.set_setting("parallelism", "off")
    assert svc._serializes_project(other) is False
    db.set_setting("parallelism", "worktree")
    assert svc._serializes_project(other) is False
    assert svc._serializes_project(self_) is True  # prof — исключение


def test_worktree_enabled_requires_git_and_not_self(tmp_env, repo):
    db.set_setting("parallelism", "worktree")
    slug = _project(repo)
    assert svc._worktree_enabled(slug) is True
    # self-prof отключён даже в worktree-режиме
    assert svc._worktree_enabled(svc.SELF_SLUG) is False
    # не-git папка → отключён
    db.upsert_project("-home-nel-nogit", "n", str(repo.parent))
    assert svc._worktree_enabled("-home-nel-nogit") is False
    # другой режим → отключён
    db.set_setting("parallelism", "project")
    assert svc._worktree_enabled(slug) is False


# --------------------------- интеграция: реальный git ---------------------------
def test_create_and_remove_worktree(tmp_env, repo):
    slug = _project(repo)
    cid = 7
    res = svc.create_worktree(slug, cid)
    assert res is not None
    wt, base = res
    assert wt.is_dir()
    assert base == "main"
    # ветка задачи создана
    br = subprocess.run(["git", "branch", "--list", "prof-card-7"],
                        cwd=str(repo), capture_output=True, text=True)
    assert "prof-card-7" in br.stdout
    # уборка
    svc.remove_worktree(slug, cid)
    assert not wt.exists()
    br = subprocess.run(["git", "branch", "--list", "prof-card-7"],
                        cwd=str(repo), capture_output=True, text=True)
    assert "prof-card-7" not in br.stdout


def test_merge_clean(tmp_env, repo):
    """Изменение в worktree-ветке вливается в main без конфликта."""
    slug = _project(repo)
    cid = 11
    wt, base = svc.create_worktree(slug, cid)
    # правка в worktree: новый файл (не конфликтует с main)
    (wt / "new.txt").write_text("from task\n")
    _git(["add", "-A"], wt)
    _git(["commit", "-m", "task work"], wt)
    m = svc.merge_worktree(slug, cid, base)
    assert m["ok"] is True
    assert m["conflict"] is False
    # файл появился в основной рабочей копии
    assert (repo / "new.txt").read_text() == "from task\n"


def test_merge_conflict(tmp_env, repo):
    """Конкурирующая правка той же строки в main и в ветке → конфликт мерджа."""
    slug = _project(repo)
    cid = 22
    wt, base = svc.create_worktree(slug, cid)
    # ветка задачи меняет f.txt
    (wt / "f.txt").write_text("task-version\n")
    _git(["add", "-A"], wt)
    _git(["commit", "-m", "task edit"], wt)
    # main параллельно меняет ту же строку
    (repo / "f.txt").write_text("main-version\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "main edit"], repo)

    m = svc.merge_worktree(slug, cid, base)
    assert m["ok"] is False
    assert m["conflict"] is True
    # основная ветка осталась чистой (merge --abort), без следов конфликта
    st = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo),
                        capture_output=True, text=True)
    assert st.stdout.strip() == ""
    # ветка задачи сохранена для ручного домерджа
    br = subprocess.run(["git", "branch", "--list", "prof-card-22"],
                        cwd=str(repo), capture_output=True, text=True)
    assert "prof-card-22" in br.stdout


def test_refresh_merges_on_done(tmp_env, repo):
    """Полный путь: задача в worktree завершилась rc=0, валидация ок → reaper
    коммитит правки, мерджит ветку в main, worktree удаляется, статус done."""
    db.set_setting("parallelism", "worktree")
    runs = tmp_env
    slug = _project(repo)
    board = db.ensure_board("proj", slug)
    card = db.add_card(board["id"], "t", "сделай дело", slug, column="approved")
    cid = card["id"]

    svc.start_card(db.get_card(cid))  # стартует в worktree (Popen замокан)
    c = db.get_card(cid)
    assert c["status"] == "running"
    assert c["worktree_path"]  # worktree создан
    wt = svc.Path(c["worktree_path"])
    assert wt.is_dir()

    # имитируем работу агента в worktree (правки НЕ закоммичены — reaper закоммитит)
    (wt / "result.txt").write_text("done by agent\n")

    # имитируем завершение процесса claude
    (runs / f"card_{cid}.rc").write_text("0\n")
    (runs / f"card_{cid}.out").write_text(
        '{"type":"result","result":"готово","total_cost_usd":0.02}')

    svc.refresh_running_cards()

    c = db.get_card(cid)
    assert c["status"] == "done"
    assert c["column"] == "review"
    # правки влиты в main
    assert (repo / "result.txt").read_text() == "done by agent\n"
    # worktree убран, путь снят
    assert not wt.exists()
    assert c["worktree_path"] is None


def test_refresh_marks_merge_conflict(tmp_env, repo, monkeypatch):
    """rc=0 и валидация ок, но мердж конфликтует → статус merge_conflict, работа
    сохранена (ветка/worktree на месте)."""
    db.set_setting("parallelism", "worktree")
    runs = tmp_env
    slug = _project(repo)
    board = db.ensure_board("proj", slug)
    card = db.add_card(board["id"], "t", "правь f.txt", slug, column="approved")
    cid = card["id"]

    svc.start_card(db.get_card(cid))
    c = db.get_card(cid)
    wt = svc.Path(c["worktree_path"])

    # агент меняет f.txt в worktree
    (wt / "f.txt").write_text("task-version\n")
    # main параллельно меняет ту же строку → будущий конфликт
    _git(["config", "user.email", "t@t"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "f.txt").write_text("main-version\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "main edit"], repo)

    (runs / f"card_{cid}.rc").write_text("0\n")
    (runs / f"card_{cid}.out").write_text(
        '{"type":"result","result":"готово","total_cost_usd":0.01}')

    svc.refresh_running_cards()

    c = db.get_card(cid)
    assert c["status"] == "merge_conflict"
    assert c["validate_status"] == "merge_conflict"
    assert "конфликт" in c["result"].lower()
    # ветка и worktree сохранены
    assert wt.exists()
    br = subprocess.run(["git", "branch", "--list", f"prof-card-{cid}"],
                        cwd=str(repo), capture_output=True, text=True)
    assert f"prof-card-{cid}" in br.stdout


def test_cleanup_orphan_worktrees(tmp_env, repo):
    """Стартовая уборка: ветка/worktree завершённой (не running) карточки сносится
    при условии, что она замерджена; running — не трогается."""
    slug = _project(repo)
    # карточка 1: завершена (idle), ветка замерджена → должна снестись
    board = db.ensure_board("proj", slug)
    c1 = db.add_card(board["id"], "t", "p1", slug)
    wt1, base = svc.create_worktree(slug, c1["id"])
    (wt1 / "a.txt").write_text("x\n")
    _git(["add", "-A"], wt1)
    _git(["commit", "-m", "w"], wt1)
    svc.merge_worktree(slug, c1["id"], base)  # ветка замерджена, но не удалена

    removed = svc.cleanup_orphan_worktrees()
    assert removed >= 1
    br = subprocess.run(["git", "branch", "--list", f"prof-card-{c1['id']}"],
                        cwd=str(repo), capture_output=True, text=True)
    assert f"prof-card-{c1['id']}" not in br.stdout
