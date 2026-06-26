"""
Учёт токенов и стоимости по live-сессиям Claude Code (~/.claude/projects/*.jsonl).
Считает реальный расход за 5-часовое окно (как лимит подписки) и за 7 дней,
чтобы понимать, во сколько обходится сессия в деньгах.

Read-only: парсит нативные JSONL-логи, ничего не запускает и не меняет.
"""
from __future__ import annotations

import glob
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECTS_DIR = Path(os.environ.get("PROF_PROJECTS_DIR", Path.home() / ".claude" / "projects"))

# Тарифы $/1M токенов (актуальные для Claude). cache_read ~10× дешевле входа.
# input/output/cache_read/cache_creation.
PRICES = {
    "opus":   {"in": 15.0, "out": 75.0, "cache_read": 1.5,  "cache_write": 18.75},
    "sonnet": {"in": 3.0,  "out": 15.0, "cache_read": 0.3,  "cache_write": 3.75},
    "haiku":  {"in": 0.8,  "out": 4.0,  "cache_read": 0.08, "cache_write": 1.0},
}


def _tier_name(model: str) -> str:
    """Имя тарифного класса по подстроке в имени модели (opus/sonnet/haiku)."""
    m = (model or "").lower()
    if "opus" in m:
        return "opus"
    if "haiku" in m:
        return "haiku"
    return "sonnet"  # дефолт + sonnet


def _price_tier(model: str) -> dict:
    return PRICES[_tier_name(model)]


def _line_cost(usage: dict, model: str) -> float:
    p = _price_tier(model)
    # «Реальная» стоимость работы = только вход + выход (то, что фактически
    # обрабатывается/генерируется в каждом турне). БЕЗ cache_read и cache_creation:
    # оба суммируются по каждому сообщению длинной сессии и многократно раздувают
    # цифру (один и тот же контекст «читается»/«пишется» в кэш в каждом турне).
    # На подписке это и подавно не деньги. Полный API-экв. — в _line_cost_full.
    return (
        (usage.get("input_tokens") or 0) * p["in"]
        + (usage.get("output_tokens") or 0) * p["out"]
    ) / 1_000_000


def _line_cost_full(usage: dict, model: str) -> float:
    """Полный API-эквивалент включая кэш (раздут суммированием, только для справки)."""
    p = _price_tier(model)
    return _line_cost(usage, model) + (
        (usage.get("cache_read_input_tokens") or 0) * p["cache_read"]
        + (usage.get("cache_creation_input_tokens") or 0) * p["cache_write"]
    ) / 1_000_000


def _parse_ts(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


# ============================================================================
#  Обзор отдельных live-сессий Claude Code (для раздела «Сессии»).
#  В отличие от cost_summary (агрегаты по проектам/окнам), здесь — по одной
#  записи на сессию: проект, ветка, last_activity, токены, оценка $.
#  Горячий путь /api/sessions НИКОГДА не читает jsonl целиком — только дельту
#  от последнего просканированного смещения (инкрементально, как readAgentJsonl).
# ============================================================================

WAITING_DIR = Path(os.environ.get("PROF_WAITING_DIR",
                                  Path(__file__).parent / "runs" / "waiting"))
ACTIVE_WINDOW_SEC = 5 * 60       # сессия «active», если файл писался < 5 мин назад
_BRANCH_TTL_SEC = 30             # кэш live git rev-parse на cwd
_TAIL_BYTES = 16 * 1024          # хвост ловит mid-session cd (cwd/branch в каждой строке)

# Инкрементальный кэш по файлу: храним смещение, до которого уже разобрали,
# накопленные агрегаты и метаданные хвоста (cwd/branch/last_ts).
_SESS_CACHE: dict[str, dict] = {}
# Кэш live-ветки по cwd: {cwd: (ts, branch)}.
_BRANCH_CACHE: dict[str, tuple[float, str]] = {}


def _git_branch(cwd: str) -> str:
    """Текущая git-ветка cwd живьём (gitBranch в JSONL устаревает при cd).
    Кэш 30с/cwd, чтобы не дёргать git на каждый запрос."""
    if not cwd:
        return ""
    now = time.time()
    hit = _BRANCH_CACHE.get(cwd)
    if hit and now - hit[0] < _BRANCH_TTL_SEC:
        return hit[1]
    branch = ""
    try:
        import subprocess
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True, timeout=1.5)
        if r.returncode == 0:
            branch = r.stdout.strip()
    except Exception:
        branch = ""
    _BRANCH_CACHE[cwd] = (now, branch)
    return branch


def _tail_meta(path: str, size: int) -> dict:
    """cwd/git_branch из хвоста файла (последняя непустая запись с этими полями).
    Хвост 16КБ ловит mid-session cd. Возвращает {cwd, git_branch}."""
    meta = {"cwd": "", "git_branch": ""}
    try:
        with open(path, "rb") as fh:
            if size > _TAIL_BYTES:
                fh.seek(size - _TAIL_BYTES)
                fh.readline()  # выбрасываем обрезанную первую строку
            chunk = fh.read()
    except OSError:
        return meta
    for raw in reversed(chunk.splitlines()):
        if b'"cwd"' not in raw:
            continue
        try:
            o = json.loads(raw)
        except Exception:
            continue
        if o.get("cwd"):
            meta["cwd"] = o.get("cwd") or ""
            meta["git_branch"] = o.get("gitBranch") or ""
            break
    return meta


def _scan_session(path: str) -> dict | None:
    """Инкрементально разбирает одну сессию. Возвращает агрегат сессии или None.

    TTL-кэш по (size, mtime): если size не вырос — 0 IO на парсинг (только stat
    выше по стеку). Если файл дозаписан — читаем ТОЛЬКО дельту от scanned_size
    и доливаем в накопленные счётчики, не перечитывая старое.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    cached = _SESS_CACHE.get(path)
    if cached and cached["size"] == st.st_size and cached["mtime"] == st.st_mtime:
        return cached["agg"]

    # стартуем либо с нуля, либо продолжаем с прошлого смещения
    if cached and cached["size"] <= st.st_size and cached.get("scanned"):
        agg = dict(cached["agg"])
        start = cached["scanned"]
    else:
        agg = {"in": 0, "out": 0, "cr": 0, "cc": 0, "cost": 0.0,
               "msgs": 0, "last_ts": 0.0}
        start = 0

    try:
        with open(path, errors="replace") as fh:
            fh.seek(start)
            for line in fh:
                # substring fast-reject: json.loads только при наличии маркеров
                if '"usage"' not in line and '"type"' not in line:
                    continue
                if '"usage"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                msg = o.get("message") or {}
                u = msg.get("usage") or {}
                if not u:
                    continue
                model = msg.get("model") or ""
                ts = _parse_ts(o.get("timestamp", ""))
                agg["in"] += u.get("input_tokens") or 0
                agg["out"] += u.get("output_tokens") or 0
                agg["cr"] += u.get("cache_read_input_tokens") or 0
                agg["cc"] += u.get("cache_creation_input_tokens") or 0
                agg["cost"] += _line_cost(u, model)
                agg["msgs"] += 1
                if ts > agg["last_ts"]:
                    agg["last_ts"] = ts
    except OSError:
        return cached["agg"] if cached else None

    _SESS_CACHE[path] = {"size": st.st_size, "mtime": st.st_mtime,
                         "scanned": st.st_size, "agg": agg}
    return agg


def _has_fresh_waiting(session_id: str, now: float) -> bool:
    """Свежий маркер runs/waiting/<sid>.json (агент ждёт ввода — формально жив)."""
    marker = WAITING_DIR / f"{session_id}.json"
    try:
        ts = json.loads(marker.read_text()).get("ts", 0)
    except Exception:
        return False
    return (now - ts) < ACTIVE_WINDOW_SEC


def _slug_name(slug: str) -> str:
    return slug[len("-home-nel"):].lstrip("-") if slug.startswith("-home-nel") else slug


def list_sessions(filter: str = "active") -> list[dict]:
    """Обзор live-сессий Claude Code из ~/.claude/projects (read-only).

    filter='active' — только сессии с mtime < 5 мин ИЛИ свежим waiting-маркером;
    дорогой полный парсинг применяется только к выжившим cheap-probe (stat mtime).
    filter='all' — все сессии (парсятся все, но инкрементально/из кэша).

    Возвращает список словарей, отсортированный по last_activity (свежие сверху).
    """
    now = time.time()
    want_active = filter != "all"
    out = []
    # projects/<slug>/<sessionId>.jsonl — НЕ заходим в подпапки (subagents/*) на
    # горячем пути: glob с фиксированной глубиной, без recursive.
    for path in glob.glob(str(PROJECTS_DIR / "*" / "*.jsonl")):
        try:
            st_mtime = os.path.getmtime(path)
        except OSError:
            continue
        session_id = Path(path).stem
        # cheap-probe: сначала дешёвая проверка active по mtime/waiting-маркеру,
        # дорогой парсинг — только для выживших.
        is_active = (now - st_mtime) < ACTIVE_WINDOW_SEC or _has_fresh_waiting(session_id, now)
        if want_active and not is_active:
            continue
        agg = _scan_session(path)
        if agg is None:
            continue
        slug = Path(path).parent.name
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        meta = _tail_meta(path, size)
        cwd = meta["cwd"]
        # для active резолвим ветку живьём (JSONL gitBranch устаревает при cd)
        branch = _git_branch(cwd) if (is_active and cwd) else meta["git_branch"]
        out.append({
            "session_id": session_id,
            "slug": slug,
            "project": _slug_name(slug),
            "cwd": cwd,
            "git_branch": branch,
            "active": is_active,
            "last_activity": agg["last_ts"] or st_mtime,
            "msg_count": agg["msgs"],
            "total_input": agg["in"],
            "total_output": agg["out"],
            "total_cache_read": agg["cr"],
            "est_cost_usd": round(agg["cost"], 4),
        })
    out.sort(key=lambda s: s["last_activity"], reverse=True)
    return out


# Кэш разобранных событий по файлу (inode/size/mtime) — не перечитываем неизменное.
_FILE_CACHE: dict[str, dict] = {}


def _scan_file(path: str) -> list[dict]:
    """Возвращает список событий {ts, model, in, out, cr, cc, cost} из одного jsonl.
    Кэшируется по (size, mtime): если файл не менялся — берём из кэша."""
    try:
        st = os.stat(path)
    except OSError:
        return []
    key = path
    cached = _FILE_CACHE.get(key)
    if cached and cached["size"] == st.st_size and cached["mtime"] == st.st_mtime:
        return cached["events"]
    events = []
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                # fast-reject: без usage не парсим
                if '"usage"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                msg = o.get("message") or {}
                u = msg.get("usage") or {}
                if not u:
                    continue
                model = msg.get("model") or ""
                ts = _parse_ts(o.get("timestamp", ""))
                events.append({
                    "ts": ts, "model": model,
                    "in": u.get("input_tokens") or 0,
                    "out": u.get("output_tokens") or 0,
                    "cr": u.get("cache_read_input_tokens") or 0,
                    "cc": u.get("cache_creation_input_tokens") or 0,
                    "cost": _line_cost(u, model),
                    "cost_full": _line_cost_full(u, model),
                    "slug": Path(path).parent.name,
                })
    except OSError:
        return []
    _FILE_CACHE[key] = {"size": st.st_size, "mtime": st.st_mtime, "events": events}
    return events


def _all_events(since_ts: float = 0.0) -> list[dict]:
    """Все usage-события из всех сессий новее since_ts.
    Файлы старше окна (mtime < since) пропускаем целиком — 0 IO."""
    out = []
    for path in glob.glob(str(PROJECTS_DIR / "**" / "*.jsonl"), recursive=True):
        try:
            if since_ts and os.path.getmtime(path) < since_ts - 1:
                # последняя запись файла старше окна — внутри тоже всё старое
                continue
        except OSError:
            continue
        for e in _scan_file(path):
            if e["ts"] >= since_ts:
                out.append(e)
    return out


def _agg(events: list[dict]) -> dict:
    tot_in = sum(e["in"] for e in events)
    tot_out = sum(e["out"] for e in events)
    tot_cr = sum(e["cr"] for e in events)
    tot_cc = sum(e["cc"] for e in events)
    cost = sum(e["cost"] for e in events)
    cost_full = sum(e.get("cost_full", e["cost"]) for e in events)
    return {
        "input": tot_in, "output": tot_out,
        "cache_read": tot_cr, "cache_creation": tot_cc,
        "total_tokens": tot_in + tot_out + tot_cr + tot_cc,
        "cost_usd": round(cost, 2),          # реальная работа (без раздутого cache_read)
        "cost_full_usd": round(cost_full, 2),  # полный API-эквивалент (для справки)
        "messages": len(events),
    }


def window_usage(hours: float = 5.0) -> dict:
    """Расход токенов и стоимость за последние `hours` часов (по live-сессиям)."""
    since = time.time() - hours * 3600
    return _agg(_all_events(since))


def cost_summary(days: int = 7) -> dict:
    """Сводка расхода: окно 5ч, 7д, и разбивка по проектам + по дням за период."""
    now = time.time()
    since_5h = now - 5 * 3600
    since_period = now - days * 86400
    events = _all_events(since_period)

    win5 = _agg([e for e in events if e["ts"] >= since_5h])
    period = _agg(events)

    by_project = {}
    by_day = {}
    by_model = {t: {"cost": 0.0, "input": 0, "output": 0, "cache_read": 0, "messages": 0}
                for t in ("opus", "sonnet", "haiku")}
    for e in events:
        slug = e["slug"]
        p = by_project.setdefault(slug, {"cost": 0.0, "input": 0, "output": 0,
                                         "cache_read": 0, "messages": 0})
        p["cost"] += e["cost"]; p["input"] += e["in"]; p["output"] += e["out"]
        p["cache_read"] += e["cr"]; p["messages"] += 1
        day = datetime.fromtimestamp(e["ts"], timezone.utc).strftime("%Y-%m-%d") if e["ts"] else "?"
        dd = by_day.setdefault(day, {"cost": 0.0, "input": 0, "output": 0, "messages": 0})
        dd["cost"] += e["cost"]; dd["input"] += e["in"]; dd["output"] += e["out"]; dd["messages"] += 1
        m = by_model[_tier_name(e["model"])]
        m["cost"] += e["cost"]; m["input"] += e["in"]; m["output"] += e["out"]
        m["cache_read"] += e["cr"]; m["messages"] += 1

    def _name(slug):
        return slug[len("-home-nel"):].lstrip("-") if slug.startswith("-home-nel") else slug
    proj_list = [{"project": _name(s), **{k: (round(v, 2) if k == "cost" else v)
                 for k, v in d.items()}} for s, d in by_project.items()]
    proj_list.sort(key=lambda x: x["cost"], reverse=True)

    return {
        "window_5h": win5,
        "period": period,
        "period_days": days,
        "by_project": proj_list,
        "by_day": {d: {**v, "cost": round(v["cost"], 2)} for d, v in sorted(by_day.items())},
        "by_model": {t: {**v, "cost": round(v["cost"], 2)} for t, v in by_model.items()},
    }


# ============================================================================
#  Дашборд «кто жрёт квоту»: разделение нагрузки на 5h-окно подписки между
#  интерактивными сессиями (наш чат / терминал / IDE) и задачами-коворкингом
#  (claude -p, запущенные prof). Главная метрика тут — НЕ доллары, а ТОКЕНЫ:
#  именно их (в первую очередь cache_read — перечитывание контекста на каждом
#  ходу) ест лимит подписки. Длинная интерактивная сессия раздувает контекст и
#  на каждом ходу прогоняет его заново — отсюда подсказка сделать /compact.
# ============================================================================

# Порог «раздутой» сессии: пик контекста (cache_read+cache_creation в одном
# сообщении) выше — значит каждый ход дорогой, пора /compact.
_BLOATED_CTX_TOKENS = 200_000


def _session_id_of(path: str) -> str:
    return Path(path).stem


def _scan_session_detail(path: str) -> dict | None:
    """Как _scan_session, но дополнительно отдаёт пик контекста (макс. cache_read+
    cache_creation в одном сообщении = размер раздутого контекста) и модель."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    agg = {"in": 0, "out": 0, "cr": 0, "cc": 0, "msgs": 0,
           "last_ts": 0.0, "peak_ctx": 0, "model": ""}
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                msg = o.get("message") or {}
                u = msg.get("usage") or {}
                if not u:
                    continue
                cr = u.get("cache_read_input_tokens") or 0
                cc = u.get("cache_creation_input_tokens") or 0
                agg["in"] += u.get("input_tokens") or 0
                agg["out"] += u.get("output_tokens") or 0
                agg["cr"] += cr
                agg["cc"] += cc
                agg["msgs"] += 1
                agg["peak_ctx"] = max(agg["peak_ctx"], cr + cc)
                if msg.get("model"):
                    agg["model"] = msg["model"]
                ts = _parse_ts(o.get("timestamp", ""))
                if ts > agg["last_ts"]:
                    agg["last_ts"] = ts
    except OSError:
        return None
    return agg


def quota_breakdown(task_session_ids: set[str] | None = None, hours: float = 5.0) -> dict:
    """Кто жрёт квоту 5h-окна: разбивка по сессиям с делением
    интерактив (наш чат/терминал/IDE) vs задачи-коворкинг (claude -p от prof).

    `task_session_ids` — множество session_id карточек (cards.session_dir);
    сессия из этого множества помечается kind='task', иначе 'interactive'.

    Считаем в ТОКЕНАХ (то, что ест лимит). Доллары даём справочно. Сессии с
    активностью внутри окна; top-список отсортирован по суммарным токенам.
    Помечаем 'bloated' сессии (пик контекста > порога) — кандидаты на /compact.
    """
    task_ids = task_session_ids or set()
    since = time.time() - hours * 3600
    sessions = []
    for path in glob.glob(str(PROJECTS_DIR / "*" / "*.jsonl")):
        try:
            if os.path.getmtime(path) < since - 1:
                continue  # файл не трогали в окне — внутри тоже всё старое
        except OSError:
            continue
        d = _scan_session_detail(path)
        if not d or d["last_ts"] < since:
            continue
        sid = _session_id_of(path)
        slug = Path(path).parent.name
        tokens = d["in"] + d["out"] + d["cr"] + d["cc"]
        kind = "task" if sid in task_ids else "interactive"
        sessions.append({
            "session_id": sid,
            "project": _slug_name(slug),
            "kind": kind,
            "model": _tier_name(d["model"]),
            "tokens": tokens,
            "input": d["in"], "output": d["out"],
            "cache_read": d["cr"], "cache_creation": d["cc"],
            "messages": d["msgs"],
            "peak_ctx": d["peak_ctx"],
            "bloated": kind == "interactive" and d["peak_ctx"] > _BLOATED_CTX_TOKENS,
            "last_activity": d["last_ts"],
            "cost_usd": round(
                _line_cost({"input_tokens": d["in"], "output_tokens": d["out"]}, d["model"]), 4),
        })
    sessions.sort(key=lambda s: s["tokens"], reverse=True)

    def _sum(items, key):
        return sum(i[key] for i in items)

    interactive = [s for s in sessions if s["kind"] == "interactive"]
    tasks = [s for s in sessions if s["kind"] == "task"]
    total_tok = _sum(sessions, "tokens") or 1
    inter_tok = _sum(interactive, "tokens")
    task_tok = _sum(tasks, "tokens")

    # самая раздутая интерактивная сессия — кандидат №1 на /compact
    bloated = [s for s in interactive if s["bloated"]]
    bloated.sort(key=lambda s: s["peak_ctx"], reverse=True)
    advice = None
    if bloated:
        top = bloated[0]
        advice = {
            "session_id": top["session_id"],
            "project": top["project"],
            "peak_ctx": top["peak_ctx"],
            "tokens": top["tokens"],
            "message": (
                f"Сессия в «{top['project']}» раздулась до "
                f"{top['peak_ctx'] // 1000}K контекста — каждый ход перечитывает "
                f"его заново. Сделай /compact в этой сессии, чтобы срезать расход квоты."
            ),
        }

    return {
        "hours": hours,
        "totals": {
            "tokens": _sum(sessions, "tokens"),
            "sessions": len(sessions),
            "interactive_tokens": inter_tok,
            "task_tokens": task_tok,
            "interactive_pct": round(inter_tok * 100 / total_tok),
            "task_pct": round(task_tok * 100 / total_tok),
            "interactive_sessions": len(interactive),
            "task_sessions": len(tasks),
            "cost_usd": round(_sum(sessions, "cost_usd"), 2),
        },
        "sessions": sessions[:30],   # топ-30 по токенам
        "advice": advice,
    }
