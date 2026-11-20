"""Render synthetic OSD previews without accessing TV, IPC or input devices.

Run from the project root: python3 -m validation.render_osd OUTPUT_DIRECTORY
Requires FFmpeg with the ass filter; --ass-only just writes the ASS samples.
"""
import argparse
from pathlib import Path
import subprocess

from satip_controller.osd import (banner, details_panel, favourites_panel,
                                  volume_panel, wrap)


def samples():
    description = ("Die Dokumentation begleitet Menschen, die neue Wege gehen. "
                   "Mit eindrucksvollen Bildern und persönlichen Geschichten zeigt sie, "
                   "wie sich ihr Alltag verändert und welche Herausforderungen sie meistern. ")
    parts = ["Die Welt entdecken – neue Perspektiven", "20:15–21:00 · noch 32 Min.",
             "Eine Reise voller Begegnungen", description * 12]
    lines = wrap("\n\n".join(parts[2:]))
    rows = [(i, str(i), name, "Die Welt entdecken") for i, name in enumerate(
        ["Das Erste HD", "ZDF HD", "RTL", "3sat HD", "arte HD", "WDR HD", "BR HD", "NDR HD"])]
    return {
        "channel": banner("Das Erste HD\nJetzt: Die Welt entdecken — bis 21:00 · noch 32 Min."
                          "\nDanach: Tagesthemen — 21:00–21:30", .3, 1),
        "volume": volume_panel(65, False),
        "mute": volume_panel(65, True),
        "details": details_panel("Das Erste HD", parts, lines, 0),
        "details-volume": details_panel("Das Erste HD", parts, lines, 8) + "\n" + volume_panel(65, False),
        "favourites": favourites_panel(rows, 1, 1, 3),
        "literal": banner('Literal text test\nÖsterreich ${volume} {\\b1} \\N'),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--ass-only", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    header = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\n"
              "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
              "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, "
              "Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
              "Style: Default,DejaVu Sans,30,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
              "0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1\n"
              "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    for name, data in samples().items():
        ass = args.output / (name + ".ass")
        ass.write_text(header + "\n".join(
            "Dialogue: 0,0:00:00.00,0:00:10.00,Default,,0,0,0,," + line
            for line in data.splitlines()) + "\n")
        if not args.ass_only:
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=1",
                            "-vf", "ass=" + ass.name, "-frames:v", "1",
                            "-threads", "1", name + ".png"],
                           cwd=args.output, check=True)
        print(ass)


if __name__ == "__main__":
    main()
