"""
Сервисные функции prof: uptime-пинг, MCP-обнаружение, headless-агент,
авто-анализ слабых мест, git-бэкап, Obsidian-синк.
Дефолты авто-паузы: pause_turns=55 ходов, pause_cache_read_m=5 млн токенов cache_read; wip_throttle_util=60% утилизации 5h-окна (сверх — новые задачи не стартуют).
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
import urllib.request
import uuid
from pathlib import Path

import db

HOME = Path.home()
PROF = Path(__file__).parent
RUNS = PROF / "runs"
RUNS.mkdir(exist_ok=True)
WAITING = RUNS / "waiting"  # маркеры «агент ждёт ввода» (пишутся хуком prof_waiting.sh)
WAITING.mkdir(exist_ok=True)

# хвост вывода claude (JSON с usage/cost — в конце файла), не читаем гигантские логи
_RESULT_TAIL_BYTES = 256 * 1024


def _read_tail(path: Path, nbytes: int = _RESULT_TAIL_BYTES) -> str:
    """Читает последние nbytes файла (вывод claude --output-format json пишет
    итоговый JSON последним, так что хвоста достаточно для парсинга)."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > nbytes:
                f.seek(size - nbytes)
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


# ====================== Telegram-уведомления ======================
# env (PROF_TG_BOT_TOKEN/PROF_TG_CHAT_ID) имеет приоритет над settings — как в
# основном проекте (MVP_TG_BOT_TOKEN): пусто → тихий no-op, ошибки глотаются.
# Дедуп: in-memory set ключей (cid, event), чтобы reaper (раз в 3с) и polling
# waiting_cards не слали один и тот же переход повторно.
_TG_SENT: set[tuple] = set()


def _tg_creds() -> tuple[str, str]:
    """(token, chat_id) из env, иначе из settings. Пустые строки если не настроено."""
    token = os.environ.get("PROF_TG_BOT_TOKEN") or db.get_setting("tg_bot_token") or ""
    chat = os.environ.get("PROF_TG_CHAT_ID") or db.get_setting("tg_chat_id") or ""
    return token, chat


def tg_configured() -> bool:
    token, chat = _tg_creds()
    return bool(token and chat)


def notify_telegram(text: str, dedup_key: tuple | None = None) -> bool:
    """Шлёт text в Telegram. Не настроено → тихий no-op. Ошибки глотаются.
    dedup_key (cid, event) → повтор с тем же ключом не отправляется. Возвращает
    True, если сообщение реально ушло (для тестов/теста настройки)."""
    if dedup_key is not None and dedup_key in _TG_SENT:
        return False
    token, chat = _tg_creds()
    if not (token and chat):
        return False
    try:
        body = json.dumps({"chat_id": chat, "text": text}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=body, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8):
            pass
    except Exception:
        return False
    if dedup_key is not None:
        _TG_SENT.add(dedup_key)
    return True


# ====================== Anthropic usage (лимиты 5h/7d) ======================
_USAGE_FILE = PROF / "runs" / ".usage_cache.json"  # переживает рестарт сервиса
_USAGE_TTL = 60  # дёргаем usage-API не чаще раза в минуту. Было 300с, но раздутая
                 # задача успевала выжечь окно за время жизни кэша (инцидент: 5h-окно
                 # сгорало раньше, чем кэш util обновлялся → авто-пауза не срабатывала).


def _usage_load() -> dict:
    try:
        return json.loads(_USAGE_FILE.read_text())
    except Exception:
        return {"ts": 0, "good": None}


def _usage_save(ts: float, good: dict) -> None:
    try:
        _USAGE_FILE.write_text(json.dumps({"ts": ts, "good": good}))
    except Exception:
        pass


def _fmt_left(reset_iso: str) -> str:
    """ISO-время сброса → остаток вида '3h 9m' или '2d 19h'."""
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(reset_iso.replace("Z", "+00:00"))
        secs = int((dt - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return ""
    if secs <= 0:
        return "now"
    if secs >= 86400:
        return f"{secs // 86400}d {(secs % 86400) // 3600}h"
    return f"{secs // 3600}h {(secs % 3600) // 60}m"


def get_usage() -> dict:
    """Лимиты подписки Claude (5h/7d) через oauth/usage.
    Кэш на диске (переживает рестарт); при ошибке (429 и пр.) отдаёт последний
    успешный результат (stale), чтобы бар не пропадал из-за rate-limit."""
    now = time.time()
    cache = _usage_load()
    good = cache.get("good")
    if good and now - cache.get("ts", 0) < _USAGE_TTL:
        return good
    creds = HOME / ".claude" / ".credentials.json"
    try:
        c = json.loads(creds.read_text())
        token = (c.get("claudeAiOauth", {}) or {}).get("accessToken") or c.get("accessToken")
        if not token:
            return good or {"available": False}
        req = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={"Authorization": f"Bearer {token}",
                     "anthropic-beta": "oauth-2025-04-20"})
        with urllib.request.urlopen(req, timeout=8) as r:
            j = json.loads(r.read())
        fh, sd = j.get("five_hour", {}), j.get("seven_day", {})
        data = {"available": True,
                "five_hour": {"util": round(fh.get("utilization", 0)),
                              "left": _fmt_left(fh.get("resets_at", "")),
                              "resets_at": fh.get("resets_at", "")},
                "seven_day": {"util": round(sd.get("utilization", 0)),
                              "left": _fmt_left(sd.get("resets_at", "")),
                              "resets_at": sd.get("resets_at", "")}}
        _usage_save(now, data)
        return data
    except Exception as e:
        # ошибка (429/сеть) — отдаём stale-данные с диска, помечая устаревшими
        if good:
            return {**good, "stale": True}
        return {"available": False, "error": str(e)[:100]}


# отступ от начала следующего 5h-периода: ждём пару минут после сброса, чтобы
# лимит точно обнулился, а не стартовать в самую секунду границы.
_NEXT_PERIOD_PAD = 5 * 60


def next_period_start() -> float | None:
    """Unix-время «начало следующего 5h-периода + 5 минут» из resets_at текущего
    окна. None, если usage недоступен или resets_at не распарсился."""
    from datetime import datetime
    u = get_usage()
    iso = ((u.get("five_hour") or {}).get("resets_at") or "")
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return None
    return dt.timestamp() + _NEXT_PERIOD_PAD


# ====================== headless claude (коворкинг) ======================
def _bashq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


# ====================== авто-роутинг модели по типу работы ======================
# Узкое место подписки — пропускная способность 5h-окна; Opus ест квоту в РАЗЫ
# быстрее Sonnet. Поэтому ДЕФОЛТ — Sonnet (и для задач тоже): он справляется с
# большинством задач за малую долю квоты. Opus — только когда задача ЯВНО помечена
# сложной (тег [opus]/[hard]/[сложно] в заголовке/задании) или settings model:task
# принудительно задаёт opus. Это оптимизация, не урезание: та же работа дешевле.
# Маппинг переопределяется settings-ключами model:review / model:analyze / model:task.
_MODELS = {
    "task": "claude-sonnet-4-6",   # дефолт задач — Sonnet (было opus); экономит квоту
    "review": "claude-sonnet-4-6",
    "analyze": "claude-sonnet-4-6",
    "route": "claude-haiku-4-5-20251001",  # дешёвый Haiku-классификатор серой зоны
}
OPUS = "claude-opus-4-8"

# Маркеры в заголовке/задании, поднимающие задачу на Opus (явно сложная).
_OPUS_MARKERS = ("[opus]", "[hard]", "[сложно]", "[сложная]", "[complex]")

# Ключевые слова — сильный сигнал сложности (→ Opus БЕЗ вызова Claude): архитектура,
# многофайловый рефакторинг, безопасность/конкурентность/финрасчёты, миграции.
_OPUS_KEYWORDS = (
    "архитектур", "проектир", "подсистем", "редизайн", "перепроектир",
    "рефактор", "переписать", "много файл", "across files", "concurrency",
    "конкурентн", "гонк", "race condition", "безопасн", "security", "крипт",
    "шифрован", "платёж", "платеж", "payment", "финанс", "webhook",
    "миграц", "схем бд", "версионир", "распределённ", "transaction",
    "architecture", "refactor", "redesign",
)

# Ключевые слова — сильный сигнал тривиальности (→ Sonnet БЕЗ вызова Claude):
# рутина с явным критерием готовности, локальная по сути.
_SONNET_KEYWORDS = (
    "опечатк", "typo", "коммент", "переименов", "rename", "лог ", "логирован",
    "тест", "юнит-тест", "docstring", "форматир", "отступ", "ui-твик",
    "текст кнопки", "подпис", "label", "css", "стил", "цвет",
)

# Серая зона (ни сильного сигнала сложности, ни тривиальности) разрешается Haiku-
# классификатором. Промпт получает ПОДСКАЗКУ эвристики (hint) — Haiku не размышляет
# с нуля, а подтверждает/опровергает склонность. Дефолт-склонность серой зоны — SONNET.
_ROUTE_PROMPT = """Ты — маршрутизатор сложности задач для оркестратора Claude Code.
Реши, какая модель нужна исполнителю: SONNET (дешёвая, рутина) или OPUS (дорогая, сложное).

OPUS — только если задача действительно требует сильной модели:
- архитектурные изменения, проектирование подсистем;
- рефакторинг многих файлов с неочевидными связями;
- тонкая логика безопасности / конкурентности / финрасчётов;
- многошаговые задачи с неочевидными зависимостями между шагами.

SONNET — всё остальное (большинство задач): локальные правки в 1-2 файлах,
тесты, CRUD, UI-твики, понятные изолированные фиксы с явным критерием готовности.

Предварительная оценка эвристики: {hint}. Перепроверь её по тексту задачи —
повышай до OPUS только при реальном сигнале сложности, иначе оставляй SONNET.

ЗАДАЧА:
---
{task}
---

Ответь СТРОГО двумя строками. Первая — метка:
MODEL: SONNET
или
MODEL: OPUS
Вторая — одна короткая фраза обоснования."""


def _smart_router_enabled() -> bool:
    """Тумблер умного авто-роутинга (settings smart_router: "0" отключает,
    откатывая на старое поведение чисто по ручным _OPUS_MARKERS)."""
    return db.get_setting("smart_router", "1") != "0"


def _classify_model_for_card(card: dict) -> str | None:
    """Гибрид: сперва бесплатная эвристика, Haiku — только в серой зоне.
    (1) сильный сигнал сложности (_OPUS_KEYWORDS) → OPUS сразу, без вызова Claude;
    (2) сигнал тривиальности (_SONNET_KEYWORDS) → None (дефолт Sonnet), без вызова;
    (3) серая зона → один Haiku-вызов (kind="route") с подсказкой эвристики в промпте.
    Возвращает имя модели или None (вызывающий падает на дефолт Sonnet). Любой сбой
    Haiku → None. Стоимость route-вызова копится в card.review_cost_usd. НЕ роняет
    запуск карты. Большинство карт решаются на шагах (1)/(2) → без вызова и без скачка
    cache_creation; платный Haiku — лишь для неоднозначных."""
    try:
        text = ((card.get("title") or "") + " " + (card.get("prompt") or "")).lower()
        if any(kw in text for kw in _OPUS_KEYWORDS):
            return OPUS
        if any(kw in text for kw in _SONNET_KEYWORDS):
            return None
        # серая зона → спрашиваем Haiku, подсказав ему дефолт-склонность SONNET
        cwd = project_path(card.get("slug") or "")
        task = ((card.get("title") or "") + "\n\n" + (card.get("prompt") or ""))[:2000]
        prompt = _ROUTE_PROMPT.format(task=task, hint="SONNET (нет явных сигналов сложности)")
        res = run_agent_once(prompt, cwd, f"route_{card['id']}.out", timeout=60, kind="route")
        rcost = (card.get("review_cost_usd") or 0) + (res.get("cost_usd") or 0)
        if res.get("cost_usd"):
            db.update_card(card["id"], review_cost_usd=round(rcost, 4))
        m = re.search(r"MODEL:\s*(SONNET|OPUS)", res.get("text", "") or "", re.IGNORECASE)
        if not m:
            return None
        return OPUS if m.group(1).upper() == "OPUS" else None
    except Exception:
        return None


def _model_for_card(card: dict) -> str:
    """Модель для запуска задачи. Приоритеты: (a) settings model:task override —
    высший; (b) ручной маркер сложности (_OPUS_MARKERS) в title/prompt → Opus
    (явная воля оператора важнее авто-роутинга); (c) гибрид-классификатор —
    эвристика по ключевым словам бесплатно, Haiku-вызов лишь в серой зоне
    (settings smart_router, вкл по умолчанию); (d) дефолт Sonnet. Дорогой Opus
    тратится только там, где нужен."""
    override = db.get_setting("model:task")
    if override:
        return override
    text = ((card.get("title") or "") + " " + (card.get("prompt") or "")).lower()
    if any(m in text for m in _OPUS_MARKERS):
        return OPUS
    if _smart_router_enabled():
        picked = _classify_model_for_card(card)
        if picked:
            return picked
    return _MODELS["task"]


def _model_for(kind: str) -> str:
    """Имя модели (для --model) по типу работы: review|analyze → sonnet,
    task → opus. settings-override (ключ model:<kind>) имеет приоритет."""
    override = db.get_setting(f"model:{kind}")
    if override:
        return override
    return _MODELS.get(kind, _MODELS["task"])


# Порог утилизации 5h-окна (%), выше которого новые задачи не стартуем, а ставим
# в очередь — чтобы не «доедать» квоту в ноль. settings-ключ window_util_limit; 0 = выкл.
WINDOW_UTIL_LIMIT_DEFAULT = 85


def _window_util_limit() -> int:
    raw = db.get_setting("window_util_limit", WINDOW_UTIL_LIMIT_DEFAULT)
    try:
        v = int(float(raw))
        return v if v > 0 else 0
    except (TypeError, ValueError):
        return WINDOW_UTIL_LIMIT_DEFAULT


def _window_util_exceeded() -> bool:
    """True, если утилизация 5h-окна превысила порог (старт задач придержать).
    Лимит 0 → выключено. usage недоступен → не блокируем (False)."""
    limit = _window_util_limit()
    if not limit:
        return False
    util = (get_usage().get("five_hour") or {}).get("util")
    return util is not None and util > limit


# Мягкий порог утилизации 5h-окна (%), выше которого параллелизм режем до 1: когда
# окно уже под нагрузкой, несколько тяжёлых задач разом суммарно жгут cache_read и
# выжигают остаток. Ниже жёсткого window_util_limit (там старт вовсе придерживается).
# settings-ключ wip_throttle_util; 0 = выкл.
WIP_THROTTLE_UTIL_DEFAULT = 60


def _effective_wip_limit() -> int:
    """WIP-лимит с учётом нагрузки окна: при util выше мягкого порога режем до 1,
    чтобы тяжёлые задачи не шли пачкой и не выжигали окно суммарным cache_read.
    Порог 0 или usage недоступен → обычный лимит."""
    base = db.get_wip_limit()
    try:
        thr = int(float(db.get_setting("wip_throttle_util", WIP_THROTTLE_UTIL_DEFAULT)))
    except (TypeError, ValueError):
        thr = WIP_THROTTLE_UTIL_DEFAULT
    if thr <= 0:
        return base
    util = (get_usage().get("five_hour") or {}).get("util")
    if util is not None and util > thr:
        return 1
    return base


# ---- авто-пауза раздувшихся задач (ночной инцидент #74: 159 turns / 18M
# cache_read в одном прогоне выжгли 5h-окно и уронили соседние задачи в
# session-limit). Идея: НЕ резать задачу (она не доделается), а если она вышла за
# рамки нормы И окну реально не хватит — поставить на ПАУЗУ и доделать через
# --resume (start_card_continue) в следующем окне. Пороги выше нормы (медиана
# задачи ~29 turns, cache_read почти всегда <5M) — ловят аномалию, не трогают
# здоровые длинные задачи. settings: pause_turns / pause_cache_read_m (0 = выкл).
PAUSE_TURNS_DEFAULT = 55
PAUSE_CACHE_READ_M_DEFAULT = 5  # млн токенов cache_read за прогон


def _pause_thresholds() -> tuple[int, int]:
    """(turns, cache_read_tokens) порогов раздувания; любой 0 → этот критерий выкл."""
    def _int(key, dflt):
        try:
            v = int(float(db.get_setting(key, dflt)))
            return v if v > 0 else 0
        except (TypeError, ValueError):
            return dflt
    return _int("pause_turns", PAUSE_TURNS_DEFAULT), \
        _int("pause_cache_read_m", PAUSE_CACHE_READ_M_DEFAULT) * 1_000_000


def _live_task_load(cid: int) -> dict:
    """Дёшево оценивает нагрузку БЕГУЩЕЙ задачи из её .out: число turns
    (assistant-сообщений с usage) и суммарный cache_read. Полный, но однопроходный
    разбор NDJSON — файл задачи небольшой (десятки-сотни строк), не хвост, чтобы
    счётчик turns был точным. {turns, cache_read}."""
    out_f = RUNS / f"card_{cid}.out"
    turns = cr = 0
    try:
        with out_f.open(errors="replace") as f:
            for line in f:
                if '"usage"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                u = (o.get("message") or {}).get("usage") or {}
                if not u:
                    continue
                turns += 1
                cr += u.get("cache_read_input_tokens") or 0
    except OSError:
        pass
    return {"turns": turns, "cache_read": cr}


def _task_bloated(cid: int) -> dict | None:
    """Если бегущая задача превысила порог раздувания — вернуть {reason, turns,
    cache_read}, иначе None. Любой порог 0 → этот критерий не проверяется."""
    pt, pcr = _pause_thresholds()
    if not pt and not pcr:
        return None
    load = _live_task_load(cid)
    if pt and load["turns"] >= pt:
        return {**load, "reason": f"{load['turns']} turns (порог {pt})"}
    if pcr and load["cache_read"] >= pcr:
        return {**load, "reason": f"{load['cache_read'] // 1_000_000}M cache_read "
                                  f"(порог {pcr // 1_000_000}M)"}
    return None


def project_path(slug: str) -> Path:
    # Привязанный проект знает свою папку явно (может лежать вне ~).
    proj = db.get_project(slug)
    if proj and proj.get("path") and Path(proj["path"]).is_dir():
        return Path(proj["path"])
    # Fallback: выводим путь из slug (проекты без явной привязки).
    name = slug
    if name.startswith("-home-nel"):
        name = name[len("-home-nel"):]
    name = name.lstrip("-")
    p = HOME / name
    return p if p.is_dir() else HOME


def path_to_slug(path: str | Path) -> str:
    """Кодирует абсолютный путь в slug так же, как ~/.claude/projects:
    /home/nel/foo → -home-nel-foo. Совместимо с обратным project_path()."""
    p = Path(path).expanduser().resolve()
    return str(p).replace("/", "-")


def scan_project(path: str | Path, max_entries: int = 40) -> str:
    """Считывает данные проекта с папки в краткое описание для БД:
    первый заголовок/строки README + верхнеуровневое дерево файлов."""
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        return ""
    parts = []
    for fname in ("README.md", "README", "readme.md"):
        f = p / fname
        if f.is_file():
            txt = f.read_text(errors="replace").strip()
            head = "\n".join(txt.splitlines()[:12]).strip()
            if head:
                parts.append(head)
            break
    entries = []
    skip = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache"}
    for e in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        if e.name in skip or e.name.startswith("."):
            continue
        entries.append(e.name + ("/" if e.is_dir() else ""))
        if len(entries) >= max_entries:
            break
    if entries:
        parts.append("Структура: " + ", ".join(entries))
    return "\n\n".join(parts).strip()


def scan_project_git(path: str | Path) -> dict:
    """Читает git-данные проекта: remote origin URL и ветку по умолчанию."""
    import subprocess
    p = Path(path).expanduser().resolve()
    if not (p / ".git").exists():
        return {"git_remote": "", "git_branch": ""}

    def _git(*args):
        try:
            r = subprocess.run(
                ["git", "-C", str(p)] + list(args),
                capture_output=True, text=True, timeout=5
            )
            return r.stdout.strip() if r.returncode == 0 else ""
        except Exception:
            return ""

    remote = _git("remote", "get-url", "origin")
    # основная ветка: сначала HEAD символический реф, иначе текущая ветка
    branch = _git("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if not branch:
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    # убираем префикс origin/
    if branch.startswith("origin/"):
        branch = branch[len("origin/"):]
    return {"git_remote": remote, "git_branch": branch}


def count_running() -> int:
    """Сколько карточек сейчас в статусе running (для WIP-лимита)."""
    return sum(1 for c in db.list_cards() if c["status"] == "running")


# Стратегия параллелизма (settings-ключ 'parallelism'):
#   project  — макс 1 running на проект (slug); разные проекты параллельно [дефолт]
#   worktree — каждая задача в своём git-worktree + мердж в основную ветку по
#              завершении; задачи одного проекта идут ПАРАЛЛЕЛЬНО без гонки записи
#   off      — без ограничений по проекту (только WIP-лимит); риск гонки записи
PARALLELISM_DEFAULT = "project"
PARALLELISM_MODES = ("project", "worktree", "off")

# slug самого prof: правит сам себя (worktree своего кода + reload-trigger + мердж
# в живой репозиторий = повышенный риск). Для него worktree-режим намеренно
# откатывается на project-lock (см. _worktree_enabled / project_busy).
SELF_SLUG = path_to_slug(PROF)


def get_parallelism() -> str:
    v = db.get_setting("parallelism", PARALLELISM_DEFAULT)
    return v if v in PARALLELISM_MODES else PARALLELISM_DEFAULT


def _is_self_project(slug: str) -> bool:
    """True, если slug указывает на сам prof (правка собственного кода)."""
    return bool(slug) and slug == SELF_SLUG


def _serializes_project(slug: str) -> bool:
    """True, если текущий режим сериализует задачи этого slug (макс 1 running на
    проект). Режим project — да; off — нет; worktree — нет, кроме self-prof."""
    if not slug:
        return False
    mode = get_parallelism()
    if mode == "off":
        return False
    if mode == "worktree":
        return _is_self_project(slug)
    return True  # project


def _worktree_enabled(slug: str) -> bool:
    """True, если для этой карточки реально применяем git-worktree изоляцию:
    режим parallelism=worktree, slug разрешается в git-репозиторий и это НЕ сам
    prof (для self-проекта worktree намеренно отключён — слишком рискованно)."""
    if get_parallelism() != "worktree" or not slug:
        return False
    if _is_self_project(slug):
        return False
    cwd = project_path(slug)
    return cwd != HOME and (cwd / ".git").exists()


def project_busy(slug: str, exclude_cid: int = None) -> bool:
    """True, если по проекту (slug) уже бежит задача И режим параллелизма требует
    сериализации по проекту. Задачи одного проекта тогда идут последовательно
    (макс 1 running на slug): headless claude правит общую рабочую папку, параллель
    = гонка записи в один файл (затёртые правки, ложные провалы, оборванные прогоны).

    Режим 'off' — лок выключен (вернёт False всегда). 'worktree' — лок снят, КРОМЕ
    self-проекта prof (для него worktree отключён → остаётся project-lock).
    None-slug (доски без проекта) не лочатся."""
    if not slug or get_parallelism() == "off":
        return False
    # worktree: задачи изолированы по копиям репо → лок не нужен. Исключение —
    # сам prof: worktree для него выключен, поэтому сериализуем как project.
    if get_parallelism() == "worktree" and not _is_self_project(slug):
        return False
    return any(c["id"] != exclude_cid and c.get("slug") == slug
               and c["status"] == "running" for c in db.list_cards())


# ====================== git-worktree (parallelism=worktree) ======================
# Изоляция параллельных задач одного проекта: каждая бежит в собственном
# git-worktree (отдельная рабочая копия + своя ветка prof-card-<cid>), поэтому
# правки не затирают друг друга. По успешному завершению ветка вливается в
# основную через merge --no-ff. Конфликт → статус merge_conflict, работа не
# теряется (ветка и worktree остаются для ручного домерджа).
_WORKTREES = PROF / "runs" / "worktrees"  # корень временных worktree-копий


def _git(args: list[str], cwd: Path, timeout: int = 60):
    """git-команда в cwd. Возвращает CompletedProcess или None при сбое запуска."""
    return _run(["git", *args], cwd, timeout=timeout)


def _wt_branch(cid: int) -> str:
    return f"prof-card-{cid}"


def _current_branch(repo: Path) -> str | None:
    """Имя текущей ветки репо (None при detached HEAD/ошибке)."""
    r = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo)
    if r is None or r.returncode != 0:
        return None
    name = r.stdout.strip()
    return name if name and name != "HEAD" else None


def create_worktree(slug: str, cid: int) -> tuple[Path, str] | None:
    """Создаёт git-worktree от HEAD основного репо проекта в отдельной папке с
    веткой prof-card-<cid>. Возвращает (путь_worktree, базовая_ветка) или None
    при ошибке. базовая_ветка — куда мерджить результат (текущая ветка репо на
    момент старта). Идемпотентно: остатки прошлого прогона карточки подчищаются."""
    repo = project_path(slug)
    if repo == HOME or not (repo / ".git").exists():
        return None
    base = _current_branch(repo) or "HEAD"
    _WORKTREES.mkdir(parents=True, exist_ok=True)
    wt = _WORKTREES / f"card_{cid}"
    branch = _wt_branch(cid)
    # подчистка остатков прошлого прогона той же карточки (иначе add упадёт)
    if wt.exists():
        _git(["worktree", "remove", "--force", str(wt)], repo)
    _git(["worktree", "prune"], repo)
    _git(["branch", "-D", branch], repo)  # снести старую ветку, если висит
    r = _git(["worktree", "add", "-b", branch, str(wt), "HEAD"], repo)
    if r is None or r.returncode != 0:
        return None
    return wt, base


def merge_worktree(slug: str, cid: int, base_branch: str | None = None) -> dict:
    """Вливает ветку задачи prof-card-<cid> в base_branch основного репо через
    merge --no-ff. base_branch — ветка, бывшая HEAD при старте worktree (cards.
    merge_branch); None → мерджим в текущую ветку репо. Возвращает {ok, conflict,
    summary}:
      ok=True            — слилось чисто;
      conflict=True      — конфликт мерджа (merge --abort выполнен, ветка и
                           worktree СОХРАНЕНЫ для ручного домерджа);
      ok=False,conflict=False — иная ошибка (нет ветки/репо/не та ветка)."""
    repo = project_path(slug)
    branch = _wt_branch(cid)
    if repo == HOME or not (repo / ".git").exists():
        return {"ok": False, "conflict": False, "summary": "нет git-репозитория"}
    # есть ли вообще что мерджить (ветка существует)?
    chk = _git(["rev-parse", "--verify", branch], repo)
    if chk is None or chk.returncode != 0:
        return {"ok": False, "conflict": False, "summary": f"ветка {branch} не найдена"}
    # основной репо должен стоять на целевой ветке — иначе мердж уйдёт не туда.
    cur = _current_branch(repo)
    if base_branch and cur != base_branch:
        co = _git(["checkout", base_branch], repo)
        if co is None or co.returncode != 0:
            return {"ok": False, "conflict": False,
                    "summary": f"не удалось переключиться на {base_branch} "
                               f"(репо на {cur}) — мердж пропущен"}
    r = _git(["merge", "--no-ff", "-m", f"prof: merge card {cid}", branch], repo)
    if r is None:
        return {"ok": False, "conflict": False, "summary": "merge: сбой запуска git"}
    if r.returncode == 0:
        return {"ok": True, "conflict": False, "summary": f"merge {branch} ✓"}
    # ненулевой rc → почти наверняка конфликт. Откатываем merge, чтобы основная
    # ветка осталась чистой; работу агента (ветку + worktree) НЕ трогаем.
    out = ((r.stdout or "") + (r.stderr or ""))
    conflict = "conflict" in out.lower()
    _git(["merge", "--abort"], repo)
    if conflict:
        # список конфликтных файлов берём из вывода merge (после abort он недоступен)
        files = []
        for ln in out.splitlines():
            m = re.search(r"CONFLICT.*?in (.+)$", ln)
            if m:
                files.append(m.group(1).strip())
        summary = "конфликт мерджа: " + (", ".join(files[:8]) if files else branch)
        return {"ok": False, "conflict": True, "summary": summary, "log": out[-2000:]}
    return {"ok": False, "conflict": False, "summary": "merge: ошибка", "log": out[-2000:]}


def remove_worktree(slug: str, cid: int, delete_branch: bool = True) -> None:
    """Удаляет worktree карточки и (по умолчанию) её ветку prof-card-<cid>.
    Тихо игнорирует ошибки — это уборка, не критичный путь."""
    repo = project_path(slug)
    if repo == HOME:
        return
    wt = _WORKTREES / f"card_{cid}"
    if wt.exists():
        _git(["worktree", "remove", "--force", str(wt)], repo)
    _git(["worktree", "prune"], repo)
    if delete_branch:
        _git(["branch", "-D", _wt_branch(cid)], repo)


def cleanup_orphan_worktrees() -> int:
    """Стартовая уборка осиротевших worktree/веток (рестарт сервиса оборвал
    задачи). Для каждого известного проекта: git worktree prune + снос веток
    prof-card-*, чья карточка уже НЕ running (незавершённые running не трогаем —
    их домерджит reaper). Возвращает число снесённых веток."""
    # cid'ы карточек, которые ещё бегут — их ветки/worktree оставляем.
    live = {c["id"] for c in db.list_cards() if c["status"] == "running"}
    removed = 0
    repos = set()
    for proj in db.list_projects_db():
        p = Path(proj.get("path") or "")
        if p.is_dir() and (p / ".git").exists():
            repos.add(p)
    for repo in repos:
        _git(["worktree", "prune"], repo)
        r = _git(["branch", "--list", "prof-card-*"], repo)
        if r is None or r.returncode != 0:
            continue
        for ln in r.stdout.splitlines():
            # git помечает текущую ветку '*', а checked-out-в-worktree — '+'
            name = ln.lstrip("*+ ").strip()
            m = re.match(r"prof-card-(\d+)$", name)
            if not m:
                continue
            cid = int(m.group(1))
            if cid in live:
                continue  # ещё работает — не наш мусор
            wt = _WORKTREES / f"card_{cid}"
            if wt.exists():
                _git(["worktree", "remove", "--force", str(wt)], repo)
            # -d сносит только полностью замердженные; осторожно не теряем работу.
            # Незамердженную ветку оставляем (её карточка не running → будет видна
            # как merge_conflict/failed, домерджат вручную).
            d = _git(["branch", "-d", name], repo)
            if d is not None and d.returncode == 0:
                removed += 1
    return removed


def start_card(card: dict) -> None:
    """Запускает headless claude по карточке (полный доступ), вывод → runs/.

    Уважает WIP-лимит: если уже бежит >= лимита задач, карточка не запускается, а
    становится 'queued' (остаётся в колонке approved) — reaper авто-стартует её
    по FIFO, как только освободится слот. См. db.get_wip_limit / start_next_queued.
    """
    # бюджет-гард: 5h-окно почти исчерпано → не жжём остаток квоты, ставим в
    # очередь (queued). reaper повторит, когда окно сбросится. Порог настраиваем
    # (window_util_limit, дефолт 85). Так задачи не «доедают» лимит в ноль.
    if _window_util_exceeded():
        db.update_card(card["id"], status="queued", column="approved",
                       result=(f"⏳ В очереди: 5h-окно почти исчерпано "
                               f"(>{_window_util_limit()}%). Запустится, когда лимит сбросится."))
        return
    limit = _effective_wip_limit()
    if count_running() >= limit:
        db.update_card(card["id"], status="queued", column="approved",
                       result=(f"⏳ В очереди: достигнут лимит параллельных задач "
                               f"({limit}). Запустится автоматически."))
        return
    # проект-лок: задачи одного slug строго последовательно (защита от гонки
    # записи в общий файл). Занят — в очередь, reaper запустит когда освободится.
    if project_busy(card.get("slug"), card["id"]):
        db.update_card(card["id"], status="queued", column="approved",
                       result="⏳ В очереди: по этому проекту уже выполняется задача "
                              "(один проект — последовательно). Запустится автоматически.")
        return
    _spawn_card(card)


def start_card_continue(card: dict, answer: str | None = None) -> None:
    """Продолжить недоделанную задачу: прокидывает агенту контекст прошлого
    прогона (его собственный итоговый отчёт), чтобы доделать, а не начинать с нуля.
    Если задача ждала ответа (needs_input), `answer` — ответ пользователя на
    заданный агентом вопрос, прокидывается в промпт. Уважает WIP-лимит как start_card."""
    prev = (card.get("result") or "").strip()
    # убираем наши служебные пометки из прошлого result
    for marker in ("⚠️ Задача НЕ доделана", "⚠️ Выполнение прервано", "⚠️ Прервано",
                   "⏳ В очереди", "⚠️ Валидация не прошла"):
        idx = prev.find(marker)
        if idx != -1:
            prev = prev[:idx].strip()
    answer_block = ""
    if answer and answer.strip():
        answer_block = (
            f"\n=== ОТВЕТ ПОЛЬЗОВАТЕЛЯ на твой вопрос/затык ===\n{answer.strip()}\n"
            f"Учти этот ответ и продолжи работу.\n")
    continue_prompt = (
        f"{card['prompt']}\n\n"
        f"=== ВАЖНО: это ПРОДОЛЖЕНИЕ ранее начатой задачи ===\n"
        f"Часть работы уже сделана в прошлый прогон. Вот твой итоговый отчёт с того раза:\n"
        f"---\n{prev[-4000:]}\n---\n"
        f"{answer_block}"
        f"Сначала проверь текущее состояние файлов (git status / git diff, прочитай "
        f"затронутые файлы) — что уже применено, а что нет. НЕ переделывай готовое. "
        f"Доделай ТОЛЬКО оставшееся и доведи задачу до конца. Прогони тесты."
    )
    # запускаем с подменённым prompt, не теряя оригинал в БД
    limit = db.get_wip_limit()
    if count_running() >= limit:
        db.update_card(card["id"], status="queued", column="approved",
                       result=(f"⏳ В очереди (продолжение): лимит параллельных задач "
                               f"({limit}). Запустится автоматически."))
        return
    if project_busy(card.get("slug"), card["id"]):
        db.update_card(card["id"], status="queued", column="approved",
                       result="⏳ В очереди (продолжение): по этому проекту уже "
                              "выполняется задача. Запустится автоматически.")
        return
    _spawn_card({**card, "prompt": continue_prompt})


# Флаг: авто-ревью включено по умолчанию (settings auto_review: "0" отключает)
def _auto_review_enabled() -> bool:
    return db.get_setting("auto_review", "1") != "0"


def _auto_review_if_needed(card: dict) -> None:
    """Запускает agent_review для карточки, попавшей в колонку review, и сохраняет
    вердикт в review_verdict / review_checked_at. Если 5h-окно исчерпано — пропускает
    (ревью отложится до следующего тика; задача остаётся в review с пустым вердиктом)."""
    if not _auto_review_enabled():
        return
    if card.get("review_verdict"):
        return  # уже проверена
    if _window_util_exceeded():
        return  # окно почти кончилось — не тратим квоту, проверим позже
    try:
        r = agent_review(card)
        rcost = (card.get("review_cost_usd") or 0) + (r.get("cost_usd") or 0)
        # Структурируем вердикт: «DONE: …» или «REWORK: …»
        verdict_tag = "DONE" if r["verdict"] == "done" else "REWORK"
        verdict_text = f"{verdict_tag}: {r['text']}"
        db.update_card(card["id"],
                       review_verdict=verdict_text,
                       review_checked_at=db.now(),
                       review_cost_usd=round(rcost, 4))
    except Exception:
        pass


_REVIEW_PROMPT = """Ты — ревьюер. Проверь, ВЫПОЛНЕНА ли поставленная задача в текущем проекте.

ЗАДАЧА (что требовалось сделать):
---
{task}
---

ОТЧЁТ ИСПОЛНИТЕЛЯ (что он якобы сделал):
---
{report}
---

Проверь по факту: прочитай затронутые файлы (git diff/status, grep, чтение), если есть тесты — прогони их. Убедись, что задача реально и полностью выполнена, а не только заявлена.

Ответь СТРОГО в таком формате (первая строка — вердикт):
VERDICT: DONE   — если задача выполнена полностью и корректно
VERDICT: REWORK — если НЕ выполнена / выполнена частично / есть проблемы
Затем 2-4 строки обоснования: что проверил и почему такой вердикт."""


def agent_review(card: dict) -> dict:
    """Синхронная агентная проверка «выполнена ли задача» через headless claude.
    Возвращает {verdict: 'done'|'rework', text, cost_usd}. Агент читает реальные
    файлы/тесты, не доверяя только отчёту исполнителя."""
    cwd = project_path(card.get("slug") or "")
    prompt = _REVIEW_PROMPT.format(task=(card.get("prompt") or "")[:3000],
                                   report=(card.get("result") or "")[:4000])
    res = run_agent_once(prompt, cwd, f"review_{card['id']}.out", timeout=600, kind="review")
    body = res.get("text", "") or ""
    # вердикт из строки с VERDICT:
    verdict = "rework"
    m = re.search(r"VERDICT:\s*(DONE|REWORK)", body, re.IGNORECASE)
    if m and m.group(1).upper() == "DONE":
        verdict = "done"
    return {"verdict": verdict, "text": body[-3000:], "cost_usd": res.get("cost_usd")}


def _spawn_card(card: dict) -> None:
    """Безусловный запуск процесса claude по карточке (после прохождения WIP-гарда)."""
    slug = card.get("slug") or ""
    cid = card["id"]
    # АТОМАРНЫЙ барьер от дублей (инцидент #77): столбим карточку как running ДО
    # медленных шагов (worktree/baseline/git занимают секунды). Если параллельный
    # вызов (тик reaper'а / двойной POST) уже застолбил — claim_spawn вернёт False,
    # и мы тихо выходим, не спавня второй процесс claude в ту же рабочую папку.
    if not db.claim_spawn(cid):
        return
    # с этой точки карточка атомарно помечена running. Любой сбой ДО Popen обязан
    # снять замок (вернуть в queued), иначе карточка зависнет running без процесса.
    try:
        # parallelism=worktree: запускаем в изолированной копии репо (своя ветка
        # prof-card-<cid>), чтобы параллельные задачи проекта не затирали файлы друг
        # друга. Результат вольётся мерджем в refresh_running_cards. Для self-проекта
        # prof и не-git папок worktree отключён (_worktree_enabled) → общий cwd.
        cwd = project_path(slug)
        worktree_path = merge_branch = None
        if _worktree_enabled(slug):
            wt = create_worktree(slug, cid)
            if wt is not None:
                cwd, merge_branch = wt[0], wt[1]
                worktree_path = str(cwd)
            # create_worktree упал (None) → молча падаем на общий cwd: лучше выполнить
            # задачу в общей папке, чем не выполнить вовсе (project_busy в worktree-
            # режиме лок снял, но одиночная задача в общей папке безопасна).
        db.update_card(cid, worktree_path=worktree_path, merge_branch=merge_branch)
        # baseline: сколько тестов падало ДО задачи → валидатор блокирует только если
        # задача ДОБАВИЛА новые провалы (чужие предсуществующие красные не в счёт).
        try:
            db.update_card(cid, test_baseline=count_failing_tests(cwd))
        except Exception:
            pass
        # head_at_start: git HEAD проекта на старте. По нему детерминированно ловим
        # «не выкачено» — если за время задачи в общей папке НЕ появилось нового
        # коммита (см. _deploy_not_done). В worktree-режиме коммитит сам мердж, тут не
        # пишем (None → проверка пропускается).
        if worktree_path is None:
            try:
                h = _git(["rev-parse", "HEAD"], cwd)
                if h is not None and h.returncode == 0:
                    db.update_card(cid, head_at_start=h.stdout.strip())
            except Exception:
                pass
    except Exception as e:
        db.update_card(cid, status="queued", column="approved",
                       result=f"⏳ В очереди: подготовка запуска сорвалась ({e}). Повтор автоматически.")
        return
    out_f = RUNS / f"card_{cid}.out"
    rc_f = RUNS / f"card_{cid}.rc"
    if rc_f.exists():
        rc_f.unlink()
    if out_f.exists():
        out_f.unlink()
    # фиксированный session_id → хук prof_waiting.sh пишет маркер waiting/<sid>.json,
    # а /api/cards/waiting матчит его на эту карточку напрямую (см. waiting_cards).
    sid = str(uuid.uuid4())
    # --output-format stream-json --verbose → NDJSON: каждое событие отдельной
    # строкой (system/assistant/user/result). Live-прогресс читаем дельтами
    # (read_progress), итоговый usage/cost — из события type=='result' в конце.
    model = _model_for_card(card)
    cmd = (
        f"claude -p {_bashq(card['prompt'])} --session-id {sid} --model {model} "
        f"--dangerously-skip-permissions "
        f"--output-format stream-json --verbose > {_bashq(str(out_f))} 2>&1; "
        f"echo $? > {_bashq(str(rc_f))}"
    )
    try:
        proc = subprocess.Popen(["bash", "-lc", cmd], cwd=str(cwd),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except Exception as e:
        # запуск сорвался — снимаем 'starting'-замок (claim_card_run), иначе карточка
        # зависнет незапускаемой: следующий /run не сможет её захватить.
        db.update_card(cid, status="failed", result=f"⚠️ Не удалось запустить: {e}")
        raise
    db.update_card(cid, status="running", pid=proc.pid, session_dir=sid, model=model,
                   started_at=db.now(), column="in_progress", result="", return_code=None)


# маркер «ждёт ввода» свежим считается ≤30 мин; старше — протух (агента уже нет)
_WAITING_FRESH = 30 * 60


def waiting_cards() -> list[dict]:
    """Карточки, чей агент стоит и ждёт ввода/разрешения.

    Сканирует маркеры runs/waiting/<session_id>.json (их пишет хук
    prof_waiting.sh) и матчит на running-карточки по cards.session_dir ==
    session_id. Возвращает [{card_id, waiting_since}] (waiting_since — сколько
    секунд назад агент встал).

    Self-heal (PostToolUse/Stop хук мог не сработать, напр. на ExitPlanMode):
      • карточка завершилась (есть runs/card_<cid>.rc) → маркер снимаем;
      • маркер протух (старше _WAITING_FRESH) → снимаем.
    В обоих случаях файл маркера удаляется, карточка не считается ждущей.
    """
    if not WAITING.exists():
        return []
    # session_id → card_id только для running-карточек
    by_sid = {c["session_dir"]: c["id"] for c in db.list_cards()
              if c.get("status") == "running" and c.get("session_dir")}
    now = db.now()
    res = []
    for marker in WAITING.glob("*.json"):
        sid = marker.stem
        try:
            ts = json.loads(marker.read_text()).get("ts", 0)
        except Exception:
            ts = 0
        # протух → подчистить и пропустить
        if now - ts > _WAITING_FRESH:
            marker.unlink(missing_ok=True)
            continue
        cid = by_sid.get(sid)
        if cid is None:
            continue  # чужая сессия / карточка не running — не наш маркер
        # карточка завершилась → хук снятия мог не отработать, чиним сами
        if (RUNS / f"card_{cid}.rc").exists():
            marker.unlink(missing_ok=True)
            continue
        res.append({"card_id": cid, "waiting_since": now - ts})
        card = db.get_card(cid)
        title = (card or {}).get("title") or f"#{cid}"
        notify_telegram(f"⏳ {title} ждёт твоего ввода", dedup_key=(cid, "waiting"))
    return res


def _parse_claude_json(raw: str) -> dict:
    """Финальный usage/cost из вывода claude. Поддерживает оба формата:
      • stream-json (--verbose): NDJSON, итог — в событии type=='result';
      • старый --output-format json: один JSON-объект (= тот же result-объект).
    Ищем result-объект с конца (он последний); обратно совместимо со старыми
    card_*.out, где это просто единственный/последний валидный JSON."""
    out = {"text": raw, "cost_usd": None, "input_tokens": None, "output_tokens": None,
           "cache_read_tokens": None, "cache_creation_tokens": None,
           "duration_ms": None, "num_turns": None}
    raw_s = raw.strip()
    obj = None
    # с конца: первый валидный JSON с type=='result' (stream-json) или с 'result'/
    # 'total_cost_usd' (старый единый объект). Substring fast-reject экономит json.loads.
    for candidate in (raw_s, *reversed(raw_s.splitlines())):
        candidate = candidate.strip()
        if not candidate.startswith("{") or '"result"' not in candidate \
                and '"total_cost_usd"' not in candidate:
            continue
        try:
            o = json.loads(candidate)
        except Exception:
            continue
        if o.get("type") == "result" or "total_cost_usd" in o or "result" in o:
            obj = o
            break
    if obj:
        u = obj.get("usage", {}) or {}
        out["text"] = obj.get("result") or obj.get("text") or _last_assistant_text(raw_s) or raw
        out["cost_usd"] = obj.get("total_cost_usd")
        out["input_tokens"] = u.get("input_tokens")
        out["output_tokens"] = u.get("output_tokens")
        out["cache_read_tokens"] = u.get("cache_read_input_tokens")
        out["cache_creation_tokens"] = u.get("cache_creation_input_tokens")
        out["duration_ms"] = obj.get("duration_ms")
        out["num_turns"] = obj.get("num_turns")
    else:
        # Нет финального type:result-события (поток оборван: рестарт/краш/задача
        # сама дёрнула reload — инцидент #77). НЕ сохраняем сырой NDJSON в result —
        # извлекаем последний осмысленный text-блок ассистента (человекочитаемо).
        last = _last_assistant_text(raw_s)
        if last:
            out["text"] = last
    return out


def _last_assistant_text(raw: str) -> str:
    """Последний непустой text-блок из assistant-сообщений NDJSON-потока.
    Спасает result, когда финального type:result нет (оборванный поток): вместо
    дампа сырого JSON показываем реальный последний ответ агента."""
    last = ""
    for line in raw.splitlines():
        line = line.strip()
        # fast-reject по подстрокам (без завязки на пробелы пунктуации JSON:
        # claude пишет компактно, но тесты/иные источники могут с пробелами).
        if not line.startswith("{") or '"text"' not in line or "assistant" not in line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("type") != "assistant":
            continue
        for c in (o.get("message") or {}).get("content") or []:
            if isinstance(c, dict) and c.get("type") == "text" and (c.get("text") or "").strip():
                last = c["text"].strip()
    return last


def read_progress(cid: int, since_offset: int = 0) -> dict:
    """Инкрементально читает NDJSON-прогресс card_{cid}.out от since_offset (по
    байтам, append-only — не перечитываем весь файл). Возвращает накопленный за
    дельту прогресс: события, новый offset, обрезанный текст последнего
    assistant-сообщения и текущую стоимость/токены (из самого свежего usage).

    last_text/running_cost берутся из событий ЭТОЙ дельты; вызывающий хранит их
    между вызовами (на дельте без assistant-сообщений last_text будет пустым)."""
    out_f = RUNS / f"card_{cid}.out"
    res = {"events": [], "offset": since_offset, "last_text": "",
           "running_cost": None, "input_tokens": None, "output_tokens": None}
    try:
        size = out_f.stat().st_size
    except OSError:
        return res
    if since_offset >= size:
        res["offset"] = size
        return res
    try:
        with out_f.open("rb") as f:
            f.seek(since_offset)
            data = f.read()
    except OSError:
        return res
    # последняя строка может быть недописана (append идёт построчно) — оставляем
    # её «на потом», сдвигая offset только до начала недописанного хвоста.
    nl = data.rfind(b"\n")
    if nl == -1:
        return res  # ни одной полной строки — ждём следующего тика
    consumed, tail = data[:nl + 1], data[nl + 1:]
    res["offset"] = since_offset + len(consumed)
    for line in consumed.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        # substring fast-reject до json.loads: строки без "type" — не события
        if not line.startswith("{") or '"type"' not in line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        res["events"].append(ev)
        t = ev.get("type")
        if t == "assistant":
            msg = ev.get("message", {}) or {}
            for c in msg.get("content", []) or []:
                if c.get("type") == "text" and c.get("text"):
                    res["last_text"] = c["text"][:200]
            u = msg.get("usage", {}) or {}
            if u.get("input_tokens") is not None:
                res["input_tokens"] = u.get("input_tokens")
            if u.get("output_tokens") is not None:
                res["output_tokens"] = u.get("output_tokens")
        elif t == "result":
            res["running_cost"] = ev.get("total_cost_usd")
            u = ev.get("usage", {}) or {}
            res["input_tokens"] = u.get("input_tokens", res["input_tokens"])
            res["output_tokens"] = u.get("output_tokens", res["output_tokens"])
            if ev.get("result"):
                res["last_text"] = ev["result"][:200]
    return res


def _wait_pid(pid: int, timeout: float) -> bool:
    """Ждёт смерти pid до timeout сек, попутно реапит зомби. True если умер."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)
            if done == pid:
                return True
        except ChildProcessError:
            return True  # не наш ребёнок / уже реапнут
        except OSError:
            pass
        try:
            os.kill(pid, 0)
        except OSError:
            return True  # процесс мёртв
        time.sleep(0.1)
    return False


def stop_card(card: dict) -> None:
    pid = card.get("pid")
    cid = card["id"]
    if pid:
        try:
            pgid = os.getpgid(pid)
        except OSError:
            pgid = None
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except OSError:
                pass
            if not _wait_pid(pid, timeout=8.0):
                # не завершился по-хорошему — добиваем группу
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except OSError:
                    pass
                _wait_pid(pid, timeout=2.0)
    # помечаем .rc, чтобы refresh_running_cards не ждал его и не перепутал с зомби
    rc_f = RUNS / f"card_{cid}.rc"
    if not rc_f.exists():
        try:
            rc_f.write_text("143")  # 128+SIGTERM
        except OSError:
            pass
    db.update_card(cid, status="stopped", return_code=143, finished_at=db.now())


def pause_card(card: dict, reason: str) -> None:
    """Ставит раздувшуюся задачу на ПАУЗУ (не финализирует как stopped/failed):
    мягко глушит процесс, сохраняет уже наработанный отчёт в result и помечает
    status='paused'. Реапер позже доделает её через --resume (start_card_continue)
    в следующем 5h-окне. Отличие от stop_card: НЕ пишем .rc (иначе reaper
    финализирует), сохраняем контекст прогона в result для продолжения."""
    pid = card.get("pid")
    cid = card["id"]
    # снимем текущий отчёт ДО убийства процесса — это контекст для resume
    raw = _read_tail(RUNS / f"card_{cid}.out")
    parsed = _parse_claude_json(raw)
    if pid:
        try:
            pgid = os.getpgid(pid)
        except OSError:
            pgid = None
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except OSError:
                pass
            if not _wait_pid(pid, timeout=8.0):
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except OSError:
                    pass
                _wait_pid(pid, timeout=2.0)
    # чистим .out/.rc, чтобы reaper не подхватил их как завершённый прогон, а
    # следующий запуск стартовал свежо (контекст прокидывается через result).
    for suffix in (".rc", ".out"):
        f = RUNS / f"card_{cid}{suffix}"
        try:
            f.unlink()
        except OSError:
            pass
    note = (f"⏸️ Поставлена на паузу: задача раздулась ({reason}) — чтобы не выжечь "
            f"5h-окно и не уронить соседние задачи в session-limit. "
            f"Продолжится автоматически в следующем окне (--resume).")
    result = (parsed["text"].strip() + "\n\n" + note).strip() if parsed["text"] else note
    # scheduled_at = начало следующего 5h-окна → resume_paused поднимет задачу не
    # раньше реального сброса окна (гистерезис против пинг-понга: иначе задача с тем
    # же раздутым контекстом мгновенно возобновится и снова перешагнёт порог). Если
    # usage недоступен — None, тогда resume гейтится только по util (как раньше).
    db.update_card(cid, status="paused", pid=None, column="approved",
                   result=result, finished_at=db.now(),
                   scheduled_at=next_period_start(),
                   cost_usd=parsed["cost_usd"], num_turns=parsed["num_turns"],
                   cache_read_tokens=parsed["cache_read_tokens"])


# Маркеры того, что агент НЕ завершил задачу, а ждёт ответа/доступа/РЕШЕНИЯ юзера:
# задаёт вопрос, упёрся в права (sudo/пароль), не смог проверить — ИЛИ предлагает
# выбор и ждёт развилку («что делаем дальше: A или B?», «без твоего слова не трогаю»).
_NEEDS_INPUT_MARKERS = (
    "incorrect password", "sudo-пароль", "sudo пароль", "пароль из памяти",
    "permission denied", "недостаточно прав", "нет доступа", "нет прав",
    "не смог проверить", "не удалось проверить", "не смог протестировать",
    "не удалось запустить тест", "не смог запустить", "требуется подтверждение",
    "дай актуальный", "дайте актуальный", "подскажи актуал", "если дашь",
    "если дадите", "скажи, если", "скажите, если", "уточни", "уточните",
    "не проверил", "проверку не провёл", "проверку не провел", "не валидировал",
    # развилки решения: агент предлагает варианты и ждёт выбор, код не трогает
    "что делаем дальше", "что делаем далее", "скажи, что делаем", "скажи что делаем",
    "без твоего слова", "без твоего решения", "жду твоего решения", "жду решения",
    "какой вариант", "какой из вариантов", "что выбираешь", "что предпочитаешь",
    "дай отмашку", "с чего начать", "с чего начинаем", "по твоему слову",
)
# «Задача НЕ доведена до конца», хотя агент отрапортовал и pytest/синтаксис зелёные.
# Кейс пользователя: агент пишет «выкатил», но деплой не состоялся (пароль не подошёл,
# не закоммитил, оставил TODO «вам нужно дописать»). git diff может быть непустым →
# validate_card даёт passed, и карточка ложно уходит в done. Эти маркеры — самопризнания
# агента в отчёте, что результат на прод/тест не попадёт. Отдельно от _needs_user_input
# (там семантика «жду твоего ответа»): здесь задача формально завершена, но впустую.
_NOT_DONE_MARKERS = (
    # деплой/выкатка не состоялись
    "не выкатил", "не выкачен", "не задеплоил", "не задеплоен", "деплой не",
    "выкатка не", "не получилось выкатить", "не удалось выкатить", "не запушил",
    "не запушен", "push не прошёл", "push не прошел", "пуш не прошёл", "пуш не прошел",
    "не попадёт в прод", "не попадет в прод", "не попало в прод", "не попал в прод",
    "ни в прод", "ни в тест", "не на проде", "не на тесте",
    # пароль/доступ сорвали именно выкатку (а не «спрашиваю пароль»)
    "пароль не подош", "пароль не верн", "неверный пароль", "пароль неверн",
    # не закоммичено / оставлено на пользователя
    "не коммитил", "не закоммитил", "не закоммичен", "не стал коммитить",
    "вам нужно дописать", "тебе нужно дописать", "нужно дописать", "вам нужно добавить",
    "пока не будет добавлен", "пока этот метод не", "не запустить, пока",
    "не заработает, пока", "не получится, пока", "оставил как есть",
    # английские самопризнания (агент часто рапортует по-английски): деплой
    "deploy failed", "deployment failed", "failed to deploy", "could not deploy",
    "couldn't deploy", "did not deploy", "didn't deploy", "not deployed",
    "push failed", "failed to push", "could not push", "couldn't push",
    "did not push", "didn't push", "not pushed", "wasn't pushed", "was not pushed",
    "incorrect password", "wrong password", "password did not work",
    "password didn't work", "authentication failed", "permission denied",
    # английское «оставлено на пользователя / не закоммичено»
    "did not commit", "didn't commit", "not committed", "wasn't committed",
    "was not committed", "you'll need to", "you will need to", "you need to add",
    "left as a todo", "left as todo", "left it as is", "not applied to prod",
    "won't reach prod", "will not reach prod", "manual step required",
)
def _claims_not_done(text: str) -> bool:
    """Агент в отчёте сам признаёт, что задача НЕ доведена (не выкачена/не закоммичена/
    оставлена на доработку), несмотря на rc=0 и зелёную валидацию. Возвращает True —
    тогда карточку не метим done, а отправляем в not_deployed (см. refresh_running_cards)."""
    if not text:
        return False
    return any(m in text.lower() for m in _NOT_DONE_MARKERS)


def _deploy_not_done(card: dict, cwd: Path) -> bool:
    """Детерминированный детект «не выкачено» — БЕЗ опоры на текст отчёта агента
    (тот мог промолчать про неудачу). Возвращает True, если за время задачи в
    общей папке проекта результат не попал на прод/тест по одной из причин:
      • не появилось нового коммита относительно head_at_start (агент наработал
        правки, но не закоммитил), ЛИБО
      • коммит появился, но локальная ветка опережает свой upstream (@{u}) —
        т.е. `git push` не прошёл (классический кейс «пароль/токен не подошёл»:
        агент закоммитил, HEAD сдвинулся, но на remote/прод ничего не уехало).

    Консервативно и OPT-IN: работает только при настройке deploy_check:<slug>=1
    (по умолчанию выкл — у многих проектов локальный коммит не равен деплою, и
    проверка давала бы ложные срабатывания). worktree-режим исключён (там коммитит
    сам мердж). Если upstream не настроен — push-часть пропускается (пушить
    некуда, прежнее поведение: судим только по наличию коммита)."""
    slug = card.get("slug") or ""
    if not slug or db.get_setting(f"deploy_check:{slug}") not in ("1", "true", "on"):
        return False
    if card.get("worktree_path"):  # в worktree коммит/мердж делает финализатор
        return False
    head_start = card.get("head_at_start")
    if not head_start:  # HEAD на старте не зафиксирован — нечего сравнивать
        return False
    h = _git(["rev-parse", "HEAD"], cwd)
    if h is None or h.returncode != 0:
        return False
    head_now = h.stdout.strip()
    # коммита не появилось → правки не закоммичены → на прод/тест не попадут.
    if head_now == head_start:
        return True
    # коммит появился. Но «закоммитил» ≠ «выкатил»: если у ветки есть upstream и
    # она его опережает — push не прошёл (пароль/токен/сеть), на remote ничего не
    # уехало. rev-list @{u}..HEAD непусто → есть незапушенные коммиты → not_deployed.
    # Нет upstream (rc≠0) → пушить некуда, не наш кейс (прежнее поведение).
    ahead = _git(["rev-list", "--count", "@{u}..HEAD"], cwd)
    if ahead is not None and ahead.returncode == 0:
        try:
            if int(ahead.stdout.strip()) > 0:
                return True
        except ValueError:
            pass
    return False


# ====================== прод-аудит «запушено, но не задеплоено» ======================
# Детект _deploy_not_done ловит «закоммичено локально, но не запушено в origin».
# Он НЕ видит финальный деплой на сервер (git pull на проде). Этот аудит закрывает
# вторую дыру: код запушен в GitHub, но прод отстаёт (забыли задеплоить).
#
# Природа гонки: между push и deploy всегда лаг, поэтому привязка к карточке давала
# бы ложные срабатывания. Поэтому это ОТДЕЛЬНЫЙ фоновый аудит состояния, не статус
# карточки. Чистый git/ssh — токены 5h-окна Claude НЕ тратит (LLM не вызывается).
#
# Конфиг per-project: deploy_remote_cmd:<slug> = shell-команда, печатающая HEAD прода
# (напр. `ssh root@HOST "cd /opt/app && git rev-parse HEAD"`). Не задана → пропуск.

_PROD_LAG_TTL = 300  # сек между авто-проверками прода (ssh не спамим)
_prod_lag_last_run = 0.0


def prod_check(slug: str) -> dict:
    """Сравнивает HEAD прода (из deploy_remote_cmd:<slug>) с origin/<branch>.
    Возвращает dict: {ok, lag, prod_head, origin_head, branch, error, checked_at}.
    lag = число коммитов, на которые прод отстаёт от origin (0 = синхронно).
    ok=False при любой ошибке (нет команды/ssh упал/не git-репо)."""
    cmd = db.get_setting(f"deploy_remote_cmd:{slug}")
    res = {"ok": False, "lag": None, "prod_head": None, "origin_head": None,
           "branch": None, "error": None, "checked_at": db.now()}
    if not cmd:
        res["error"] = "no_cmd"
        return res
    repo = project_path(slug)
    if not (repo / ".git").is_dir():
        res["error"] = "not_git"
        return res
    # 1) prod HEAD — выполняем заданную shell-команду
    try:
        p = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as e:
        res["error"] = f"cmd_failed:{e}"
        return res
    if p.returncode != 0:
        res["error"] = f"cmd_rc{p.returncode}:{(p.stderr or '').strip()[:200]}"
        return res
    prod_head = (p.stdout or "").strip().split()[-1] if (p.stdout or "").strip() else ""
    if len(prod_head) < 7:
        res["error"] = f"bad_prod_head:{prod_head!r}"
        return res
    res["prod_head"] = prod_head
    # 2) обновляем origin и берём origin/<branch>
    br = _current_branch(repo)
    res["branch"] = br
    _git(["fetch", "--quiet"], repo, timeout=30)
    u = _git(["rev-parse", f"origin/{br}"], repo) if br else None
    if u is None or u.returncode != 0:
        res["error"] = "no_origin_branch"
        return res
    origin_head = u.stdout.strip()
    res["origin_head"] = origin_head
    if prod_head == origin_head:
        res.update(ok=True, lag=0)
        return res
    # 3) на сколько прод отстаёт: коммиты origin/br, которых нет в prod_head.
    #    prod_head может отсутствовать в локальном репо (другой клон) — тогда
    #    rev-list упадёт; это значит прод на неизвестном коммите → считаем lag>0.
    cnt = _git(["rev-list", "--count", f"{prod_head}..origin/{br}"], repo)
    if cnt is not None and cnt.returncode == 0:
        try:
            res.update(ok=True, lag=int(cnt.stdout.strip()))
            return res
        except ValueError:
            pass
    # prod_head неизвестен локально → точное число не посчитать, но факт расхождения есть
    res.update(ok=True, lag=-1)  # -1 = «прод на неизвестном/чужом коммите»
    return res


def refresh_prod_lag(force: bool = False) -> dict:
    """Проходит по всем проектам с заданным deploy_remote_cmd, пишет prod_lag:<slug>
    (JSON результата prod_check). Троттлится _PROD_LAG_TTL, кроме force=True."""
    global _prod_lag_last_run
    if not force and (time.time() - _prod_lag_last_run) < _PROD_LAG_TTL:
        return {}
    _prod_lag_last_run = time.time()
    out = {}
    for b in db.list_boards():
        slug = b.get("slug")
        if not slug or not db.get_setting(f"deploy_remote_cmd:{slug}"):
            continue
        r = prod_check(slug)
        db.set_setting(f"prod_lag:{slug}", json.dumps(r))
        out[slug] = r
    return out


def _needs_user_input(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    if any(m in low for m in _NEEDS_INPUT_MARKERS):
        return True
    # Прямой вопрос в финале: ищем '?' ГДЕ УГОДНО в хвосте (~400 симв), а не только
    # в самом конце — агент часто задаёт вопрос, а затем добавляет уверенную
    # фразу-концовку («…A или B? Без твоего слова код не трогаю.») → endswith('?')
    # промахивался (инцидент #53). Достаточно вопросительного знака + слова-маркера.
    tail = text.rstrip()[-400:].lower()
    if "?" in tail and any(w in tail for w in (
            "нужно ли", "хочешь", "хотите", "актуал", "пароль", "что делаем",
            "какой", "реализовать", "продолж", "выбира", "или ", "дальше")):
        return True
    return False


# ====================== авто-валидация результата ======================
_VALIDATE_TIMEOUT = 120  # сек на весь прогон проверок
_VALIDATE_LOG_TAIL = 4000  # символов хвоста лога в БД/модалку
# Максимум, сколько ждём «соседей по проекту» перед валидацией задачи, чья .rc
# уже готова. Если в той же папке (slug) ещё бежит другая задача, её правки могут
# поломать pytest валидируемой → ложные провалы (инцидент #28). Откладываем
# валидацию, пока проект не освободится, но не дольше этого окна (защита от
# зависшего соседа). Считается от finished-момента появления .rc (started_at
# валидируемой нам тут не подходит — её процесс уже мёртв).
_VALIDATE_DEFER_MAX = 600  # сек


def _rc_mtime(rc_f: Path) -> float:
    """mtime файла .rc = момент реального завершения задачи (точка отсчёта окна
    отложенной валидации). now() при отсутствии — окно сразу истечёт, не зависнем."""
    try:
        return rc_f.stat().st_mtime
    except OSError:
        return db.now()


# Сколько секунд .out должен молчать, чтобы счесть процесс реально мёртвым (а не
# «PID мелькнул мёртвым» в гонке waitpid). claude в работе пишет в .out часто —
# 4 мин тишины при отсутствии .rc = процесс действительно оборван.
_OUT_STALE = 4 * 60


def _out_mtime(out_f: Path) -> float:
    """mtime .out = момент последней записи процесса. now() при отсутствии файла
    (out_silent=False → не финализируем по orphan, ждём .rc или появления .out)."""
    try:
        return out_f.stat().st_mtime
    except OSError:
        return db.now()


def _project_has_other_running(card: dict) -> bool:
    """True, если в том же проекте (slug) есть ДРУГАЯ ЕЩЁ РАБОТАЮЩАЯ задача —
    её правки в общей папке могут поломать pytest валидируемой (гонка #28).

    «Ещё работающая» = status=running И нет её .rc-файла. Сосед, у которого .rc
    уже есть, завершился и файлы не трогает — он НЕ блокирует (иначе два
    одновременно дописавших .rc соседа в одном проекте вечно блокируют друг друга
    взаимно → оба висят running, доска моргает через SSE d.done — реальный баг)."""
    slug = card.get("slug")
    if not slug:
        return False
    cid = card.get("id")
    for c in db.list_cards():
        if c["id"] == cid or c.get("slug") != slug or c["status"] != "running":
            continue
        if not (RUNS / f"card_{c['id']}.rc").exists():
            return True  # сосед реально ещё пишет — гонка возможна
    return False


def _run(cmd: list[str], cwd: Path, timeout: int = _VALIDATE_TIMEOUT):
    """subprocess.run с capture_output; None при таймауте/отсутствии бинаря."""
    try:
        return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                              timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def _nvm_bin() -> str | None:
    """Путь к bin/ актуальной версии node из ~/.nvm. Берёт .nvmrc проекта (если есть)
    или последнюю по имени директорию в ~/.nvm/versions/node/. None если nvm нет."""
    nvm_root = HOME / ".nvm" / "versions" / "node"
    if not nvm_root.is_dir():
        return None
    # .nvmrc в корне nvm (глобальный) — используем, если существует
    nvmrc = HOME / ".nvmrc"
    if nvmrc.exists():
        try:
            ver = nvmrc.read_text().strip().lstrip("v")
            p = nvm_root / f"v{ver}" / "bin"
            if p.is_dir():
                return str(p)
        except OSError:
            pass
    # иначе — последняя версия по имени (сортировка строковая: v20 > v18 > v10 правильно
    # при одинаковой длине; для смешанных — берём max по кортежу чисел)
    try:
        versions = sorted(
            nvm_root.iterdir(),
            key=lambda p: tuple(int(x) for x in p.name.lstrip("v").split(".")),
        )
        if versions:
            return str(versions[-1] / "bin")
    except (OSError, ValueError):
        pass
    return None


def _run_bash_lc(cmd: str, cwd: Path, timeout: int = _VALIDATE_TIMEOUT):
    """bash -lc <cmd> с nvm-бинарями в PATH (если nvm установлен).
    Нужно для validate_cmd в TS-проектах: демон prof стартует без nvm init,
    npx/node не в PATH без явного пути."""
    nvm = _nvm_bin()
    env = None
    if nvm:
        import os
        e = os.environ.copy()
        e["PATH"] = nvm + ":" + e.get("PATH", "")
        env = e
    try:
        return subprocess.run(["bash", "-lc", cmd], cwd=str(cwd),
                              capture_output=True, text=True, timeout=timeout, env=env)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def _parse_pytest_failed(out: str) -> int:
    """Число упавших тестов из вывода pytest -q (строка '... N failed ...').
    -1 если не удалось определить (нет тестов / pytest не запустился)."""
    m = re.search(r"(\d+)\s+failed", out or "")
    if m:
        return int(m.group(1))
    # «N passed» без failed → 0 провалов
    if re.search(r"\d+\s+passed", out or ""):
        return 0
    return -1


def count_failing_tests(cwd: Path) -> int:
    """Сколько pytest-тестов падает в проекте сейчас (для baseline). -1 если н/д."""
    if not _has_pytest(cwd):
        return -1
    r = _run([_python_bin(cwd), "-m", "pytest", "-q"], cwd)
    if r is None:
        return -1
    return _parse_pytest_failed((r.stdout or "") + (r.stderr or ""))


def _has_pytest(cwd: Path) -> bool:
    """Проект использует pytest, если есть pytest.ini/tox.ini[pytest] или
    [tool.pytest] в pyproject.toml."""
    if (cwd / "pytest.ini").exists():
        return True
    pp = cwd / "pyproject.toml"
    if pp.exists():
        try:
            if "[tool.pytest" in pp.read_text(errors="replace"):
                return True
        except OSError:
            pass
    return False


def _python_bin(cwd: Path) -> str:
    """.venv/bin/python проекта, иначе интерпретатор, под которым крутится prof."""
    import sys
    venv = cwd / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def _card_board_slug(card: dict) -> str:
    """Slug доски карточки — источник истины о проекте. Если доска недоступна,
    возвращаем cards.slug как fallback (старое поведение)."""
    bid = card.get("board_id")
    if bid:
        board_slug = db.get_board_slug(bid)
        if board_slug:
            return board_slug
    return card.get("slug") or ""


def _card_cwd(card: dict) -> Path:
    """Рабочая папка карточки: её git-worktree (parallelism=worktree), если ещё
    существует, иначе общая папка проекта. Slug берётся от доски (не карты) —
    при переносе карты между досками cards.slug устаревает, boards.slug — нет."""
    wt = card.get("worktree_path")
    if wt and Path(wt).is_dir():
        return Path(wt)
    return project_path(_card_board_slug(card))


def validate_card(card: dict) -> dict:
    """Детерминированная проверка результата задачи в cwd проекта — БЕЗ вызова
    claude (нулевые токены). По очереди: pytest (если настроен), синтаксис
    изменённых .py (py_compile), наличие хоть каких-то изменений (git status).

    settings-ключ validate_cmd:<slug> переопределяет дефолт — тогда гоняется
    только эта команда (через bash -lc), ok=rc==0.

    Возвращает {ok, summary, log}. НЕ падает при отсутствии тестов (ok=True
    с пометкой). Пустой git diff трактуется как провал — агент ничего не сделал."""
    cwd = _card_cwd(card)
    slug = _card_board_slug(card)

    # slug не разрешился в реальную папку проекта (project_path вернул HOME) —
    # нечего и негде валидировать; гонять git/pytest в домашней директории нельзя.
    if cwd == HOME:
        return {"ok": True, "summary": "нет папки проекта — валидация пропущена", "log": ""}

    # переопределение командой из настроек проекта
    override = db.get_setting(f"validate_cmd:{slug}")
    if override:
        r = _run_bash_lc(override, cwd)
        if r is None:
            return {"ok": False, "summary": "validate_cmd: таймаут/ошибка запуска",
                    "log": override}
        log = ((r.stdout or "") + (r.stderr or ""))[-_VALIDATE_LOG_TAIL:]
        ok = r.returncode == 0
        return {"ok": ok, "summary": f"validate_cmd: rc={r.returncode}", "log": log}

    parts, logs, ok = [], [], True

    # 1) тесты (если проект на pytest) — БЛОКИРУЮТ при НОВЫХ провалах.
    # Сравниваем с baseline (сколько падало ДО задачи): если задача добавила
    # провалы — валидация не проходит. Предсуществующие красные тесты (не вина
    # задачи) не блокируют. baseline=-1 (неизвестен) → любой провал блокирует.
    baseline = card.get("test_baseline")
    if baseline is None:
        baseline = -1
    if _has_pytest(cwd):
        r = _run([_python_bin(cwd), "-m", "pytest", "-q"], cwd)
        if r is None:
            ok = False
            parts.append("✗ тесты: таймаут/не запустились")
        else:
            out = (r.stdout or "") + (r.stderr or "")
            logs.append("$ pytest -q\n" + out)
            tail = next((ln.strip() for ln in reversed(out.splitlines()) if ln.strip()), "")
            now_failed = _parse_pytest_failed(out)
            # сколько было «допустимо» падающих до задачи (baseline). Новые сверх — фейл.
            allowed = baseline if baseline >= 0 else 0
            if now_failed > allowed:
                ok = False
                delta = now_failed - allowed if baseline >= 0 else now_failed
                parts.append(f"✗ тесты: {tail} (+{delta} новых провалов)")
            elif now_failed > 0:
                # провалы есть, но не новые (предсуществующие) — предупреждаем, не валим
                parts.append(f"⚠️ тесты: {tail} (предсуществующие, не вина задачи)")
            else:
                parts.append(f"тесты: {tail}")
    else:
        parts.append("нет тестов")

    # 2) git: что вообще изменилось
    gr = _run(["git", "status", "--porcelain"], cwd)
    changed_files = []
    no_git_changes = False
    if gr is not None and gr.returncode == 0:
        changed_files = [ln for ln in gr.stdout.splitlines() if ln.strip()]
        if not changed_files:
            no_git_changes = True
            parts.append("git: нет изменений")
        else:
            parts.append(f"git: {len(changed_files)} файла")
    # (если git недоступен — не валим валидацию из-за этого)

    # 3) синтаксис изменённых .py (py_compile) — дешёвая ловля поломок
    py_files = []
    for ln in changed_files:
        # формат porcelain: 'XY path' (XY — 2 символа статуса)
        path = ln[3:].strip().strip('"')
        if " -> " in path:  # переименование
            path = path.split(" -> ", 1)[1]
        if path.endswith(".py") and (cwd / path).exists():
            py_files.append(path)
    if py_files:
        cr = _run([_python_bin(cwd), "-m", "py_compile", *py_files], cwd)
        if cr is None or cr.returncode != 0:
            ok = False
            err = (cr.stderr if cr else "py_compile: таймаут")[-_VALIDATE_LOG_TAIL:]
            logs.append("$ py_compile\n" + (err or ""))
            parts.append("синтаксис: ошибка")

    # пустой git diff при rc=0 — агент выполнил аналитическую задачу или задал вопрос.
    # НЕ считаем провалом — пусть refresh_running_cards решит (needs_input), а не failed.
    return {"ok": ok, "summary": " / ".join(parts),
            "log": "\n\n".join(logs)[-_VALIDATE_LOG_TAIL:],
            "no_git_changes": no_git_changes}


def _finalize_worktree_merge(card: dict, result: str) -> dict:
    """Финализация задачи, бежавшей в git-worktree (parallelism=worktree), после
    успешной валидации: коммитим правки в ветке prof-card-<cid>, мерджим в базовую
    ветку (cards.merge_branch). Чистый мердж → done + worktree удаляется; конфликт
    → merge_conflict (ветка и worktree СОХРАНЕНЫ для ручного домерджа).

    Возвращает {status, column, result, validate_status?, val_summary?}."""
    cid = card["id"]
    slug = card.get("slug") or ""
    wt = Path(card["worktree_path"])
    # 1) закоммитить правки агента в worktree-ветке (агент мог не коммитить —
    # тогда merge нечего было бы влить, работа потерялась бы).
    if wt.is_dir():
        _git(["add", "-A"], wt)
        _git(["commit", "-m", f"prof card {cid}"], wt)  # пусто → no-op (nonzero rc ок)
    # 2) мердж в базовую ветку
    m = merge_worktree(slug, cid, card.get("merge_branch"))
    if m["conflict"]:
        # работу не теряем: ветка + worktree остаются, основная ветка чиста (abort).
        return {"status": "merge_conflict", "column": "review",
                "validate_status": "merge_conflict",
                "val_summary": m["summary"],
                "result": result + "\n⚠️ " + m["summary"]
                          + "\nВетка prof-card-%d и worktree сохранены — домерджи вручную."
                            % cid}
    if not m["ok"]:
        # не конфликт, но мердж не прошёл (нет ветки/не та ветка) — не теряем,
        # помечаем merge_conflict, чтобы человек разобрался (worktree оставляем).
        return {"status": "merge_conflict", "column": "review",
                "validate_status": "merge_conflict",
                "val_summary": m["summary"],
                "result": result + "\n⚠️ Мердж не выполнен: " + m["summary"]
                          + "\nWorktree сохранён."}
    # чистый мердж → убираем worktree и ветку, помечаем worktree_path снятым
    remove_worktree(slug, cid)
    db.update_card(cid, worktree_path=None)
    return {"status": "done", "column": "review", "validate_status": "passed",
            "val_summary": m["summary"], "result": result + "\n✓ " + m["summary"]}


def refresh_running_cards() -> None:
    """Финализирует завершённые карточки (детект по .rc, уборка зомби).
    Вызывается фоновым reaper-потоком (см. start_reaper), а не из GET-хендлеров."""
    for card in db.list_cards():
        if card["status"] != "running":
            continue
        cid = card["id"]
        rc_f = RUNS / f"card_{cid}.rc"
        out_f = RUNS / f"card_{cid}.out"
        pid = card.get("pid")
        # Авто-пауза раздувшихся задач (инциденты #74 и выжигание окна за ~5 мин):
        # если задача ещё БЕЖИТ (нет .rc) и вышла за порог раздувания — ставим на
        # паузу. Не резка: доделается через --resume в следующем окне (см. paused-
        # ветку ниже + start_card_continue).
        # БЕЗУСЛОВНО по порогу, НЕ дожидаясь _window_util_exceeded(): util берётся из
        # usage-кэша (TTL), и раздутая задача успевала выжечь окно за время жизни
        # кэша — к моменту, когда util покажет дефицит, окно уже сгорело. Пороги
        # (pause_turns / pause_cache_read_m) сами по себе ловят аномалию: здоровая
        # задача до них не дорастает.
        if not rc_f.exists():
            bloat = _task_bloated(cid)
            if bloat:
                pause_card(card, bloat["reason"])
                continue
        # Реапим зомби безусловно (а не только если pid «жив» на момент проверки):
        # WNOHANG не блокирует, ChildProcessError = уже реапнут/не наш ребёнок.
        pid_dead = pid is None
        if pid is not None:
            try:
                done, _ = os.waitpid(pid, os.WNOHANG)
                pid_dead = done == pid
            except ChildProcessError:
                pid_dead = True
            except OSError:
                pid_dead = True
            if not pid_dead:
                try:
                    os.kill(pid, 0)  # ещё жив (не зомби)?
                except OSError:
                    pid_dead = True
        # .rc — ЕДИНСТВЕННЫЙ надёжный сигнал завершения. После рестарта PID
        # ненадёжен (claude переживает рестарт через KillMode=process + new_session
        # и допишет .rc позже), поэтому мёртвый PID сам по себе НЕ финализирует —
        # иначе пережившую рестарт задачу ошибочно метим прерванной.
        rc_exists = rc_f.exists()
        # Задачу с мёртвым PID и без .rc считаем оборванной ТОЛЬКО при двойном
        # условии (иначе reaper ошибочно метит ЖИВЫЕ задачи interrupted —
        # «дёрганье»): claude через start_new_session переживает кажущуюся смерть
        # PID (waitpid в момент гонки) и дописывает .rc/продолжает писать .out.
        #   1) с момента старта прошло > GRACE — даём долгой задаче время;
        #   2) .out молчит дольше _OUT_STALE — процесс ТОЧНО мёртв и не пишет,
        #      а не просто «PID мелькнул мёртвым». Это и убирает ложные interrupted.
        GRACE = 15 * 60  # секунд — терпим долгие задачи (отложенный старт и пр.)
        started = card.get("started_at") or 0
        out_silent = (db.now() - _out_mtime(out_f)) > _OUT_STALE
        truly_orphaned = (pid_dead and not rc_exists
                          and started and (db.now() - started) > GRACE
                          and out_silent)
        finished = rc_exists or truly_orphaned
        if finished:
            rc = None
            if rc_f.exists():
                try:
                    rc = int(rc_f.read_text().strip())
                except Exception:
                    rc = None
            raw = _read_tail(out_f)
            parsed = _parse_claude_json(raw)
            result = parsed["text"]
            validate_status = validate_log = None
            val_summary = ""
            if rc == 0 and _needs_user_input(result):
                # rc=0, НО агент задал вопрос / не смог проверить (нет доступа,
                # просит пароль/подтверждение). Не считаем готовой — возвращаем
                # к юзеру на доработку/ответ, а не в «Проверка».
                status, col = "needs_input", "approved"
            elif rc == 0 and not card.get("worktree_path") \
                    and _has_pytest(project_path(card.get("slug") or "")) \
                    and _project_has_other_running(card) \
                    and (db.now() - _rc_mtime(rc_f)) < _VALIDATE_DEFER_MAX:
                # rc=0 и задача готова к валидации, НО в том же проекте ещё бежит
                # другая задача — её правки поломают pytest валидируемой (гонка,
                # инцидент #28: 14 ложных failed). Откладываем финализацию: задача
                # остаётся running, .rc на месте, следующий тик reaper'а повторит.
                # Окно _VALIDATE_DEFER_MAX страхует от зависшего соседа.
                # В worktree-режиме изоляции этой гонки нет (каждая задача в своей
                # копии) → defer не нужен (worktree_path выставлен).
                continue
            elif rc == 0:
                # rc=0 — но прежде чем слать на проверку, прогоняем дешёвую
                # детерминированную валидацию (тесты/синтаксис/git). Это ловит
                # «успешно написанный мусор» до ручного ревью.
                v = validate_card(card)
                validate_log = v["log"]
                val_summary = v["summary"]
                # no_git_changes при rc=0 — НЕ провал и НЕ needs_input автоматически.
                # ops/инфра-задачи (ssh, systemd, деплой на прод) не меняют локальный
                # git по определению. Autosave-коммит мог появиться после завершения,
                # но до нашей проверки (паттерн 3: гонка autosave). В обоих случаях
                # правильный исход — done/review, а не needs_input.
                # Исключение: если агент сам задал вопрос (_needs_user_input) — уже
                # обработано выше (до validate_card). Здесь продолжаем к not_done/done.
                if v["ok"] and _deploy_not_done(card, _card_cwd(card)):
                    # Детерминированный детект: deploy_check включён, HEAD не сдвинулся
                    # или не запушен → результат точно не попал на прод/тест. Это
                    # объективный факт, не зависящий от текста отчёта.
                    status, col, validate_status = "not_deployed", "review", "not_deployed"
                    result += "\n⚠️ Задача не доведена: результат не выкачен/не закоммичен (на прод/тест не попадёт)."
                elif v["ok"] and v.get("no_git_changes") and _claims_not_done(result):
                    # Гибридный детект: git-изменений нет И агент сам признался, что
                    # не выкатил/не закоммитил. Оба сигнала вместе → not_deployed.
                    # (только _claims_not_done без git-факта — ненадёжно: агент мог
                    # написать стоп-фразу о прошлом или чужой задаче, паттерн 2.)
                    status, col, validate_status = "not_deployed", "review", "not_deployed"
                    result += "\n⚠️ Задача не доведена: результат не выкачен/не закоммичен (на прод/тест не попадёт)."
                elif v["ok"]:
                    status, col, validate_status = "done", "review", "passed"
                    # parallelism=worktree: вливаем ветку задачи в основную. Конфликт
                    # → merge_conflict (работа сохранена), чистый мердж → worktree
                    # убираем. См. _finalize_worktree_merge.
                    if card.get("worktree_path"):
                        m = _finalize_worktree_merge(card, result)
                        status, col = m["status"], m["column"]
                        result = m["result"]
                        if m.get("validate_status"):
                            validate_status = m["validate_status"]
                        val_summary = m.get("val_summary", val_summary)
                else:
                    status, col, validate_status = "failed", "in_progress", "failed"
                    result += "\n⚠️ Валидация не прошла: " + v["summary"]
            elif rc is None:
                # нет .rc после грейс-периода = процесс реально оборван
                # (рестарт со старым KillMode, краш). Возвращаем в очередь.
                status, col = "interrupted", "approved"
                result = "⚠️ Выполнение прервано (рестарт/обрыв). Запусти заново."
            else:
                # есть вывод, но rc≠0 — реальная ошибка
                status, col = "failed", "in_progress"
            db.update_card(cid, status=status, return_code=rc,
                           result=result, finished_at=db.now(), column=col,
                           cost_usd=parsed["cost_usd"], input_tokens=parsed["input_tokens"],
                           output_tokens=parsed["output_tokens"],
                           cache_read_tokens=parsed["cache_read_tokens"],
                           cache_creation_tokens=parsed["cache_creation_tokens"],
                           duration_ms=parsed["duration_ms"], num_turns=parsed["num_turns"],
                           validate_status=validate_status, validate_log=validate_log)
            # Авто-ревью: если карточка попала в review — запускаем LLM-проверку.
            # Ревью синхронное (~1-3 мин), поэтому спавним в отдельный поток, чтобы
            # не блокировать reaper (он должен крутиться раз в 3с).
            if col == "review":
                updated_card = db.get_card(cid)
                threading.Thread(
                    target=_auto_review_if_needed,
                    args=(updated_card,),
                    daemon=True,
                ).start()
            # Уведомление о ключевом переходе (дедуп по (cid, status)).
            title = card.get("title") or f"#{cid}"
            cost = parsed["cost_usd"]
            cost_s = f"${cost:.2f}" if isinstance(cost, (int, float)) else "$?"
            if status == "done":
                summary = val_summary or "валидация пройдена"
                notify_telegram(f"✅ {title} готова — {summary}, {cost_s}",
                                dedup_key=(cid, "done"))
            elif status == "failed":
                notify_telegram(f"❌ {title} упала rc={rc}",
                                dedup_key=(cid, "failed"))
            elif status == "not_deployed":
                notify_telegram(f"🚧 {title}: отрапортована, но не выкачена — нужна доработка",
                                dedup_key=(cid, "not_deployed"))
            elif status == "merge_conflict":
                notify_telegram(f"⚠️ {title}: конфликт мерджа worktree — домерджи вручную",
                                dedup_key=(cid, "merge_conflict"))


def start_next_queued() -> int:
    """Авто-старт queued-карточек, пока есть свободные слоты под WIP-лимитом.
    Вызывается reaper'ом после финализации завершённых задач.
    Возвращает число запущенных карточек.

    ПРОЕКТ-ЛОК (режим parallelism='project'): по одному проекту (slug)
    одновременно бежит максимум одна задача — headless claude правит общую рабочую
    папку, параллель = гонка записи в один файл. За проход запускаем не более одной
    задачи на slug, пропуская занятые проекты. FIFO по created_at между свободными
    проектами. Режим 'off' — лок снят (project_busy всегда False), только WIP-лимит.
    Режим 'worktree' — лок снят (задачи изолированы по git-worktree), несколько
    задач одного проекта стартуют за проход; ИСКЛЮЧЕНИЕ — сам prof (worktree для
    него отключён → ведёт себя как project). None-slug (доски без проекта) лок не
    трогает."""
    limit = _effective_wip_limit()
    cards = db.list_cards()
    queued = sorted((c for c in cards if c["status"] == "queued"),
                    key=lambda c: (c.get("created_at") or 0, c["id"]))
    if not queued:
        return 0

    started = 0
    # slug'и, занятые в рамках ЭТОГО прохода (running + только что стартованные),
    # чтобы не запустить две задачи одного проекта за один тик reaper'а.
    locked: set = set()
    for card in queued:
        if count_running() >= limit:
            break
        slug = card.get("slug")
        # проект уже занят (бежит другая задача или стартовали в этом проходе) →
        # пропускаем. project_busy учитывает режим parallelism (off/worktree → не
        # лочит, кроме self-prof в worktree).
        if slug and (slug in locked or project_busy(slug, card["id"])):
            continue
        _spawn_card(card)
        # лочим slug в этом проходе только если режим реально сериализует проект
        # (project определяет лок; worktree/off — нет, кроме self-prof в worktree).
        if slug and _serializes_project(slug):
            locked.add(slug)
        started += 1
    return started


def start_scheduled() -> int:
    """Запуск отложенных карточек (status=scheduled), у которых наступило время
    scheduled_at, при наличии свободного слота под WIP-лимитом. Вызывается
    reaper'ом рядом с start_next_queued. Через start_card → уважает лимит (если
    слотов нет, карточка станет queued и стартует по FIFO). Возвращает число
    обработанных карточек (запущенных или поставленных в очередь)."""
    now = db.now()
    due = sorted(
        (c for c in db.list_cards()
         if c["status"] == "scheduled" and (c.get("scheduled_at") or 0) <= now),
        key=lambda c: (c.get("scheduled_at") or 0, c["id"]))
    started = 0
    for card in due:
        if count_running() >= db.get_wip_limit():
            break  # слотов нет — оставляем scheduled, следующий тик повторит
        cont = bool(card.get("sched_continue"))
        db.update_card(card["id"], scheduled_at=None, sched_continue=None)
        fresh = {**card, "scheduled_at": None}
        # sched_continue=1 (отложили failed/needs_input «доделать») → продолжаем с
        # учётом прошлого прогона, иначе обычный запуск с нуля.
        if cont:
            start_card_continue(fresh)
        else:
            start_card(fresh)
        started += 1
    return started


def resume_paused() -> int:
    """Возобновляет задачи, поставленные на паузу из-за раздувания (pause_card),
    как только 5h-окно снова освободилось. Доделывает через --resume-контекст
    (start_card_continue: прокидывает прошлый отчёт, «доделай остаток»), а не с
    нуля. Пока окно всё ещё на исходе — ничего не трогаем (ждём сброса).
    Возвращает число возобновлённых задач."""
    if _window_util_exceeded():
        return 0  # окно ещё не отпустило — рано будить, иначе снова упрёмся
    now = db.now()
    paused = sorted((c for c in db.list_cards() if c["status"] == "paused"),
                    key=lambda c: (c.get("created_at") or 0, c["id"]))
    started = 0
    for card in paused:
        if count_running() >= db.get_wip_limit():
            break
        # гистерезис: задача, поставленная на паузу за раздувание, несёт
        # scheduled_at = начало следующего окна. Не будим раньше — иначе тот же
        # раздутый контекст мгновенно возобновится и снова перешагнёт порог
        # (пинг-понг, жгущий токены). scheduled_at пуст (старая пауза/нет usage) →
        # поднимаем как раньше, гейтом служит util выше.
        sched = card.get("scheduled_at")
        if sched and now < sched:
            continue
        # сбрасываем scheduled_at, чтобы не залип в scheduled-семантике после resume
        db.update_card(card["id"], scheduled_at=None)
        # start_card_continue сам поставит в queued, если проект/WIP заняты
        start_card_continue(card)
        started += 1
    return started


def run_agent_once(prompt: str, cwd: Path, out_name: str, timeout=600,
                   kind: str = "analyze") -> dict:
    """Синхронный headless-вызов claude (для аудита/анализа/проверки).
    --output-format json → можем достать и текст ответа, и cost_usd/токены.
    kind задаёт модель (см. _model_for): по умолчанию analyze → дешёвая модель.
    Возвращает {ok, text, cost_usd}."""
    out_f = RUNS / out_name
    model = _model_for(kind)
    cmd = (f"claude -p {_bashq(prompt)} --model {model} --dangerously-skip-permissions "
           f"--output-format json > {_bashq(str(out_f))} 2>&1; "
           f"echo $? > {_bashq(str(out_f))}.rc")
    try:
        subprocess.run(["bash", "-lc", cmd], cwd=str(cwd), timeout=timeout,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"ok": False, "text": "timeout", "cost_usd": None}
    raw = out_f.read_text(errors="replace") if out_f.exists() else ""
    p = _parse_claude_json(raw)
    return {"ok": True, "text": p.get("text", raw), "cost_usd": p.get("cost_usd")}


# ====================== фоновый reaper ======================
_REAPER_STARTED = False
_REAPER_INTERVAL = 3  # сек


def start_reaper() -> None:
    """Запускает единственный фоновый поток, периодически финализирующий
    завершённые карточки и реапящий зомби. Раньше это делалось синхронно
    внутри GET /api/cards — медленно и не реапило зомби при гонке."""
    global _REAPER_STARTED
    if _REAPER_STARTED:
        return
    _REAPER_STARTED = True

    # стартовая уборка осиротевших worktree/веток (рестарт сервиса оборвал задачи)
    try:
        cleanup_orphan_worktrees()
    except Exception:
        pass

    def loop():
        while True:
            try:
                refresh_running_cards()
                resume_paused()      # окно освободилось → доделываем paused (--resume)
                start_scheduled()    # созрел отложенный старт → запускаем (уважая WIP)
                start_next_queued()  # освободился слот → авто-старт очереди (FIFO)
                refresh_prod_lag()   # прод отстал от origin? (троттл 5мин, git/ssh, без LLM)
            except Exception:
                pass
            time.sleep(_REAPER_INTERVAL)

    threading.Thread(target=loop, daemon=True).start()


# ====================== uptime-пинг ======================
def ping_service(svc: dict) -> dict:
    url = svc["url"]
    t0 = time.time()
    status, err = None, None
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "prof-uptime"})
        with urllib.request.urlopen(req, timeout=10) as r:
            status = r.status
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception as e:
        err = str(e)[:120]
    ms = int((time.time() - t0) * 1000)
    db.update_service(svc["id"], last_status=status or 0, last_ms=ms, last_checked=db.now())
    return {"id": svc["id"], "status": status, "ms": ms, "error": err}


def ping_all_services() -> list[dict]:
    return [ping_service(s) for s in db.list_services()]


# ====================== MCP-обнаружение ======================
def discover_mcp() -> list[dict]:
    """Список MCP-серверов из `claude mcp list` (реальный статус подключения)."""
    found = []
    try:
        r = subprocess.run(["claude", "mcp", "list"], capture_output=True,
                           text=True, timeout=30)
        for line in r.stdout.splitlines():
            line = line.strip()
            # формат: "Name: url-or-cmd - ✔ Connected"  /  "- ! Needs authentication"
            m = re.match(r"^(.+?):\s+(.+?)\s+-\s+(.+)$", line)
            if not m:
                continue
            name, target, status_raw = m.group(1), m.group(2), m.group(3)
            ok = "Connected" in status_raw or "✔" in status_raw
            need_auth = "auth" in status_raw.lower()
            found.append({"name": name.strip(), "target": target.strip(),
                          "transport": "http" if target.startswith("http") else "stdio",
                          "connected": ok, "needs_auth": need_auth,
                          "status": status_raw.strip()})
    except Exception as e:
        return [{"name": "(ошибка чтения)", "status": str(e)[:100], "connected": False}]
    return found


# ====================== git-бэкап ======================
def git_backup(message: str = None) -> dict:
    msg = message or f"prof autosave {time.strftime('%Y-%m-%d %H:%M')}"
    try:
        subprocess.run(["git", "add", "-A"], cwd=str(PROF), check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        r = subprocess.run(["git", "commit", "-m", msg], cwd=str(PROF),
                           capture_output=True, text=True)
        committed = r.returncode == 0
        pushed = None
        remote = subprocess.run(["git", "remote"], cwd=str(PROF),
                                capture_output=True, text=True).stdout.strip()
        if remote and committed:
            pr = subprocess.run(["git", "push"], cwd=str(PROF),
                                capture_output=True, text=True, timeout=60)
            pushed = pr.returncode == 0
        return {"committed": committed, "pushed": pushed,
                "msg": (r.stdout or r.stderr).strip()[:200]}
    except Exception as e:
        return {"committed": False, "error": str(e)[:200]}


def git_log(n=15) -> list[dict]:
    try:
        r = subprocess.run(["git", "log", f"-{n}", "--pretty=%h|%ar|%s"],
                           cwd=str(PROF), capture_output=True, text=True)
        out = []
        for line in r.stdout.strip().splitlines():
            h, ago, *rest = line.split("|")
            out.append({"hash": h, "ago": ago, "subject": "|".join(rest)})
        return out
    except Exception:
        return []


def git_set_remote(url: str) -> dict:
    try:
        subprocess.run(["git", "remote", "remove", "origin"], cwd=str(PROF),
                       capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", url], cwd=str(PROF), check=True)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ====================== Obsidian-синк ======================
def obsidian_vault() -> Path:
    v = db.get_setting("obsidian_vault")
    return Path(v) if v else (HOME / "ObsidianVault" / "prof")


def sync_to_obsidian() -> dict:
    """Экспортирует доски/карточки в markdown-vault с [[wiki-links]]."""
    vault = obsidian_vault()
    vault.mkdir(parents=True, exist_ok=True)
    n = 0
    for board in db.list_boards():
        cards = db.list_cards(board["id"])
        lines = [f"# {board['name']}\n"]
        for col in db.DEFAULT_COLUMNS:
            cc = [c for c in cards if c["column"] == col]
            if not cc:
                continue
            lines.append(f"\n## {db.COLUMN_TITLES.get(col, col)}\n")
            for c in cc:
                lines.append(f"- [[{c['title']}]] ({c['status']}) — {c.get('origin','')}")
        (vault / f"board-{board['slug'] or board['id']}.md").write_text("\n".join(lines))
        n += 1
        for c in cards:
            safe = "".join(ch for ch in c["title"] if ch not in '/\\:*?"<>|')[:80]
            body = f"---\nstatus: {c['status']}\ncolumn: {c['column']}\norigin: {c.get('origin','')}\n---\n\n# {c['title']}\n\n{c.get('prompt','')}\n\n## Результат\n\n{c.get('result','')[:4000]}"
            (vault / f"{safe}.md").write_text(body)
    return {"synced_boards": n, "vault": str(vault)}


# ====================== обзор навыков (skills overview) ======================
CLAUDE_DIR = HOME / ".claude"

# Краткие описания известных MCP-серверов (если сервер не отдал инструкций).
MCP_BLURBS = {
    "gmail": "Чтение/отправка почты Gmail, поиск писем, работа с черновиками.",
    "calendar": "Google Calendar: события, расписание, создание встреч.",
    "gcal": "Google Calendar: события, расписание, создание встреч.",
    "drive": "Google Drive: поиск, чтение и загрузка файлов.",
    "gdrive": "Google Drive: поиск, чтение и загрузка файлов.",
    "higgsfield": "Генерация изображений/видео/аудио, 3D, апскейл, motion-control.",
    "claude.ai": "Интеграция с claude.ai (синк агентов, ресурсы аккаунта).",
    "github": "GitHub: PR, issue, репозитории, обзор кода через API.",
    "slack": "Slack: чтение/отправка сообщений, каналы.",
    "notion": "Notion: страницы, базы данных, поиск.",
    "linear": "Linear: задачи, проекты, трекинг.",
    "filesystem": "Доступ к файловой системе (чтение/запись в разрешённых путях).",
    "puppeteer": "Управление браузером: навигация, скриншоты, скрейпинг.",
    "playwright": "Браузерная автоматизация и e2e-проверки.",
    "memory": "Граф знаний/память для агента.",
    "fetch": "Загрузка веб-страниц и преобразование в текст для модели.",
    "sentry": "Sentry: ошибки, трейсы, мониторинг приложений.",
}


def _mcp_blurb(name: str) -> str:
    key = name.strip().lower()
    if key in MCP_BLURBS:
        return MCP_BLURBS[key]
    for k, v in MCP_BLURBS.items():
        if k in key:
            return v
    return ""


def _read_text(p: Path) -> str:
    try:
        return p.read_text(errors="replace")
    except OSError:
        return ""


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Лёгкий парсер YAML-frontmatter (--- … ---). Снимает кавычки,
    склеивает многострочные значения (отступ-продолжение и `>` блоки)."""
    meta, body = {}, text
    if not text.startswith("---"):
        return meta, text.strip()
    end = text.find("\n---", 3)
    if end == -1:
        return meta, text.strip()
    block, body = text[3:end], text[end + 4:]
    last_key = None
    for line in block.splitlines():
        m = re.match(r"^(\w[\w_-]*):\s*(.*)$", line)
        if m:
            last_key = m.group(1)
            val = m.group(2).strip().strip('"').strip("'")
            # `>` / `|` — начало многострочного скаляра, тело идёт ниже
            meta[last_key] = "" if val in (">", "|", ">-", "|-") else val
        elif last_key and line.strip() and (line.startswith((" ", "\t"))):
            # строка-продолжение многострочного значения
            cont = line.strip()
            meta[last_key] = (meta[last_key] + " " + cont).strip() if meta[last_key] else cont
    # пустые значения убираем (как в app._parse_fm)
    meta = {k: v for k, v in meta.items() if v}
    return meta, body.strip()


def _first_md_summary(text: str) -> str:
    """Первая содержательная строка markdown (для команд без frontmatter)."""
    for line in text.splitlines():
        s = line.strip().lstrip("#").strip()
        if s and not s.startswith("---"):
            return s[:200]
    return ""


def _skills_hooks() -> list[dict]:
    """Хуки из ~/.claude/settings.json — событие + краткое описание команды."""
    out = []
    try:
        data = json.loads(_read_text(CLAUDE_DIR / "settings.json") or "{}")
    except Exception:
        return out
    for event, entries in (data.get("hooks") or {}).items():
        for entry in entries:
            matcher = entry.get("matcher") or "*"
            for h in entry.get("hooks", []):
                cmd = h.get("command", "")
                # краткое имя: последний осмысленный токен команды
                short = cmd.replace("\n", " ")[:160]
                out.append({"event": event, "matcher": matcher, "command": short})
    return out


def _skills_commands() -> list[dict]:
    """Slash-команды из ~/.claude/commands/*.md."""
    out = []
    cmd_dir = CLAUDE_DIR / "commands"
    if not cmd_dir.is_dir():
        return out
    for f in sorted(cmd_dir.glob("*.md")):
        txt = _read_text(f)
        meta, body = parse_frontmatter(txt)
        out.append({
            "name": meta.get("name", f.stem),
            "description": meta.get("description") or _first_md_summary(body),
        })
    return out


def _skills_skills() -> list[dict]:
    """Скилы из ~/.claude/skills/* (SKILL.md с frontmatter)."""
    out = []
    sk_dir = CLAUDE_DIR / "skills"
    if not sk_dir.is_dir():
        return out
    for d in sorted(sk_dir.iterdir()):
        sk = d / "SKILL.md"
        if not sk.is_file():
            continue
        meta, _ = parse_frontmatter(_read_text(sk))
        out.append({
            "name": meta.get("name", d.name),
            "description": meta.get("description", ""),
        })
    return out


def _skills_agents() -> list[dict]:
    """Сабагенты из ~/.claude/agents/*.md."""
    out = []
    ag_dir = CLAUDE_DIR / "agents"
    if not ag_dir.is_dir():
        return out
    for f in sorted(ag_dir.glob("*.md")):
        meta, _ = parse_frontmatter(_read_text(f))
        out.append({
            "name": meta.get("name", f.stem),
            "description": meta.get("description", ""),
            "tools": meta.get("allowed-tools") or meta.get("tools") or "",
        })
    return out


def _skills_mcp() -> list[dict]:
    """MCP-серверы с расшифровкой возможностей."""
    out = []
    for s in discover_mcp():
        status = ("connected" if s.get("connected")
                  else "needs_auth" if s.get("needs_auth") else "offline")
        out.append({
            "name": s.get("name", ""),
            "transport": s.get("transport", ""),
            "target": s.get("target", ""),
            "status": status,
            "capabilities": _mcp_blurb(s.get("name", "")),
        })
    return out


def _skills_integrations() -> list[dict]:
    """Чеклист возможностей самого prof (доступно/нет)."""
    import vectors
    vault = obsidian_vault()
    obsidian_ok = bool(db.get_setting("obsidian_vault")) or vault.exists()
    tg = bool(os.environ.get("PROF_TG_BOT_TOKEN") or db.get_setting("tg_bot_token"))
    has_remote = False
    try:
        has_remote = bool(subprocess.run(
            ["git", "remote"], cwd=str(PROF), capture_output=True,
            text=True, timeout=5).stdout.strip())
    except Exception:
        pass
    return [
        {"key": "coworking", "name": "Коворкинг (headless claude)",
         "available": True, "note": "запуск задач агентом без API-ключа"},
        {"key": "validator", "name": "Детерминированный валидатор",
         "available": True, "note": "проверка результата карточки по критериям"},
        {"key": "review", "name": "Агентное ревью",
         "available": True, "note": "ревью-агент по завершённым задачам"},
        {"key": "analyze", "name": "Авто-анализ слабых мест",
         "available": True, "note": "предложения задач из памяток проектов"},
        {"key": "vector", "name": "Vector-поиск",
         "available": vectors.available(),
         "note": "семантический поиск (fastembed + sqlite-vec)"},
        {"key": "obsidian", "name": "Obsidian-синк",
         "available": obsidian_ok, "note": str(vault)},
        {"key": "uptime", "name": "Uptime-пинг сервисов",
         "available": True, "note": "периодическая проверка статуса"},
        {"key": "git", "name": "Git-бэкап",
         "available": True, "note": "remote настроен" if has_remote else "только локальные коммиты"},
        {"key": "telegram", "name": "Telegram-уведомления",
         "available": tg, "note": "уведомления об ожидании/завершении"},
    ]


def discover_skills() -> dict:
    """Обзор подключённого: MCP, хуки, команды, скилы, сабагенты, интеграции."""
    return {
        "mcp": _skills_mcp(),
        "hooks": _skills_hooks(),
        "commands": _skills_commands(),
        "skills": _skills_skills(),
        "agents": _skills_agents(),
        "integrations": _skills_integrations(),
    }
