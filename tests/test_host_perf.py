"""Tests for `host_perf` — literal /proc text in, rates out.

No hardware, no sleeps: every reader is injected, so a laptop with no Pi and no
thermal zones runs exactly the same code the box does.
"""

import unittest

import host_perf


STAT_TWO_SAMPLES = {
    # 4 cores; by the second sample core 0 burned 40 of 100 jiffies, core 1 100%.
    "first": """cpu  1000 10 500 8000 40 0 20 0 0 0
cpu0 250 2 125 1990 10 0 5 0 0 0
cpu1 250 3 125 2000 10 0 5 0 0 0
cpu2 250 2 125 2005 10 0 5 0 0 0
cpu3 250 3 125 2005 10 0 5 0 0 0
intr 12345
""",
    "second": """cpu  1040 10 520 8100 40 0 20 0 0 0
cpu0 260 2 130 2030 10 0 5 0 0 0
cpu1 300 3 150 2040 10 0 5 0 0 0
cpu2 250 2 125 2015 10 0 5 0 0 0
cpu3 250 3 125 2015 10 0 5 0 0 0
intr 12400
""",
}


def reader(files):
    def read(path):
        if path not in files:
            raise OSError(path)
        return files[path]
    return read


def lister(dirs):
    def listdir(path):
        if path not in dirs:
            raise OSError(path)
        return dirs[path]
    return listdir


class CpuRateTests(unittest.TestCase):
    def test_busy_share_comes_from_the_delta_not_the_total(self):
        """A box that has been busy for a week must not read as 100 % now."""
        samples = [STAT_TWO_SAMPLES["first"], STAT_TWO_SAMPLES["second"]]
        ticks = iter([0.0, 2.0])
        calls = {"n": 0}

        def read(path):
            if path == "/proc/stat":
                value = samples[min(calls["n"], 1)]
                calls["n"] += 1
                return value
            raise OSError(path)

        sampler = host_perf.Sampler(
            clock=lambda: next(ticks), read=read,
            listdir=lister({}),
            read_sample_fn=lambda: {
                "cpu": host_perf.read_cpu(read),
                "mem": {}, "load": {"load1": 1.0, "load5": 1.0, "load15": 1.0,
                                    "running": 1, "threads": 200},
                "disk": {}, "net": {}, "thermal": {}, "procs": {},
            })
        first = sampler.tick()
        self.assertFalse(first["available"])          # 一个样本做不出速率
        second = sampler.tick()
        self.assertTrue(second["available"])
        # 第一轮 100 jiffies 里忙 20（user 40 + system 20 的新增是 60? 见下），第二轮
        # 明确：cpu 行 +40 user +20 system，+100 idle → 忙 60/160 = 37.5 %
        self.assertAlmostEqual(second["cpu"]["busy_pct"], 37.5, places=1)
        self.assertEqual(len(second["cpu"]["cores"]), 4)
        # cpu0: 忙 15（user 10 + system 5）/ 总 55 = 27.3 %
        self.assertAlmostEqual(second["cpu"]["cores"][0], 27.3, places=1)
        # cpu1: 忙 75（user 50 + system 25）/ 总 115 = 65.2 %
        self.assertAlmostEqual(second["cpu"]["cores"][1], 65.2, places=1)

    def test_iowait_is_reported_separately(self):
        """「CPU 很忙」与「CPU 在等磁盘」是两种病，得能分开看。"""
        entry = {"user": 10, "nice": 0, "system": 10, "idle": 100,
                 "iowait": 60, "irq": 0, "softirq": 0, "steal": 0}
        busy, total = host_perf._cpu_busy(entry)
        self.assertEqual((busy, total), (20, 180))


class MemTests(unittest.TestCase):
    def test_available_wins_over_free(self):
        """`MemFree` 在 Linux 上永远很小；用它算「用了多少」会天天报警。"""
        text = ("MemTotal:        2000000 kB\n"
                "MemFree:           50000 kB\n"
                "MemAvailable:    1500000 kB\n"
                "Buffers:           10000 kB\n"
                "Cached:           400000 kB\n"
                "SwapTotal:             0 kB\n"
                "SwapFree:              0 kB\n")
        mem = host_perf.read_mem(reader({"/proc/meminfo": text}))
        self.assertEqual(mem["total_kb"], 2000000)
        self.assertEqual(mem["available_kb"], 1500000)


class DiskTests(unittest.TestCase):
    DISKSTATS = (
        # major minor name reads rd_merged rd_sectors ... writes ...
        " 179       0 mmcblk2 100 0 4000 200 50 0 2000 100 0 300 400 0 0 0 0 0 0\n"
        " 179       1 mmcblk2p1 40 0 1000 50 10 0 500 20 0 40 60 0 0 0 0 0 0\n"
        " 179       2 mmcblk2p2 60 0 3000 150 40 0 1500 80 0 260 340 0 0 0 0 0 0\n"
    )

    def test_partitions_are_not_counted_twice(self):
        """`mmcblk2p1` 的扇区数已经算在 `mmcblk2` 里；再加一遍就是假的高负载。"""
        disk = host_perf.read_disk(reader({"/proc/diskstats": self.DISKSTATS}))
        self.assertEqual(disk["devices"], ["mmcblk2"])
        self.assertEqual(disk["sectors_read"], 4000)
        self.assertEqual(disk["sectors_written"], 2000)
        self.assertEqual(disk["busy_ms"], 300)

    def test_dm_and_md_are_whole_devices(self):
        text = ("   8       0 sda 1 0 100 0 1 0 100 0 0 10 10 0 0 0 0 0 0\n"
                " 253       0 dm-0 1 0 100 0 1 0 100 0 0 10 10 0 0 0 0 0 0\n")
        disk = host_perf.read_disk(reader({"/proc/diskstats": text}))
        self.assertEqual(sorted(disk["devices"]), ["dm-0", "sda"])


class NetTests(unittest.TestCase):
    NETDEV = (
        "Inter-|   Receive                                                |  Transmit\n"
        " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets\n"
        "    lo: 9999999  1000 0 0 0 0 0 0 9999999 1000 0 0 0 0 0 0\n"
        "  end0:  500000   700 0 0 0 0 0 0  250000   400 0 0 0 0 0 0\n"
    )

    def test_loopback_is_hidden(self):
        """`lo` 是服务自己的频谱与音频，算进「网络使用率」只会误导。"""
        net = host_perf.read_net(reader({"/proc/net/dev": self.NETDEV}))
        self.assertNotIn("lo", net)
        self.assertEqual(net["end0"], {"rx": 500000, "tx": 250000})

    def test_rates_are_per_second(self):
        prev = {"end0": {"rx": 1000, "tx": 0}}
        cur = {"end0": {"rx": 3000, "tx": 1000}}
        sampler = host_perf.Sampler()
        rates = sampler._rates({"net": prev}, {"net": cur}, 2.0)
        self.assertEqual(rates["net"][0]["rx_bps"], 1000)
        self.assertEqual(rates["net"][0]["tx_bps"], 500)


class ThermalTests(unittest.TestCase):
    def test_max_zone_and_average_clock(self):
        files = {
            "/sys/class/thermal/thermal_zone0/temp": "45000\n",
            "/sys/class/thermal/thermal_zone1/temp": "61000\n",
            "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq": "1800000\n",
            "/sys/devices/system/cpu/cpu1/cpufreq/scaling_cur_freq": "1200000\n",
        }
        dirs = {"/sys/class/thermal": ["thermal_zone0", "thermal_zone1", "cooling_device0"],
                "/sys/devices/system/cpu": ["cpu0", "cpu1", "cpuidle"]}
        thermal = host_perf.read_thermal(reader(files), lister(dirs))
        self.assertEqual(thermal["max_c"], 61.0)
        self.assertEqual(thermal["freq_mhz"], 1500.0)
        self.assertEqual(thermal["cores_online"], 2)

    def test_a_missing_zone_is_absent_not_zero(self):
        """0 °C 在这台盒子上等于「很凉快，继续跑」——比没有更危险。"""
        thermal = host_perf.read_thermal(reader({}), lister({}))
        self.assertNotIn("max_c", thermal)


class ProcessTests(unittest.TestCase):
    def test_busiest_processes_are_ranked_by_delta(self):
        prev = {"100": {"ticks": 1000, "name": "server.py"},
                "200": {"ticks": 500, "name": "scope_pipe"}}
        cur = {"100": {"ticks": 1100, "name": "server.py"},
               "200": {"ticks": 590, "name": "scope_pipe"}}
        sampler = host_perf.Sampler()
        rates = sampler._rates({"procs": prev}, {"procs": cur}, 2.0)
        self.assertEqual([p["name"] for p in rates["processes"]],
                         ["server.py", "scope_pipe"])
        # 100 ticks / (100 ticks/s × 2 s) = 50 %
        self.assertAlmostEqual(rates["processes"][0]["cpu_pct"], 50.0, places=1)
        self.assertAlmostEqual(rates["processes"][1]["cpu_pct"], 45.0, places=1)

    def test_a_process_name_with_spaces_still_parses(self):
        """comm 里可以有空格与括号，/proc/<pid>/stat 的字段得从最后一个 `)` 之后切。"""
        stat = "1234 (my (odd) name) S 1 1 1 0 -1 4194560 100 0 0 0 55 45 0 0 20 0 1 0\n"
        files = {"/proc/4242/stat": stat, "/proc/4242/comm": ""}
        procs = host_perf.read_procs(reader(files), lister({"/proc": ["4242", "self"]}))
        self.assertEqual(procs["4242"]["ticks"], 100)
        self.assertEqual(procs["4242"]["name"], "my (odd) name")


class ProcessNameTests(unittest.TestCase):
    def test_a_python_entry_point_is_named_by_its_script(self):
        """comm 全是 "python"；不读 cmdline 就分不清 server.py 与 scope_pipe。"""
        self.assertEqual(
            host_perf._compact_argv(["/opt/mrrc_modern/venv/bin/python",
                                     "server.py"]), "server.py")
        self.assertEqual(
            host_perf._compact_argv(["/opt/mrrc_modern/venv/bin/python", "-m",
                                     "backends.ft710.scope_pipe"]), "scope_pipe")

    def test_inline_code_is_not_mistaken_for_a_name(self):
        """`python3 -c "..."` 的下一段是代码；截 24 字符会把半句 Python 贴在表里。"""
        self.assertEqual(
            host_perf._compact_argv(["/usr/bin/python3", "-c", "import sys; ..."]),
            "python3")

    def test_a_plain_binary_keeps_its_basename(self):
        self.assertEqual(host_perf._compact_argv(["/opt/mrrc_modern/fleet/frpc",
                                                  "-c", "/x.toml"]), "frpc")

    def test_cmdline_wins_over_comm(self):
        files = {"/proc/77/cmdline": "/x/venv/bin/python\x00server.py\x00",
                 "/proc/77/comm": "python\n",
                 "/proc/77/stat": "77 (python) S 1 1 1 0 -1 0 0 0 0 0 5 5 0 0 20 0 1 0\n"}
        procs = host_perf.read_procs(reader(files), lister({"/proc": ["77"]}))
        self.assertEqual(procs["77"]["name"], "server.py")

    def test_tunnel_interfaces_are_not_network_usage(self):
        """`sit0`/`ip6tnl0` 这类隧道不是「网络忙」的证据。"""
        text = ("  sit0: 100 1 0 0 0 0 0 0 100 1 0 0 0 0 0 0\n"
                "  eth0: 200 2 0 0 0 0 0 0 300 3 0 0 0 0 0 0\n")
        net = host_perf.read_net(reader({"/proc/net/dev": text}))
        self.assertEqual(list(net), ["eth0"])


class VerdictTests(unittest.TestCase):
    def snap(self, **over):
        base = {"available": True, "cpus": 4, "load_per_core": 0.3,
                "cpu": {"busy_pct": 12.0, "iowait_pct": 0.5},
                "disk": {"busy_pct": 5.0}, "mem": {"used_pct": 40.0}}
        base.update(over)
        return base

    def test_a_healthy_box_says_so(self):
        self.assertEqual(host_perf.verdict(self.snap())["level"], "ok")

    def test_a_blocked_event_loop_outranks_cpu(self):
        """CPU 不高但循环卡住 = 服务端自己堵了，这时候去查 CPU 是白费时间。"""
        result = host_perf.verdict(self.snap(), {"event_loop_lag_ms": 900})
        self.assertEqual(result["level"], "bad")
        self.assertIn("事件循环", result["text"])

    def test_high_cpu_with_iowait_names_io_as_the_bottleneck(self):
        result = host_perf.verdict(self.snap(cpu={"busy_pct": 95.0, "iowait_pct": 70.0}))
        self.assertEqual(result["level"], "bad")
        self.assertIn("IO", result["text"])

    def test_before_the_second_sample_it_refuses_to_judge(self):
        result = host_perf.verdict({"available": False})
        self.assertEqual(result["level"], "unknown")
        self.assertIn("2 秒", result["text"])

    def test_high_load_on_many_cores_is_not_a_bottleneck(self):
        """4 核上 load 3.0 还有余量；只有每核都排上队才算。"""
        self.assertEqual(host_perf.verdict(self.snap(load_per_core=0.75))["level"], "ok")
        self.assertEqual(host_perf.verdict(self.snap(load_per_core=1.8))["level"], "warn")


class HumanBytesTests(unittest.TestCase):
    def test_units(self):
        self.assertEqual(host_perf.human_bytes(0), "0 B")
        self.assertEqual(host_perf.human_bytes(812), "812 B")
        self.assertEqual(host_perf.human_bytes(42100), "42.1 kB")
        self.assertEqual(host_perf.human_bytes(3400000), "3.4 MB")
        self.assertEqual(host_perf.human_bytes(None), "—")


if __name__ == "__main__":
    unittest.main()
