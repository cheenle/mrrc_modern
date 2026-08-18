#!/usr/bin/env python3
"""
FT-710 电源控制 —— 直接读写串口 CAT 设备,不经过服务器 / WebSocket。

用法:
    python3 ft710_power.py on        # 开机  (PS1; + FA 回读验证, 最多 3 次重试)
    python3 ft710_power.py off       # 关机  (PS0; 连发两次, 不要求应答——断电后电台 CAT 静默)
    python3 ft710_power.py status    # 查询电源状态 (PS; 读)
    python3 ft710_power.py cycle     # 断电 -> 等待 8s -> 上电

默认串口:  /dev/cu.usbserial-0121DB3A0 @ 38400
可用环境变量覆盖:  FT710_SERIAL_PORT  FT710_BAUD_RATE

安全规则 (来自 FT-710_CAT_Knowledge_Base 实测 2026-07-27/28):
  * PS0 关机可靠; PS1 开机不可靠 —— 待机时电台可能忽略, 必须 FA; 回读验证。
  * PS1 之后 15 秒内禁止发 PS0 —— 启动中途关机会打挂 CAT MCU,
    需物理断电才能恢复。
  * PS0 是写后即忘命令: 电台断电后 CAT 串口静默, 不要求应答 (与 server.py
    一致)。 关机的判定只看 PS0 之后电台是否仍回答 PS1 —— 仍回答才算失败。
  * 串口被服务器(server.py)或 rigctld 占用时, 直接命令会与它们抢答。
    建议先 ./stop.sh 停掉服务器再运行本脚本。
"""

import os
import sys
import time

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    sys.exit(
        "缺少 pyserial。请用项目 venv 运行:\n"
        "  /Users/cheenle/HAM/mrrc_modern/venv/bin/python ft710_power.py <on|off|status|cycle>"
    )

PORT = os.environ.get("FT710_SERIAL_PORT", "/dev/cu.usbserial-0121DB3A0")
BAUD = int(os.environ.get("FT710_BAUD_RATE", "38400"))

BOOT_WINDOW_S = 15.0   # PS1 后禁止 PS0 的保护窗口
ON_ATTEMPTS = 3        # PS1 重试次数上限
ON_VERIFY_S = 12.0     # 每次尝试内等待电台应答 FA 的时间上限

# 进程内记忆的 PS1 保护窗口 (monotonic)
_boot_until = 0.0


def open_port() -> serial.Serial:
    s = serial.Serial(
        port=PORT,
        baudrate=BAUD,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=1.0,
        write_timeout=1.0,
    )
    time.sleep(0.3)
    s.reset_input_buffer()
    return s


def send_cmd(s: serial.Serial, cmd: str, timeout: float = 1.0) -> str | None:
    """发送 CAT 命令, 读取并返回去掉结尾 ';' 的应答; 无应答返回 None。"""
    s.timeout = timeout
    s.reset_input_buffer()
    s.write((cmd + ";").encode("ascii"))
    resp = s.read_until(b";")
    if not resp:
        return None
    text = resp.decode("ascii", errors="replace").rstrip(";").strip()
    return text


def warn_if_port_in_use() -> None:
    """串口被其他进程占用时提示(不阻断, macOS 串口可多进程打开但会抢答)。"""
    try:
        import subprocess
        holders = subprocess.run(
            ["lsof", "-t", PORT], capture_output=True, text=True, timeout=5
        ).stdout.split()
        if holders:
            for h in holders:
                proc = subprocess.run(
                    ["ps", "-o", "command=", "-p", h],
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip()
                print(f"  ⚠ 串口正被 pid {h} 占用: {proc[:80]}")
            print("  ⚠ 继续操作会与占用方抢答。建议先停掉服务器(./stop.sh)再执行。")
    except Exception:
        pass


def power_status() -> str | None:
    """返回 'on' / 'off'; 电台无应答返回 None。"""
    with open_port() as s:
        resp = send_cmd(s, "PS")
    if resp is None:
        return None
    if resp.startswith("PS1"):
        return "on"
    if resp.startswith("PS0"):
        return "off"
    return resp


def power_off() -> bool:
    global _boot_until
    if time.monotonic() < _boot_until:
        remain = int(_boot_until - time.monotonic()) + 1
        print(f"✗ 电台刚开过机, 保护窗口未过, 请 {remain}s 后再关机 (防止打挂 CAT MCU)")
        return False
    with open_port() as s:
        # 首包偶发丢失, 连发两次 (与 server.py 一致)
        r1 = send_cmd(s, "PS0")
        time.sleep(0.3)
        r2 = send_cmd(s, "PS0")
    print(f"  PS0 应答: {r1!r} / {r2!r}")

    # PS0 是写后即忘命令 (server.py / cat_controller.py 均不等待应答):
    # 电台断电时 CAT 串口随之静默 —— "无应答" 恰恰是关机成功的正常表现,
    # 不能据此判失败。 只有关机后电台仍回答 PS1 (PS0 被忽略, 电台还在开机)
    # 才算真正的失败。
    time.sleep(1.0)
    st = power_status()
    if st == "on":
        print("  ✗ 电台仍回答 PS1, 关机未生效")
        return False
    if st == "off":
        print("  ✓ 关机确认 (PS 回读: OFF)")
        return True
    print("  ✓ 电台已静默 (符合断电后的表现), 视为关机成功")
    return True


def power_on() -> bool:
    global _boot_until
    for attempt in range(1, ON_ATTEMPTS + 1):
        try:
            with open_port() as s:
                resp = send_cmd(s, "PS1")
            print(f"  第 {attempt} 次 PS1 应答: {resp!r}")
        except Exception as e:
            print(f"  第 {attempt} 次端口未就绪: {e}")

        # 每次 PS1 都刷新保护窗口: 即使 PS1 延迟生效, 也不能让 PS0 落进启动期
        _boot_until = time.monotonic() + BOOT_WINDOW_S

        deadline = time.monotonic() + ON_VERIFY_S
        while time.monotonic() < deadline:
            time.sleep(1.0)
            try:
                with open_port() as s:
                    fa = send_cmd(s, "FA", timeout=0.4)
            except Exception:
                fa = None
            if fa and "?" not in fa:
                print(f"  ✓ 开机已验证 (FA 应答: {fa}), 第 {attempt} 次尝试")
                _boot_until = time.monotonic() + BOOT_WINDOW_S
                return True
        print(f"  第 {attempt} 次: 电台未应答")
    return False


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("on", "off", "status", "cycle"):
        print(__doc__)
        return 2

    action = sys.argv[1]
    print(f"FT-710 电源控制  port={PORT} @ {BAUD}")
    warn_if_port_in_use()

    if action == "status":
        st = power_status()
        if st == "on":
            print("  ✓ 电台电源: ON")
        elif st == "off":
            print("  ✓ 电台电源: OFF")
        else:
            print("  ✗ 电台无应答(未连接/处于深度断电, 或串口被占用)")
            return 1
        return 0

    if action == "on":
        ok = power_on()
        print("  → 开机成功" if ok else "  → 开机失败, 请手动按电源键")
        return 0 if ok else 1

    if action == "off":
        ok = power_off()
        print("  → 关机成功" if ok else "  → 关机失败")
        return 0 if ok else 1

    # cycle: off -> 等待 -> on
    if action == "cycle":
        if not power_off():
            print("  ✗ 关机阶段失败, 中止")
            return 1
        print("  等待 8 秒让电台断电...")
        time.sleep(8)
        if not power_on():
            print("  ✗ 开机阶段失败, 请手动按电源键")
            return 1
        print("  → 重启完成")
        return 0


if __name__ == "__main__":
    sys.exit(main())
