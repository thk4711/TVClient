"""Broadcast programme cache: backend access stays in the management service."""
import asyncio
import math
import time
import textwrap
from datetime import datetime
from pathlib import Path

from .backend import BackendError
from .i18n import tr
from .config import library
from .state import atomic_json, read_json, runtime_path, JSONCache


_CACHE = JSONCache()


def epg_text(value):
    if not isinstance(value, str):
        return ""
    # Retain paragraphs, but never pass control codes to the OSD.
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = "".join(c for c in value if c == "\n" or ord(c) >= 32)
    value = "\n".join(" ".join(line.split()) for line in value.splitlines()).strip()
    if len(value) > 65536:
        value = value[:65536] + "…"
    return value


def events(rows):
    result = []
    if not isinstance(rows, list):
        return result
    for row in rows:
        if not isinstance(row, dict):
            continue
        start, stop, title = row.get("start"), row.get("stop"), row.get("title")
        if (type(start) not in (int, float) or type(stop) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(stop)
                or not 0 < start < stop <= 4102444800 or stop-start > 7*86400
                or not isinstance(title, str)):
            continue
        title = " ".join(epg_text(title).split())
        if title:
            result.append({"title": title, "start": start, "stop": stop,
                           **{key: epg_text(row.get(key)) for key in ("subtitle", "summary", "description")}})
    return sorted(result, key=lambda item: item["start"])[:8]


def remaining(stop, now, language="en"):
    minutes = max(1, math.ceil((stop-now)/60))
    hours, minutes = divmod(minutes, 60)
    hour_text = tr("{hours} hr", language, hours=hours)
    minute_text = tr("{minutes} min", language, minutes=minutes)
    return (hour_text + (" " + minute_text if minutes else "")) if hours else minute_text


def channel_events(channel, directory, now):
    rows = []
    try:
        cache = _CACHE.read(runtime_path(Path(directory), "epg.json"))
        item = cache["channels"][channel.uuid]
        if cache["version"] == 1 and 0 <= now-item["fetched"] <= 300:
            rows = events(item["events"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return rows


def programme_banner(channel, directory, now=None, language="en"):
    now = time.time() if now is None else now
    rows = channel_events(channel, directory, now)
    current = next((e for e in rows if e["start"] <= now < e["stop"]), None)
    following = next((e for e in rows if e["start"] >= (current["stop"] if current else now)), None)
    def clock(timestamp):
        return datetime.fromtimestamp(timestamp).strftime("%H:%M")
    lines = [channel.name]
    lines.append(tr("Now: {title} — until {stop} · {remaining} left", language,
                    title=current['title'][:100], stop=clock(current['stop']),
                    remaining=remaining(current['stop'], now, language)) if current else
                 tr("Now: Programme information not yet available", language))
    lines.append(tr("Next: {title} — {start}–{stop} · ends in {remaining}", language,
                    title=following['title'][:100], start=clock(following['start']),
                    stop=clock(following['stop']), remaining=remaining(following['stop'], now, language))
                 if following else tr("Next: Programme information not yet available", language))
    progress = (now-current["start"]) / (current["stop"]-current["start"]) if current else None
    return "\n".join(lines), progress


def programme_text(channel, directory, now=None, language="en"):
    text, _ = programme_banner(channel, directory, now, language)
    return "\n".join(textwrap.fill(line, width=90) for line in text.splitlines())


def programme_parts(channel, directory, now=None, language="en"):
    """Return identity, fixed header fields and deduplicated unwrapped paragraphs."""
    now = time.time() if now is None else now
    current = next((e for e in channel_events(channel, directory, now)
                    if e["start"] <= now < e["stop"]), None)
    if current is None:
        return None, [tr("Programme information not yet available", language)]
    start = datetime.fromtimestamp(current["start"]).strftime("%H:%M")
    stop = datetime.fromtimestamp(current["stop"]).strftime("%H:%M")
    parts = [current["title"], tr("{start}–{stop} · {remaining} left", language, start=start, stop=stop,
                                   remaining=remaining(current["stop"], now, language))]
    texts = [current[key] for key in ("subtitle", "summary", "description") if current[key]]
    # Providers sometimes repeat the short synopsis in the long description.
    for text in texts:
        normalized = " ".join(text.split())
        if normalized == " ".join(current["title"].split()):
            continue
        if any(normalized in " ".join(other.split()) and len(other) > len(text) for other in texts):
            continue
        if not any(normalized == " ".join(part.split()) for part in parts):
            parts.append(text)
    if len(parts) == 2:
        parts.append(tr("No detailed description available", language))
    return (current["start"], current["title"]), parts


def programme_details(channel, directory, now=None, language="en"):
    """Plain-text compatibility view; the HDMI UI lays out programme_parts."""
    identity, parts = programme_parts(channel, directory, now, language)
    lines = []
    for part in parts:
        if lines:
            lines.append("")
        for paragraph in part.split("\n"):
            lines.extend(textwrap.wrap(paragraph, width=76) or [""])
    return identity, lines


class EPGCache:
    def __init__(self, backend, config):
        self.backend, self.config = backend, config
        self.path = runtime_path(Path(config["management_dir"]), "epg.json")
        self.channels = {}
        self.files = JSONCache()
        self.cursor = 0
        self.last_write = 0
        self.warming = True
        self.clock = (time.time(), time.monotonic())
        self.enigma_attempts = {}

    async def refresh(self):
        channels, favourites = library(self.files.read(Path(self.config["library"])))
        now, monotonic = time.time(), time.monotonic()
        # A Pi without an RTC can start at 1970 and synchronize after the web
        # service starts. Refetch the whole guide after a wall-clock correction,
        # rather than waiting for the slow sweep of pre-sync queries to expire.
        if abs((now-self.clock[0]) - (monotonic-self.clock[1])) > 30:
            self.cursor = 0
            self.warming = True
        self.clock = (now, monotonic)
        # Rebuild the RAM cache promptly from the restored backend guide. In
        # steady state, sweep in at most 50 batches (~100s plus backend time),
        # leaving room for latency and the 60s publication interval before the
        # 300s expiry. The 500-favourite limit bounds each batch to ten channels,
        # plus the requested channel, without flooding small installations.
        warming = self.warming
        count = min(4, len(favourites)-self.cursor) if warming else math.ceil(len(favourites)/50)
        selected = [favourites[(self.cursor+i) % len(favourites)] for i in range(max(1, count))]
        self.cursor += len(selected)
        self.warming = self.warming and self.cursor < len(favourites)
        ids = []
        try:
            status = read_json(runtime_path(Path(self.config["player_state_dir"]), "status.json"))
            requested = status.get("requested")
            if requested in channels:
                ids.append(requested)
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        ids = list(dict.fromkeys(ids + selected))
        results = await asyncio.gather(*(self.backend.request("epg/events/grid", channel=channel, limit=8,
            sort="start", dir="ASC",
            filter=[{"field": "stop", "type": "numeric", "comparison": "gt", "value": int(now)}])
            for channel in ids), return_exceptions=True)
        # Enigma2 service streams often omit EIT. OpenWebif guide queries do
        # not open another tuner subscription, and retain the normal cache shape.
        guide = Path(self.config["private_dir"]) / "tv-source-enigma2.json"
        try:
            profile = self.files.read(guide) if guide.exists() else {}
            info = profile.get("enigma2") if profile.get("network_uuid") == self.config["network_uuid"] else None
            if info:
                from .enigma2 import Enigma2
                receiver = Enigma2(info["address"], info["web_port"], info["stream_port"])
                async def fallback(channel, result):
                    rows = result.get("entries", []) if isinstance(result, dict) else []
                    reference = info["services"].get(channels[channel].mux_uuid)
                    if not events(rows) and reference:
                        if monotonic - self.enigma_attempts.get(channel, -60) < 60:
                            return {"entries": self.channels.get(channel, {}).get("events", [])}
                        self.enigma_attempts[channel] = monotonic
                        rows = await asyncio.to_thread(receiver.events, reference)
                        return {"entries": rows}
                    return result
                results = await asyncio.gather(*(fallback(channel, result) for channel, result in zip(ids, results)),
                                               return_exceptions=True)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        changed = False
        refreshed = False
        for channel, result in zip(ids, results):
            # One failed channel must not discard successful replies or starve
            # later favourites. Retain its previous cache until the next sweep.
            if isinstance(result, (BackendError, OSError, ValueError, KeyError, TypeError, AttributeError)):
                continue
            if isinstance(result, BaseException):
                raise result
            if not isinstance(result, dict) or not isinstance(result.get("entries"), list):
                continue
            rows = events(result["entries"])
            changed |= self.channels.get(channel, {}).get("events") != rows
            self.channels[channel] = {"fetched": now, "events": rows}
            refreshed = True
        self.channels = {key: value for key, value in self.channels.items() if key in channels}
        # Publish fresh timestamps for every warm-up batch, even if the event
        # text is unchanged (especially after the 1970-to-current-time step).
        if changed or warming or time.time()-self.last_write >= 60:
            atomic_json(self.path, {"version": 1, "channels": self.channels}, 0o640, durable=False)
            self.last_write = time.time()
        return refreshed

    async def run(self):
        while True:
            delay = 2
            try:
                if await self.refresh() and self.warming:
                    # Yield between published batches, without the normal polling
                    # delay during the first sweep. Outages still back off.
                    delay = 0
            except (BackendError, OSError, ValueError, KeyError, TypeError, AttributeError):
                # EPG outages must not take down TV or the management interface.
                pass
            await asyncio.sleep(delay)
