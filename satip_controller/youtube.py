"""YouTube TV-code receiver; no LAN listener or Google login.

Lounge is an undocumented protocol, not Google Cast. Network work and video
resolution run separately; only Controller.tick touches its owned mpv instance.
"""
import asyncio
import json
import math
import os
from pathlib import Path
import re
import secrets
import sys
import time
from urllib.parse import urlsplit
import uuid

import httpx

from .state import atomic_json, boot_id, read_json, runtime_path
from .mpv_ipc import IPCError

BASE = "https://www.youtube.com/api/lounge/"
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
MAX_FRAME = 1024 * 1024
DEFAULT = {"version": 1, "enabled": False, "pair_request": "", "unlink_request": ""}


def preferences_path(directory):
    return Path(directory) / "youtube.json"


def preferences(directory):
    try:
        data = read_json(preferences_path(directory))
    except FileNotFoundError:
        return dict(DEFAULT)
    if (not isinstance(data, dict) or set(data) != set(DEFAULT)
            or type(data["version"]) is not int or data["version"] != 1
            or type(data["enabled"]) is not bool
            or any(not isinstance(data[k], str) or
                   (data[k] and not re.fullmatch(r"[0-9a-f]{32}", data[k]))
                   for k in ("pair_request", "unlink_request"))):
        raise ValueError("Invalid YouTube preferences")
    return data


def public_status(directory):
    try:
        data = read_json(runtime_path(Path(directory), "youtube-status.json"))
        if data.get("boot_id") != boot_id() or time.monotonic() - data["updated"] > 10:
            raise ValueError("Stale receiver")
        # Do not expose identity, tokens, URLs, arbitrary protocol data or IPC.
        result = {k: data.get(k) for k in ("state", "active", "video_id", "error")}
        result["pairing_code"] = data.get("pairing_code") if data.get("code_until", 0) > time.monotonic() else None
        return result
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {"state": "unavailable", "active": False, "pairing_code": None,
                "error": "Player receiver is not running"}


class Frames:
    """Incremental BrowserChannel framing (lengths are UTF-16 code units)."""
    def __init__(self):
        self.buffer = ""

    def feed(self, text):
        self.buffer += text
        result = []
        while self.buffer:
            header, separator, body = self.buffer.partition("\n")
            if not separator:
                if len(header) > 10:
                    raise ValueError("Invalid Lounge frame header")
                break
            if not header.isascii() or not header.isdigit() or not 0 < int(header) <= MAX_FRAME:
                raise ValueError("Invalid Lounge frame length")
            needed, units, end = int(header), 0, 0
            for end, char in enumerate(body, 1):
                units += 2 if ord(char) > 0xffff else 1
                if units >= needed:
                    break
            if units < needed:
                break
            if units != needed:
                raise ValueError("Invalid Lounge character boundary")
            messages = json.loads(body[:end])
            if not isinstance(messages, list):
                raise ValueError("Invalid Lounge messages")
            for message in messages:
                if (not isinstance(message, list) or len(message) != 2
                        or type(message[0]) is not int or not isinstance(message[1], list)
                        or not message[1] or not isinstance(message[1][0], str)):
                    raise ValueError("Invalid Lounge message")
                result.append(message)
            self.buffer = body[end:]
        if len(self.buffer) > MAX_FRAME:
            raise ValueError("Lounge buffer too large")
        return result


class Lounge:
    def __init__(self, identity, incoming):
        self.identity, self.incoming = identity, incoming
        self.token = self.sid = self.gsession = ""
        self.aid, self.rid, self.offset = -1, 45000, 0
        self.outgoing = asyncio.Queue(maxsize=32)
        self.client = None
        self.ready = False

    def params(self, **extra):
        return {"device": "LOUNGE_SCREEN", "id": self.identity["device_id"],
                "name": "YouTube on SAT-IP Pi", "app": "ytcr", "theme": "cl",
                "obfuscatedGaiaId": "", "capabilities": "dsp,mic,dpa,ntb",
                "cst": "m", "mdxVersion": 2, "loungeIdToken": self.token,
                "VER": 8, "v": 2, "zx": secrets.token_hex(6), "t": 1, **extra}

    async def request(self, endpoint, data=None, params=None):
        async with self.client.stream("GET" if data is None else "POST", BASE + endpoint,
                                      data=data, params=params) as response:
            response.raise_for_status()
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > MAX_FRAME:
                    raise ValueError("Lounge response too large")
            return content.decode()

    async def code(self):
        code = (await self.request("pairing/get_pairing_code", {
            "access_type": "permanent", "app": "ytcr", "lounge_token": self.token,
            "screen_id": self.identity["screen_id"], "screen_name": "YouTube on SAT-IP Pi",
            "device_id": self.identity["device_id"]})).strip()
        if not re.fullmatch(r"[0-9]{12}", code):
            raise ValueError("YouTube did not issue a numeric TV code")
        return code

    async def dispatch(self, messages):
        for index, command in messages:
            self.aid = max(self.aid, index)
            name, *args = command
            if name == "c" and args and isinstance(args[0], str):
                self.sid = args[0]
            elif name == "S" and args and isinstance(args[0], str):
                self.gsession = args[0]
            elif name not in ("noop", "c", "S"):
                await self.incoming.put((name, args[0] if args and isinstance(args[0], dict) else {}))

    def notify(self, name, payload):
        # Coalesce by dropping the oldest status; never an unbounded queue.
        if self.outgoing.full():
            self.outgoing.get_nowait()
        self.outgoing.put_nowait((name, payload))

    async def poll(self):
        while True:
            parser = Frames()
            params = self.params(SID=self.sid, RID="rpc", CI=0, AID=self.aid,
                                 gsessionid=self.gsession, TYPE="xmlhttp")
            async with self.client.stream("GET", BASE + "bc/bind", params=params) as response:
                response.raise_for_status()
                async for text in response.aiter_text():
                    await self.dispatch(parser.feed(text))
            if parser.buffer:
                raise ValueError("Truncated Lounge response")
            await asyncio.sleep(.1)

    async def send(self):
        while True:
            name, payload = await self.outgoing.get()
            body = {"count": "1", "ofs": str(self.offset), "req0__sc": name}
            body.update({"req0_" + k: str(v).lower() if isinstance(v, bool) else str(v)
                         for k, v in payload.items()})
            self.offset += 1
            self.rid += 1
            await self.request("bc/bind", body, self.params(
                SID=self.sid, RID=self.rid, AID=self.aid, gsessionid=self.gsession))

    async def run(self, ready):
        # No proxies, redirects, cookies, disabled TLS verification or raw URL logs.
        async with httpx.AsyncClient(timeout=httpx.Timeout(45, connect=10),
                                     trust_env=False, follow_redirects=False) as self.client:
            tokens = json.loads(await self.request("pairing/get_lounge_token_batch",
                                                   {"screen_ids": self.identity["screen_id"]}))
            self.token = tokens["screens"][0]["loungeToken"]
            info = {"brand": "SAT-IP", "model": "Raspberry Pi 4", "year": 0,
                    "os": "Linux", "osVersion": "1", "chipset": "",
                    "clientName": "TVHTML5", "dialAdditionalDataSupportLevel": "unsupported",
                    "mdxDialServerType": "MDX_DIAL_SERVER_TYPE_UNKNOWN"}
            initial = await self.request("bc/bind", {"count": "0"},
                                         self.params(RID=self.rid, CVER=1, deviceInfo=json.dumps(info)))
            parser = Frames()
            await self.dispatch(parser.feed(initial))
            if parser.buffer or not self.sid or not self.gsession:
                raise ValueError("YouTube did not establish a receiver session")
            self.ready = True
            ready()
            async with asyncio.TaskGroup() as group:
                group.create_task(self.poll())
                group.create_task(self.send())


def media_url(value):
    if not isinstance(value, str) or len(value) > 16384 or any(ord(c) < 33 for c in value):
        raise ValueError("Invalid video URL")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname
            or not parsed.hostname.endswith(".googlevideo.com")
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError("Unexpected video host")
    return value


def local_media_url(value):
    if not isinstance(value, str):
        raise ValueError("Invalid local media URL")
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.username or parsed.password or not parsed.port
            or parsed.query or parsed.fragment
            or not re.fullmatch(r"/[0-9a-f]{48}/(video|audio)", parsed.path)):
        raise ValueError("Invalid local media URL")
    return value


class MediaProxy:
    """Private ranged MP4 bridge for images whose FFmpeg lacks HTTPS.

    No arbitrary URL endpoint, LAN binding, cookies, proxy environment or
    forwarded client headers. Closing joins every connection before TV resumes.
    """
    def __init__(self):
        self.server = self.client = None
        self.tasks = set()
        self.writers = set()
        self.urls = {}

    async def start(self, video, audio):
        token = secrets.token_hex(24)
        self.urls = {"/" + token + "/video": media_url(video)}
        if audio:
            self.urls["/" + token + "/audio"] = media_url(audio)
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10),
                                       trust_env=False, follow_redirects=False)
        try:
            self.server = await asyncio.start_server(self.accept, "127.0.0.1", 0, limit=8192)
            port = self.server.sockets[0].getsockname()[1]
            base = f"http://127.0.0.1:{port}/{token}/"
            return base + "video", base + "audio" if audio else None
        except BaseException:
            await self.close()
            raise

    def accept(self, reader, writer):
        if len(self.tasks) >= 8:
            writer.close()
            return
        task = asyncio.create_task(self.serve(reader, writer))
        self.writers.add(writer)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def serve(self, reader, writer):
        response = None
        started = False
        try:
            async with asyncio.timeout(10):
                request = await reader.readuntil(b"\r\n\r\n")
            if len(request) > 8192:
                raise ValueError("Media request too large")
            lines = request.decode("ascii").split("\r\n")
            method, path, version = lines[0].split(" ")
            if method not in ("GET", "HEAD") or path not in self.urls or version not in ("HTTP/1.0", "HTTP/1.1"):
                raise ValueError("Unknown private media request")
            headers = {}
            for line in lines[1:]:
                if not line:
                    continue
                name, value = line.split(":", 1)
                if name.lower() == "range":
                    value = value.strip()
                    if not re.fullmatch(r"bytes=([0-9]{1,20}-[0-9]{0,20}|-[0-9]{1,20})", value):
                        raise ValueError("Invalid media range")
                    headers["Range"] = value
            url = self.urls[path]
            for _ in range(5):
                upstream = self.client.build_request(method, url, headers=headers)
                # Ignore any upstream Set-Cookie accumulated by the client too.
                upstream.headers.pop("cookie", None)
                response = await self.client.send(upstream, stream=True)
                if response.status_code not in (301, 302, 303, 307, 308):
                    break
                redirect = response.headers.get("location")
                await response.aclose()
                response = None
                # Only absolute HTTPS googlevideo redirects are permitted.
                url = media_url(redirect)
            if response is None or response.status_code not in (200, 206, 416):
                raise ValueError("Media server rejected playback")
            header = f"HTTP/1.0 {response.status_code} Media\r\nConnection: close\r\n"
            for name in ("content-length", "content-range", "accept-ranges", "content-type"):
                value = response.headers.get(name)
                if value and len(value) <= 200 and not any(ord(c) < 32 for c in value):
                    header += name + ": " + value + "\r\n"
            writer.write((header + "\r\n").encode("ascii"))
            await writer.drain()
            started = True
            if method != "HEAD":
                async for chunk in response.aiter_raw():
                    writer.write(chunk)
                    async with asyncio.timeout(30):
                        await writer.drain()
        except (OSError, ValueError, TimeoutError, httpx.HTTPError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            if not started and not writer.is_closing():
                writer.write(b"HTTP/1.0 502 Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        finally:
            if response:
                await response.aclose()
            writer.close()
            try:
                async with asyncio.timeout(2):
                    await writer.wait_closed()
            except (OSError, TimeoutError):
                pass
            self.writers.discard(writer)

    async def close(self):
        server, self.server = self.server, None
        if server:
            server.close()
        for writer in self.writers:
            writer.close()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        self.writers.clear()
        if server:
            await server.wait_closed()
        if self.client:
            await self.client.aclose()
            self.client = None
        self.urls.clear()


async def resolve(video_id):
    if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id):
        raise ValueError("Invalid YouTube video ID")
    environment = dict(os.environ)
    # In-place development resolver; image builds install the same module normally.
    if Path("/opt/satip-youtube/yt_dlp").is_dir():
        environment["PYTHONPATH"] = "/opt/satip-youtube"
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist",
        "--no-cache-dir", "--socket-timeout", "10", "--retries", "0",
        "--extractor-retries", "0", "--skip-download",
        # Full metadata can exceed the bound (captions and dubbed tracks).
        # Ask yt-dlp for selected playback fields, not its entire catalogue.
        "--print", "%(.{url,vcodec,acodec,requested_formats})j",
        "-f", "bestvideo[vcodec^=avc1][height<=720][protocol=https]+bestaudio[acodec^=mp4a][protocol=https]/best[vcodec^=avc1][acodec^=mp4a][height<=720][protocol=https]",
        "https://www.youtube.com/watch?v=" + video_id, env=environment,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        async with asyncio.timeout(45):
            output = bytearray()
            while chunk := await process.stdout.read(65536):
                output.extend(chunk)
                if len(output) > 8 * MAX_FRAME:
                    raise ValueError("Resolver output too large")
            await process.wait()
        if process.returncode:
            raise ValueError("No compatible public YouTube stream; resolver/runtime may need updating")
        data = json.loads(output)
        formats = data.get("requested_formats")
        if formats:
            if len(formats) != 2:
                raise ValueError("Unexpected video formats")
            video, audio = formats
            if not video.get("vcodec", "").startswith("avc1") or not audio.get("acodec", "").startswith("mp4a"):
                raise ValueError("Unsupported video/audio codec")
            return media_url(video["url"]), media_url(audio["url"])
        if (not data.get("vcodec", "").startswith("avc1")
                or not data.get("acodec", "").startswith("mp4a")):
            raise ValueError("Unsupported video/audio codec")
        return media_url(data["url"]), None
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


def number(value, default=0, maximum=86400):
    try:
        value = float(value)
        if math.isfinite(value) and 0 <= value <= maximum:
            return value
    except (TypeError, ValueError):
        pass
    return default


class YouTube:
    def __init__(self, controller):
        self.controller = controller
        self.config = controller.config
        self.incoming = asyncio.Queue(maxsize=32)
        self.lounge = None
        self.active = False
        self.video_id = None
        self.code = None
        self.code_until = 0
        self.state = "disabled"
        self.error = None
        self.resolver = None
        self.queue = []
        self.index = 0
        self.position = 0
        self.deadline = 0
        self.last_position = None
        self.last_progress = 0
        self.loaded = False
        self.shown_code = None
        self.reported = 0
        self.enabled = False
        self.lock = asyncio.Lock()
        self.media = None

    def publish(self):
        atomic_json(runtime_path(self.config.state_dir, "youtube-status.json"),
                    {"boot_id": boot_id(), "updated": time.monotonic(), "state": self.state,
                     "active": self.active, "video_id": self.video_id, "error": self.error,
                     "pairing_code": self.code, "code_until": self.code_until},
                    0o640, durable=False)

    async def identity(self):
        path = self.config.state_dir / "youtube-identity.json"
        try:
            data = read_json(path)
            if (set(data) != {"screen_id", "device_id"}
                    or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", v) for v in data.values())):
                raise ValueError("Invalid private YouTube identity")
            return data
        except FileNotFoundError:
            async with httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=False) as client:
                async with client.stream("GET", BASE + "pairing/generate_screen_id") as response:
                    response.raise_for_status()
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 256:
                            raise ValueError("Invalid screen identity")
            screen = content.decode().strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", screen):
                raise ValueError("Invalid screen identity")
            data = {"screen_id": screen, "device_id": str(uuid.uuid4())}
            atomic_json(path, data, 0o600)
            return data

    async def run(self):
        task = None
        previous_pair = previous_unlink = ""
        # Do not replay a persisted old pairing request after restart.
        try:
            initial = preferences(self.config.management_dir)
            previous_pair, previous_unlink = initial["pair_request"], initial["unlink_request"]
        except (OSError, ValueError):
            pass
        try:
            while True:
                try:
                    prefs = preferences(self.config.management_dir)
                    self.enabled = prefs["enabled"]
                    unlink = prefs["unlink_request"] != previous_unlink
                    previous_unlink = prefs["unlink_request"]
                    if task and (not self.enabled or unlink):
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        task = None
                        self.lounge = None
                        self.discard_commands()
                        self.incoming.put_nowait(("stopVideo", {}))
                    if unlink:
                        (self.config.state_dir / "youtube-identity.json").unlink(missing_ok=True)
                    if not self.enabled:
                        self.state, self.code = "disabled", None
                        self.error = None
                    elif task is None:
                        self.state, self.error = "connecting", None
                        self.lounge = Lounge(await self.identity(), self.incoming)
                        def ready():
                            self.state = "ready"
                        task = asyncio.create_task(self.lounge.run(ready))
                    elif task.done():
                        await asyncio.gather(task, return_exceptions=True)
                        task = None
                        self.lounge = None
                        self.state, self.code = "error", None
                        self.error = "YouTube receiver connection failed; retrying"
                        self.discard_commands()
                        self.incoming.put_nowait(("stopVideo", {}))
                        self.publish()
                        await asyncio.sleep(5)
                    if (self.lounge and self.lounge.ready and prefs["pair_request"] != previous_pair):
                        self.code = await self.lounge.code()
                        self.code_until = time.monotonic() + 300
                        previous_pair = prefs["pair_request"]
                    if time.monotonic() >= self.code_until:
                        self.code = None
                    self.publish()
                except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError):
                    self.state, self.code = "error", None
                    self.error = "Cannot configure or pair YouTube receiver"
                    self.publish()
                await asyncio.sleep(1)
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.enabled = False
            self.state, self.code = "disabled", None
            self.publish()

    def notify(self, name, payload):
        if self.lounge and self.lounge.ready:
            self.lounge.notify(name, payload)

    async def stop(self):
        # Invalidate pending extraction before releasing playback ownership.
        resolver, self.resolver = self.resolver, None
        if resolver:
            resolver.cancel()
            await asyncio.gather(resolver, return_exceptions=True)
        try:
            if self.active:
                await self.controller.player.stop_stream()
                await self.controller.player.ipc.command("set_property", "pause", False)
        finally:
            media, self.media = self.media, None
            if media:
                await media.close()
            self.active, self.loaded, self.video_id = False, False, None
            self.controller.wake.set()
        self.notify("nowPlaying", {})
        self.notify("onStateChange", {"state": -1})

    async def begin(self, video_id, position=0):
        if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id):
            return
        await self.stop()
        self.active, self.loaded, self.video_id = True, False, video_id
        self.position = number(position)
        self.last_progress = time.monotonic()
        self.error = None
        self.controller.generation += 1
        self.controller.loading = self.controller.playing = None
        self.controller.details = self.controller.menu_index = None
        await self.controller.boot_splash.hide(finished=True)
        await self.controller.osd.dismiss()
        await self.controller.player.stop_stream()
        await self.controller.osd.show(self.controller.text("YouTube\nLoading video…"), 10000)
        self.notify("nowPlaying", {"videoId": video_id, "state": 3})
        self.resolver = asyncio.create_task(resolve(video_id))

    async def local_stop(self):
        async with self.lock:
            self.discard_commands()
            await self.stop()

    async def local_toggle_pause(self):
        async with self.lock:
            ipc = self.controller.player.ipc
            if self.active and self.loaded and ipc and not ipc.closed:
                await ipc.command("cycle", "pause")
                # Report the new playback state to linked phones on the next tick.
                self.reported = 0

    def discard_commands(self):
        while not self.incoming.empty():
            self.incoming.get_nowait()

    async def tick(self, external_pause):
        async with self.lock:
            await self._tick(external_pause)

    async def sample_property(self, name):
        ipc = self.controller.player.ipc
        try:
            return await ipc.get(name)
        except IPCError:
            if ipc.closed:
                raise
            # These properties are unavailable while a file opens, not proof
            # of a failed load. Readiness/deadline are checked separately.
            return None

    async def _tick(self, external_pause):
        if external_pause or not self.enabled:
            self.discard_commands()
            if self.active:
                await self.stop()
        # Bound work per tick so a remote cannot starve local controls.
        for _ in range(8):
            if self.incoming.empty():
                break
            name, payload = self.incoming.get_nowait()
            ipc = self.controller.player.ipc
            if name in ("remoteConnected", "loungeStatus", "getNowPlaying", "getPlaylist"):
                self.notify("onAutoplayModeChanged", {"autoplayMode": "UNSUPPORTED"})
                self.notify("onHasPreviousNextChanged", {"hasPrevious": self.index > 0, "hasNext": self.index + 1 < len(self.queue)})
                self.notify("nowPlaying", {"videoId": self.video_id} if self.video_id else {})
            elif name == "stopVideo":
                await self.stop()
            elif not external_pause and self.enabled:
                if name in ("setPlaylist", "setVideo", "updatePlaylist"):
                    ids = str(payload.get("videoIds", payload.get("videoId", ""))).split(",")[:500]
                    if not ids or any(not VIDEO_ID.fullmatch(i) for i in ids):
                        continue
                    self.queue = ids
                    self.index = int(number(payload.get("currentIndex"), maximum=len(ids)-1))
                    if name != "updatePlaylist":
                        selected = payload.get("videoId", ids[self.index])
                        await self.begin(selected, payload.get("currentTime"))
                elif name in ("next", "previous") and self.active:
                    index = self.index + (1 if name == "next" else -1)
                    if 0 <= index < len(self.queue):
                        self.index = index
                        await self.begin(self.queue[index])
                elif name in ("pause", "play") and self.active and self.loaded:
                    await ipc.command("set_property", "pause", name == "pause")
                elif name == "seekTo" and self.active and self.loaded:
                    await ipc.command("seek", number(payload.get("newTime")), "absolute")
                elif name == "setVolume" and self.active:
                    await ipc.command("set_property", "volume", number(payload.get("volume"), maximum=100))
                    await ipc.command("set_property", "mute", payload.get("muted") in (True, "true"))
                elif name == "getVolume":
                    self.notify("onVolumeChanged", {"volume": self.controller.state.data["volume"],
                                                     "muted": self.controller.state.data["mute"]})
        if self.resolver and self.resolver.done():
            task, self.resolver = self.resolver, None
            try:
                video, audio = task.result()
                self.media = MediaProxy()
                video, audio = await self.media.start(video, audio)
                await self.controller.player.load_youtube(video, audio, self.position)
                self.deadline = time.monotonic() + 30
                self.last_progress = time.monotonic()
                self.last_position = None
                self.loaded = True
            except (ValueError, OSError, TimeoutError):
                self.error = "No compatible YouTube stream. Some videos require a newer resolver/JavaScript runtime."
                await self.stop()
        if self.active and self.loaded:
            ipc = self.controller.player.ipc
            position = await self.sample_property("time-pos")
            idle = await self.sample_property("idle-active")
            if position is not None and await self.sample_property("video-codec") and await self.sample_property("audio-codec"):
                self.deadline = 0
                paused = await ipc.get("pause")
                if paused or self.last_position is None or position != self.last_position:
                    self.last_progress = time.monotonic()
                self.last_position = position
                if time.monotonic() - self.reported >= 1:
                    self.reported = time.monotonic()
                    duration = number(await self.sample_property("duration"))
                    payload = {"videoId": self.video_id, "state": 2 if paused else 1,
                               "currentTime": number(position), "duration": duration,
                               "loadedTime": duration, "seekableStartTime": 0, "seekableEndTime": duration}
                    self.notify("nowPlaying", payload)
                    self.notify("onStateChange", payload)
                    self.controller.state.update(volume=await ipc.get("volume"), mute=await ipc.get("mute"))
            elif (self.deadline and time.monotonic() > self.deadline) or ipc.load_failure:
                self.error = "YouTube video/audio could not start; returning to TV"
                await self.stop()
            elif idle and not self.deadline:
                if self.index + 1 < len(self.queue):
                    self.index += 1
                    await self.begin(self.queue[self.index])
                else:
                    await self.stop()
            if self.active and time.monotonic() - self.last_progress > max(30, self.config.stall_timeout):
                self.error = "YouTube playback stalled; returning to TV"
                await self.stop()
        if self.code != self.shown_code:
            self.shown_code = self.code
            if self.code and not external_pause:
                pretty = " ".join(self.code[i:i+3] for i in range(0, 12, 3))
                await self.controller.osd.show(self.controller.text(
                    "YouTube · Link with TV code\n{code}\nYouTube on iPhone → Settings → Watch on TV",
                    code=pretty), 30000)
        self.publish()
