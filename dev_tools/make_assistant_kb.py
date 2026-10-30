#!/usr/bin/env python3
"""把 CHANGELOG 生成 `assistant_kb.json`（AI 助手的知识库）。

    ./.venv/bin/python dev_tools/make_assistant_kb.py --out tools/assistant_gateway/

知识库**只放中央**（规格 2026-10-30 §3）：盒子不留副本，所以这个文件随网关部署，
不进盒子包。同输入同输出（排序 + 不含时间戳）—— 否则每次构建都产出一个"新"文件，
diff 与发布检查都失去意义。

条目来源目前只有 CHANGELOG 的两种标题（镜像线 / 桌面线）。看板与答复页在运营侧那份
规格里再接（规格 §2 的「非目标」），但在数据形状上留好位置：`kind` 与 `applies_to`。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

BOX_HEADING = re.compile(
    r"^##\s*\[镜像线 v(\d+\.\d+\.\d+)(?:\s*·\s*(FP\d+))?\]\s*[—-]\s*(\d{4}-\d\d-\d\d)\s*[—-]\s*(.+)$",
    re.M)
APP_HEADING = re.compile(
    r"^##\s*\[v(\d+\.\d+\.\d+)\]\s*[—-]\s*(\d{4}-\d\d-\d\d)\s*[—-]\s*(.+)$", re.M)
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]*")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")


def keywords(text: str) -> list[str]:
    """拉丁词 + 中文二字组。

    中文没有空格，二元组是最便宜且够用的索引单位；建索引与查询**共用这一个函数**，
    否则「建库一种切分、查询另一种切分」的漂移会让命中率莫名其妙地掉。
    """
    words = {w.lower() for w in _LATIN.findall(text) if len(w) >= 2}
    for run in _CJK_RUN.findall(text):
        words.update(run[i:i + 2] for i in range(len(run) - 1))
    return sorted(words)


def _entry_id(kind: str, version: str, title: str) -> str:
    digest = hashlib.sha256(f"{kind}|{version}|{title}".encode()).hexdigest()[:8]
    return f"kb-{kind}-{version}-{digest}"


def _section_body(text: str, start: int) -> str:
    """标题到下一个 `## ` 之间的正文，压掉空行，截断到 2000 字。"""
    rest = text[start:]
    end = rest.find("\n## ")
    body = rest if end < 0 else rest[:end]
    return re.sub(r"\n{2,}", "\n", body).strip()[:2000]


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


def build(root: Path) -> dict:
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    entries = _release_entries(changelog)
    for entry in entries:
        entry["keywords"] = keywords(entry["title"] + "\n" + entry["body"])
    entries.sort(key=lambda e: (e["applies_to"]["version_min"], e["id"]), reverse=True)
    return {"version": 1, "entries": entries}


def newest_box_version(root: Path) -> str:
    """镜像线的最新版本号 —— 发布检查用它核对「知识库有没有跟上这一版」。"""
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    match = BOX_HEADING.search(text)
    if not match:
        raise SystemExit("CHANGELOG 里没有镜像线标题")
    return match.group(1)


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
