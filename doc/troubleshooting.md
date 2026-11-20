# Troubleshooting

**Audience: developers and advanced administrators.**

For checks that do not require SSH, start with the
[User manual troubleshooting table](user-manual.md#troubleshooting).
This page contains the additional checks for problems that browser controls alone cannot resolve.

## Brief freeze after changing channel

Distinguish buffering from rendering overload using player status:

~~~sh
cat /run/satip-status/status.json
tail -n 80 /run/satip-supervisor/player.log
tail -n 80 /run/satip-supervisor/tvheadend.log
~~~

`health.paused-for-cache=true` with stopped `time-pos`/`audio-pts` indicates
automatic buffering, even if the user's `paused` state is false. Cache
duration can be limited by audio while video still has queued frames. Status
is sampled about once a second and can miss a short pause; the passive mpv
monitor or switching benchmark provides a finer trace without another stream.

Live channel loads build a one-second RAM demuxer cushion before output
starts, rather than starting immediately and refilling after an early underrun.
The explicit startup gate checks the selected-stream cache as well as German
audio policy; enabling mpv's initial-cache flag alone did not reliably prevent
the pause in the controlled Enigma2 test.
See the [Enigma2 investigation](enigma2-internals.md#live-startup-buffering).
No Ethernet-only workaround or Wi-Fi setting change is required. Do not
disable `cache-pause` to hide an underrun: it can leave audible gaps instead.

If playback advances normally but frame-drop counters climb, investigate
decoding/rendering load separately. Backend continuity-counter warnings are
evidence of stream discontinuities, not proof of a Wi-Fi fault. Later cache
pauses can still reflect source/network interruptions; correlate relay and
upstream byte/age counters rather than assuming the USB drive is responsible.

## Time shift unexpectedly returns to live

Before rebooting, read these files over SSH (an absent failure record means
none was retained in the current boot):

~~~sh
tail -n 80 /run/satip-supervisor/player.log
tail -n 80 /run/satip-supervisor/tvheadend.log
cat /run/satip-player/last-capture-failure.json
cat /run/satip-player/last-usb-failure.json
~~~

The two bounded failure records survive ordinary session cleanup, but not reboot.
The capture record's `reason` distinguishes `upstream_eof`, `upstream_timeout`,
`upstream_error` and `upstream_stalled` from `capture_blocked`, `muxer_exit`,
`segment_stalled` and RAM/index operations. Terminal health counters are preserved.
**TV input interrupted** is an input failure, not a USB-capacity diagnosis. Correlate
its time with Tvheadend `no data received`, RTSP errors and tuning failures.
These establish reception trouble, but do not by themselves distinguish the
tuner, satellite signal, network or client. **Time-shift capture stopped** indicates
a muxer/segment failure; the exit code is recorded without retaining raw stderr.
Do not lengthen deadlines or suppress recovery to hide these symptoms.

Automatic capture retries use 30/60/120-second backoff, including failures after
a replacement process starts. After exhaustion, direct live TV continues without
further automatic prebuffer reloads. Fresh input/capture writes for 120 seconds
reset the retry budget; Pause, channel changes and storage reconnection permit
a new attempt. A retry never starts USB writes automatically.

In the USB record,
`ram_index`/`ram_segment` failures indicate stalled capture or missing RAM
content; `usb_write`/`usb_publish` indicate copy/publication failure, and
`storage` indicates failed storage readiness/identity checks. Check free space,
mount readiness and kernel USB/I/O messages separately; a copier error is not
proof of physical USB removal. Do not format the drive merely for this notice.

A live-reader queue-overflow warning while paused can be expected: the bounded
live reader stops consuming, while capture continues. That warning alone is
not evidence of a USB failure. See [time-shift validation](timeshift-validation.md)
for the long-pause/repeated-seek check and its qualification limits.

## Configuration backup or restore problems

- Finish browser Live TV, channel searches, pending channel recovery and Wi-Fi
  trials before creating or restoring a backup. Keep the Settings page open:
  brief HTTP interruption during backup is expected.
- Restore only a complete, trusted `satip-backup-….tar` archive from this feature,
  up to 32 MiB. Legacy tarballs, compressed archives, mismatched data/backend
  versions and files with invalid checksums are refused without activation.
- Enter the **current web password** to confirm restore. After reboot use the
  **restored** password and network settings. Check your router's DHCP list if
  the old address no longer works. Review restored SSH host keys/fingerprints
  rather than disabling host-key checking globally.
- A failed upload or cancelled confirmation does not change settings. If a
  maintenance request loses its response, check device status before retrying.
  Temporary validated uploads/downloads expire after ten minutes.
- Restore requires spare data space. A pre-restore store remains private on the
  receiver; use the [operations recovery guidance](operations.md#backups) if
  activation cannot complete. Do not delete transaction markers blindly.
- EPG caches, live sessions and time-shift history are not restored. Reconnect
  the saved USB drive and allow programme information to refill.


[Documentation index](README.md) · [Operations](operations.md)

## Start safely

Use SSH and read status/logs first. Do not run an extra player, stream probe,
learner, grabbing verifier, scan, or restart merely to collect information.
Those actions can interrupt viewing or compete for the sole tuner.

The appliance uses BusyBox init and owned service supervisors. Read both the
service and supervisor logs: a supervisor can be waiting for data or a tuner
before its service starts. See [splash diagnostics](hdmi-startup-screen.md#diagnostics)
and [service lifecycle](buildroot-services-persistence.md). Run as root over SSH:

~~~sh
cat /data/store/installation.json
cat /run/satip-boot-stage
cat /run/satip-boot-error
tail -n 80 /run/satip-supervisor/player.log
tail -n 80 /run/satip-supervisor/player.supervisor.log
tail -n 80 /run/satip-supervisor/web.log
tail -n 80 /run/satip-supervisor/tvheadend.log
netstat -lnt
ls -l /dev/dri/by-path
cat /sys/class/thermal/thermal_zone0/temp
~~~

Temperature is reported in millidegrees Celsius. `vcgencmd` is not bundled;
power/throttling telemetry may be unavailable. Missing boot-error/log files are
not by themselves a failure. The Status page at `http://APPLIANCE_IP:8080/`
and `GET /api/status` are non-tuning; use the configured browser login.
`--check` validates player files; diagnostics lists devices without grabs.
Neither proves physical output. See [operations](operations.md#updates) for
running these as the player account.

## Performance monitor

Use [Status → Performance → Open performance monitor](usage.md#performance-monitor)
to open the graphs in a window and correlate playback
problems with per-core CPU, memory, network traffic and disk activity. An average
CPU reading can hide one busy core; process CPU can exceed 100% because it is
measured per core. Disk traffic may include time shifting and filesystem cache
flushes; it is not a measurement of a particular application's file I/O.

Wait for two samples (about five seconds) for rates. Returning after a long gap
requires a fresh baseline. Missing sensors/process permissions are not zero
usage. Stale badges retain the last reading; graph gaps are not measurements.
History clears on browser reload, and no monitor log is written to the SD card.
The monitor does not change tuner ownership or prove HDMI picture/audio.

## German SAT.1, ProSieben or kabel eins missing

Run a **full channel search**, not an enabled-only search or catalogue refresh.
Older software imported only one of several Astra spacecraft tables because
Tvheadend returned colliding scanfile identifiers. In particular, 12545 MHz
horizontal (DVB-S, 22000 kSym/s) was absent even though Austrian variants on
12051 MHz vertical were found. Updated full searches add missing transponders
from all installed Astra 19.2°E tables without resetting setup or changing
favourites. Searches interrupt TV; wait for completion, then add the German
channels to your favourites and save. Their encrypted HD variants are not
supported. See [scan internals](tvheadend.md#scan-and-favourite-save-mutations).

## Common symptoms

| Symptom | First checks | Next action / caution |
|---|---|---|
| Service fails immediately | Config schema, absolute paths, credential permissions, service-account directory access | Correct only reviewed files; no second instance |
| Black HDMI output | Is player paused/loading? Did splash handoff complete? Does configured connector/mode exist? | Read splash/player/HDMI logs; a supervised player restart interrupts TV |
| Picture but no audio | Configured ALSA route, monitor mute/volume, main MP2 PID, application mute | Device initialization is not proof of audible sound; do not select an unqualified codec by guess |
| “Cannot receive TV” retries | Backend service/listeners, signal/SAT-IP availability, requested mapping/ACL, main audio metadata | Message covers several possible layers; it is not an exact diagnosis |
| TV remains paused | Active time-shift pause, preview, scan, or recovery-pending status | Resume time shift, stop owned preview, or retry scan recovery; never delete journal/lease blindly |
| Remote/keyboard does nothing | Selected node identity, udev permissions, service grabs, overlay routing | Input learners/verifiers require stopping active player first |
| Up/Down scroll instead of switch | Programme details open | Close with I/Menu or Esc |
| Pause is unavailable | USB time-shift storage is not configured or not ready | Check Settings storage readiness; Space/Pause or Play/Pause controls time shifting, and Left/Right controls volume |
| Time shift unavailable | Settings storage readiness, actual writable USB ext4 mount, free space | Configure storage through Settings; never use an ordinary SD directory as a fallback |
| EPG missing | Current mux reception, passive EIT mapping, cache age, system time | Wait for collection; do not enable dedicated EPG tuning just to speed it up |
| Channel missing in catalogue | FTA/codec/German MP2 eligibility, partial/failure counts, saved backend metadata | Non-tuning refresh is different from a new scan; discovery is not qualification |
| New channel has no preview icon | It has not been mapped/saved | Add → Save → Preview |
| Save controls remain disabled | Scan/preview/recovery, request still pending, connection feedback | API deadline is 30 seconds; save may commit despite a lost response |
| Preview starts but does not play | Browser autoplay, HLS support, session readiness, chosen mapped channel | Press Play; stop/retry after cleanup; other channels may be unqualified |
| Reloaded page cannot resume preview | Page no longer owns old session token | Wait for heartbeat timeout or supervised web cleanup |

If the early splash works but video goes black after moving the HDMI cable,
compare `/sys/class/drm/card*-HDMI-A-*/status` with the player configuration.
The player falls back from a disconnected Pi HDMI port to the connected
one at mpv startup and logs `HDMI output ... disconnected; using ...`.
An empty EDID with only basic modes
in `modetest -M vc4 -c` indicates missing receiver/TV display capabilities.
The Buildroot boot helper preserves valid real EDID with a usable progressive
1080p50/60 mode. It applies `/lib/firmware/edid/satip-1080p.bin` only to ports
with missing/invalid EDID or no usable 1080p rate, assuming 1080p/stereo PCM
support. Check `/run/satip-hdmi.log` and `/run/satip-hdmi.json` for the decision.
For the default 1080p setting, the player falls back only between 50/60 Hz,
not to a lower resolution. Explicit 720p needs real display support; see
[configuration](configuration.md#player-configuration). Change the display
chain and reboot to reconsider EDID; do not rerun preparation during playback.
A plain `video=...:1920x1080@50` override is not equivalent: it may generate
a non-CTA timing and does not declare HDMI audio. Older unconditional-profile
cards must remove the bundled `drm.edid_firmware=HDMI-A-1:edid/satip-1080p.bin,HDMI-A-2:edid/satip-1080p.bin`
option after installing the conditional helper and matching application code.
An explicitly configured administrator override is otherwise preserved.
With an AV receiver, also check the selected HDMI input, TV/receiver power and
HDMI cables. Missing EDID can affect audio as well as video; successful mode
initialization is not proof of audible sound. `load_failed` can also result from
video-output initialization, not just a rejected stream; backend subscriptions
alone do not prove HDMI readiness.

## Picture starts, then “TV playback unavailable” repeats

Check the fixed reason in `/run/satip-supervisor/player.log` or
`/run/satip-status/status.json`; the translated message is not a diagnosis.

- **`audio_pid_missing`:** video can already be decoding while the saved main
  audio PID is absent. At the default 20-second load deadline the controller
  stops the stream and retries, even when mpv auto-selected audible audio.
  HDMI accepts an unambiguous, explicitly identified normal German MP2
  track when the saved PID is missing; it never guesses among accessibility
  tracks or other codecs. A warning gives the saved/live PID. Refresh the
  channel catalogue and save updated metadata; see
  [audio fallback policy](configuration.md#saved-audio-pid-and-live-fallback).
- **`playback_stalled`:** playback stopped advancing for the default 10 seconds.
  Look for corresponding Tvheadend `no data received, restarting RTSP`, tuning
  failures or new transport continuity errors. These indicate reception/transport
  trouble, not a stale audio PID. Compare error-counter deltas over the same
  interval. A strong Wi-Fi signal or high negotiated bitrate does not rule out
  packet loss; compare Ethernet when practical, without disabling your only
  SSH recovery connection. Do not lengthen deadlines or disable recovery merely
  to hide the symptom.
- **Backend `Write failed -- Resource temporarily unavailable`:** the local
  stream consumer did not drain the socket fast enough. This alone does not
  distinguish decoder blockage, a paused consumer or another cause; inspect
  player status and progress. It is not proof of SAT-IP tuner failure.

Initial TV loads remain paused while mpv probes both audio and video. The gate requires
verified normal German audio and at least one second of selected-stream cache. This avoids discarding
early audio packets by attaching an audio track after video has already begun;
it does not override a user pause during established time-shift playback.

When USB time-shift storage is configured, ordinary live viewing also uses the
RAM prebuffer relay, even before Pause. A time-shift regression can therefore
affect live TV. On a playback stall, the player logs a secret-free
`Playback stall diagnostics` snapshot (read this alongside the retry warning):

- `time-pos`, `audio-pts`, `avsync`, volume and mute describe the player;
  unsupported audio timestamp properties appear as null.
- `relay_age` and `relay_bytes` describe receipt of original TS data.
- `upstream_age`, `capture_write_age` and `capture_queue_chunks` describe the
  background prebuffer writer. Capture readings update about once a second.
- `relay_overflow` indicates a bounded live-reader queue filled; it is not
  proof of a tuner fault.

The prebuffer writer has a bounded, independent input queue. A blocked FFmpeg
pipe must not block the upstream live-data pump. Queue overflow fails capture
explicitly and requests recovery rather than dropping transport packets into a
corrupted time-shift buffer. Capture/relay failure returns to direct live playback
first, with a 30-second initial delay before retrying the prebuffer path. These protections do not establish the cause of
an earlier failure or prove audible HDMI output.

No extra stream probe is needed to inspect status. Use
[recovery policies](recovery.md) to distinguish audio-metadata fallback from
independent transport failure.

## Slow startup after pulling power

The external SAT-IP tuner may keep a session when the Pi disappears without
sending RTSP TEARDOWN. A reachable server and registered tuner do not guarantee
that a new RTSP SETUP will be accepted. Temporary RTSP 503 responses alone do
not prove why a tuner refused the stream.

Read the backend log for RTSP SETUP errors and the player log for retry timing.
During the first minute of the player lifetime, reception retry waits are capped
at 3 seconds, with a stable connecting/waiting message. After that, normal backoff
and diagnostic warnings apply. See [startup behaviour](hdmi-startup-screen.md).
A healthy waiting appliance is not rebooted because the external tuner is busy.
This cannot shorten a tuner-side session timeout or protect the SD card against
power-loss damage. Use an orderly shutdown when possible; power-cut durability
and repeated recovery still require qualification.

## Soft SD picture

HDMI MPEG-2 SD playback deinterlaces with double-rate BWDIF, applies gentle
luma-only sharpening at source resolution, then uses software bicubic scaling
to square-pixel 720p. The fast renderer enlarges this to the configured HDMI
mode (normally 1080p50); the HDMI mode itself is unchanged. Broadcast 4:3 and
anamorphic 16:9 aspect ratios are retained. HD, YouTube and browser preview do
not use this SD filter chain. These are fixed playback policies, not browser
settings.

The processing can improve perceived edge clarity, but cannot restore missing
broadcast detail. Compression blocks, noise and halos may remain visible; avoid
stacking aggressive TV sharpness or edge-enhancement settings on top. Compare
faces, captions and moving footage on the actual TV. Use an HD version of the
channel when available for a larger improvement.

Selecting a GPU scaler while keeping `gpu-dumb-mode=yes` does not enable it:
mpv uses bilinear in that mode.
The software 720p stage avoids that limitation without enabling expensive GPU
processing. Check dropped-frame counter changes, A/V sync and temperature
when validating on another receiver or display.

## Browser HTTP errors

- **401:** login required, missing/wrong browser credentials. Backend-admin login
  is not necessarily the browser login.
- **400 from host checking:** use the receiver's current local IPv4 address or
  hostname. Appliance host validation tracks those dynamically; a proxy alias
  is not automatically trusted.
- **403 on actions:** missing action header or cross-origin request. Do not
  disable protections to solve a client mismatch.
- **409:** exclusive operation/lock/session conflict. Check status before retrying.
- **410 preview:** session expired/stopped; start a new session after cleanup.
- **503:** backend/recovery/save unavailable; old favourites may remain while
  already-completed backend mutations require review.

The UI polls for reconnect and preserves unsaved edits. Reload loses page-local
edits. If preview stalls, it can pause HDMI until explicit stop or timeout.
Check served assets after updates and reload once to load changed JavaScript.

## Distinguish the failing layer

1. **Signal/tuner:** lock, correct satellite/source, external server, competing clients.
2. **Transport/backend:** SAT-IP RTP/RTCP, Tvheadend subscription, local stream auth.
3. **Decode/filter:** H.264 hardware copy decoder or software MPEG-2/BWDIF.
4. **Display/audio:** active VT, stable connector/mode, ALSA route, monitor controls.
5. **Controller/UI:** valid library, selected audio PID, generation/watchdog/lease state.

Read backend status using protected admin access; playback deliberately lacks
admin API rights. Advancing playback matters more than `vo-configured` (true
while idle) or a single backend `bps` reading.
Do not interpret cumulative error counters as errors in the current interval.

## Escalation and recovery

Collect redacted service status, software versions, symptom timing, selected
channel, and whether scan/preview/details were active. Do not attach private
backups/captures or raw authenticated logs. Record what was actually tested,
not just successful property queries.

A restart, backend stop/start, physical unplug, scan, or reboot is a disruptive
test: arrange supervision and SSH recovery first. Never disconnect the only
Wi-Fi recovery link to simulate failure. Restore only owned services/files;
[rollback](operations.md#rollback) describes file and card recovery.
