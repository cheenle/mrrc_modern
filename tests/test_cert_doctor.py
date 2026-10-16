"""The downloadable certificate checker must not drift from its source.

Why this exists
---------------
`MRRC-CertDoctor.zip` is a support tool a user downloads and runs on their own Windows
machine, at the moment their app cannot reach the portal. Two drifts are invisible to
everyone until a user is stuck with them:

* **A zip built from an older copy of the script.** The zip is assembled by hand from
  `tools/windows/`, so the published `.ps1` can be several fixes behind the source while
  both still look healthy. The archive is compared here, member by member.
* **A script PowerShell 5.1 cannot read.** The script and the report it writes are Chinese,
  and Windows PowerShell decodes a BOM-less UTF-8 file as the ANSI code page — the text
  turns to mojibake and, in the field, an unterminated string became a parse error. The
  BOM and the ASCII-only `.cmd` (cmd.exe has the same problem with its own code page) are
  asserted rather than trusted.

The bundled `openssl.exe` is pinned to the same payload the installer ships, because the
whole point of the tool is to reproduce *the application's* verdict: swapping in a
different OpenSSL build would make the printed verdict incomparable to the app's.
"""
from __future__ import annotations

import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = ROOT / "tools" / "windows"
DOWNLOADS = ROOT / "website" / "downloads"
PACKAGE = DOWNLOADS / "MRRC-CertDoctor.zip"
PAYLOAD = ROOT / "packaging" / "payload" / "windows-amd64"

SCRIPT = "MRRC-CertDoctor.ps1"
CMD = "MRRC-CertDoctor.cmd"
README = "README.txt"
ARCHIVE_ROOT = "MRRC-CertDoctor"
#: The DLL closure MSYS2's openssl.exe needs; without them it cannot start on a user machine.
OPENSSL_RUNTIME = ("openssl.exe", "openssl.cnf", "libcrypto-3-x64.dll", "libssl-3-x64.dll",
                   "libwinpthread-1.dll", "zlib1.dll", "libiconv-2.dll", "libintl-8.dll",
                   "libbz2-1.dll", "liblzma-5.dll", "libzstd.dll")


class SourceFilesTests(unittest.TestCase):
    def test_script_has_a_utf8_bom(self):
        raw = (SOURCE_DIR / SCRIPT).read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"),
                        "PowerShell 5.1 reads a BOM-less UTF-8 file as ANSI: the Chinese text "
                        "would be mojibake and string literals can fail to parse")
        self.assertTrue(raw[3:].decode("utf-8").strip(), "the script must not be empty")

    def test_launcher_is_ascii_only(self):
        raw = (SOURCE_DIR / CMD).read_bytes()
        raw.decode("ascii")          # raises if any non-ASCII byte sneaked in
        self.assertIn(b"-Fix", raw, "the .cmd must keep offering the repair switch")

    def test_launcher_does_not_put_parentheses_inside_blocks(self):
        """cmd closes a `( ... )` block at the first unescaped `)`, even inside echo text.

        Measured 2026-10-16: `echo ... (MRRC-CertDoctor.ps1)...` inside an `if (...)` block
        made cmd.exe abort with "… was unexpected at this time" — the tool never started.
        """
        text = (SOURCE_DIR / CMD).read_text(encoding="ascii")
        depth = 0
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.lower().startswith(("echo", "rem")):
                if depth > 0 and ("(" in line or ")" in line):
                    self.fail(f"{CMD}:{lineno}: 括号出现在 ( ... ) 块内的 echo/rem 里，"
                              f"cmd.exe 会提前结束这个块：{line!r}")
                continue
            depth += line.count("(") - line.count(")")
            self.assertGreaterEqual(depth, 0, f"{CMD}:{lineno}: 右括号多于左括号")


class PackageTests(unittest.TestCase):
    def setUp(self):
        if not PACKAGE.exists():
            self.skipTest(f"{PACKAGE.name} has not been assembled yet")
        self.zip = zipfile.ZipFile(PACKAGE)

    def _read(self, name: str) -> bytes:
        return self.zip.read(f"{ARCHIVE_ROOT}/{name}")

    def test_archive_carries_the_current_sources(self):
        self.assertEqual((SOURCE_DIR / SCRIPT).read_bytes(), self._read(SCRIPT),
                         "zip 里的脚本与 tools/windows/ 下的源不一致：zip 是用旧副本打的")
        self.assertEqual((SOURCE_DIR / CMD).read_bytes(), self._read(CMD),
                         "zip 里的 .cmd 与源不一致")
        self.assertIn("CertDoctor", self._read(README).decode("utf-8"),
                      "README 必须是给人看的那份说明")

    def test_archive_carries_a_runnable_openssl(self):
        for name in OPENSSL_RUNTIME:
            self.assertIn(f"{ARCHIVE_ROOT}/{name}", self.zip.namelist(),
                          f"包内缺 {name}：用户机上 openssl.exe 起不来，判词就出不来")
        if not (PAYLOAD / "openssl.exe").exists():
            # 构建 VM 的源码包里不含 packaging/payload（构建脚本自带排除表），这一条只能在本机跑。
            self.skipTest("packaging/payload 不在这棵树里（源码包已排除），无法比对")
        self.assertEqual((PAYLOAD / "openssl.exe").read_bytes(),
                         self._read("openssl.exe"),
                         "包内 openssl.exe 必须与安装包 payload 里那份是同一份（判词才可比）")

    def test_published_loose_copies_match_the_sources(self):
        """The .cmd can be handed out on its own; it then downloads the loose .ps1."""
        self.assertEqual((SOURCE_DIR / SCRIPT).read_bytes(),
                         (DOWNLOADS / SCRIPT).read_bytes())
        self.assertEqual((SOURCE_DIR / CMD).read_bytes(), (DOWNLOADS / CMD).read_bytes())


class DocumentationTests(unittest.TestCase):
    def test_guide_and_answers_page_point_at_the_download(self):
        guide = (ROOT / "docs" / "OPERATION_GUIDE.md").read_text(encoding="utf-8")
        self.assertIn("MRRC-CertDoctor.zip", guide,
                      "操作指南必须给出下载地址，否则用户只能凭猜")
        page = (ROOT / "website" / "answers" / "index.html").read_text(encoding="utf-8")
        self.assertIn("MRRC-CertDoctor.zip", page,
                      "答复页的已知问题卡必须链接到同一个下载地址")
        for rel in ("guide.html", "zh/guide.html"):
            html = (ROOT / "website" / rel).read_text(encoding="utf-8")
            self.assertIn("downloads/MRRC-CertDoctor.zip", html,
                          f"{rel} 是从 OPERATION_GUIDE.md 生成的：改完指南要重跑 build_guide.py")


if __name__ == "__main__":
    unittest.main()
