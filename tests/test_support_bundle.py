"""Support bundle core (spec 2026-09-17 §4/§6): redaction is the security boundary."""
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import support_bundle as sb


class RedactEnvTextTests(unittest.TestCase):
    def test_password_key_is_dropped_by_omission(self):
        text, dropped, hits = sb.redact_env_text(
            "MRRC_WEB_PASSWORD=hunter2\nMRRC_WEB_PORT=8888\n")
        self.assertNotIn("hunter2", text)
        self.assertNotIn("MRRC_WEB_PASSWORD", text)
        self.assertIn("MRRC_WEB_PORT=8888", text)
        self.assertEqual(dropped, 1)
        self.assertEqual(hits, 0)

    def test_ssl_key_path_is_dropped(self):
        text, dropped, _ = sb.redact_env_text("MRRC_SSL_KEY=/x/privkey.pem\n")
        self.assertNotIn("privkey.pem", text)
        self.assertEqual(dropped, 1)

    def test_allowlisted_diagnostic_keys_survive(self):
        text, dropped, _ = sb.redact_env_text(
            "MRRC_RADIO_MODEL=ft710\nMRRC_SERIAL_PORT=/dev/cu.usbserial-0121DB3A0\n"
            "MRRC_BAUD_RATE=38400\nMRRC_AUDIO_RX_DEVICE=USB Audio\n")
        self.assertEqual(dropped, 0)
        self.assertIn("MRRC_SERIAL_PORT=/dev/cu.usbserial-0121DB3A0", text)
        self.assertIn("MRRC_AUDIO_RX_DEVICE=USB Audio", text)

    def test_comments_and_blank_lines_are_kept(self):
        text, dropped, _ = sb.redact_env_text("# radio\n\nMRRC_WEB_PORT=8888\n")
        self.assertIn("# radio", text)
        self.assertEqual(dropped, 0)

    def test_value_pass_cleans_a_secret_pasted_into_a_comment(self):
        text, _, hits = sb.redact_env_text("# old password=hunter2\n")
        self.assertNotIn("hunter2", text)
        self.assertEqual(hits, 1)


class RedactTextTests(unittest.TestCase):
    def test_replaces_values_and_counts(self):
        cleaned, hits = sb.redact_text(
            'GET /login?token=abc123&x=1\nMRRC_WEB_PASSWORD=x\n')
        self.assertNotIn("abc123", cleaned)
        self.assertEqual(hits, 2)

    def test_plain_text_untouched(self):
        cleaned, hits = sb.redact_text("RX open failed (-9996) — no device\n")
        self.assertEqual(hits, 0)
        self.assertIn("-9996", cleaned)


class CollectableTests(unittest.TestCase):
    def test_user_data_and_keys_are_refused(self):
        for path in ("recordings/15515kHz_20260912_222653.mp3",
                     "certs/server.key", "state/config.pem",
                     "mem_channels.json", "atr1000_tuner.json"):
            self.assertFalse(sb.is_collectable(path), path)

    def test_logs_and_diagnostics_are_allowed(self):
        for path in ("logs/server.log", "logs/server-stdout.log",
                     "logs/launcher.log", "diagnostics/env.json"):
            self.assertTrue(sb.is_collectable(path), path)

    def test_windows_separators_are_normalised(self):
        self.assertFalse(sb.is_collectable(r"C:\Users\x\certs\server.key"))


class TailLinesTests(unittest.TestCase):
    def test_small_file_is_read_whole(self):
        # write_bytes, not write_text: Windows would translate \n to \r\n and the
        # fixture — not the code — would decide the outcome (VM failure 2026-09-17).
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server.log"
            path.write_bytes(b"line1\nline2\n")
            self.assertEqual(sb.tail_lines(str(path)), "line1\nline2\n")

    def test_tail_is_byte_bounded_and_line_aligned(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server.log"
            path.write_text("".join(f"line{i:05d}\n" for i in range(1000)), encoding="utf-8")
            text = sb.tail_lines(str(path), max_bytes=100)
            self.assertLessEqual(len(text.encode()), 100)
            self.assertTrue(text.startswith("line"), text[:20])
            self.assertTrue(text.endswith("\n"))

    def test_missing_file_is_empty_not_an_exception(self):
        self.assertEqual(sb.tail_lines("/nonexistent/server.log"), "")

    def test_invalid_utf8_is_replaced_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server.log"
            path.write_bytes(b"ok\n\xff\xfe bad\n")
            self.assertIn("ok", sb.tail_lines(str(path)))


class ResolveLogFilesTests(unittest.TestCase):
    def test_finds_server_stdout_and_launcher_across_two_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / "logs").mkdir()
            (data / "logs" / "server.log").write_text("s", encoding="utf-8")
            (data / "logs" / "server-stdout.log").write_text("o", encoding="utf-8")
            (data / "logs" / "server.log.1").write_text("p", encoding="utf-8")
            (data / "launcher.log").write_text("l", encoding="utf-8")
            found = sb.resolve_log_files(data / "logs", data)
        self.assertEqual(Path(found["server"]).name, "server.log")
        self.assertEqual(Path(found["server-prev"]).name, "server.log.1")
        self.assertEqual(Path(found["stdout"]).name, "server-stdout.log")
        self.assertEqual(Path(found["launcher"]).name, "launcher.log")

    def test_only_existing_files_are_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(sb.resolve_log_files(Path(tmp) / "logs", Path(tmp)), {})

    def test_source_mode_legacy_name_is_picked_up_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = Path(tmp)
            (install / "logs").mkdir()
            (install / "logs" / "ft710-server.log").write_text("x", encoding="utf-8")
            found = sb.resolve_log_files(install / "logs", "", install)
        self.assertEqual(Path(found["legacy"]).name, "ft710-server.log")
        self.assertEqual(len(set(found.values())), len(found))


class SummarizeLogTests(unittest.TestCase):
    def test_startups_without_tracebacks_are_not_a_crash(self):
        log = ("2026-09-17 07:00:00 [INFO] mrrc: Server ready!\n"
               "2026-09-17 07:05:00 [INFO] mrrc: Server ready!\n")
        out = sb.summarize_log(log, freshness_hours=0.2)
        self.assertIn("启动次数：2 次", out)
        self.assertIn("无崩溃痕迹", out)
        self.assertIn("数据新鲜度：日志写于 12 分钟前", out)

    def test_traceback_changes_the_verdict(self):
        log = ("2026-09-17 07:00:00 [INFO] mrrc: Server ready!\n"
               "Traceback (most recent call last):\n")
        out = sb.summarize_log(log)
        self.assertIn("Traceback", out)
        self.assertIn("按崩溃排查", out)

    def test_stale_bundle_is_flagged_as_not_the_scene(self):
        out = sb.summarize_log("2026-09-14 07:00:00 [INFO] mrrc: Server ready!\n",
                              freshness_hours=72.0)
        self.assertIn("可能不是本次故障现场", out)

    def test_no_logs_at_all_is_stated_not_omitted(self):
        out = sb.summarize_log("")
        self.assertIn("数据新鲜度：无日志文件", out)
        self.assertIn("启动次数：日志中未见", out)

    def test_audio_and_serial_and_scope_facts_are_reported(self):
        log = ("Configured audio device 'X' not found\n"
               "RX open failed (-9996) — re-initializing PortAudio\n"
               "[Errno 6] Device not configured\n"
               "scope_pipe worker not found — spectrum will use S-meter fallback only\n"
               "Recording writer is falling behind — dropping audio (the encoder is slower)\n")
        out = sb.summarize_log(log)
        self.assertIn("音频设备", out)
        self.assertIn("-9996", out)
        self.assertIn("串口掉线", out)
        self.assertIn("S 表合成", out)
        self.assertIn("录音写入", out)

    def test_unverified_tx_gate_is_reported(self):
        out = sb.summarize_log("Transmit disabled: set MRRC_ALLOW_UNVERIFIED_TX=1 and restart "
                              "to enable TX after checking\n")
        self.assertIn("TX 门禁", out)

    def test_each_class_shows_at_most_three_recent_lines(self):
        log = "".join(f"Configured audio device 'D{i}' not found\n" for i in range(9))
        out = sb.summarize_log(log)
        self.assertIn("音频设备：9 条", out)
        self.assertEqual(out.count("      - Configured audio device"), 3)
        self.assertIn("D8", out)                   # newest kept


class VersionTests(unittest.TestCase):
    def test_version_txt_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "version.txt").write_text("1.17.0\n", encoding="utf-8")
            self.assertEqual(sb.detect_version(Path(tmp)), "1.17.0")

    def test_falls_back_to_changelog_then_iss(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "CHANGELOG.md").write_text("# Changelog\n\n## [v1.17.0] — 2026-09-13\n",
                                               encoding="utf-8")
            self.assertEqual(sb.detect_version(root), "1.17.0")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "packaging" / "windows").mkdir(parents=True)
            (root / "packaging" / "windows" / "MRRC-Modern.iss").write_text(
                '#define MyAppVersion "1.16.0"\n', encoding="utf-8")
            self.assertEqual(sb.detect_version(root), "1.16.0")

    def test_legacy_rpi_version_file_is_accepted(self):
        """The rpi64 image wrote /opt/mrrc_modern/VERSION before 2026-09-17."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "VERSION").write_text("1.17.0\n", encoding="utf-8")
            self.assertEqual(sb.detect_version(Path(tmp)), "1.17.0")

    def test_version_txt_wins_over_the_legacy_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "VERSION").write_text("1.16.0\n", encoding="utf-8")
            (Path(tmp) / "version.txt").write_text("1.17.0\n", encoding="utf-8")
            self.assertEqual(sb.detect_version(Path(tmp)), "1.17.0")

    def test_unknown_when_nothing_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(sb.detect_version(Path(tmp)), "unknown")


class EnvSnapshotTests(unittest.TestCase):
    def test_carries_version_platform_and_caller_extras(self):
        snap = sb.collect_env_snapshot("1.17.0", extra={"backend": "ft710"})
        self.assertEqual(snap["version"], "1.17.0")
        self.assertEqual(snap["backend"], "ft710")
        self.assertIn("platform", snap)
        self.assertIn("python", snap)
        self.assertIn("frozen", snap)


class BuildBundleTests(unittest.TestCase):
    def _build(self, tmp, **kwargs):
        logs = Path(tmp) / "logs"
        logs.mkdir(exist_ok=True)
        (logs / "server.log").write_text(
            "2026-09-17 07:00:00 [INFO] mrrc: Server ready!\nMRRC_WEB_PASSWORD=hunter2\n",
            encoding="utf-8")
        return sb.build_bundle(
            Path(tmp) / "out",
            problem="接收有杂音",
            contact="BH1XXX",
            env={"version": "1.17.0"},
            log_files={"server": str(logs / "server.log")},
            config_text="MRRC_WEB_PASSWORD=hunter2\nMRRC_WEB_PORT=8888\n",
        )

    def test_bundle_contains_the_expected_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._build(tmp)
            with zipfile.ZipFile(result["path"]) as archive:
                names = set(archive.namelist())
        for expected in ("problem.txt", "README.txt", "manifest.json", "logs/server.log",
                         "state/config-redacted.env", "diagnostics/summary.txt",
                         "diagnostics/env.json"):
            self.assertIn(expected, names)

    def test_no_secret_anywhere_in_the_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._build(tmp)
            with zipfile.ZipFile(result["path"]) as archive:
                blob = b"".join(archive.read(n) for n in archive.namelist())
        self.assertNotIn(b"hunter2", blob)
        self.assertGreater(result["redactions"], 0)

    def test_redaction_count_covers_config_and_logs(self):
        """The page promises 已脱敏 N 处 — N must include the log value pass."""
        with tempfile.TemporaryDirectory() as tmp:
            result = self._build(tmp)
        # 1 dropped config key (MRRC_WEB_PASSWORD) + 1 log line value replacement
        self.assertEqual(result["redactions"], 2)

    def test_manifest_hashes_every_collected_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._build(tmp)
            with zipfile.ZipFile(result["path"]) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                for name, digest in manifest["sha256"].items():
                    self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(), digest)

    def test_never_collects_forbidden_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = sb.build_bundle(
                Path(tmp) / "out", log_files={"recordings": str(Path(tmp) / "qso.mp3")},
                extra_files={"certs/server.key": b"PRIVATE"})
            with zipfile.ZipFile(result["path"]) as archive:
                names = " ".join(archive.namelist())
        self.assertNotIn("qso.mp3", names)
        self.assertNotIn("server.key", names)
        self.assertTrue(any("受限" in w for w in result["warnings"]), result["warnings"])

    def test_degradation_shrinks_the_tail_to_the_smallest_rung(self):
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "logs" / "server.log"
            big.parent.mkdir(parents=True)
            big.write_text("".join(f"line{i:06d}\n" for i in range(30000)), encoding="utf-8")
            self.assertGreater(big.stat().st_size, 200 * 1024)   # bigger than any rung
            result = sb.build_bundle(Path(tmp) / "out",
                                     log_files={"server": str(big)},
                                     max_total_bytes=1024)
            with zipfile.ZipFile(result["path"]) as archive:
                collected = archive.read("logs/server.log")
        self.assertLess(len(collected), 140 * 1024)               # last rung is 128 KB
        self.assertIn("已降级", " ".join(result["warnings"]))

    def test_id_is_validated_before_any_path_is_built(self):
        self.assertRegex(sb.new_bundle_id(), sb.BUNDLE_ID_RE)
        self.assertTrue(sb.is_valid_bundle_id("20260917-072530-ab12"))
        self.assertFalse(sb.is_valid_bundle_id("../../etc/passwd"))
        self.assertFalse(sb.is_valid_bundle_id("20260917-072530-ZZZZ"))

    def test_prune_keeps_the_newest_and_ignores_foreign_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            names = [f"support-20260917-0725{i:02d}-ab12.zip" for i in range(7)]
            for name in names:
                (out / name).write_bytes(b"z")
            (out / "keep-me.txt").write_text("x", encoding="utf-8")
            sb.prune_bundles(out, keep=5)
            remaining = sorted(p.name for p in out.glob("support-*.zip"))
            # inside the block: TemporaryDirectory is gone after it
            self.assertTrue((out / "keep-me.txt").exists())
        self.assertEqual(len(remaining), 5)
        self.assertEqual(remaining[0], names[2])
