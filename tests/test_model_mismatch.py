"""19 00 model-identity handling (spec 2026-09-12 §6.2).

No profile records an expected model ID yet, so the runtime must log the
observed bytes and never raise a mismatch verdict from a guess — a false
mismatch warning on a working radio would be a worse outcome than no
check at all.
"""
import dataclasses
import unittest
from unittest import mock

from backends.ic7300.backend import IC705Backend, IC7300Backend


class ModelIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_expectation_logs_observed_bytes_only(self):
        backend = IC705Backend(port="/dev/null")
        backend._civ.get_model_id = mock.AsyncMock(return_value=bytes((0xA4,)))
        with self.assertLogs("ic7300.backend", level="INFO") as cap:
            verdict = await backend._check_model_identity()
        self.assertIsNone(verdict)
        self.assertTrue(any("A4" in line.upper() for line in cap.output))

    async def test_silent_radio_is_skipped(self):
        backend = IC705Backend(port="/dev/null")
        backend._civ.get_model_id = mock.AsyncMock(return_value=None)
        self.assertIsNone(await backend._check_model_identity())

    async def test_query_error_is_skipped(self):
        backend = IC705Backend(port="/dev/null")
        backend._civ.get_model_id = mock.AsyncMock(
            side_effect=RuntimeError("port gone"))
        self.assertIsNone(await backend._check_model_identity())

    async def test_expected_bytes_match_returns_false(self):
        backend = IC705Backend(port="/dev/null")
        backend._civ.get_model_id = mock.AsyncMock(return_value=bytes((0xA4,)))
        # The profile is a frozen dataclass and lives on the class
        # attribute, so inject an expectation by patching the class.
        patched = dataclasses.replace(IC705Backend._profile,
                                      model_id_bytes=(0xA4,))
        with mock.patch.object(IC705Backend, "_profile", patched):
            self.assertIs(await backend._check_model_identity(), False)

    async def test_expected_bytes_mismatch_returns_true_and_warns(self):
        backend = IC7300Backend(port="/dev/null")
        backend._civ.get_model_id = mock.AsyncMock(return_value=bytes((0xB2,)))
        patched = dataclasses.replace(IC7300Backend._profile,
                                      model_id_bytes=(0x94,))
        with mock.patch.object(IC7300Backend, "_profile", patched):
            with self.assertLogs("ic7300.backend", level="WARNING") as cap:
                verdict = await backend._check_model_identity()
        self.assertIs(verdict, True)
        self.assertIn("mismatch", cap.output[0].lower())

    async def test_sync_result_carries_model_mismatch(self):
        backend = IC705Backend(port="/dev/null")
        backend._civ.initial_state_sync = mock.AsyncMock(return_value={"mode": 1})
        backend._check_model_identity = mock.AsyncMock(return_value=True)
        data = await backend.initial_state_sync()
        self.assertIs(data["model_mismatch"], True)
        self.assertEqual(data["mode"], 1)

    async def test_sync_result_omits_the_field_when_unknown(self):
        # None must NOT be written: the state field would otherwise be
        # reset to False and clear a real mismatch on the next sync.
        backend = IC705Backend(port="/dev/null")
        backend._civ.initial_state_sync = mock.AsyncMock(return_value={"mode": 1})
        backend._check_model_identity = mock.AsyncMock(return_value=None)
        data = await backend.initial_state_sync()
        self.assertNotIn("model_mismatch", data)


if __name__ == "__main__":
    unittest.main()
