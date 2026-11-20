import subprocess
import tempfile
from pathlib import Path
import types
import unittest
from unittest.mock import patch

from validation import buildroot_media as media


class BuildrootMediaToolTests(unittest.TestCase):
    def test_current_preview_flags_without_web_dependencies(self):
        channel = types.SimpleNamespace(video="h264", audio_pid=257)
        args = media.preview_arguments(channel, Path("/run/check"))
        self.assertEqual(args[0], "/usr/bin/ffmpeg")
        self.assertIn("pipe:0", args)
        self.assertIn("0:i:257", args)
        self.assertEqual(args[args.index("-c:v") + 1], "copy")
        self.assertEqual(args[args.index("-c:a") + 1], "aac")
        channel.video = "mpeg2"
        args = media.preview_arguments(channel, Path("/run/check"))
        self.assertIn("libx264", args)
        self.assertIn("bwdif=mode=send_frame:parity=auto:deint=interlaced,scale=640:trunc(640/dar/2)*2,setsar=1", args)

    def test_subprocess_is_bounded_and_failure_is_not_ignored(self):
        with patch.object(subprocess, "run", return_value="result") as run:
            self.assertEqual(media.run(["ffmpeg", "-version"]), "result")
        self.assertTrue(run.call_args.kwargs["check"])
        self.assertEqual(run.call_args.kwargs["timeout"], 180)

    def test_existing_output_is_refused_before_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("sys.argv", ["buildroot_media.py", "generate", directory]), \
                    patch.object(media, "run", side_effect=AssertionError("executed")):
                with self.assertRaises(FileExistsError):
                    media.main()

    def test_fixtures_match_progressive_hd_and_interlaced_sd_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            commands = []
            def run(args, **kwargs):
                commands.append(args)
                Path(args[-1]).write_bytes(b"synthetic")
            def probe(executable, path):
                hd = path.name == "hd.ts"
                return [{"codec_type": "video", "codec_name": "h264" if hd else "mpeg2video",
                         "field_order": "progressive" if hd else "tt",
                         "r_frame_rate": "50/1" if hd else "25/1"},
                        {"codec_type": "audio", "codec_name": "mp2", "id": "0x101"}]
            with patch.object(media, "run", side_effect=run), patch.object(media, "probe", side_effect=probe):
                report = media.generate("ffmpeg", "ffprobe", output)
            self.assertFalse(report["hardware_tested"])
            self.assertIn("testsrc2=size=1280x720:rate=50", commands[0])
            self.assertNotIn("tinterlace=interleave_top", commands[0])
            self.assertIn("tinterlace=interleave_top", commands[1])


if __name__ == "__main__":
    unittest.main()
