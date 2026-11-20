# Validation and limitations

**Audience: developers and advanced administrators.** Current capability and
qualification reference, not a chronological deployment record.

[Documentation index](README.md) · [Development](development.md) · [Validation tools](../validation/README.md)

The appliance has been tested on real Raspberry Pi 4 hardware, where its core
reception, playback, control and recovery features run reliably. SAT-IP, Enigma2,
USB time shifting and YouTube are supported within their documented compatibility
limits. The image profile remains a development profile: a full current-source
image build, fresh-card setup and the complete hardware qualification matrix below
are not yet formalized, and automated tests plus in-place device checks do not by
themselves constitute a complete physical acceptance sign-off.

## Evidence levels

- **Automated tests:** isolated unit, fake backend/input, IPC, API and UI behavior.
- **Build/offline checks:** configuration, compiled features, hashes, credential
  policy and image contents. A verified artifact still needs hardware acceptance.
- **Live programmatic checks:** stream progress, selected audio PID, decoder mode,
  resource ownership or browser decoding on the identified hardware.
- **Physical acceptance:** observed picture, sound, readability and actual controls.

Record these separately. Repeat affected checks after changes to the kernel,
Mesa, mpv, FFmpeg, backend, application or hardware.

## Capability and acceptance matrix

| Area | Current implementation | Qualification still required |
|---|---|---|
| Image and first setup | Integrated backend/player/web; SAT-IP discovery/tuner scan or Enigma2 discovery/bouquet import; shared favourites ordering | Full current-source build, fresh-card setup, cancellation/retry, reboot and persistence |
| H.264 HD | Raspberry Pi FFmpeg backport with V4L2 M2M copy decoding | Physical picture/audio, interlaced channels, sustained playback and thermal headroom |
| MPEG-2 SD | Software decode, double-rate BWDIF, mild luma sharpening and aspect-preserving 720p bicubic scaling | Interlaced/progressive and aspect-ratio matrix, physical quality, A/V sync and sustained frame timing |
| HDMI and startup | Connected-port selection, real-EDID retention, conditional 1080p/stereo fallback and early splash handoff | Both ports, displays/AV receivers, cold start, late connection and visible handoff on the current image |
| Input and OSD | Selected USB keyboard/GPIO remote, channel/favourites/details controls, English/German OSD | Actual buttons/repeats, readable layouts, audible volume and timing on the intended TV |
| Browser TV | Exclusive HLS preview with idle cleanup and physical-input takeover | Current-image media stack, supported desktop/mobile browsers, interruption cleanup and endurance |
| EPG | Passive broadcast EIT, hourly backend persistence and RAM-cache warm-up | Reboot/time-sync behavior, a full save cycle and power-loss durability |
| Device settings | Authenticated identity, passwords, SSH, Wi-Fi trial/rollback and maintenance controls | Real credential/access changes, different-network confirmation, helper-restart rollback, reboot/shutdown and fresh-image persistence |
| USB time shifting | Rolling TS buffer, explicit paused-expiry policy and separate safe-remove/forget actions; see [qualification](timeshift-validation.md) | Real formatting, HD/SD pause/seek, limits, unplug/read-only/full media, reboot remount and endurance |
| YouTube | TV-code reception, selected public H.264/AAC videos and return to TV | Phone/video compatibility matrix, QR app handoff, physical output, cold boot and endurance |
| Recovery | Owned-service crash/freeze detection, targeted restart and independent hardware watchdog | Current-image failure matrix, hard reset, scan interruption, storage failures and endurance |
| Configuration backup/restore | Authenticated, bounded, quiesced export and journaled restore with reboot; [qualification checklist](backup-validation.md) | Current-source image/fresh-card restore, other compatible devices, network migration, USB selection, power-loss/storage-full campaigns |
| Persistent import | Transactional allowlisted import with root validation | Compatibility of an actual source database with the pinned backend and post-import device behavior |

The [image guide](../buildroot/README.md), pinned
[defconfig](../buildroot/external/configs/satip_rpi4_64_defconfig) and
[dependency fixture](../tests/buildroot-requirements.txt) identify build inputs.
No guaranteed switching latency, power-to-picture time, zero-drop playback or
long-duration reliability is claimed.

## Supported versus qualified channels

Discovery offers free-to-air H.264/MPEG-2 TV with German MP2 main audio according
to the [candidate filter](tvheadend.md#supported-channel-policy). A mapped channel
or preview icon is not compatibility proof. Interlaced H.264, type-1 German audio
fallback and unknown video dimensions require playback acceptance. Other tuners,
remotes, displays and browsers need their own checks.

HD uses `v4l2m2m-copy`; software recovery does not prove hardware decoding. An IPC
timeout can select software decoding for the controller lifetime; restarting the
controller retries hardware. Passive EIT can take a minute or more on a newly
visited mux, and unvisited transponders may have no cached programme information.

## Reproducible validation procedure

### 1. Run isolated checks

Follow [development tests](development.md#automated-tests), including the relevant
Python, Node and isolated browser suites. Review platform skips. Use
[synthetic media and OSD tools](../validation/README.md) for local rendering and
codec checks; they generate disposable files without tuning or opening HDMI.
Build and verify the image using the [image guide](../buildroot/README.md).

### 2. Qualify a fresh card

Keep the working card intact. Prepare a separate card using
[first boot](buildroot-first-boot-setup.md), including automatic factory-empty initialization, optional
web-password activation and actual keyboard/IR controls. Check setup authentication, discovery and
manual registration, tuner selection, scan cancellation/recovery and lineup save.
Verify HDMI picture/audio, then reboot and confirm saved settings, favourites,
access and startup behavior. Check EDID/connector selection through the
[HDMI diagnostics](hdmi-startup-screen.md).

### 3. Observe playback and ownership

Use representative HD/SD channels and inspect picture, sound, aspect ratio,
programme information, buttons, volume and OSD readability. Observe repeated
switching, frame drops, A/V sync and temperature over a recorded duration.
The [passive monitor](../validation/monitor_playback_on_pi.py) distinguishes
cache pauses from rendering drops without opening another stream.

Use the [validation tool index](../validation/README.md) for the running-player
switching benchmark and live browser checks. Verify that preview and scans pause
HDMI, releasing the tuner, and that stop/timeout/physical takeover restores TV
without leaving owned child processes or stale leases. Do not start a second
controller or input-grabbing learner alongside the player.

### 4. Exercise settings, storage and recovery

Begin with read-only device checks, then separately schedule disruptive actions.
Keep independent recovery access available for Wi-Fi trials and credential/SSH
changes. Use disposable USB media for [time-shift qualification](timeshift-validation.md).
Test [YouTube live checks](youtube-internals.md#live-validation) with actual phone controls as well as
the synthetic sender.

Follow the bounded [service recovery procedure](recovery.md#diagnosis-and-safe-tests)
for crash/freeze checks. Never signal the hardware-watchdog feeder during an
ordinary service test. Hard-reset and missing/full/read-only data tests require
a separate disposable-card procedure. A root-supervisor freeze/exit is recovered by its owning guardian; loss of the
guardian or unsafe local cleanup escalates through the independent feeder. See
[recovery limits](recovery.md).
After each interruption, verify service cleanup, playback, saved data and access.

### 5. Record reviewable evidence

Identify the source commit, dirty-state status, image manifest, hardware and
software versions, exact procedure, duration and expected result. Distinguish
physical observations from software counters, and list skips or untested cases.
Keep raw logs, captures, device identities, credentials, browser profiles and
backup inventories private. Publish only reviewed, sanitized evidence tied to
the qualified source/image; do not treat old in-place results as fresh-image proof.

## Out of scope / unavailable

There is no UHD/HEVC, encrypted-service support, recording scheduler, full browser
EPG grid, simultaneous HDMI/browser viewing or general multi-tuner reservation.
Time shifting retains a bounded rolling history: reaching a retention limit
evicts old content without ending the session. Channel changes, Return to live,
restarts and other viewing modes discard history.

The image has no U-Boot/A/B, SWUpdate, data-preserving firmware updater or
qualified cross-version database migration. Browser configuration and channel
backup/restore are available within their compatibility restrictions. Whole-image flashing replaces
persistent data. Use [operations](operations.md) for backups and recovery.

## Hard-power-cut startup retry checks

`tests/test_splash.py` includes startup retry tests with simulated stream rejection,
a fixed monotonic first-minute window, a 3-second retry-wait cap, stable connecting
artwork/text fallback and unchanged non-reception/post-playback recovery.
These simulated checks do not establish physical power-cut recovery.

For a backed-up physical repeat test, unplug/reconnect only with supervision and
SSH recovery available. Record time from power-on to confirmed picture/audio,
RTSP failures and retry waits, configuration/library integrity and process ownership.
The tuner may retain an old session after the Pi disappears: recovery speed cannot
be inferred from normal service restart. Do not reset the external tuner or steal
other clients' sessions merely to improve the measurement.

## Root-supervisor recovery qualification

Use [exercise_recovery_on_pi.py](../validation/exercise_recovery_on_pi.py) with
`--target supervisor` for both `--failure crash` and `--failure freeze`, for
player, web and Tvheadend. The tool requires root, healthy initial playback/API,
no active scan/preview/recovery, and `--confirm-interrupt`. It checks a new
supervisor and useful service, old group removal, and stable configuration hashes.
Add `--preview-before-failure` for web-supervisor tests to generate HLS before
the fault and require HDMI-controller recovery afterwards. See the current
[recovery policy](recovery.md#root-supervisor-guardians).
Repeat actual browser decoding and physical takeover/return separately; an HTTP health response is not
physical HDMI acceptance. Linux isolated tests cover adopted descendants escaping
sessions, unrelated-process preservation and crash/freeze cleanup.

Never crash/freeze a guardian or the hardware feeder as an ordinary recovery test:
that intentionally invokes hardware reset. Such testing requires a separate
interrupted-power/reset qualification procedure. A software feed-withholding test
is not proof of a physical reset. Current-image qualification remains separate.
