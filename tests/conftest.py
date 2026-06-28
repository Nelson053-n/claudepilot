"""
Фикстуры для тестов prof.

Токен задаётся через PROF_TOKEN ДО импорта app, чтобы он был детерминирован
и не зависел от prof.db (get_token() читает env в первую очередь).
"""
import os
import subprocess as _subprocess
import sys
from pathlib import Path

import pytest

# корень проекта в sys.path + фиксированный токен до импорта app
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["PROF_TOKEN"] = "test-token-fixed"

from fastapi.testclient import TestClient  # noqa: E402

import app as prof_app  # noqa: E402
import services as _svc  # noqa: E402

TOKEN = os.environ["PROF_TOKEN"]


def _is_claude_cmd(args) -> bool:
    """Команда args (для subprocess.run/Popen) запускает headless `claude -p`?
    Реальный спавн в prof — всегда bash -lc '... claude -p ...' (см. _spawn_card,
    run_agent_once). Только их и глушим; git/pytest/прочее пропускаем к реальному."""
    try:
        flat = " ".join(args) if isinstance(args, (list, tuple)) else str(args)
    except Exception:
        return False
    return "claude -p" in flat


class _FakeClaudeProc:
    """Заглушка процесса claude для Popen: только pid, без communicate/wait."""
    def __init__(self, *a, **kw):
        self.pid = 424242

    def poll(self):
        return None


def _fake_run_agent_once(prompt, cwd, out_name, timeout=600, kind="analyze"):
    """Нейтральный вердикт вместо headless claude (route-классификатор И авто-ревью)."""
    return {"ok": True, "text": "VERDICT: DONE\nMODEL: SONNET", "cost_usd": None}


@pytest.fixture(scope="session", autouse=True)
def _no_real_claude_session():
    """СЕССИОННЫЙ барьер для run_agent_once — НЕ снимается между тестами.

    Авто-ревью (refresh_running_cards при колонке review) спавнит run_agent_once в
    ДЕМОН-потоке без join. Per-test monkeypatch снимался бы в teardown раньше, чем
    поток дойдёт до вызова → поток догонял бы РЕАЛЬНЫЙ claude (гонка — утечка
    test_pattern3_autosave_race). Ставим мок один раз на всю сессию через прямой
    setattr (переживает teardown каждого теста). Тест-специфичные monkeypatch
    (test_analyze/test_model_routing/test_auto_review) накладываются поверх и
    откатываются обратно к ЭТОМУ моку, а не к реальному run_agent_once."""
    real = _svc.run_agent_once
    _svc.run_agent_once = _fake_run_agent_once
    try:
        yield
    finally:
        _svc.run_agent_once = real


@pytest.fixture(autouse=True)
def _no_real_claude(monkeypatch):
    """ГЛОБАЛЬНЫЙ барьер: ни один тест не спавнит реальный `claude -p`.

    Раньше часть тестов (через start_card/_spawn_card/refresh_running_cards→
    start_next_queued и _model_for_card→run_agent_once) запускала живые claude-
    процессы — pytest плодил десятки сессий в ~/.claude/projects/, каждая жгла
    5h-окно подписки. Здесь перехватываем subprocess.run/Popen в services и, если
    команда — это `claude -p`, возвращаем заглушку вместо запуска. Не-claude
    вызовы (git, pytest валидатора) проходят к реальному subprocess.

    autouse → применяется ко всем тестам. Тест-специфичные monkeypatch (свой
    Popen/_spawn_card/_model_for_card) ставятся ПОЗЖЕ и переопределяют барьер —
    их поведение не меняется, барьер лишь страхует от случайного спавна."""
    real_run = _svc.subprocess.run
    real_popen = _svc.subprocess.Popen

    def guarded_run(args, *a, **kw):
        if _is_claude_cmd(args):
            # run_agent_once пишет вывод в файл через shell-редирект (> out.rc);
            # без реального claude файла не будет, но run_agent_once это переживает
            # (читает '' → парсит в пустой text). Возвращаем «успешный» процесс.
            return _subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return real_run(args, *a, **kw)

    def guarded_popen(args, *a, **kw):
        if _is_claude_cmd(args):
            return _FakeClaudeProc(args, *a, **kw)
        return real_popen(args, *a, **kw)

    monkeypatch.setattr(_svc.subprocess, "run", guarded_run)
    monkeypatch.setattr(_svc.subprocess, "Popen", guarded_popen)
    yield


@pytest.fixture
def client():
    with TestClient(prof_app.app) as c:
        yield c
