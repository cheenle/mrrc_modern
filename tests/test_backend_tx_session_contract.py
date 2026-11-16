"""The TX-session contract, as the two network engines really behave.

Guard tests for the facts ``backends/{flex,sunsdr}/audio.py`` build their PCM16
bridge lifecycle on (AD-038): which liveness flag an adapter can trust the
moment ``start_tx()``/``stop_tx()`` land, and therefore where a session's
streaming state may legally be retired.  If an engine ever changes one of these,
the adapters' comments and ``tests/test_pcm16_backends.py`` go stale at the same
moment this fails — which is the point.  No radio, no socket.
"""
import inspect
import unittest

from backends.flex.engine import SmartSDREngine
from backends.sunsdr.engine import SunSDREngine


def _source(cls, name):
    try:
        return inspect.getsource(inspect.getattr_static(cls, name))
    except (OSError, TypeError, AttributeError):     # generated / missing member
        return ""


def _first_write_offset(cls, method, attribute):
    """(offset, kind) of the first assignment to ``self.<attribute>`` inside
    ``method``, where ``offset`` counts non-blank lines from the body's start and
    ``kind`` says whether it appeared before the first ``await``.

    Textual rather than behavioural on purpose: driving these engines needs a
    radio or a fake UDP peer, and what matters here is the *shape* of the code —
    whether a reader on another thread can trust the flag the moment the call
    returns.
    """
    src = _source(cls, method)
    if not src:
        return None
    lines = [ln.strip() for ln in src.splitlines()[1:] if ln.strip()]
    needle = f"self.{attribute}"
    for offset, line in enumerate(lines):
        if line.startswith("await ") or " await " in line:
            return (offset, "after-await")
        if line.startswith(needle) and "==" not in line and "=" in line:
            return (offset, "before-await")
    return None


class FlexTxSessionContractTests(unittest.TestCase):
    def test_start_tx_raises_the_liveness_flag_before_its_first_await(self):
        """``tx_active`` is a plain assignment ahead of any suspension point, so
        the adapter may read it the moment ``start_tx()`` lands."""
        found = _first_write_offset(SmartSDREngine, "start_tx", "tx_active")
        self.assertIsNotNone(found, "SmartSDREngine.start_tx does not set tx_active")
        self.assertEqual(found[1], "before-await",
                         "Flex start_tx now awaits before raising tx_active")

    def test_stop_tx_clears_it_before_awaiting_the_drain(self):
        """A graceful drain must therefore never be gated on ``tx_active``:
        retiring the streaming bridge before kicking the pacer would cut the
        tail of a live call short."""
        found = _first_write_offset(SmartSDREngine, "stop_tx", "tx_active")
        self.assertIsNotNone(found, "SmartSDREngine.stop_tx does not clear tx_active")
        self.assertEqual(found[1], "before-await",
                         "Flex stop_tx now awaits before clearing tx_active")

    def test_only_start_and_stop_write_the_flag_on_a_live_connection(self):
        """The adapter's ``start_tx`` reads the flag right after the await, so a
        third writer that could clear it in between would turn a good key-up into
        a refused one.  Today the only other writers are the two teardown paths
        (``stop()`` and the reconnect loop), which both stop the pacer first — if
        one ever appears on the happy path, this list grows and the read in
        ``backends/flex/audio.py`` has to be re-examined."""
        self.assertEqual(
            sorted(_writers_of(SmartSDREngine, "tx_active")),
            ["__init__", "_reconnect_loop", "start_tx", "stop", "stop_tx"],
            "SmartSDREngine gained a writer of tx_active")


def _writers_of(cls, attribute):
    import re
    pattern = re.compile(rf"self\.{re.escape(attribute)}\s*=[^=]")
    out = []
    for name in dir(cls):
        source = _source(cls, name)
        if source and pattern.search(source):
            out.append(name)
    return out


class SunSDRTxSessionContractTests(unittest.TestCase):
    def test_tx_active_is_derived_from_the_pacer_task(self):
        """It is a property over the task handle, so the adapter learns "this
        keystroke is over" exactly when the pacer has been reaped — which is what
        its uplink gate is allowed to key off."""
        self.assertIsInstance(getattr(SunSDREngine, "tx_active", None), property,
                              "SunSDREngine.tx_active is no longer a derived flag")

    def test_start_tx_returns_the_engine_verdict(self):
        """An unverified mode answers False, and that answer — not the flag — is
        what makes the server unkey. The flag cannot say anything before the
        first await, because the task is created after the verdict."""
        source = _source(SunSDREngine, "start_tx")
        self.assertIn("return False", source)
        self.assertLess(source.index("return False"),
                        source.index("_tx_pacer_task = asyncio.create_task"),
                        "SunSDR start_tx now creates the pacer before deciding")

    def test_stop_tx_reaps_the_pacer_task(self):
        source = _source(SunSDREngine, "stop_tx")
        self.assertIn("self._tx_pacer_task = None", source,
                      "SunSDR stop_tx no longer drops the task the flag reads")


if __name__ == "__main__":
    unittest.main()
