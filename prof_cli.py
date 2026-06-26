#!/usr/bin/env python3
"""
prof_cli — тонкий локальный CLI к доске prof (http://127.0.0.1:7777).

Даёт Claude Code управлять канбаном изнутри сессии: ставить задачи в 'Предложено',
видеть статусы/колонки/стоимость и бюджет, запускать/закрывать карточки. Это
позволяет master-агенту декомпозировать цель и детерминированно оркестрировать
подзадачи (нулевой токен-оверхед на саму координацию после декомпозиции).

stdlib only. Токен — из env PROF_TOKEN (мутации требуют Bearer). Хост — из
PROF_URL (по умолчанию http://127.0.0.1:7777, только локально).

Примеры:
    prof list --board -home-nel-prof
    prof add --board -home-nel-prof --title "Починить X" --prompt "Сделай Y"
    prof status --json
    prof run 42
    prof done 42
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("PROF_URL", "http://127.0.0.1:7777").rstrip("/")
TOKEN = os.environ.get("PROF_TOKEN", "")


def _req_raw(method: str, path: str, body: dict | None = None) -> object:
    """Сетевой запрос; бросает HTTPError/URLError (для опциональных вызовов)."""
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else {}


def _req(method: str, path: str, body: dict | None = None) -> object:
    """Как _req_raw, но при ошибке печатает и завершает процесс (для основных команд)."""
    try:
        return _req_raw(method, path, body)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except Exception:
            pass
        _die(f"HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        _die(f"prof недоступен на {BASE}: {e.reason}")


def _die(msg: str) -> None:
    print(msg, file=sys.stderr)
    sys.exit(1)


def _out(obj: object, as_json: bool, render) -> None:
    """Печатает JSON (--json) либо человекочитаемо через render(obj)."""
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        render(obj)


# ---------- board-резолвер ----------
def _resolve_board(ref: str) -> int:
    """ref → id доски. Числовой ref — как есть. slug/имя — ищет в /api/boards,
    при отсутствии создаёт через ensure_board (POST /api/boards)."""
    if ref.isdigit():
        return int(ref)
    data = _req("GET", "/api/boards")
    for b in data.get("boards", []):
        if b.get("slug") == ref or b.get("name") == ref:
            return b["id"]
    # нет доски — создаём (по slug проекта или по имени ручной доски)
    if ref.startswith("-home-nel"):
        name = ref[len("-home-nel"):].lstrip("-") or "(root)"
        b = _req("POST", "/api/boards", {"name": name, "slug": ref})
    else:
        b = _req("POST", "/api/boards", {"name": ref})
    return b["id"]


# ---------- команды ----------
def cmd_list(a) -> None:
    board_id = _resolve_board(a.board) if a.board else None
    cards = _req("GET", "/api/cards" + (f"?board_id={board_id}" if board_id else ""))

    def render(cs):
        if not cs:
            print("(карточек нет)")
            return
        for c in cs:
            cost = c.get("cost_usd")
            cost_s = f"  ${cost:.4f}" if cost else ""
            print(f"#{c['id']:>4}  [{c['column']:<11}] {c['status']:<8}  "
                  f"{c['title']}{cost_s}")

    _out(cards, a.json, render)


def cmd_add(a) -> None:
    board_id = _resolve_board(a.board)
    body = {
        "board_id": board_id,
        "title": a.title,
        "prompt": a.prompt,
        "column": a.column,
        "origin": "agent",
    }
    # board как slug проекта → карточка наследует slug (для оценки стоимости по проекту)
    if a.board.startswith("-home-nel"):
        body["slug"] = a.board
    card = _req("POST", "/api/cards", body)

    def render(c):
        print(f"создана карточка #{c['id']} [{c['column']}] {c['title']}")

    _out(card, a.json, render)


def _try(path):
    """GET, который не валит всю сводку, если эндпоинт недоступен (404/старый сервер)."""
    try:
        return _req_raw("GET", path)
    except Exception:
        return None


def cmd_status(a) -> None:
    stats = _req("GET", "/api/cards/stats")
    cost = _req("GET", "/api/cards/cost")
    usage = _try("/api/usage")
    sessions = _try("/api/cost/sessions")
    summary = {
        "cards": stats,
        "cost": {
            "total_cost_usd": cost.get("total_cost_usd"),
            "total_review_cost_usd": cost.get("total_review_cost_usd"),
            "total_input": cost.get("total_input"),
            "total_output": cost.get("total_output"),
            "avg_cost_usd": cost.get("avg_cost_usd"),
            "by_project": cost.get("by_project", []),
        },
        "usage": usage,
        "sessions": sessions,
    }

    def render(s):
        c = s["cards"]
        print(f"карточки: всего {c['total']}  running {c['running']}  "
              f"proposed {c['proposed']}  done {c['done']}  failed {c['failed']}")
        co = s["cost"]
        print(f"затраты задач: ${co['total_cost_usd']}  "
              f"(проверки ${co['total_review_cost_usd']})  "
              f"in {co['total_input']} / out {co['total_output']} ток.")
        u = s.get("usage") or {}
        fh = u.get("five_hour") or {}
        if fh:
            print(f"лимит 5h: {fh.get('util')}% использовано, осталось {fh.get('left')}")
        if co["by_project"]:
            print("по проектам:")
            for p in co["by_project"][:8]:
                print(f"  {p['project']:<24} ${p['total_usd']:<8} "
                      f"задач {p['tasks']}")

    _out(summary, a.json, render)


def cmd_done(a) -> None:
    card = _req("POST", f"/api/cards/{a.cid}/done")
    _out(card, a.json, lambda c: print(f"#{c['id']} → done"))


def cmd_run(a) -> None:
    card = _req("POST", f"/api/cards/{a.cid}/run")
    _out(card, a.json, lambda c: print(f"#{c['id']} запущена ({c['status']})"))


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="prof", description="CLI к доске prof")
    p.add_argument("--json", action="store_true", help="вывод в JSON")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="карточки со статусом/колонкой/стоимостью")
    pl.add_argument("--board", help="id, slug проекта (-home-nel-…) или имя доски")
    pl.set_defaults(func=cmd_list)

    pa = sub.add_parser("add", help="создать карточку")
    pa.add_argument("--board", required=True,
                    help="id, slug проекта (-home-nel-…) или имя доски (создастся)")
    pa.add_argument("--title", required=True)
    pa.add_argument("--prompt", default="", help="задание для агента")
    pa.add_argument("--column", default="proposed",
                    help="колонка (по умолчанию proposed)")
    pa.set_defaults(func=cmd_add)

    ps = sub.add_parser("status", help="сводка карточек + затраты + бюджет 5h")
    ps.set_defaults(func=cmd_status)

    pd = sub.add_parser("done", help="пометить карточку выполненной")
    pd.add_argument("cid", type=int)
    pd.set_defaults(func=cmd_done)

    pr = sub.add_parser("run", help="запустить агента по карточке")
    pr.add_argument("cid", type=int)
    pr.set_defaults(func=cmd_run)

    a = p.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
