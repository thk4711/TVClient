# Automatic recovery on Buildroot

**Audience: developers and advanced administrators.**

[Documentation index](README.md) · [Services/data](buildroot-services-persistence.md)

The receiver uses layered recovery.
Recover the smallest owned component first; an unavailable external SAT-IP tuner
or LAN is **not** a reason to reboot the Pi.

## Recovery layers

| Failure | Detection and action |
|---|---|
| Stalled TV / mpv IPC | Existing controller load/stall deadlines, bounded retries and replacement of owned mpv; IPC timeout selects software decoding for that controller lifetime |
| Service crash | Existing root supervisors reap the exact owned process group before restart; 1–30 s backoff, reset after a stable minute |
| Frozen controller | Main control loop writes generation-bound progress to RAM every 2 s; supervisor checks every 5 s, restarts after 45 s without progress |
| Frozen web / backend | Supervisor probes local web liveness or authenticated read-only Tvheadend serverinfo every 5 s with a 1 s socket timeout; six consecutive failures cause targeted cleanup/restart |
| Frozen/crashed root supervisor | Per-service root guardian detects exit or stale loop progress; stops the supervisor, adopts and cleans its descendants, then replaces it |
| Failed guardian / unsafe local recovery | Independent watchdog feeder stops feeding; hardware resets the board |
| Whole-system failure | Hardware watchdog resets the board if its feeder stops running |

A new generation has up to 120 s to establish health. Once it establishes
health, normal deadlines apply. Player pulses come from the **actual controller
loop**, including paused HDMI, retries and YouTube handover—not an independent
thread that could hide a stuck controller. Unchanged player status JSON is not a
heartbeat. PID, random generation identity and monotonic timestamps prevent an
old file from making a new player appear healthy; no wall-clock/NTP dependence.
Malformed, oversized or linked progress files do not crash the root supervisor.

Normal web routes keep their existing authentication and action/host checks.
Both setup and configured web applications expose only PID/generation liveness
at GET **/healthz** to direct loopback clients; LAN clients receive 404. Uvicorn
forwarded-header processing is disabled so an incoming header cannot impersonate
loopback. No credentials, channel data or backend requests are exposed there.

The existing 10 s player/backend and 40 s web cleanup grace periods still apply,
followed by bounded KILL/reaping. Consequently useful recovery takes longer than
the detection deadline. Scan journals and preview ownership are retained; the
replacement web service performs existing recovery before serving normal work.

## Startup after abrupt power loss

Reception failures before first playback use retry waits of 1 s, 2 s, then at
most 3 s during the first minute of the player lifetime. The clock is monotonic
and channel changes do not renew this window. Afterwards the existing exponential
backoff resumes, capped at 30 s. Load deadlines and exact stream ownership are
unchanged; in-flight SAT-IP SETUP requests are not interrupted to retry sooner.
The native startup artwork stays in its existing localized connecting state;
text-only fallback says “Waiting for SAT-IP tuner …”. Other failure reasons keep
normal diagnostics. See [startup](hdmi-startup-screen.md) and
[power-cut troubleshooting](troubleshooting.md#slow-startup-after-pulling-power).

This handles delayed tuner availability without rebooting the appliance or
resetting/stealing sessions on the external tuner. It is not a power-loss storage
guarantee. A retained tuner session can delay reconnecting; Pi-side RTSP errors alone
do not establish its cause.

## Stale channel audio metadata

A missing saved main audio PID does not force a retry when the live stream
provides exactly one explicitly identified normal German MP2 track. Playback
uses that track without changing persistent metadata or resetting an already
selected decoder. Accessibility, unknown and ambiguous tracks remain excluded;
see [the fallback policy](configuration.md#saved-audio-pid-and-live-fallback).
This does not relax the load/stall deadlines or conceal transport outages.

## Hardware watchdog

**S89satip-watchdog** starts after network/data recovery access and before TV
services. Reverse shutdown order stops TV services before disarming the watchdog.
The root-owned Python feeder opens **/dev/watchdog0**, requests 15 s, reads back
the accepted timeout and feeds at most every 3 s (or one third of a shorter
accepted timeout). Startup refuses **nowayout=1** because clean disarming would
not be safe. A lock and PID start identity/pidfd avoid duplicate feeders and PID
reuse during stop. No blanket Python-process killing is used.

Orderly stop magic-closes the device and checks that it is inactive. Never
SIGSTOP or SIGKILL this feeder during ordinary diagnosis: doing so can cause an
abrupt hardware reset and loss of recent SD writes.

The feeder is independent of tuner availability but requires fresh progress
from every enabled service guardian. A dead guardian, stale progress, or failed
local cleanup causes the feeder to exit **without magic-closing** the device.
The accepted hardware timeout then resets the board. An explicit orderly service
stop removes that service's expectation only after verified cleanup. Services
intentionally stopped do not cause resets; setup's idle player guardian continues
publishing healthy prerequisite-wait progress.

This does not detect every kernel/driver failure, or an init failure before any
service is enabled. There is no A/B rollback or reboot-storm suppression across
boots; a permanent internal fault may cause repeated resets. External tuner or
network outages do not consume the guardian's internal-failure retry budget.
Never delete ownership/expectation markers to force another player into existence.

## Root-supervisor guardians

Each service has a separate root guardian, itself a Linux subreaper, above its
existing root supervisor. BusyBox starts these through **S90satip-services**.
The independent S89 feeder monitors them; loss of a guardian is a reset condition,
not an opportunity for another process to guess ownership from an old PGID.

Supervisors publish boot/PID/start-identity-bound monotonic progress from their
actual loop every two seconds, and on phase changes. Healthy prerequisite waits
(including missing LAN/tuner) keep progressing. The normal stale-progress deadline
is **45 s**; a supervisor in cleanup has **100 s** for its bounded cleanup/I/O.
Guardians monitor every **0.5 s**. No heartbeat thread masks blocked root work.

On a supervisor failure the owning guardian sends TERM and CONT through a pidfd,
allowing a deliberately frozen supervisor to perform normal cleanup. It allows
10 s (player/backend) or 40 s (web), then KILL with a 3 s deadline. After supervisor
exit, its remaining descendants are adopted by the guardian and stopped through
pidfds, including descendants that escaped the media process group. No adopted
child is reaped until all live descendants are gone, retaining PID reservations.
Only then may this continuously owning guardian clear the stale generation marker
and start a replacement. An interrupted launch is covered by the same ancestry,
without depending on a successfully written child PID.

Five supervisor failures within 300 s cause escalation rather than endless local
restarts. Replacement backoff is bounded to 30 s. Guardian cleanup progress has a
fixed **100 s** watchdog deadline; normal guardian progress expires after 45 s.
Missing guardian startup progress is tolerated for only **15 s**. A verified
stop acknowledgement has a **5 s** handoff window for the control command to
remove its expectation after guardian exit. PID reuse,
malformed records, and old-boot records cannot establish guardian health.

Ordinary per-service start/stop commands go through the guardian. Direct supervisor
launches and stale-marker recovery are not supported administrative shortcuts.
The watchdog is stopped and reloaded during coordinated development deployment,
after TV writers have stopped; it is restarted before the new guardians.


The image inputs explicitly select the BCM2835 watchdog, retain the BusyBox
watchdog diagnostic applet and add **dtparam=watchdog=on**. Application modules
are included through existing package source allowlists.

## Diagnosis and safe tests

Run on the Buildroot Pi as root:

~~~sh
cat /run/satip-watchdog.ready
cat /sys/class/watchdog/watchdog0/state
cat /sys/class/watchdog/watchdog0/timeout
cat /run/satip-player/progress.json
cat /data/store/operations/recovery.json
grep Recovery /run/satip-supervisor/*.supervisor.log
cat /run/satip-supervisor/*.guard.progress
tail -n 30 /run/satip-supervisor/*.guard.log

# Safe clean disarm / re-arm, not a hardware-reset test:
/etc/init.d/S89satip-watchdog stop
/etc/init.d/S89satip-watchdog start
~~~

The private persistent summary is limited to 32 recent events and bounded
allowlisted counters (under 16 KiB). Events contain service, fixed reason, boot
identity and monotonic time, never raw errors/credentials/URLs. A separate
root writer with a short deadline and nonblocking lock keeps diagnostics out of
the recovery critical path. Heartbeats, generated media and normal logs stay in
RAM. A previous clean-services-stop marker yields **clean**; a missing marker
for a known boot yields **unclean**, not a proven watchdog reset or power loss.
Restarting services clears the marker; the first observed boot is **unknown**.

After arranging interrupted viewing, copy the reviewed diagnostic script onto
the Pi and run:

~~~sh
PYTHONPATH=/opt/satip SATIP_RUNTIME_ROOT=/run python3 exercise_recovery_on_pi.py --service player --failure freeze --confirm-interrupt
~~~

See [the tool](../validation/exercise_recovery_on_pi.py). It refuses active
scan/preview/recovery, signals the precise generation through a pidfd and checks
that its old group is gone and the replacement is useful. It also supports
**web**, **tvheadend**, **--failure crash**, and **--target supervisor**. Supervisor
tests verify replacement supervisor/service identities, old-group removal and
unchanged stable user configuration. For web supervisor tests, add
**--preview-before-failure** to produce HLS first, keep the preview session alive
during fault detection, and require useful HDMI-controller playback afterwards.
This checks FFmpeg/HLS and controller state, not browser decoding or physical output. Its failure cleanup resumes an
original deliberately stopped process; it never signals the hardware feeder.

Qualify both crash and freeze recovery for each service **and each supervisor**, including restoration
after browser preview. Check configuration and library integrity after recovery.
Actual watchdog hard-reset, cold-boot, scan-interruption, missing/full/read-only
SD and long-term endurance acceptance remain open. See [validation](validation.md).
