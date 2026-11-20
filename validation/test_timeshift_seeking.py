"""Existing-session diagnostic cleanup must never discard USB history."""
import unittest
from unittest.mock import AsyncMock, Mock, patch

from validation.measure_timeshift_seeking_on_pi import restore_session


class SessionCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.status = {"owner_pid": 1, "playing": "channel", "health": {"relay_capture_pid": 2},
                       "timeshift": {"enabled": True}}
        self.ipc = Mock(command=AsyncMock(), get=AsyncMock(return_value={"seekable-ranges": [{"start": 10, "end": 100}]}))
        self.settled = AsyncMock(return_value={"target_reached": True})

    async def restore(self, position):
        with patch("validation.measure_timeshift_seeking_on_pi.read_json", return_value=self.status):
            return await restore_session(self.ipc, None, 1, "channel", 2, position, self.settled)

    async def test_restore_only_within_existing_packet_cache(self):
        self.assertTrue(await self.restore(50))
        self.assertEqual([c.args for c in self.ipc.command.await_args_list],
                         [("set_property", "pause", True), ("seek", 50, "absolute+exact")])

    async def test_expired_start_frame_leaves_session_paused_without_reload(self):
        self.assertFalse(await self.restore(0))
        self.ipc.command.assert_awaited_once_with("set_property", "pause", True)
        self.settled.assert_not_awaited()

    async def test_changed_owner_channel_or_capture_refuses_cleanup_controls(self):
        for section, key, value in ((None, "owner_pid", 3), (None, "playing", "other"),
                                    ("health", "relay_capture_pid", 4), ("timeshift", "enabled", False)):
            target = self.status if section is None else self.status[section]
            original = target[key]
            target[key] = value
            with self.assertRaises(AssertionError):
                await self.restore(50)
            target[key] = original
        self.ipc.command.assert_not_awaited()
