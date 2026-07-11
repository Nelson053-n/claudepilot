"""
Векторная память prof: локальные эмбеддинги (fastembed/ONNX) + sqlite-vec.
Индексирует памятки/KB/задачи → семантический поиск. Синк в Obsidian-vault.

Модель грузится лениво при первом обращении (старт сервиса остаётся быстрым).
При отсутствии зависимостей деградирует в no-op (поиск вернёт пусто).
"""
from __future__ import annotations

import struct
from pathlib import Path

VEC_DB = Path(__file__).parent / "vectors.db"
_MODEL = None
_DIM = 384  # BAAI/bge-small-en-v1.5

_AVAILABLE = True
try:
    import sqlite3
    import sqlite_vec
    from fastembed import TextEmbedding
except Exception:  # pragma: no cover
    _AVAILABLE = False


def available() -> bool:
    return _AVAILABLE


def _model():
    global _MODEL
    if _MODEL is None:
        _MODEL = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
    return _MODEL


def _embed(text: str) -> list[float]:
    return list(_model().embed([text]))[0].tolist()


def _conn():
    c = sqlite3.connect(VEC_DB, timeout=30)  # ждать разблокировки, не падать сразу (см. db._conn)
    c.enable_load_extension(True)
    sqlite_vec.load(c)
    c.enable_load_extension(False)
    return c


def init():
    if not _AVAILABLE:
        return
    c = _conn()
    c.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_items USING vec0(embedding float[{_DIM}])"
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS vec_meta(
            rowid INTEGER PRIMARY KEY,
            kind TEXT, ref TEXT, title TEXT, text TEXT
        )"""
    )
    c.commit()
    c.close()


def _serialize(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)


def index_doc(kind: str, ref: str, title: str, text: str) -> None:
    """Добавляет/обновляет документ в индексе. ref — уникальный ключ (путь/файл)."""
    if not _AVAILABLE:
        return
    c = _conn()
    old = c.execute("SELECT rowid FROM vec_meta WHERE ref=?", (ref,)).fetchone()
    if old:
        c.execute("DELETE FROM vec_items WHERE rowid=?", (old[0],))
        c.execute("DELETE FROM vec_meta WHERE rowid=?", (old[0],))
    emb = _embed(f"{title}\n{text}"[:2000])
    cur = c.execute("INSERT INTO vec_meta(kind,ref,title,text) VALUES(?,?,?,?)",
                    (kind, ref, title, text[:500]))
    rid = cur.lastrowid
    c.execute("INSERT INTO vec_items(rowid,embedding) VALUES(?,?)", (rid, _serialize(emb)))
    c.commit()
    c.close()


def search(query: str, k: int = 15) -> list[dict]:
    if not _AVAILABLE:
        return []
    c = _conn()
    try:
        emb = _embed(query)
        rows = c.execute(
            """SELECT m.kind,m.ref,m.title,m.text,v.distance
               FROM vec_items v JOIN vec_meta m ON m.rowid=v.rowid
               WHERE v.embedding MATCH ? AND k=?
               ORDER BY v.distance""",
            (_serialize(emb), k),
        ).fetchall()
    finally:
        c.close()
    return [{"kind": r[0], "ref": r[1], "title": r[2], "snippet": r[3],
             "score": round(1 - r[4], 3)} for r in rows]


def count() -> int:
    if not _AVAILABLE:
        return 0
    c = _conn()
    try:
        return c.execute("SELECT COUNT(*) FROM vec_meta").fetchone()[0]
    except Exception:
        return 0
    finally:
        c.close()
