"""
Фикс 1 — надёжный PATH для validate_cmd в TS-проектах.

_nvm_bin() должен находить bin/ актуальной версии node из ~/.nvm/versions/node/.
_run_bash_lc должен подмешивать nvm-bin в PATH, чтобы npx/node резолвились.
"""
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import services as svc


def test_nvm_bin_finds_latest(tmp_path, monkeypatch):
    """_nvm_bin возвращает bin/ самой новой версии по semver."""
    nvm_versions = tmp_path / ".nvm" / "versions" / "node"
    for ver in ("v18.1.0", "v20.20.1", "v16.0.0"):
        (nvm_versions / ver / "bin").mkdir(parents=True)
    monkeypatch.setattr(svc, "HOME", tmp_path)

    result = svc._nvm_bin()

    assert result is not None
    assert "v20.20.1" in result
    assert result.endswith("bin")


def test_nvm_bin_uses_nvmrc(tmp_path, monkeypatch):
    """.nvmrc в HOME указывает конкретную версию — берём её."""
    nvm_versions = tmp_path / ".nvm" / "versions" / "node"
    for ver in ("v18.1.0", "v20.20.1"):
        (nvm_versions / ver / "bin").mkdir(parents=True)
    (tmp_path / ".nvmrc").write_text("18.1.0\n")
    monkeypatch.setattr(svc, "HOME", tmp_path)

    result = svc._nvm_bin()

    assert result is not None
    assert "v18.1.0" in result


def test_nvm_bin_none_when_no_nvm(tmp_path, monkeypatch):
    """Нет ~/.nvm → _nvm_bin возвращает None без исключений."""
    monkeypatch.setattr(svc, "HOME", tmp_path)

    assert svc._nvm_bin() is None


def test_run_bash_lc_injects_nvm_path(tmp_path, monkeypatch):
    """_run_bash_lc добавляет nvm bin в PATH процесса."""
    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir()
    monkeypatch.setattr(svc, "_nvm_bin", lambda: str(fake_bin))

    # команда печатает PATH и rc=0
    r = svc._run_bash_lc("echo $PATH", tmp_path)

    assert r is not None
    assert r.returncode == 0
    assert str(fake_bin) in r.stdout


def test_run_bash_lc_works_without_nvm(tmp_path, monkeypatch):
    """_run_bash_lc не падает, если nvm отсутствует."""
    monkeypatch.setattr(svc, "_nvm_bin", lambda: None)

    r = svc._run_bash_lc("exit 0", tmp_path)

    assert r is not None
    assert r.returncode == 0


def test_validate_cmd_override_uses_run_bash_lc(tmp_path, monkeypatch):
    """validate_card с validate_cmd использует _run_bash_lc (а не bare bash)."""
    calls = []

    def fake_run_bash_lc(cmd, cwd, timeout=None):
        calls.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    db.set_setting("validate_cmd:myslug", "npx tsc")

    # project_path → tmp_path (не HOME, чтобы не пропустить валидацию)
    monkeypatch.setattr(svc, "project_path", lambda slug: tmp_path)
    monkeypatch.setattr(svc, "_run_bash_lc", fake_run_bash_lc)

    svc.validate_card({"slug": "myslug", "board_id": None})

    assert calls == ["npx tsc"]
