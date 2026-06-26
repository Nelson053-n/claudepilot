"""
Фикс 2 — slug должен следовать за доской, не за картой.

При переносе карты на другую доску cards.slug устаревает. _card_cwd и validate_card
должны брать slug из boards (через board_id), а не из cards.slug.
"""
import subprocess
from pathlib import Path

import pytest

import db
import services as svc


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(svc, "RUNS", tmp_path / "runs")
    (tmp_path / "runs").mkdir()
    db.init_db()
    return tmp_path


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "x.txt").write_text("hi\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=path, check=True)
    return path


def test_card_board_slug_returns_board_slug(env):
    """_card_board_slug возвращает slug доски, не cards.slug."""
    board = db.ensure_board("ProjectA", "slug-board-a")
    card = db.add_card(board["id"], "task", "do x", "slug-OLD-stale")

    result = svc._card_board_slug(card)

    assert result == "slug-board-a"


def test_card_board_slug_fallback_to_card_slug(env):
    """Если board_id=None — fallback на cards.slug."""
    card = {"slug": "fallback-slug", "board_id": None}

    assert svc._card_board_slug(card) == "fallback-slug"


def test_card_cwd_uses_board_slug(env, monkeypatch):
    """_card_cwd резолвит путь через slug ДОСКИ, а не cards.slug."""
    board_path = env / "board_proj"
    _git_repo(board_path)

    board = db.ensure_board("BoardProj", "slug-board-proj")
    card = db.add_card(board["id"], "task", "do x", "slug-STALE-from-old-board")

    # project_path для slug доски → board_path; для cards.slug → HOME
    def fake_project_path(slug):
        if slug == "slug-board-proj":
            return board_path
        return svc.HOME

    monkeypatch.setattr(svc, "project_path", fake_project_path)

    cwd = svc._card_cwd(card)

    assert cwd == board_path, f"ожидали {board_path}, получили {cwd}"


def test_validate_card_uses_board_slug_for_validate_cmd(env, monkeypatch):
    """validate_cmd ищется по slug доски, не по устаревшему cards.slug."""
    proj = env / "real_proj"
    proj.mkdir()

    board = db.ensure_board("RealProj", "slug-real")
    card = db.add_card(board["id"], "task", "do x", "slug-STALE")

    db.set_setting("validate_cmd:slug-real", "exit 0")
    db.set_setting("validate_cmd:slug-STALE", "exit 1")  # не должен выполняться

    monkeypatch.setattr(svc, "project_path", lambda slug: proj)

    v = svc.validate_card(card)

    assert v["ok"] is True, f"validate_cmd взял stale slug: {v}"
