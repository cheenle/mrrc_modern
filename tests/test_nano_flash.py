"""固件升级的状态机、资产挑选与校验（spec §4.10）。无设备、无 dfu-util、无网络。"""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nano import flash


def _release(tag="1.2.50", names=("NanoVNA-H4-MS_20260516.bin",
                                  "NanoVNA-H4-SI_20260516.bin",
                                  "NanoVNA-H-MS_20260516.bin")):
    items = []
    for name in names:
        items.append({"name": name, "size": 92456, "digest": "sha256:" + ("a" * 64),
                      "browser_download_url": "https://example.invalid/" + name})
    return {"tag_name": tag, "published_at": "2026-05-16T05:57:50Z", "assets": items}


class ParseTest(unittest.TestCase):
    def test_releases_and_digests(self):
        got = flash.parse_releases([_release()])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].tag, "1.2.50")
        self.assertEqual(got[0].assets[0].sha256, "a" * 64)

    def test_missing_digest_is_kept_empty(self):
        payload = [_release()]
        payload[0]["assets"][0]["digest"] = None
        got = flash.parse_releases(payload)
        self.assertEqual(got[0].assets[0].sha256, "")

    def test_garbage_is_ignored(self):
        self.assertEqual(flash.parse_releases("nope"), [])
        self.assertEqual(flash.parse_releases({}), [])


class AssetPickTest(unittest.TestCase):
    def setUp(self):
        self.rel = flash.parse_releases([_release()])[0]

    def test_each_variant_maps_to_its_asset(self):
        self.assertEqual(flash.pick_asset(self.rel, "h4-ms").name, "NanoVNA-H4-MS_20260516.bin")
        self.assertEqual(flash.pick_asset(self.rel, "h4-si").name, "NanoVNA-H4-SI_20260516.bin")
        self.assertEqual(flash.pick_asset(self.rel, "h-ms").name, "NanoVNA-H-MS_20260516.bin")

    def test_missing_variant_returns_none(self):
        self.assertIsNone(flash.pick_asset(self.rel, "h4-zk"))

    def test_unknown_variant_is_an_error(self):
        with self.assertRaises(ValueError):
            flash.pick_asset(self.rel, "h9-xx")

    def test_hex_and_dfu_are_not_candidates(self):
        rel = flash.parse_releases([_release(names=("NanoVNA-H4-MS_20260516.hex",
                                                    "NanoVNA-H4-MS_20260516.dfu"))])[0]
        self.assertIsNone(flash.pick_asset(rel, "h4-ms"))


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _asset(self, body=b"firmware-bytes"):
        sha = hashlib.sha256(body).hexdigest()
        return flash.Asset(name="NanoVNA-H4-MS_x.bin", url="https://example.invalid/x.bin",
                           size=len(body), sha256=sha), body

    def test_download_verifies_and_lands(self):
        asset, body = self._asset()
        opener = mock.MagicMock()
        opener.return_value.__enter__.return_value.read.side_effect = [body, b""]
        got = flash.download_asset(asset, self.dir, opener=opener)
        self.assertTrue(got.exists())
        self.assertEqual(flash.sha256_file(got), asset.sha256)

    def test_sha_mismatch_deletes_the_file(self):
        asset, body = self._asset()
        asset.sha256 = "0" * 64
        opener = mock.MagicMock()
        opener.return_value.__enter__.return_value.read.side_effect = [body, b""]
        with self.assertRaises(flash.FlashError):
            flash.download_asset(asset, self.dir, opener=opener)
        self.assertEqual(list(self.dir.glob("*.bin")), [])

    def test_no_digest_is_refused(self):
        asset, _ = self._asset()
        asset.sha256 = ""
        with self.assertRaises(flash.FlashError):
            flash.download_asset(asset, self.dir, opener=mock.MagicMock())

    def test_cached_file_is_reused(self):
        asset, body = self._asset()
        (self.dir / asset.name).write_bytes(body)
        opener = mock.MagicMock()
        got = flash.download_asset(asset, self.dir, opener=opener)
        opener.assert_not_called()
        self.assertEqual(got.name, asset.name)


class UsbTest(unittest.TestCase):
    def _tree(self, entries):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name, (vid, pid) in entries.items():
            d = root / name
            d.mkdir()
            (d / "idVendor").write_text(vid + "\n")
            (d / "idProduct").write_text(pid + "\n")
        return root

    def test_finds_the_device(self):
        root = self._tree({"1-1": ("0483", "5740"), "2-1": ("0403", "601c")})
        self.assertTrue(flash.usb_present("0483", "5740", root=root))
        self.assertFalse(flash.usb_present("0483", "df11", root=root))

    def test_missing_sysfs_is_not_a_crash(self):
        self.assertFalse(flash.usb_present("0483", "5740", root=Path("/nonexistent/xyz")))

    def test_wait_returns_true_when_state_reached(self):
        root = self._tree({"1-1": ("0483", "df11")})
        self.assertTrue(flash.wait_for_usb("0483", "df11", probe=lambda:
                                           flash.usb_present("0483", "df11", root=root)))

    def test_wait_times_out(self):
        self.assertFalse(flash.wait_for_usb("0483", "df11", timeout=0.01,
                                            probe=lambda: False, sleep=lambda s: None))


class DfuArgsTest(unittest.TestCase):
    def test_exact_argv(self):
        args = flash.dfu_args(Path("/tmp/fw.bin"))
        self.assertEqual(args[:6], ["dfu-util", "-a", "0", "-s", "0x08000000:leave", "-D"])
        self.assertEqual(args[6], "/tmp/fw.bin")

    def test_missing_binary_is_reported(self):
        def runner(_args):
            raise FileNotFoundError("dfu-util")
        with self.assertRaises(flash.FlashError):
            flash.run_dfu(Path("/tmp/fw.bin"), runner=runner)

    def test_success_and_failure(self):
        ok = mock.Mock(returncode=0, stdout="File downloaded successfully", stderr="")
        self.assertEqual(flash.run_dfu(Path("/tmp/fw.bin"), runner=lambda a: ok)[0], 0)
        bad = mock.Mock(returncode=74, stdout="", stderr="No DFU capable USB device")
        rc, out = flash.run_dfu(Path("/tmp/fw.bin"), runner=lambda a: bad)
        self.assertEqual(rc, 74)
        self.assertIn("No DFU", out)


class Hooks:
    def __init__(self, after="1.2.50", dfu_rc=0, enters=True, comes_back=True,
                 self_check=None):
        self.after = after
        self.dfu_rc = dfu_rc
        self.enters = enters
        self.comes_back = comes_back
        self.self_check_result = self_check if self_check is not None else {"ok": True}
        self.calls = []
        self.versions = iter(["1.2.14", after])

    def read_version(self):
        self.calls.append("read_version")
        try:
            return next(self.versions)
        except StopIteration:
            return self.after

    def hooks(self):
        return flash.FlashHooks(
            enter_dfu=lambda: self.calls.append("enter_dfu"),
            wait_dfu=lambda: self.enters,
            run_dfu=lambda image: (self.calls.append("run_dfu"), (self.dfu_rc, "ok"))[1],
            wait_app=lambda: self.comes_back,
            read_version=self.read_version,
            self_check=lambda: self.self_check_result,
        )


class StateMachineTest(unittest.TestCase):
    def _job(self, hooks, expected="1.2.50"):
        return flash.FlashJob(image=Path("/tmp/fw.bin"), expected_version=expected,
                              hooks=hooks.hooks())

    def test_happy_path(self):
        h = Hooks()
        snap = self._job(h).run()
        self.assertEqual(snap["state"], "done")
        self.assertEqual(snap["before_version"], "1.2.14")
        self.assertEqual(snap["after_version"], "1.2.50")
        self.assertEqual(h.calls[:2], ["read_version", "enter_dfu"])

    def test_device_never_enters_dfu(self):
        snap = self._job(Hooks(enters=False)).run()
        self.assertEqual(snap["state"], "failed")
        self.assertIn("DFU", snap["detail"])

    def test_dfu_failure_stops_before_verifying(self):
        snap = self._job(Hooks(dfu_rc=74)).run()
        self.assertEqual(snap["state"], "failed")
        self.assertIn("74", snap["detail"])
        self.assertEqual(snap["after_version"], "")

    def test_device_does_not_come_back(self):
        snap = self._job(Hooks(comes_back=False)).run()
        self.assertEqual(snap["state"], "failed")
        self.assertIn("应用模式", snap["detail"])

    def test_wrong_version_is_a_failure_not_a_success(self):
        snap = self._job(Hooks(after="1.2.44")).run()
        self.assertEqual(snap["state"], "failed")
        self.assertIn("1.2.44", snap["detail"])

    def test_variant_mismatch_is_reported_as_its_own_state(self):
        snap = self._job(Hooks(self_check={"ok": False, "variant_suspect": True})).run()
        self.assertEqual(snap["state"], "variant_mismatch")

    def test_self_check_crash_does_not_fail_the_upgrade(self):
        def boom():
            raise RuntimeError("port busy")
        h = Hooks()
        h.self_check_result = boom
        job = flash.FlashJob(image=Path("/tmp/fw.bin"), expected_version="1.2.50",
                             hooks=flash.FlashHooks(
                                 enter_dfu=lambda: None, wait_dfu=lambda: True,
                                 run_dfu=lambda i: (0, "ok"), wait_app=lambda: True,
                                 read_version=h.read_version, self_check=boom))
        snap = job.run()
        self.assertEqual(snap["state"], "done")
        self.assertIn("error", snap["self_check"])

    def test_snapshot_trims_output(self):
        job = flash.FlashJob(image=Path("/tmp/fw.bin"), expected_version="",
                             hooks=Hooks().hooks())
        job.output = "x" * 10000
        self.assertLessEqual(len(job.snapshot()["output"]), 4000)


class RecordTest(unittest.TestCase):
    def test_record_is_written_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "firmware-state.json"
            flash.record(path, {"state": "done", "after_version": "1.2.50"},
                         variant="h4-ms", tag="1.2.50", sha256="a" * 64)
            import json
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["after_version"], "1.2.50")
            self.assertEqual(data["variant"], "h4-ms")
            self.assertIn("finished", data)


if __name__ == "__main__":
    unittest.main()
