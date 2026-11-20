#!/usr/bin/env python3
"""Synthetic fixtures and local-file preview checks; never tunes or opens HDMI."""

import argparse
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import types


ROOT = Path(__file__).resolve().parents[1]


def preview_arguments(channel, directory):
    # Load only the reviewed self-contained argument function, not web/backend code.
    path = ROOT / "satip_controller/preview.py"
    tree = ast.parse(path.read_text())
    function, = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name == "ffmpeg_arguments"]
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["ffmpeg_arguments"](channel, directory)


def run(args, **kwargs):
    return subprocess.run(args, check=True, timeout=180, **kwargs)


def probe(ffprobe, path):
    result = run([ffprobe, "-v", "error", "-show_streams", "-of", "json", str(path)],
                 capture_output=True, text=True)
    return json.loads(result.stdout)["streams"]


def generate(ffmpeg, ffprobe, directory):
    records = {}
    for name, size, codec in (("hd", "1280x720", "libx264"),
                              ("sd", "720x576", "mpeg2video")):
        path = directory / (name + ".ts")
        args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
                "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=50",
                "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
                "-t", "12", "-filter_threads", "2",
                "-c:v", codec, "-threads", "2", "-pix_fmt", "yuv420p"]
        if name == "hd":
            args += ["-preset", "ultrafast", "-crf", "23", "-g", "50",
                     "-x264-params", "scenecut=0"]
        else:
            args += ["-vf", "tinterlace=interleave_top", "-flags", "+ilme+ildct",
                     "-g", "25", "-b:v", "4000k", "-aspect", "16:9"]
        args += ["-c:a", "mp2", "-b:a", "192k", "-ac", "2", "-ar", "48000",
                 "-streamid", "0:256", "-streamid", "1:257", "-f", "mpegts", str(path)]
        run(args, stdin=subprocess.DEVNULL)
        streams = probe(ffprobe, path)
        video, = [stream for stream in streams if stream["codec_type"] == "video"]
        audio, = [stream for stream in streams if stream["codec_type"] == "audio"]
        expected_fields = ("progressive",) if name == "hd" else ("tt", "tb")
        expected_rate = "50/1" if name == "hd" else "25/1"
        if video["codec_name"] != ("h264" if name == "hd" else "mpeg2video") or \
                video["field_order"] not in expected_fields or video["r_frame_rate"] != expected_rate or \
                audio["codec_name"] != "mp2" or int(audio["id"], 0) != 257:
            raise ValueError("Synthetic fixture does not meet HD/SD codec/PID contract")
        records[name] = {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                         "streams": streams, "generation_arguments": args}
    return {"kind": "synthetic-fixtures", "hardware_tested": False, "assets": records}


def check_preview(ffmpeg, ffprobe, fixtures, directory):
    manifest = json.loads((fixtures / "report.json").read_text())
    results = {}
    for name in ("hd", "sd"):
        path = fixtures / (name + ".ts")
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["assets"][name]["sha256"]:
            raise ValueError("Fixture hash mismatch")
        destination = directory / name
        destination.mkdir(mode=0o700)
        channel = types.SimpleNamespace(video="h264" if name == "hd" else "mpeg2", audio_pid=257)
        args = preview_arguments(channel, destination)
        # Only the executable is adapted for native-host smoke tests.
        args[0] = ffmpeg
        with path.open("rb") as source:
            run(args, stdin=source, stdout=subprocess.DEVNULL)
        playlist = destination / "index.m3u8"
        lines = playlist.read_text().splitlines()
        segments = [line for line in lines if line and not line.startswith("#")]
        if len(segments) < 2 or "#EXT-X-ENDLIST" not in lines:
            raise ValueError("Incomplete HLS output")
        streams = probe(ffprobe, destination / segments[0])
        video, = [stream for stream in streams if stream["codec_type"] == "video"]
        audio, = [stream for stream in streams if stream["codec_type"] == "audio"]
        if video["codec_name"] != "h264" or audio["codec_name"] != "aac" or \
                audio["channels"] != 2 or audio["sample_rate"] != "48000":
            raise ValueError("Unexpected preview codecs/audio format")
        if name == "sd" and (video["width"] != 640 or video["field_order"] != "progressive"):
            raise ValueError("SD preview scaling/deinterlacing failed")
        if name == "hd" and (video["width"] != 1280 or video["height"] != 720 or
                             video["r_frame_rate"] != "50/1"):
            raise ValueError("HD preview did not preserve 720p50")
        run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
             "-i", str(playlist), "-f", "null", "-"], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL)
        results[name] = {"segments": len(segments), "streams": streams, "arguments": args}
    return {"kind": "local-file-preview-smoke", "hardware_tested": False,
            "preview_source_sha256": hashlib.sha256((ROOT / "satip_controller/preview.py").read_bytes()).hexdigest(),
            "results": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("generate", "preview"))
    parser.add_argument("output", type=Path, help="new directory; existing paths refused")
    parser.add_argument("--fixtures", type=Path)
    parser.add_argument("--ffmpeg", default="/usr/bin/ffmpeg")
    parser.add_argument("--ffprobe", default="/usr/bin/ffprobe")
    args = parser.parse_args()
    if args.mode == "preview" and not args.fixtures:
        parser.error("preview requires --fixtures")
    args.output.mkdir(mode=0o700)
    versions = {}
    for name, executable in (("ffmpeg", args.ffmpeg), ("ffprobe", args.ffprobe)):
        versions[name] = run([executable, "-version"], capture_output=True, text=True).stdout
    report = generate(args.ffmpeg, args.ffprobe, args.output) if args.mode == "generate" else \
        check_preview(args.ffmpeg, args.ffprobe, args.fixtures, args.output)
    report["tool_versions"] = versions
    (args.output / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"Completed {args.mode}: {args.output}; not HDMI/Pi acceptance")


if __name__ == "__main__":
    main()
