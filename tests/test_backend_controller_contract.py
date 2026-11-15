"""The `cat` surface `server.py` asks of **every** backend, pinned.

2026-11-16 field defect (bundle `20261116-000702-36fa`, box line 1.28.66): the
operator selected FlexRadio in 连接设置, then saved SunSDR2DX — the save
answered `200 OK`, the env file was exactly right (`MRRC_RADIO_MODEL=sunsdr2dx`,
both hosts), and **nothing restarted**.  `_schedule_restart()`'s safety guard
asks `cat.connected` before dropping a carrier, `FlexController` has no such
member, and the `AttributeError` killed the timer thread *before* `os._exit()`::

    Exception in thread Thread-2:
      File "/opt/mrrc_modern/server.py", line 5005, in _exit
        and c.connected and r.is_transmitting):
    AttributeError: 'FlexController' object has no attribute 'connected'

The UI then reloaded onto a server still running the old model — "选了 flexradio
后再配置其他电台连接不起作用了".  Two guards live here, because either one alone
would have let the other half of the defect through:

* every backend controller answers the members the server queries on paths that
  run **regardless of model** (this class) — a new backend cannot ship a
  controller that is missing them;
* the server never trusts that answer to be there (`test_server_setup.py`
  `ScheduleRestartTests`, `HealthEndpointTests`) — a restart that silently does
  not happen costs the operator every later save.
"""

import asyncio
import importlib
import json
import unittest
from unittest import mock

import server

#: Controller classes, one per distinct controller implementation.  The Xiegu
#: G7250 has no class of its own — it runs the shared ``CivController``.
CONTROLLERS = (
    ("backends.ft710.cat_controller", "CatController"),
    ("backends.ic7300.civ_controller", "CivController"),
    ("backends.yaesu.cat_core", "YaesuCatController"),
    ("backends.flex.backend", "FlexController"),
    ("backends.sunsdr.backend", "SunSDR2DXController"),
)

#: Members used on paths that run for every model: connection state (health
#: endpoint, restart guard, WS fullState), connect/disconnect (lifespan), and
#: the PTT/frequency/mode writes behind the three controls every UI has.  A
#: capability-gated setter (``set_tuner``, ``set_scope_on`` …) is deliberately
#: **not** here: those are hidden by ``RadioCapabilities`` instead.
CORE_SURFACE = (
    "connected",
    "connect",
    "disconnect",
    "set_ptt",
    "set_frequency",
    "set_mode",
)


class ControllerSurfaceTests(unittest.TestCase):
    def test_every_controller_answers_the_core_surface(self):
        for module_name, class_name in CONTROLLERS:
            with self.subTest(controller=class_name):
                klass = getattr(importlib.import_module(module_name), class_name)
                missing = [name for name in CORE_SURFACE
                           if not hasattr(klass, name)]
                self.assertEqual(
                    missing, [],
                    f"{class_name} is missing {missing}; server.py asks for it "
                    "on paths that run for every model",
                )

    def test_the_flex_controller_reports_connection_state(self):
        """The one member the FlexController was missing — and the reason a
        model switch could not be applied."""
        from backends.flex.backend import FlexBackend
        backend = FlexBackend()  # type: ignore[abstract]
        backend.engine.connected = False
        self.assertFalse(backend.cat.connected)
        backend.engine.connected = True
        self.assertTrue(backend.cat.connected)


class HealthEndpointTests(unittest.TestCase):
    """`/api/health` is what the launcher, the hub and the settings dialog poll.

    It answered **500** on every flex box for as long as the flex backend was
    running (it reads `cat.connected`), so a health check could not tell "radio
    not connected" from "the endpoint is broken".
    """

    def _health(self, cat, audio=None, scope=None):
        with mock.patch.object(server, "cat", cat), \
             mock.patch.object(server, "audio", audio), \
             mock.patch.object(server, "scope", scope):
            response = asyncio.run(server.api_health())
        return response.status_code, json.loads(bytes(response.body))

    def test_answers_200_when_the_controller_has_no_connected(self):
        class _Bare:                     # FlexController-shaped
            async def set_ptt(self, tx):
                pass

        status, payload = self._health(_Bare())
        self.assertEqual(status, 200)
        self.assertFalse(payload["radio_connected"])
        self.assertEqual(payload["status"], "degraded")

    def test_reports_a_connected_radio(self):
        class _Cat:
            connected = True

        class _Audio:
            _rx_running = True

        class _Scope:
            connected = True

        status, payload = self._health(_Cat(), _Audio(), _Scope())
        self.assertEqual(status, 200)
        self.assertTrue(payload["radio_connected"])
        self.assertTrue(payload["audio_available"])
        self.assertTrue(payload["scope_connected"])
        self.assertEqual(payload["status"], "healthy")

    def test_a_member_that_raises_does_not_break_the_endpoint(self):
        class _Cat:
            @property
            def connected(self):
                raise RuntimeError("engine torn down")

        status, payload = self._health(_Cat())
        self.assertEqual(status, 200)
        self.assertFalse(payload["radio_connected"])


if __name__ == "__main__":
    unittest.main()
