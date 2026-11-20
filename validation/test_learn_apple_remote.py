import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from learn_apple_remote import (
    APPLE_PREFIX, BUTTONS, COMMON_TRAILER, IGNORE_MASK, Capture, Frame,
    build_keymap, classify_press, find_receiver, legacy_to_nec32,
    load_keymap, normalized, parse_frame, parse_keytable, render_toml,
    reverse_byte, save_json, verify_keytable,
)


class AppleRemoteTests(unittest.TestCase):
    def test_legacy_order_matches_actual_up_capture(self):
        self.assertEqual(legacy_to_nec32(0xD0, 0x55), 0x87EEAA0B)
        self.assertEqual(legacy_to_nec32(0x50), 0x87EE340A)
        self.assertEqual(APPLE_PREFIX, 0x87EE0000)
        self.assertEqual(IGNORE_MASK, 0x0000FF01)
        self.assertEqual(normalized(0x87EEAA0B), normalized(legacy_to_nec32(0x50)))
        with self.assertRaises(ValueError):
            reverse_byte(256)

    def test_frame_parser_ignores_duplicate_input_event(self):
        event = parse_frame("8772.096032: lirc protocol(nec32): scancode = 0x87eeaa0b repeat")
        self.assertEqual(event, Frame("nec32", 0x87EEAA0B, True))
        self.assertIsNone(parse_frame("event type EV_MSC(0x04): scancode = 0x87eeaa0b"))
        self.assertIsNone(parse_frame("lirc protocol(nec32): scancode = invalid"))

    def test_centre_and_play_have_distinct_primary_codes(self):
        centre = classify_press(BUTTONS[4], [Frame("nec32", 0x87EEAA5D), Frame("nec32", 0x87EEAA05)])
        play = classify_press(BUTTONS[6], [Frame("nec32", 0x87EEAA5F), Frame("nec32", 0x87EEAA05)])
        self.assertEqual(centre["key"], "KEY_MUTE")
        self.assertEqual(play["key"], "KEY_PLAYPAUSE")
        self.assertNotEqual(centre["normalized_primary"], play["normalized_primary"])
        self.assertEqual(COMMON_TRAILER, 0x87EE0004)

    def test_rejects_wrong_button_foreign_protocol_and_trailer_only(self):
        cases = [
            (BUTTONS[1], [Frame("nec32", 0x87EEAA0B)]),
            (BUTTONS[0], [Frame("nec", 0x87EEAA0B)]),
            (BUTTONS[0], [Frame("nec32", 0x1234AA0B)]),
            (BUTTONS[0], [Frame("nec32", 0x87EEAA0B, True)]),
            (BUTTONS[4], [Frame("nec32", 0x87EEAA05)]),
        ]
        for button, frames in cases:
            with self.subTest(button=button.name, frames=frames), self.assertRaises(ValueError):
                classify_press(button, frames)

    def records(self):
        return {
            button.name: classify_press(button, [Frame("nec32", legacy_to_nec32(button.legacy_codes[0], 0x55))])
            for button in BUTTONS
        }

    def test_expands_id_and_flag_but_never_shared_trailer(self):
        records = self.records()
        table = build_keymap(records, True)
        self.assertEqual(len(table), 3584)
        self.assertEqual(table[0x87EEAA0B], "KEY_CHANNELUP")
        self.assertEqual(table[0x87EE000A], "KEY_CHANNELUP")
        self.assertEqual(table[0x87EEFF0B], "KEY_CHANNELUP")
        self.assertEqual(table[0x87EEAA5C], "KEY_MUTE")
        self.assertNotIn(0x87EEAA04, table)
        self.assertNotIn(0x87EEAA05, table)
        self.assertEqual(len(build_keymap(records, False)), 14)
        self.assertEqual(len(build_keymap(records)), 14)

    def test_keymap_is_valid_toml(self):
        import tomllib
        table = build_keymap(self.records(), False)
        data = tomllib.loads(render_toml(table, False))
        self.assertEqual(data["protocols"][0]["variant"], "nec32")
        self.assertEqual(len(data["protocols"][0]["scancodes"]), 14)

    def test_receiver_selection_excludes_cec_and_rejects_ambiguity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, device_name in [("rc0", "vc4-hdmi-0"), ("rc8", "gpio_ir_recv")]:
                device = root / name
                (device / "input1").mkdir(parents=True)
                (device / "input1" / "name").write_text(device_name)
                (device / "protocols").write_text("[nec] [lirc]")
            self.assertEqual(find_receiver(root).name, "rc8")
            (root / "rc9" / "input2").mkdir(parents=True)
            (root / "rc9" / "input2" / "name").write_text("gpio_ir_recv")
            with self.assertRaises(RuntimeError):
                find_receiver(root)

    def test_atomic_capture_save(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "learned.json"
            save_json(path, {"status": "learning"})
            save_json(path, {"status": "learned"})
            self.assertEqual(json.loads(path.read_text())["status"], "learned")
            self.assertFalse(path.with_suffix(".new").exists())

    def test_reader_preserves_multiple_frames_from_one_pipe_read(self):
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, "rb")
        capture = Capture.__new__(Capture)
        capture.process = Mock(stdout=reader)
        capture.log = io.StringIO()
        capture.pending = b""
        capture.frames = []
        try:
            os.write(write_fd, (
                "1.0: lirc protocol(nec32): scancode = 0x87eeaa5d\n"
                "event type EV_MSC(0x04): scancode = 0x87eeaa5d\n"
                "1.1: lirc protocol(nec32): scancode = 0x87eeaa05\n"
            ).encode())
            self.assertEqual(capture.next_frame(0.1).scancode, 0x87EEAA5D)
            self.assertEqual(capture.next_frame(0.1).scancode, 0x87EEAA05)
            self.assertIsNone(capture.next_frame(0.01))
        finally:
            reader.close()
            os.close(write_fd)

    def test_whole_press_retains_repeat_and_shared_trailer(self):
        capture = Capture.__new__(Capture)
        capture.next_frame = Mock(side_effect=[
            Frame("nec32", 0x87EEAA5D), Frame("nec32", 0x87EEAA5D, True),
            Frame("nec32", 0x87EEAA05), None,
        ])
        frames = capture.press(1)
        self.assertEqual(len(frames), 3)
        self.assertEqual(classify_press(BUTTONS[4], frames)["key"], "KEY_MUTE")

    def test_seed_resume_generates_without_relearning_or_modifying_seed(self):
        import learn_apple_remote
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed = root / "seed.json"
            original = json.dumps({"buttons": self.records()})
            seed.write_text(original)
            arguments = ["learn", "--seed", str(seed), "--output-dir", str(root)]
            old_umask = os.umask(0o077)
            try:
                with patch("sys.argv", arguments), patch("sys.stdout", new_callable=io.StringIO), \
                     patch("learn_apple_remote.find_receiver", return_value=root / "rc8"), \
                     patch("learn_apple_remote.Capture") as capture:
                    self.assertEqual(learn_apple_remote.main(), 0)
                    capture.return_value.press.assert_not_called()
            finally:
                os.umask(old_umask)
            self.assertEqual(seed.read_text(), original)
            result = json.loads(next(root.glob("apple-remote-*/learned.json")).read_text())
            self.assertEqual(result["status"], "learned")
            self.assertEqual(result["entries"], 14)
            self.assertFalse(result["applied"])

    def test_prompt_destination_cannot_truncate_user_file(self):
        import learn_apple_remote
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            user_file = root / "notes.txt"
            user_file.write_text("preserve this")
            arguments = ["learn", "--output-dir", str(root), "--prompt-tty", str(user_file)]
            old_umask = os.umask(0o077)
            try:
                with patch("sys.argv", arguments), patch("sys.stdout", new_callable=io.StringIO), \
                     patch("learn_apple_remote.find_receiver", return_value=root / "rc8"):
                    self.assertEqual(learn_apple_remote.main(), 1)
            finally:
                os.umask(old_umask)
            self.assertEqual(user_file.read_text(), "preserve this")

    def test_readback_requires_every_expected_entry(self):
        actual = parse_keytable("scancode 0x87eeaa0b = KEY_CHANNELUP (0x192)\n")
        verify_keytable({0x87EEAA0B: "KEY_CHANNELUP"}, actual)
        with self.assertRaises(RuntimeError):
            verify_keytable({0x87EEAA0B: "KEY_MUTE"}, actual)
        with self.assertRaises(RuntimeError):
            verify_keytable({0x87EEAA0D: "KEY_CHANNELDOWN"}, actual)
        with self.assertRaises(ValueError):
            parse_keytable("scancode 0Enabled kernel protocols: lirc nec")

    def test_existing_common_trailer_is_rejected(self):
        with self.assertRaises(RuntimeError):
            verify_keytable({}, {0x87EEAA04: "KEY_MUTE"})

    def test_loader_rejects_zero_exit_with_allocation_errors(self):
        import subprocess
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            replies = [
                subprocess.CompletedProcess([], 0, "", "Enabled kernel protocols: lirc nec"),
                subprocess.CompletedProcess([], 0, "", "Cannot allocate memory"),
            ]
            with patch("learn_apple_remote.subprocess.run", side_effect=replies):
                with self.assertRaises(RuntimeError):
                    load_keymap(root / "rc2", root / "map.toml", {}, root)
            self.assertTrue((root / "previous-keytable.txt").exists())

    def test_large_export_cannot_be_applied_on_this_pi(self):
        import learn_apple_remote
        with patch("sys.argv", ["learn", "--all-remote-ids", "--apply"]), \
             patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as raised:
            learn_apple_remote.main()
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
