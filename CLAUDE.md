# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Что это

`prof` — личная система-дашборд: канбан-доски проектов + само-оркестрация headless-агентов
Claude Code. Карточка с заданием подтверждается → prof запускает `claude -p ...` как
дочерний процесс, следит за прогрессом, валидирует результат, считает токены/стоимость и
сводит расход по проектам/моделям с учётом бюджета 5h/7d-окон подписки.

## Команды

```bash
# Запуск (прод — через systemd unit prof.service, см. prof.service.bak)
./start.sh                              # = .venv/bin/python app.py  (127.0.0.1:7777)
PROF_HOST=0.0.0.0 .venv/bin/python app.py   # слушать в сети

# Тесты
.venv/bin/python -m pytest              # все (testpaths=tests в pytest.ini)
.venv/bin/python -m pytest tests/test_validate.py -q          # один файл
.venv/bin/python -m pytest tests/test_cli.py::test_add -q     # один тест
```

Тесты бьют по `TestClient(app)` (fixture `client` в `tests/conftest.py`); токен жёстко
задан `PROF_TOKEN=test-token-fixed` **до** импорта `app` — `get_token()` читает env первым.
Тесты не должны спавнить реальные `claude`-процессы — мокай `svc.start_card`/`_spawn_card`.

## Окружение

- Виртуалка `.venv/`, зависимости в `requirements.txt` (FastAPI, uvicorn, sqlite-vec, fastembed).
- `PROF_TOKEN` — Bearer для мутаций (POST/PUT/PATCH/DELETE). GET открыты. Если не задан —
  токен генерится и хранится в settings, рендерится в `index.html` через `__PROF_TOKEN__`.
- `PROF_HOST`/`PROF_PORT` — хост/порт. `PROF_RELOAD=0` — выключить watch-reload (тесты/прямой запуск).

## Архитектура

Бэкенд: один процесс FastAPI. Четыре Python-модуля + один HTML-файл фронта (vanilla, без сборки).

- **`app.py`** — все HTTP-роуты + auth-middleware + lifespan (старт reaper'а и git-автобэкапа).
  Тут же чтение памяти/KB из `~/.claude/projects/<slug>/memory/` и `~/claude-memory-compiler/`,
  агрегации стоимости (`_cost_by_project`, `_cards_by_model`, `/api/cost/combined`).
- **`db.py`** — SQLite-слой (`prof.db`, WAL). Таблицы boards/cards/services/mcp/settings/projects.
  Схема расширяется через идемпотентные `ALTER TABLE ... ADD COLUMN` в `init_db()` — **новое поле
  карточки = добавить в этот список миграций И в `CARD_FIELDS`** (иначе `update_card` отвергнет).
- **`services.py`** — вся «грязная» работа: спавн/валидация/реап карточек, git-worktree,
  модель-роутинг, usage/quota, uptime-пинги, MCP/skills-дискавери, obsidian-синк, Telegram.
- **`sessions.py`** — парсинг `*.jsonl` live-сессий Claude из `~/.claude/projects` для расчёта
  стоимости (`PRICES`, `_line_cost`), окон 5h (`window_usage`) и разбивки квоты (`quota_breakdown`).
- **`vectors.py`** — опциональный векторный поиск памяти (sqlite-vec + fastembed BAAI/bge-small,
  `vectors.db`). Деградирует мягко: `vectors.available()` → False, текстовый поиск как фолбэк.
- **`prof_cli.py`** — тонкий stdlib-CLI к HTTP-API (для управления доской из сессии Claude;
  см. также skill `~/.claude/skills/prof/SKILL.md`). Бьёт на `PROF_URL`, токен из `PROF_TOKEN`.

### Жизненный цикл карточки (ключевой механизм)

Колонки: `proposed → approved → in_progress → review → done` (+ `rejected`). Поток:

1. Карточка попадает в `approved` (drag в UI / `POST /api/cards` column=approved / авто-анализ).
   → `start_card` проверяет WIP-лимит и parallelism-лок проекта.
2. `_spawn_card` запускает `claude -p <prompt> --session-id <uuid> --model <m>
   --dangerously-skip-permissions --output-format stream-json` дочерним процессом
   (`start_new_session=True` — переживает рестарт сервиса). Вывод → `runs/card_<cid>.out` (NDJSON),
   код возврата → `runs/card_<cid>.rc` по завершении.
3. Фоновый **reaper** (`start_reaper`, тик 3с → `refresh_running_cards`) видит `.rc`, парсит
   итоговый `result`-event (usage/cost), прогоняет детерминированную валидацию (`validate_card`:
   тесты/линт/git-дельта vs `test_baseline`) и двигает карточку в `review`/`done` либо назад при провале.
4. Live-прогресс — SSE `/api/cards/<cid>/stream` (дельты `read_progress`). «Агент ждёт ввода» —
   маркеры `runs/waiting/<sid>.json`, которые пишет хук `hooks/prof_waiting.sh` (Notification/
   PreToolUse↑, PostToolUse/Stop↓), матчатся на карточку по `cards.session_dir`.

### Параллелизм и гонки (важно при правках спавна)

- **WIP-лимит** (`settings.wip_limit`, дефолт 3) — потолок одновременно бегущих задач; сверх —
  `queued`, reaper добирает через `start_next_queued`.
- **parallelism** (`settings.parallelism`: `project`|`worktree`|`off`). `project` — одна задача
  на проект (`project_busy`-лок). `worktree` — изолированная git-копия (ветка `prof-card-<cid>`),
  результат вливается мерджем в `refresh_running_cards`. Self-проект prof и не-git папки → общий cwd.
- **Дедуп старта**: два пути защиты от двойного процесса — `claim_card_run` (внешние /run,/move →
  `starting`) и `claim_spawn` (внутренние пути reaper'а → атомарно `running` ДО медленных шагов,
  инцидент #77). Любая правка `_spawn_card` обязана сохранить этот атомарный барьер.

### Бюджет / модель-роутинг

- Задачи по умолчанию на **Sonnet** (`_MODELS["task"]`); Opus только при маркере сложности
  в title/prompt (`_OPUS_MARKERS`: `[opus]`,`[сложно]`,...) или override `settings.model:task`.
- Старт новых задач блокируется при утилизации 5h-окна > `WINDOW_UTIL_LIMIT` (дефолт 85%) — задача
  ждёт в очереди; авто-анализ тоже скипается выше порога (`ANALYZE_UTIL_LIMIT`). Usage кэшируется
  5 мин (`runs/.usage_cache.json`, переживает рестарт).

### Reload без полного рестарта

uvicorn следит **только** за `runs/.reload-trigger` (не за `*.py`), иначе правка кода
агентом-коворкером посреди задачи пересоздавала бы worker → гонка reaper'а. Применить свежий
код: `POST /api/apply-code` (touch trigger) когда нет активных задач, либо `POST /api/restart`.

## Рабочие файлы (gitignored)

`runs/` — рантайм: `card_<cid>.out`/`.rc` (вывод/код задач), `waiting/` (маркеры ожидания),
`.usage_cache.json`, `.reload-trigger`. `vectors.db` тоже не в git. `prof.db` — в git
(автокоммит «prof autosave» каждые 10 мин; коммиты в истории — это бэкап состояния, не код).
