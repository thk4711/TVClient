# Time-shift qualification

**Audience: developers and advanced administrators.** Current procedure and
acceptance limits, not chronological test results.

[User controls](timeshift.md) · [Implementation](timeshift-internals.md) · [Validation](validation.md)

## Automated coverage

Use the rolling, copier, time-shift, remote-input and player tests for independent
RAM/USB limits, queue failure, monotonic freshness, cache hit/miss reader handover,
seek accumulation, timestamp gaps, obsolete generations, paused expiry, missing
storage, recovery notices and safe-remove/forget contracts. Review Linux/native
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
  expiry and source/channel identity without adding a second subscription.
- Seek timing includes confirmed position checks, not first visible-picture timing.
  Repeat long-history forward/backward/alternating bursts and resume/re-pause cycles.
- The backpressure fault exercise freezes only the identified FFmpeg child and
  verifies direct-live recovery; it must resume that exact process in cleanup.

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
