"""Conservative HDMI PCM capabilities and normal German TV track policy."""
from pathlib import Path

GERMAN = ("deu", "ger", "de")
CODECS = ("mp2", "ac3", "eac3", "aac")


def pcm_channels(edid):
    """Maximum LPCM channels at our fixed 48 kHz / 16-bit output format."""
    if (len(edid) < 128 or len(edid) % 128
            or edid[:8] != bytes.fromhex("00ffffffffffff00")
            or len(edid) != (edid[126] + 1) * 128
            or any(sum(edid[i:i + 128]) % 256 for i in range(0, len(edid), 128))):
        return 2
    maximum = 2
    for offset in range(128, len(edid), 128):
        block = edid[offset:offset + 128]
        if block[0] != 2:
            continue
        end = block[2]
        if end == 0:
            continue
        if not 4 <= end <= 127:
            return 2
        pos = 4
        while pos < end:
            tag, length = block[pos] >> 5, block[pos] & 31
            if pos + 1 + length > end:
                return 2
            data = block[pos + 1:pos + 1 + length]
            if tag == 1:
                if length % 3:
                    return 2
                for i in range(0, length, 3):
                    descriptor = data[i:i + 3]
                    if ((descriptor[0] >> 3) & 15 == 1
                            and descriptor[1] & 4 and descriptor[2] & 1):
                        maximum = max(maximum, (descriptor[0] & 7) + 1)
            pos += 1 + length
    return maximum


def output_layouts(config, connector, audio, sysfs=Path("/sys/class/drm")):
    # EDID describes only its HDMI route, never a custom USB/analogue device.
    if connector not in ("HDMI-A-1", "HDMI-A-2"):
        return "stereo"
    expected = f"alsa/hdmi:CARD=vc4hdmi{int(connector[-1]) - 1},DEV=0"
    if audio != expected:
        return "stereo"
    try:
        card = Path(config.drm_device).resolve(strict=True).name
        port = sysfs / f"{card}-{connector}"
        if (port / "status").read_text().strip() != "connected":
            return "stereo"
        count = pcm_channels((port / "edid").read_bytes())
    except OSError:
        return "stereo"
    # Explicit layouts: ALSA's raw HDMI limits alone do not describe the sink.
    layouts = []
    if count >= 8:
        layouts.append("7.1")
    if count >= 7:
        layouts.append("6.1")
    if count >= 6:
        layouts.extend(("5.1", "5.1(side)"))
    if count >= 5:
        layouts.extend(("5.0", "5.0(side)"))
    if count >= 4:
        layouts.extend(("quad", "4.0"))
    if count >= 3:
        layouts.extend(("2.1", "3.0"))
    return ",".join([*layouts, "stereo"])


def normal_german(track):
    pid = track.get("src-id", track.get("demux-id"))
    return (track.get("type") == "audio" and track.get("lang") in GERMAN
            and track.get("codec") in CODECS
            and track.get("visual-impaired") is False
            and track.get("hearing-impaired") is False
            and track.get("external") is False
            and type(pid) is int and 1 <= pid <= 8191)


def choose_track(tracks, saved_pid, multichannel):
    audio = [track for track in tracks or [] if normal_german(track)]
    def rank(track):
        saved = track.get("src-id", track.get("demux-id")) == saved_pid
        count = track.get("demux-channel-count", track.get("audio-channels", 0))
        surround = type(count) is int and count > 2
        if multichannel:
            # Keep Dolby selected even during stereo programmes so a later
            # 5.1 transition is decoded and detected without another zap.
            return surround, track.get("codec") in ("ac3", "eac3"), saved
        return saved, track.get("codec") == "mp2", not surround
    if not audio:
        return None
    best = max(rank(track) for track in audio)
    matches = [track for track in audio if rank(track) == best]
    return matches[0] if len(matches) == 1 else None


def layout_label(params):
    if not isinstance(params, dict):
        return None
    count = params.get("channel-count")
    if type(count) is not int or not 1 <= count <= 8:
        return None
    layout = params.get("channels", "")
    if count == 1:
        return "Mono"
    if count == 2:
        return "Stereo"
    if isinstance(layout, str) and layout.split("(")[0] in ("5.1", "7.1"):
        return layout.split("(")[0]
    return f"{count} ch"
