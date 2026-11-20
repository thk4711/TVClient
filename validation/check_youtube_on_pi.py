"""Disruptive live Lounge/HDMI check; does not prove iPhone app compatibility."""
import argparse
import asyncio
import logging
from pathlib import Path
import uuid

from satip_controller.config import Config
from satip_controller.mpv_ipc import IPCError, MPVIPC
from satip_controller.state import read_json, runtime_path
from satip_controller.youtube import Lounge, VIDEO_ID, public_status


class Sender(Lounge):
    def params(self, **extra):
        params = super().params(**extra)
        params.update(device="REMOTE_CONTROL", app="youtube-desktop", name="SAT-IP validation sender")
        return params


async def check(config, video_id):
    status = public_status(config.state_dir)
    player_status = runtime_path(config.state_dir, "status.json")
    player = read_json(player_status)
    if status["state"] != "ready" or status["active"] or player.get("paused"):
        raise ValueError("Enable the receiver first; no YouTube, scan or preview may be active")
    identity = read_json(config.state_dir / "youtube-identity.json")
    identity["device_id"] = str(uuid.uuid4())
    sender = Sender(identity, asyncio.Queue(maxsize=32))
    ready = asyncio.Event()
    task = asyncio.create_task(sender.run(ready.set))
    async def drain_messages():
        while True:
            await sender.incoming.get()
    drain = asyncio.create_task(drain_messages())
    ipc = None
    async def property(name):
        try:
            return await ipc.get(name)
        except IPCError:
            if ipc.closed:
                raise
            return None
    try:
        await asyncio.wait_for(ready.wait(), 20)
        print("Synthetic phone control session connected", flush=True)
        sender.notify("setPlaylist", {"videoIds": video_id, "videoId": video_id,
                                      "currentIndex": 0, "currentTime": 0})
        previous = None
        started = False
        async with asyncio.timeout(80):
            while True:
                status = public_status(config.state_dir)
                if started and status.get("error"):
                    raise ValueError(status["error"])
                if status.get("active"):
                    started = True
                    if ipc is None:
                        ipc = await MPVIPC.connect(config.runtime_dir / "mpv.sock")
                    position = await property("time-pos")
                    video, audio = await property("video-codec"), await property("audio-codec")
                    if position is not None and video and audio:
                        if previous is not None and position > previous + .1:
                            print("HDMI playback confirmed:", video, audio, "time:", round(position, 2), flush=True)
                            assert await property("http-header-fields") == [], "TV Authorization header was not cleared"
                            print("External playback has no TV Authorization header", flush=True)
                            break
                        previous = position
                if task.done():
                    raise ValueError("Synthetic sender session ended")
                await asyncio.sleep(1)
        sender.notify("pause", {})
        async with asyncio.timeout(15):
            while await property("pause") is not True:
                if task.done():
                    raise ValueError("Synthetic sender session ended")
                await asyncio.sleep(.25)
        assert await property("pause") is True, "Phone pause was not applied"
        print("Phone pause applied", flush=True)
        sender.notify("play", {})
        async with asyncio.timeout(15):
            while await property("pause") is not False:
                await asyncio.sleep(.25)
        assert await property("pause") is False, "Phone resume was not applied"
        print("Phone resume applied", flush=True)
        sender.notify("seekTo", {"newTime": 30})
        async with asyncio.timeout(15):
            while (await property("time-pos") or 0) < 29:
                await asyncio.sleep(.25)
        print("Phone seek applied", flush=True)
    finally:
        if sender.ready and not task.done():
            sender.notify("stopVideo", {})
            await asyncio.sleep(3)
        task.cancel()
        drain.cancel()
        await asyncio.gather(task, drain, return_exceptions=True)
        if ipc:
            await ipc.close()
    async with asyncio.timeout(20):
        while True:
            player = read_json(player_status)
            if player.get("playing") and not player.get("paused"):
                ipc = await MPVIPC.connect(config.runtime_dir / "mpv.sock")
                try:
                    assert any(header.startswith("Authorization: Basic ") for header in
                               (await ipc.get("http-header-fields") or [])), "TV credentials were not restored"
                    assert await ipc.get("audio-files") == [], "YouTube audio was not released"
                finally:
                    await ipc.close()
                print("TV resumed:", player["playing"], flush=True)
                return
            await asyncio.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/satip-player/config.json")
    parser.add_argument("--video", default="aqz-KE-bpKQ")
    parser.add_argument("--confirm-interrupt", action="store_true")
    args = parser.parse_args()
    if not args.confirm_interrupt or not VIDEO_ID.fullmatch(args.video):
        parser.error("Provide --confirm-interrupt and a valid public YouTube video ID")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(check(Config.load(Path(args.config)), args.video))
    except (OSError, ValueError, TimeoutError, IPCError, AssertionError) as error:
        # Do not print raw HTTP errors: request URLs contain private tokens.
        print("YouTube check failed:", type(error).__name__, flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
