# Development and contributing

**Audience: developers and advanced administrators.**

## Maintaining the user manual

The illustrated [User manual](user-manual.md) is the owner-facing source of truth.
Update it whenever setup, controls or settings change; keep the reference guides
and their UI button names consistent. Regenerate `doc/user-manual.html` with
`python3 scripts/render_manual.py`. The dependency-free renderer supports the
manual's headings, paragraphs, lists, notices, tables, links and images; it rejects
unsupported blocks. `--check` and `tests/test_manual.py` detect stale HTML.

Keep the opening feature list concise. Give each procedure one detailed home and
link to it from setup summaries rather than repeating it. Keep data-flow, process
and buffer implementation details in technical guides, not the owner manual.
SAT-IP, Enigma2, USB time shifting and YouTube are stable supported features
within their stated compatibility limits. Keep historical evidence outside current guides under `validation/records/` and
track broader image/hardware qualification separately; neither a stable label
nor a documentation edit is new hardware-test evidence.

When adding, renaming or removing guides, update both `scripts/source_manifest.py`
and the `satip_controller/manual.py` serving inventory; check that each guide
is listed for its intended audience. Historical records are neither installed nor
served. Do not append deployment logs or old test totals to current guides.

For documentation-only live updates, replace the changed public files under
`/usr/share/doc/satip` with the same repository-relative layout and permissions
(0644 files, 0755 directories). Retain a private rollback copy and compare hashes
after installation. No service restart or image rebuild is required for content-only
changes. Changes to `satip_controller/manual.py` need a web-service restart to
reload the serving inventory; a compatible route-only update need not restart
the player or discard time-shift history. If visible
settings text also changes, update the matching installed static asset. Run
`python3 scripts/render_manual.py --check`, the manual/documentation unit tests,
and the read-only [manual browser check](../validation/check_manual_browser.cjs)
against the device to check links, images, mobile layout and printing.

`doc/user-manual.css` supplies phone layout and A4 print styles. The HTML uses no
scripts, remote fonts or remote images. Screenshots have captions and alternative
text. The seventeen reviewed visual assets (eleven browser captures, two native
TV exports and four labelled diagrams), PNG icon, Markdown, HTML and CSS
are installed under `/usr/share/doc/satip` and hashed as image inputs.
`satip_controller/manual.py` serves only allowlisted public files, following the
parent application's login and host policy in setup and normal management.
Technical-reference links open the installed plain-text Markdown guides.
Never expose a directory mount containing private configuration or credentials.

Regenerate browser illustrations with [capture_manual.cjs](../validation/capture_manual.cjs).
It uses real Chrome with live receiver GET requests only; a browser-network guard
blocks receiver actions. Setup runs the actual setup scripts against a separate
localhost example API for both SAT-IP and Enigma2, never a reset or rehearsal
on the configured receiver. Generate the flashing, source/network and IR wiring
diagrams with `python3 scripts/draw_manual_diagrams.py` (Pillow and a local
TrueType font required; override with `--font`). These are labelled diagrams,
not simulated Imager screenshots. Physical pin numbering and the sensor-specific
pinout/3.3 V warnings must remain explicit in any wiring revision.
Review every image before publication; anonymise names, addresses and Wi-Fi SSIDs,
and exclude pairing codes, credentials, stream tokens and private network details.
The welcome and Wi-Fi illustrations use the installed native renderer's
`--frame` export with an example URL/onboarding page; export mode does not touch DRM or change the live display.
See the [capture procedure](../validation/README.md#user-manual-screenshots).

New manual assets must be added to the reviewed inventory in
`scripts/source_manifest.py`, the serving allowlist and relevant tests. The
existing [in-place deployment](operations.md#updates) includes public documentation
and retains a private rollback archive.

[Documentation index](README.md) · [Architecture](architecture.md)

## Appliance implementation

The [project README](../README.md) describes what the appliance does for viewers.
Keep decoder, service, image-build, and validation details here and in the linked
technical guides when contributing changes.

- **Signal path:** satellite → external SAT-IP tuner → Tvheadend → mpv → HDMI.
  A Python controller handles selected keyboard/GPIO IR input; the web manager
  handles channels, favourites, EPG, and exclusive HLS preview. See
  [architecture](architecture.md) for component ownership and handover.
- **Display and audio:** direct DRM/KMS playback defaults to 1080p50 and
  capability-aware PCM (multichannel when advertised, otherwise stereo).
  Normal German TV audio only; software volume remains available.
  The image keeps usable real EDID, supplies a 1080p/stereo fallback when
  missing (including AV receiver setups), and selects the connected HDMI port.
  See [HDMI startup](hdmi-startup-screen.md) and [boot process](boot-process.md).
- **Decoder policy:** persistent mpv uses H.264 hardware copy decoding and
  software MPEG-2 SD decoding with double-rate deinterlacing, mild luma
  sharpening and aspect-preserving 720p bicubic scaling. Playback recovery
  is bounded. See [playback lifecycle](architecture.md#playback-lifecycle).
- **Service lifecycle:** per-service root guardians recover crashed/frozen
  supervisors with exact descendant ownership. Frozen-service detection, targeted restarts, a hardware
  watchdog, and bounded recovery history are intentional appliance behavior. See
  [recovery](recovery.md) and [services/persistence](buildroot-services-persistence.md).
- **Management and state:** search cancellation/recovery, filtering, drag-to-sort
  favourites, concurrent-edit protection, atomic state writes, and saves without
  player restart. Telemetry is read-only. Player/web services run as separate
  non-root users with protected credentials. See [HTTP API](api.md).
- **Browser viewing:** continuous FFmpeg/HLS preview pauses HDMI and releases
  the tuner when stopped, abandoned, or interrupted by physical controls. HDMI,
  scans, and preview are exclusive. See [architecture](architecture.md).
- **Image integration:** BusyBox init, self-contained first-boot setup, SAT-IP
  discovery/manual registration, persistent data, and early HDMI splash. Image
  inputs include the Raspberry Pi FFmpeg V4L2 M2M backport and Python XML/zlib
  modules. See the [image build guide](../buildroot/README.md).

Build from current source rather than assuming an existing artifact is current.
A changed defconfig requires a fresh output directory. The current image profile
is a development profile, not a qualified release image; fresh-image and physical
acceptance remain incomplete. Source tests are not hardware proof. See
[validation](validation.md) and [reference hardware](reference-setup.md).

## In-place time-shift development

Use scripts/deploy_appliance.py with --host and --apply to install matching
application modules and public documentation without reflashing. On the current
writable development receiver, --root-backup stages/retains private rollback under
/root/satip-deployments instead of the nearly full data partition. The transaction
checks free space before stopping services; it never deletes older archives or
persistent configuration to make room. This option is not a production update flow.

Initial TV loads probe with audio enabled but playback paused; select_audio
releases this initial gate only after verifying a normal German track and
at least one finite second of selected-stream demuxer cache. An
existing time-shift pause is not released by periodic audio checks. This avoids
discarding early audio packets through late track enablement on the live relay.

Live passthrough and FFmpeg prebuffer input are separate: the upstream pump
never waits on FFmpeg's stdin. A bounded queue (64 chunks, each at most 188 × 256
bytes from the pump, approximately 3 MiB) feeds the muxer on a daemon thread.
Overflow fails capture rather than dropping bytes; teardown terminates FFmpeg,
which releases a blocked pipe writer. Capture/relay failure marks a retry
cooldown so the recovery load uses direct live playback before attempting the
prebuffer again (30/60/120-second backoff, then no further automatic retries).
Runtime failures retain that backoff until capture has remained fresh for
120 seconds; merely starting a process does not reset it. A terminal RAM index
and `/run/satip-player/last-capture-failure.json` retain sanitized cause and health
counters, so an input outage is not reported as a USB-capacity problem.
The existing bounded playback recovery remains enabled. RAM-only health snapshots and stall diagnostics distinguish
upstream receipt, capture writes and live-reader overflow without stream URLs or
credentials. Tests in tests/test_rolling.py cover queue overflow, ordered feeding,
pipe failure and diagnostic sanitization.

For an opt-in live comparison, validation/measure_switching_on_pi.py supports
--compare-timeshift with --confirm-interrupt. It temporarily revokes the runtime
USB readiness file permissions to select direct playback, restores the original
contents/mode in finally, and restores the original channel. It changes playback
and must not run concurrently with physical controls or another validation tool.
Programmatic track selection is not proof of audible sound.

validation/exercise_prebuffer_backpressure_on_pi.py is a separate opt-in fault
test. With --confirm-interrupt it freezes only the identified prebuffer FFmpeg
child using a pidfd, checks recovery to direct playback with the same controller
and German track, and resumes that exact process in cleanup if it still exists.
It requires healthy live RAM viewing and must not overlap a switching test.

See [time-shift qualification](timeshift-validation.md) for running-device checks
and remaining physical acceptance. Validation scripts live
in validation/ and require root on the receiver; they are not appliance UI features.

Time-shift seek input is drained without awaiting picture completion. The
controller accumulates absolute requested positions, with one worker and one
replaceable pending target; it does not spawn a task per button event. A
200 ms coalescing window allows native holds to keep making progress. Other
controls discard pending targets; current reader transitions finish under the
existing input lock. Paused remote repeats accelerate from 10 to 30 seconds
after two seconds of kernel hold time, without increasing the event/seek rate
or changing normal volume steps. Repeats at an unchanged boundary do no work.
Reader telemetry gaps use the existing requested position rather than clearing
the accumulated target. `SYN_DROPPED` recovery queries actual held keys at the
next `SYN_REPORT` so native repeat-only holds can continue without a fresh press.
Channel generations invalidate stale work, and shutdown
cancels/joins the worker before closing the player. Cached archive targets seek
without loadfile; uncached exact targets reopen with a short backward cushion.
Timestamp gaps select the preceding segment rather than accidentally selecting
the newest segment. Tests cover cache hit/miss/raw handover, wrap offsets, gaps,
bursts, opposite directions, in-flight updates and obsolete-target disposal.

For a real-channel five-press/alternating timing check (interrupts HDMI and
returns to live, with no persistent USB setting changes):

~~~sh
SATIP_RUNTIME_ROOT=/run PYTHONPATH=/opt/satip python3 validation/measure_timeshift_seeking_on_pi.py --confirm-interrupt
~~~

Start from healthy live RAM playback, with no physical control input or other
validation running. The default pause lasts 65 seconds before seeking. Timings
include a 400 ms stable-position observation; they are software confirmation,
not physical picture/audio acceptance. Optional --channel 'RTL Television'
selects exactly one named favourite and restores the original channel afterward.

For a long-history control stress check, add `--wait-seconds 1500 --cycles 8`.
The tool checks USB capture/channel identity during the pause, repeats forward,
backward and alternating paused seek bursts, and resumes/repauses between
cycles. It returns to live and restores initial volume/mute in cleanup; it
does not alter USB settings.

## Contributing

Keep changes consistent with existing modules and test the affected behavior.
Include documentation updates with feature or configuration changes. Describe
user-visible behavior in the README and usage guides; put implementation details
in the technical guides. Keep qualification reports tied to a source/image
identity and separate from current usage instructions. Follow the checks below
and preserve third-party notices when adding or updating bundled components.

## Source map

| Path | Responsibility |
|---|---|
| `satip_controller/__main__.py` | Player CLI, instance lock, signals/shutdown |
| `config.py` | Player/library/credential validation |
| `channels.py` | Favourite wraparound selection |
| `controller.py` | Actions, load generations, watchdog, leases, overlays |
| `player.py` | Fixed mpv policy, audio PID selection, child ownership |
| `audio.py` | HDMI PCM EDID limits, normal German track ranking and audio labels |
| `mpv_ipc.py` | Correlated asynchronous JSON IPC |
| `input.py` | Selected evdev devices, repeat/debounce, hotplug |
| `i18n.py`, `settings.py` | English/German HDMI messages and persistent OSD preferences |
| `osd.py` | Literal text, overlay priority, message sequence |
| `state.py` | Atomic JSON storage, serialized durable writers, cache, monotonic leases and library revision |
| `lifecycle.py`, `coordination.py` | Cancellation barriers, process ownership and shared tuner handover |
| `http.py`, `static/request.js` | Strict/bounded server requests and shared browser request handling |
| `backend.py` | Loopback-only Tvheadend API and fail-closed service filtering |
| `scan.py` | Catalogue, scan/restoration journal, favourite mappings |
| `epg.py` | Bounded secret-free cache, banner/detail text |
| `preview.py` | Continuous exclusive FFmpeg/HLS session, idle cleanup and local takeover |
| `telemetry.py` | Independently cached read-only Linux/network/Tvheadend measurements |
| `web.py` | FastAPI lifecycle, authentication, routes |
| `diagnostics.py` | Read-only selected-device/DRM listing |
| `static/index.html`, `static/app.css` | Responsive application layout and shared setup theme |
| `static/app.js` | Page navigation, telemetry dashboard and Live TV channel selector |
| `static/ui.js` | Channel/scan actions, favourites and independent stream heartbeats |
| `static/` | Setup assets and locally bundled hls.js |
| `satip_appliance/channel_backup.py` | Channel-only allowlist, catalogue/service validation and fixed-path overlay; preserves current device configuration |
| `satip_appliance/backup.py` | Private bounded archives, root snapshot/activation and restore recovery journal |
| `satip_appliance/` | Buildroot provisioning, discovery/registration, setup, persistent import, supervisors and splash |
| `buildroot/`, `scripts/build_image.py` | Pinned image profile, media backport, guarded build/staging and artifact collection |
| `examples/` | [Sanitized configuration examples](../examples/README.md); appliance configuration is generated during setup |
| `tests/` | Fake/unit/IPC/API/UI and isolated browser regression tests |
| `validation/` | Explicit real-Pi/remote/preview checks and learner tests |

Unqualified module paths in the rows are relative to `satip_controller/`. Importing the
package does not open input devices. Most policies can be exercised without
Linux hardware; actual playback requires a compatible Linux/Pi media stack.

## Dependencies and entry points

[pyproject.toml](../pyproject.toml) declares Python >=3.13, Linux evdev >=1.9,
FastAPI >=0.115, uvicorn >=0.32, and httpx >=0.28. Setuptools is the build backend;
Both `satip_controller` and `satip_appliance` are packaged;
static HTML/JS/CSS/TXT assets are included as package data. The appliance uses
Buildroot-packaged Python and dependencies; target pip is not required. Use an
isolated virtual environment for host development.

Module entry points, from a source checkout/deployed source directory:

~~~sh
python3 -m satip_controller --check --config /etc/satip-player/config.json
python3 -m satip_controller.diagnostics --config /etc/satip-player/config.json
python3 -m satip_appliance.web
~~~

The first two do not tune/grab. **The third starts the appliance web service**
using its fixed `/data/store` paths, with
backend/cache/lease/recovery effects; do not launch it alongside production using
the same paths. Packaging also declares `satip-player` and `satip-web` console
scripts. Appliance supervisors launch fixed `python3 -m` commands with
`PYTHONPATH=/opt/satip` and `SATIP_RUNTIME_ROOT=/run`; the appliance web entry
point is `satip_appliance.web`, which selects setup or configured management.
See [service-account checks](operations.md#updates) for commands on the Pi.

## Automated tests

Run from the project root in an isolated development environment:

~~~sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
python -m unittest discover -s tests -v
python3 -m unittest discover -s validation -p 'test_*.py' -v
node --test tests/test_ui.cjs tests/test_settings_ui.cjs tests/test_device_ui.cjs
~~~

Python tests use temporary files, fake backend/player/input/IPC, and TestClient;
they do not tune the Pi. Validation's `test_*.py` files exercise learner/verifier
logic with fakes, not the actual diagnostic scripts. Node UI tests use a lightweight
DOM/API fake without browser packages or receiver access.

Missing FastAPI/httpx dependencies fail collection rather than silently skipping
web coverage. Linux-specific tests may skip on macOS; review reported reasons.
The baseline runner is unittest. Release checks also include:

~~~sh
ruff check satip_controller satip_appliance tests scripts/check_package.py
mypy
python -m build
python scripts/check_package.py dist/*.whl dist/*.tar.gz
~~~

Ruff checks syntax/undefined-name classes; mypy currently covers lifecycle and
coordination only. The package smoke check installs the wheel in a fresh venv,
checks dependencies, both console entry points and packaged static assets from
outside the checkout. CI runs Python 3.13 with the Buildroot dependency pins and
Python 3.14 with current dependencies, plus Node and both Chrome regressions.

### Isolated real-browser regression

`tests/test_ui_browser.cjs` requires Node **22+**, Chrome, and a fresh disposable
browser profile. It serves a localhost fake API, does not connect to the Pi, and
tests mouse/touch sorting, removal, save acknowledgement, timeout recovery,
navigation with unsaved edits/streaming, channel switching, takeover feedback,
dashboard rendering, and mobile overflow.

~~~sh
CHROME_BINARY=/absolute/path/to/chrome CHROME_USER_DATA_DIR=/absolute/path/to/a/fresh/test-profile node --test tests/test_ui_browser.cjs
~~~

`tests/test_device_browser.cjs` uses the same command/environment with a separate
fresh profile. It covers password-dialog focus, Cancel/Escape, local mismatch
validation, server errors, password-change requests and mobile layout against a
fake device API; it also checks USB chooser focus, cancellation, mounted storage
capacity/free-space rendering and the device-sized limit on desktop/mobile. It
never changes real receiver credentials or formats real storage.

`tests/test_setup_browser.cjs` uses the same command/environment with a separate
fresh profile. It covers request timeout recovery, retained errors/selections,
empty discovery, manual server entry and selection, persistent tuner choice,
connection loss, scan cancellation/recovery, and navigation into management.
`tests/test_satip_discovery.py` covers description/SSDP parsing, bounded reads,
registration persistence and ownership checks, delayed backend enumeration,
actual serialized server classes and interrupted-selection recovery.

Replace both paths. Never use an everyday browser profile; Chrome writes profile
data and the test opens a local server. `UI_SOURCE` optionally selects a different
UI script for regression comparison. Node/Chrome are not production dependencies.
Set `UI_SCREENSHOT_DIR` to save desktop/mobile screenshots with fake telemetry.

`tests/test_backup.py` covers archive round trips, file/size/schema/credential
validation, packed backend records, explicit exclusions, API authentication and
streamed upload, private metadata modes, activation interruptions and helper
restart recovery. These tests use isolated fake roots, not the live store.
Node device tests additionally cover binary download, upload/confirmation
cancellation, password clearing, size rejection and Wi-Fi-trial controls.
`tests/test_channel_backup.py` adds channel-only catalogue/order, non-channel
setting preservation, kind separation, service/mapping validation, space checks
and non-rebooting activation/recovery tests. Node device tests cover the separate
channel download, counts/overwrite confirmation and reload after restore.
See [backup qualification](backup-validation.md) for the live acceptance checklist.

Telemetry tests cover missing/zero readings, scale conversion, independent cache
failures and counter resets. Performance collection lives in `performance.py`
(read-only procfs/sysfs, bounded scans). `tests/test_performance.py` covers CPU,
process and disk units, PID reuse, long gaps, missing counters and shared-cache
failures. `tests/test_performance_ui.cjs` covers graph deduplication, age limits,
gaps, stale readings, vanished interfaces and history collection without hidden
graph rendering. The real-browser dashboard test also checks the on-demand
performance dialog, Close/Escape focus restoration, reopening with history,
navigation cleanup and mobile dialog overflow. Preview tests cover a
simulated hour of viewing,
idle cleanup, takeover during startup, stale generations and cleanup-before-lease
release. These do not establish real-time endurance or physical button acceptance.

Buildroot staging, build metadata and source manifests include the shared CSS
and application scripts. The post-build validator checks installed source/asset
hashes against build inputs before the image is assembled.

## Explicit live validation

[Validation index](../validation/README.md) explains the tools and their safety
boundaries. They are deliberately excluded from normal test discovery:

- `check_controller_on_pi.py` starts controller/player/input, tunes channels,
  changes persisted state, and grabs devices. Stop production first; arrange VT
  and bounded recovery. It assumes at least two favourites and a 35% initial
  volume for some assertions, so it is not a generic current-state check.
- `check_preview_browser.cjs` uses a real API and pauses HDMI while checking
  browser decoding. It needs Node/Chrome, a fresh profile and the receiver access
  settings described in the [tool index](../validation/README.md). Use actual
  services from the receiver being qualified.
- Remote learners/verifiers need the real GPIO receiver and may load/grab input;
  they must not run on top of the active player.

Distinguish programmatic property success from physical sound, picture, buttons,
and OSD layout. See [validation and limits](validation.md).

## Documentation maintenance

The image installs the README, current guides, and licence notices under
`/usr/share/doc/satip`; the image staging and metadata include these public files.
Documentation-only updates can be copied there on a running appliance without
restarting services. Source-code links in these guides refer to the repository.

Keep guides aligned with source/configuration defaults. Describe current behavior
and limitations, not planning sessions, execution logs, or layered update notices.
Check relative links/anchors; keep generic instructions separate from
[reference hardware](reference-setup.md). Label read-only, mutating, and
live-disruptive commands. Never turn implementation into hardware proof.

## Publication and licensing

The project's original code and documentation use the [MIT License](../LICENSE).
Include that licence with copies or substantial portions of the project. The
appliance image also contains separately licensed software; the MIT licence does
not replace those components' licences. Preserve third-party notices:

- Bundled hls.js: [licence](../satip_controller/static/hls-LICENSE.txt).
- Bundled QR encoder: [licence](../satip_controller/static/qrcode-LICENSE.txt).
- FFmpeg Raspberry Pi backport: copyright/licence notices in the patch sources.
- Remote learner: reference provenance and attribution in its source.

Exclude credentials, private captures/backups, browser profiles, and generated
artifacts. Review machine metadata in examples and the actual candidate file list;
`.gitignore` alone is not a secret policy. Ignoring or later deleting a tracked
secret does not remove it from Git history.

Before a commit, inspect both `git status --short --ignored` and
`git diff --cached --stat`, then review the staged contents privately. Keep build
host settings in ignored `scripts/build-image.ini`, and provision only the flashed
card, never the public source template. The repository-hygiene tests check that
private/generated paths are ignored while examples, fixtures and notices remain
eligible; they are not a complete secret scanner.

## Source browser development and acceptance

The source API and leased importer live in `satip_appliance/source_api.py` and
`source_browser.py`; both browser modes use `satip_controller/static/sources.js`.
`scripts/source_manifest.py` explicitly inventories new modules/static assets
for the next image. Use `scripts/deploy_appliance.py` for matching application/
documentation deployment without flashing.

Run `tests/test_source_browser.py` for staged import, receiver identity,
publication rollback, cancellation, startup gating and API checks.
`tests/test_source_browser_ui.cjs` exercises source-aware discovery and the
Enigma2 wizard in real Chrome, alongside the existing SAT-IP setup browser test.
Chrome tests require isolated `CHROME_USER_DATA_DIR` values.

`validation/exercise_source_browser_on_pi.py` is an explicit, interrupting
on-device acceptance script. It requires root, `--confirm-interrupt`, a source
`--address` and repeated `--bouquet` arguments. Without `--first-setup` it
imports into the configured receiver. With `--first-setup` it uses a separate
`source-browser` rehearsal snapshot, reaches configuration/playback and restores
the original installation in cleanup. It retains the normal network/SSH identity
and never clears Wi-Fi. Existing default rehearsal snapshots are untouched.
The general rehearsal CLI supports `--name SHORT_NAME` for isolated development
snapshots; always use the same name for start/status/restore.

A five-second `--timeout` is useful for bounded smoke tests but can reject slow
streams; the browser default remains 20 seconds. See [Enigma2 validation](enigma2.md)
for measured results and outstanding hardware qualification.
