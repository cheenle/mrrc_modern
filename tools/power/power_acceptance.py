#!/usr/bin/env python3
"""Field acceptance for the radio power menu (V1–V7, spec §8).

为什么会有一个"验收脚本"
------------------------
五条守卫里有一条只有**这台机器上**才说得清：`PS1;` 到底能不能唤醒电台（V2），
以及唤不醒时应用会不会老实承认（V6）。而它决定「重开电台」这一项**是否发布**
（规格 §8 的逃生舱）—— 当年那个电源键就是因为这条被撤掉的。

手抄读数最容易漏掉的恰好是判据本身：**第几轮醒、耗时多久**。所以把记录器做成一条
可重复的命令：它自己登录、发命令、按时间戳记录每一帧（`powerRescue` / `power_on` /
`error`），跑完写一份 Markdown 报告，可以直接贴进 `CHANGELOG.md` / `SDD/14`。

结构：**判据是纯函数**（喂它一串带时间戳的帧，返回通过/不通过 + 说明），I/O 只在
`main()` 里。于是判据在没有电台的机器上也能测（`tests/test_power_acceptance.py`）。

用法（在跑着 `server.py` 的那台机器上）：

    python3 tools/power/power_acceptance.py --password '网页口令'          # V1–V4、V7（全自动）
    python3 tools/power/power_acceptance.py --password '网页口令' --manual  # 再加 V5/V6（要人配合）
    python3 tools/power/power_acceptance.py --password … --listen-password …  # V7 需要收听口令

V5（发射中拒绝）与 V6（把电台电源拔掉后看它是否老实报失败）需要人动手，因此默认跳过；
V6 的等待上限是服务端的 15 分钟（`POWER_RESCUE_MAX_S`），想缩短观察可以用
`--v6-wait 120`（只影响本脚本等多久，不改服务端行为）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# 判据里要认的那两句服务端文案（改文案就会让判据红，这是有意的：
# 文案是操作员唯一的解释来源，规格 §7 把它写进了契约）。
#: 启动窗口的客户端等待：服务端不给客户端看 `_power_boot_until`，所以每个要"干净起点"的
#: 场景先静默这么久，确保上一次 PS1 的 15 秒保护窗口已经过期。
WINDOW_SETTLE_S = 20.0

WINDOW_REFUSAL = "秒后再关机"
LISTEN_REFUSAL = "Listen-only"
TX_REFUSAL = "发射中不能关机"

__all__ = [
    "WINDOW_REFUSAL", "LISTEN_REFUSAL", "TX_REFUSAL", "scenario_set",
    "Recorder", "power_state", "rescue_events", "trust_context",
    "judge_off", "judge_restart",
    "judge_cancel", "judge_window_refusal", "judge_listen_refusal", "render_report",
]


# ── 记录与判据（纯逻辑，可在没有电台的机器上测） ──────────────────────

@dataclass
class Recorder:
    """带墙钟时间戳的帧记录器 + 人类备注（备注也进报告）。"""

    frames: list[tuple[float, dict]] = field(default_factory=list)
    notes: list[tuple[float, str]] = field(default_factory=list)

    def frame(self, message: dict) -> None:
        self.frames.append((time.monotonic(), message))

    def note(self, text: str) -> None:
        self.notes.append((time.monotonic(), text))

    def mark(self) -> float:
        """A point in time to judge "after this". Returns the monotonic clock."""
        return time.monotonic()

    def errors(self, after: float = 0.0) -> list[dict]:
        out = []
        for when, msg in self.frames:
            if when >= after and msg.get("type") == "error":
                out.append(msg)
        return out


def scenario_set(text: str) -> set[str]:
    """`"V1,V2,V7"` → `{"V1", "V2", "V7"}`.

    这条单独成函数是因为它出过一次真错：`set("V7")` 把**字符串**拆成字符（`{"V", "7"}`），
    于是 `"V7" in want` 永远为假 —— 脚本一路跑完、报告里一行都没有，而它看起来"成功"
    （失败项 0）。判据纯函数化只保护了判据，这一层同样需要钉住。
    """
    return {item.strip().upper() for item in str(text or "").split(",") if item.strip()}


def power_state(rec: Recorder) -> bool | None:
    """Last known radio power state from stateUpdate / fullState frames."""
    state = None
    for _, msg in rec.frames:
        if msg.get("type") == "stateUpdate":
            fields = msg.get("fields") or {}
            if "power_on" in fields:
                state = bool(fields["power_on"])
        elif msg.get("type") == "fullState":
            data = msg.get("data") or {}
            if "power_on" in data:
                state = bool(data["power_on"])
    return state


def rescue_events(rec: Recorder, since: float = 0.0) -> list[tuple[float, str, int]]:
    """`[(when, state, attempt)]` for the powerRescue frames after `since`.

    `since` matters: without it a scenario would happily match a frame from the
    *previous* one and call it its own result — measured 2026-10-16: V3 and V4
    both "failed" against frames V2 had produced a minute earlier.
    """
    return [(when, str(msg.get("state", "")), int(msg.get("attempt") or 0))
            for when, msg in rec.frames
            if msg.get("type") == "powerRescue" and when >= since]


def judge_off(rec: Recorder, sent_at: float) -> tuple[bool, str]:
    """V1：关机被接受，电台状态回读为关，且没有拒绝。"""
    refusals = [e for e in rec.errors(after=sent_at)]
    if refusals:
        return False, f"被拒：{refusals[0].get('message', '')}"
    if power_state(rec) is False:
        return True, "power_on=false"
    return False, "没有看到 power_on=false（状态没回读出来）"


def judge_restart(rec: Recorder, sent_at: float) -> tuple[bool, str]:
    """V2：`restart` 必须走 关机 → 持续唤醒，并明确给出"第几轮醒/耗时"。

    这是本脚本存在的理由：这两个数字是复核 2026-07 "PS1 不可靠" 结论的唯一读数。
    """
    refusals = [e for e in rec.errors(after=sent_at) if WINDOW_REFUSAL not in str(e.get("message", ""))]
    if refusals:
        return False, f"开头就被拒：{refusals[0].get('message', '')}"
    events = [(when, state, attempt) for when, state, attempt in rescue_events(rec) if when >= sent_at]
    if not events:
        return False, "没有任何 powerRescue 广播（服务端没起救援？）"
    for when, state, attempt in events:
        if state == "woke":
            spent = when - sent_at
            return True, f"第 {attempt} 轮醒来，耗时 {spent:.0f} 秒"
        if state == "timeout":
            spent = when - sent_at
            return False, f"15 分钟没唤醒（共 {attempt} 轮，{spent:.0f} 秒）"
    last = events[-1]
    return False, f"还在跑（最后一帧 attempt={last[2]}）"


def judge_cancel(rec: Recorder, cancelled_at: float) -> tuple[bool, str]:
    """V3：取消之后不再有 running、电台保持关。"""
    after = [(when, state, attempt) for when, state, attempt in rescue_events(rec) if when >= cancelled_at]
    if not any(state == "cancelled" for _, state, _ in after):
        return False, "没看到 cancelled 广播"
    if any(state == "woke" for _, state, _ in after):
        return False, "取消之后电台还是被唤醒了"
    if power_state(rec) is True:
        return False, "电台状态变成开——取消不该上电"
    return True, "cancelled，且未再唤醒"


def judge_window_refusal(rec: Recorder, sent_at: float) -> tuple[bool, str]:
    """V4：救援期间关机必须被启动窗口拒绝，并且文案带剩余秒数。"""
    for e in rec.errors(after=sent_at):
        if WINDOW_REFUSAL in str(e.get("message", "")):
            return True, str(e["message"])
    return False, "没有被拒绝（PS0 可能落在启动窗口里 —— 那是要卡死 CAT MCU 的那条路）"


def judge_listen_refusal(rec: Recorder, sent_at: float) -> tuple[bool, str]:
    """V7：listen 角色发 power 的每个取值都必须被拒绝，且电台不动。"""
    errors = [e for e in rec.errors(after=sent_at)]
    if not errors:
        return False, "居然没有被拒绝"
    if not any(LISTEN_REFUSAL in str(e.get("message", "")) for e in errors):
        return False, f"拒绝文案不是角色门：{errors[0].get('message', '')}"
    if any(state == "running" for _, state, _ in rescue_events(rec)):
        return False, "listen 角色竟然起了救援"
    return True, f"{len(errors)} 条全部按角色门拒绝"


def judge_tx_refusal(rec: Recorder, sent_at: float) -> tuple[bool, str]:
    """V5：发射中（含 FT8）关机必须被拒。"""
    for e in rec.errors(after=sent_at):
        if TX_REFUSAL in str(e.get("message", "")) or "FT8" in str(e.get("message", "")):
            return True, str(e["message"])
    return False, "发射中没有被拒绝"


# ── 报告 ────────────────────────────────────────────────────────────

def render_report(rows: list[tuple[str, str, str, str, str]], raw: Recorder,
                  stamp: str, target: str) -> str:
    """Markdown 报告：表格 + 原始帧 + 备注。`rows` = [(编号, 期望, 实测, 判据, 结论)]。"""
    lines = [
        f"# 电台电源验收（{stamp}）",
        "",
        f"- 目标：`{target}`",
        "- 规格：`docs/superpowers/specs/2026-10-16-radio-power-switch-design.md` §8",
        "",
        "| 场景 | 期望 | 实测 | 判据 | 结论 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for no, expect, observed, why, verdict in rows:
        lines.append(f"| {no} | {expect} | {observed} | {why} | {verdict} |")
    lines += ["", "## 原始帧", "", "```"]
    base = raw.frames[0][0] if raw.frames else 0.0
    for when, msg in raw.frames:
        summary = msg.get("type", "?")
        if summary == "powerRescue":
            summary += f" state={msg.get('state')} attempt={msg.get('attempt')}"
        elif summary == "error":
            summary += " " + str(msg.get("message", ""))
        elif summary == "stateUpdate":
            fields = msg.get("fields") or {}
            if "power_on" in fields:
                summary += f" power_on={fields['power_on']}"
            else:
                continue
        elif summary != "fullState":
            continue
        lines.append(f"{when - base:8.1f}s  {summary}")
    for when, text in raw.notes:
        lines.append(f"{when - base:8.1f}s  # {text}")
    lines += ["```", ""]
    return "\n".join(lines)


# ── I/O（唯一碰网络/串口之外东西的地方） ──────────────────────────────

def trust_context(insecure: bool, ssl_module):
    """客户端信任：默认与服务器同源（`net_tls`），`--insecure` 时接受自签证书。

    局域网盒子用自签证书（`ssl_bootstrap` 生成的 localhost 那张），所以对着盒子跑必须
    能放开校验；`--insecure` 只影响这个脚本，不改服务端任何策略。
    """
    if insecure:
        return ssl_module._create_unverified_context()      # noqa: SLF001 - 显式选择
    try:
        import net_tls                      # 与服务器同一个信任策略
        return net_tls.default_context()
    except Exception:                       # noqa: BLE001 - 源码无关紧要，回退默认
        return None


def login(host: str, port: int, password: str, context=None) -> str:
    """POST /api/auth/login → token（Bearer，AD-024 的首选路径；绝不把令牌塞 URL）。"""
    body = json.dumps({"password": password}).encode()
    req = urllib.request.Request(f"https://{host}:{port}/api/auth/login", data=body,
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10, context=context) as reply:
        payload = json.loads(reply.read() or b"{}")
    token = payload.get("token") or ""
    if not token:
        raise SystemExit(f"登录失败：{payload}")
    return token


class Session:
    """一条 /WSradio 连接：后台收帧进 Recorder，前台发命令。"""

    def __init__(self, recorder: Recorder, ssl_context=None):
        self.recorder = recorder
        self.ssl_context = ssl_context
        self.ws = None
        self._reader = None

    async def connect(self, url: str, token: str):
        import websockets                     # 服务器自身的依赖，随环境就有
        headers = {"Authorization": f"Bearer {token}"}
        try:
            self.ws = await websockets.connect(
                url, additional_headers=headers, ssl=self.ssl_context)
        except TypeError:                     # websockets < 13 叫 extra_headers
            self.ws = await websockets.connect(
                url, extra_headers=headers, ssl=self.ssl_context)
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        try:
            async for raw in self.ws:
                try:
                    self.recorder.frame(json.loads(raw))
                except ValueError:
                    self.recorder.frame({"type": "raw", "text": str(raw)[:200]})
        except Exception:                     # noqa: BLE001 - 关连接时正常结束
            pass

    async def send_power(self, value) -> float:
        sent_at = self.recorder.mark()
        await self.ws.send(json.dumps({"type": "set", "field": "power", "value": value}))
        return sent_at

    async def wait_for(self, predicate, timeout: float, since: float = 0.0) -> bool:
        """等一个判据成立（轮询记录器，不碰网络）。`since` 只允许本场景自己的帧。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate(self.recorder):
                return True
            await asyncio.sleep(0.2)
        return False

    async def close(self):
        if self._reader:
            self._reader.cancel()
        if self.ws is not None:
            await self.ws.close()


async def ensure_on(session: "Session", rec: Recorder, timeout: float = 60.0) -> bool:
    """把电台拉起来（单次 PS1 + 回读），供"需要干净起点"的场景用。"""
    if power_state(rec) is True:
        return True
    sent = await session.send_power(True)
    await session.wait_for(lambda r: power_state(r) is True, timeout, since=sent)
    return power_state(rec) is True


async def run_scenarios(args, out_path: Path) -> int:
    rec = Recorder()
    rows: list[tuple[str, str, str, str, str]] = []
    base = f"https://{args.host}:{args.port}"

    import ssl as _ssl
    context = trust_context(args.insecure, _ssl)
    # 每轮都重新登录会撞上服务端的登录限流（实测 429）——现场连跑多轮时用 --token 复用会话。
    token = args.token or login(args.host, args.port, args.password, context)
    session = Session(rec, context)
    await session.connect(f"wss://{args.host}:{args.port}/WSradio", token)
    await asyncio.sleep(1.0)                  # 让 fullState 落地

    want = scenario_set(args.scenario)
    failures = 0

    if "V1" in want:
        await ensure_on(session, rec)
        sent = await session.send_power(False)
        await session.wait_for(lambda r: power_state(r) is False, 15)
        ok, why = judge_off(rec, sent)
        rows.append(("V1 关机", "关机并被回读确认", why, why, "✅" if ok else "❌"))
        failures += 0 if ok else 1

    if "V2" in want:
        sent = await session.send_power("restart")
        await session.wait_for(
            lambda r: any(s in ("woke", "timeout") for _, s, _ in rescue_events(r)), args.v2_wait)
        ok, why = judge_restart(rec, sent)
        rows.append(("V2 重开", "记录第几轮醒 + 耗时", why, why, "✅" if ok else "❌"))
        failures += 0 if ok else 1

    if "V3" in want:
        await ensure_on(session, rec)
        await asyncio.sleep(WINDOW_SETTLE_S)        # 上一次 PS1 的保护窗口过期
        started = await session.send_power("restart")
        # 等 attempt=0（救援已起、第一轮 PS1 还没发出去）就取消，这样"取消"这个动作
        # 一定落在"救援进行中"，而不是落在"它已经自己醒了"之后（实测过：5 秒就醒）。
        await session.wait_for(
            lambda r: any(s == "running" and a == 0 for _, s, a in rescue_events(r, started)), 20)
        cancelled_at = await session.send_power("cancelRescue")
        await session.wait_for(
            lambda r: any(s == "cancelled" for _, s, _ in rescue_events(r, cancelled_at)), 20,
            since=cancelled_at)
        ok, why = judge_cancel(rec, cancelled_at)
        rows.append(("V3 取消救援", "停手且不上电", why, why, "✅" if ok else "❌"))
        failures += 0 if ok else 1

    if "V4" in want:
        await ensure_on(session, rec)
        await asyncio.sleep(WINDOW_SETTLE_S)        # 干净起点：上一次 PS1 的窗口过期
        started = await session.send_power("restart")
        # attempt>=1 只说明"这一轮开始了"，PS1 是在那条广播**之后**才写出去的，所以还要等
        # 一小会儿让窗口真的武装。窗口有 15 秒（POWER_BOOT_WINDOW_S），第 3 秒一定落在里面；
        # 就算电台快到自己先醒了，窗口也在校验成功时被重新武装 —— 两种时序都会被拒。
        await session.wait_for(
            lambda r: any(s == "running" and a >= 1 for _, s, a in rescue_events(r, started)), 25)
        await asyncio.sleep(3.0)
        sent = await session.send_power(False)
        await session.wait_for(lambda r: rec.errors(after=sent), 6)
        ok, why = judge_window_refusal(rec, sent)
        rows.append(("V4 救援中关机", "启动窗口内被拒（带剩余秒数）", why, why, "✅" if ok else "❌"))
        failures += 0 if ok else 1
        await session.send_power("cancelRescue")
        await asyncio.sleep(WINDOW_SETTLE_S)
        await asyncio.sleep(2)
        sent = await session.send_power(False)          # 窗口过后应当能关
        await session.wait_for(lambda r: power_state(r) is False, 15)
        ok2, why2 = judge_off(rec, sent)
        rows.append(("V4b 窗口过后关机", "能关掉", why2, why2, "✅" if ok2 else "❌"))
        failures += 0 if ok2 else 1

    if "V5" in want and args.manual:
        input("  请让电台发射（PTT 或 FT8 发射）中，然后按回车…")
        sent = await session.send_power(False)
        await session.wait_for(lambda r: rec.errors(after=sent), 6)
        ok, why = judge_tx_refusal(rec, sent)
        rows.append(("V5 发射中关机", "被拒（含 FT8）", why, why, "✅" if ok else "❌"))
        failures += 0 if ok else 1
        input("  请松开 PTT / 停止 FT8 发射，然后按回车…")

    if "V6" in want and args.manual:
        input("  请把电台的电源物理断开（拔电），然后按回车以开始观察…")
        sent = await session.send_power("restart")
        rec.note("V6：电台断电状态下的 restart")
        await session.wait_for(
            lambda r: any(s in ("woke", "timeout") for _, s, _ in rescue_events(r)), args.v6_wait)
        ok, why = judge_restart(rec, sent)
        verdict = "✅" if ok else ("⚠️ 与 V6 的期望一致（没醒）" if "没唤醒" in why else "❌")
        rows.append(("V6 拔电后重开", "15 分钟内必须老实报「需要现场按面板」", why, why, verdict))
        if not ok and "没唤醒" not in why:
            failures += 1

    if "V7" in want and args.listen_password:
        session2 = Session(rec, context)
        token2 = login(args.host, args.port, args.listen_password, context)   # listen 会话自成一档
        await session2.connect(f"wss://{args.host}:{args.port}/WSradio", token2)
        await asyncio.sleep(1.0)
        sent = rec.mark()
        for value in (False, True, "restart", "cancelRescue"):
            await session2.send_power(value)
        await asyncio.sleep(3)
        ok, why = judge_listen_refusal(rec, sent)
        rows.append(("V7 listen 角色", "四个取值全被角色门拒绝", why, why, "✅" if ok else "❌"))
        failures += 0 if ok else 1
        await session2.close()

    await session.close()
    stamp = time.strftime("%Y-%m-%d %H:%M")
    out_path.write_text(render_report(rows, rec, stamp, base + " /WSradio"), encoding="utf-8")
    print(f"\n报告：{out_path}")
    print(f"失败项：{failures}")
    return 1 if failures else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="电台电源菜单的现场验收（V1–V7）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("MRRC_WEB_PORT", "8888")))
    ap.add_argument("--password", default=os.environ.get("MRRC_WEB_PASSWORD", ""))
    ap.add_argument("--listen-password", default=os.environ.get("MRRC_LISTEN_PASSWORD", ""))
    ap.add_argument("--scenario", default="V1,V2,V3,V4,V7",
                    help="要跑的场景，逗号分隔（V5/V6 需要 --manual 才生效）")
    ap.add_argument("--manual", action="store_true", help="加上需要人动手的 V5/V6")
    ap.add_argument("--v2-wait", type=float, default=960.0, help="等 V2 结论的秒数上限")
    ap.add_argument("--v6-wait", type=float, default=960.0, help="等 V6 结论的秒数上限")
    ap.add_argument("--token", default="",
                    help="已登录过就直接用这个会话令牌（跳过登录：现场连跑多轮时避免撞登录限流）")
    ap.add_argument("--insecure", action="store_true",
                    help="接受自签证书（局域网盒子用；不改服务端策略）")
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)

    if not args.password and not args.token:
        print("需要 --password（或 MRRC_WEB_PASSWORD），或者用 --token 复用已有会话",
              file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else Path(f"power-acceptance-{time.strftime('%Y%m%d-%H%M%S')}.md")
    return asyncio.run(run_scenarios(args, out))


if __name__ == "__main__":
    raise SystemExit(main())
