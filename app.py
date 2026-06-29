"""
prof — личная система: канбан-доски, память, векторный поиск, MCP, uptime,
авто-агент (анализ слабых мест → задачи на подтверждение), git-бэкап, Obsidian.

Запуск: systemctl --user start prof.service  ·  http://192.168.10.32:7777
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

import db
import services as svc
import sessions as sess
import vectors

PROF_VERSION = "1.9.1"

HOME = Path.home()
PROJECTS_DIR = HOME / ".claude" / "projects"
COMPILER = HOME / "claude-memory-compiler"
DAILY_DIR = COMPILER / "daily"
KB_DIR = COMPILER / "knowledge"
PROF = Path(__file__).parent

TODO_RE = re.compile(
    r"(НЕ начато|ЖДЁТ|Осталось|⏳|TODO|FIXME|не сделал|не чинил|кандидат на|Отложено|ОТЛОЖЕНО)",
    re.IGNORECASE)


# ========================= чтение памяти/KB =========================
def _slug_name(slug: str) -> str:
    p = slug[len("-home-nel"):] if slug.startswith("-home-nel") else slug
    return p.lstrip("-") or "(root)"


def _parse_fm(text: str):
    meta, body = {}, text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            block, body = text[3:end], text[end + 4:]
            for line in block.splitlines():
                m = re.match(r"^\s*(\w[\w_-]*):\s*(.*)$", line)
                if m and m.group(2).strip():
                    meta[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return meta, body.strip()


def list_projects():
    out, seen = [], set()
    # индекс проектов из БД для быстрого лукапа
    db_projects = {p["slug"]: p for p in db.list_projects_db()}
    if PROJECTS_DIR.exists():
        for d in sorted(PROJECTS_DIR.iterdir()):
            mem = d / "memory"
            if not mem.is_dir():
                continue
            files = [f for f in mem.glob("*.md") if f.name != "MEMORY.md"]
            if not files:
                continue
            todo = sum(1 for f in files if TODO_RE.search(f.read_text(errors="replace")))
            last = max((f.stat().st_mtime for f in files), default=0)
            db_p = db_projects.get(d.name, {})
            out.append({"slug": d.name, "name": _slug_name(d.name),
                        "memory_count": len(files), "todo_count": todo, "last_activity": last,
                        "description": db_p.get("description", ""),
                        "git_remote": db_p.get("git_remote", ""),
                        "git_branch": db_p.get("git_branch", "")})
            seen.add(d.name)
    # проекты, заведённые вручную (привязка папки) — даже если памяти ещё нет
    for p in db.list_projects_db():
        if p["slug"] in seen:
            continue
        out.append({"slug": p["slug"], "name": p["name"], "memory_count": 0,
                    "todo_count": 0, "last_activity": p["created_at"] or 0,
                    "description": p.get("description", ""),
                    "git_remote": p.get("git_remote", ""),
                    "git_branch": p.get("git_branch", "")})
    out.sort(key=lambda x: x["last_activity"], reverse=True)
    return out


def list_memory(slug):
    mem = PROJECTS_DIR / slug / "memory"
    if not mem.is_dir():
        raise HTTPException(404, "no project")
    items = []
    for f in sorted(mem.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        txt = f.read_text(errors="replace")
        meta, body = _parse_fm(txt)
        items.append({"file": f.name, "name": meta.get("name", f.stem),
                      "description": meta.get("description", ""), "type": meta.get("type", "memory"),
                      "has_todo": bool(TODO_RE.search(txt)), "mtime": f.stat().st_mtime, "body": body})
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def list_todos():
    found = []
    for d in sorted(PROJECTS_DIR.iterdir()):
        mem = d / "memory"
        if not mem.is_dir():
            continue
        for f in mem.glob("*.md"):
            if f.name == "MEMORY.md":
                continue
            for line in f.read_text(errors="replace").splitlines():
                if TODO_RE.search(line) and len(line.strip()) > 12:
                    found.append({"project": _slug_name(d.name), "slug": d.name,
                                  "source": f.name, "text": line.strip().lstrip("-*# ")[:300]})
    return found


def list_daily():
    if not DAILY_DIR.exists():
        return []
    return [{"date": f.stem, "size": f.stat().st_size}
            for f in sorted(DAILY_DIR.glob("*.md"), reverse=True)]


def list_kb():
    out = []
    for sub in ("concepts", "connections"):
        d = KB_DIR / sub
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            txt = f.read_text(errors="replace")
            meta, _ = _parse_fm(txt)
            links = re.findall(r"\[\[([^\]]+)\]\]", txt)
            out.append({"category": sub, "file": f.stem, "title": meta.get("title", f.stem),
                        "links": [l.split("|")[0].split("/")[-1] for l in links]})
    return out


def text_search(qq):
    ql, hits = qq.lower(), []
    for d in PROJECTS_DIR.iterdir():
        mem = d / "memory"
        if not mem.is_dir():
            continue
        for f in mem.glob("*.md"):
            if f.name == "MEMORY.md":
                continue
            txt = f.read_text(errors="replace")
            if ql in txt.lower():
                meta, _ = _parse_fm(txt)
                i = txt.lower().find(ql)
                hits.append({"project": _slug_name(d.name), "slug": d.name, "file": f.name,
                             "name": meta.get("name", f.stem),
                             "snippet": txt[max(0, i - 60):i + 120].replace("\n", " ")})
    return hits[:80]


# ========================= векторная индексация =========================
def reindex_vectors():
    if not vectors.available():
        return {"ok": False, "reason": "vectors unavailable"}
    vectors.init()
    n = 0
    for d in PROJECTS_DIR.iterdir():
        mem = d / "memory"
        if not mem.is_dir():
            continue
        for f in mem.glob("*.md"):
            if f.name == "MEMORY.md":
                continue
            txt = f.read_text(errors="replace")
            meta, body = _parse_fm(txt)
            vectors.index_doc("memory", str(f), meta.get("name", f.stem),
                              meta.get("description", "") + "\n" + body)
            n += 1
    for k in list_kb():
        p = KB_DIR / k["category"] / f"{k['file']}.md"
        vectors.index_doc("kb", str(p), k["title"], p.read_text(errors="replace"))
        n += 1
    return {"ok": True, "indexed": n}


# ========================= авто-агент анализа =========================
ANALYZE_PROMPT = """Ты — технический аудитор. Проанализируй проект «{name}» (рабочая папка — текущая).
Прочитай README, структуру кода и память проекта в ~/.claude/projects/{slug}/memory/.
Найди 3-5 КОНКРЕТНЫХ слабых мест: техдолг, риски, баги, отсутствующие тесты, незакрытые TODO.
Верни СТРОГО JSON-массив объектов вида:
[{{"title":"краткий заголовок задачи","prompt":"что именно сделать агенту"}}]
Только JSON, без markdown-обёртки, без пояснений.{existing}"""

# Порог утилизации 5h-окна, выше которого авто-анализ не запускаем (бюджет-гард).
ANALYZE_UTIL_LIMIT = 85


def parse_suggestions(text: str) -> list:
    """Извлекает JSON-массив задач из ответа агента (валидный/обёрнутый в markdown)."""
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _norm_title(title: str) -> set:
    """Нормализует заголовок в множество значимых слов: lower, без пунктуации.
    Короткие слова (≤2 символов — предлоги/союзы) отбрасываем как шум."""
    words = re.findall(r"\w+", (title or "").lower())
    return {w for w in words if len(w) > 2}


def _is_dup(title: str, existing: list[set]) -> bool:
    """True если title похож на одну из уже существующих карточек.
    Совпадение: вхождение нормализованного заголовка в существующий (или наоборот),
    либо пересечение слов > 70% от меньшего множества."""
    cand = _norm_title(title)
    if not cand:
        return False
    for ex in existing:
        if not ex:
            continue
        if cand <= ex or ex <= cand:
            return True
        overlap = len(cand & ex)
        if overlap / min(len(cand), len(ex)) > 0.7:
            return True
    return False


# Колонки, карточки в которых считаются «уже предложено/в работе» (для дедупа).
# rejected/done исключаем: отклонённое/закрытое можно предлагать заново.
_ACTIVE_COLUMNS = [c for c in db.DEFAULT_COLUMNS if c not in ("rejected", "done")]


def run_analysis(slug: str) -> dict:
    name = _slug_name(slug)
    # бюджет-гард: не жжём токены на анализ, если 5h-лимит почти исчерпан.
    usage = svc.get_usage()
    util = (usage.get("five_hour") or {}).get("util")
    if util is not None and util > ANALYZE_UTIL_LIMIT:
        return {"skipped": True, "reason": "лимит 5h почти исчерпан",
                "five_hour_util": util}

    board = db.ensure_board(name, slug)
    # активные карточки этой доски → нормализованные заголовки для дедупа/промпта
    active = [c for c in db.list_cards(board["id"]) if c["column"] in _ACTIVE_COLUMNS]
    existing_norm = [_norm_title(c["title"]) for c in active]
    existing_titles = [c["title"] for c in active]
    # список уже предложенного в промпт → агент сразу не плодит дубли
    existing_block = ""
    if existing_titles:
        joined = "; ".join(t[:80] for t in existing_titles[:40])
        existing_block = (f"\n\nУже предложено/в работе (НЕ предлагай то, что уже "
                          f"есть в этом списке): {joined}")

    cwd = svc.project_path(slug)
    res = svc.run_agent_once(
        ANALYZE_PROMPT.format(name=name, slug=slug, existing=existing_block), cwd,
        f"analyze_{slug}.out", timeout=600, kind="analyze")
    text = res.get("text", "")
    suggestions = parse_suggestions(text)
    created, skipped_dup = [], 0
    for s in suggestions[:6]:
        if not (isinstance(s, dict) and s.get("title")):
            continue
        title = s["title"][:120]
        if _is_dup(title, existing_norm):
            skipped_dup += 1
            continue
        c = db.add_card(board["id"], title, s.get("prompt", ""),
                        slug, origin="agent", column="proposed")
        created.append(c)
        existing_norm.append(_norm_title(title))  # дедуп и внутри одного прогона
    return {"suggested": len(created), "skipped_dup": skipped_dup,
            "board_id": board["id"], "raw": text[:500] if not created else ""}


# ========================= FastAPI =========================
# интервал авто-бэкапа (сек) и время последнего успешного — для вкладки «Расписание»
BACKUP_INTERVAL = 600
_LAST_BACKUP = {"ts": 0.0}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # стартовая уборка осиротевших git-worktree/веток prof-card-* (рестарт оборвал
    # задачи parallelism=worktree). Сносит только замердженные ветки — незавершённую
    # работу не теряем. Безопасно при любом режиме (нет worktree → no-op).
    try:
        svc.cleanup_orphan_worktrees()
    except Exception:
        pass
    # фоновый reaper: финализирует карточки и реапит зомби (раньше — синхронно в GET)
    svc.start_reaper()
    # фоновый авто-бэкап раз в 10 минут
    def loop():
        while True:
            time.sleep(BACKUP_INTERVAL)
            try:
                svc.git_backup("prof autosave")
                _LAST_BACKUP["ts"] = time.time()
            except Exception:
                pass
    threading.Thread(target=loop, daemon=True).start()
    yield
    # При остановке НЕ хороним running-задачи: с KillMode=process дочерние
    # claude-процессы продолжают работать и допишут .rc. После рестарта
    # refresh_running_cards подхватит их результат по .rc-файлу.
    # (Зомби с реально мёртвым PID отлавливает refresh_running_cards при старте.)


app = FastAPI(title="prof", version=PROF_VERSION, lifespan=lifespan)
db.init_db()


# ========================= аутентификация =========================
def get_token() -> str:
    """Токен из PROF_TOKEN, иначе из settings; при отсутствии — генерируется и сохраняется."""
    t = os.environ.get("PROF_TOKEN") or db.get_setting("auth_token")
    if not t:
        t = secrets.token_urlsafe(24)
        db.set_setting("auth_token", t)
    return t


# методы, которые меняют состояние или запускают агента, требуют токен.
# GET-эндпоинты (чтение памяти/досок/uptime) остаются открытыми, чтобы не ломать поток.
PROTECTED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    if request.method in PROTECTED_METHODS:
        token = get_token()
        auth = request.headers.get("authorization", "")
        provided = auth[7:] if auth.lower().startswith("bearer ") else request.cookies.get("prof_token", "")
        if not secrets.compare_digest(provided, token):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


# ---- память ----
@app.get("/api/projects")
def r_projects(): return list_projects()

@app.get("/api/folders")
def r_folders():
    """Папки в $HOME, ещё не привязанные как проект — для формы «создать проект»."""
    have = {p["slug"] for p in list_projects()}
    out = []
    for d in sorted(HOME.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        slug = svc.path_to_slug(d)
        if slug in have:
            continue
        out.append({"name": d.name, "path": str(d), "slug": slug})
    return out

class ProjectIn(BaseModel):
    path: str

@app.post("/api/projects")
def r_project_create(p: ProjectIn):
    """Привязать папку как проект: считать данные с папки в БД и создать доску."""
    folder = Path(p.path).expanduser()
    if not folder.is_dir():
        raise HTTPException(400, "папка не найдена")
    folder = folder.resolve()
    slug = svc.path_to_slug(folder)
    name = _slug_name(slug)
    desc = svc.scan_project(folder)
    git = svc.scan_project_git(folder)
    db.upsert_project(slug, name, str(folder), desc, git["git_remote"], git["git_branch"])
    board = db.ensure_board(name, slug)
    return {"slug": slug, "name": name, "description": desc,
            "git_remote": git["git_remote"], "git_branch": git["git_branch"],
            "board_id": board["id"]}

@app.post("/api/projects/{slug}/rescan")
def r_project_rescan(slug: str):
    """Пересканировать существующий проект: обновить README, структуру и git-данные."""
    proj = db.get_project(slug)
    if not proj:
        raise HTTPException(404, "проект не найден")
    folder = Path(proj["path"])
    if not folder.is_dir():
        raise HTTPException(400, "папка не найдена")
    desc = svc.scan_project(folder)
    git = svc.scan_project_git(folder)
    db.upsert_project(slug, proj["name"], str(folder), desc, git["git_remote"], git["git_branch"])
    return {"slug": slug, "description": desc,
            "git_remote": git["git_remote"], "git_branch": git["git_branch"]}

@app.post("/api/projects/sync-all")
def r_projects_sync_all():
    """Авто-привязать все папки из ~/.claude/projects: считать git-данные в БД."""
    results = []
    if not PROJECTS_DIR.exists():
        return {"synced": 0, "results": []}
    for d in sorted(PROJECTS_DIR.iterdir()):
        if not d.is_dir():
            continue
        slug = d.name
        # реальная папка проекта: выводим из slug (/-home-nel-xxx → /home/nel/xxx)
        folder = svc.project_path(slug)
        if not folder.is_dir() or folder == HOME:
            results.append({"slug": slug, "ok": False, "error": "папка не найдена"})
            continue
        name = _slug_name(slug)
        desc = svc.scan_project(folder)
        git = svc.scan_project_git(folder)
        db.upsert_project(slug, name, str(folder), desc, git["git_remote"], git["git_branch"])
        db.ensure_board(name, slug)
        results.append({"slug": slug, "name": name, "ok": True,
                        "git_remote": git["git_remote"], "git_branch": git["git_branch"]})
    ok = sum(1 for r in results if r["ok"])
    return {"synced": ok, "total": len(results), "results": results}

@app.delete("/api/projects/{slug}")
def r_project_del(slug: str):
    db.delete_project(slug)
    return {"ok": True}
@app.get("/api/memory/{slug}")
def r_memory(slug: str): return list_memory(slug)
@app.get("/api/todos")
def r_todos(): return list_todos()
@app.get("/api/daily")
def r_daily(): return list_daily()
@app.get("/api/daily/{name}")
def r_daily_one(name: str):
    f = DAILY_DIR / f"{name}.md"
    if not f.exists(): raise HTTPException(404)
    return {"text": f.read_text(errors="replace")}
@app.get("/api/kb")
def r_kb(): return list_kb()
@app.get("/api/kb/{cat}/{name}")
def r_kb_one(cat: str, name: str):
    f = KB_DIR / cat / f"{name}.md"
    if not f.exists(): raise HTTPException(404)
    return {"text": f.read_text(errors="replace")}
@app.get("/api/search")
def r_search(q: str = "", mode: str = "text"):
    if len(q.strip()) < 2: return []
    if mode == "vector":
        return vectors.search(q.strip())
    return text_search(q.strip())
@app.post("/api/reindex")
def r_reindex(): return reindex_vectors()
@app.get("/api/vectors/status")
def r_vec_status(): return {"available": vectors.available(), "count": vectors.count()}
@app.get("/api/usage")
def r_usage():
    # лимиты подписки (5h/7d %) + реальный расход токенов/денег за 5ч-окно
    u = svc.get_usage()
    try:
        u = {**u, "window_5h": sess.window_usage(5)}
    except Exception:
        pass
    return u
@app.get("/api/sessions/cost")
@app.get("/api/cost/sessions")
def r_sessions_cost(days: int = 7):
    return sess.cost_summary(days=days)


@app.get("/api/quota/breakdown")
def r_quota_breakdown(hours: float = 5.0):
    """Кто жрёт квоту 5h-окна: сессии с делением интерактив vs задачи-коворкинг.
    task_session_ids берём из cards.session_dir (jsonl задач), всё прочее —
    интерактивные сессии (наш чат/терминал/IDE)."""
    task_ids = {c["session_dir"] for c in db.list_cards() if c.get("session_dir")}
    out = sess.quota_breakdown(task_session_ids=task_ids, hours=hours)
    # приклеим % утилизации окна из лимитов подписки — чтобы видеть, насколько
    # окно вообще выбрано (квота ≠ токены 1:1, но даёт ориентир «сколько осталось»).
    try:
        u = svc.get_usage()
        fh = u.get("five_hour") or {}
        out["window"] = {"util": fh.get("util"), "left": fh.get("left"),
                         "available": u.get("available", False)}
    except Exception:
        out["window"] = {"available": False}
    return out


def _project_of(card: dict) -> str:
    """Имя проекта карточки (из slug), либо '—'."""
    return _slug_name(card["slug"]) if card.get("slug") else "—"


def build_schedule() -> dict:
    """Единый обзор всего, что в prof работает по времени:
    отложенные карточки (status=scheduled, scheduled_at — задача #40), очередь WIP
    (status=queued), регулярные фоновые процессы с интервалами и временем след.
    запуска, и лимитные окна 5h/7d со временем сброса."""
    cards = db.list_cards()

    # 1) отложенные карточки (#40): scheduled_at — unix-время старта. Reaper зовёт
    #    svc.start_scheduled, когда время наступило и есть слот под WIP-лимитом.
    scheduled = [
        {"id": c["id"], "title": c["title"], "project": _project_of(c),
         "slug": c.get("slug"), "scheduled_at": c.get("scheduled_at")}
        for c in cards if c.get("status") == "scheduled"
    ]
    scheduled.sort(key=lambda x: x["scheduled_at"] or 0)

    # 2) очередь WIP: status=queued (ждут свободного слота, FIFO по created_at)
    queued = sorted(
        ({"id": c["id"], "title": c["title"], "project": _project_of(c),
          "slug": c.get("slug"), "created_at": c.get("created_at")}
         for c in cards if c.get("status") == "queued"),
        key=lambda x: x["created_at"] or 0)

    # 3) регулярные фоновые процессы prof (интервалы заданы в коде)
    last_backup = _LAST_BACKUP["ts"] or None
    periodic = [
        {"name": "git-автобэкап", "interval": BACKUP_INTERVAL,
         "last_run": last_backup,
         "next_run": (last_backup + BACKUP_INTERVAL) if last_backup else None,
         "where": "app.py lifespan loop"},
        {"name": "reaper (финализация карточек + старт очереди)",
         "interval": svc._REAPER_INTERVAL, "last_run": None, "next_run": None,
         "where": "services.start_reaper"},
        {"name": "обновление usage (5h/7d лимиты)", "interval": svc._USAGE_TTL,
         "last_run": svc._usage_load().get("ts") or None, "next_run": None,
         "where": "services.get_usage (кэш TTL)"},
        {"name": "renderSideUsage (обновление бара в UI)", "interval": 30,
         "last_run": None, "next_run": None, "where": "index.html (клиент)"},
    ]
    # авто-анализ слабых мест крона не имеет — только по кнопке /api/analyze/{slug}
    periodic.append(
        {"name": "авто-анализ слабых мест", "interval": None, "last_run": None,
         "next_run": None, "where": "по требованию (/api/analyze/{slug})"})

    # 4) лимитные окна: % утилизации, остаток и время сброса. next_period —
    #    unix-время начала следующего 5h-окна (+пад), к которому удобно привязывать
    #    отложенный старт из #40.
    u = svc.get_usage()
    windows = {"available": u.get("available", False),
               "five_hour": u.get("five_hour"), "seven_day": u.get("seven_day"),
               "next_period": svc.next_period_start()}

    return {"version": PROF_VERSION, "scheduled": scheduled, "queued": queued,
            "periodic": periodic, "windows": windows}


@app.get("/api/schedule")
def r_schedule():
    return build_schedule()
@app.get("/api/sessions")
def r_sessions(filter: str = "active"):
    # обзор live-сессий Claude Code (терминал/IDE/prof). Открытый GET,
    # инвалидация по mtime внутри sessions._scan_session.
    return sess.list_sessions(filter=filter if filter in ("active", "all") else "active")


# ---- WIP-лимит (одновременно бегущие задачи) ----
class WipIn(BaseModel):
    limit: int

@app.get("/api/settings/wip")
def r_wip_get():
    return {"limit": db.get_wip_limit(), "running": svc.count_running()}

@app.post("/api/settings/wip")
def r_wip_set(w: WipIn):
    if w.limit < 1:
        raise HTTPException(400, "лимит должен быть >= 1")
    db.set_setting("wip_limit", str(w.limit))
    # подняли лимит — освободились слоты, дёрнем очередь сразу (не ждём reaper)
    started = svc.start_next_queued()
    return {"limit": db.get_wip_limit(), "running": svc.count_running(), "started": started}


class ParallelismIn(BaseModel):
    mode: str  # project | worktree | off

@app.get("/api/settings/parallelism")
def r_parallelism_get():
    return {"mode": svc.get_parallelism(), "modes": list(svc.PARALLELISM_MODES)}

@app.post("/api/settings/parallelism")
def r_parallelism_set(p: ParallelismIn):
    if p.mode not in svc.PARALLELISM_MODES:
        raise HTTPException(400, f"режим должен быть из {svc.PARALLELISM_MODES}")
    db.set_setting("parallelism", p.mode)
    # сняли лок (off) — могли освободиться проекты, дёрнем очередь сразу
    started = svc.start_next_queued()
    return {"mode": svc.get_parallelism(), "started": started}


# ---- deploy-check: детект «отрапортовал готово, но не выкатил» ----
# Per-project opt-in (settings-ключ deploy_check:<slug>). Включён — задача с rc=0 и
# зелёной валидацией, но БЕЗ нового коммита, уходит в not_deployed, а не в done
# (см. svc._deploy_not_done). Выкл по умолчанию: у проектов, где локальный коммит
# ≠ деплой, давал бы ложные срабатывания.
class DeployCheckIn(BaseModel):
    enabled: bool

@app.get("/api/settings/deploy-check/{slug}")
def r_deploy_check_get(slug: str):
    return {"slug": slug,
            "enabled": db.get_setting(f"deploy_check:{slug}") in ("1", "true", "on")}

@app.post("/api/settings/deploy-check/{slug}")
def r_deploy_check_set(slug: str, d: DeployCheckIn):
    db.set_setting(f"deploy_check:{slug}", "1" if d.enabled else "0")
    return {"slug": slug, "enabled": d.enabled}


# ---- прод-аудит: «запушено в origin, но не задеплоено на сервер» ----
# Команда deploy_remote_cmd:<slug> печатает HEAD прода; фоновый цикл (svc.refresh_prod_lag,
# троттл 5мин, git/ssh — без LLM, токены окна не тратит) сравнивает с origin/<branch> и
# пишет prod_lag:<slug>. lag>0 → прод отстал; lag=-1 → прод на неизвестном коммите.
class ProdCmdIn(BaseModel):
    cmd: str = ""

@app.get("/api/prod-lag")
def r_prod_lag_all():
    out = {}
    for b in db.list_boards():
        slug = b.get("slug")
        if not slug or not db.get_setting(f"deploy_remote_cmd:{slug}"):
            continue
        raw = db.get_setting(f"prod_lag:{slug}")
        out[slug] = json.loads(raw) if raw else None
    return out

@app.post("/api/prod-lag/refresh")
def r_prod_lag_refresh():
    return svc.refresh_prod_lag(force=True)

@app.get("/api/settings/deploy-remote-cmd/{slug}")
def r_deploy_remote_cmd_get(slug: str):
    return {"slug": slug, "cmd": db.get_setting(f"deploy_remote_cmd:{slug}") or ""}

@app.post("/api/settings/deploy-remote-cmd/{slug}")
def r_deploy_remote_cmd_set(slug: str, d: ProdCmdIn):
    if d.cmd.strip():
        db.set_setting(f"deploy_remote_cmd:{slug}", d.cmd.strip())
    else:
        db.set_setting(f"deploy_remote_cmd:{slug}", "")
        db.set_setting(f"prod_lag:{slug}", "")  # очищаем устаревший результат
    return {"slug": slug, "cmd": d.cmd.strip()}


# ---- Telegram-уведомления ----
class TelegramIn(BaseModel):
    token: str = ""
    chat_id: str = ""

@app.get("/api/settings/telegram")
def r_tg_get():
    """Только статус настройки (без раскрытия токена)."""
    return {"configured": svc.tg_configured()}

@app.post("/api/settings/telegram")
def r_tg_set(t: TelegramIn):
    # env имеет приоритет над settings (см. svc._tg_creds); сюда пишем fallback.
    db.set_setting("tg_bot_token", t.token.strip())
    db.set_setting("tg_chat_id", t.chat_id.strip())
    return {"configured": svc.tg_configured()}

@app.get("/api/notify/test")
def r_tg_test():
    """Открытый GET — отправить тестовое сообщение для проверки настройки."""
    if not svc.tg_configured():
        return {"ok": False, "detail": "не настроено (token/chat_id)"}
    ok = svc.notify_telegram("🔔 prof: тестовое уведомление")
    return {"ok": ok}


# ---- доски / карточки (канбан) ----
class BoardIn(BaseModel):
    name: str
    slug: str | None = None

class CardIn(BaseModel):
    board_id: int | None = None  # либо явный id, либо board (slug/name) ниже
    board: str | None = None     # slug проекта или имя доски — ensure_board создаст/найдёт
    title: str
    prompt: str = ""
    slug: str | None = None
    column: str = "proposed"
    origin: str = "user"

class MoveIn(BaseModel):
    column: str
    position: int = 0


@app.get("/api/boards")
def r_boards():
    return {"columns": db.DEFAULT_COLUMNS, "titles": db.COLUMN_TITLES, "boards": db.list_boards()}

@app.post("/api/boards")
def r_board_add(b: BoardIn): return db.ensure_board(b.name, b.slug)

class BoardEdit(BaseModel):
    name: str

@app.patch("/api/boards/{bid}")
def r_board_edit(bid: int, e: BoardEdit):
    b = db.update_board(bid, e.name.strip())
    if not b:
        raise HTTPException(404, "доска не найдена")
    return b

@app.delete("/api/boards/{bid}")
def r_board_del(bid: int): db.delete_board(bid); return {"ok": True}

@app.get("/api/cards")
def r_cards(board_id: int | None = None):
    return db.list_cards(board_id)  # финализацию делает фоновый reaper

@app.get("/api/cards/stats")
def r_cards_stats():
    """Глобальная сводка по ВСЕМ доскам (для сайдбарного бейджа)."""
    cards = db.list_cards()
    return {
        "total": len(cards),
        "running": sum(1 for c in cards if c["status"] == "running"),
        "proposed": sum(1 for c in cards if c["column"] == "proposed"),
        "done": sum(1 for c in cards if c["column"] == "done"),
        "failed": sum(1 for c in cards if c["status"] == "failed"),
    }

@app.get("/api/cards/waiting")
def r_cards_waiting():
    """Карточки, чей агент стоит и ждёт ввода/разрешения (см. services.waiting_cards)."""
    return svc.waiting_cards()

@app.get("/api/cards/cost")
def r_cards_cost():
    """Статистика затрат по выполненным задачам: токены, стоимость, длительность."""
    cards = [c for c in db.list_cards() if c.get("cost_usd") is not None]
    def s(k): return sum(c.get(k) or 0 for c in cards)
    completed = [c for c in db.list_cards() if c["status"] in ("done",)]
    failed = [c for c in db.list_cards() if c["status"] == "failed"]
    durs = [c["duration_ms"] for c in cards if c.get("duration_ms")]
    return {
        "tasks_with_cost": len(cards),
        "completed": len(completed),
        "failed": len(failed),
        "total_cost_usd": round(s("cost_usd"), 4),
        "total_input": s("input_tokens"),
        "total_output": s("output_tokens"),
        "total_cache_read": s("cache_read_tokens"),
        "total_cache_creation": s("cache_creation_tokens"),
        "avg_cost_usd": round(s("cost_usd") / len(cards), 4) if cards else 0,
        "avg_duration_ms": round(sum(durs) / len(durs)) if durs else 0,
        # суммарно по всем задачам: стоимость проверок (агентных ревью)
        "total_review_cost_usd": round(sum(c.get("review_cost_usd") or 0
                                           for c in db.list_cards()), 4),
        # суммарно по всем задачам: стоимость роутинга модели (Haiku-классификатор)
        "total_route_cost_usd": round(sum(c.get("route_cost_usd") or 0
                                          for c in db.list_cards()), 4),
        "by_project": _cost_by_project(db.list_cards()),
        # средняя стоимость задачи по каждому slug — для точной оценки перед запуском
        "by_slug": _avg_by_slug(cards),
        # разбивка prof-задач по модели (колонка cards.model, авто-роутинг).
        # Карты без model (запуски до роутинга) идут в 'unknown'.
        "by_model": _cards_by_model(cards),
    }

def _cards_by_model(cards: list) -> dict:
    """Агрегация затрат prof-задач по тарифному классу модели (opus/sonnet/haiku).

    Карты без проставленной model (старые запуски до роутинга) собираются
    в класс 'unknown' — фронт показывает секцию, только если есть карты с
    реальной моделью.
    """
    agg: dict[str, dict] = {}
    for c in cards:
        tier = sess._tier_name(c["model"]) if c.get("model") else "unknown"
        a = agg.setdefault(tier, {"cost": 0.0, "input": 0, "output": 0, "tasks": 0})
        a["cost"] += c.get("cost_usd") or 0
        a["input"] += c.get("input_tokens") or 0
        a["output"] += c.get("output_tokens") or 0
        a["tasks"] += 1
    return {t: {**v, "cost": round(v["cost"], 4)} for t, v in agg.items()}

def _merge_cost_by_project(cards_proj: list, sess_proj: list) -> list:
    """Мерж разбивки по проектам: карточки (prof-задачи + проверки) и консоль (live-сессии).

    Матчинг по нормализованному имени проекта (поле `project`, оно одинаково
    нормализовано в _cost_by_project и sessions.cost_summary). Проект может быть
    только в карточках, только в консоли или в обоих. Возвращает по проекту:
    задачи $, проверки $, консоль $ (+токены консоли) и итог = сумма всех трёх.
    """
    by_name: dict[str, dict] = {}

    def row(name):
        return by_name.setdefault(name or "—", {
            "project": name or "—",
            "cost_usd": 0.0, "review_cost_usd": 0.0, "tasks": 0,
            "console_cost_usd": 0.0, "console_input": 0, "console_output": 0,
        })

    for p in cards_proj:
        r = row(p["project"])
        r["cost_usd"] += p.get("cost_usd") or 0
        # route-стоимость (классификатор модели) суммируем в графу проверок —
        # обе суть накладные расходы prof поверх самой задачи; отдельной колонки в
        # combined-таблице нет, в итог total попадает корректно.
        r["review_cost_usd"] += (p.get("review_cost_usd") or 0) + (p.get("route_cost_usd") or 0)
        r["tasks"] += p.get("tasks") or 0

    for p in sess_proj:
        r = row(p["project"])
        r["console_cost_usd"] += p.get("cost") or 0
        r["console_input"] += p.get("input") or 0
        r["console_output"] += p.get("output") or 0

    out = []
    for r in by_name.values():
        total = r["cost_usd"] + r["review_cost_usd"] + r["console_cost_usd"]
        out.append({
            "project": r["project"],
            "tasks": r["tasks"],
            "cost_usd": round(r["cost_usd"], 4),
            "review_cost_usd": round(r["review_cost_usd"], 4),
            "console_cost_usd": round(r["console_cost_usd"], 4),
            "console_input": r["console_input"],
            "console_output": r["console_output"],
            "total_usd": round(total, 4),
        })
    out.sort(key=lambda x: x["total_usd"], reverse=True)
    return out


@app.get("/api/cost/combined")
def r_cost_combined(days: int = 7):
    """Объединённая разбивка по проектам: карточки (задачи+проверки) + консоль.

    Консоль = ручная работа в Claude Code по проекту (терминал/IDE, не через prof),
    из live-сессий ~/.claude/projects за `days` дней. Так видно реальный полный
    расход по каждому проекту: задачи $ + проверки $ + консоль $ = итого $.
    """
    cards_proj = _cost_by_project(db.list_cards())
    try:
        sess_proj = sess.cost_summary(days=days).get("by_project", [])
    except Exception:
        sess_proj = []
    return {"days": days, "by_project": _merge_cost_by_project(cards_proj, sess_proj)}


def _avg_by_slug(cards):
    """{slug: {avg_cost_usd, tasks}} — средняя именно этого проекта, не всех."""
    agg = {}
    for c in cards:
        key = c.get("slug") or "—"
        a = agg.setdefault(key, {"cost": 0.0, "tasks": 0})
        a["cost"] += c.get("cost_usd") or 0
        a["tasks"] += 1
    return {k: {"avg_cost_usd": round(a["cost"] / a["tasks"], 4), "tasks": a["tasks"]}
            for k, a in agg.items() if a["tasks"]}

def _cost_by_project(cards):
    agg = {}
    for c in cards:
        # учитываем проект, если есть стоимость задачи ИЛИ проверок ИЛИ роутинга
        if c.get("cost_usd") is None and not c.get("review_cost_usd") and not c.get("route_cost_usd"):
            continue
        key = c.get("slug") or "—"
        a = agg.setdefault(key, {"cost": 0.0, "review": 0.0, "route": 0.0, "input": 0, "output": 0, "tasks": 0})
        a["cost"] += c.get("cost_usd") or 0
        a["review"] += c.get("review_cost_usd") or 0
        a["route"] += c.get("route_cost_usd") or 0
        a["input"] += c.get("input_tokens") or 0
        a["output"] += c.get("output_tokens") or 0
        if c.get("cost_usd") is not None:
            a["tasks"] += 1
    out = []
    for slug, a in agg.items():
        name = slug[len("-home-nel"):].lstrip("-") if slug.startswith("-home-nel") else slug
        out.append({"project": name or "—", "cost_usd": round(a["cost"], 4),
                    "review_cost_usd": round(a["review"], 4),
                    "route_cost_usd": round(a["route"], 4),
                    # итог = задачи + проверки + роутинг (классификатор модели)
                    "total_usd": round(a["cost"] + a["review"] + a["route"], 4),
                    "input": a["input"], "output": a["output"], "tasks": a["tasks"]})
    out.sort(key=lambda x: x["total_usd"], reverse=True)
    return out

def _check_column(column: str):
    if column not in db.DEFAULT_COLUMNS:
        raise HTTPException(400, f"недопустимая колонка: {column!r}")

@app.post("/api/cards")
def r_card_add(c: CardIn):
    _check_column(c.column)
    board_id = c.board_id
    if board_id is None:
        if not c.board:
            raise HTTPException(400, "нужен board_id или board (slug/имя доски)")
        # board вида '-home-nel-…' → доска привязана к slug проекта; иначе имя ручной
        # доски. ensure_board идемпотентен (создаст при отсутствии, иначе найдёт).
        if c.board.startswith("-home-nel"):
            b = db.ensure_board(_slug_name(c.board), c.board)
        else:
            b = db.ensure_board(c.board)
        board_id = b["id"]
    # slug карточки по умолчанию наследует slug доски — для оценки стоимости по проекту
    card_slug = c.slug
    if card_slug is None and c.board and c.board.startswith("-home-nel"):
        card_slug = c.board
    # Авто-запуск (создать+сразу стартовать): дедупим двойной сабмит/ретрай, иначе
    # появятся две одинаковые бегущие задачи. Ручное создание в 'proposed' не дедупим.
    if c.column == "approved":
        dup = db.find_recent_card_dup(board_id, c.title, c.prompt)
        if dup:
            return dup
    return db.add_card(board_id, c.title, c.prompt, card_slug, c.origin, c.column)

@app.post("/api/cards/{cid}/move")
def r_card_move(cid: int, m: MoveIn):
    _check_column(m.column)
    card = db.move_card(cid, m.column, m.position)
    # подтверждение задачи (approved) → запуск агента
    if m.column == "approved" and card["status"] == "idle" and card.get("prompt"):
        # атомарный захват: при двойном approve/перетаскивании стартует только один.
        if db.claim_card_run(cid):
            svc.start_card(card)
    return db.get_card(cid)

class ScheduleIn(BaseModel):
    when: str  # "next_period" | ISO-8601 | unix-секунды (строкой/числом)
    cont: bool = False  # True = при старте продолжить (failed/needs_input), не с нуля
    answer: str | None = None  # ответ/решение агенту (для needs_input), вписать сейчас


def _parse_when(when: str) -> float:
    """when → unix-время старта. 'next_period' → начало следующего 5h-окна (+5мин);
    иначе ISO-8601 или unix-секунды. HTTPException(400) при невалидном/недоступном."""
    if when == "next_period":
        ts = svc.next_period_start()
        if ts is None:
            raise HTTPException(400, "лимиты 5h недоступны — нельзя вычислить начало периода")
        return ts
    s = str(when).strip()
    try:
        return float(s)  # unix-секунды
    except ValueError:
        pass
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, f"непонятное время: {when!r}")
    return dt.timestamp()


@app.post("/api/cards/{cid}/schedule")
def r_card_schedule(cid: int, s: ScheduleIn):
    """Отложенный старт: ставит карточку в колонку approved со status=scheduled и
    scheduled_at. reaper.start_scheduled запустит её, когда время наступит и будет
    свободный слот под WIP-лимитом.

    cont=True (для отложенных failed/needs_input) — при старте ПРОДОЛЖИТЬ с учётом
    прошлого прогона (start_card_continue), не с нуля; result НЕ стираем (там
    прогресс/вопрос). answer — твоё решение/ответ агенту, дописывается в result,
    чтобы continue прокинул его в промпт при старте."""
    card = db.get_card(cid)
    if not card: raise HTTPException(404)
    if not card.get("prompt"): raise HTTPException(400, "у карточки нет задания")
    ts = _parse_when(s.when)
    fields = {"status": "scheduled", "column": "approved", "scheduled_at": ts}
    if s.cont:
        fields["sched_continue"] = 1
        result = card.get("result") or ""
        if s.answer and s.answer.strip():
            result += (f"\n\n=== РЕШЕНИЕ/ОТВЕТ (вписано при откладывании) ===\n"
                       f"{s.answer.strip()}")
        fields["result"] = result  # сохраняем прогресс + решение
    else:
        fields["result"] = ""  # запуск с нуля — чистим
    return db.update_card(cid, **fields)


@app.post("/api/cards/{cid}/unschedule")
def r_card_unschedule(cid: int):
    """Снимает отложенный старт: status=idle, scheduled_at=NULL."""
    card = db.get_card(cid)
    if not card: raise HTTPException(404)
    return db.update_card(cid, status="idle", scheduled_at=None)


@app.post("/api/cards/{cid}/done")
def r_card_done(cid: int):
    """Ручная пометка задачи выполненной → колонка done, статус done."""
    card = db.get_card(cid)
    if not card: raise HTTPException(404)
    return db.update_card(cid, column="done", status="done")

class CardEdit(BaseModel):
    title: str | None = None
    prompt: str | None = None

@app.patch("/api/cards/{cid}")
def r_card_edit(cid: int, e: CardEdit):
    card = db.get_card(cid)
    if not card: raise HTTPException(404)
    fields = {k: v for k, v in {"title": e.title, "prompt": e.prompt}.items() if v is not None}
    return db.update_card(cid, **fields) if fields else card

@app.post("/api/cards/{cid}/run")
def r_card_run(cid: int):
    card = db.get_card(cid)
    if not card: raise HTTPException(404)
    if card["status"] == "running": raise HTTPException(400, "running")
    # атомарный захват закрывает гонку двойного клика/ретрая: проигравший /run
    # вернёт текущее состояние карточки, второй процесс не спавнится.
    if not db.claim_card_run(cid): return db.get_card(cid)
    svc.start_card(card); return db.get_card(cid)

class ContinueIn(BaseModel):
    answer: str | None = None  # ответ пользователя на вопрос агента (needs_input)

@app.post("/api/cards/{cid}/continue")
def r_card_continue(cid: int, body: ContinueIn | None = None):
    """Продолжить недоделанную задачу: запускает агента с тем же заданием +
    контекстом прошлого прогона (что уже сделано), чтобы доделать, а не начать
    с нуля. Если задача ждала ответа (needs_input) — answer прокидывается агенту
    как ответ на его вопрос. Экономит токены и сохраняет прогресс."""
    card = db.get_card(cid)
    if not card: raise HTTPException(404)
    if card["status"] == "running": raise HTTPException(400, "running")
    if not db.claim_card_run(cid): return db.get_card(cid)
    svc.start_card_continue(card, answer=(body.answer if body else None))
    return db.get_card(cid)

@app.post("/api/cards/{cid}/stop")
def r_card_stop(cid: int):
    card = db.get_card(cid)
    if not card: raise HTTPException(404)
    svc.stop_card(card); return db.get_card(cid)

@app.post("/api/cards/{cid}/validate")
def r_card_validate(cid: int):
    """Ручной перезапуск детерминированной валидации (без вызова claude).
    ok → колонка review/passed; не ok → in_progress/failed с пометкой в результате."""
    card = db.get_card(cid)
    if not card: raise HTTPException(404)
    v = svc.validate_card(card)
    if v["ok"]:
        col, status, vstatus = "review", "done", "passed"
        result = (card.get("result") or "").split("\n⚠️ Валидация не прошла:")[0]
    else:
        col, status, vstatus = "in_progress", "failed", "failed"
        result = ((card.get("result") or "").split("\n⚠️ Валидация не прошла:")[0]
                  + "\n⚠️ Валидация не прошла: " + v["summary"])
    db.update_card(cid, column=col, status=status, result=result,
                   validate_status=vstatus, validate_log=v["log"])
    return {**db.get_card(cid), "validate_summary": v["summary"]}

@app.post("/api/cards/{cid}/review-agent")
def r_card_review_agent(cid: int):
    """Агентная проверка «выполнена ли задача»: headless claude читает файлы/тесты
    и выносит вердикт. DONE → колонка done; REWORK → in_progress на доработку."""
    card = db.get_card(cid)
    if not card: raise HTTPException(404)
    r = svc.agent_review(card)
    base = (card.get("result") or "").split("\n\n=== Проверка ревьюера")[0]
    note = f"\n\n=== Проверка ревьюера ({'✓ выполнено' if r['verdict']=='done' else '✗ на доработку'}) ===\n{r['text']}"
    # накапливаем стоимость проверок этой карточки (может проверяться несколько раз)
    rcost = (card.get("review_cost_usd") or 0) + (r.get("cost_usd") or 0)
    verdict_tag = "DONE" if r["verdict"] == "done" else "REWORK"
    verdict_text = f"{verdict_tag}: {r['text']}"
    common = {"validate_status": "passed" if r["verdict"] == "done" else "failed",
              "result": base + note, "review_cost_usd": round(rcost, 4),
              "review_verdict": verdict_text, "review_checked_at": db.now()}
    if r["verdict"] == "done":
        db.update_card(cid, column="done", status="done", **common)
    else:
        db.update_card(cid, column="in_progress", status="failed", **common)
    return {**db.get_card(cid), "verdict": r["verdict"], "review_cost": r.get("cost_usd")}

@app.get("/api/cards/{cid}/stream")
async def r_card_stream(cid: int):
    """SSE live-прогресс headless-агента: раз в 1.5с шлёт дельту read_progress
    (last_text последнего assistant-сообщения + бегущий cost/токены), завершает
    поток когда появился .rc (задача финализирована). GET → открытый эндпоинт."""
    rc_f = svc.RUNS / f"card_{cid}.rc"

    async def gen():
        offset = 0
        last_text, cost = "", None
        in_tok = out_tok = None
        last_beat = time.time()
        while True:
            p = await asyncio.to_thread(svc.read_progress, cid, offset)
            offset = p["offset"]
            # дельта без assistant-события не сбрасывает накопленный прогресс
            if p["last_text"]:
                last_text = p["last_text"]
            if p["running_cost"] is not None:
                cost = p["running_cost"]
            if p["input_tokens"] is not None:
                in_tok = p["input_tokens"]
            if p["output_tokens"] is not None:
                out_tok = p["output_tokens"]
            payload = {"last_text": last_text, "running_cost": cost,
                       "input_tokens": in_tok, "output_tokens": out_tok,
                       "events": len(p["events"])}
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            if rc_f.exists():
                yield "data: {\"done\": true}\n\n"
                return
            now = time.time()
            if now - last_beat >= 30:
                last_beat = now
                yield ":\n\n"  # heartbeat-комментарий, держит соединение живым
            await asyncio.sleep(1.5)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})

@app.delete("/api/cards/{cid}")
def r_card_del(cid: int): db.delete_card(cid); return {"ok": True}


# ---- авто-анализ ----
@app.post("/api/analyze/{slug}")
def r_analyze(slug: str): return run_analysis(slug)


# ---- сервисы / uptime ----
class ServiceIn(BaseModel):
    name: str
    url: str
    slug: str | None = None

class ServiceEdit(BaseModel):
    name: str | None = None
    url: str | None = None
    slug: str | None = None

# ---- бейджи бокового меню (фоновый опрос) ----
_MENU_CACHE = {"ts": 0.0, "data": None}
_MENU_TTL = 60.0

@app.get("/api/menu-counts")
def r_menu_counts():
    now = time.time()
    if _MENU_CACHE["data"] is None or now - _MENU_CACHE["ts"] > _MENU_TTL:
        services = db.list_services()
        _MENU_CACHE["data"] = {
            "projects": len(list_projects()),
            "todos": len(list_todos()),
            "services_ok": sum(1 for s in services
                               if s["last_status"] and s["last_status"] < 400),
            "services_total": len(services),
            "mcp": len(db.list_mcp()),
        }
        _MENU_CACHE["ts"] = now
    return _MENU_CACHE["data"]


@app.get("/api/services")
def r_services(): return db.list_services()
@app.post("/api/services")
def r_service_add(s: ServiceIn): return db.add_service(s.name, s.url, s.slug)
@app.patch("/api/services/{sid}")
def r_service_edit(sid: int, e: ServiceEdit):
    s = next((x for x in db.list_services() if x["id"] == sid), None)
    if not s: raise HTTPException(404)
    fields = e.model_dump(exclude_unset=True)
    if fields: db.update_service(sid, **fields)
    return next((x for x in db.list_services() if x["id"] == sid), s)
@app.delete("/api/services/{sid}")
def r_service_del(sid: int): db.delete_service(sid); return {"ok": True}
@app.post("/api/services/ping")
def r_ping_all(): return svc.ping_all_services()
@app.post("/api/services/{sid}/ping")
def r_ping(sid: int):
    s = next((x for x in db.list_services() if x["id"] == sid), None)
    if not s: raise HTTPException(404)
    return svc.ping_service(s)


# ---- MCP ----
@app.get("/api/mcp")
def r_mcp(): return {"discovered": svc.discover_mcp(), "saved": db.list_mcp()}


# ---- навыки (обзор подключённого) ----
@app.get("/api/skills")
def r_skills(): return svc.discover_skills()


# ---- git ----
class RemoteIn(BaseModel):
    url: str

@app.get("/api/git/log")
def r_git_log(): return svc.git_log()
@app.post("/api/git/backup")
def r_git_backup(): return svc.git_backup()
@app.post("/api/git/remote")
def r_git_remote(r: RemoteIn): return svc.git_set_remote(r.url)


# ---- безопасный рестарт (защита от обрыва активных задач) ----
@app.get("/api/restart/check")
def r_restart_check():
    svc.refresh_running_cards()
    running = [{"id": c["id"], "title": c["title"]}
               for c in db.list_cards() if c["status"] == "running"]
    return {"can_restart": not running, "running": running}

class RestartIn(BaseModel):
    force: bool = False

@app.post("/api/restart")
def r_restart(r: RestartIn):
    svc.refresh_running_cards()
    running = [c for c in db.list_cards() if c["status"] == "running"]
    if running and not r.force:
        raise HTTPException(409, f"{len(running)} активных задач — рестарт заблокирован")
    # бэкап перед рестартом + грейсфул-пометка активных задач
    try:
        svc.git_backup("prof autosave (перед рестартом)")
    except Exception:
        pass
    for c in running:
        db.update_card(c["id"], status="interrupted", column="approved",
                       result="⚠️ Прервано принудительным рестартом. Запусти заново.",
                       finished_at=db.now())
    # systemd с Restart=always поднимет сервис заново после выхода
    import threading, os as _os, signal as _sig
    threading.Timer(0.5, lambda: _os.kill(_os.getpid(), _sig.SIGTERM)).start()
    return {"restarting": True, "interrupted": len(running)}


@app.post("/api/apply-code")
def r_apply_code():
    """Применить свежий код на диске БЕЗ полного рестарта: trigger-файл, за которым
    следит uvicorn-reload (watchfiles), пересоздаёт worker. claude-задачи через
    start_new_session переживают (порт у супервизора). Возвращает кол-во активных
    задач — фронт предупреждает, что reaper-поток на миг перезапустится."""
    svc.refresh_running_cards()
    running = sum(1 for c in db.list_cards() if c["status"] == "running")
    trigger = svc.RUNS / ".reload-trigger"
    try:
        trigger.write_text(str(db.now()))  # mtime меняется → watchfiles ловит reload
    except OSError as e:
        raise HTTPException(500, f"не удалось тронуть trigger: {e}")
    return {"applied": True, "running": running}


# ---- obsidian ----
class VaultIn(BaseModel):
    path: str

@app.post("/api/obsidian/sync")
def r_obsidian_sync(): return svc.sync_to_obsidian()
@app.post("/api/obsidian/vault")
def r_obsidian_vault(v: VaultIn):
    db.set_setting("obsidian_vault", v.path); return {"ok": True}
@app.get("/api/obsidian/vault")
def r_obsidian_get(): return {"vault": str(svc.obsidian_vault())}


@app.get("/", response_class=HTMLResponse)
def index():
    html = (PROF / "index.html").read_text(encoding="utf-8").replace("__PROF_TOKEN__", get_token())
    resp = HTMLResponse(html)
    resp.set_cookie("prof_token", get_token(), httponly=False, samesite="strict")
    # всегда отдавать свежий HTML — иначе браузер кэширует старую версию страницы
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("PROF_HOST", "127.0.0.1")
    port = int(os.environ.get("PROF_PORT", "7777"))
    # Управляемый reload: следим ТОЛЬКО за флаг-файлом runs/.reload-trigger, а НЕ
    # за *.py. Иначе uvicorn пересоздавал бы worker при каждой правке services.py/
    # app.py агентом-коворкером посреди задачи → гонка reaper'а («дёрганье»).
    # Применить свежий код безопасно: `touch runs/.reload-trigger` (когда нет
    # активных задач). PROF_RELOAD=0 — выключить (прямой запуск/тесты).
    reload_on = os.environ.get("PROF_RELOAD", "1") != "0"
    trigger = PROF / "runs" / ".reload-trigger"
    if reload_on and not trigger.exists():
        trigger.write_text("")  # include-путь должен существовать до старта watcher
    uvicorn.run("app:app", host=host, port=port,
                reload=reload_on,
                reload_dirs=[str(PROF / "runs")],
                reload_includes=[".reload-trigger"],
                reload_excludes=["*.py", "*.out", "*.rc", "*.json", "*.log", "*.db"])
