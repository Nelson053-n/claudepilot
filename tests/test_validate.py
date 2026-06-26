"""
Тесты services.validate_card и интеграции в refresh_running_cards.

validate_card детерминированно (без вызова claude) проверяет результат задачи
в cwd проекта: pytest (если настроен), синтаксис изменённых .py, наличие git-
изменений. Здесь — мок-проекты с pytest rc=0 / rc=1 и проверка маппинга колонок:
  ok   → review / passed
  fail → in_progress / failed
"""
import itertools
import subprocess
from pathlib import Path

import pytest

import db
import services as svc


@pytest.fixture
def tmp_env(tmp_path, monkeypatch):
    """Изолированная БД + runs/. project_path указываем на мок-проект через
    monkeypatch (slug→путь не выводим из HOME)."""
    test_db = tmp_path / "test.db"
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(db, "DB_PATH", test_db)
    monkeypatch.setattr(svc, "RUNS", runs)
    db.init_db()
    yield tmp_path, runs


def _make_project(root, *, test_passes: bool) -> Path:
    """Создаёт git-репо с pytest.ini и одним тестом (проходящим/падающим)."""
    proj = Path(root) / "proj"
    proj.mkdir()
    (proj / "pytest.ini").write_text("[pytest]\n")
    assertion = "assert 1 == 1" if test_passes else "assert 1 == 2"
    (proj / "test_x.py").write_text(f"def test_x():\n    {assertion}\n")
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
    # коммитим тест, потом меняем файл — чтобы git status видел изменение
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=proj, check=True)
    (proj / "test_x.py").write_text(
        f"def test_x():\n    {assertion}\n# изменение\n")
    return proj


def test_validate_ok_passing_tests(tmp_env, monkeypatch):
    root, _ = tmp_env
    proj = _make_project(root, test_passes=True)
    monkeypatch.setattr(svc, "project_path", lambda slug: proj)

    v = svc.validate_card({"slug": "-home-nel-proj"})

    assert v["ok"] is True
    assert "passed" in v["summary"]
    assert "git:" in v["summary"]


def test_validate_new_failures_block(tmp_env, monkeypatch):
    # Провалы тестов БЕЗ baseline (test_baseline отсутствует) → блокируют (ok=False).
    root, _ = tmp_env
    proj = _make_project(root, test_passes=False)
    monkeypatch.setattr(svc, "project_path", lambda slug: proj)

    v = svc.validate_card({"slug": "-home-nel-proj"})  # без test_baseline

    assert v["ok"] is False
    assert "✗" in v["summary"] and "тесты" in v["summary"]


def test_validate_preexisting_failures_dont_block(tmp_env, monkeypatch):
    # Если столько же провалов было ДО задачи (baseline) — не блокируем (ok=True),
    # помечаем как предсуществующие. Проект с 1 падающим тестом, baseline=1.
    root, _ = tmp_env
    proj = _make_project(root, test_passes=False)
    monkeypatch.setattr(svc, "project_path", lambda slug: proj)
    # узнаём фактическое число падающих и подаём его как baseline
    base = svc.count_failing_tests(proj)

    v = svc.validate_card({"slug": "-home-nel-proj", "test_baseline": base})

    assert v["ok"] is True
    assert "предсуществующие" in v["summary"]


def test_validate_ok_when_no_tests(tmp_env, monkeypatch):
    """Нет pytest.ini → ok=True с пометкой 'нет тестов' (но есть git-изменения)."""
    root, _ = tmp_env
    proj = Path(root) / "notests"
    proj.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    (proj / "readme.md").write_text("hi\n")  # неотслеженный файл = изменение
    monkeypatch.setattr(svc, "project_path", lambda slug: proj)

    v = svc.validate_card({"slug": "-home-nel-notests"})

    assert v["ok"] is True
    assert "нет тестов" in v["summary"]


def test_validate_no_git_changes_returns_marker(tmp_env, monkeypatch):
    """Чистый git при rc=0 — агент выполнил аналитическую задачу или задаёт вопрос.
    validate_card не должна считать это провалом (ok=True), а выставить no_git_changes=True,
    чтобы refresh_running_cards направил карту в needs_input, а не в failed."""
    root, _ = tmp_env
    proj = Path(root) / "clean"
    proj.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    (proj / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=proj, check=True)
    monkeypatch.setattr(svc, "project_path", lambda slug: proj)

    v = svc.validate_card({"slug": "-home-nel-clean"})

    assert v.get("no_git_changes") is True
    assert "нет изменений" in v["summary"]


def test_validate_cmd_override(tmp_env, monkeypatch):
    """settings validate_cmd:<slug> переопределяет дефолт; ok=rc==0."""
    root, _ = tmp_env
    proj = Path(root) / "ov"
    proj.mkdir()
    monkeypatch.setattr(svc, "project_path", lambda slug: proj)
    db.set_setting("validate_cmd:-home-nel-ov", "exit 0")

    v = svc.validate_card({"slug": "-home-nel-ov"})
    assert v["ok"] is True

    db.set_setting("validate_cmd:-home-nel-ov", "exit 3")
    v = svc.validate_card({"slug": "-home-nel-ov"})
    assert v["ok"] is False
    assert "rc=3" in v["summary"]


# ---- интеграция в refresh_running_cards: маппинг колонок ----
_seq = itertools.count()


def _running_card(slug="-home-nel-proj"):
    # уникальный prompt: add_card схлопывает дубли title+prompt в окне 10с.
    board = db.ensure_board("T", slug)
    card = db.add_card(board["id"], "title", f"do it {next(_seq)}", slug, column="in_progress")
    db.update_card(card["id"], status="running", pid=None)
    return card["id"]


def test_refresh_review_on_validation_pass(tmp_env, monkeypatch):
    root, runs = tmp_env
    proj = _make_project(root, test_passes=True)
    monkeypatch.setattr(svc, "project_path", lambda slug: proj)
    cid = _running_card()
    (runs / f"card_{cid}.rc").write_text("0\n")
    (runs / f"card_{cid}.out").write_text('{"result":"готово","total_cost_usd":0.01}')

    svc.refresh_running_cards()

    card = db.get_card(cid)
    assert card["column"] == "review"
    assert card["status"] == "done"
    assert card["validate_status"] == "passed"


def test_refresh_defers_validation_while_project_busy(tmp_env, monkeypatch):
    # Гонка #28: задача готова (.rc), но в том же проекте бежит ДРУГАЯ задача —
    # её правки поломали бы pytest валидируемой. Финализация откладывается:
    # задача остаётся running, .rc на месте, валидация не запускается этим тиком.
    root, runs = tmp_env
    proj = _make_project(root, test_passes=True)
    monkeypatch.setattr(svc, "project_path", lambda slug: proj)
    cid = _running_card()           # задача A — готова к финализации
    other = _running_card()         # задача B — тот же slug, ещё бежит
    (runs / f"card_{cid}.rc").write_text("0\n")
    (runs / f"card_{cid}.out").write_text('{"result":"готово","total_cost_usd":0.01}')

    svc.refresh_running_cards()

    a = db.get_card(cid)
    assert a["status"] == "running"          # отложена, не финализирована
    assert a["validate_status"] is None      # валидация не гонялась
    assert (runs / f"card_{cid}.rc").exists()  # .rc сохранён для след. тика

    # B завершилась → проект свободен → A финализируется нормально
    db.update_card(other, status="done")
    svc.refresh_running_cards()
    a = db.get_card(cid)
    assert a["column"] == "review"
    assert a["status"] == "done"
    assert a["validate_status"] == "passed"


def test_two_finished_neighbors_dont_block_each_other(tmp_env, monkeypatch):
    # Баг взаимной блокировки: ДВА соседа одного проекта оба завершились (.rc есть),
    # но оба ещё status=running. Раньше каждый видел другого как «running» и вечно
    # откладывал валидацию → оба висели running, доска моргала через SSE d.done.
    # Теперь сосед с .rc не считается блокирующим → оба финализируются.
    root, runs = tmp_env
    proj = _make_project(root, test_passes=True)
    monkeypatch.setattr(svc, "project_path", lambda slug: proj)
    a_id = _running_card()
    b_id = _running_card()  # тот же slug
    for cid in (a_id, b_id):  # ОБА завершились (.rc есть)
        (runs / f"card_{cid}.rc").write_text("0\n")
        (runs / f"card_{cid}.out").write_text('{"result":"ok","total_cost_usd":0.01}')

    svc.refresh_running_cards()  # один проход финализирует обоих

    for cid in (a_id, b_id):
        c = db.get_card(cid)
        assert c["status"] == "done", f"#{cid} завис в {c['status']} (взаимная блокировка)"
        assert c["column"] == "review"


def test_refresh_back_to_progress_on_validation_fail(tmp_env, monkeypatch):
    # Валидацию ЖЁСТКО валит только синтаксическая ошибка в изменённом .py
    # (точно вина задачи), не падающий pytest. Битый файл → задача обратно в работу.
    root, runs = tmp_env
    proj = Path(root) / "syntaxbad"
    proj.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    (proj / "broken.py").write_text("def f(:\n")  # синтаксическая ошибка
    monkeypatch.setattr(svc, "project_path", lambda slug: proj)
    cid = _running_card()
    (runs / f"card_{cid}.rc").write_text("0\n")
    (runs / f"card_{cid}.out").write_text('{"result":"готово","total_cost_usd":0.01}')

    svc.refresh_running_cards()

    card = db.get_card(cid)
    assert card["column"] == "in_progress"
    assert card["status"] == "failed"
    assert card["validate_status"] == "failed"
    assert "Валидация не прошла" in card["result"]
