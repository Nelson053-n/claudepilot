"""
Тесты обзора навыков (services.discover_skills и парсеры):
- parse_frontmatter: кавычки, многострочные значения (`>` блоки), без блока;
- хуки из settings.json (событие + matcher + команда);
- команды/скилы/агенты из *.md (имя + описание из frontmatter);
- безопасное чтение при битых/отсутствующих файлах (errors=replace, try/except).

Изоляция: svc.CLAUDE_DIR подменяется на временную папку.
"""
import json

import services as svc


# ---------------- parse_frontmatter ----------------
def test_parse_frontmatter_basic():
    meta, body = svc.parse_frontmatter("---\nname: foo\ndescription: hello\n---\nтело")
    assert meta == {"name": "foo", "description": "hello"}
    assert body == "тело"


def test_parse_frontmatter_strips_quotes():
    meta, _ = svc.parse_frontmatter('---\nname: "q"\ntitle: \'s\'\n---\nb')
    assert meta["name"] == "q"
    assert meta["title"] == "s"


def test_parse_frontmatter_multiline_folded():
    # `>` запускает многострочный скаляр — строки-продолжения склеиваются
    txt = ("---\nname: geo-content\ndescription: >\n"
           "  Content quality specialist evaluating\n"
           "  E-E-A-T signals and depth.\n"
           "allowed-tools: Read, Bash\n---\nbody")
    meta, _ = svc.parse_frontmatter(txt)
    assert meta["name"] == "geo-content"
    assert meta["description"] == "Content quality specialist evaluating E-E-A-T signals and depth."
    assert meta["allowed-tools"] == "Read, Bash"


def test_parse_frontmatter_no_block():
    meta, body = svc.parse_frontmatter("просто текст\nвторая строка")
    assert meta == {}
    assert body == "просто текст\nвторая строка"


def test_parse_frontmatter_empty_values_dropped():
    meta, _ = svc.parse_frontmatter("---\nname: kept\nempty:\n---\nb")
    assert meta == {"name": "kept"}


# ---------------- helpers для построения ~/.claude ----------------
def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "CLAUDE_DIR", tmp_path)
    return tmp_path


# ---------------- hooks ----------------
def test_hooks_parsing(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    settings = {"hooks": {
        "SessionStart": [{"matcher": "", "hooks": [
            {"type": "command", "command": "python session-start.py"}]}],
        "PostToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "bash check.sh"}]}],
    }}
    (root / "settings.json").write_text(json.dumps(settings))
    hooks = svc._skills_hooks()
    assert len(hooks) == 2
    start = next(h for h in hooks if h["event"] == "SessionStart")
    assert start["matcher"] == "*"  # пустой matcher → "*"
    assert "session-start" in start["command"]
    post = next(h for h in hooks if h["event"] == "PostToolUse")
    assert post["matcher"] == "Bash"


def test_hooks_missing_settings(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)  # файла нет
    assert svc._skills_hooks() == []


def test_hooks_broken_json(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    (root / "settings.json").write_text("{ это не json ")
    assert svc._skills_hooks() == []  # try/except → пустой список


# ---------------- commands ----------------
def test_commands_parsing(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    cmd = root / "commands"
    cmd.mkdir()
    (cmd / "deploy.md").write_text("---\nname: deploy\ndescription: выкатить прод\n---\nтело")
    # без frontmatter → описание из первой строки
    (cmd / "note.md").write_text("# Быстрая заметка\nостальное")
    cmds = svc._skills_commands()
    by = {c["name"]: c for c in cmds}
    assert by["deploy"]["description"] == "выкатить прод"
    assert by["note"]["description"] == "Быстрая заметка"


def test_commands_missing_dir(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    assert svc._skills_commands() == []


# ---------------- skills ----------------
def test_skills_parsing(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    sk = root / "skills" / "prof"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: prof\ndescription: канбан-доска\n---\n")
    # директория без SKILL.md игнорируется
    (root / "skills" / "empty").mkdir()
    skills = svc._skills_skills()
    assert len(skills) == 1
    assert skills[0] == {"name": "prof", "description": "канбан-доска"}


# ---------------- agents ----------------
def test_agents_parsing(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    ag = root / "agents"
    ag.mkdir()
    (ag / "geo-content.md").write_text(
        "---\nname: geo-content\ndescription: качество контента\n"
        "allowed-tools: Read, Bash\n---\nтело")
    agents = svc._skills_agents()
    assert len(agents) == 1
    assert agents[0]["name"] == "geo-content"
    assert agents[0]["description"] == "качество контента"
    assert agents[0]["tools"] == "Read, Bash"


def test_agents_replace_on_bad_bytes(tmp_path, monkeypatch):
    # битые байты не должны ронять чтение (errors=replace)
    root = _setup(tmp_path, monkeypatch)
    ag = root / "agents"
    ag.mkdir()
    (ag / "bad.md").write_bytes(b"---\nname: bad\ndescription: \xff\xfe bin\n---\n")
    agents = svc._skills_agents()
    assert agents[0]["name"] == "bad"


# ---------------- mcp blurb ----------------
def test_mcp_blurb_known_and_substring():
    assert "Gmail" in svc._mcp_blurb("gmail")
    assert svc._mcp_blurb("my-github-mcp")  # подстрока github → непустое
    assert svc._mcp_blurb("неизвестный-сервер") == ""


# ---------------- discover_skills структура + /api/skills ----------------
def test_discover_skills_shape(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(svc, "discover_mcp", lambda: [
        {"name": "higgsfield", "transport": "http", "target": "https://x",
         "connected": True, "needs_auth": False}])
    d = svc.discover_skills()
    assert set(d) == {"mcp", "hooks", "commands", "skills", "agents", "integrations"}
    assert d["mcp"][0]["status"] == "connected"
    assert d["mcp"][0]["capabilities"]  # higgsfield → есть расшифровка
    # интеграции — всегда непустой чеклист с ключом available
    assert all("available" in i for i in d["integrations"])
    keys = {i["key"] for i in d["integrations"]}
    assert {"vector", "obsidian", "git"} <= keys


def test_api_skills_endpoint(client, tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "CLAUDE_DIR", tmp_path)
    monkeypatch.setattr(svc, "discover_mcp", lambda: [])
    r = client.get("/api/skills")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"mcp", "hooks", "commands", "skills", "agents", "integrations"}
