# Time-shift qualification

**Audience: developers and advanced administrators.** Current procedure,
qualification evidence and acceptance limits, not an exhaustive test log.

[User controls](timeshift.md) · [Implementation](timeshift-internals.md) · [Validation](validation.md)

## Automated coverage

Use the rolling, copier, time-shift, remote-input and player tests for independent
RAM/USB limits, queue failure, monotonic freshness, cache hit/miss reader handover,
seek accumulation, timestamp gaps, obsolete generations, paused expiry, missing
storage, recovery notices and safe-remove/forget contracts. Capture tests also
cover input EOF/timeouts, muxer exit, terminal health preservation, private
credential-free failure records, repeated runtime-failure retry exhaustion and
backoff reset only after 120 seconds of fresh capture. Review Linux/native
skips; fake storage readiness is not a test of an actual USB drive.
`tests/test_timeshift_copier.py` checks linear eviction work at 1,426 and 4,096
segments using filename-access counts rather than hardware-dependent deadlines.
It also exercises the real copier loop with a reduced segment cap, confirming
that trimming preserves the session and idle history is not republished.
`tests/test_rolling.py` also checks timeline/lookup reuse with 4,096 segments,
health-only updates, eviction, USB-to-RAM transitions, non-blocking slow reads
and cancellation without overlapping file reads. Remote tests feed queued
kernel-timestamped events to check debounce and native-repeat spacing.
`tests/test_timeshift_remote.py` also checks a 20-second native hold, two-second
3× acceleration, release/direction reset, unchanged playing/YouTube volume,
history-boundary no-ops and requested-target preservation during reader telemetry
gaps. Input-overflow tests check `SYN_DROPPED`/`SYN_REPORT` key-state recovery,
discarded intermediate events, no synthetic pause/tuner handovers, and released
keys not becoming phantom holds. A restored hold restarts its acceleration delay.
An isolated virtual-input check on the development Pi also forced a real kernel
queue overflow while Right remained held. Repeats continued after key-state
resynchronization, accelerated after two seconds and reset on release/repress.
This validates native evdev recovery without controlling TV; it does not prove
that overflow caused the originally reported physical-remote stall.
The updated player also confirmed all eight ZDF HD forward/backward/alternating
burst targets across two pause/resume cycles, with unchanged capture identity
and paused seek state. These are software position checks, not physical
picture/audio acceptance or a long-history accelerated-hold qualification.
`validation/test_timeshift_seeking.py` checks that current-session cleanup never
reloads a reader outside its packet cache or controls a replacement session.

## Running-device checks

Follow the prerequisites and interruption warnings in the
[validation tools index](../validation/README.md). Use real disposable USB media,
healthy playback, independent recovery access and no competing controls/tests.

- The synthetic exercise uses real USB and headless mpv without a tuner. It changes
  only runtime readiness limits and restores them; it is not physical HDMI acceptance.
- The write check distinguishes RAM-only live viewing from an activated USB session.
  Confirm that Pause starts capture, resume retains it and Return to live stops it.
- Control/matrix checks exercise HD/SD pause, seeks, reader transitions, retention
  expiry and source/channel identity without adding a second subscription. Virtual
  input checks require `/dev/uinput` (`modprobe uinput` on the development image);
  start from healthy live RAM mode, waiting for recovery cooldown after fault tests.
- Seek timing includes confirmed position checks, not first visible-picture timing.
  Repeat long-history forward/backward/alternating bursts and resume/re-pause cycles.
  For the long-session copier regression, wait at least 3,600 seconds before
  repeated bursts (`measure_timeshift_seeking_on_pi.py --confirm-interrupt
  --wait-seconds 3600 --cycles 8`). Start in healthy live RAM mode, with enough
  USB budget for an hour and no physical controls or competing validation. Check
  that capture identity and USB writing remain unchanged, no copier failure is
  recorded, and Return to live stops writes. This interrupts HDMI for over an
  hour; shorter seek checks and operation-count tests do not qualify that soak.
  To investigate an already long-running session without discarding its history,
  pause it and run `measure_timeshift_seeking_on_pi.py --confirm-interrupt
  --use-current-session --wait-seconds 0 --cycles 8`. This requires at least
  60 seconds of forward history and 30 seconds behind the selected position.
  Do not use physical controls concurrently. This mode does not activate USB,
  switch channels, resume between cycles or return to live. It checks capture
  and player identity and leaves the session paused, including on seek failure.
  Cleanup restores the starting frame only if it is still in mpv's packet cache;
  otherwise it explicitly reports that the frame was not restored. History can
  still expire under ordinary retention; this is not a promise to preserve an
  indefinitely paused frame. Compare reader-reload cases separately from cache
  hits, and compare both warm and uncached USB history.
  Reader phase logs and archive read counters help distinguish metadata work,
  slow USB reads and decoder/demuxer handover. Target confirmation timings are
  not first-picture measurements. Keeping the UI responsive does not make a
  slow USB drive read faster, and enlarging mpv's RAM cache is not this fix.
- `exercise_capture_failure_on_pi.py` uses native FFmpeg and a private loopback
  source in `/run` to exercise EOF, HTTP timeout and muxer exit. It checks specific
  notices, terminal health and failure-record retention without tuning, player
  controls, USB writes or readiness-marker changes. Run as root with
  `PYTHONPATH=/opt/satip`; it requires configured writable USB readiness.
- The backpressure fault exercise freezes only the identified FFmpeg child and
  verifies direct-live recovery; it must resume that exact process in cleanup.
  Its ownership check supports both legacy and start-tick-verified forked captures.

Confirm original runtime marker contents/modes, channel and volume after cleanup.
A SIGKILL or power loss bypasses ordinary finally cleanup; arrange supervised recovery.

## Observed longer-history seek behaviour

A Pi 4/ZDF HD checkpoint on 20 November 2026 exercised the existing paused USB
session with 44.6 minutes of retained history (3.88 GiB), positioned 26.7 minutes
behind live. Eight current-session cycles covered 32 five-forward, five-backward,
alternating and single-backward cases. All requested positions were confirmed,
with no lost presses, capture/player restart or new capture/copier/seek failure.
Cleanup restored the starting position, pause state and volume while USB capture
continued. No service restart or Return to live was used.

| Measurement | Median | Range |
|---|---:|---:|
| Controller cache-hit seek | 0.219 s | 0.121–0.358 s |
| Controller cache-miss reader handover/target confirmation | 0.746 s | 0.707–0.822 s |
| Five forward presses, +50 s | 1.362 s | 1.319–1.436 s |
| Five backward presses, −50 s | 1.929 s | 1.880–2.007 s |
| Alternating presses, net +10 s | 1.611 s | 1.586–1.653 s |
| Single backward press, −10 s | 0.779 s | 0.743–0.853 s |

Burst timings include event generation and 0.4 seconds of stable-position
confirmation; they are not individual seek or first-visible-picture times.
The earlier short-session burst medians were respectively 1.438, 2.046, 1.427
and 0.999 seconds: this checkpoint did not reproduce the large progressive
slowdown. Unchanged timeline refresh at 1,396 retained segments had a 0.039 ms
median, consistent with the bounded cached-refresh cost.

This is evidence that the fixes helped in this longer session, not a controlled
same-history pre/post latency comparison, a full one-hour soak, sustained
playing/resume qualification or physical HDMI/audio acceptance. The pre-update
baseline dropped two of five forward presses, so its failed target timeout is
not a valid latency comparison. Repeatedly seeking this area also warms USB
file data; unrelated cold-history seeks and slow media may still take longer.
Private raw measurements remain on the development receiver under its
`/root/satip-deployments` seek-update directory, not in the repository.

## Physical and image acceptance

- Observe picture/sound and actual keyboard/Apple Remote pause, resume, seek,
  panel navigation and Return to live, with English/German OSD.
  Hold remote Left/Right for at least eight seconds with enough history in the
  chosen direction; verify continued progress and 30-second repeated steps after
  two seconds. Release/repress and reverse direction to verify 10-second reset.
  At either retained boundary, verify that TV stays paused and reversing still
  works. A short buffer can reach a boundary before acceleration becomes visible.
- Observe exact pause/resume/seek pictures, A/V sync, live/tune delay and sustained
  HD/SD playback across thousands of segment boundaries.
- Fill retained history until paused/playing positions expire; confirm that the
  session continues and a paused viewer remains paused at retained content.
- Test real USB hotplug, safe removal, reconnect and reboot remount without formatting.
- Schedule slow/full/read-only/disappearing media and interrupted-power checks
  separately on disposable media. Synthetic errors do not replace physical faults.
- Qualify a complete current-source image and a freshly flashed card.

Time shifting is supported within its documented limits; these broader hardware,
storage-failure and image checks remain incomplete. Buffers are temporary media,
not permanent recordings or configuration-backup contents.
