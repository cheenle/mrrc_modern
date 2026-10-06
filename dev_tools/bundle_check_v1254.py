# -*- coding: utf-8 -*-
"""v1.25.4 冻结字节码走查：把包里的字节码和本来要冻进去的源码逐字节比对。

第 3 层（符号走查）存在的唯一理由：v1.25.3 出过一次「门禁全绿、version.txt 对、
size/SHA 也都变了，包里却是修复前的 server.py」—— PyInstaller 的 workpath 缓存
按 (size, mtime) 决定要不要重分析，tar 又原样保留了构建 Mac 的 mtime，于是
server.py 比缓存「旧」就被整段跳过了。看 mtime/size/SHA 一律抓不到这种。

比对方法：co_code（指令字节）逐字节相等，且 co_consts 里的字符串集合相等
（字符串常量里嵌着源码行号，行号漂了就说明冻的不是这一份源码）。

用法（在 VM 上，用带 PyInstaller 的那个 venv）：
    venv\\Scripts\\python.exe C:\\tools\\bundle_check_v1254.py
"""
import marshal
import os
import sys
import tempfile
import types

from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

SRC = r"C:\mrrc_modern"
EXE = r"C:\mrrc_modern\dist\windows\MRRC-Modern\MRRC-Modern-Server.exe"

failures = []


def _collect(k, names, consts):
    if isinstance(k, types.CodeType):
        walk(k, names, consts)
    elif isinstance(k, str):
        consts.add(k)
    elif isinstance(k, (tuple, frozenset, list, set)):
        # 元组不可漏：["ps", "-eo", "pid=,command="] 在 co_consts 里是**一个元组常量**，
        # 只收 str 的走查会对它视而不见，然后报假 MISS（本脚本第一版就栽在这）。
        for item in k:
            _collect(item, names, consts)


def walk(code, names, consts):
    names.update(code.co_names)
    for k in code.co_consts:
        _collect(k, names, consts)
    return names, consts


def find_code(code, name):
    """在 code 及其嵌套里按名字找一个 code 对象。"""
    for k in code.co_consts:
        if isinstance(k, types.CodeType):
            if k.co_name == name:
                return k
            hit = find_code(k, name)
            if hit is not None:
                return hit
    return None


def load_entry(reader, name):
    raw = reader.extract(name)
    try:
        return marshal.loads(raw), 0
    except Exception:
        return marshal.loads(raw[8:]), 8          # 可能带 8 字节头


def compare(label, frozen, source):
    ok = True
    if frozen is None or source is None:
        print("  MISSING  %s (frozen=%r source=%r)" % (label, frozen is not None, source is not None))
        failures.append(label + ": not found")
        return
    if frozen.co_code != source.co_code:
        print("  DIFF     %s: co_code %d vs %d bytes"
              % (label, len(frozen.co_code), len(source.co_code)))
        failures.append(label + ": co_code")
        ok = False
    f_names, f_consts = walk(frozen, set(), set())
    s_names, s_consts = walk(source, set(), set())
    if f_consts != s_consts:
        only_src = sorted(c for c in s_consts - f_consts if len(c) < 60)
        only_frz = sorted(c for c in f_consts - s_consts if len(c) < 60)
        print("  DIFF     %s: consts differ; only-in-source=%s only-in-frozen=%s"
              % (label, only_src[:4], only_frz[:4]))
        failures.append(label + ": co_consts")
        ok = False
    if f_names != s_names:
        print("  DIFF     %s: names differ; only-in-source=%s only-in-frozen=%s"
              % (label, sorted(s_names - f_names)[:6], sorted(f_names - s_names)[:6]))
        failures.append(label + ": co_names")
        ok = False
    if not source.co_consts or source.co_firstlineno != frozen.co_firstlineno:
        print("  DIFF     %s: firstlineno %d vs %d"
              % (label, frozen.co_firstlineno, source.co_firstlineno))
        failures.append(label + ": firstlineno")
        ok = False
    if ok:
        print("  OK       %s (co_code %d bytes)" % (label, len(frozen.co_code)))


def src_code(relpath, module_name):
    path = os.path.join(SRC, relpath)
    with open(path, encoding="utf-8") as fh:
        return compile(fh.read(), module_name, "exec")


def self_control(frozen_root, source_root):
    """比对逻辑本身必须报得出差异。

    拿两个功能毫不相干的函数去比 —— 如果这一步也说"一致"，那说明 compare()
    根本没在比（对象取错、比较分支走空），后面所有的 OK 都是空的。
    """
    before = len(failures)
    compare("SELF-CONTROL（期望 DIFF）", find_code(frozen_root, "lifespan"),
            find_code(source_root, "_configured_ssl_cert"))
    detected = len(failures) > before
    del failures[before:]
    print("  自对照确实报出差异 =", detected)
    if not detected:
        failures.append("SELF-CONTROL: compare() 无法报出差异，整份走查不可信")
    return detected


print("== 入口 server（CArchive 条目，不是 PYZ 模块）==")
reader = CArchiveReader(EXE)
keys = list(reader.toc.keys()) if hasattr(reader.toc, "keys") else list(reader.toc)
if "server" not in keys:
    print("  归档条目里没有 'server'，实有:", keys[:10])
    failures.append("CArchive 没有 server 条目")
    raise SystemExit(1)
frozen_server, header = load_entry(reader, "server")
print("  entry header bytes =", header)
source_server = src_code("server.py", "server")
self_control(frozen_server, source_server)
compare("server.lifespan", find_code(frozen_server, "lifespan"),
        find_code(source_server, "lifespan"))

print("== PYZ 模块 ==")
pyz_name = None
for k, v in reader.toc.items():
    typecode = getattr(v, "typecode", None) or (v[4] if len(v) > 4 else None)
    if typecode == "z":
        pyz_name = k
        break
fd, tmp = tempfile.mkstemp(suffix=".pyz")
os.write(fd, reader.extract(pyz_name))
os.close(fd)
za = ZlibArchiveReader(tmp)
mods = set(za.toc.keys())
print("  PYZ 模块数 =", len(mods))
for want in ("cloud_hub", "net_tls", "ssl_bootstrap", "upgrade_core", "config"):
    print("  %-14s in PYZ: %s" % (want, want in mods))
    if want not in mods:
        failures.append("PYZ missing " + want)

frozen_hub = za.extract("cloud_hub")
if isinstance(frozen_hub, (bytes, bytearray)):   # 这版直接给 code 对象，别版本给 marshal 字节
    frozen_hub = marshal.loads(frozen_hub)
source_hub = src_code("cloud_hub.py", "cloud_hub")
for fn in ("_terminate", "_kill_stale_frpc", "_enumerate_frpc", "_stale_frpc_pids",
           "_windows_norm"):
    compare("cloud_hub." + fn, find_code(frozen_hub, fn), find_code(source_hub, fn))

hub_names, hub_consts = walk(frozen_hub, set(), set())
print("  cloud_hub: 'signal' in names =", "signal" in hub_names,
      "| 'ps' in consts =", "ps" in hub_consts,
      "| '-eo' in consts =", "-eo" in hub_consts,
      "| 'pid=,command=' in consts =", "pid=,command=" in hub_consts,
      "| 'SIGTERM' in names =", "SIGTERM" in hub_names)
for sym, hay in (("signal", hub_names), ("-eo", hub_consts), ("SIGTERM", hub_names)):
    if sym not in hay:
        failures.append("cloud_hub missing symbol " + sym)

print()
if failures:
    print("FAILED (%d):" % len(failures))
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")
