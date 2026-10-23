"""下载类 PowerShell 工具的公共静态守卫。

三条都在现场踩过：
* **UTF-8 无 BOM** —— Windows PowerShell 5.1 按 ANSI 代码页解码无 BOM 的 UTF-8，
  中文变乱码，字符串字面量甚至会解析失败（2026-10-16，CertDoctor 现场）。
* **`.cmd` 里有非 ASCII** —— cmd.exe 用同一套代码页，中文提示会变问号，
  而 `if (...)` 块里的乱码能让整段脚本不执行。
* **`( ... )` 块内的 `echo` 带括号** —— cmd 在第一个 `)` 结束这个块，
  报 `… was unexpected at this time`，工具根本没起来。

放在共享模块里，是因为第二份拷贝晚于第一份的修复，而复制来的 bug 不会自己变旧。
"""
from __future__ import annotations

from pathlib import Path


def assert_utf8_bom(test, path, what="PowerShell 5.1") -> None:
    raw = Path(path).read_bytes()
    test.assertTrue(
        raw.startswith(b"\xef\xbb\xbf"),
        f"{what} 会把无 BOM 的 UTF-8 当 ANSI 解码：{path} 必须以 UTF-8 BOM 开头",
    )
    test.assertTrue(raw[3:].decode("utf-8").strip(), f"{path} 不能是空文件")


def assert_ascii_cmd(test, path, must_contain=()) -> None:
    raw = Path(path).read_bytes()
    raw.decode("ascii")  # 非 ASCII 直接抛 UnicodeDecodeError
    for needle in must_contain:
        test.assertIn(needle.encode("ascii"), raw, f"{path} 里少了 {needle!r}")


def assert_no_parens_in_blocks(test, path) -> None:
    """块内 echo/rem 不得含括号：cmd 在第一个 `)` 结束块。"""
    text = Path(path).read_text(encoding="ascii")
    depth = 0
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.lower().startswith(("echo", "rem")):
            if depth > 0 and ("(" in line or ")" in line):
                test.fail(
                    f"{Path(path).name}:{lineno}: 括号出现在 ( ... ) 块内的 echo/rem 里，"
                    f"cmd.exe 会提前结束这个块：{line!r}"
                )
            continue
        depth += line.count("(") - line.count(")")
        test.assertGreaterEqual(depth, 0, f"{Path(path).name}:{lineno}: 右括号多于左括号")
