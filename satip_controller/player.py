import asyncio
import base64
import contextlib
import os
import math
import logging
import time
from pathlib import Path

from .config import credentials
from .drm import hd_rates, modes
from .audio import choose_track, layout_label, normal_german, output_layouts
from .mpv_ipc import MPVIPC, IPCError
from .timeshift import Timeshift
from .tuning import TuneFeedback
from .switching import SwitchExperiments

LOG = logging.getLogger(__name__)

BWDIF = [{"name": "lavfi", "params": {"graph": "bwdif=mode=send_field:parity=auto:deint=interlaced"}}]

# Full GPU scaling and CPU 1080p scaling exceed the Pi 4 frame budget at
# 50 fields/s. Sharpen luma at source size, then bicubic-scale to square-pixel
# 720p; the fast renderer handles the remaining enlargement to the display.
# Use DAR, not coded width: PAL SD commonly has non-square pixels (4:3/16:9).
SD_FILTERS = [{"name": "lavfi", "params": {"graph": (
    BWDIF[0]["params"]["graph"] +
    ",unsharp=5:5:0.35:5:5:0"
    ",scale=w=trunc(720*dar/2)*2:h=720:flags=bicubic,setsar=1"
)}}]

# Live TS audio can arrive behind video in bursts. Build the same one-second
# cushion before output starts that mpv otherwise builds after an underrun.
# File-local: history readers and YouTube retain their own buffering policy.
LIVE_BUFFER_OPTIONS = {"cache": "yes", "cache-pause": "yes",
                       "cache-pause-initial": "yes", "cache-pause-wait": "1"}


def url(channel):
    return f"http://127.0.0.1:9981/stream/channel/{channel.uuid}?profile=pass"


def output_devices(config, sysfs=Path("/sys/class/drm")):
    """Prefer the configured port, but follow a monitor on the other Pi HDMI port."""
    connector, audio = config.drm_connector, config.audio_device
    if connector not in ("HDMI-A-1", "HDMI-A-2"):
        return connector, audio
    other = "HDMI-A-1" if connector == "HDMI-A-2" else "HDMI-A-2"
    try:
        # Resolve the configured card, not an assumed card0 or another GPU.
        card = Path(config.drm_device).resolve(strict=True).name
        status = (sysfs / f"{card}-{connector}" / "status").read_text().strip()
        other_status = (sysfs / f"{card}-{other}" / "status").read_text().strip()
    except OSError:
        return connector, audio
    if status != "disconnected" or other_status != "connected":
        return connector, audio
    # Only move the standard Pi audio route associated with the old port.
    # Explicit USB/analogue/custom ALSA routes must remain untouched.
    old_audio = f"alsa/hdmi:CARD=vc4hdmi{int(connector[-1]) - 1},DEV=0"
    if audio == old_audio:
        audio = f"alsa/hdmi:CARD=vc4hdmi{int(other[-1]) - 1},DEV=0"
    LOG.warning("HDMI output %s disconnected; using %s (audio: %s)", connector, other, audio)
    return other, audio


def arguments(config, state, hwdec="v4l2m2m-copy"):
    connector, audio = output_devices(config)
    mode = config.drm_mode
    if mode in ("1920x1080@50", "1920x1080@60"):
        rates = hd_rates(modes(config.drm_device, connector))
        requested = int(mode.split("@")[1])
        if rates and not any(round(rate, 2) == requested for rate in rates):
            target = requested if any(abs(rate - requested) < .5 for rate in rates) else 110 - requested
            selected = min(rates, key=lambda rate: abs(rate - target))
            rate_text = f"{selected:.2f}".rstrip("0").rstrip(".")
            mode = f"1920x1080@{rate_text}"
            LOG.warning("HDMI mode %s unavailable on %s; using %s", config.drm_mode, connector, mode)
    return ["mpv", "--no-config", "--vo=gpu", "--gpu-api=opengl", "--gpu-context=drm",
            f"--drm-device={config.drm_device}", f"--drm-connector={connector}",
            f"--drm-mode={mode}", f"--hwdec={hwdec}", "--scale=bilinear",
            "--cscale=bilinear", "--dscale=bilinear", "--gpu-dumb-mode=yes", "--dither-depth=no",
            "--fbo-format=rgba8", "--deband=no", "--interpolation=no", "--ao=alsa",
            f"--audio-device={audio}", f"--audio-channels={output_layouts(config, connector, audio)}",
            "--ad-lavc-downmix=no", "--aid=no",
            "--audio-samplerate=48000", "--audio-format=s16", "--audio-spdif=",
            f"--volume={state['volume']}", f"--mute={'yes' if state['mute'] else 'no'}",
            "--volume-max=100", "--alang=deu,ger,de", "--sid=no", "--idle=yes",
            "--force-window=yes", "--input-terminal=no", "--input-default-bindings=no",
            "--no-terminal", "--msg-level=all=no", "--osd-font-size=32", "--osd-border-size=2",
            "--osd-back-color=#90000000", "--osd-margin-x=50", "--osd-margin-y=40",
            f"--input-ipc-server={config.runtime_dir / 'mpv.sock'}",
            f"--include={config.runtime_dir / 'auth.conf'}"]


class Player:
    def __init__(self, config, state):
        self.config, self.state = config, state
        self.process = None
        self.ipc = None
        self.owned_runtime = False
        self.hwdec = "v4l2m2m-copy"
        self.tv_header = None
        self.external_stream = False
        self.timeshift = Timeshift()
        self.tuning = TuneFeedback(config.runtime_dir)
        self.window_pending = False
        self.audio_layouts = "stereo"
        self.audio_info = None
        self.audio_selection = None
        self.current_channel = None
        self.audio_id = None
        self.initial_audio_pending = False
        self.saved_audio = {}
        self.audio_cache_key = None
        self.tv_properties = {}
        self.switch_experiments = SwitchExperiments()

    def recover(self, reason):
        if reason == "ipc_timeout" and self.hwdec != "no":
            # Some Pi/FFmpeg V4L2 combinations block the mpv core indefinitely.
            # Keep software decoding for this controller lifetime, including zaps.
            self.hwdec = "no"
            LOG.warning("mpv unresponsive; retrying with software video decoding")

    async def start(self):
        await self.close()
        self.tv_properties.clear()
        self.saved_audio.clear()
        runtime = self.config.runtime_dir
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        if runtime.stat().st_mode & 0o077:
            raise ValueError("Player runtime directory must have mode 0700")
        sock = runtime / "mpv.sock"
        # An advisory application lock prevents another instance owning this path.
        if sock.exists():
            try:
                reader, writer = await asyncio.open_unix_connection(str(sock))
            except (ConnectionRefusedError, FileNotFoundError):
                sock.unlink(missing_ok=True)
            else:
                writer.close()
                await writer.wait_closed()
                raise IPCError("A live player already owns the runtime socket")
        self.owned_runtime = True
        secret = credentials(self.config.credentials)
        header = base64.b64encode((secret['username'] + ':' + secret['password']).encode()).decode()
        self.tv_header = "Authorization: Basic " + header
        self.external_stream = False
        auth = runtime / "auth.conf"
        fd = os.open(auth, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as out:
            os.fchmod(out.fileno(), 0o600)
            out.write('http-header-fields="Authorization: Basic ' + header + '"\n')
        argv = arguments(self.config, self.state.data, self.hwdec)
        self.window_pending = os.environ.get("SATIP_BOOT_SPLASH") == "1"
        if self.window_pending:
            argv[argv.index("--force-window=yes")] = "--force-window=no"
            # The initial EGL buffer is transparent. Drawing on an overlay
            # keeps the retained primary splash visible until the first opaque
            # mpv frame. Copy decoding does not use the drmprime video plane.
            argv += ["--drm-draw-plane=overlay", "--drm-drmprime-video-plane=primary"]
        self.process = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        try:
            async with asyncio.timeout(10):
                while self.process.returncode is None:
                    try:
                        self.ipc = await MPVIPC.connect(sock)
                        return
                    except OSError:
                        await asyncio.sleep(0.1)
                raise IPCError("Player failed to start on appliance VT")
        except BaseException:
            await self.close()
            raise

    async def show_window(self):
        # The controller queues the splash while mpv is still headless, so its
        # first normal VO draw already includes the overlay.
        if self.window_pending:
            await self.ipc.command("set_property", "force-window", "immediate")
            self.window_pending = False
            LOG.info("Player display enabled (%.3f s since Linux start)", time.monotonic())

    async def stop_stream(self):
        self.audio_id = None
        self.initial_audio_pending = False
        self.audio_info = None
        self.audio_selection = None
        self.tuning.cancel()
        await self.timeshift.close()
        if self.ipc and not self.ipc.closed:
            await self.ipc.command("stop")
            async with asyncio.timeout(5):
                while not await self.ipc.get("idle-active"):
                    await asyncio.sleep(0.05)

    async def tv_property(self, name, value):
        if (not self.switch_experiments.redundant_ipc
                or name not in self.tv_properties or self.tv_properties[name] != value):
            await self.ipc.command("set_property", name, value)
            # Cache only successful writes, and only controller-owned properties.
            self.tv_properties[name] = value

    async def load(self, channel):
        await self.stop_stream()
        self.switch_experiments = SwitchExperiments.load(self.config.runtime_dir)
        self.current_channel = channel
        if self.external_stream:
            await self.ipc.command("set_property", "http-header-fields", [self.tv_header])
            await self.ipc.command("set_property", "audio-files", [])
            await self.ipc.command("set_property", "start", "none")
            await self.ipc.command("set_property", "pause", False)
            self.external_stream = False
        # Pi 4 H.264 HD uses V4L2; MPEG-2 SD stays on CPU for picture processing.
        await self.tv_property("hwdec", "no" if channel.video == "mpeg2" else self.hwdec)
        await self.tv_property("vf", SD_FILTERS if channel.video == "mpeg2" else [])
        await self.tv_property("deinterlace", False)
        connector, audio = output_devices(self.config)
        self.audio_layouts = output_layouts(self.config, connector, audio)
        await self.tv_property("audio-channels", self.audio_layouts)
        # Probe audio and video together, but never play an unverified track.
        # Starting with aid=no then enabling audio discards its early packets
        # and can leave audio ahead of video on the non-seekable live relay.
        await self.ipc.command("set_property", "pause", True)
        await self.ipc.command("set_property", "aid", "auto")
        self.initial_audio_pending = True
        self.ipc.begin_load()
        # Live relay and demuxer buffers are RAM-only; verify audio before output.
        # USB capture is activated only by Pause, without a tuner handover.
        path = await self.timeshift.prepare(url(channel), self.tv_header,
                                            self.config.runtime_dir, self.reload_buffer)
        self.audio_cache_key = (channel, self.audio_layouts, bool(self.timeshift.buffer))
        options = {**self.timeshift.options(), **LIVE_BUFFER_OPTIONS, **self.tuning.begin(),
                   **self.switch_experiments.probe_options()}
        if self.switch_experiments.saved_audio and self.audio_cache_key in self.saved_audio:
            # mpv IDs are not MPEG-TS PIDs. Seed only a previously verified ID;
            # live selection below still runs while paused, including fallback.
            options["aid"] = str(self.saved_audio[self.audio_cache_key])
        await self.ipc.command("loadfile", path, "replace", -1, options)

    async def reload_buffer(self, path, start, paused):
        self.tv_properties.clear()
        self.audio_cache_key = None
        # Keep the selected normal audio stream active during demuxer probing.
        # Selecting it afterwards discards its old cache and loses the pause PTS.
        # Stay paused until the new reader's audio policy has been verified.
        self.ipc.begin_load()
        await self.ipc.command("set_property", "pause", True)
        options = {**self.timeshift.options(), "start": "none",
                   "demuxer-readahead-secs": "60"}
        if self.audio_id is not None:
            options["aid"] = str(self.audio_id)
        await self.ipc.command("loadfile", path, "replace", -1, options)
        if self.current_channel is not None:
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                if await self.select_audio(self.current_channel):
                    break
                await asyncio.sleep(.05)
            else:
                raise IPCError("Normal German audio unavailable after reader handover", reason="audio_pid_missing")
        await self.ipc.command("set_property", "pause", paused)

    async def timeshift_action(self, action, target=None):
        return await self.timeshift.action(self.ipc, action, target=target)

    async def load_youtube(self, video, audio, position=0):
        from .youtube import local_media_url
        local_media_url(video)
        if audio:
            local_media_url(audio)
        await self.stop_stream()
        self.tv_properties.clear()
        self.audio_cache_key = None
        # The global TV Authorization header MUST NOT reach an external host.
        self.external_stream = True
        await self.ipc.command("set_property", "http-header-fields", [])
        await self.ipc.command("set_property", "tls-verify", True)
        await self.ipc.command("set_property", "audio-files", [audio] if audio else [])
        await self.ipc.command("set_property", "hwdec", self.hwdec)
        await self.ipc.command("set_property", "vf", [])
        await self.ipc.command("set_property", "deinterlace", False)
        await self.ipc.command("set_property", "aid", "auto")
        await self.ipc.command("set_property", "pause", False)
        await self.ipc.command("set_property", "start", str(position))
        self.ipc.begin_load()
        # File-local buffering: build a small startup cushion for internet
        # streams without changing low-latency TV loads after this file ends.
        await self.ipc.command("loadfile", video, "replace", -1, {
            "cache": "yes", "cache-pause": "yes",
            "cache-pause-initial": "yes", "cache-pause-wait": "3",
            "demuxer-readahead-secs": "20", "cache-secs": "20",
        })

    async def select_audio(self, channel):
        tracks = await self.ipc.get("track-list")
        track = choose_track(tracks, channel.audio_pid, self.audio_layouts != "stereo")
        if track is None:
            self.saved_audio.pop(self.audio_cache_key, None)
            self.audio_info = None
            if any(t.get("type") == "audio" and t.get("selected") for t in tracks or []):
                await self.ipc.command("set_property", "aid", "no")
            return False
        self.audio_id = track["id"]
        if self.audio_cache_key is not None:
            if len(self.saved_audio) >= 500 and self.audio_cache_key not in self.saved_audio:
                self.saved_audio.pop(next(iter(self.saved_audio)))
            self.saved_audio[self.audio_cache_key] = self.audio_id
        pid = track.get("src-id", track.get("demux-id"))
        if self.audio_selection != pid:
            if not self.timeshift.buffer and not any(t.get("src-id", t.get("demux-id")) == channel.audio_pid for t in tracks or []):
                LOG.warning("Saved audio PID %s absent for channel %s; using live normal German PID %s. "
                            "Refresh the channel catalogue to update saved metadata.",
                            channel.audio_pid, channel.uuid, pid)
            self.audio_selection = pid
        # Reassigning even the active track resets decoding in mpv.
        # Avoid draining/reinitializing the Pi V4L2 decoder needlessly.
        if not track.get("selected"):
            await self.ipc.command("set_property", "aid", track["id"])
            self.audio_info = None
        if self.initial_audio_pending:
            # Track discovery is not buffer readiness. mpv's automatic initial
            # cache pause can still happen after an early playback restart,
            # especially when selecting audio rebuilds its demuxer queue. Keep
            # the explicit audio-policy pause until the selected streams have
            # the same cushion required for underrun recovery. The controller
            # keeps polling this gate within its existing load deadline.
            duration = await self.ipc.get("demuxer-cache-duration")
            if (type(duration) not in (int, float) or not math.isfinite(duration)
                    or duration < 1):
                return False
            await self.ipc.command("set_property", "pause", False)
            self.initial_audio_pending = False
        return True

    async def refresh_audio_info(self):
        tracks = await self.ipc.get("track-list")
        selected = next((t for t in tracks or [] if t.get("type") == "audio" and t.get("selected")), None)
        if not selected or not normal_german(selected):
            self.audio_info = None
            return
        source = layout_label(await self.ipc.get("audio-params"))
        output = layout_label(await self.ipc.get("audio-out-params"))
        self.audio_info = {"source": source, "output": output} if source and output else None

    def audio_label(self):
        if not self.audio_info:
            return ""
        source, output = self.audio_info["source"], self.audio_info["output"]
        return "Deutsch · " + (source if source == output else f"{source} → {output}")

    async def sample(self, channel):
        ipc = self.ipc
        if not ipc or ipc.closed or self.process.returncode is not None:
            raise IPCError("Player disconnected")
        if ipc.load_failure or self.tuning.failed():
            raise IPCError("Channel load failed", reason="load_failed")
        expected = self.timeshift.buffer.path if self.timeshift.buffer else url(channel)
        if await ipc.get("path") != expected or await ipc.get("idle-active"):
            return None
        if not await ipc.get("video-codec") or not await ipc.get("audio-codec"):
            return None
        position = await ipc.get("time-pos")
        if type(position) not in (int, float):
            return None
        self.tuning.cancel()
        return position

    async def diagnostics(self):
        # Explicit property allowlist: never expose paths, headers or raw IPC.
        names = ("paused-for-cache", "demuxer-cache-duration", "frame-drop-count",
                 "decoder-frame-drop-count", "avsync", "hwdec-current",
                 "time-pos", "audio-pts", "volume", "mute")
        async def reading(name):
            try:
                value = await self.ipc.get(name)
                if type(value) in (bool, str):
                    return value[:80] if isinstance(value, str) else value
                if type(value) in (int, float) and math.isfinite(value):
                    return value
            except IPCError:
                pass
            return None
        try:
            async with asyncio.timeout(.5):
                values = await asyncio.gather(*(reading(name) for name in names))
                health = dict(zip(names, values))
                if self.timeshift.buffer:
                    health.update(self.timeshift.buffer.diagnostics())
                return health
        except TimeoutError:
            return dict.fromkeys(names)

    async def close(self):
        self.tuning.cancel()
        await self.timeshift.shutdown()
        if self.ipc:
            await self.ipc.close()
            self.ipc = None
        if self.process:
            if self.process.returncode is None:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 5)
                except asyncio.TimeoutError:
                    self.process.kill()
            await self.process.wait()
            self.process = None
        if self.owned_runtime:
            for name in ("auth.conf", "mpv.sock"):
                with contextlib.suppress(FileNotFoundError):
                    (self.config.runtime_dir / name).unlink()
            self.owned_runtime = False
