"""The local control socket (AD-028).

A physical remote has no web password, so it does not go through the web auth
path at all: the daemon runs as the same user as the server and talks over a
0600 Unix socket. What must hold, and is tested here:

* a line the socket accepts reaches the same ``_execute_set_command`` the
  browser uses (one command path, not two);
* only whitelisted fields are accepted — the socket is not a generic "run
  anything" door;
* the first line tells the daemon where the radio already is;
* a refused command answers instead of dropping the connection.
"""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

import server as srv

#: 服务端的本地控制 socket 走 AF_UNIX（`/run/mrrc/localctl.sock`），只存在于 POSIX。
_POSIX = os.name == "posix"


@unittest.skipUnless(_POSIX, "本地控制 socket 是 AF_UNIX，Windows 上没有")
class LocalControlSocketTests(unittest.TestCase):
    def _roundtrip(self, lines):
        """Open the real handler on a temp socket, send lines, return replies."""
        async def run():
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            path = str(Path(tmp.name) / "ctl.sock")
            calls = []

            async def fake_exec(field, value, ws):
                calls.append((field, value))
                if field == "ptt":
                    await ws.send_text(json.dumps({"type": "error",
                                                   "message": "Radio not connected"}))

            orig = srv._execute_set_command
            srv._execute_set_command = fake_exec
            try:
                server = await asyncio.start_unix_server(srv._local_control_handle, path=path)
                reader, writer = await asyncio.open_unix_connection(path)
                state = await reader.readline()
                replies = []
                for line in lines:
                    writer.write(line.encode() + b"\n")
                    await writer.drain()
                    replies.append(await reader.readline())
                writer.close()
                server.close()
                await server.wait_closed()
            finally:
                srv._execute_set_command = orig
            return json.loads(state.decode()), replies, calls

        return asyncio.run(run())

    def test_state_line_reports_where_the_radio_is(self):
        state, _replies, _calls = self._roundtrip([])
        self.assertEqual(state.get("type"), "state")
        self.assertIn("freq", state)

    def test_state_line_reports_the_af_gain(self):
        # Without it the daemon starts from its own 50 % default and the first
        # volume press steps *down* from whatever the radio was at.
        state, _replies, _calls = self._roundtrip([])
        self.assertEqual(state.get("af_gain"), srv.radio.af_gain)
        self.assertTrue(0 <= state["af_gain"] <= 255)

    def test_state_line_carries_the_band_and_channel_tables(self):
        """遥控器的 TV / 频道键靠这两张表，而它们必须来自**这台电台**。

        TV 键不能写死一张"业余波段"表：FT-891 只有 HF+6 m，IC-705 还有 VHF/UHF。
        频道表同理来自 `mem_channels.json`（用户自己存的 6 个），不是预设表。
        """
        state, _replies, _calls = self._roundtrip([])
        self.assertIn("bands", state)
        self.assertIn("channels", state)
        for band in state["bands"]:
            self.assertIn("name", band)
            self.assertIsInstance(band["freq"], int)
        for channel in state["channels"]:
            self.assertIsInstance(channel["index"], int)
            self.assertIsInstance(channel["freq"], int)

    def test_the_remote_only_fields_are_whitelisted(self):
        """遥控器多要四个字段，全部走现成的 set 执行器 —— 权限面没有变大。"""
        for field in ("band", "cq", "mem", "qsy"):
            self.assertIn(field, srv.LOCAL_CONTROL_FIELDS)

    def test_allowed_field_reaches_one_command_path(self):
        _state, replies, calls = self._roundtrip(['{"field":"freq","value":7051000}'])
        self.assertEqual(calls, [("freq", 7051000)])
        self.assertTrue(json.loads(replies[0].decode())["ok"])

    def test_reply_carries_the_gain_back(self):
        # So a knob turned on the radio itself does not make the daemon's next
        # volume press jump from a stale percentage.
        _state, replies, _calls = self._roundtrip(['{"field":"freq","value":7051000}'])
        self.assertEqual(json.loads(replies[0].decode()).get("af_gain"), srv.radio.af_gain)

    def test_unknown_field_is_refused_without_reaching_the_radio(self):
        _state, replies, calls = self._roundtrip(['{"field":"reboot","value":1}'])
        self.assertEqual(calls, [])
        self.assertFalse(json.loads(replies[0].decode())["ok"])

    def test_bad_json_is_refused_and_the_link_survives(self):
        _state, replies, calls = self._roundtrip(["not json", '{"field":"mode","value":"CW"}'])
        self.assertFalse(json.loads(replies[0].decode())["ok"])
        self.assertEqual(calls, [("mode", "CW")])

    def test_command_error_is_reported_as_not_ok(self):
        _state, replies, _calls = self._roundtrip(['{"field":"ptt","value":true}'])
        payload = json.loads(replies[0].decode())
        self.assertFalse(payload["ok"])
        self.assertIn("not connected", payload["error"])

    def test_field_whitelist_covers_what_the_remote_sends(self):
        for field in ("freq", "mode", "ptt", "af_gain", "vfo_a_freq", "vfo_b_freq"):
            self.assertIn(field, srv.LOCAL_CONTROL_FIELDS)
        self.assertNotIn("power", srv.LOCAL_CONTROL_FIELDS)


if __name__ == "__main__":
    unittest.main()


class ChannelTableRefreshTests(unittest.TestCase):
    """保存频道后必须**推**给已经在线的遥控器守护进程。

    现场（2026-11-17）：「储存的频率频道现在遥控器为啥不能轮转覆盖 6 个了」。
    守护进程只保持一条长连接，而频道表只在**连接时**那一行里下发 —— 网页上刚
    存的第 6 个频道，要到那台盒子重连才轮得到。
    """

    def _push_roundtrip(self):
        async def run():
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            path = str(Path(tmp.name) / "ctl.sock")
            saved = srv._load_mem_channels()
            server = await asyncio.start_unix_server(srv._local_control_handle, path=path)
            reader, writer = await asyncio.open_unix_connection(path)
            try:
                first = json.loads((await reader.readline()).decode())
                six = [{"freq": 7_050_000 + i, "mode": "LSB", "label": f"CH{i}"}
                       for i in range(6)]
                await asyncio.to_thread(srv._save_mem_channels, six)
                await srv._local_control_push_state()
                second = json.loads(
                    await asyncio.wait_for(reader.readline(), timeout=5))
            finally:
                writer.close()
                server.close()
                await server.wait_closed()
                await asyncio.sleep(0.05)          # 让 handler 的 finally 跑完
                writers = len(srv._local_control_writers)
                srv._save_mem_channels(saved)
            return first, second, writers

        return asyncio.run(run())

    def test_a_saved_channel_reaches_a_connected_remote(self):
        first, second, _writers = self._push_roundtrip()
        self.assertIsInstance(first.get("channels"), list)
        self.assertEqual(second.get("type"), "state")
        self.assertEqual([c["index"] for c in second["channels"]], [0, 1, 2, 3, 4, 5])

    def test_the_writer_set_is_cleaned_up_on_disconnect(self):
        _first, _second, writers = self._push_roundtrip()
        self.assertEqual(writers, 0)
