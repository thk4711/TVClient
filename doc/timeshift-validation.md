# Time-shift qualification

**Audience: developers and advanced administrators.** Current procedure and
acceptance limits, not chronological test results.

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

## Physical and image acceptance

- Observe picture/sound and actual keyboard/Apple Remote pause, resume, seek,
  panel navigation and Return to live, with English/German OSD.
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
