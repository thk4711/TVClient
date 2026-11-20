# Using the receiver

**Audience: users.**

[Documentation index](README.md) · [Troubleshooting](troubleshooting.md)

For task-by-task illustrated instructions, see the [User manual](user-manual.md).
Choose **User manual** in the browser interface to read or print the local HTML
edition without interrupting the current page or requiring internet access.

## Television controls

| Action | Keyboard | Apple Remote A1294 |
|---|---|---|
| Next / previous favourite | Up / Down | Up / Down |
| Volume +5 / −5 points | Right / Left | Right / Left while playing |
| Back / forward 10 seconds while time shift is paused | Not assigned | Left / Right |
| Open favourites / confirm selection | Enter | Centre |
| Pause / resume buffered TV | Space, Pause or multimedia Play/Pause | Play/Pause |
| Back / forward 30 seconds | Page Up / Page Down | Not assigned |
| Return to live (no navigation panel open) | Home or Esc | Menu |
| Toggle programme details | I | Menu |
| Scroll open details | Up / Down | Up / Down |
| Close overlay without choosing | Esc | From favourites, Menu opens details; press again to close |

Centre/Enter opens favourites and confirms the highlighted channel. Up/Down
moves the selection while a list is open; otherwise it changes channel, wrapping
at the ends. I/Menu shows programme details and Esc closes a panel. The saved
favourite order determines channel numbers. Keep pressing channel keys while a
channel loads if needed: the last choice wins, but an intermediate channel can
appear before the final choice.

Pause/resume requires usable USB storage. While delayed, the lower bar shows
buffered position and delay; remote Left/Right seeks only while paused, otherwise
it adjusts volume. Close navigation panels before using Esc/Menu to return to live.
Use [time-shift controls](timeshift.md#playback-controls) for retention and seeking
limits. During YouTube, Play/Pause and volume control that video.

Only configured receiver controls take over browser viewing. The normal TV remote
through HDMI-CEC is not supported. Favourite and volume settings restore at startup.

## Programme information

The lower channel banner shows the current/next programme, scheduled times,
remaining time and progress when information is available. Channel and volume
bars show the local date/time. Change banner duration and English/German language
in [TV on-screen display settings](device-settings.md#tv-on-screen-display).

The channel banner also shows the selected audio: stereo, surround, or surround
converted to stereo. It appears when audio is ready. Broadcast programme titles,
descriptions and audio retain their broadcast language.

Favourites includes current shows when available. I/Apple Menu opens current
programme details; Up/Down scrolls the description instead of changing channel.
Close details with I/Menu or Esc, or use Centre/Enter to open favourites. Picture
and sound continue underneath; volume works without closing the panel.

Programme data can take a minute or more after tuning. Unvisited channels may
have none, and schedules are broadcaster estimates, not actual ending times.
While paused/delayed, **Live programme information** describes the live broadcast,
not the historical picture. Browser lists also show live information.

## HDMI audio

TV playback uses normal German audio only; audio description, hearing-impaired
tracks and foreign-language fallbacks are excluded. If no unambiguous supported
German track is available, the receiver cannot play its audio and reports a playback problem.

The receiver checks which PCM audio formats the connected HDMI device supports. Multichannel-capable
devices receive German surround audio when broadcast. Stereo programmes remain
stereo; stereo-only devices receive stereo, downmixing surround when necessary.
If the connected device's capabilities cannot be determined, output is stereo. Capabilities are reconsidered on each channel load. Reboot after changing the HDMI connection.

Audio is decoded on the Pi, so receiver volume controls work with stereo and surround sound. Compressed
Dolby/DTS passthrough is not enabled. A TV/receiver that supports compressed
Dolby but advertises only two-channel PCM receives stereo. For surround, connect
the Pi directly to an AV receiver advertising multichannel PCM; a TV or ARC
connection may limit the capabilities advertised to the Pi. A Dolby track can
carry stereo for one programme and 5.1 for another.

Browser viewing uses stereo audio; surround output is for HDMI TV playback.

## Browser access

Open the URL or QR code shown on HDMI on your home LAN. First setup is
password-free, and new installations default to login-free management. Setting
a web password enables login (username satip). HTTP does not encrypt passwords or
backups: use only a trusted home network. See [first boot](buildroot-first-boot-setup.md)
and [passwords and access](device-settings.md#passwords--access).

## Application pages

The responsive interface has five pages: **Status**, **Live TV**, **Channels**,
**Channel search**, and **Settings**. Navigation preserves unsaved favourites and an active
browser stream. The header shows HDMI activity and a stop control while streaming.
First-boot setup uses the same visual theme.

### Settings

Use [Device settings](device-settings.md) for all Settings controls, including
Wi-Fi, passwords, on-screen timeout, USB storage and YouTube pairing.

### Status

Status shows the active tuner/transponder description, signal strength and
quality/SNR, stream bitrate and transport/continuity errors. Measurements are
read from Tvheadend without tuning. Relative measurements use percentages;
physical measurements use the backend's dB/dBm scale. Missing measurements are
labelled **Not reported**, and an idle tuner is distinct from zero signal.

System information includes hostname, hardware, CPU temperature/utilization,
uptime, memory, management-storage space, OS and image identity. Network cards
show interface state, IPv4/IPv6 addresses, IPv4 default route, link speed, traffic
rates and errors. Wi-Fi adds network name, signal, frequency and connection rate
when available. Some system/power measurements may be unavailable; this does not
mean their value is zero.

Telemetry refreshes every five seconds while Status is visible. Sections have
independent timestamps and stale/unavailable states. CPU and traffic rates need
two samples. Recent activity is bounded to this browser session, not a durable log.

#### Performance monitor

On **Status**, click **Open performance monitor** in the **Performance** section
to show the graphs and top-process table in a window inside the web interface.
The window stays closed until you press the button. Close it with **Close** or
**Escape**; keyboard focus returns to the opening button. It scrolls on smaller
screens and closes when you navigate away from Status. Opening or closing it
does not interrupt TV playback.

The monitor helps investigate playback interruptions without installing extra software.
It shows total and per-core CPU utilization,
per-core frequency, RAM and swap use, network receive/send rates, physical disk
read/write rates, load averages, available RAM and file cache. CPU temperature
remains in the Status summary. USB disks appear while connected.

Graphs cover up to five minutes **in this browser session**. They refresh every
five seconds only while Status is visible. History continues while the monitor
window is closed, but graphs render only while it is open. Reopening retains
history; reloading clears it. Gaps indicate
missing samples or time spent away from Status. CPU, disk and process rates need
two samples; after a gap longer than 15 seconds they need a new baseline.
Network and disk graphs auto-scale independently; CPU/RAM/swap use 0–100%.
Network throughput is Mbit/s, disk throughput is MiB/s, and RAM is MiB.

RAM used is total minus available (not total minus completely free memory).
The cache figure is Linux's file cache and is not additional application usage.
Zero swap usage with zero capacity means swap is not configured.
Disk rates describe whole physical devices, not individual filesystems: SD-card
partitions are not counted twice, and RAM/loop disks are excluded.

The top 12 processes are sorted by CPU, then resident RAM. Process CPU uses
**100% for one fully busy core**, so a multithreaded process can reach 400% on a
four-core Pi; total/per-core meters remain 0–100%. Resident memory includes shared
pages and should not be summed as total RAM usage. Names are kernel process names,
not full command lines. The list is read-only and labelled if collection is limited.
No process controls, command lines, environments or durable history are exposed.

## Favourites

1. Open **Channels** and filter available channels by name.
2. Add to Your favourites; remove with the trash icon.
3. Drag the ⋮⋮ handle to reorder, or use arrow buttons.
4. Click **Save favourites** to apply remote navigation/TV menu order.

At least one favourite is required. Unsaved page edits survive polling/refresh,
but not page closing/reload. Cancelling a drag restores the previous order.
Scanning never changes saved favourites.

The **Channel backup** card on this page separately downloads/restores available
channels and the saved favourite order, without changing device settings. Save
your edits before downloading. Restore replaces the lists and briefly pauses TV
without rebooting. See [channel backup](device-settings.md#channel-backup) for
confirmation, compatibility and recovery details.

A previously saved channel can remain available for browser viewing after removal
from favourites. A save timeout does not necessarily mean failure; check page
feedback before retrying. Failed saves retain local unsaved edits.

## Channel search

With SAT-IP active, **Search for channels** confirms a viewing pause and searches
the configured satellite channels. Quick search checks only enabled transponders;
full search checks the configured Astra definitions, not every possible satellite
frequency. It can take many minutes. With Enigma2 active, search rechecks imported
streams; use bouquet import to load new channel groups.

Progress counts completed transponders. Supported discoveries appear
incrementally; duplicate discoveries appear once and known channels remain. Valid partial results
are kept, with separate partial/failure counters. Encrypted/unknown-encryption,
UHD/HEVC, radio, and unsupported audio services are excluded. See [supported viewing modes](../README.md#supported-tv-and-viewing-modes).

- **Stop search:** cancel, restore reception settings, retain published results.
- **Restore TV playback:** retry pending recovery; offered only when needed.
- **Reload saved channels:** reread backend metadata without tuning.

Saving is blocked during scanning/recovery. Failed recovery keeps TV paused for
safety. Use the offered recovery control rather than repeatedly starting searches.

## Live TV

Open **Live TV** and choose a saved channel, or use the **Watch live** icon on
Channels. Streaming starts without a confirmation popup and pauses HDMI. For
a newly found channel: Add → Save → Watch live. The Live TV channel selector
lists favourites first and supports switching channels.

Live TV offers browser playback, volume, fullscreen, and current/next programme
information when available in the EPG cache. If autoplay is blocked, press Play.
Browser volume is independent of HDMI volume. Streaming has
several seconds of latency and no HDMI OSD.

Only one stream runs. Search, refresh, and saving are blocked until it ends.
**Stop streaming and resume TV** releases it. There is no maximum viewing
duration. If you close the page unexpectedly, automatic return to HDMI can take about
20 seconds. Prefer the explicit stop button. Reload does not reclaim an old
session. Web shutdown/restart cleans up streaming and releases its pause.

**Any physical key-down on the configured Pi keyboard or GPIO remote takes
priority over streaming**, even an unmapped key. HDMI resumes and the receiver applies the button action. The browser clears buffered playback and explains
that the receiver was used locally. Streaming requires an explicit new start.
Keys on the computer running the browser do not request this handover.

## Importing Enigma2 bouquets

Use [TV source settings](device-settings.md#tv-source) to configure a receiver
and [Enigma2 import](enigma2.md#discover-and-import) for compatibility and bouquet
selection. Loading bouquets alone does not activate a source. Import interrupts
Pi viewing, preserves matching favourite order, and does not reconfigure the source
receiver.
