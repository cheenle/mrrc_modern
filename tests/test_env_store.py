"""env_store: the one writer of MRRC's env file (design D-8).

Seven code paths used to write it on their own and disagreed in ways that only
a field report reveals — one dropped every comment and re-sorted the keys, one
widened the box's 0640 to 0644, and none could notice a key written between its
own read and its own write. These tests hold the single implementation to all
seven's promises at once.

Design: docs/superpowers/specs/2026-10-08-w103d-setup-ap-design.md §D-8
"""
from __future__ import annotations

import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path

import env_store


TEMPLATE = """\
# MRRC Modern Configuration
# 注释必须活下来：这是运维唯一能读到的说明

# ── Serial ──────────────────────────────────────────────
MRRC_SERIAL_PORT=/dev/ttyUSB0
MRRC_BAUD_RATE=38400

# ── Web Server ──────────────────────────────────────────
MRRC_WEB_PORT=8888
MRRC_WEB_HOST=0.0.0.0
#MRRC_WEB_PASSWORD=
"""


class RenderTests(unittest.TestCase):
    """合并语义（纯函数，不碰磁盘）。"""

    def test_an_existing_key_keeps_its_line_and_its_comment(self):
        out = env_store.render(TEMPLATE, {"MRRC_WEB_PORT": "9000"})
        self.assertIn("MRRC_WEB_PORT=9000\n", out)
        self.assertIn("# ── Web Server ─", out)
        self.assertNotIn("MRRC_WEB_PORT=8888", out)

    def test_a_new_key_is_appended(self):
        out = env_store.render(TEMPLATE, {"MRRC_ATR1000_HOST": "10.0.0.7"})
        self.assertTrue(out.endswith("MRRC_ATR1000_HOST=10.0.0.7\n"))

    def test_keys_this_call_does_not_own_survive(self):
        """规格 D-8 第 2 条：保留不属于自己的键（口令、证书路径、Cloud Hub 写的键）。"""
        out = env_store.render(TEMPLATE, {"MRRC_WEB_PORT": "9000"})
        self.assertIn("MRRC_SERIAL_PORT=/dev/ttyUSB0", out)
        self.assertIn("MRRC_BAUD_RATE=38400", out)
        self.assertIn("MRRC_WEB_HOST=0.0.0.0", out)

    def test_a_commented_out_key_is_not_treated_as_that_key(self):
        """`#MRRC_WEB_PASSWORD=` 是文档，不是一个待改的键。

        `install.sh` 的模板与 `macos/default.env` 都靠注释掉的键做说明；把它当成键来
        改，会把运维的说明变成一条真的配置。
        """
        out = env_store.render(TEMPLATE, {"MRRC_WEB_PASSWORD": "s3cret"})
        self.assertIn("#MRRC_WEB_PASSWORD=", out, "the commented line is documentation")
        self.assertTrue(out.endswith("MRRC_WEB_PASSWORD=s3cret\n"))

    def test_the_value_is_taken_verbatim_after_the_first_equals(self):
        """`MRRC_SSL_CERT=/path/a=b.pem` 里只有第一个 `=` 是分隔符。"""
        out = env_store.render("", {"MRRC_SSL_CERT": "/etc/ssl/a=b.pem"})
        self.assertEqual(out, "MRRC_SSL_CERT=/etc/ssl/a=b.pem\n")

    def test_only_if_absent_leaves_a_present_key_alone(self):
        """两份 firstboot 与 box-overlay 的 headless defaults 就是这个语义。"""
        out = env_store.render(
            TEMPLATE,
            {"MRRC_WEB_PORT": "9000",
             "MRRC_SSL_CERT": "/var/lib/mrrc/certs/server.crt"},
            only_if_absent=True,
        )
        self.assertIn("MRRC_WEB_PORT=8888", out)
        self.assertNotIn("9000", out)
        self.assertIn("MRRC_SSL_CERT=/var/lib/mrrc/certs/server.crt", out)

    def test_an_empty_body_renders_as_a_single_newline(self):
        """字节兼容：今天的实现是 `"\\n".join(out) + "\\n"`。"""
        self.assertEqual(env_store.render("", {}), "\n")

    def test_a_file_without_a_trailing_newline_gains_one(self):
        self.assertEqual(env_store.render("MRRC_WEB_PORT=1", {}), "MRRC_WEB_PORT=1\n")


class ParseTests(unittest.TestCase):
    def test_comments_blanks_and_valueless_lines_are_skipped(self):
        parsed = env_store.parse_env_text(TEMPLATE)
        self.assertEqual(parsed["MRRC_SERIAL_PORT"], "/dev/ttyUSB0")
        self.assertEqual(parsed["MRRC_BAUD_RATE"], "38400")
        self.assertNotIn("MRRC_WEB_PASSWORD", parsed, "commented out is not set")

    def test_values_are_stripped(self):
        self.assertEqual(env_store.parse_env_text("A=  x  \n")["A"], "x")

    def test_the_last_occurrence_of_a_duplicated_key_wins(self):
        """systemd 的 EnvironmentFile 也是后者胜；两份不一致时别发明第三种。"""
        self.assertEqual(env_store.parse_env_text("A=1\nA=2\n")["A"], "2")

    def test_render_then_parse_round_trips_the_update(self):
        body = env_store.render(TEMPLATE, {"MRRC_WEB_PORT": "9000"})
        self.assertEqual(env_store.parse_env_text(body)["MRRC_WEB_PORT"], "9000")


#: 现场那份损坏文件：em dash `e2 80 94` 被 ANSI 编辑器写成了 `e2 80 3f`。
DAMAGED = (b"# mrrc_modern.env \xe2\x80?MRRC Modern launcher "
           b"configuration template.\nMRRC_WEB_PASSWORD=secret\n"
           b"MRRC_RADIO_MODEL=ft710\n")


class ReadEnvTextTests(unittest.TestCase):
    """容错读：一个在运维自己的 PC 上、用他的编辑器存过的文件。"""

    def _write(self, tmp: str, raw: bytes) -> Path:
        path = Path(tmp) / "mrrc.env"
        path.write_bytes(raw)
        return path

    def test_the_field_regression_file_is_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            text, enc = env_store.read_env_text(self._write(tmp, DAMAGED))
        self.assertIn("MRRC_WEB_PASSWORD=secret", text)
        self.assertIn("MRRC_RADIO_MODEL=ft710", text)
        self.assertNotEqual(enc, "utf-8")

    def test_gbk_values_survive(self):
        name = "麦克风 (USB Audio CODEC)"
        raw = ("# 配置\nMRRC_AUDIO_RX_DEVICE=" + name + "\n").encode("cp936")
        with tempfile.TemporaryDirectory() as tmp:
            text, enc = env_store.read_env_text(self._write(tmp, raw))
        self.assertNotEqual(enc, "utf-8")
        self.assertIn(f"MRRC_AUDIO_RX_DEVICE={name}", text)

    def test_utf16_saved_file_is_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = "MRRC_WEB_PORT=8888\r\n".encode("utf-16")
            text, enc = env_store.read_env_text(self._write(tmp, raw))
        self.assertIn("MRRC_WEB_PORT=8888", text)

    def test_utf8_files_stay_untouched(self):
        raw = "# 中文注释 — em dash\nMRRC_RADIO_MODEL=ic7300\n".encode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            text, enc = env_store.read_env_text(self._write(tmp, raw))
        self.assertEqual(enc, "utf-8")
        self.assertIn("中文注释 — em dash", text)

    def test_a_missing_file_raises_rather_than_inventing_config(self):
        """读不存在的文件是调用者的错（它应该先 `load()`），不是这里该吞的。"""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                env_store.read_env_text(Path(tmp) / "nope.env")


class LoadTests(unittest.TestCase):
    def test_a_missing_file_loads_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(env_store.load(Path(tmp) / "nope.env"), {})

    def test_load_is_read_then_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mrrc.env"
            path.write_bytes(DAMAGED)
            self.assertEqual(env_store.load(path)["MRRC_RADIO_MODEL"], "ft710")


class UpdateEnvFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "mrrc.env"
        # newline="" keeps the fixture byte-identical to what `render` writes:
        # Windows would otherwise translate \n to \r\n, and the "unchanged"
        # byte comparison below would see a difference the fixture invented.
        self.path.write_text(TEMPLATE, encoding="utf-8", newline="")

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_merge_keeps_the_comment_the_key_sat_under(self):
        """写入点 #3 今天会把整个文件的注释删光。"""
        env_store.update_env_file(self.path, {"MRRC_WEB_PORT": "9000"})
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# ── Web Server ─", text)
        self.assertIn("# 注释必须活下来", text)
        self.assertIn("MRRC_WEB_PORT=9000", text)

    def test_a_key_it_does_not_own_survives_on_disk(self):
        env_store.update_env_file(self.path, {"MRRC_WEB_PORT": "9000"})
        loaded = env_store.load(self.path)
        self.assertEqual(loaded["MRRC_SERIAL_PORT"], "/dev/ttyUSB0")
        self.assertEqual(loaded["MRRC_WEB_HOST"], "0.0.0.0")

    def test_it_creates_the_file_and_its_parents(self):
        target = Path(self._tmp.name) / "env" / "deep" / "mrrc.env"
        env_store.update_env_file(target, {"MRRC_WEB_PORT": "8888"})
        self.assertEqual(env_store.load(target)["MRRC_WEB_PORT"], "8888")

    def test_a_non_utf8_file_is_healed_to_utf8_by_the_write(self):
        self.path.write_bytes(DAMAGED)
        env_store.update_env_file(self.path, {"MRRC_WEB_PORT": "8443"})
        text = self.path.read_bytes().decode("utf-8")   # must not raise
        self.assertIn("MRRC_WEB_PORT=8443", text)
        self.assertIn("MRRC_WEB_PASSWORD=secret", text)

    def test_only_if_absent_leaves_the_present_value_alone(self):
        env_store.update_env_file(self.path, {"MRRC_WEB_PORT": "9000"},
                                  only_if_absent=True)
        self.assertEqual(env_store.load(self.path)["MRRC_WEB_PORT"], "8888")

    def test_no_temporary_file_is_left_behind(self):
        env_store.update_env_file(self.path, {"MRRC_WEB_PORT": "9000"})
        leftovers = [p.name for p in Path(self._tmp.name).iterdir()
                     if p.name.endswith(".tmp")]
        self.assertEqual([], leftovers)

    def test_the_result_reports_what_changed_and_what_was_added(self):
        result = env_store.update_env_file(
            self.path,
            {"MRRC_WEB_PORT": "9000", "MRRC_ATR1000_HOST": "10.0.0.7"},
        )
        self.assertEqual(result.changed, {"MRRC_WEB_PORT": "9000"})
        self.assertEqual(result.added, ("MRRC_ATR1000_HOST",))
        self.assertEqual(result.encoding, "utf-8")
        self.assertTrue(result.written)
        self.assertEqual(result.path, self.path)

    def test_a_write_that_changes_nothing_says_so(self):
        """`written=False` 是调用者"要不要重启"的判据之一，不能总是 True。"""
        result = env_store.update_env_file(self.path, {"MRRC_WEB_PORT": "8888"})
        self.assertFalse(result.written)
        self.assertEqual(result.changed, {})
        self.assertEqual(result.added, ())


@unittest.skipIf(os.name == "nt", "POSIX file modes; Windows has no 0640/0600")
class FileModeTests(unittest.TestCase):
    """盒子的 env 文件是 `0640 mrrc:mrrc`，里面是 Web 口令。"""

    def test_an_atomic_write_keeps_the_mode_of_the_file_it_replaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mrrc.env"
            path.write_text("MRRC_WEB_PASSWORD=secret\n", encoding="utf-8")
            os.chmod(path, 0o640)
            env_store.update_env_file(path, {"MRRC_WEB_PORT": "8888"})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_the_mutation_that_caused_the_widening_is_caught(self):
        """把保持 mode 的那行删掉，本用例必须红——它就是云接入今天的写法。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mrrc.env"
            path.write_text("A=1\n", encoding="utf-8")
            os.chmod(path, 0o600)
            env_store.update_env_file(path, {"B": "2"})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


class LockTests(unittest.TestCase):
    """规格 R4：并发改不丢字段（整文件不可 last-write-wins）。"""

    def test_a_second_writer_waits_rather_than_interleaving(self):
        """确定性版本：锁真的在拦人（不依赖时序，所以不会闪红）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mrrc.env"
            path.write_text("A=1\n", encoding="utf-8")
            with env_store._locked(path, 1.0):
                with self.assertRaises(env_store.EnvLockTimeout):
                    env_store.update_env_file(path, {"B": "2"}, lock_timeout=0.2)

    def test_two_writers_in_parallel_lose_neither_field(self):
        """真并发版本：两个入口各改一个字段，第三个字段没丢。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mrrc.env"
            path.write_text("MRRC_WEB_PASSWORD=keepme\n", encoding="utf-8")
            errors: list[BaseException] = []
            barrier = threading.Barrier(2)

            def writer(key: str, value: str) -> None:
                try:
                    barrier.wait(timeout=5)
                    for i in range(25):
                        env_store.update_env_file(path, {key: f"{value}{i}"})
                except BaseException as exc:      # noqa: BLE001 - reported below
                    errors.append(exc)

            threads = [
                threading.Thread(target=writer, args=("MRRC_WEB_PORT", "9")),
                threading.Thread(target=writer, args=("MRRC_SCOPE_BAUD", "1")),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            self.assertEqual([], errors)
            loaded = env_store.load(path)
            self.assertEqual(loaded["MRRC_WEB_PASSWORD"], "keepme")
            self.assertTrue(loaded["MRRC_WEB_PORT"].startswith("9"))
            self.assertTrue(loaded["MRRC_SCOPE_BAUD"].startswith("1"))


if __name__ == "__main__":
    unittest.main()
