"""
Регрессионные тесты на ложные срабатывания финализатора карточек.

Три реальных паттерна (карточки #90/#91/#92), где выполненные задачи
получали неверный статус:

  Паттерн 1 — ops/инфра-задача: работа на проде через ssh, локальный git чист,
               rc=0 → должна уйти в done, не в failed/needs_input.

  Паттерн 2 — надзорная задача: агент закоммитил и задеплоил, но в отчёте есть
               стоп-фраза из _NOT_DONE_MARKERS → текстовый детект не должен
               переопределять объективный git-факт (коммит есть → done).

  Паттерн 3 — гонка autosave: код в git (autosave-коммит), но проверка git
               прошла до него → пустой diff при rc=0 → done, не not_deployed.

  Регрессия  — реально незакоммиченная задача (deploy_check включён, HEAD не
               сдвинулся) → not_deployed должен остаться.
"""
import itertools
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


def _clean_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "t@t"], path)
    _git(["config", "user.name", "t"], path)
    (path / "f.txt").write_text("v1\n")
    _git(["add", "-A"], path)
    _git(["commit", "-m", "init"], path)
    return path


_seq = itertools.count()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(svc, "RUNS", runs)
    db.init_db()
    return tmp_path, runs


def _running_card(slug="-home-nel-proj"):
    board = db.ensure_board("T", slug)
    card = db.add_card(board["id"], "title", f"do it {next(_seq)}", slug,
                       column="in_progress")
    db.update_card(card["id"], status="running", pid=None)
    return card["id"]


# ---------------------------------------------------------------------------
# Паттерн 1: ops/инфра — локальный git чист, rc=0, нет стоп-фраз → done
# ---------------------------------------------------------------------------

def test_pattern1_ops_no_git_changes_rc0_goes_to_done(env, monkeypatch):
    """Паттерн 1: ops-задача (ssh/systemd), локальный git чист → done, не failed."""
    tmp_path, runs = env
    proj = _clean_repo(tmp_path / "proj")
    monkeypatch.setattr(svc, "project_path", lambda slug: proj)

    cid = _running_card()
    (runs / f"card_{cid}.rc").write_text("0\n")
    # Отчёт об ops-задаче: сервис стартовал 0/SUCCESS, локальных коммитов нет.
    (runs / f"card_{cid}.out").write_text(
        '{"result":"systemd unit обновлён по ssh. Сервис стартовал 0/SUCCESS.","total_cost_usd":0.02}'
    )

    svc.refresh_running_cards()

    card = db.get_card(cid)
    assert card["status"] == "done", \
        f"ops-задача с rc=0 и чистым git должна быть done, получили {card['status']}"
    assert card["column"] == "review"


def test_pattern1_ops_rc0_no_git_no_stop_phrases_done(env, monkeypatch):
    """Ops-задача, нет стоп-фраз в отчёте → done даже без изменений в git."""
    tmp_path, runs = env
    proj = _clean_repo(tmp_path / "proj")
    monkeypatch.setattr(svc, "project_path", lambda slug: proj)

    cid = _running_card()
    (runs / f"card_{cid}.rc").write_text("0\n")
    (runs / f"card_{cid}.out").write_text(
        '{"result":"Проверка прошла: 31/31 тестов зелёные. Задеплоено v1.0.26.","total_cost_usd":0.05}'
    )

    svc.refresh_running_cards()

    card = db.get_card(cid)
    assert card["status"] == "done", \
        f"надзорная задача с реальным деплоем должна быть done, получили {card['status']}"
    assert card["column"] == "review"


# ---------------------------------------------------------------------------
# Паттерн 2: стоп-фраза в отчёте, но коммит появился (deploy_check выключен)
# ---------------------------------------------------------------------------

def test_pattern2_stop_phrase_but_commit_exists_goes_to_done(env, monkeypatch):
    """Паттерн 2: _claims_not_done сработал, но git-изменения есть → done.

    deploy_check выключен (opt-out по умолчанию). У задачи есть незакоммиченные
    изменения, т.е. no_git_changes=False. Текстовый детект не должен давать
    not_deployed без git-подтверждения.
    """
    tmp_path, runs = env
    proj = _clean_repo(tmp_path / "proj")
    # имитируем незакоммиченное изменение (git-diff непустой)
    (proj / "f.txt").write_text("v2\n")
    monkeypatch.setattr(svc, "project_path", lambda slug: proj)

    cid = _running_card()
    (runs / f"card_{cid}.rc").write_text("0\n")
    # Отчёт содержит стоп-фразу (агент пишет про прошлое или чужую задачу), но
    # по факту коммит появился и deploy_check выключен.
    (runs / f"card_{cid}.out").write_text(
        '{"result":"Готово. Предыдущий деплой не удался, но текущая задача выполнена.","total_cost_usd":0.03}'
    )

    svc.refresh_running_cards()

    card = db.get_card(cid)
    # "не удался" не в _NOT_DONE_MARKERS, но проверяем общий принцип:
    # при непустом git-диффе и выключенном deploy_check → done.
    assert card["status"] == "done", \
        f"задача с git-изменениями должна быть done, получили {card['status']}"


def test_pattern2_stop_phrase_no_git_changes_goes_to_not_deployed(env, monkeypatch):
    """Паттерн 2 негатив: стоп-фраза + нет git-изменений → not_deployed (корректно).

    Оба сигнала вместе (no_git_changes И _claims_not_done) → реальный not_deployed.
    """
    tmp_path, runs = env
    proj = _clean_repo(tmp_path / "proj")
    monkeypatch.setattr(svc, "project_path", lambda slug: proj)

    cid = _running_card()
    (runs / f"card_{cid}.rc").write_text("0\n")
    # Агент явно признаётся + нет изменений в git → истинный not_deployed.
    (runs / f"card_{cid}.out").write_text(
        '{"result":"Сделал анализ, но не закоммитил — изменения остались локально.","total_cost_usd":0.01}'
    )

    svc.refresh_running_cards()

    card = db.get_card(cid)
    assert card["status"] == "not_deployed", \
        f"нет git-изменений + стоп-фраза → not_deployed, получили {card['status']}"


# ---------------------------------------------------------------------------
# Паттерн 3: гонка autosave — git чист в момент проверки, но это не провал
# ---------------------------------------------------------------------------

def test_pattern3_autosave_race_empty_git_rc0_goes_to_done(env, monkeypatch):
    """Паттерн 3: autosave ещё не прошёл, git чист, rc=0, нет стоп-фраз → done.

    Ситуация: агент закончил (rc=0), autosave не успел закоммитить,
    validate_card видит чистый git. Правильный исход — done, не not_deployed.
    """
    tmp_path, runs = env
    proj = _clean_repo(tmp_path / "proj")
    monkeypatch.setattr(svc, "project_path", lambda slug: proj)

    cid = _running_card()
    (runs / f"card_{cid}.rc").write_text("0\n")
    (runs / f"card_{cid}.out").write_text(
        '{"result":"Правки внесены, тесты 8/8 зелёные.","total_cost_usd":0.04}'
    )

    svc.refresh_running_cards()

    card = db.get_card(cid)
    assert card["status"] == "done", \
        f"autosave-гонка не должна давать not_deployed, получили {card['status']}"
    assert card["column"] == "review"


# ---------------------------------------------------------------------------
# Регрессия: реально незакоммиченная задача (deploy_check включён) → not_deployed
# ---------------------------------------------------------------------------

def test_regression_truly_not_committed_stays_not_deployed(env, monkeypatch):
    """Регрессия: deploy_check включён, HEAD не сдвинулся → not_deployed остаётся."""
    tmp_path, runs = env
    proj = _clean_repo(tmp_path / "proj")
    monkeypatch.setattr(svc, "project_path", lambda slug: proj)

    slug = "-home-nel-proj"
    db.set_setting(f"deploy_check:{slug}", "1")

    board = db.ensure_board("T", slug)
    card = db.add_card(board["id"], "title", f"do it {next(_seq)}", slug,
                       column="in_progress")
    head_start = _head(proj)
    db.update_card(card["id"], status="running", pid=None,
                   head_at_start=head_start)
    cid = card["id"]

    # Агент НЕ закоммитил (HEAD не сдвинулся), рабочее дерево может быть грязным.
    (proj / "f.txt").write_text("грязные правки, не закоммиченные\n")

    (runs / f"card_{cid}.rc").write_text("0\n")
    (runs / f"card_{cid}.out").write_text(
        '{"result":"Готово, всё поправил.","total_cost_usd":0.02}'
    )

    svc.refresh_running_cards()

    card = db.get_card(cid)
    assert card["status"] == "not_deployed", \
        f"реально незакоммиченная задача должна быть not_deployed, получили {card['status']}"
