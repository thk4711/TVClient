# Time-shift internals

**Audience: developers and advanced administrators.**

[User controls](timeshift.md) · [Architecture](architecture.md) · [Qualification](timeshift-validation.md)

Time shifting uses a bounded rolling USB buffer. A retention limit removes
old content; it does **not** discard the whole session or return the viewer to live.
Supported storage is bounded and checked independently by the helper and player.
See [qualification](timeshift-validation.md) for the current acceptance boundaries.

## Live and archive data paths

**Watching live TV does not write programme data to USB.** With configured usable
storage, a short RAM-only prebuffer keeps approximately 20 seconds, with a 64 MiB
file budget (completed segments plus the in-progress segment). The first Pause
freezes the existing picture and starts a separate USB copier from that prebuffer.
Neither mpv nor the tuner is reloaded on activation. Live TV is raw-stream
passthrough: FFmpeg analysis and segment completion run off the picture-start
critical path. Resume switches the paused reader to the archive and uses retained
broadcast timestamps plus a cached exact seek to preserve the pause point. There
is one backend subscription and no transcode. The RAM files must be on verified tmpfs; there is no SD fallback.

| State | USB programme writes |
|---|---|
| Watching live TV | None; only the short RAM prebuffer runs |
| Paused / resumed time-shift session | Bounded rolling USB capture |
| Return to live | USB writer stops; USB session history is discarded |
| Channel change, YouTube, browser preview or search | USB session stops and is discarded |

Resume continues the time-shift session; use **Return to live** to stop USB writes.
Seeking near the live edge does not itself end the session. Completing/cleaning an
old session can cause a short final filesystem/journal flush, not continuous writes.
Without usable USB, newly loaded TV uses the original direct live path. Reconnecting
configured storage can briefly reload TV to enable the RAM prebuffer, not USB capture.

## Playback controls

User bindings are listed in [Playback controls](timeshift.md#playback-controls).

First Pause freezes the current picture without reloading it. First resume from
raw live playback switches only the local reader to the archive at the saved pause
timestamp, not the tuner. If that point is not yet seekable, TV stays paused and
shows the building-buffer notice; another Play/Pause retries. Seek requests are
clamped to retained content. A brief reader transition still needs physical
picture/sound observation; software timestamp checks alone do not qualify it.
Thirty-second jumps are segment/keyframe based. Ten-second paused jumps and
raw-to-archive resume use an exact seek within mpv’s RAM cache once the target
is buffered. Broadcaster timestamps/GOPs still constrain actual accuracy.
Paused seeks display the new frame without resuming sound or playback. Holding
remote Left/Right uses native repeat handling. Rapid presses accumulate into one
requested position (five Right presses request approximately +50 seconds), with
one seek in progress and at most one updated target waiting. The lower bar shows
the requested delay during the transition, then the confirmed position. Opposite
presses adjust that requested position; each request is clamped to retained content.
Pause/resume, Return to live and channel changes discard pending seeks.
An in-progress reader handover finishes safely before another control changes it.

Return to live stops the USB copier, discards USB history, and resumes the
original raw live stream. A subsequent Pause starts a new USB session. SAT-IP,
short live-preroll and decoder latency remain: “live” does not mean zero delay.
Live playback does not wait for segment completion. Archive seeks reuse mpv's
seekable RAM cache when possible. Cache misses reopen only the local player
reader, not the tuner/capture; the first raw-to-archive seek also needs that
handover. Archive playback crosses
segment boundaries over a single continuous response; live is raw passthrough.

Remote Menu keeps its navigation behaviour inside favourites/details. Close
an open details panel first, then press Menu again to return to live. To inspect
programme details while delayed, open favourites with Centre, then press Menu.
Keyboard Esc closes a navigation panel first; a subsequent Esc returns to live.

The lower overlay shows paused/delayed playback, delay behind the completed live
edge, retained history, and a Return-to-live hint. It stays visible while paused.
Resume/seek reveal it briefly; volume and navigation panels retain priority.
Programme banners/details explicitly say **Live programme information** when
paused or delayed: they describe the current broadcast, not historical EPG for
the delayed picture. Browser programme lists also remain live information.

Channel changes, scan/browser takeover, YouTube, player restart and reboot discard
history. The next channel starts live/unpaused with a fresh RAM-only prebuffer if
USB is usable. Seek controls do not activate USB capture; press Pause first.
Without storage, pause/seek shows an unavailable notice and leaves live picture
and sound unchanged.

## Rolling retention and oldest-content behaviour

The browser budget bounds retained content. Saved/API duration limits
bound retained history, not elapsed session lifetime; zero means no time
limit. Changing the budget trims history without ending the session.

- Evict completed TS segments oldest first, at whole-segment granularity.
- Reserve 32 MiB of the budget for the in-progress file, preserve approximately
  1 GiB filesystem free space, and cap metadata at 4,096 segments.
- The segment-count cap can limit retention before the byte budget is reached:
  at two seconds per segment it is approximately 2.3 hours. Increasing the USB
  budget alone neither removes that cap nor speeds up seeking.
  RAM completion and USB copier indexes update independently; the public
  timeline can briefly include a newer RAM segment before USB trimming catches
  up. The USB retention budget is still enforced by the copier.
- A Linux per-file 32 MiB limit bounds a segment even when FFmpeg finds no next
  keyframe. Excessively long/large segments cause explicit recovery.
- Warn a paused viewer when estimated remaining retention is 60 seconds or less.
  The countdown uses recent bitrate and configured limits; sudden storage or
  bitrate changes may shorten the warning. It is an estimate, not a guarantee.
- If the position expires, move to the oldest playable retained content.
  **Paused playback remains paused.** A playing viewer stays playing. Neither
  returns to live merely because the buffer reaches its limit.
- If the viewer remains paused at that boundary, the selected frame must keep
  moving forward as history is evicted. A bounded buffer cannot preserve an
  indefinitely paused position.
- USB disappearance, read-only storage, insufficient space, failed capture or
  unusable media returns to direct live TV with an explicit notice. Transient
  RAM-prebuffer-start failures retry after 30, 60 and 120 seconds, then retain live TV
  without further automatic interruptions. Pause requests an explicit retry;
  storage reconnection or channel changes also allow a new attempt. These retries
  never start USB writes automatically. A failed USB activation restores live
  playback/unpaused state; another Pause explicitly retries USB activation.

This is not recording/PVR or persistent playback. Intentional pause/bounded seek
is excluded from stall detection; IPC failures still trigger recovery.

Copier failures are distinguished from USB disappearance: a capture that falls
behind reports a capture notice rather than claiming that the USB is missing.
One credential-free failure record survives session cleanup at
`/run/satip-player/last-usb-failure.json` until another copier failure or reboot.
It contains the failed operation, exception class/error number, last copied
timestamp and RAM-index age, not programme data or credentials.
The USB history index is published only when content/readiness/settings change;
idle polling does not repeatedly serialize the entire retained history. RAM
capture freshness uses elapsed monotonic time, not the device's wall clock.

## Mounting persistence and removal

The root helper mounts at **/media/satip-timeshift** with **nosuid,nodev,noexec**.
Only the player owns the mode-0700 **buffer** directory. Sessions use private,
randomly named subdirectories. The playback relay binds only to loopback with a
random session path. Backend credentials travel privately on stdin, not capture
process arguments or public status.

- Saved selection/budget: **/data/store/identity/timeshift.json**, root-only.
- Readiness: **/run/satip-timeshift/storage.json**, root-written, current-boot only.
- Remount exactly one matching saved USB UUID after reboot/reconnection, without
  formatting; refuse busy/ambiguous disks. Device-name changes do not affect
  selection. Discovery retries every five seconds.
- Independently verify readiness, writable ext4 mount, actual USB identity and
  free space in the player/writer. There is **no ordinary-directory or SD fallback**.

Use **Safely remove USB** before unplugging. It revokes readiness, stops playback
and capture, unmounts normally, and restarts direct TV. It **retains** the saved
selection/budget. A current-boot hotplug-identity gate prevents immediate remount
while the same device stays connected, including after helper restart. Physical
reconnection or explicit browser mounting allows reuse; reboot clears the gate.
**Forget USB configuration** is separate and available after safe removal; it
erases no USB files.

A failed unmount is not permission to unplug. Retry safe removal; never use
forced/lazy unmount. Unexpected removal can leave a stale mount needing safe
removal before reconnecting. Automatic reuse after intentional removal requires
observing disconnection or a changed hotplug identity.

The separate RAM supervisor keeps the short prebuffer bounded if the controller
freezes or USB I/O is slow. An independent USB copier runs only during an activated
session and maintains its own rolling limits. The copier can temporarily hold one
open RAM segment (up to 32 MiB) in addition to the prebuffer budget; player/relay
buffers have their own separate RAM costs. mpv is configured for 64 MiB forward
and 32 MiB backward packet-cache limits (not a total-process RSS guarantee).
Archive readers request up to 60 seconds of read-ahead, bounded by those byte
limits; an exact cache-miss seek starts about 15 seconds before its target to
retain a short backward-seek cushion. Live read-ahead remains unchanged. A
parent-lifetime pipe stops capture when the controller dies. Normal
shutdown removes owned segments/metadata; later sessions clean stale owned files
without following links or deleting live capture. Buffers are excluded from
configuration backups. Ext4 journal recovery is kernel-managed; filesystem repair
and reformatting are never automatic.

## Dependencies and validation

The image requires FFmpeg MPEG-TS/segment muxers and HTTP/file/pipe support, mpv,
and native e2fsprogs for formatting. Build validation checks SEGMENT_MUXER.
Application-only updates do not install media dependencies missing from older images.
Verify the target features before deployment.

From a checkout on the Pi, run as root:

    PYTHONPATH=/opt/satip python3 validation/exercise_timeshift_on_pi.py

This synthetic accelerated test uses real USB and headless mpv without a tuner.
It temporarily changes the readiness marker's duration and restores it, without
formatting or changing persistent settings. It is not physical HDMI/remote
qualification. See [validation evidence](timeshift-validation.md) and the
[acceptance matrix](validation.md).
