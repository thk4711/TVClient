"""TV-safe, scalable ASS panels rendered by the existing mpv GPU output."""
import asyncio
import contextlib
import math
import time
from datetime import datetime

from .mpv_ipc import IPCError
from .i18n import tr
from .font_metrics import text_width

WIDTH, HEIGHT = 1920, 1080
MENU_X, MENU_Y, MENU_WIDTH, MENU_HEIGHT = 72, 72, WIDTH - 144, HEIGHT - 144
MENU_TEXT_X, MENU_TEXT_WIDTH, MENU_BOTTOM = 112, MENU_WIDTH - 80, 950
BODY_TOP, BODY_LINE_HEIGHT, BODY_SIZE = 264, 40, 34
SCROLLBAR_X = MENU_TEXT_X + MENU_TEXT_WIDTH - 12
BODY_WIDTH = SCROLLBAR_X - MENU_TEXT_X - 16
BODY_ROWS = (MENU_BOTTOM - BODY_TOP) // BODY_LINE_HEIGHT
FAVOURITES_TOP, FAVOURITES_ROW_HEIGHT, FAVOURITES_BOTTOM = 190, 50, 950
FAVOURITES_ROWS = (FAVOURITES_BOTTOM - FAVOURITES_TOP) // FAVOURITES_ROW_HEIGHT
PANEL_ALPHA = "33"  # ASS transparency: 20% transparent / 80% opaque.
WHITE, MUTED, BLUE, PANEL = "F7F5F2", "C8BBB0", "FFB34D", "201B17"  # ASS BGR


def literal(text):
    return "".join(c for c in text if c == "\n" or ord(c) >= 32)


def escape(text):
    # Same escaping as mpv escape-ass: break backslash escapes with WORD JOINER.
    # Array/node IPC avoids property expansion; only our own tags are executable.
    return literal(text).replace("\\", "\\\u2060").replace("{", "\\{").replace("\n", "\\N")


def advance(text, size):
    # Conservative proportional-font estimate, including wide glyphs and CJK.
    return sum((1 if ord(c) > 0x2fff or c in "MWmw@%" else
                .38 if c in " il.,:;!'|I" else .82 if c.isupper() else .68)
               * size for c in text)


def font_advance(text, size, bold=False):
    measured = text_width(text, size, bold)
    return measured if measured is not None else advance(text, size) * (1.08 if bold else 1)


def fit(text, width, size, measure=advance):
    text = literal(text).replace("\n", " ")
    if measure(text, size) <= width:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if measure(text[:middle] + "…", size) <= width:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + "…"


def wrap(text, width=BODY_WIDTH, size=BODY_SIZE):
    lines = []
    # Use the same font advances as rendering, not a character-count limit.
    # Keep paragraph breaks and split oversized words only where they must break.
    for paragraph in literal(text).split("\n"):
        line = ""
        for word in paragraph.split():
            candidate = (line + " " + word).strip()
            if font_advance(candidate, size) <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
            while font_advance(word, size) > width:
                low, high = 1, len(word)
                while low < high:
                    middle = (low + high + 1) // 2
                    if font_advance(word[:middle], size) <= width:
                        low = middle
                    else:
                        high = middle - 1
                lines.append(word[:low])
                word = word[low:]
            line = word
        lines.append(line)
    return lines


class Canvas:
    def __init__(self):
        self.events = []

    def rect(self, x, y, w, h, color=PANEL, alpha=PANEL_ALPHA):
        self.events.append(
            f"{{\\rDefault\\an7\\pos({x},{y})\\bord0\\shad0\\1c&H{color}&"
            f"\\1a&H{alpha}&\\p1}}m 0 0 l {w} 0 {w} {h} 0 {h}{{\\p0}}")

    def text(self, x, y, text, size=30, color=WHITE, bold=False, width=1640, measured=False, right=False):
        measure = (lambda value, size: font_advance(value, size, bold)) if measured else advance
        anchor = 9 if right else 7  # ASS top-right / top-left anchoring.
        self.events.append(
            f"{{\\rDefault\\fnDejaVu Sans\\an{anchor}\\pos({x},{y})\\fs{size}\\b{int(bold)}"
            f"\\bord0\\shad0\\fscx100\\fscy100\\1c&H{color}&\\1a&H00&\\q2}}"
            + escape(fit(text, width, size, measure)))

    def data(self):
        return "\n".join(self.events)


def clock_label(now=None, language="en"):
    # /etc/localtime may be changed by the appliance Settings page.
    import time
    if hasattr(time, "tzset"):
        time.tzset()
    return (now or datetime.now()).strftime("%d.%m.%Y  %H:%M" if language == "de" else "%Y-%m-%d  %H:%M")


def banner(text, progress=None, number=None, clock=None, language="en", audio=""):
    c = Canvas()
    c.rect(0, 800, WIDTH, 280)
    c.rect(72, 836, 5, 190, BLUE, "00")
    lines = text.splitlines()
    c.text(100, 830, ((f"{number:02d}   " if number else "") + lines[0]) if lines else "",
           38, bold=True, width=1230)
    c.text(WIDTH - 100, 835, clock or clock_label(language=language), 30, MUTED, width=430, right=True)
    if audio:
        c.text(WIDTH - 100, 873, audio, 24, BLUE, width=430, right=True)
    for i, line in enumerate(lines[1:4]):
        title, separator, schedule = line.rpartition(" — ")
        y = 894 + i * 48
        if separator:
            c.text(100, y, title, 32, WHITE if i == 0 else MUTED, width=1020)
            c.text(1160, y + 3, schedule, 30, MUTED, width=660)
        else:
            c.text(100, y, line, 32, WHITE if i == 0 else MUTED, width=1720)
    if progress is not None:
        c.rect(100, 1032, 1720, 4, MUTED, "80")
        c.rect(100, 1032, max(1, round(1720 * max(0, min(1, progress)))), 4, BLUE, "00")
    return c.data()


def details_panel(channel, parts, lines, offset, language="en"):
    c = Canvas()
    c.rect(MENU_X, MENU_Y, MENU_WIDTH, MENU_HEIGHT)
    c.rect(MENU_X, MENU_Y, MENU_WIDTH, 4, BLUE, "00")
    c.text(112, 88, channel + "  /  " + tr("PROGRAMME INFORMATION", language), 30, MUTED,
           width=MENU_TEXT_WIDTH, measured=True)
    c.text(112, 130, parts[0], 42, bold=True, width=MENU_TEXT_WIDTH, measured=True)
    if len(parts) > 1:
        c.text(112, 189, parts[1], 32, BLUE, width=MENU_TEXT_WIDTH, measured=True)
    c.rect(112, 238, 1696, 1, MUTED, "A0")
    for i, line in enumerate(lines[offset:offset + BODY_ROWS]):
        c.text(MENU_TEXT_X, BODY_TOP + i * BODY_LINE_HEIGHT, line, BODY_SIZE,
               width=BODY_WIDTH, measured=True)
    c.rect(MENU_TEXT_X, MENU_BOTTOM, MENU_TEXT_WIDTH, 1, MUTED, "A0")
    c.text(112, MENU_BOTTOM + 16, tr("↑ / ↓  Scroll    I / Menu / Esc  Close    Enter  Channels", language),
           30, MUTED, width=1400, measured=True)
    total = len(lines)
    if total > BODY_ROWS:
        track_height = BODY_ROWS * BODY_LINE_HEIGHT
        c.rect(SCROLLBAR_X, BODY_TOP, 4, track_height, MUTED, "A0")
        thumb = max(24, round(track_height * BODY_ROWS / total))
        y = BODY_TOP + round((track_height - thumb) * offset / (total - BODY_ROWS))
        c.rect(SCROLLBAR_X, y, 4, thumb, BLUE, "00")
        c.text(MENU_TEXT_X + MENU_TEXT_WIDTH, MENU_BOTTOM + 16,
               f"{offset + 1}–{min(offset + BODY_ROWS, total)} / {total}",
               30, MUTED, width=235, measured=True, right=True)
    return c.data()


def favourites_columns(rows, language="en"):
    # Measure the installed font, including bold names so moving selection never
    # shifts the columns. Cap pathological names to protect programme space.
    name_width = min(480, math.ceil(max([font_advance(tr("Channel", language), 30),
                                      *(font_advance(row[2], 34, True) for row in rows)])) + 4)
    programme_x = 236 + name_width + 32
    return name_width, programme_x, 1800 - programme_x


def favourites_panel(rows, selected, page, pages, language="en"):
    name_width, programme_x, programme_width = favourites_columns(rows, language)
    c = Canvas()
    c.rect(MENU_X, MENU_Y, MENU_WIDTH, MENU_HEIGHT)
    c.rect(MENU_X, MENU_Y, MENU_WIDTH, 4, BLUE, "00")
    c.text(112, 100, tr("Channels", language), 42, bold=True)
    c.text(1540, 109, tr("Page {page} / {pages}", language, page=page, pages=pages), 30, MUTED, width=260)
    c.text(236, 153, tr("Channel", language), 30, MUTED, width=name_width, measured=True)
    c.text(programme_x, 153, tr("Current programme", language), 30, MUTED, width=programme_width, measured=True)
    c.rect(132, 185, 1648, 1, MUTED, "A0")
    for i, (index, _uuid, name, programme) in enumerate(rows):
        y = FAVOURITES_TOP + i * FAVOURITES_ROW_HEIGHT
        if index == selected:
            c.rect(104, y, 1712, FAVOURITES_ROW_HEIGHT - 4, "704020", "30")
            c.rect(104, y, 4, FAVOURITES_ROW_HEIGHT - 4, BLUE, "00")
        c.text(132, y + 6, f"{index + 1:02d}", 34, BLUE, width=90)
        c.text(236, y + 6, name, 34, bold=index == selected, width=name_width, measured=True)
        c.text(programme_x, y + 6, programme, 34, WHITE if index == selected else MUTED,
               bold=index == selected, width=programme_width, measured=True)
    c.rect(112, FAVOURITES_BOTTOM, 1696, 1, MUTED, "A0")
    c.text(112, FAVOURITES_BOTTOM + 16, tr("↑ / ↓  Select    Enter / Centre  Watch    I / Menu  Info    Esc  Close", language),
           30, MUTED, width=1696)
    return c.data()


def volume_panel(volume, mute, language="en"):
    c = Canvas()
    c.rect(0, 916, WIDTH, 164)
    c.text(WIDTH - 100, 927, clock_label(language=language), 30, MUTED, width=430, right=True)
    c.text(100, 963, tr("Muted", language) if mute else tr("Volume", language), 32, bold=True, width=290)
    c.rect(430, 982, 1150, 10, MUTED, "90")
    if volume > 0:
        c.rect(430, 982, round(1150 * max(0, min(100, volume)) / 100), 10,
               MUTED if mute else BLUE, "00")
    c.text(1650, 963, f"{volume:.0f}%", 32, MUTED if mute else WHITE, width=190)
    return c.data()


def elapsed_label(seconds):
    seconds = max(0, round(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def timeshift_panel(status, language="en"):
    c = Canvas()
    c.rect(0, 870, WIDTH, 210)
    warning = status.get("warning", "")
    if warning:
        expiry = status.get("position_expires_seconds")
        text = tr(warning, language) + (f" ({expiry}s)" if expiry is not None else "")
        c.text(100, 882, text, 28, BLUE, width=1720)
    c.text(100, 1048, tr("Play/Pause: pause or resume · Menu/Back: return to live", language), 24, MUTED, width=1720)
    c.text(100, 927, tr("TV paused" if status["paused"] else "Time-shift playback", language),
           32, bold=True, width=700)
    c.text(WIDTH - 100, 927, clock_label(language=language), 30, MUTED, width=430, right=True)
    available = status.get("timeline_seconds", status["available_seconds"])
    delay = status["behind_live_seconds"]
    if available <= 0:
        c.text(100, 983, tr("Building time-shift buffer – no seek range available yet", language), 30)
    else:
        c.text(100, 976, "−" + elapsed_label(delay), 32, BLUE, bold=True, width=300)
        c.rect(430, 992, 1150, 6, MUTED, "90")
        fraction = max(0, min(1, 1 - delay / available))
        c.rect(430, 992, max(1, round(1150 * fraction)), 6, BLUE, "00")
        c.rect(430 + round(1150 * fraction) - 3, 984, 6, 22, WHITE, "00")
        c.text(430, 1017, "−" + elapsed_label(available), 24, MUTED, width=350)
        c.text(1580, 1017, tr("Live", language), 24, MUTED, width=200, right=True)
        c.text(1650, 976, tr("Live", language), 32, width=190)
    return c.data()


class OSD:
    def __init__(self, player, duration=3000, language="en"):
        self.language = language
        self.player, self.duration = player, duration
        self.overlay = None
        self.lock = asyncio.Lock()
        self.sequence = 0
        self.timers = {}
        self.tasks = set()
        self.durations = {}
        self.timeshift_until = 0
        self.timeshift_data = None

    async def send(self, id, data, ipc=None):
        ipc = ipc or self.player.ipc
        if ipc and not ipc.closed:
            await ipc.command({"name": "osd-overlay", "id": id,
                               "format": "ass-events" if data else "none",
                               "data": data, "res_x": WIDTH, "res_y": HEIGHT,
                               "z": 3 if id == 2 else 2 if id == 3 else id})

    def cancel_timer(self, id):
        timer = self.timers.pop(id, None)
        if timer:
            timer.cancel()
        self.durations.pop(id, None)

    def expire_later(self, id, duration):
        self.cancel_timer(id)
        ipc = self.player.ipc
        self.durations[id] = duration
        handle = None

        async def expire():
            async with self.lock:
                if self.timers.get(id) is handle:
                    self.cancel_timer(id)
                    if ipc is self.player.ipc:
                        with contextlib.suppress(IPCError, OSError):
                            await self.send(id, "", ipc)

        def schedule():
            task = asyncio.create_task(expire())
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

        handle = asyncio.get_running_loop().call_later(duration / 1000, schedule)
        self.timers[id] = handle

    async def show(self, text, duration=None, expected=None, data=None):
        async with self.lock:
            if expected is not None and self.sequence != expected:
                return None
            self.sequence += 1
            self.cancel_timer(1)
            if not self.overlay:
                self.cancel_timer(2)
                await self.send(2, "")
            await self.send(1, self.overlay or data or (banner(text, language=self.language) if text else ""))
            if not self.overlay and text:
                self.expire_later(1, self.duration if duration is None else duration)
            return self.sequence

    async def menu(self, text, data=None):
        async with self.lock:
            self.overlay = data or banner(text, language=self.language)
            self.sequence += 1
            self.cancel_timer(1)
            await self.send(1, self.overlay)

    async def volume(self, volume, mute):
        async with self.lock:
            self.sequence += 1  # Late EPG must not replace fresh volume feedback.
            if not self.overlay:
                self.cancel_timer(1)
                await self.send(1, "")
            await self.send(2, volume_panel(volume, mute, self.language))
            self.expire_later(2, self.duration)

    async def timeshift(self, status, reveal=False):
        async with self.lock:
            if reveal:
                self.timeshift_until = time.monotonic() + self.duration / 1000
                self.sequence += 1
                if not self.overlay:
                    self.cancel_timer(1)
                    await self.send(1, "")
            visible = status["enabled"] and (status["paused"] or status.get("warning") or time.monotonic() < self.timeshift_until)
            data = timeshift_panel(status, self.language) if visible else ""
            if data != self.timeshift_data:
                await self.send(3, data)
                self.timeshift_data = data
            if not status["enabled"]:
                self.timeshift_until = 0

    async def dismiss(self):
        async with self.lock:
            self.overlay = None
            self.sequence += 1
            self.timeshift_until = 0
            self.timeshift_data = None
            for id in (1, 2, 3):
                self.cancel_timer(id)
                await self.send(id, "")

    async def reconnect(self):
        async with self.lock:
            self.timeshift_until = 0
            self.timeshift_data = None
            for id in (1, 2, 3):
                self.cancel_timer(id)
            await self.send(3, "")
            self.sequence += 1
            await self.send(1, self.overlay or "")

    async def close(self):
        for id in list(self.timers):
            self.cancel_timer(id)
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


def volume_label(volume, mute, language="en"):
    return (tr("Muted — ", language) if mute else "") + tr("Volume: {volume}%", language, volume=f"{volume:.0f}")
