import asyncio
import contextlib
import logging
import os
import time

from .channels import step
from .coordination import preview_held, process_identity
from .config import library
from .epg import programme_banner, programme_parts, channel_events
from .settings import settings_path, validate_settings, DEFAULT_LANGUAGE
from .i18n import tr
from .mpv_ipc import IPCError
from .splash import BootSplash
from .osd import (OSD, BODY_ROWS, MENU_TEXT_WIDTH, FAVOURITES_ROWS, font_advance, banner,
                  clock_label, details_panel, favourites_panel, wrap)
from .state import atomic_json, read_json, runtime_path, lease_active, JSONCache
from .youtube import YouTube
from .switching import SwitchExperiments

LOG = logging.getLogger(__name__)
STARTUP_RETRY_WINDOW = 60
STARTUP_RETRY_CAP = 3
RECEPTION_FAILURES = {"load_failed", "load_timeout"}
PLAYBACK_HINTS = {
    "load_failed": "TV stream rejected; check Tvheadend access and tuner availability",
    "load_timeout": "No playable stream; check Tvheadend and SAT-IP reception",
    "playback_stalled": "Picture/audio stalled; check network and SAT-IP reception",
    "audio_pid_missing": "Saved audio track missing; check channel configuration",
}


class Controller:
    def __init__(self, config, player, state):
        self.config, self.player, self.state = config, player, state
        self.channels, self.favourites = library(read_json(config.library))
        self.requested = state.data["channel"]
        self.loading = self.playing = None
        self.generation = 0
        self.wake = asyncio.Event()
        self.osd = OSD(player, config.osd_ms)
        self.language = DEFAULT_LANGUAGE
        self.banner_ms = 5000
        self.menu_checked = 0
        self.boot_splash = BootSplash(player)
        self.menu_index = None
        self.error = None
        self.reason = None
        self.retry_count = 0
        self.switch_seconds = None
        self.health = {}
        self.health_checked = 0
        self.file_cache = JSONCache()
        self.owner = process_identity(os.getpid())
        self.paused = False
        self.last_status = None
        self.input_lock = asyncio.Lock()
        self.takeover_lock = asyncio.Lock()
        self.epg_window = None
        self.details = None
        self.requested_at = time.monotonic()
        self.request_burst = False
        self.request_committed = False
        self.startup_began = self.requested_at
        self.first_playback = False
        self.youtube = YouTube(self)
        self.seek_task = None
        self.seek_target = None
        self.seek_requested = None
        self.seek_action = None

    def text(self, message, **values):
        return tr(message, self.language, **values)

    def request(self, channel, force=False, committed=False):
        if channel not in self.favourites:
            raise ValueError("Channel is not a favourite")
        if (not force and channel == self.requested == self.playing and not self.paused
                and not self.error and self.player.ipc and not self.player.ipc.closed):
            return False
        self.requested = channel
        now = time.monotonic()
        self.request_burst = now - self.requested_at <= .25
        self.request_committed = committed
        self.requested_at = now
        self.epg_window = None
        if self.details is not None:
            self.details = None
            self.osd.overlay = None
        self.generation += 1
        self.clear_pending_seek()
        self.wake.set()
        return True

    async def coalesce_requests(self):
        experiment = SwitchExperiments.load(self.config.runtime_dir)
        while True:
            generation = self.generation
            age = time.monotonic() - self.requested_at
            # Preserve the *full* existing key-zapping wait, including the
            # input/OSD work before this point. Only a request already waiting
            # through a tune (>250 ms) has paid that grace period.
            delay = (.08 if not self.request_committed and age < .25 else
                     experiment.coalescing_delay(age, self.request_burst or not self.request_committed))
            await asyncio.sleep(delay)
            # A second press during the isolated grace period is a burst, not
            # permission to launch its tune after only the first tap's 30 ms.
            if not experiment.adaptive_coalescing or generation == self.generation:
                return

    async def local_press(self, action=None):
        """Ask the stream owner to stop; never revoke its lease before cleanup."""
        async with self.takeover_lock:
            if self.youtube.active and action not in ("volume_up", "volume_down", "remote_left", "remote_right", "mute", "pause"):
                await self.youtube.local_stop()
            path = runtime_path(self.config.management_dir, "preview-lease.json")
            try:
                lease = read_json(path)
                generation = lease.get("generation")
                if not preview_held(lease) or not generation:
                    return True
                atomic_json(runtime_path(self.config.state_dir, "local-takeover.json"),
                            {"generation": generation}, 0o640)
                deadline = time.monotonic() + 20
                while preview_held(read_json(path)):
                    if time.monotonic() >= deadline:
                        return False
                    await asyncio.sleep(.1)
                await self.sync_management()
                return True
            except FileNotFoundError:
                return True
            except (OSError, ValueError, AttributeError):
                LOG.warning("Local streaming handover unavailable")
                return False

    def clear_pending_seek(self):
        # Let a reader handover finish safely, but discard obsolete follow-ups.
        self.seek_target = self.seek_requested = None

    def queue_seek(self, action):
        ts = getattr(self.player, "timeshift", None)
        if (self.youtube.active or not ts or not ts.active
                or not ts.ranges or not self.playing or self.playing != self.requested or self.paused):
            return False
        multiplier = 1
        if action in ("remote_left", "remote_right", "remote_left_fast", "remote_right_fast"):
            if not ts.paused:
                return False
            multiplier = 3 if action.endswith("_fast") else 1
            action = "skip_back" if action.startswith("remote_left") else "skip_forward"
        delta = {"skip_back": -10, "skip_forward": 10, "seek_back": -30, "seek_forward": 30}.get(action)
        if delta is None:
            return False
        base = ts.position if self.seek_requested is None else self.seek_requested
        if base is None:
            return False
        target = max(ts.ranges[0][0], min(ts.ranges[-1][1] - .5, base + delta * multiplier))
        # Repeating against a retained-history boundary is a consumed no-op,
        # not another expensive reload of exactly the same frame.
        if target == base:
            return True
        self.seek_target = self.seek_requested = target
        self.seek_action = action
        if self.seek_task is None or self.seek_task.done():
            self.seek_task = asyncio.create_task(self.run_seeks())
        return True

    async def run_seeks(self):
        generation = self.generation
        try:
            while self.seek_target is not None:
                # Read input continuously; keep one latest cumulative target,
                # not a queue of expensive reloads. Holds still make progress.
                await asyncio.sleep(.2)
                async with self.input_lock:
                    if generation != self.generation or self.seek_target is None or self.paused:
                        break
                    target, action = self.seek_target, self.seek_action
                    self.seek_target = None
                    ts = self.player.timeshift
                    preview = ts.status()
                    preview["behind_live_seconds"] = round(max(0, ts.ranges[-1][1] - target), 1)
                    await self.osd.timeshift(preview, reveal=True)
                    started = time.monotonic()
                    await self.player.timeshift_action(action, target=target)
                    LOG.info("Time-shift seek completed in %.3fs (pending=%s)",
                             time.monotonic() - started, self.seek_target is not None)
                    await self.osd.timeshift(ts.status(), reveal=True)
        except (IPCError, OSError) as error:
            LOG.warning("Time-shift seek failed (%s)", getattr(error, "reason", "storage"))
            self.wake.set()
        finally:
            self.clear_pending_seek()

    async def action(self, action):
        if self.queue_seek(action):
            return
        self.clear_pending_seek()
        async with self.input_lock:
            try:
                if action in ("remote_left", "remote_right", "remote_left_fast", "remote_right_fast"):
                    timeshift = getattr(self.player, "timeshift", None)
                    if (not self.youtube.active and timeshift and timeshift.config and timeshift.paused
                            and self.playing == self.requested and self.playing and not self.paused):
                        action = "skip_back" if action.startswith("remote_left") else "skip_forward"
                    else:
                        action = "volume_down" if action.startswith("remote_left") else "volume_up"
                timeshift = getattr(self.player, "timeshift", None)
                # Apple Menu emits KEY_INFO, not KEY_BACK.
                if (action == "info" and self.menu_index is None and self.details is None
                        and not self.youtube.active and timeshift and timeshift.status()["enabled"]):
                    action = "live"
                if action in ("mute", "pause") and self.youtube.active:
                    await self.youtube.local_toggle_pause()
                elif action in ("pause", "seek_back", "seek_forward", "skip_back", "skip_forward", "live"):
                    if self.playing and self.playing == self.requested and not self.paused:
                        text = await self.player.timeshift_action(action)
                        timeshift = getattr(self.player, "timeshift", None)
                        if timeshift and timeshift.config:
                            await self.osd.timeshift(timeshift.status(), reveal=True)
                            if not timeshift.status()["enabled"]:
                                await self.osd.show(self.text(text), 3000)
                        else:
                            await self.osd.show(self.text(text), 3000)
                elif action == "menu":
                    if self.details is not None:
                        await self.close_details()
                    if self.menu_index is None:
                        self.menu_index = self.favourites.index(self.requested) if self.requested in self.favourites else 0
                        await self.draw_menu()
                    else:
                        chosen = self.favourites[self.menu_index]
                        self.menu_index = None
                        await self.osd.dismiss()
                        if not self.request(chosen, committed=True):
                            await self.show_programme(self.channels[chosen])
                elif action == "back":
                    timeshift = getattr(self.player, "timeshift", None)
                    if (self.menu_index is None and self.details is None and timeshift
                            and timeshift.status()["enabled"] and not self.youtube.active):
                        await self.player.timeshift_action("live")
                        await self.osd.timeshift(timeshift.status(), reveal=True)
                    else:
                        self.menu_index = None
                        self.details = None
                        await self.osd.dismiss()
                elif action in ("next", "previous"):
                    delta = 1 if action == "next" else -1
                    if self.details is not None:
                        # Up/next scrolls towards the start; Down/previous towards the end.
                        self.details["offset"] -= delta
                        await self.draw_details(force=True)
                    elif self.menu_index is not None:
                        # Up/next moves up the list; Down/previous moves down.
                        self.menu_index = (self.menu_index - delta) % len(self.favourites)
                        await self.draw_menu()
                    else:
                        if self.request(step(self.favourites, self.requested, delta)):
                            await self.show_switching(self.channels[self.requested])
                        else:
                            await self.show_programme(self.channels[self.requested])
                elif action in ("volume_up", "volume_down", "mute"):
                    ipc = self.player.ipc
                    if ipc and not ipc.closed:
                        if action == "mute":
                            await ipc.command("cycle", "mute")
                        else:
                            value = await ipc.get("volume")
                            value = max(0, min(100, value + (5 if action == "volume_up" else -5)))
                            await ipc.command("set_property", "volume", value)
                        self.state.update(volume=await ipc.get("volume"), mute=await ipc.get("mute"))
                        if self.youtube.active:
                            self.youtube.notify("onVolumeChanged", {
                                "volume": self.state.data["volume"], "muted": self.state.data["mute"],
                            })
                        await self.osd.volume(self.state.data["volume"], self.state.data["mute"])
                elif action == "info":
                    if self.details is not None:
                        await self.close_details()
                    else:
                        self.menu_index = None
                        await self.osd.dismiss()
                        if not self.paused:
                            self.epg_window = None
                            self.details = {"channel": self.requested, "generation": self.generation,
                                            "event": None, "offset": 0, "text": None,
                                            "checked": 0, "drawn": 0}
                            await self.draw_details(force=True)
            except (IPCError, OSError):
                self.wake.set()

    async def draw_menu(self):
        # Page the list: readable even with hundreds of favourites.
        self.menu_index %= len(self.favourites)
        start = (self.menu_index // FAVOURITES_ROWS) * FAVOURITES_ROWS
        rows = []
        now = time.time()
        for i in range(start, min(start + FAVOURITES_ROWS, len(self.favourites))):
            uuid = self.favourites[i]
            channel = self.channels[uuid]
            current = next((event for event in channel_events(channel, self.config.management_dir, now)
                            if event["start"] <= now < event["stop"]), None)
            rows.append((i, uuid, channel.name, current["title"] if current else ""))
        data = favourites_panel(rows, self.menu_index, start // FAVOURITES_ROWS + 1,
                                (len(self.favourites) + FAVOURITES_ROWS - 1) // FAVOURITES_ROWS, self.language)
        if self.osd.overlay != data:
            await self.osd.menu(self.text("Channels"), data=data)

    async def close_details(self):
        self.details = None
        await self.osd.dismiss()

    async def draw_details(self, force=False):
        details = self.details
        if details is None:
            return
        if (self.paused or details["channel"] != self.requested
                or details["generation"] != self.generation):
            await self.close_details()
            return
        now = time.monotonic()
        if not force and now-details["checked"] < 1:
            return
        details["checked"] = now
        channel = self.channels[details["channel"]]
        if self.playing == channel.uuid:
            event, parts = programme_parts(channel, self.config.management_dir, language=self.language)
        else:
            event, parts = None, [self.text("Loading channel — programme information will follow")]
        if event is None:
            parts = [self.text("Programme information"), "", parts[0]]
        body = parts[2:]
        if font_advance(parts[0], 42, True) > MENU_TEXT_WIDTH:
            body = [parts[0], *body]  # Even unusually long titles remain accessible.
        lines = wrap("\n\n".join(body))
        if event != details["event"]:
            details["event"], details["offset"] = event, 0
        size = BODY_ROWS
        offset = max(0, min(details["offset"], max(0, len(lines)-size)))
        details["offset"] = offset
        name = channel.name + (" · " + self.text("Live programme information") if self.live_information() else "")
        text = details_panel(name, parts, lines, offset, self.language)
        if force or text != details["text"] or now-details["drawn"] >= 60:
            await self.osd.menu(self.text("Programme information"), data=text)
            details["text"], details["drawn"] = text, now

    def channel_number(self, channel):
        return self.favourites.index(channel.uuid) + 1 if channel.uuid in self.favourites else None

    def startup_retrying(self, now=None):
        now = time.monotonic() if now is None else now
        return (not self.first_playback and self.reason in RECEPTION_FAILURES
                and 0 <= now - self.startup_began < STARTUP_RETRY_WINDOW)

    def retry_delay(self, delay, now=None):
        return min(delay, STARTUP_RETRY_CAP) if self.startup_retrying(now) else delay

    async def show_switching(self, channel):
        await self.osd.timeshift({"enabled": False})
        if not self.boot_splash.enabled and self.boot_splash.shown:
            await self.boot_splash.hide()
        waiting = self.startup_retrying()
        if self.boot_splash.enabled:
            # Keep the same waiting artwork across retry and load attempts.
            if waiting:
                await self.boot_splash.show(27)
            elif self.reason and not self.first_playback:
                await self.boot_splash.show(11, self.reason)
            else:
                await self.boot_splash.show()
            if self.boot_splash.shown:
                return self.osd.sequence
        if waiting:
            return await self.osd.show(self.text("Waiting for SAT-IP tuner …"),
                                       (self.config.load_timeout + STARTUP_RETRY_CAP + 2) * 1000)
        text = channel.name + "\n" + self.text("Loading channel…")
        return await self.osd.show(text, data=banner(text, number=self.channel_number(channel), language=self.language))

    def live_information(self):
        timeshift = getattr(self.player, "timeshift", None)
        return bool(timeshift and timeshift.status()["enabled"]
                    and (timeshift.paused or timeshift.status()["behind_live_seconds"] > 5))

    def label_programme(self, text):
        if self.live_information():
            lines = text.splitlines()
            if lines:
                lines[0] += " · " + self.text("Live programme information")
            return "\n".join(lines)
        return text

    async def show_programme(self, channel, expected=None):
        text, progress = programme_banner(channel, self.config.management_dir, language=self.language)
        text = self.label_programme(text)
        clock = clock_label(language=self.language)
        if hasattr(self.player, "refresh_audio_info"):
            with contextlib.suppress(IPCError):
                await self.player.refresh_audio_info()
        audio = getattr(self.player, "audio_label", lambda: "")()
        end = time.monotonic()+self.banner_ms/1000
        ticket = await self.osd.show(text, self.banner_ms, expected=expected,
                                     data=banner(text, progress, self.channel_number(channel), clock, audio=audio))
        self.epg_window = (self.generation, channel, end, ticket, (text, clock, audio)) if ticket is not None else None

    async def refresh_programme(self):
        if self.epg_window is None:
            return
        generation, channel, end, ticket, previous = self.epg_window
        remaining = end-time.monotonic()
        if (remaining <= 0 or generation != self.generation or self.paused
                or self.playing != channel.uuid or self.osd.sequence != ticket
                or self.osd.overlay is not None):
            self.epg_window = None
            return
        text, progress = programme_banner(channel, self.config.management_dir, language=self.language)
        text = self.label_programme(text)
        clock = clock_label(language=self.language)
        audio = getattr(self.player, "audio_label", lambda: "")()
        if (text, clock, audio) != previous:
            # Late EIT data can fill the banner, but must not restart its timer.
            ticket = await self.osd.show(text, max(1, int(remaining*1000)), expected=ticket,
                                         data=banner(text, progress, self.channel_number(channel), clock, audio=audio))
            self.epg_window = (generation, channel, end, ticket, (text, clock, audio)) if ticket is not None else None

    async def sync_management(self):
        previous_language = self.language
        try:
            settings = validate_settings(self.file_cache.read(settings_path(self.config.management_dir)))
            self.osd.duration = self.banner_ms = settings["osd_timeout_ms"]
            self.language = settings["osd_language"]
        except FileNotFoundError:
            self.osd.duration, self.banner_ms = self.config.osd_ms, 5000
            self.language = DEFAULT_LANGUAGE
        except (OSError, ValueError, TypeError):
            LOG.warning("Ignoring invalid OSD settings; retaining last valid preferences")
        self.osd.language = self.language
        await self.boot_splash.set_language(self.language)
        if self.language != previous_language:
            async with self.input_lock:
                if self.details is not None:
                    await self.draw_details(force=True)
                elif self.menu_index is not None:
                    await self.draw_menu()
        try:
            channels, favourites = library(self.file_cache.read(self.config.library))
            if channels != self.channels or favourites != self.favourites:
                changed_policy = channels.get(self.requested) != self.channels.get(self.requested)
                self.channels, self.favourites = channels, favourites
                if self.requested not in favourites:
                    self.request(favourites[0])
                elif changed_policy:
                    self.request(self.requested, force=True)
                if self.menu_index is not None:
                    await self.draw_menu()
        except (OSError, ValueError, TypeError, KeyError):
            LOG.warning("Ignoring invalid management library; retaining last valid favourites")
        try:
            lease = read_json(runtime_path(self.config.management_dir, "scan-lease.json"))
            paused = lease.get("recovery_pending") is True or lease_active(lease)
        except (OSError, ValueError, AttributeError):
            paused = False
        previewing = False
        try:
            lease = read_json(runtime_path(self.config.management_dir, "preview-lease.json"))
            previewing = preview_held(lease)
        except (OSError, ValueError, AttributeError):
            pass
        try:
            paused = paused or read_json(self.config.management_dir / "recovery.json").get("pending") is True
        except (OSError, ValueError, AttributeError):
            pass
        paused = paused or previewing
        await self.youtube.tick(paused)
        youtube_active = self.youtube.active
        paused = paused or youtube_active
        if paused != self.paused:
            self.paused = paused
            self.generation += 1
            self.clear_pending_seek()
            if paused and not youtube_active:
                await self.boot_splash.hide()
                self.details = None
                self.menu_index = None
                await self.osd.dismiss()
                async with self.input_lock:
                    await self.player.stop_stream()
                self.loading = self.playing = None
                await self.osd.show(self.text("TV playback paused\nWeb preview running" if previewing else
                                              "TV playback paused\nChannel search running"), 10000)
            self.wake.set()
        status = {"version": 1, "owner_pid": os.getpid(), "requested": self.requested, "loading": self.loading,
                  "playing": self.playing, "paused": self.paused, "error": self.error,
                  "owner": self.owner, "reason": self.reason, "retry_count": self.retry_count,
                  "switch_seconds": self.switch_seconds, "health": self.health,
                  "state_error": self.state.error}
        if hasattr(self.player, "timeshift"):
            status["timeshift"] = self.player.timeshift.status()
        if youtube_active:
            status["source"] = "youtube"
        if status != self.last_status:
            atomic_json(runtime_path(self.config.state_dir, "status.json"), status, 0o640, durable=False)
            self.last_status = status
        await self.refresh_programme()
        async with self.input_lock:
            await self.draw_details()
            if self.menu_index is not None and time.monotonic()-self.menu_checked >= 1:
                self.menu_checked = time.monotonic()
                await self.draw_menu()

    async def run(self):
        try:
            self.language = validate_settings(read_json(settings_path(self.config.management_dir)))["osd_language"]
        except (OSError, ValueError, TypeError):
            pass
        self.osd.language = self.boot_splash.language = self.language
        from .recovery import Progress
        progress = Progress()
        delay = 1
        youtube_task = asyncio.create_task(self.youtube.run())
        try:
            while True:
                try:
                    if not self.player.ipc or self.player.ipc.closed:
                        await self.player.start()
                        await self.osd.reconnect()
                        await self.boot_splash.show(27 if self.startup_retrying() else 35)
                        await self.player.show_window()
                    await self.sync_management()
                    progress.pulse()
                    if self.paused:
                        await asyncio.sleep(.25)
                        continue
                    # Coalesce bursts before any tuning request; no obsolete queue.
                    await self.coalesce_requests()
                    generation, channel = self.generation, self.channels[self.requested]
                    self.wake.clear()
                    self.loading, self.playing = channel.uuid, None
                    self.error = None
                    osd_ticket = await self.show_switching(channel)
                    async with self.input_lock:
                        if generation != self.generation or self.paused:
                            continue
                        await self.player.load(channel)
                    previous = None
                    last_advance = time.monotonic()
                    deadline = last_advance + self.config.load_timeout
                    audio_selected = False
                    # Do not abort a tune while SAT-IP SETUP is in flight. Some
                    # servers reject subsequent SETUPs after an early teardown.
                    # Input still updates requested; finish this bounded load,
                    # then tune only the latest selection without saving stale TV.
                    while not self.paused:
                        await self.sync_management()
                        progress.pulse()
                        if self.paused or (self.playing and generation != self.generation):
                            break
                        now = time.monotonic()
                        timeshift = getattr(self.player, "timeshift", None)
                        if timeshift and self.playing and now - self.health_checked >= 1:
                            async with self.input_lock:
                                reset = await timeshift.update(self.player.ipc, self.player.process.pid)
                                await self.osd.timeshift(timeshift.status())
                                if timeshift.notice and not reset:
                                    await self.osd.show(self.text(timeshift.notice), 6000)
                                    timeshift.notice = ""
                            if reset and generation == self.generation and not self.paused:
                                LOG.info("Time-shift reload (%s): %s", reset, timeshift.notice)
                                if reset != "ready" and hasattr(self.player, "diagnostics"):
                                    LOG.warning("Time-shift recovery diagnostics: %s", await self.player.diagnostics())
                                self.request(channel.uuid, force=True)
                                break
                        if timeshift and (timeshift.paused or now < timeshift.seek_until):
                            last_advance = now
                        try:
                            if not audio_selected or (self.playing and now-self.health_checked >= 1):
                                audio_selected = await self.player.select_audio(channel)
                            position = await self.player.sample(channel)
                        except IPCError as failure:
                            if self.player.ipc.closed or failure.reason == "load_failed":
                                raise
                            position = None
                        if self.paused or (self.playing and generation != self.generation):
                            break
                        if position is not None:
                            if not self.playing and generation == self.generation and self.boot_splash.enabled:
                                await self.boot_splash.show(36)
                            if previous is not None and position > previous + .01:
                                last_advance = now
                                if not self.playing and audio_selected:
                                    if generation != self.generation:
                                        break
                                    if self.boot_splash.enabled:
                                        LOG.info("First playback confirmed (%.3f s since Linux start)", now)
                                    await self.boot_splash.complete()
                                    if generation != self.generation or self.paused:
                                        break
                                    self.loading, self.playing = None, channel.uuid
                                    self.first_playback = True
                                    delay = 1
                                    self.reason = None
                                    self.retry_count = 0
                                    self.switch_seconds = round(now-self.requested_at, 3)
                                    self.state.update(channel=channel.uuid)
                                    if timeshift and timeshift.notice:
                                        await self.osd.show(self.text(timeshift.notice), 6000)
                                        timeshift.notice = ""
                                    elif self.osd.sequence == osd_ticket:
                                        await self.show_programme(channel, expected=osd_ticket)
                                    LOG.info("Playing channel %s (request to confirmed playback %.3fs)",
                                             channel.uuid, now-self.requested_at)
                            previous = position
                        if not self.playing and now > deadline:
                            raise IPCError("Channel load timed out", reason="audio_pid_missing" if position is not None and not audio_selected else "load_timeout")
                        if self.playing and now-last_advance > self.config.stall_timeout:
                            if hasattr(self.player, "diagnostics"):
                                LOG.warning("Playback stall diagnostics: %s", await self.player.diagnostics())
                            raise IPCError("Playback stopped advancing", reason="playback_stalled")
                        if self.playing:
                            if now-self.health_checked >= 1 and hasattr(self.player, "diagnostics"):
                                self.health = await self.player.diagnostics()
                                if hasattr(self.player, "refresh_audio_info"):
                                    with contextlib.suppress(IPCError):
                                        await self.player.refresh_audio_info()
                                self.health_checked = now
                            self.state.update(volume=await self.player.ipc.get("volume"),
                                              mute=await self.player.ipc.get("mute"))
                        try:
                            # An obsolete load must keep sampling, not spin on
                            # the pending request's already-set wake event.
                            self.wake.clear()
                            await asyncio.wait_for(self.wake.wait(), .25 if self.playing else .1)
                        except asyncio.TimeoutError:
                            pass
                except (IPCError, OSError, asyncio.TimeoutError) as failure:
                    with contextlib.suppress(IPCError, OSError):
                        await self.youtube.stop()
                    self.reason = failure.reason if isinstance(failure, IPCError) else "device_or_storage_unavailable"
                    self.retry_count += 1
                    if hasattr(self.player, "recover"):
                        self.player.recover(self.reason)
                    starting = self.startup_retrying()
                    delay = self.retry_delay(delay)
                    self.error = None if starting and self.boot_splash.enabled else {
                        "load_failed": "TV stream was rejected or could not be opened. Check Tvheadend's player access and tuner availability. Retrying…",
                        "load_timeout": "No playable TV stream within the load deadline. Check Tvheadend and SAT-IP reception. Retrying…",
                        "playback_stalled": "TV audio/video stopped advancing. Check network and SAT-IP reception. Retrying…",
                        "audio_pid_missing": "The configured audio track is missing. Check the channel configuration. Retrying…",
                    }.get(self.reason, "TV player unavailable. Check the player log, HDMI/audio devices and storage. Retrying…")
                    self.loading = self.playing = None
                    LOG.warning("Playback unavailable (%s); retry %s in %s seconds", self.reason, self.retry_count, delay)
                    with contextlib.suppress(IPCError, OSError, asyncio.TimeoutError):
                        self.details = None
                        self.menu_index = None
                        await self.osd.dismiss()
                        await self.player.stop_stream()
                        await self.boot_splash.show(27 if starting else 11, "" if starting else self.reason)
                        if not self.boot_splash.shown:
                            await self.osd.show(self.text("Waiting for SAT-IP tuner …") if starting else
                                                self.text("TV playback unavailable") + "\n" +
                                                self.text(PLAYBACK_HINTS.get(self.reason, "Check player, HDMI/audio and storage")) +
                                                "\n" + self.text("Diagnostics: /run/satip-supervisor/player.log"), max(3000, delay*1000))
                    if self.player.ipc and self.player.ipc.closed:
                        await self.player.close()
                    # Wake early for a changed channel or scan; keep input responsive.
                    self.wake.clear()
                    end = time.monotonic() + delay
                    while time.monotonic() < end and not self.wake.is_set():
                        await self.sync_management()
                        progress.pulse()
                        await asyncio.sleep(.25)
                    delay = min(delay*2, 30)
        finally:
            if self.seek_task:
                self.seek_task.cancel()
                await asyncio.gather(self.seek_task, return_exceptions=True)
            youtube_task.cancel()
            await asyncio.gather(youtube_task, return_exceptions=True)
            with contextlib.suppress(IPCError, OSError):
                await self.youtube.stop()
            await self.boot_splash.hide(finished=True)
            await self.osd.close()
            await self.player.close()
            await self.state.close()
            atomic_json(runtime_path(self.config.state_dir, "status.json"), {"version": 1,
                "owner_pid": None, "requested": self.requested, "loading": None, "playing": None,
                "paused": False, "error": "Player stopped", "owner": None}, 0o640, durable=False)
