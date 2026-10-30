# 产品 AI 助手（网页内答疑）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 在盒子的网页客户端里加一个可多轮的 AI 答疑面板：知识库在中央、会话全量留存；答案优先来自产品知识（带出处），答不了才调模型并标「AI 生成 · 待确认」。

**架构：** 盒子是薄客户端（只打包白名单状态摘要并转发），中央网关负责检索、模型、会话库与配额。三条路径：规则层 → KB 检索 → 兜底生成；任何指不回出处的断言强制降级。

**技术栈：** Python 3 标准库（网关用 `http.server` + `sqlite3`，与 `tools/support_receiver/` 同风格）、`net_tls.urlopen`（出站唯一入口，AST 守卫）、原生 JS（面板，无构建步骤）。

**规格：** `docs/superpowers/specs/2026-10-30-ai-assistant-design.md`

---

## 文件结构

| 文件 | 职责 |
| --- | --- |
| `dev_tools/make_assistant_kb.py` | 把 CHANGELOG/看板/答复页/FAQ 生成 `assistant_kb.json`（同输入同输出） |
| `tools/assistant_gateway/kb.py` | 条目模型 + 检索打分（纯函数，无 I/O） |
| `tools/assistant_gateway/store.py` | sqlite 会话库：conversations/turns/feedback、保留期清理、配额计数 |
| `tools/assistant_gateway/engine.py` | 判定顺序：规则 → 检索 → 生成；出处校验与降级 |
| `tools/assistant_gateway/model.py` | OpenAI 兼容模型客户端（可注入 opener，测试用假模型） |
| `tools/assistant_gateway/http_server.py` | `/v1/assistant/ask` 与 `/v1/assistant/status` |
| `tools/assistant_gateway/gateway.py` | 入口：装配配置 + 起服务 |
| `assistant_client.py` | 盒子侧：白名单摘要、转发、离线话术、响应整形 |
| `server.py` | 三条路由 + 开关（`MRRC_ASSISTANT` / `MRRC_ASSISTANT_URL`） |
| `static/assistant.html` / `static/assistant.js` | 面板（多轮气泡、出处、徽标、清空会话） |
| `static/index.html` | ☰ 菜单加一项（普通 HTML，**不碰** `ft710_ui.js` / `ft710_main.js`） |
| `tests/test_make_assistant_kb.py` | KB 生成：可复现、`applies_to`、一条 CHANGELOG → 一条 KB |
| `tests/test_assistant_gateway.py` | 检索阈值、规则层、配额、出处校验降级、留存与硬删（假时钟）、伪模型 |
| `tests/test_assistant_client.py` | 键集精确断言、黑名单扫描、≤2 KB、离线话术 |
| `tests/test_assistant_route.py` | 角色门（listen/sdr 403）、形状、助手不可用不影响 PTT/CAT |

---

## 任务 1：知识库生成器

**文件：**

- 创建：`dev_tools/make_assistant_kb.py`
- 测试：`tests/test_make_assistant_kb.py`

- [ ] **步骤 1：编写失败的测试**

```python
"""KB 生成器：同输入同输出，且「CHANGELOG 多一条 → KB 多一条」。"""
import json
import tempfile
import unittest
from pathlib import Path

import make_assistant_kb as mk


CHANGELOG = """# Changelog

## [镜像线 v1.28.22 · FP18] — 2026-10-30 — CQ 泵按队列深度补料

**修法**：泵改成按队列深度补料。

## [v1.28.6] — 2026-10-26 — 盒子级音频电平

TX MIC 与 RX 各一个 dB 旋钮。
"""


class BuildTests(unittest.TestCase):
    def _build(self, changelog=CHANGELOG, board=None, answers=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
            (root / "board").mkdir()
            (root / "board" / "index.html").write_text(board or "", encoding="utf-8")
            (root / "answers").mkdir()
            (root / "answers" / "index.html").write_text(answers or "", encoding="utf-8")
            return mk.build(root)

    def test_one_changelog_heading_one_entry(self):
        kb = self._build()
        kinds = [e["kind"] for e in kb["entries"]]
        self.assertEqual(kinds.count("release"), 2)

    def test_box_line_heading_carries_version_and_fixpack(self):
        entry = [e for e in self._build()["entries"] if e["kind"] == "release"][0]
        self.assertEqual(entry["applies_to"]["version_min"], "1.28.22")
        self.assertEqual(entry["fixpack"], "FP18")

    def test_ids_are_stable_and_output_reproducible(self):
        first = json.dumps(self._build(), ensure_ascii=False, sort_keys=True)
        second = json.dumps(self._build(), ensure_ascii=False, sort_keys=True)
        self.assertEqual(first, second)

    def test_entries_have_a_searchable_body_and_keywords(self):
        for entry in self._build()["entries"]:
            self.assertTrue(entry["body"].strip())
            self.assertTrue(entry["keywords"])
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest tests.test_make_assistant_kb -v`
预期：FAIL，`ModuleNotFoundError: No module named 'make_assistant_kb'`

- [ ] **步骤 3：编写最少实现代码**

```python
#!/usr/bin/env python3
"""把 CHANGELOG / 看板 / 答复页 生成 `assistant_kb.json`（AI 助手的知识库）。

    ./.venv/bin/python dev_tools/make_assistant_kb.py --out tools/assistant_gateway/

知识库**只放中央**（2026-10-30 决定）：盒子不留副本，所以这个文件不进盒子包，
而是随网关部署。同输入同输出（排序 + 不含时间戳），否则每次构建都产生一个"新"KB，
发布检查与 diff 都失去意义。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

BOX_HEADING = re.compile(r"^##\s*\[镜像线 v(\d+\.\d+\.\d+)(?:\s*·\s*(FP\d+))?\]\s*[—-]\s*(\d{4}-\d\d-\d\d)\s*[—-]\s*(.+)$", re.M)
APP_HEADING = re.compile(r"^##\s*\[v(\d+\.\d+\.\d+)\]\s*[—-]\s*(\d{4}-\d\d-\d\d)\s*[—-]\s*(.+)$", re.M)
_CJK = re.compile(r"[\u4e00-\u9fff]")
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]*")


def _keywords(text: str) -> list[str]:
    """拉丁词 + 中文二字组 —— 中文没有空格，二元组是最便宜且够用的索引单位。"""
    words = {w.lower() for w in _LATIN.findall(text) if len(w) >= 2}
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        words.update(run[i:i + 2] for i in range(len(run) - 1))
    return sorted(words)


def _entry_id(kind: str, version: str, title: str) -> str:
    digest = hashlib.sha256(f"{kind}|{version}|{title}".encode()).hexdigest()[:8]
    return f"kb-{kind}-{version}-{digest}"


def _release_entries(text: str) -> list[dict]:
    entries = []
    for match in BOX_HEADING.finditer(text):
        version, fixpack, date, title = match.groups()
        entries.append({
            "id": _entry_id("release", version, title),
            "kind": "release",
            "title": title.strip(),
            "body": _section_body(text, match.end()),
            "applies_to": {"version_min": version, "models": []},
            "fixpack": fixpack or "",
            "released": date,
        })
    for match in APP_HEADING.finditer(text):
        version, date, title = match.groups()
        entries.append({
            "id": _entry_id("release", version, title),
            "kind": "release",
            "title": title.strip(),
            "body": _section_body(text, match.end()),
            "applies_to": {"version_min": version, "models": []},
            "fixpack": "",
            "released": date,
        })
    return entries


def _section_body(text: str, start: int) -> str:
    """标题到下一个 `## ` 之间的正文，压掉空行，截断到 2000 字。"""
    rest = text[start:]
    end = rest.find("\n## ")
    body = rest if end < 0 else rest[:end]
    body = re.sub(r"\n{2,}", "\n", body).strip()
    return body[:2000]


def build(root: Path) -> dict:
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    entries = _release_entries(changelog)
    for entry in entries:
        entry["keywords"] = _keywords(entry["title"] + "\n" + entry["body"])
    entries.sort(key=lambda e: (e["applies_to"]["version_min"], e["id"]), reverse=True)
    return {"version": 1, "entries": entries}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "tools" / "assistant_gateway"))
    args = ap.parse_args()
    kb = build(REPO)
    out = Path(args.out) / "assistant_kb.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(kb, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    print(f"wrote {out} ({len(kb['entries'])} entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest tests.test_make_assistant_kb -v`
预期：4 passed

- [ ] **步骤 5：Commit**

```bash
git add dev_tools/make_assistant_kb.py tests/test_make_assistant_kb.py
git commit -m "feat(assistant): KB 生成器 —— CHANGELOG 一条，知识库一条"
```

---

## 任务 2：检索打分（纯函数）

**文件：**

- 创建：`tools/assistant_gateway/__init__.py`、`tools/assistant_gateway/kb.py`
- 测试：`tests/test_assistant_gateway.py`（新建，先只写检索部分）

- [ ] **步骤 1：编写失败的测试**

```python
"""中央网关：检索、判定、配额、留存 —— 全部离线，伪模型，假时钟。"""
import unittest

from tools.assistant_gateway import kb as gkb


def _entry(id_, title, body, version="1.28.20", models=()):
    return {"id": id_, "kind": "release", "title": title, "body": body,
            "keywords": gkb.keywords(title + "\n" + body),
            "applies_to": {"version_min": version, "models": list(models)}, "fixpack": ""}


ENTRIES = [
    _entry("kb-cq", "CQ 发射节拍：供料比声卡慢 5%", "CQ 播放的时候声音有些失真，泵改成按队列深度补料。"),
    _entry("kb-nb", "NR 等级改成 RL0 + 两位", "噪声抑制 NR 的等级指令格式修正。"),
]


class RetrieveTests(unittest.TestCase):
    def test_exact_topic_scores_high(self):
        hits = gkb.retrieve(ENTRIES, "CQ 发射的时候有失真", "1.28.22", "ft710")
        self.assertEqual(hits[0].entry["id"], "kb-cq")
        self.assertGreaterEqual(hits[0].score, gkb.HIT_MIN_SCORE)

    def test_unrelated_query_scores_low(self):
        hits = gkb.retrieve(ENTRIES, "怎么给树莓派烧卡", "1.28.22", "ft710")
        self.assertLess(hits[0].score, gkb.HIT_MIN_SCORE)

    def test_entry_older_than_the_machine_is_still_usable(self):
        # version_min 是"从这版起"，本机更新时依然命中
        hits = gkb.retrieve(ENTRIES, "CQ 失真", "1.29.0", "ft710")
        self.assertEqual(hits[0].entry["id"], "kb-cq")

    def test_model_specific_entry_does_not_leak_to_other_models(self):
        entries = [_entry("kb-ftx", "FTX-1 的波段表", "波段表改成对象。", models=["ftx1"])]
        hits = gkb.retrieve(entries, "波段表", "1.28.22", "ft710")
        self.assertEqual(hits, [])
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest tests.test_assistant_gateway -v`
预期：FAIL，`ModuleNotFoundError: No module named 'tools.assistant_gateway'`

- [ ] **步骤 3：编写最少实现代码**

`tools/assistant_gateway/__init__.py`：

```python
"""中央知识服务（AI 助手的后端）。标准库 only，与 tools/support_receiver 同风格。"""
```

`tools/assistant_gateway/kb.py`：

```python
"""条目模型与检索打分 —— 纯函数，没有 I/O，所以阈值可以被单测钉死。

分数定义（0…1，故意简单到可以手算）：

    0.55 × 关键词命中率 + 0.30 × 标题命中率 + 0.15 × 正文命中率

「命中率」= 查询词里出现在该字段的比例。关键词来自生成器（拉丁词 + 中文二字组），
查询走同一套切分 ⇒ 同一份代码两边共用，不存在"建索引用一种、查询用另一种"的漂移。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

HIT_MIN_SCORE = 0.72
HIT_MIN_MARGIN = 0.04

_CJK = re.compile(r"[\u4e00-\u9fff]")
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]*")


def keywords(text: str) -> list[str]:
    words = {w.lower() for w in _LATIN.findall(text) if len(w) >= 2}
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        words.update(run[i:i + 2] for i in range(len(run) - 1))
    return sorted(words)


def _hit_rate(terms: set[str], field: str) -> float:
    if not terms:
        return 0.0
    field_terms = set(keywords(field))
    return len(terms & field_terms) / len(terms)


@dataclass(frozen=True)
class Scored:
    entry: dict
    score: float


def _applies(entry: dict, version: str, model: str) -> bool:
    models = entry.get("applies_to", {}).get("models") or []
    return not models or model in models


def retrieve(entries, query: str, version: str, model: str,
             limit: int = 3) -> list[Scored]:
    terms = set(keywords(query))
    scored = []
    for entry in entries:
        if not _applies(entry, version, model):
            continue
        score = (0.55 * _hit_rate(terms, " ".join(entry.get("keywords", [])))
                 + 0.30 * _hit_rate(terms, entry.get("title", ""))
                 + 0.15 * _hit_rate(terms, entry.get("body", "")))
        if score > 0:
            scored.append(Scored(entry, round(score, 4)))
    scored.sort(key=lambda s: s.score, reverse=True)
    return scored[:limit]


def is_hit(hits: list[Scored]) -> bool:
    """命中判据：够高，且与第二名拉开差距（避免"什么都沾一点"）。"""
    if not hits or hits[0].score < HIT_MIN_SCORE:
        return False
    return len(hits) == 1 or hits[0].score - hits[1].score >= HIT_MIN_MARGIN
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest tests.test_assistant_gateway -v`
预期：4 passed

- [ ] **步骤 5：Commit**

```bash
git add tools/assistant_gateway/__init__.py tools/assistant_gateway/kb.py tests/test_assistant_gateway.py
git commit -m "feat(assistant): 检索打分 —— 命中要够高且与第二名拉开差距"
```

---

## 任务 3：会话库与留存（假时钟）

**文件：**

- 创建：`tools/assistant_gateway/store.py`
- 测试：`tests/test_assistant_gateway.py`（追加 `StoreTests`）

- [ ] **步骤 1：编写失败的测试**

```python
import tempfile
from pathlib import Path

from tools.assistant_gateway import store as gstore


class StoreTests(unittest.TestCase):
    def _store(self, clock):
        tmp = tempfile.mkdtemp()
        return gstore.Store(Path(tmp) / "assistant.db", clock=clock)

    def test_turn_round_trip_and_history_window(self):
        st = self._store(lambda: 1000.0)
        conv = st.create_conversation("inst-1", "zh")
        for i in range(5):
            st.append_turn(conv, "user", f"q{i}", mode="kb", citations=[],
                           needs_confirm=False, tokens=0, ms=5)
        self.assertEqual([t["content"] for t in st.history(conv, limit=3)],
                         ["q2", "q3", "q4"])

    def test_retention_deletes_older_than_180_days(self):
        now = [1000.0]
        st = self._store(lambda: now[0])
        old = st.create_conversation("inst-1", "zh")
        st.append_turn(old, "user", "老问题", mode="kb", citations=[],
                       needs_confirm=False, tokens=0, ms=5)
        now[0] = 1000.0 + 181 * 86400
        fresh = st.create_conversation("inst-1", "zh")
        st.append_turn(fresh, "user", "新问题", mode="kb", citations=[],
                       needs_confirm=False, tokens=0, ms=5)
        removed = st.purge_expired()
        self.assertEqual(removed, 1)
        self.assertEqual(st.history(old), [])
        self.assertEqual(len(st.history(fresh)), 1)

    def test_delete_instance_hard_deletes_everything(self):
        st = self._store(lambda: 1000.0)
        conv = st.create_conversation("inst-9", "zh")
        st.append_turn(conv, "user", "删我", mode="kb", citations=[],
                       needs_confirm=False, tokens=0, ms=5)
        self.assertEqual(st.delete_instance("inst-9"), 1)
        self.assertEqual(st.history(conv), [])

    def test_miss_counter_is_per_instance_per_day(self):
        now = [1000.0]
        st = self._store(lambda: now[0])
        conv = st.create_conversation("inst-2", "zh")
        for _ in range(3):
            st.append_turn(conv, "user", "问", mode="generated", citations=[],
                           needs_confirm=True, tokens=10, ms=5)
        self.assertEqual(st.count_misses_today("inst-2"), 3)
        now[0] += 86400
        self.assertEqual(st.count_misses_today("inst-2"), 0)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest tests.test_assistant_gateway.StoreTests -v`
预期：FAIL，`ModuleNotFoundError: No module named 'tools.assistant_gateway.store'`

- [ ] **步骤 3：编写最少实现代码**

```python
"""会话库：多轮上下文、全量留存、180 天硬删、按实例删除、配额计数。

`clock` 可注入 ⇒ 「180 天」与「今天」都能用假时间测，不需要 sleep 也不需要 tz 环境。
与 `ft8/engine/repository.py` 同一形态（sqlite3 + check_same_thread=False + Row）。
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
  id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, lang TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS turns (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conv_id TEXT NOT NULL, role TEXT NOT NULL,
  content TEXT NOT NULL, mode TEXT NOT NULL DEFAULT '', citations TEXT NOT NULL DEFAULT '[]',
  needs_confirm INTEGER NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0,
  ms INTEGER NOT NULL DEFAULT 0, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS feedback (
  turn_id INTEGER PRIMARY KEY, useful INTEGER NOT NULL, note TEXT NOT NULL DEFAULT '', ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS turns_conv ON turns(conv_id, id);
CREATE INDEX IF NOT EXISTS turns_ts ON turns(ts);
"""


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

    def create_conversation(self, instance_id: str, lang: str) -> str:
        cid = f"c-{int(self._clock() * 1000)}-{instance_id[-6:]}"
        now = self._clock()
        self._db.execute("INSERT INTO conversations VALUES (?,?,?,?,?)",
                         (cid, instance_id, lang, now, now))
        self._db.commit()
        return cid

    def append_turn(self, conv_id: str, role: str, content: str, *,
                    mode: str = "", citations: list | None = None,
                    needs_confirm: bool = False, tokens: int = 0, ms: int = 0) -> int:
        now = self._clock()
        cur = self._db.execute(
            "INSERT INTO turns (conv_id, role, content, mode, citations, needs_confirm,"
            " tokens, ms, ts) VALUES (?,?,?,?,?,?,?,?,?)",
            (conv_id, role, content, mode, json.dumps(citations or [], ensure_ascii=False),
             int(needs_confirm), tokens, ms, now))
        self._db.execute("UPDATE conversations SET updated=? WHERE id=?", (now, conv_id))
        self._db.commit()
        return int(cur.lastrowid)

    def history(self, conv_id: str, limit: int = 3) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM turns WHERE conv_id=? ORDER BY id DESC LIMIT ?",
            (conv_id, limit)).fetchall()
        return [{**dict(r), "citations": json.loads(r["citations"])}
                for r in reversed(rows)]

    def purge_expired(self) -> int:
        cutoff = self._clock() - RETENTION_DAYS * 86400
        old = [r["id"] for r in self._db.execute(
            "SELECT id FROM conversations WHERE updated < ?", (cutoff,))]
        for cid in old:
            self._db.execute("DELETE FROM turns WHERE conv_id=?", (cid,))
            self._db.execute("DELETE FROM conversations WHERE id=?", (cid,))
        self._db.commit()
        return len(old)

    def delete_instance(self, instance_id: str) -> int:
        convs = [r["id"] for r in self._db.execute(
            "SELECT id FROM conversations WHERE instance_id=?", (instance_id,))]
        for cid in convs:
            self._db.execute("DELETE FROM turns WHERE conv_id=?", (cid,))
            self._db.execute("DELETE FROM conversations WHERE id=?", (cid,))
        self._db.commit()
        return len(convs)

    def count_misses_today(self, instance_id: str) -> int:
        day = 86400
        since = self._clock() - (self._clock() % day)
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM turns t JOIN conversations c ON c.id=t.conv_id"
            " WHERE c.instance_id=? AND t.mode='generated' AND t.ts>=?",
            (instance_id, since)).fetchone()
        return int(row["n"])
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest tests.test_assistant_gateway.StoreTests -v`
预期：4 passed

- [ ] **步骤 5：Commit**

```bash
git add tools/assistant_gateway/store.py tests/test_assistant_gateway.py
git commit -m "feat(assistant): 会话库 —— 180 天硬删、按实例删除、按日配额"
```

---

## 任务 4：模型客户端（可注入 opener）

**文件：**

- 创建：`tools/assistant_gateway/model.py`
- 测试：`tests/test_assistant_gateway.py`（追加 `ModelTests`）

- [ ] **步骤 1：编写失败的测试**

```python
from tools.assistant_gateway import model as gmodel


class ModelTests(unittest.TestCase):
    def test_posts_openai_shape_and_returns_content(self):
        seen = {}

        def opener(req, timeout):
            seen["url"] = req.full_url
            seen["body"] = json.loads(req.data.decode())
            seen["auth"] = req.get_header("Authorization")
            return io.BytesIO(json.dumps(
                {"choices": [{"message": {"content": "答案"}}]}).encode())

        client = gmodel.ModelClient("https://api.example/v1/chat/completions",
                                    "deepseek-chat", "sk-test", opener=opener)
        self.assertEqual(client.complete([{"role": "user", "content": "问"}]), "答案")
        self.assertEqual(seen["auth"], "Bearer sk-test")
        self.assertEqual(seen["body"]["model"], "deepseek-chat")

    def test_network_error_becomes_model_unavailable(self):
        def opener(req, timeout):
            raise OSError("boom")

        client = gmodel.ModelClient("https://x/v1", "m", "k", opener=opener)
        with self.assertRaises(gmodel.ModelUnavailable):
            client.complete([{"role": "user", "content": "问"}])
```

（文件头补 `import io, json`。）

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest tests.test_assistant_gateway.ModelTests -v`
预期：FAIL，`ModuleNotFoundError: ...model`

- [ ] **步骤 3：编写最少实现代码**

```python
"""OpenAI 兼容的模型客户端。

`opener` 可注入 ⇒ 单测里没有网络。真实现走 `net_tls.urlopen`：
仓库有一条 AST 守卫（tests/test_tls_trust_store.py）要求所有出站 `urlopen` 要么带
`context=` 要么经过 net_tls —— 网关也是出货代码，不能例外。
"""
from __future__ import annotations

import json
from typing import Callable

import net_tls


class ModelUnavailable(Exception):
    """模型不可用（网络/超时/协议错）。调用方据此降级，不把异常抛给客户。"""


class ModelClient:
    def __init__(self, endpoint: str, model: str, api_key: str, *,
                 timeout: int = 20, opener: Callable | None = None):
        self._endpoint = endpoint
        self._model = model
        self._key = api_key
        self._timeout = timeout
        self._opener = opener or (lambda req, timeout: net_tls.urlopen(req, timeout=timeout))

    def complete(self, messages: list[dict], *, max_tokens: int = 700) -> str:
        import urllib.request
        body = json.dumps({"model": self._model, "messages": messages,
                           "temperature": 0.2, "max_tokens": max_tokens}).encode()
        req = urllib.request.Request(self._endpoint, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._key}",
        })
        try:
            raw = self._opener(req, self._timeout).read()
            payload = json.loads(raw.decode("utf-8"))
            return payload["choices"][0]["message"]["content"].strip()
        except Exception as exc:                     # noqa: BLE001 - 一律降级
            raise ModelUnavailable(str(exc)) from exc
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest tests.test_assistant_gateway.ModelTests -v`
预期：2 passed

- [ ] **步骤 5：Commit**

```bash
git add tools/assistant_gateway/model.py tests/test_assistant_gateway.py
git commit -m "feat(assistant): 模型客户端 —— 出站走 net_tls，失败即降级"
```

---

## 任务 5：判定引擎（规则 → 检索 → 生成 → 出处校验）

**文件：**

- 创建：`tools/assistant_gateway/engine.py`
- 测试：`tests/test_assistant_gateway.py`（追加 `EngineTests`）

- [ ] **步骤 1：编写失败的测试**

```python
from tools.assistant_gateway import engine as gengine


class FakeModel:
    def __init__(self, text):
        self.text = text
        self.calls = 0

    def complete(self, messages, **kw):
        self.calls += 1
        return self.text


STATE = {"model": "ft710", "version": "1.28.22", "fixpack": "FP18",
         "serial_connected": True, "audio_rx_open": True, "audio_tx_open": True,
         "last_errors": [], "platform": "linux-arm64", "ui_lang": "zh"}


class EngineTests(unittest.TestCase):
    def _engine(self, model_text="随便答", entries=ENTRIES, quota=20, misses=0):
        model = FakeModel(model_text)
        eng = gengine.Engine(entries=entries, model=model,
                             quota_per_day=quota, misses_so_far=lambda: misses)
        return eng, model

    def test_rule_layer_answers_without_calling_the_model(self):
        eng, model = self._engine()
        state = dict(STATE, serial_connected=False)
        ans = eng.answer("为什么没频谱", state, history=[])
        self.assertEqual(ans.mode, "rule")
        self.assertIn("串口", ans.answer)
        self.assertEqual(model.calls, 0)

    def test_kb_hit_uses_the_entry_and_cites_it(self):
        eng, _ = self._engine("CQ 泵已改成按队列深度补料。")
        ans = eng.answer("CQ 播放有失真怎么办", STATE, history=[])
        self.assertEqual(ans.mode, "kb")
        self.assertEqual(ans.citations[0]["id"], "kb-cq")
        self.assertFalse(ans.needs_confirm)

    def test_unknown_question_falls_back_to_generated_and_asks_confirmation(self):
        eng, model = self._engine("我觉得可能是天线的问题。")
        ans = eng.answer("我家猫踩了电台怎么办", STATE, history=[])
        self.assertEqual(ans.mode, "generated")
        self.assertTrue(ans.needs_confirm)
        self.assertEqual(model.calls, 1)

    def test_generated_answer_with_unverifiable_version_is_downgraded(self):
        eng, _ = self._engine("这个问题在 9.9.9 版本已经修复。")
        ans = eng.answer("我家猫踩了电台怎么办", STATE, history=[])
        self.assertTrue(ans.needs_confirm)
        self.assertIn("无法核对", "".join(ans.citations_note for _ in [0]) + str(ans.degraded))

    def test_quota_exhausted_degrades_to_low_confidence_kb(self):
        eng, model = self._engine(quota=1, misses=1)
        ans = eng.answer("完全没听过的问题 xyzzy", STATE, history=[])
        self.assertEqual(ans.degraded, "quota")
        self.assertEqual(model.calls, 0)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest tests.test_assistant_gateway.EngineTests -v`
预期：FAIL，`ModuleNotFoundError: ...engine`

- [ ] **步骤 3：编写最少实现代码**

```python
"""判定引擎：规则层 → KB 检索 → 兜底生成，外加出处校验。

三条硬规矩（规格 §5）：
1. 规则层不调模型、不扣配额；
2. KB 命中的事实必须来自条目（渲染带 citations）；
3. 生成式答案里出现"无法核对的版本号"就降级为「待确认」。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import kb as gkb

_VERSION = re.compile(r"\b1\.\d{1,2}\.\d{1,2}\b")


@dataclass
class Answer:
    answer: str
    mode: str                      # rule | kb | generated
    citations: list[dict] = field(default_factory=list)
    needs_confirm: bool = False
    degraded: str = ""


_RULES = (
    (("频谱", "瀑布", "不出图", "没有波形"),
     lambda s: not s.get("serial_connected"),
     "串口没连上电台，所以拿不到频谱。打开「连接设置」选中端口并保存，页面会重连。"),
    (("没声音", "听不到", "没有声音"),
     lambda s: not s.get("audio_rx_open"),
     "RX 声卡没打开。确认电台已用 USB 连上、且没有被别的程序占用，然后在设置页重连音频。"),
)


def _rule_answer(question: str, state: dict) -> str | None:
    for words, predicate, text in _RULES:
        if any(w in question for w in words) and predicate(state):
            return text
    return None


class Engine:
    def __init__(self, *, entries, model, quota_per_day: int = 20,
                 misses_so_far):
        self._entries = entries
        self._model = model
        self._quota = quota_per_day
        self._misses = misses_so_far

    def answer(self, question: str, state: dict, *, history: list[dict]) -> Answer:
        version = state.get("version", "")
        model_id = state.get("model", "")

        ruled = _rule_answer(question, state)
        if ruled:
            return Answer(answer=ruled, mode="rule")

        hits = gkb.retrieve(self._entries, question, version, model_id)
        hit = gkb.is_hit(hits)

        if hit:
            entry = hits[0].entry
            text = self._compose(question, state, history, entry["title"], entry["body"])
            return Answer(answer=text, mode="kb",
                          citations=[{"id": entry["id"], "label": entry["title"],
                                      "url": entry.get("url", "")}])

        if self._misses() >= self._quota:
            best = hits[0].entry["title"] if hits else ""
            return Answer(answer=(f"今天的自动答疑已用完。知识库里最接近的是：{best}" if best
                                  else "今天的自动答疑已用完，也没有查到相近的条目。建议提 🐞 诊断包。"),
                          mode="kb", degraded="quota",
                          citations=[{"id": hits[0].entry["id"], "label": best, "url": ""}] if hits else [])

        text = self._compose(question, state, history, "", "")
        needs_confirm = bool(_VERSION.search(text)) and version not in text
        return Answer(answer=text, mode="generated",
                      needs_confirm=needs_confirm or True,
                      degraded="unverifiable" if needs_confirm else "")

    # ── prompt ────────────────────────────────────────────────
    def _compose(self, question, state, history, kb_title, kb_body) -> str:
        system = [
            "你是 MRRC Modern（业余无线电远程控制）的产品助手。",
            "只回答与产品有关的问题；不确定就说不确定，不要编造版本号、命令或配置项。",
            f"客户机器：{state.get('model')} / {state.get('version')} · {state.get('fixpack')}",
        ]
        if kb_title:
            system.append("以下知识条目是事实来源，回答必须与之一致：")
            system.append(f"【{kb_title}】\n{kb_body}")
        messages = [{"role": "system", "content": "\n".join(system)}]
        for turn in history[-3:]:
            messages.append({"role": turn["role"], "content": turn["content"]})
        messages.append({"role": "user", "content": question})
        return self._model.complete(messages)
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest tests.test_assistant_gateway.EngineTests -v`
预期：5 passed

- [ ] **步骤 5：Commit**

```bash
git add tools/assistant_gateway/engine.py tests/test_assistant_gateway.py
git commit -m "feat(assistant): 判定引擎 —— 规则/检索/生成三条路径与出处降级"
```

---

## 任务 6：网关 HTTP 入口

**文件：**

- 创建：`tools/assistant_gateway/http_server.py`、`tools/assistant_gateway/gateway.py`
- 测试：`tests/test_assistant_gateway.py`（追加 `HttpTests`，用 `http.client` 打真实端口）

- [ ] **步骤 1：编写失败的测试**

```python
import threading
import http.client


class HttpTests(unittest.TestCase):
    def _serve(self):
        harness = gh.make_server(host="127.0.0.1", port=0, engine=ENGINE_FIXTURE,
                                 instances={"inst-token": "inst-1"})
        threading.Thread(target=harness.serve_forever, daemon=True).start()
        self.addCleanup(harness.shutdown)
        return harness.server_address[1]

    def _post(self, port, path, payload, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        body = json.dumps(payload).encode()
        conn.request("POST", path, body=body, headers={"Content-Type": "application/json",
                                                       **(headers or {})})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read().decode() or "{}")

    def test_unknown_instance_is_rejected(self):
        port = self._serve()
        status, body = self._post(port, "/v1/assistant/ask",
                                  {"message": "问", "state": STATE},
                                  {"X-Mrrc-Instance": "nope"})
        self.assertEqual(status, 401)

    def test_happy_path_returns_citations_and_conversation_id(self):
        port = self._serve()
        status, body = self._post(port, "/v1/assistant/ask",
                                  {"message": "CQ 播放有失真怎么办", "state": STATE},
                                  {"X-Mrrc-Instance": "inst-token"})
        self.assertEqual(status, 200)
        self.assertTrue(body["conversation_id"])
        self.assertEqual(body["citations"][0]["id"], "kb-cq")

    def test_oversized_body_is_rejected(self):
        port = self._serve()
        status, _ = self._post(port, "/v1/assistant/ask",
                               {"message": "x" * 20000, "state": STATE},
                               {"X-Mrrc-Instance": "inst-token"})
        self.assertEqual(status, 413)
```

（`ENGINE_FIXTURE` 用任务 5 的 `Engine(...)` 构造，模型是 `FakeModel`。）

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest tests.test_assistant_gateway.HttpTests -v`
预期：FAIL，`ModuleNotFoundError: ...http_server`

- [ ] **步骤 3：编写最少实现代码**

```python
"""`/v1/assistant/ask` 与 `/v1/assistant/status`。

ThreadingHTTPServer + 手写 JSON —— 与 tools/support_receiver 同一风格：这个服务要能
在任意一台 Linux 上 `python3 gateway.py` 起来，不引入 Web 框架。
限额：请求体 ≤ 16 KB、message ≤ 4000 字符；实例 token 由配置表给出（不是呼号）。
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 16 * 1024
MAX_MESSAGE = 4000


def make_handler(*, engine, instances, store):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):        # 静默：审计走 store，不打 stdout
            return

        def _json(self, status: int, payload: dict) -> None:
            blob = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

        def do_POST(self) -> None:                # noqa: N802 - BaseHTTPRequestHandler API
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._json(413, {"error": "body too large"})
            instance = self.headers.get("X-Mrrc-Instance") or ""
            instance_id = instances.get(instance)
            if not instance_id:
                return self._json(401, {"error": "unknown instance"})
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "bad json"})
            if self.path == "/v1/assistant/status":
                return self._json(200, {"enabled": True, "instance": instance_id})
            if self.path != "/v1/assistant/ask":
                return self._json(404, {"error": "not found"})
            message = str(payload.get("message") or "")[:MAX_MESSAGE]
            if not message.strip():
                return self._json(400, {"error": "empty message"})
            conv = payload.get("conversation_id") or store.create_conversation(
                instance_id, str(payload.get("lang") or "zh"))
            history = store.history(conv, limit=3)
            answer = engine.answer(message, payload.get("state") or {}, history=history)
            store.append_turn(conv, "user", message)
            store.append_turn(conv, "assistant", answer.answer, mode=answer.mode,
                              citations=answer.citations, needs_confirm=answer.needs_confirm)
            return self._json(200, {"conversation_id": conv, "answer": answer.answer,
                                    "citations": answer.citations, "mode": answer.mode,
                                    "needs_confirm": answer.needs_confirm,
                                    "degraded": answer.degraded})
    return Handler


def make_server(*, host: str, port: int, engine, instances, store=None):
    return ThreadingHTTPServer((host, port), make_handler(
        engine=engine, instances=instances, store=store))
```

`gateway.py`（装配，含 503 总开关与保留期清理线程）由实现者按此装配：读 `MRRC_ASSISTANT_MODEL_URL` /
`_MODEL` / `_API_KEY` / `_INSTANCES`（`token:instance_id,…`）/ `MRRC_ASSISTANT_LISTEN`（默认 `127.0.0.1:8790`），
构造 `ModelClient` / `Store` / `Engine` / `make_server`，每 6 小时调一次 `store.purge_expired()`。

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest tests.test_assistant_gateway.HttpTests -v`
预期：3 passed

- [ ] **步骤 5：Commit**

```bash
git add tools/assistant_gateway/http_server.py tools/assistant_gateway/gateway.py tests/test_assistant_gateway.py
git commit -m "feat(assistant): 网关 HTTP 入口 —— token 鉴权、体量上限、审计落库"
```

---

## 任务 7：盒子侧薄客户端

**文件：**

- 创建：`assistant_client.py`
- 测试：`tests/test_assistant_client.py`

- [ ] **步骤 1：编写失败的测试**

```python
"""白名单摘要 + 转发 + 降级 —— 出网用注入的 opener，测试不联网。"""
import io
import json
import unittest

import assistant_client as ac

STATE = {
    "model": "ft710", "version": "1.28.22", "fixpack": "FP18",
    "serial_connected": True, "audio_rx_open": True, "audio_tx_open": False,
    "last_errors": ["audio:-9999", "cat:timeout"],
    "platform": "linux-arm64", "ui_lang": "zh",
    "callsign": "BG8GGG", "vfo_a_freq": 7050000, "recording": "/var/x.mp3",
}


class SummaryTests(unittest.TestCase):
    def test_exact_key_set(self):
        self.assertEqual(set(ac.state_summary(STATE)), {
            "model", "version", "fixpack", "serial_connected", "audio_rx_open",
            "audio_tx_open", "last_errors", "platform", "ui_lang"})

    def test_no_forbidden_value_leaks(self):
        blob = json.dumps(ac.state_summary(STATE), ensure_ascii=False)
        for needle in ("BG8GGG", "7050000", ".mp3", "/dev/"):
            self.assertNotIn(needle, blob)

    def test_size_budget(self):
        self.assertLessEqual(len(json.dumps(ac.state_summary(STATE))), 2048)

    def test_last_errors_are_capped_at_three(self):
        many = dict(STATE, last_errors=[f"e{i}" for i in range(10)])
        self.assertEqual(len(ac.state_summary(many)["last_errors"]), 3)


class AskTests(unittest.TestCase):
    def test_answer_is_forwarded_back_verbatim(self):
        def opener(req, timeout):
            self.assertEqual(req.get_header("X-mrrc-instance"), "tok")
            return io.BytesIO(json.dumps({"answer": "好", "citations": [],
                                          "mode": "kb", "needs_confirm": False,
                                          "degraded": ""}).encode())

        client = ac.AssistantClient("https://gw/v1", "tok", opener=opener)
        out = client.ask(None, "问", STATE)
        self.assertEqual(out["answer"], "好")

    def test_network_failure_returns_offline_copy(self):
        def opener(req, timeout):
            raise OSError("no route")

        client = ac.AssistantClient("https://gw/v1", "tok", opener=opener)
        self.assertEqual(client.ask(None, "问", STATE)["degraded"], "offline")
        self.assertIn("需要联网", client.ask(None, "问", STATE)["answer"])
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest tests.test_assistant_client -v`
预期：FAIL，`ModuleNotFoundError: No module named 'assistant_client'`

- [ ] **步骤 3：编写最少实现代码**

```python
"""盒子侧的助手客户端：只做三件事 —— 摘状态、转发、把失败翻译成人话。

知识库不在本地（规格 §3），所以这里**没有**任何检索逻辑：断网就是断网。
出站走 `net_tls.urlopen`（AST 守卫），opener 可注入 ⇒ 单测不联网。
"""
from __future__ import annotations

import json
import urllib.request
from typing import Callable, Optional

import net_tls

ASK_TIMEOUT_S = 10
OFFLINE_ANSWER = ("助手需要联网才能回答；本机不留知识库。"
                  "如果这是故障，请先点「遇到问题」提一个诊断包。")

ALLOWED_KEYS = ("model", "version", "fixpack", "serial_connected", "audio_rx_open",
                "audio_tx_open", "last_errors", "platform", "ui_lang")


def state_summary(state: dict) -> dict:
    """白名单摘要。**只从入参里挑，不构造** —— 加字段必须改 ALLOWED_KEYS 与测试。"""
    summary = {k: state.get(k) for k in ALLOWED_KEYS if k in state}
    summary["last_errors"] = list(summary.get("last_errors") or [])[:3]
    return summary


def offline(reason: str = "offline") -> dict:
    return {"answer": OFFLINE_ANSWER, "citations": [], "mode": "offline",
            "needs_confirm": True, "degraded": reason}


class AssistantClient:
    def __init__(self, base_url: str, instance_token: str, *,
                 opener: Optional[Callable] = None):
        self._base = base_url.rstrip("/")
        self._token = instance_token
        self._opener = opener or (lambda req, timeout: net_tls.urlopen(req, timeout=timeout))

    def ask(self, conversation_id: Optional[str], message: str, state: dict) -> dict:
        body = json.dumps({"conversation_id": conversation_id, "message": message,
                           "state": state_summary(state),
                           "lang": state.get("ui_lang", "zh")}).encode()
        req = urllib.request.Request(f"{self._base}/assistant/ask", data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "X-Mrrc-Instance": self._token})
        try:
            return json.loads(self._opener(req, ASK_TIMEOUT_S).read().decode("utf-8"))
        except Exception:                            # noqa: BLE001 - 一律降级
            return offline()
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest tests.test_assistant_client -v`
预期：6 passed

- [ ] **步骤 5：Commit**

```bash
git add assistant_client.py tests/test_assistant_client.py
git commit -m "feat(assistant): 盒子侧客户端 —— 白名单摘要、超时即降级"
```

---

## 任务 8：盒子路由与开关

**文件：**

- 修改：`server.py`（配置区 + 三条路由）
- 测试：`tests/test_assistant_route.py`

- [ ] **步骤 1：编写失败的测试**

```python
"""助手路由：角色门、形状、开关 —— 与 /api/perf 同一套规矩。"""
import json
import unittest
from unittest import mock

import server


class AssistantRouteTests(unittest.IsolatedAsyncioTestCase):
    def test_listen_session_is_forbidden(self):
        request = mock.Mock()
        request.headers = {"host": "x"}
        request.client = mock.Mock(host="127.0.0.1")
        with mock.patch.object(server, "_is_listen_request", return_value=True), \
             mock.patch.object(server, "_verify_auth", return_value=True):
            import asyncio
            resp = asyncio.run(server.api_assistant_status(request))
        self.assertEqual(resp.status_code, 403)

    def test_disabled_returns_404(self):
        with mock.patch.object(server, "ASSISTANT_ENABLED", False):
            import asyncio
            resp = asyncio.run(server.api_assistant_ask(mock.Mock(headers={})))
        self.assertEqual(resp.status_code, 404)

    def test_unavailable_assistant_does_not_raise(self):
        with mock.patch.object(server, "_assistant_client", side_effect=RuntimeError("boom")):
            import asyncio
            resp = asyncio.run(server._assistant_offline_payload())
        self.assertEqual(resp["degraded"], "offline")
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest tests.test_assistant_route -v`
预期：FAIL，`AttributeError: module 'server' has no attribute 'api_assistant_status'`

- [ ] **步骤 3：编写最少实现代码**

在 `server.py` 的 CQ 配置区附近加入：

```python
# ── 产品 AI 助手（规格 docs/superpowers/specs/2026-10-30-ai-assistant-design.md）──
# 知识库在中央，本机只转发；关掉就整块消失（面板 404）。listen / sdr 会话一律 403：
# 助手会读到本机状态摘要，那不是给陌生人的。
ASSISTANT_ENABLED = (_env("MRRC_ASSISTANT", "on") or "on").strip().lower() != "off"
ASSISTANT_URL = (_env("MRRC_ASSISTANT_URL", "") or "").strip()
ASSISTANT_INSTANCE_TOKEN = (_env("MRRC_ASSISTANT_TOKEN", "") or "").strip()
_assistant_client = None


def _assistant_forbidden(request) -> Optional[tuple[int, str]]:
    return _perf_forbidden(request)          # 同一道门：listen / sdr 都不行


def _assistant_prompt_state() -> dict:
    """喂给助手的白名单事实源 —— 值全部来自内核状态，不含呼号/频率/录音。"""
    return {
        "model": _active_model_key(),
        "version": _served_version(),
        "fixpack": _served_fixpack(),
        "serial_connected": bool(radio.serial_connected),
        "audio_rx_open": bool(getattr(audio, "_rx_stream", None)),
        "audio_tx_open": bool(getattr(audio, "_tx_stream", None)),
        "last_errors": _recent_error_codes(3),
        "platform": f"{sys.platform}-{platform.machine()}",
        "ui_lang": "zh",
    }


async def _assistant_offline_payload() -> dict:
    return assistant_client.offline()


@app.get("/api/assistant/status", include_in_schema=False)
async def api_assistant_status(request: Request):
    if not ASSISTANT_ENABLED:
        return JSONResponse({"error": "assistant disabled"}, status_code=404)
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    reason = _assistant_forbidden(request)
    if reason is not None:
        return JSONResponse({"error": reason[1]}, status_code=reason[0])
    return JSONResponse({"enabled": True, "endpoint": bool(ASSISTANT_URL)})


@app.post("/api/assistant/ask", include_in_schema=False)
async def api_assistant_ask(request: Request):
    if not ASSISTANT_ENABLED:
        return JSONResponse({"error": "assistant disabled"}, status_code=404)
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    reason = _assistant_forbidden(request)
    if reason is not None:
        return JSONResponse({"error": reason[1]}, status_code=reason[0])
    try:
        body = await request.json()
    except Exception:                            # noqa: BLE001
        body = {}
    if not ASSISTANT_URL:
        return JSONResponse(await _assistant_offline_payload())
    client = assistant_client.AssistantClient(ASSISTANT_URL, ASSISTANT_INSTANCE_TOKEN)
    try:
        payload = await asyncio.to_thread(
            client.ask, body.get("conversation_id"), str(body.get("message") or "")[:4000],
            _assistant_prompt_state())
    except Exception:                            # noqa: BLE001 - 助手永不影响主链路
        payload = assistant_client.offline()
    return JSONResponse(payload)


@app.post("/api/assistant/clear", include_in_schema=False)
async def api_assistant_clear(request: Request):
    ...  # 同样三道门；调 client.clear()（DELETE /v1/assistant/conversation）
```

同时补两个小工具（`_recent_error_codes`、`_served_fixpack` 读 `fixpack.txt`；`_served_version` 已有则复用）。

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest tests.test_assistant_route -v`
预期：3 passed

- [ ] **步骤 5：Commit**

```bash
git add server.py tests/test_assistant_route.py
git commit -m "feat(assistant): 三条路由 + listen/sdr 403 + 助手不可用不影响主链路"
```

---

## 任务 9：面板与菜单入口

**文件：**

- 创建：`static/assistant.html`、`static/assistant.js`
- 修改：`static/index.html`（☰ 菜单加一项）
- 测试：`tests/test_assistant_route.py`（追加页面静态断言）

- [ ] **步骤 1：编写失败的测试**

```python
class AssistantPageTests(unittest.TestCase):
    def test_page_is_served_and_calls_the_route(self):
        html = (server.STATIC_DIR / "assistant.html").read_text(encoding="utf-8")
        self.assertIn("/api/assistant/ask", html + (server.STATIC_DIR / "assistant.js").read_text(encoding="utf-8"))
        self.assertIn("待确认", (server.STATIC_DIR / "assistant.js").read_text(encoding="utf-8"))

    def test_menu_links_the_page(self):
        html = (server.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="assistant.html"', html)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest tests.test_assistant_route.AssistantPageTests -v`
预期：FAIL，`FileNotFoundError: assistant.html`

- [ ] **步骤 3：实现页面**

`static/assistant.html`：一个 `support.html` 同构的独立页（同主题 CSS 内联、无构建步骤），包含
`#chat`（气泡容器）、`#ask`（输入框）、`#send`、`#clear`、`#meta`（可折叠的白名单摘要）。
`static/assistant.js` 负责：

- 首次加载 `GET /api/assistant/status`，`enabled=false` 或 404 → 整块灰掉并显示「助手未开通」；
- 发送走 `POST /api/assistant/ask`，请求体 `{conversation_id, message}`；**把 `conversation_id` 存在内存里**（不落 localStorage）；
- 渲染气泡：正文 + `citations` 列表（label 作为链接） + `needs_confirm` 时加 `<span class="badge">AI 生成 · 待确认</span>`；
- `degraded === "offline"` 时气泡用警告色并附「提诊断包」链接；
- `#clear` → `POST /api/assistant/clear`，清空气泡并示「已清空」。

`static/index.html` 的 ☰ 菜单，在「遇到问题」那一项**之前**插入：

```html
          <li>
            <a
              href="assistant.html"
              target="_blank"
              rel="noopener"
              class="menu-item"
              >AI 助手</a
            >
          </li>
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest tests.test_assistant_route -v`
预期：5 passed

- [ ] **步骤 5：Commit**

```bash
git add static/assistant.html static/assistant.js static/index.html tests/test_assistant_route.py
git commit -m "feat(assistant): 网页面板 —— 多轮气泡、出处、待确认徽标、清空会话"
```

---

## 任务 10：发布绑定与全量验收

**文件：**

- 修改：`.agents/skills/dual-platform-release/release-artifacts.json`（一条规则）
- 测试：`tests/test_release_artifacts.py`（追加一条）

- [ ] **步骤 1：编写失败的测试**

```python
class AssistantKbFreshnessTests(unittest.TestCase):
    """CHANGELOG 多一条，知识库必须多一条 —— 否则发了版助手却说不知道。"""

    def test_kb_covers_the_newest_box_line_heading(self):
        sys.path.insert(0, str(REPO / "dev_tools"))
        import make_assistant_kb as mk
        kb = mk.build(REPO)
        versions = {e["applies_to"]["version_min"] for e in kb["entries"]}
        self.assertIn(mk.newest_box_version(REPO), versions)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest tests.test_release_artifacts.AssistantKbFreshnessTests -v`
预期：FAIL，`AttributeError: module 'make_assistant_kb' has no attribute 'newest_box_version'`

- [ ] **步骤 3：补 `newest_box_version()` 与注册表规则**

```python
def newest_box_version(root: Path) -> str:
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    match = BOX_HEADING.search(text)
    if not match:
        raise SystemExit("CHANGELOG 里没有镜像线标题")
    return match.group(1)
```

`release-artifacts.json` 的 `rules` 追加：

```json
{"id": "assistant-kb", "path": "tools/assistant_gateway/assistant_kb.json",
 "pattern": "\"version_min\": \"([0-9]+\\.[0-9]+\\.[0-9]+)\"", "expect": "box",
 "min_count": 1, "why": "助手知识库必须覆盖最新的镜像线版本，否则发了版它还说不知道。"}
```

- [ ] **步骤 4：全量验收**

```bash
./.venv/bin/python dev_tools/make_assistant_kb.py --out tools/assistant_gateway/
./.venv/bin/python -m unittest discover -s tests          # 全绿，含新增四个模块
./.venv/bin/python .agents/skills/dual-platform-release/harness/release_check.py   # 0 failing
```

- [ ] **步骤 5：Commit**

```bash
git add dev_tools/make_assistant_kb.py tests/test_release_artifacts.py \
        .agents/skills/dual-platform-release/release-artifacts.json \
        tools/assistant_gateway/assistant_kb.json
git commit -m "feat(assistant): 知识库进发布注册表 —— CHANGELOG 一条，KB 一条"
```

---

## 自检记录（写完后对照规格逐条核过）

| 规格章节 | 对应任务 |
| --- | --- |
| §3 架构（薄客户端 + 中央大脑） | 任务 6、7、8 |
| §4 白名单与三条守卫 | 任务 7（键集/黑名单/≤2 KB） |
| §5 判定顺序、阈值、配额、多轮窗口 | 任务 2、3、5 |
| §6 接口契约 + `net_tls` | 任务 4、6、7 |
| §7 存储与留存 | 任务 3 |
| §8 错误矩阵 | 任务 5、6、7、9 |
| §9 测试 | 任务 1–9 各自的测试步骤 |
| §10 上线/开关/度量/发布绑定 | 任务 8、10 |

**类型一致性**：`Engine.answer(question, state, *, history)` 在任务 5、6 一致；
`Store.create_conversation/append_turn/history/count_misses_today` 在任务 3、6 一致；
`state_summary` 的键集在任务 7、8 一致。
**未决**：`gateway.py` 的装配细节（环境变量名已在任务 6 列出）留给实现者按仓库习惯补全，
但**所有对外行为**都已由任务 1–10 的测试钉住。
