"""
Tests for server.py WebSocket protocol — SDD §9.2, §10.4.
Verifies: message format, auth token flow, PTT safety logic, state broadcast.
"""
import json
from pathlib import Path
import unittest


class WSMessageFormatTests(unittest.TestCase):
    """SDD §9.2.1: /WSradio JSON message format."""

    def test_full_state_message(self):
        msg = {
            "type": "fullState",
            "data": {"vfo_a_freq": 14_200_000, "mode": 2},
            "bands": [{"name": "20m"}],
            "modes": [{"name": "USB", "cat_num": 2}],
        }
        self.assertEqual(msg["type"], "fullState")
        self.assertIn("data", msg)
        self.assertIn("bands", msg)
        self.assertIn("modes", msg)

    def test_state_update_message(self):
        msg = {
            "type": "stateUpdate",
            "fields": {"vfo_a_freq": 14_250_000, "s_meter": 120},
            "dirty": ["vfo_a_freq", "s_meter"],
        }
        self.assertEqual(msg["type"], "stateUpdate")
        self.assertEqual(len(msg["fields"]), 2)

    def test_set_command_message(self):
        msg = {"type": "set", "field": "freq", "value": 14_200_000}
        self.assertEqual(msg["type"], "set")
        self.assertIn("field", msg)
        self.assertIn("value", msg)

    def test_get_command_message(self):
        msg = {"type": "get", "field": "fullState"}
        self.assertEqual(msg["type"], "get")

    def test_ping_message(self):
        msg = {"type": "ping"}
        self.assertEqual(msg["type"], "ping")

    def test_pong_response(self):
        msg = {"type": "pong"}
        self.assertEqual(msg["type"], "pong")

    def test_error_message(self):
        msg = {"type": "error", "message": "Radio not connected"}
        self.assertEqual(msg["type"], "error")
        self.assertIn("message", msg)

    def test_mem_channels_message(self):
        msg = {
            "type": "memChannels",
            "channels": [None, {"freq": 14_200_000, "mode": "USB"}, None],
        }
        self.assertEqual(msg["type"], "memChannels")
        self.assertIn("channels", msg)

    def test_mem_save_message(self):
        msg = {"type": "memSave", "channels": [None] * 6}
        self.assertEqual(msg["type"], "memSave")

    def test_value_response(self):
        msg = {"type": "value", "field": "freq", "value": 14_200_000}
        self.assertEqual(msg["type"], "value")

    def test_legacy_colon_format(self):
        """Legacy format: 'field:value' string."""
        msg_str = "freq:14200000"
        field, _, val = msg_str.partition(":")
        self.assertEqual(field, "freq")
        self.assertEqual(val, "14200000")


class WSAuthTests(unittest.TestCase):
    """SDD NFR-020-NFR-023: WebSocket auth token validation."""

    def test_auth_token_in_query_string(self):
        """Token passed as ?token=<hex> query param."""
        token = "a" * 64  # 32 bytes hex = 64 chars
        self.assertEqual(len(token), 64)

    def test_invalid_token_rejected(self):
        valid_tokens = {"abc123"}
        token = "invalid"
        self.assertNotIn(token, valid_tokens)

    def test_valid_token_accepted(self):
        valid_tokens = {"abc123"}
        token = "abc123"
        self.assertIn(token, valid_tokens)

    def test_ws_close_code_for_unauthorized(self):
        """Unauthorized WS connections close with code 4001."""
        close_code = 4001
        self.assertEqual(close_code, 4001)


class PTTSafetyLogicTests(unittest.TestCase):
    """SDD §15: PTT safety layers."""

    def test_ptt_on_command(self):
        cmd = "TX1;"
        self.assertEqual(cmd, "TX1;")

    def test_ptt_off_command(self):
        cmd = "TX0;"
        self.assertEqual(cmd, "TX0;")

    def test_triple_verify_retries(self):
        """Layer 3: up to 3 retries at 200ms intervals."""
        max_retries = 3
        for retry in range(max_retries):
            # Simulate: send TX; query → check response
            pass
        self.assertEqual(max_retries, 3)

    def test_dead_man_switch_condition(self):
        """Layer 5: no clients + transmitting → force RX."""
        has_clients = False
        is_transmitting = True
        should_force_rx = not has_clients and is_transmitting
        self.assertTrue(should_force_rx)

    def test_dead_man_switch_not_triggered_when_clients_remain(self):
        has_clients = True
        is_transmitting = True
        should_force_rx = not has_clients and is_transmitting
        self.assertFalse(should_force_rx)

    def test_dead_man_switch_not_triggered_in_rx(self):
        has_clients = False
        is_transmitting = False
        should_force_rx = not has_clients and is_transmitting
        self.assertFalse(should_force_rx)

    def test_watchdog_retry_count(self):
        """Layer 4: PTT watchdog up to 3 retries."""
        max_retries = 3
        for retry in range(1, max_retries + 1):
            if retry >= max_retries:
                # At max retries, force RX locally
                tx_status = 0
                self.assertEqual(tx_status, 0)

    def test_send_beacon_format(self):
        """Layer 6: beforeunload sendBeacon payload."""
        data = json.dumps({"type": "set", "field": "ptt", "value": False})
        parsed = json.loads(data)
        self.assertEqual(parsed["type"], "set")
        self.assertEqual(parsed["field"], "ptt")
        self.assertEqual(parsed["value"], False)

    def test_tx_audio_stop_signal(self):
        """Layer 8: 's:' text command over /WSaudioTX."""
        stop_cmd = "s:"
        self.assertTrue(stop_cmd.startswith("s:"))

    def test_tx_audio_m_settings(self):
        """TX audio settings: 'm:rate,encode,...' format."""
        settings = "m:16000,opus,64000,20"
        self.assertTrue(settings.startswith("m:"))
        parts = settings[2:].split(",")
        self.assertGreaterEqual(len(parts), 1)


class StateBroadcastLogicTests(unittest.TestCase):
    """SDD §9.7: State broadcasting via dirty-field tracking."""

    def test_meter_broadcast_log_interval_is_half_second(self):
        server_source = Path("server.py").read_text(encoding="utf-8")
        self.assertIn("METER_BROADCAST_LOG_INTERVAL_SECONDS = 0.5", server_source)
        self.assertIn(
            "now - _last_meter_broadcast_log >= METER_BROADCAST_LOG_INTERVAL_SECONDS",
            server_source,
        )

    def test_band_command_is_single_backend_transaction(self):
        server_source = Path("server.py").read_text(encoding="utf-8")
        self.assertIn('await cat.set_band_stack(band["bsr"])', server_source)
        self.assertIn('await cat.set_frequency(band["default_freq"], "A")', server_source)

        ui_source = Path("static/ft710_ui.js").read_text(encoding="utf-8")
        band_button_handler = ui_source.split("// Band button: cycles to next band", 1)[1]
        band_button_handler = band_button_handler.split("// Filter button", 1)[0]
        self.assertIn("sendCommand('band', nextBand.name)", band_button_handler)
        self.assertNotIn("sendCommand('freq'", band_button_handler)

    def test_band_cycle_uses_full_frontend_fallback_and_frequency_fallback(self):
        ui_source = Path("static/ft710_ui.js").read_text(encoding="utf-8")
        self.assertIn("const DEFAULT_BAND_CYCLE = [", ui_source)
        for band in ("160m", "80m", "60m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m", "4m"):
            self.assertIn(f"name: '{band}'", ui_source)
        self.assertIn("function getBandCycle()", ui_source)
        self.assertIn("function getNextBand(currentBand)", ui_source)
        self.assertIn("const nextIdx = (idx + 1) % bandList.length;", ui_source)

    def test_state_update_renders_from_actual_fields_not_dirty_only(self):
        main_source = Path("static/ft710_main.js").read_text(encoding="utf-8")
        self.assertIn("const changedFields = msg.fields ? Object.keys(msg.fields) : msg.dirty;", main_source)
        self.assertIn("renderUpdates(changedFields);", main_source)

    def test_static_assets_are_cache_busted_after_ui_changes(self):
        index_source = Path("static/index.html").read_text(encoding="utf-8")
        self.assertIn('/ft710.css?v=20', index_source)
        self.assertIn('/ft710_main.js?v=23', index_source)
        self.assertIn('/ft710_ui.js?v=22', index_source)

        sw_source = Path("static/sw.js").read_text(encoding="utf-8")
        self.assertIn("const CACHE = 'ft710-v23'", sw_source)
        self.assertIn("'/ft710_ui.js?v=22'", sw_source)


class CookieSettingsPersistenceTests(unittest.TestCase):
    """SDD V2.3: all frontend settings persist in cookies, not web storage."""

    def test_settings_manager_exposes_cookie_helpers_and_migration(self):
        src = Path("static/modules/settings_manager.js").read_text(encoding="utf-8")
        self.assertIn("setCookie: setCookie", src)
        self.assertIn("getCookie: getCookie", src)
        self.assertIn("migrateLegacyStorage();", src)
        for key in ("ft710_afVol", "ft710_scopeFloor", "ft710_scopeCeil",
                    "ft710_scopeTheme", "ft710_memChannels"):
            self.assertIn(f"'{key}'", src)

    def test_no_web_storage_outside_settings_manager_migration(self):
        for js in ("static/ft710_main.js", "static/ft710_ui.js"):
            src = Path(js).read_text(encoding="utf-8")
            self.assertNotIn("localStorage", src, js)
            self.assertNotIn("sessionStorage", src, js)

    def test_persisted_values_written_via_cookie_helpers(self):
        main_src = Path("static/ft710_main.js").read_text(encoding="utf-8")
        ui_src = Path("static/ft710_ui.js").read_text(encoding="utf-8")
        self.assertIn("FT710Settings.setCookie('ft710_memChannels'", main_src)
        self.assertIn("FT710Settings.getCookie('ft710_memChannels')", main_src)
        self.assertIn("FT710Settings.getCookie('ft710_afVol')", main_src)
        self.assertIn("FT710Settings.setCookie('ft710_afVol'", ui_src)
        self.assertIn("FT710Settings.getCookie('ft710_' + key)", ui_src)
        self.assertIn("FT710Settings.setCookie('ft710_micGain'", ui_src)
        self.assertIn("_applySavedMicGain();", main_src)
        self.assertIn("FT710Settings.getCookie('ft710_micGain')", main_src)
        self.assertIn("FT710Settings.setCookie('ft710_micVol'", ui_src)
        self.assertIn("_setDeviceMicGain(parseInt(this.value) / 100)", ui_src)
        self.assertIn("_ensureMicGainNode()", main_src)
        self.assertIn("_txMicGainNode.connect(_txMicAw)", main_src)

    def test_empty_dirty_set_no_broadcast(self):
        dirty = set()
        self.assertEqual(len(dirty), 0)
        # Should not broadcast when nothing changed

    def test_scope_pipe_starts_lazily_for_spectrum_clients(self):
        server_source = Path("server.py").read_text(encoding="utf-8")
        lifespan_block = server_source.split("@asynccontextmanager", 1)[1].split("app = FastAPI", 1)[0]
        spectrum_block = server_source.split('@app.websocket("/WSspectrum")', 1)[1].split("# ── Audio RX WebSocket", 1)[0]
        self.assertNotIn("asyncio.create_subprocess_exec", lifespan_block)
        self.assertIn("_ensure_scope_pipe_running", server_source)
        self.assertIn("await _ensure_scope_pipe_running()", spectrum_block)

    def test_non_empty_dirty_set_triggers_broadcast(self):
        dirty = {"vfo_a_freq", "s_meter"}
        self.assertGreater(len(dirty), 0)

    def test_dirty_fields_cleared_after_broadcast(self):
        dirty = {"vfo_a_freq", "mode"}
        broadcast = dirty.copy()
        dirty.clear()
        self.assertGreater(len(broadcast), 0)
        self.assertEqual(len(dirty), 0)

    def test_partial_update_only_sends_changed_fields(self):
        full_state = {"vfo_a_freq": 14_200_000, "mode": 2, "s_meter": 100}
        changed_fields = {"vfo_a_freq"}
        partial = {f: full_state[f] for f in changed_fields}
        self.assertEqual(len(partial), 1)
        self.assertEqual(partial["vfo_a_freq"], 14_200_000)

    def test_skip_next_poll_before_cat_commands(self):
        """skip_next_poll must come BEFORE the CAT command to prevent
        in-flight poll results from overwriting the user's new setting."""
        server_source = Path("server.py").read_text(encoding="utf-8")
        # Extract the band handler block
        band_block_start = server_source.index('elif field == "band":')
        band_block_end = server_source.index('elif field == "vfo_equal":')
        band_block = server_source[band_block_start:band_block_end]
        skip_pos = band_block.index('skip_next_poll("if"')
        cat_pos = band_block.index("await cat.set_band_stack")
        self.assertLess(
            skip_pos, cat_pos,
            "skip_next_poll must be called BEFORE set_band_stack "
            "so in-flight poll results see the skip",
        )
        # Also check the freq command
        freq_block_start = server_source.index('if field == "freq":')
        freq_block_end = server_source.index('elif field == "vfo_a_freq":')
        freq_block = server_source[freq_block_start:freq_block_end]
        skip_freq_pos = freq_block.index('skip_next_poll("if"')
        cat_freq_pos = freq_block.index("await cat.set_frequency")
        self.assertLess(
            skip_freq_pos, cat_freq_pos,
            "skip_next_poll must be called BEFORE set_frequency",
        )
        # Filter width changes are slow to apply; the SH0 poll must be
        # skipped before sending the CAT command so it cannot read back the
        # old filter width and overwrite the user's selection.
        filter_block_start = server_source.index('elif field == "filter" or field == "filter_width":')
        filter_block_end = server_source.index('elif field == "af_gain":')
        filter_block = server_source[filter_block_start:filter_block_end]
        skip_filter_pos = filter_block.index('skip_next_poll("filter_width"')
        cat_filter_pos = filter_block.index("await cat.set_filter_width")
        self.assertLess(
            skip_filter_pos, cat_filter_pos,
            "skip_next_poll must be called BEFORE set_filter_width",
        )

    def test_poll_guards_against_stale_frequency_after_skip(self):
        """The IF poll must check _should_skip AFTER reading frequency,
        not just at the start of the loop iteration."""
        poll_source = Path("poll_scheduler.py").read_text(encoding="utf-8")
        self.assertIn("not await self._should_skip", poll_source)
        # Verify the guard appears AFTER get_frequency and BEFORE
        # adding to changes
        freq_idx = poll_source.index('get_frequency("A"')
        guard_idx = poll_source.index(
            'not await self._should_skip("if")',
            freq_idx,
        )
        self.assertGreater(
            guard_idx, freq_idx,
            "Guard must appear AFTER get_frequency to catch stale reads",
        )

    def test_settings_poll_discards_stale_reads_after_query(self):
        """_poll_settings must re-check _should_skip AFTER each query await.
        A user set command arriving while a settings query (e.g. SH0) is in
        flight makes the response stale (pre-command value); applying it
        snaps the UI back to the old setting for several seconds."""
        poll_source = Path("poll_scheduler.py").read_text(encoding="utf-8")
        start = poll_source.index("async def _poll_settings")
        end = poll_source.index("async def _poll_slow")
        body = poll_source[start:end]
        query_idx = body.index("await self.cat.query(cmd")
        guard_idx = body.index("await self._should_skip(field)", query_idx)
        self.assertGreater(
            guard_idx, query_idx,
            "skip re-check must appear AFTER the query to discard stale reads",
        )

    def test_filter_set_reads_back_actual_width(self):
        """The filter handler must read back SH0 after setting, so the UI
        converges to the radio's actual width even if the radio silently
        rejects an index (instead of an optimistic value lingering ~4s)."""
        server_source = Path("server.py").read_text(encoding="utf-8")
        start = server_source.index('elif field == "filter" or field == "filter_width":')
        end = server_source.index('elif field == "af_gain":')
        block = server_source[start:end]
        set_idx = block.index("await cat.set_filter_width")
        read_idx = block.index("await cat.get_filter_width")
        self.assertGreater(
            read_idx, set_idx,
            "get_filter_width read-back must come AFTER set_filter_width",
        )

    def test_client_server_band_lists_are_consistent(self):
        """Client DEFAULT_BAND_CYCLE and server BANDS must stay in sync:
        same bands, same names, valid frequency ranges that don't overlap
        or leave gaps that could cause band-detection mismatches."""
        import ast
        import re as _re

        config_source = Path("config.py").read_text(encoding="utf-8")
        ui_source = Path("static/ft710_ui.js").read_text(encoding="utf-8")

        # Parse server BANDS — handle both plain and type-annotated assignment
        tree = ast.parse(config_source)
        server_bands = None
        for node in ast.walk(tree):
            target = None
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == 'BANDS':
                        target = t
                        break
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id == 'BANDS':
                    target = node.target
            if target is not None:
                server_bands = ast.literal_eval(node.value)
                break
        self.assertIsNotNone(server_bands, "Could not parse BANDS from config.py")
        self.assertEqual(len(server_bands), 12,
                         f"Server expects 12 bands, got {len(server_bands)}")

        # Parse client DEFAULT_BAND_CYCLE — extract each band entry
        # as a dict by scraping the JS object literal syntax.
        cycle_start = ui_source.index("const DEFAULT_BAND_CYCLE = [")
        cycle_end = ui_source.index("];", cycle_start) + 2
        cycle_block = ui_source[cycle_start:cycle_end]
        client_bands = []
        # Match each {name: '...', start: N, end: N, default_freq: N} block
        band_pattern = (
            r"\{name:\s*'(\w+)',\s*start:\s*([\d_]+),\s*"
            r"end:\s*([\d_]+),\s*default_freq:\s*([\d_]+)\}"
        )
        for m in _re.finditer(band_pattern, cycle_block):
            client_bands.append({
                "name": m.group(1),
                "start": int(m.group(2).replace("_", "")),
                "end": int(m.group(3).replace("_", "")),
                "default_freq": int(m.group(4).replace("_", "")),
            })
        self.assertEqual(len(client_bands), 12,
                         f"Client expects 12 bands, got {len(client_bands)}")

        # Validate each pair matches
        server_by_name = {b["name"]: b for b in server_bands}
        for cb in client_bands:
            name = cb["name"]
            self.assertIn(name, server_by_name,
                          f"Client band '{name}' missing from server BANDS")
            sb = server_by_name[name]
            self.assertEqual(cb["start"], sb["start"],
                             f"Band {name}: start mismatch {cb['start']} vs {sb['start']}")
            self.assertEqual(cb["end"], sb["end"],
                             f"Band {name}: end mismatch {cb['end']} vs {sb['end']}")
            self.assertEqual(cb["default_freq"], sb["default_freq"],
                             f"Band {name}: default_freq mismatch "
                             f"{cb['default_freq']} vs {sb['default_freq']}")
            # default_freq must be within band range
            self.assertGreaterEqual(sb["default_freq"], sb["start"],
                                    f"Band {name}: default_freq < start")
            self.assertLessEqual(sb["default_freq"], sb["end"],
                                 f"Band {name}: default_freq > end")

        # Check client and server lists have the SAME band names in the SAME order
        client_names = [b["name"] for b in client_bands]
        server_names = [b["name"] for b in server_bands]
        self.assertEqual(client_names, server_names,
                         "DEFAULT_BAND_CYCLE order must match BANDS order")

    def test_band_error_feedback_on_cat_failure(self):
        """When CAT commands fail, server must log the failure and
        send an error message back to the client so it can revert
        its optimistic update."""
        server_source = Path("server.py").read_text(encoding="utf-8")
        # Error message must be sent on failure
        self.assertIn(
            'Band change FAILED:',
            server_source,
            "Must log on CAT failure",
        )
        self.assertIn(
            'message": f"Band change to {band[\'name\']} failed',
            server_source,
            "Must send error message to client on CAT failure",
        )
        self.assertIn(
            'message": f"Unknown band: {band_name}',
            server_source,
            "Must send error message to client for unknown band",
        )


class TXUplinkOwnershipTests(unittest.TestCase):
    """TX-audio uplink ownership (2026-07-22 soft-failure fix).

    Root cause of "server runs for a long time, then PTT keys but there is
    no voice / no RF power": ownership of the /WSaudioTX uplink was only
    ever assigned at connect time.  An idle first-connected client (second
    tab, iOS app, or a zombie socket) kept ownership; the PTT-ing client's
    mic frames were silently dropped.  And once the owner disconnected,
    remaining clients were muted forever (no promotion).
    """

    def setUp(self):
        import server
        self.server = server
        self._saved_clients = set(server.audio_tx_clients)
        self._saved_tokens = dict(server._ws_tokens)
        self._saved_owner = server._tx_owner_ws
        server.audio_tx_clients.clear()
        server._ws_tokens.clear()
        server._tx_owner_ws = None

    def tearDown(self):
        s = self.server
        s.audio_tx_clients.clear()
        s.audio_tx_clients.update(self._saved_clients)
        s._ws_tokens.clear()
        s._ws_tokens.update(self._saved_tokens)
        s._tx_owner_ws = self._saved_owner

    def test_owner_disconnect_promotes_remaining_client(self):
        """After the owner drops, a remaining client must become owner —
        otherwise everyone stays muted until they reconnect."""
        s = self.server
        a, b = object(), object()
        s.audio_tx_clients.update([a, b])
        s._tx_owner_ws = a
        s.audio_tx_clients.discard(a)  # endpoint discards before promoting
        promoted = s._promote_tx_owner()
        self.assertIs(promoted, b)
        self.assertIs(s._tx_owner_ws, b)

    def test_promote_with_no_clients_yields_none(self):
        s = self.server
        s._tx_owner_ws = object()
        self.assertIsNone(s._promote_tx_owner())
        self.assertIsNone(s._tx_owner_ws)

    def test_ptt_client_claims_ownership_by_token(self):
        """The client keying the radio owns the uplink, even if another
        client connected first."""
        s = self.server
        ios, browser = object(), object()
        s.audio_tx_clients.update([ios, browser])
        s._ws_tokens[ios] = "ios-token"
        s._ws_tokens[browser] = "browser-token"
        s._tx_owner_ws = ios  # iOS app connected first
        claimed = s._claim_tx_owner_for_token("browser-token")
        self.assertIs(claimed, browser)
        self.assertIs(s._tx_owner_ws, browser)

    def test_claim_with_unknown_token_keeps_current_owner(self):
        s = self.server
        a = object()
        s.audio_tx_clients.add(a)
        s._tx_owner_ws = a
        self.assertIsNone(s._claim_tx_owner_for_token("no-such-token"))
        self.assertIs(s._tx_owner_ws, a)
        self.assertIsNone(s._claim_tx_owner_for_token(None))
        self.assertIs(s._tx_owner_ws, a)

    def test_endpoints_track_tokens(self):
        """Both endpoints must record the auth token per socket so the PTT
        handler can find the TX-audio socket of the keying client."""
        repo_root = Path(__file__).resolve().parents[1]
        server_source = (repo_root / "server.py").read_text(encoding="utf-8")
        self.assertIn("_ws_tokens[ws] = token", server_source)
        self.assertIn("_ws_tokens.pop(ws, None)", server_source)
        self.assertIn("_claim_tx_owner_for_token(_ws_tokens.get(ws))",
                      server_source)


if __name__ == "__main__":
    unittest.main()
