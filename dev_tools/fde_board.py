#!/usr/bin/env python3
"""FDE board operator CLI — scheduling decisions for classified reports.

The unattended pipeline (support_autopilot.py) does the analysis and the
implementation; this tool is where the HUMAN decides what gets built:

    python3 dev_tools/fde_board.py list                          # 看板状态
    python3 dev_tools/fde_board.py show <编号>                    # 单条详情
    python3 dev_tools/fde_board.py decide <编号> schedule [--note ...]
    python3 dev_tools/fde_board.py implement [编号]               # 后台实施（同 cron）
    python3 dev_tools/fde_board.py rebuild                       # 重建+发布看板页
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import support_board as board                                    # noqa: E402
from dev_tools.support_autopilot import (                        # noqa: E402
    BOARD_STATE_FILE, implement_one, rebuild_board_page, run_implementations)


def cmd_list(_args) -> int:
    state = board.load_state(BOARD_STATE_FILE)
    if not state:
        print("看板为空（还没有分类上报）")
        return 0
    for col in board.COLUMNS:
        entries = [(k, v) for k, v in state.items() if v.get("status") == col]
        if not entries:
            continue
        print(f"── {board.COLUMN_TITLES[col]} ({len(entries)})")
        for bundle_id, e in sorted(entries, key=lambda kv: kv[1].get("updated", "")):
            sev = f"[{e['severity']}]" if e.get("severity") else ""
            print(f"  {bundle_id}  [{e.get('kind') or '未分类'}]{sev} "
                  f"{e.get('title', '')[:70]}")
            if e.get("note"):
                print(f"      备注：{e['note'][:100]}")
            if e.get("branch"):
                sha = f"@{e['commit'][:8]}" if e.get("commit") else ""
                print(f"      分支：{e['branch']}{sha}  {e.get('result', '')[:90]}")
    return 0


def cmd_show(args) -> int:
    state = board.load_state(BOARD_STATE_FILE)
    entry = state.get(args.bundle_id)
    if not entry:
        print(f"未知条目：{args.bundle_id}", file=sys.stderr)
        return 1
    print(json.dumps(entry, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_decide(args) -> int:
    state = board.load_state(BOARD_STATE_FILE)
    result = board.decide(state, args.bundle_id, args.action, note=args.note or "")
    board.save_state(BOARD_STATE_FILE, state)
    rebuild_board_page()
    print(result)
    return 0 if "未知" not in result else 1


def cmd_implement(args) -> int:
    if args.bundle_id:
        results = [(args.bundle_id, implement_one(args.bundle_id))]
    else:
        results = run_implementations()
    for bundle_id, result in results:
        print(f"{bundle_id}: {result}")
    rebuild_board_page()
    return 0 if all(r in ("committed", "rejected", "not_scheduled",
                          "skipped_dirty") for _, r in results) else 1


def cmd_rebuild(_args) -> int:
    print(f"看板已重建并发布：{rebuild_board_page()}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="MRRC Modern FDE 看板（排期决策）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, help_text in (("list", "列出看板"), ("rebuild", "重建并发布看板页")):
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(func={"list": cmd_list, "rebuild": cmd_rebuild}[name])
    p_show = sub.add_parser("show", help="单条详情（JSON）")
    p_show.add_argument("bundle_id")
    p_show.set_defaults(func=cmd_show)
    p_decide = sub.add_parser("decide", help="排期决策：schedule / reject / backlog")
    p_decide.add_argument("bundle_id")
    p_decide.add_argument("action", choices=("schedule", "reject", "backlog"))
    p_decide.add_argument("--note", default="", help="决策备注")
    p_decide.set_defaults(func=cmd_decide)
    p_impl = sub.add_parser("implement", help="后台实施（无编号=全部已排期，单飞每轮 1 条）")
    p_impl.add_argument("bundle_id", nargs="?", default="")
    p_impl.set_defaults(func=cmd_implement)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
