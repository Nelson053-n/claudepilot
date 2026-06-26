"""
Пустой git diff + rc=0 — не провал.

validate_card возвращает no_git_changes=True; refresh_running_cards направляет
карту в done (не failed, не needs_input): ops/инфра-задачи и autosave-гонка
не должны давать ложный статус. needs_input выставляется только если агент
явно спросил пользователя (_needs_user_input срабатывает ДО validate_card).
"""
import itertools
import subprocess
from pathlib import Path

import pytest

import db
import services as svc


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(svc, "RUNS", runs)
    db.init_db()
    return tmp_path, runs


def _clean_git_repo(path: Path) -> Path:
    """Git-репо без незакоммиченных изменений (чистый diff)."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=path, check=True)
    return path


def test_validate_card_no_changes_returns_no_git_changes(env, monkeypatch):
    """validate_card при чистом git возвращает no_git_changes=True."""
    tmp_path, _ = env
    proj = _clean_git_repo(tmp_path / "proj")
    monkeypatch.setattr(svc, "project_path", lambda slug: proj)

    v = svc.validate_card({"slug": "any", "board_id": None})

    assert v.get("no_git_changes") is True
    assert "нет изменений" in v["summary"]


def test_validate_card_with_changes_no_git_changes_false(env, monkeypatch):
    """При наличии изменений no_git_changes отсутствует (или False)."""
    tmp_path, _ = env
    proj = _clean_git_repo(tmp_path / "proj")
    (proj / "new.txt").write_text("added\n")  # незакоммиченный файл
    monkeypatch.setattr(svc, "project_path", lambda slug: proj)

    v = svc.validate_card({"slug": "any", "board_id": None})

    assert not v.get("no_git_changes")


_seq = itertools.count()


def _running_card(slug="-home-nel-proj"):
    board = db.ensure_board("T", slug)
    card = db.add_card(board["id"], "title", f"do it {next(_seq)}", slug, column="in_progress")
    db.update_card(card["id"], status="running", pid=None)
    return card["id"]


def test_refresh_empty_diff_ops_goes_to_done(env, monkeypatch):
    """rc=0 + пустой git diff + нет стоп-фраз → ops/инфра-задача уходит в done.

    Паттерн 1 (ops/инфра): задача делала деплой по ssh, локальный git чист.
    Паттерн 3 (autosave-гонка): коммит был, но проверка git прошла до autosave.
    В обоих случаях done/review — правильный исход.
    """
    tmp_path, runs = env
    proj = _clean_git_repo(tmp_path / "proj")
    monkeypatch.setattr(svc, "project_path", lambda slug: proj)

    cid = _running_card()
    (runs / f"card_{cid}.rc").write_text("0\n")
    (runs / f"card_{cid}.out").write_text(
        '{"result":"Анализ завершён. Правок не требуется — это информационный отчёт.","total_cost_usd":0.01}'
    )

    svc.refresh_running_cards()

    card = db.get_card(cid)
    assert card["status"] == "done", f"ожидали done, получили {card['status']}"
    assert card["column"] == "review", f"ожидали review, получили {card['column']}"


def test_refresh_nonempty_diff_still_done(env, monkeypatch):
    """rc=0 + есть изменения → карта уходит в done (поведение не сломано)."""
    tmp_path, runs = env
    proj = _clean_git_repo(tmp_path / "proj")
    (proj / "change.txt").write_text("changed\n")  # незакоммиченный файл
    monkeypatch.setattr(svc, "project_path", lambda slug: proj)

    cid = _running_card()
    (runs / f"card_{cid}.rc").write_text("0\n")
    (runs / f"card_{cid}.out").write_text('{"result":"готово","total_cost_usd":0.01}')

    svc.refresh_running_cards()

    card = db.get_card(cid)
    assert card["status"] == "done", f"ожидали done, получили {card['status']}"
    assert card["column"] == "review"
