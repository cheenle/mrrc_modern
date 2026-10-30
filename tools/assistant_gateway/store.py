"""会话库：多轮上下文、全量留存、180 天硬删、按实例删除、配额计数。

`clock` 可注入 ⇒ 「180 天」与「今天」都能用假时间测，不需要 sleep 也不需要 tz 环境。
与 `ft8/engine/repository.py` 同一形态（sqlite3 + check_same_thread=False + Row）。

留存口径（规格 §7）：`turns` 是唯一的事实来源，`conversations` 只是它们的归属；
删除一律**硬删**（客户点「清空会话」立刻生效，不留墓碑、不留匿名化的副本）。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Callable

RETENTION_DAYS = 180

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, lang TEXT NOT NULL,
  created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS turns (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conv_id TEXT NOT NULL, role TEXT NOT NULL,
  content TEXT NOT NULL, mode TEXT NOT NULL DEFAULT '', citations TEXT NOT NULL DEFAULT '[]',
  needs_confirm INTEGER NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0,
  ms INTEGER NOT NULL DEFAULT 0, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS feedback (
  turn_id INTEGER PRIMARY KEY, useful INTEGER NOT NULL,
  note TEXT NOT NULL DEFAULT '', ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS turns_conv ON turns(conv_id, id);
CREATE INDEX IF NOT EXISTS turns_ts ON turns(ts);
"""


def _decode(raw: str) -> list:
    """citations 列是 JSON 文本；坏数据当空列表，不让一条脏行炸掉整段历史。"""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


class Store:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._db = sqlite3.connect(self._path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # ── conversations ─────────────────────────────────────────
    def create_conversation(self, instance_id: str, lang: str) -> str:
        now = self._clock()
        # 时间戳 + 实例后缀：同一毫秒里两次新建也不会撞（测试里就是这么用的）。
        cid = f"c-{now:.3f}-{instance_id[-6:]}"
        suffix = 1
        while self._db.execute("SELECT 1 FROM conversations WHERE id=?", (cid,)).fetchone():
            cid = f"c-{now:.3f}-{instance_id[-6:]}-{suffix}"
            suffix += 1
        self._db.execute("INSERT INTO conversations VALUES (?,?,?,?,?)",
                         (cid, instance_id, lang, now, now))
        self._db.commit()
        return cid

    def conversation(self, conv_id: str) -> dict | None:
        row = self._db.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
        return dict(row) if row else None

    # ── turns ─────────────────────────────────────────────────
    def append_turn(self, conv_id: str, role: str, content: str, *,
                    mode: str = "", citations: list | None = None,
                    needs_confirm: bool = False, tokens: int = 0, ms: int = 0) -> int:
        now = self._clock()
        cur = self._db.execute(
            "INSERT INTO turns (conv_id, role, content, mode, citations, needs_confirm,"
            " tokens, ms, ts) VALUES (?,?,?,?,?,?,?,?,?)",
            (conv_id, role, content, mode, json.dumps(citations or [], ensure_ascii=False),
             1 if needs_confirm else 0, tokens, ms, now))
        self._db.execute("UPDATE conversations SET updated=? WHERE id=?", (now, conv_id))
        self._db.commit()
        return cur.lastrowid or 0

    def history(self, conv_id: str, limit: int = 3) -> list:
        """最近 `limit` 轮，按时间正序 —— 引擎直接把它拼进 messages。"""
        rows = self._db.execute(
            "SELECT * FROM turns WHERE conv_id=? ORDER BY id DESC LIMIT ?",
            (conv_id, limit)).fetchall()
        return [{**dict(row), "citations": _decode(row["citations"])}
                for row in reversed(rows)]

    def delete_conversation(self, conv_id: str) -> int:
        cur = self._db.execute("DELETE FROM turns WHERE conv_id=?", (conv_id,))
        self._db.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
        self._db.commit()
        return cur.rowcount or 0

    # ── retention / erasure ───────────────────────────────────
    def purge_expired(self) -> int:
        """删掉 180 天没动过的会话，返回删掉的会话数。"""
        cutoff = self._clock() - RETENTION_DAYS * 86400
        stale = [r["id"] for r in self._db.execute(
            "SELECT id FROM conversations WHERE updated < ?", (cutoff,))]
        for cid in stale:
            self.delete_conversation(cid)
        return len(stale)

    def delete_instance(self, instance_id: str) -> int:
        """整台实例的会话一次删净（换机器 / 停用）。"""
        convs = [r["id"] for r in self._db.execute(
            "SELECT id FROM conversations WHERE instance_id=?", (instance_id,))]
        for cid in convs:
            self.delete_conversation(cid)
        return len(convs)

    # ── quota ─────────────────────────────────────────────────
    def count_misses_today(self, instance_id: str) -> int:
        """今天这台实例用掉多少次「兜底生成」—— 配额只扣这里（规格 §5）。"""
        day = 86400
        now = self._clock()
        since = now - (now % day)
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM turns t JOIN conversations c ON c.id=t.conv_id"
            " WHERE c.instance_id=? AND t.mode='generated' AND t.ts>=?",
            (instance_id, since)).fetchone()
        return row["n"]

    # ── feedback ──────────────────────────────────────────────
    def add_feedback(self, turn_id: int, useful: bool, note: str = "") -> None:
        self._db.execute("INSERT OR REPLACE INTO feedback VALUES (?,?,?,?)",
                         (turn_id, 1 if useful else 0, note, self._clock()))
        self._db.commit()
