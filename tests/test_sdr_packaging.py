"""打包契约：SDR 的东西必须由服务层装，且 MRRC_SDR=off 时一个都不装。

字符串级断言（服务层在 arm64 chroot 里跑，套件在开发机上跑，不执行它）。
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAYER = os.path.join(ROOT, "packaging", "common", "mrrc-service-layer.sh")
UPDATE = os.path.join(ROOT, "linux", "mrrc_update.sh")
FETCH = os.path.join(ROOT, "tools", "sdr", "fetch_sdrplay_api.sh")


class SdrPackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(LAYER, encoding="utf-8") as f:
            cls.layer = f.read()
        with open(UPDATE, encoding="utf-8") as f:
            cls.update = f.read()
        with open(FETCH, encoding="utf-8") as f:
            cls.fetch = f.read()

    def test_service_layer_installs_the_five_artifacts(self):
        for needle in ("libsdr_dsp.so", "libsdrplay_api.so.3.15",
                       "/opt/sdrplay_api", "66-sdrplay.rules", "mrrc-sdrplay.conf",
                       "sdrplay.service"):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.layer)

    def test_bl_kernel_driver_is_blacklisted(self):
        self.assertIn("blacklist msi2500", self.layer)
        self.assertIn("blacklist msi001", self.layer)

    def test_off_switch_skips_everything(self):
        self.assertIn('if [ "${MRRC_SDR:-on}" = "off" ]', self.layer)
        off_block = self.layer.split('if [ "${MRRC_SDR:-on}" = "off" ]', 1)[1]
        head = off_block.split("else", 1)[0]
        self.assertIn("not installed", head)
        for needle in ("libsdr_dsp.so", "sdrplay.service", "66-sdrplay.rules"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, head)

    def test_vendor_api_is_vendored_not_downloaded_at_build_time(self):
        self.assertIn("vendor/sdrplay", self.layer)
        self.assertIn("fetch_sdrplay_api.sh", self.layer)
        self.assertNotIn("curl", self.layer.split("log \"SDR receiver", 1)[1].split("fi\n", 1)[0])

    def test_fetch_script_pins_a_hash(self):
        self.assertIn("SHA256=", self.fetch)
        self.assertIn("--check", self.fetch)

    def test_update_rebuilds_the_libs_without_touching_the_vendor_stack(self):
        self.assertIn("libsdr_dsp.so", self.update)
        self.assertIn("libsdr_api_shim.so", self.update)
        for forbidden in ("66-sdrplay.rules", "systemctl enable sdrplay"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.update)


if __name__ == "__main__":
    unittest.main()
