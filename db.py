"""SQLite-слой prof: канбан-доски, задачи, сервисы, MCP, аудит, настройки."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "prof.db"

# Колонки канбана по умолчанию. Поток: агент кладёт в proposed → ты approve/reject.
DEFAULT_COLUMNS = ["proposed", "approved", "in_progress", "review", "done", "rejected"]
COLUMN_TITLES = {
    "proposed": "Предложено",
    "approved": "Подтверждено",
    "in_progress": "В работе",
    "review": "Проверка",
    "done": "Готово",
    "rejected": "Отклонено",
}


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init_db() -> None:
    c = _conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS boards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT,                 -- привязка к проекту (рабочая папка)
            created_at REAL
        );
        CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            board_id INTEGER REFERENCES boards(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            prompt TEXT,               -- задание для агента
            column TEXT DEFAULT 'proposed',
            position INTEGER DEFAULT 0,
            origin TEXT DEFAULT 'user',-- user | agent | uptime | audit
            slug TEXT,                 -- проект/папка запуска
            status TEXT DEFAULT 'idle',-- idle|running|done|failed|stopped
            pid INTEGER,
            result TEXT DEFAULT '',
            return_code INTEGER,
            created_at REAL,
            started_at REAL,
            finished_at REAL
        );
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            slug TEXT,                 -- проект, к которому относится
            last_status INTEGER,       -- HTTP-код последней проверки
            last_ms INTEGER,
            last_checked REAL,
            created_at REAL
        );
        CREATE TABLE IF NOT EXISTS mcp_servers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            command TEXT,              -- для stdio: бинарь
            args TEXT,                 -- JSON-массив
            url TEXT,                  -- для http/sse
            transport TEXT DEFAULT 'stdio',
            enabled INTEGER DEFAULT 1,
            created_at REAL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS projects (
            slug TEXT PRIMARY KEY,      -- кодированный путь рабочей папки (как ~/.claude/projects)
            name TEXT NOT NULL,
            path TEXT NOT NULL,         -- абсолютный путь к рабочей папке
            description TEXT DEFAULT '',-- краткое описание из README/структуры при привязке
            created_at REAL
        );
        """
    )
    # миграции: учёт токенов/стоимости по карточке (claude -p --output-format json)
    for col, decl in [
        ("cost_usd", "REAL"),
        ("input_tokens", "INTEGER"),
        ("output_tokens", "INTEGER"),
        ("cache_read_tokens", "INTEGER"),
        ("cache_creation_tokens", "INTEGER"),
        ("duration_ms", "INTEGER"),
        ("num_turns", "INTEGER"),
        ("session_dir", "TEXT"),  # session_id запущенного claude (для матчинга маркеров «ждёт ввода»)
        ("validate_status", "TEXT"),  # passed|failed — детерминированная проверка результата
        ("validate_log", "TEXT"),     # хвост вывода тестов/линта/git для модалки
        ("test_baseline", "INTEGER"), # сколько тестов падало ДО запуска (для дельты)
        ("review_cost_usd", "REAL"),  # суммарная стоимость агентных проверок этой карточки
        ("scheduled_at", "REAL"),     # отложенный старт (unix): status=scheduled до наступления
        ("model", "TEXT"),            # имя модели, на которой бежал запуск (авто-роутинг по типу)
        ("sched_continue", "INTEGER"),# 1 = при отложенном старте ПРОДОЛЖИТЬ (continue), а не с нуля
        ("worktree_path", "TEXT"),    # путь git-worktree задачи (режим parallelism=worktree); None в др. режимах
        ("merge_branch", "TEXT"),     # ветка проекта, в которую мержить результат (HEAD на старте worktree)
        ("head_at_start", "TEXT"),    # git HEAD проекта на старте задачи (для детекта «не выкачено»: появился ли коммит)
        ("review_verdict", "TEXT"),   # результат авто-ревью: DONE | REWORK + обоснование
        ("review_checked_at", "REAL"), # unix-время последнего авто-ревью
        ("route_cost_usd", "REAL"),   # стоимость Haiku-классификатора модели (route), отдельно от ревью
    ]:
        try:
            c.execute(f"ALTER TABLE cards ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass
    # миграции: git-данные проекта (remote URL, ветка по умолчанию)
    for col, decl in [
        ("git_remote", "TEXT"),
        ("git_branch", "TEXT"),
    ]:
        try:
            c.execute(f"ALTER TABLE projects ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass
    # миграция: уникальность доски по slug (проекту). NULL-slug (ручные доски) не
    # участвуют в ограничении — у них уникальность по name на уровне ensure_board.
    c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_boards_slug "
        "ON boards(slug) WHERE slug IS NOT NULL"
    )
    c.commit()
    c.close()


# ---------- helpers ----------
def now() -> float:
    return time.time()


def _row(r) -> dict:
    return dict(r) if r else None


def q(sql: str, args=(), one=False):
    c = _conn()
    cur = c.execute(sql, args)
    rows = cur.fetchall()
    c.commit()
    c.close()
    if one:
        return _row(rows[0]) if rows else None
    return [dict(r) for r in rows]


def execute(sql: str, args=()) -> int:
    c = _conn()
    cur = c.execute(sql, args)
    c.commit()
    rid = cur.lastrowid
    c.close()
    return rid


# ---------- boards ----------
def list_boards() -> list[dict]:
    return q("SELECT * FROM boards ORDER BY id")


def ensure_board(name: str, slug: str = None) -> dict:
    # Доска идентифицируется по slug (проекту); разные slug с одинаковым name
    # (например '(root)') остаются раздельными досками. Ручные доски без slug
    # ищутся по name (старое поведение).
    if slug is not None:
        b = q("SELECT * FROM boards WHERE slug=?", (slug,), one=True)
    else:
        b = q("SELECT * FROM boards WHERE slug IS NULL AND name=?", (name,), one=True)
    if b:
        return b
    bid = execute("INSERT INTO boards(name,slug,created_at) VALUES(?,?,?)", (name, slug, now()))
    return q("SELECT * FROM boards WHERE id=?", (bid,), one=True)


def delete_board(bid: int) -> None:
    execute("DELETE FROM boards WHERE id=?", (bid,))


def update_board(bid: int, name: str) -> dict | None:
    execute("UPDATE boards SET name=? WHERE id=?", (name, bid))
    return q("SELECT * FROM boards WHERE id=?", (bid,), one=True)


def get_board_slug(bid: int) -> str | None:
    """Slug доски по board_id; None если доска не найдена."""
    b = q("SELECT slug FROM boards WHERE id=?", (bid,), one=True)
    return b["slug"] if b else None


# ---------- projects ----------
def list_projects_db() -> list[dict]:
    return q("SELECT * FROM projects ORDER BY created_at DESC")


def get_project(slug: str) -> dict:
    return q("SELECT * FROM projects WHERE slug=?", (slug,), one=True)


def upsert_project(slug: str, name: str, path: str, description: str = "",
                   git_remote: str = "", git_branch: str = "") -> dict:
    execute(
        "INSERT INTO projects(slug,name,path,description,git_remote,git_branch,created_at) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(slug) DO UPDATE SET name=excluded.name, path=excluded.path, "
        "description=excluded.description, git_remote=excluded.git_remote, git_branch=excluded.git_branch",
        (slug, name, path, description, git_remote or "", git_branch or "", now()),
    )
    return get_project(slug)


def delete_project(slug: str) -> None:
    execute("DELETE FROM projects WHERE slug=?", (slug,))


# ---------- cards ----------
# Сортировка внутри колонки: по НЕДАВНЕЙ АКТИВНОСТИ (свежие сверху).
# Ключ активности = max(finished_at, started_at, created_at). position идёт
# первым — на случай будущего ручного drag&drop-порядка (сейчас у всех 0,
# поэтому фактически решает время). DESC = недавнее сверху.
_CARD_ORDER = ("ORDER BY column, position, "
               "COALESCE(finished_at, started_at, created_at, 0) DESC, id DESC")


def list_cards(board_id: int = None) -> list[dict]:
    if board_id:
        return q(f"SELECT * FROM cards WHERE board_id=? {_CARD_ORDER}", (board_id,))
    return q(f"SELECT * FROM cards {_CARD_ORDER}")


def get_card(cid: int) -> dict:
    return q("SELECT * FROM cards WHERE id=?", (cid,), one=True)


def add_card(board_id, title, prompt, slug, origin="user", column="proposed") -> dict:
    cid = execute(
        "INSERT INTO cards(board_id,title,prompt,column,origin,slug,created_at) VALUES(?,?,?,?,?,?,?)",
        (board_id, title, prompt, column, origin, slug, now()),
    )
    return get_card(cid)


def move_card(cid: int, column: str, position: int = 0) -> dict:
    execute("UPDATE cards SET column=?, position=? WHERE id=?", (column, position, cid))
    return get_card(cid)


CARD_FIELDS = {
    "board_id", "title", "prompt", "column", "position", "origin", "slug",
    "status", "pid", "result", "return_code", "created_at", "started_at",
    "finished_at", "cost_usd", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_creation_tokens", "duration_ms", "num_turns",
    "session_dir", "validate_status", "validate_log", "test_baseline",
    "review_cost_usd", "scheduled_at", "model", "sched_continue", "worktree_path",
    "merge_branch", "head_at_start", "review_verdict", "review_checked_at",
    "route_cost_usd",
}

def update_card(cid: int, **fields) -> dict:
    if fields:
        bad = set(fields) - CARD_FIELDS
        if bad:
            raise ValueError(f"недопустимые поля cards: {bad}")
        sets = ",".join(f"{k}=?" for k in fields)
        execute(f"UPDATE cards SET {sets} WHERE id=?", (*fields.values(), cid))
    return get_card(cid)


def find_recent_card_dup(board_id, title, prompt, window=10.0) -> dict | None:
    """Недавняя карточка-близнец на той же доске (тот же title+prompt за `window`с).

    Нужна для дедупа авто-запуска: двойной сабмит/ретрай шлёт два POST /api/cards
    почти одновременно, и без этого создались бы две одинаковые задачи, которые
    обе уйдут в работу. Возвращает существующую карточку либо None.
    """
    return q(
        "SELECT * FROM cards WHERE board_id=? AND title=? AND prompt=? AND created_at>=? "
        "ORDER BY id DESC LIMIT 1",
        (board_id, title, prompt, now() - window),
        one=True,
    )


def claim_card_run(cid: int) -> bool:
    """Атомарно «захватывает» карточку под запуск, закрывая гонку двойного старта.

    Два почти одновременных POST /run (двойной клик, ретрай) оба проходили
    проверку `status != 'running'` в обработчике и оба спавнили процесс claude —
    отсюда дубли. Здесь один UPDATE ... WHERE атомарен в SQLite: помечает карточку
    `starting`, только если она ещё не запускается/в очереди. rowcount==1 у
    победителя гонки, 0 — у проигравшего (его /run просто отбрасываем).
    `starting` — короткоживущее промежуточное состояние: start_card сразу же
    переводит карточку в running (или queued при WIP-лимите).
    """
    c = _conn()
    cur = c.execute(
        "UPDATE cards SET status='starting' WHERE id=? "
        "AND status NOT IN ('running','starting','queued')",
        (cid,),
    )
    c.commit()
    won = cur.rowcount == 1
    c.close()
    return won


def claim_spawn(cid: int) -> bool:
    """Атомарно столбит карточку под спавн, переводя её СРАЗУ в 'running'.

    Финальный барьер от дублей: внешние /run-/continue-/move-пути зовут
    claim_card_run, НО внутренние пути reaper'а (start_next_queued / start_scheduled
    / resume_paused) спавнят через _spawn_card напрямую, а старый _spawn_card
    выставлял status='running' только В КОНЦЕ (после медленных worktree/baseline/git
    — секунды). В этом окне следующий тик reaper'а видел карточку ещё не-running и
    запускал ВТОРОЙ процесс в ту же папку (инцидент #77: 537КБ .out, дубль-запись).

    Решение: помечаем 'running' АТОМАРНО в начале _spawn_card, до медленных шагов.
    UPDATE...WHERE status!='running' в SQLite атомарен и идемпотентен по значению:
    победитель переводит из не-running в running (rowcount==1), а если карточка УЖЕ
    running (первый _spawn_card успел) — WHERE не совпадает, rowcount==0, второй
    вызов проигрывает и выходит без процесса. pid дозапишет _spawn_card после Popen.
    """
    c = _conn()
    cur = c.execute(
        "UPDATE cards SET status='running' WHERE id=? AND status != 'running'",
        (cid,),
    )
    c.commit()
    won = cur.rowcount == 1
    c.close()
    return won


def delete_card(cid: int) -> None:
    execute("DELETE FROM cards WHERE id=?", (cid,))


# ---------- services ----------
def list_services() -> list[dict]:
    return q("SELECT * FROM services ORDER BY id")


def add_service(name, url, slug=None) -> dict:
    sid = execute("INSERT INTO services(name,url,slug,created_at) VALUES(?,?,?,?)", (name, url, slug, now()))
    return q("SELECT * FROM services WHERE id=?", (sid,), one=True)


SERVICE_FIELDS = {
    "name", "url", "slug", "last_status", "last_ms", "last_checked", "created_at",
}

def update_service(sid, **f):
    if f:
        bad = set(f) - SERVICE_FIELDS
        if bad:
            raise ValueError(f"недопустимые поля services: {bad}")
        sets = ",".join(f"{k}=?" for k in f)
        execute(f"UPDATE services SET {sets} WHERE id=?", (*f.values(), sid))


def delete_service(sid):
    execute("DELETE FROM services WHERE id=?", (sid,))


# ---------- mcp ----------
def list_mcp() -> list[dict]:
    return q("SELECT * FROM mcp_servers ORDER BY id")


def add_mcp(name, command=None, args=None, url=None, transport="stdio") -> dict:
    mid = execute(
        "INSERT INTO mcp_servers(name,command,args,url,transport,created_at) VALUES(?,?,?,?,?,?)",
        (name, command, json.dumps(args or []), url, transport, now()),
    )
    return q("SELECT * FROM mcp_servers WHERE id=?", (mid,), one=True)


def toggle_mcp(mid, enabled):
    execute("UPDATE mcp_servers SET enabled=? WHERE id=?", (1 if enabled else 0, mid))


def delete_mcp(mid):
    execute("DELETE FROM mcp_servers WHERE id=?", (mid,))


# ---------- settings ----------
def get_setting(key, default=None):
    r = q("SELECT value FROM settings WHERE key=?", (key,), one=True)
    return r["value"] if r else default


def set_setting(key, value):
    execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=?", (key, value, value))


# WIP-лимит: сколько задач может бежать одновременно (lean-канбан, 3-5 по Nimbalyst/
# Eric Tech). Без лимита approve→сразу запуск плодит заброшенные параллельные прогоны,
# жгущие токены и квоту 5h/7d. Дефолт 3.
WIP_LIMIT_DEFAULT = 3


def get_wip_limit() -> int:
    try:
        v = int(get_setting("wip_limit", WIP_LIMIT_DEFAULT))
        return v if v >= 1 else WIP_LIMIT_DEFAULT
    except (TypeError, ValueError):
        return WIP_LIMIT_DEFAULT
