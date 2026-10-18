"""NanoVNA-H 天线诊断台（spec 2026-10-18）。

三个单元，边界清楚到可以各自替换：

- ``nano.analysis``  纯数学：Γ → SWR/阻抗/谐振，导出 CSV/s1p。没有 I/O。
- ``nano.vna``       唯一碰串口的模块：``scan_bin`` 二进制帧、帧长自校验、端口发现。
- ``nano.flash``     固件升级（第二期）：ROM DFU + 版本回读。没有页面。

``server.py`` 只做路由与守卫（发射中拒绝、一次一件、不抢 CAT 口、角色隔离）。
"""
from nano.analysis import (SWR_CAP, Z0, analyse, compare, resonances, suspect_points,
                           swr, to_csv, to_s1p, to_z)
from nano.vna import (DFU_VID_PID, NANO_VID_PID, DeviceInfo, FrameError, NanoError,
                      NanoVna, PortConflict, Sweep, discover_port, parse_info,
                      parse_scan_bin, scan_command)

__all__ = [
    "SWR_CAP", "Z0", "analyse", "compare", "resonances", "suspect_points", "swr",
    "to_csv", "to_s1p", "to_z",
    "DFU_VID_PID", "NANO_VID_PID", "DeviceInfo", "FrameError", "NanoError",
    "NanoVna", "PortConflict", "Sweep", "discover_port", "parse_info",
    "parse_scan_bin", "scan_command",
]
