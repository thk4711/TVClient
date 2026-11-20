# Validation tools

**Audience: developers and advanced administrators.**

[Development and tests](../doc/development.md) · [Acceptance checklist](../doc/validation.md)

These tools are not a history of test runs. Fake tests and synthetic fixtures
check software contracts; live checks require an explicitly selected receiver
and do not establish physical HDMI/audio quality or fresh-image acceptance.

## Safety

Before a check that starts a controller or grabs physical input, stop the
production player through its owned supervisor and arrange bounded SSH recovery.
Never run a second player, input learner or grabbing verifier alongside it.
The switching benchmark instead requires the existing player to remain active.

Live preview, switching, crash/freeze and YouTube checks interrupt TV. Obtain
approval first, finish scans/Wi-Fi trials/recovery, and retain SSH access. Do not
unplug internal IR wiring live or disable your sole network recovery route.
Keep generated traces, captures, profiles and screenshots private and outside
the source checkout. They may contain device or network identifiers.

## TV-source Settings and SAT-IP tuner selection

The read-only live Settings check uses the real application and blocks non-GET
requests. It currently expects the two-tuner SAT-IP test setup with tuner #2
receiving. Selecting dropdowns is local only; it never applies a tuner/source.
Use a new disposable Chrome profile:

~~~sh
SATIP_SETTINGS_URL='http://RECEIVER_IP:8080/#settings' \
CHROME_BINARY='/path/to/Chrome' \
CHROME_USER_DATA_DIR='/private/path/new-settings-profile' \
node validation/check_source_settings_browser.cjs
~~~

`tests/test_satip_tuner.py` checks inventory, policy copying, lineup preservation,
refusals and recoverable rollback. `tests/test_device_ui.cjs` and
`tests/test_source_browser_ui.cjs` cover explicit actions, source-specific help,
no-data/unavailable status and preserving unsaved tuner selection during polling.

`exercise_tuner_settings_on_pi.py --confirm-interrupt` is a separate opt-in
root-on-Pi acceptance script for a login-free configured SAT-IP receiver. It
switches to another tuner through the queued device API, checks stream data,
player readiness and lineup/configuration hashes, and restores the original
tuner in a finally block. **Both tuners must have working satellite cables**
for full reception acceptance. The current test device's tuner #1 cable is
disconnected; do not run the round-trip reception test until it is connected.
The hardware run verified reassignment/restoration and working tuner #2, not
successful reception on tuner #1. No scan/import is started by this script.

## User manual screenshots

`capture_manual.cjs` captures eleven browser illustrations without receiver actions.
It requires Node 22+, Chrome, an explicitly selected home-LAN receiver and its
existing root SSH access. Use a new private browser profile and capture directory
outside the checkout:

~~~sh
CHROME_BINARY='/path/to/Chrome' \
CHROME_USER_DATA_DIR='/private/path/new-manual-profile' \
MANUAL_SCREENSHOT_DIR='/private/path/manual-captures' \
SATIP_DEVICE_URL='http://RECEIVER_IP:8080' \
node validation/capture_manual.cjs
~~~

The script reads optional browser credentials over SSH into memory, blocks
non-GET requests to the receiver and does not start streaming, search, pair, save
or reset setup. First setup uses the real interface against an isolated localhost
fixture covering SAT-IP scanning/favourites and Enigma2 address/bouquet selection.
Live pages retain the selected source; do not switch sources merely to
illustrate the manual. Inspect and anonymise every screenshot before deliberately
copying the reviewed PNGs into `doc/images/`. Re-capture after relevant UI changes.

The TV welcome illustration can be generated through the existing native
`/usr/bin/satip-splash --frame 1280 720 5 http://192.168.1.42:8080/ en` export
over SSH. Convert the stdout BGRX pixels to a PNG with Pillow. This uses an example
URL, does not open DRM or replace the running TV framebuffer, and is not a
photograph or proof of HDMI quality. Never publish a real pairing QR code.

Run `python3 scripts/render_manual.py` after changing the Markdown source.
`python3 -m unittest tests.test_manual tests.test_documentation` checks HTML
parity, captioned image inventory, links, installation, authentication and the
allowlisted read-only routes.

Generate the four labelled diagrams with `python3 scripts/draw_manual_diagrams.py`
(Pillow and a TrueType font; optional `--font` and `--output`). The Wi-Fi export
uses native `--frame 1280 720 38` with an example onboarding page produced by
`satip_appliance.onboarding.page`; it never starts a real Wi-Fi trial. Preserve
3.3 V and physical/GPIO numbering warnings in the infrared schematic.

`check_manual_browser.cjs` validates the HTML in real Chrome: all eighteen
captioned image occurrences (seventeen unique visual assets),
contents/reference links, 1200px/390px layout, print rules and generated PDF.
Set `SATIP_MANUAL_URL` to the receiver's `/user-manual.html` URL or to the local
`file:///…/doc/user-manual.html` for an offline check. Also set `CHROME_BINARY`,
a new `CHROME_USER_DATA_DIR` and optional private `MANUAL_EVIDENCE_DIR` for the
print PDF. HTTP checks use existing root SSH to read optional login credentials
into memory. No receiver actions are sent, and no external assets are required.
Optional Imager/Argon website references are checked against an allowlist but are
not fetched, so the document remains readable offline.

## Tool index

| Tool | Purpose and boundary |
|---|---|
| [capture_manual.cjs](capture_manual.cjs) | Read-only live browser screenshots and isolated synthetic first-setup illustrations; inspect/anonymise before publishing |
| [check_manual_browser.cjs](check_manual_browser.cjs) | Offline/live manual links, images, mobile layout and print/PDF; GET-only |
| [exercise_channel_backup_on_pi.py](exercise_channel_backup_on_pi.py) | Root-only opt-in real channel backup/restore API exercise; briefly interrupts TV, temporarily rotates favourites/narrows the catalogue, restores its original archive in cleanup, checks device settings and no reboot; private original/evidence retained under `/root/satip-deployments` |
| [qualify_buildroot_setup.py](qualify_buildroot_setup.py) | Opt-in native Linux bootstrap/API test with a separate native Tvheadend binary; temporary loopback backend, no tuner or target execution |
| [exercise_mux_grace_release_on_pi.py](exercise_mux_grace_release_on_pi.py) | Explicitly interrupt HDMI, verify bounded idle-mux release and restore player/channel; requires enabled native grace and no other validation |
| [observe_satip_sessions_on_pi.py](observe_satip_sessions_on_pi.py) | Bounded passive RTSP observer; root-only, sanitized methods/session hashes, no tuning or packet archives |
| [render_osd.py](render_osd.py) | Synthetic ASS/PNG layout previews; no TV, IPC or input access |
| [buildroot_media.py](buildroot_media.py) | Generate synthetic HD/SD TS and check preview arguments against local files; needs FFmpeg/ffprobe |
| [learn_apple_remote.py](learn_apple_remote.py) | Guided GPIO IR capture and optional keymap loading; writes private outputs |
| [verify_apple_remote.py](verify_apple_remote.py) | Grabs only selected GPIO IR and checks mapped taps/repeats |
| [capture_ir_button.py](capture_ir_button.py) | Single-button capture using existing private learned state; not a fresh learner |
| [check_controller_on_pi.py](check_controller_on_pi.py) | Starts the controller/mpv/input and changes saved state; stop production first |
| [measure_switching_on_pi.py](measure_switching_on_pi.py) | Running-player benchmark via temporary virtual keyboard; changes channels, attempts restoration |
| [measure_timeshift_seeking_on_pi.py](measure_timeshift_seeking_on_pi.py) | Opt-in running-player pause and rapid/opposite seek timings through virtual remote events; requires live RAM mode and --confirm-interrupt, restores live, does not change USB settings |
| [exercise_prebuffer_backpressure_on_pi.py](exercise_prebuffer_backpressure_on_pi.py) | Opt-in live-RAM test: freezes only the owned prebuffer FFmpeg via pidfd, verifies direct recovery, and resumes it in cleanup; requires root and --confirm-interrupt; never run alongside switching tests or physical controls |
| [exercise_recovery_on_pi.py](exercise_recovery_on_pi.py) | Opt-in owned-service crash/freeze test; requires healthy idle services and interruption confirmation |
| [monitor_playback_on_pi.py](monitor_playback_on_pi.py) | Passive allowlisted mpv/cache/rendering trace; no tuning or option changes |
| [check_preview_browser.cjs](check_preview_browser.cjs) | Authenticated live Chrome preview for explicitly supplied service UUIDs; interrupts HDMI |
| [check_device_settings.py](check_device_settings.py) | Read-only settings/storage API checks by default; explicit flags perform bounded exercises |
| [check_device_browser.cjs](check_device_browser.cjs) | Live desktop/mobile settings and masked-dialog checks; no device actions submitted |
| [check_youtube_on_pi.py](check_youtube_on_pi.py) | Synthetic Lounge sender against enabled production receiver; interrupts TV and checks restoration |

## Short pauses after channel switches

The switching benchmark samples `paused-for-cache` and cache duration every
100 ms during settling. Each timed row includes `cache_pause_count`,
`sampled_cache_pause_seconds` and `min_cache_seconds`; these are sampled
software buffering measurements, not physical HDMI freeze durations. Initial
playback readiness alone does not establish an uninterrupted start. Use
`--settle-seconds 10` to include the first seconds after readiness.

Before promoting a buffering change, compare opposite phase orders:

~~~sh
PYTHONPATH=/opt/satip SATIP_RUNTIME_ROOT=/run \
python3 /opt/satip/validation/measure_switching_on_pi.py \
  --confirm-interrupt --rounds 1 --phase-blocks 2 --settle-seconds 10 \
  --channels '3sat HD' 'DMAX' 'ntv' --compare-initial-buffer
~~~

This runs baseline/candidate then candidate/baseline and restores the previous
mpv options and original channel. Use only favourites present on that device.
It changes channels; arrange no other input, preview or scan during the run.
It neither changes Wi-Fi nor creates an additional tuner stream. This option
compares the current initial-cache option. The harness rejects it when initial
buffering is already enabled (including the new file-local policy) or the
active option differs from the reported options value: changing an option
between tunes would not override the live-load policy. Retain
pre-change traces and compare installed application versions instead.
After the first warm-up it also verifies that the loaded file really applied
the intended option, rejecting a live-load override before emitting timings.

`--compare-timeshift` independently compares direct playback with the RAM
relay. It temporarily revokes the storage-readiness marker permissions, then
restores them; it does not change persistent USB configuration. Do not combine
the two comparisons or run them concurrently.

## Fake and synthetic checks

From the project root:

~~~sh
python3 -m unittest discover -s validation -p 'test_*.py' -v
python3 -m validation.render_osd /absolute/disposable/osd-previews
python3 -m validation.buildroot_media generate /absolute/disposable/fixtures
python3 -m validation.buildroot_media preview /absolute/disposable/results \
  --fixtures /absolute/disposable/fixtures
~~~

Use new output paths. PNG rendering requires FFmpeg with libass; OSD
`--ass-only` needs only Python. Media checks need FFmpeg and ffprobe. These
commands neither tune a receiver nor establish physical readability or listening.
For isolated Chrome regressions against fake APIs, see
[development](../doc/development.md#isolated-real-browser-regression).

## Appliance settings

With provisioned passwordless root SSH and host-key verification:

~~~sh
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes root@<receiver-ipv4> \
  'PYTHONPATH=/opt/satip SATIP_RUNTIME_ROOT=/run python3 -' < validation/check_device_settings.py
~~~

Append `--exercise` to the remote Python command to check rejected
requests, save unchanged hostname/timezone and scan Wi-Fi. It does not change
passwords or disable Wi-Fi. `--wifi-trial` interrupts connectivity and TV,
tries the current saved network and waits for rollback; arrange approval/recovery.

For read-only real-browser layout/dialog checks, use Node 22+ and a new profile:

~~~sh
SATIP_DEVICE_URL=http://<receiver-ipv4>:8080/ \
CHROME_BINARY=/path/to/chrome \
CHROME_USER_DATA_DIR=/new/disposable/profile \
node validation/check_device_browser.cjs
~~~

Login is read over root SSH into process memory. No passwords are placed in
argv or browser storage. Optional `DEVICE_SCREENSHOT_DIR` writes private
screenshots. Actual password changes, SSH removal, reboot/shutdown and storage
formatting remain separate acceptance checks.

## Live browser preview

Select service UUIDs from the target's authenticated channel catalogue, using
free-to-air mapped services supported by the preview policy. Do not use test
fixture identifiers. Direct trusted-LAN access is required; the image disables
SSH forwarding. Root SSH supplies the login privately in memory.

~~~sh
SATIP_PREVIEW_URL=http://<receiver-ipv4>:8080/ \
SATIP_PREVIEW_SERVICES=<service-uuid>,<another-service-uuid> \
SATIP_CONFIRM_INTERRUPT=yes \
CHROME_BINARY=/path/to/chrome \
CHROME_USER_DATA_DIR=/new/disposable/profile \
node validation/check_preview_browser.cjs
~~~

Video-frame/audio-decode observations do not prove physical listening or
endurance. Verify HDMI resumes after every test, including failures.

## Running-player diagnostics

Run as root on the Buildroot Pi, with reviewed validation sources available.
Keep the player running and avoid physical controls, favourites edits, scans and
preview during the switching check. Names must exactly match current favourites.

~~~sh
PYTHONPATH=/opt/satip SATIP_RUNTIME_ROOT=/run \
python3 validation/measure_switching_on_pi.py --confirm-interrupt --rounds 2 \
  --channels 'Channel A' 'Channel B'

PYTHONPATH=/opt/satip SATIP_RUNTIME_ROOT=/run \
python3 validation/monitor_playback_on_pi.py --seconds 60 > /path/to/private/trace.json
~~~

The benchmark requires evdev, /dev/uinput and matching virtual-keyboard access.
It measures software readiness proxies, not physical first picture/audio.
Option comparisons are opt-in experiments, not production recommendations.
For independent saved-audio, probing, coalescing and IPC comparisons, see the
[bounded channel-switch experiments](../doc/channel-switching.md#diagnostic-overrides).
The independent [persistent-relay comparison](../doc/channel-switching.md#persistent-relay)
uses `--small-experiments persistent_relay`, records supervisor/capture PIDs and
rejects candidate fallback; it is excluded from the stage-one combined preset.
Relay reuse is the application default; the comparison explicitly disables
it in the historical all-off baseline and stage-one phases. Restoration of an
absent override file returns to the enabled default.
Use one `--small-experiments` candidate with `--phase-blocks 2` (or another
even count) for counterbalanced baseline/candidate repeats. `--phase-order`
selects the starting order; `--rounds` applies to each phase in each block.
Keep the ordered channel/transponder matrix and settling time identical across
invocations. Repeated mode warms all selected channels and emits block/round/
transition labels plus descriptive summaries. Failures invalidate the complete
comparison rather than becoming omitted or zero-duration samples. Keep physical
controls, rapid-zap checks, preview and scans separate from these timing runs.
Restoration is attempted on ordinary exceptions, Ctrl-C and SIGTERM; SIGKILL,
lost access or concurrent edits can prevent it. Verify final channel/options and
service health. Never loosen permissions on the private mpv socket to monitor it.

For controller, recovery and YouTube invocations and acceptance boundaries, see
[validation](../doc/validation.md), [recovery](../doc/recovery.md) and
[YouTube](../doc/youtube.md).

## Apple Remote A1294

Stop the production player before learning or grabbing verification. Run as root
with reviewed sources and optional `--prompt-tty /dev/tty1` for HDMI prompts:

~~~sh
PYTHONPATH=/opt/satip python3 validation/learn_apple_remote.py
PYTHONPATH=/opt/satip python3 validation/verify_apple_remote.py
~~~

The learner discovers GPIO IR by name, writes a new private output directory,
and can resume with `--seed /path/to/private/learned.json`. It captures
whole bursts and excludes the shared Centre/Play trailer. Pairing is not changed.
The compact keymap covers observed remote IDs; relearn after changing pairing.
All-ID exports can exceed kernel keytable capacity and cannot be applied through
this tool. Loading is opt-in with `--apply`, backs up the selected receiver's
keytable, and verifies entries by readback. Persistent installation, physical
repeat behavior and reboot acceptance are separate checks. No LIRC daemon is
required.

### Root-supervisor fault injection

On the Pi, with `PYTHONPATH=/opt/satip SATIP_RUNTIME_ROOT=/run`, run
`python3 exercise_recovery_on_pi.py --service player --target supervisor --failure freeze --confirm-interrupt`.
Repeat crash/freeze for web and Tvheadend. The default target remains the service. For web-supervisor tests, add
`--preview-before-failure` to confirm HLS generation and return to HDMI-controller
playback after the fault.
The script refuses active scans/preview/recovery and checks stable configuration
hashes without displaying secrets. Guardian/feeder fault injection is deliberately
not offered because it invokes hardware reset. See [recovery](../doc/recovery.md).


## Live-relay channel-switch comparison

measure_switching_on_pi.py --confirm-interrupt --compare-timeshift --rounds 2
--channels "Das Erste HD" "RTL Television" compares direct HTTP and raw live relay
with identical player options. It temporarily revokes/restores RAM readiness, uses
only its virtual keyboard, observes backend subscriptions and verifies audio/filter
policy, then restores the original channel/settings. See the dated
[time-shift validation](../doc/timeshift-validation.md) record. Do not use physical
controls or change the lineup during the benchmark.

## Rolling time-shift checks on the Pi

See [time-shift evidence](../doc/timeshift-validation.md) before treating any check
as hardware qualification. Copy these scripts outside the application package and
run with PYTHONPATH=/opt/satip (plus SATIP_RUNTIME_ROOT=/run for controller status):

- exercise_timeshift_writes_on_pi.py: actual USB partition sector-write counters
  while live, paused, resumed/delayed and returned to live; explicitly flushes only
  the USB filesystem (ctypes/syncfs on the test Pi) to separate pending writeback,
  then checks zero steady-live writes and active writes without changing settings.
- exercise_timeshift_on_pi.py: synthetic, accelerated, real-USB/headless-mpv RAM
  promotion/pause/seek/expiry/USB-stop test; no tuner or physical HDMI. Temporarily changes the readiness
  marker's retained duration, then restores it. Never formats storage.
- exercise_timeshift_controls_on_pi.py: actual channel and controller via virtual
  Apple Remote events, including one-minute paused retention expiry. Requires root,
  evdev and /dev/uinput (load the existing uinput module if necessary). Only its own
  virtual node receives satip-input access. Restores the readiness marker.
- exercise_timeshift_matrix_on_pi.py: short H.264 HD/MPEG-2 SD matrix through the
  installed controller; measures tuning/rendering counters and checks pause/seek/live.
  Returns to the original channel without changing the lineup or formatting USB.

These scripts interrupt TV control. They do not observe physical picture/sound or
actual infrared button operation. Duration boundaries are exercised on the Pi;
byte-budget eviction, pathological segments, forget/eject guards and many failures
also have deterministic automated policy tests. Actual full/slow/read-only media,
USB hotplug, power cuts and endurance remain separate acceptance work.

## Browser source and Enigma2 setup acceptance

`exercise_source_browser_on_pi.py` runs on the Pi as root with explicit
`--confirm-interrupt --address ENIGMA2_IP` and repeated `--bouquet NAME` options.
It lists bouquets, checks LAN discovery, imports/validates the selected streams
and checks reference deduplication. `--first-setup` uses a separately named
`source-browser` rehearsal, completes Enigma2-only setup and restores the current
installation. The default rehearsal, network/SSH identity and Wi-Fi are retained.
It does not zap or modify the source receiver. Do not overlap physical controls,
other scans, preview, restore or another rehearsal. A short explicit per-input
`--timeout` is smoke-test policy, not physical reception qualification.
See [Enigma2 evidence](../doc/enigma2.md).
