# Channel switching

**Audience: developers and advanced administrators.** Current defaults, ownership
rules and diagnostic comparisons; not a development timeline or latency guarantee.

[Architecture](architecture.md#playback-lifecycle) · [Tvheadend](tvheadend.md#same-transponder-handover) · [Validation tools](../validation/README.md)

## Defaults

| Mechanism | Normal setting | Scope |
|---|---|---|
| Persistent relay worker | Enabled | HDMI live RAM-prebuffer playback with configured usable time-shift storage |
| Native SAT-IP idle-mux grace | 1000 ms, maximum 2000 ms | Eligible same-mux handover, with direct or relay playback |
| Saved-audio hint, bounded probe, adaptive coalescing, redundant IPC | Disabled | Explicit private diagnostic experiments only |

Normal channel keys use the existing 80 ms coalescing grace. Latest-request
selection does not cancel an already-started backend tune; obsolete work finishes
safely before loading the last requested favourite. Live output remains paused
until supported German audio and the selected-stream startup cache gate pass.
No optimization removes load/stall deadlines, stream filters or scan/preview barriers.

## Persistent relay

A single-threaded Python fork supervisor preloads capture/HTTP modules. Each
channel still gets a new capture process, HTTP connection, FFmpeg instance,
private token, socketpair, RAM directory, timestamp/index state and USB copier.
Only interpreter/import state is reused. Credentials and descriptors travel over
private IPC, not process arguments or public status.

Capture startup is gated until the child PID and independent process group are
tracked. Closing the old lifetime pipe shuts down upstream HTTP and joins its
pump. Positive release acknowledgement permits the next generation while old
FFmpeg/file cleanup finishes in the background. Missing acknowledgement forces
process-group termination and confirmed exit before another upstream opens.
This preserves one playback connection; it does not itself preserve SAT-IP sessions.

Retired cleanup has a five-second deadline, with at most two retired generations
before startup waits. Controller/supervisor death cleans known groups through
lifetime pipes and ownership records containing process start identities.
Repeated cancellation is joined at startup/closure barriers.

## Same-transponder mux grace

The pinned backend patch is
[0001-idle-mux-grace.patch](../buildroot/external/patches/tvheadend/0001-idle-mux-grace.patch).
After the last subscription closes, an eligible SAT-IP mux remains receiving for
a bounded window. Another service on the same mux UUID reuses it and changes
service PIDs. The old HTTP connection closes before the next opens; no dummy
subscription, second tuner or broader playback credentials are needed.

The SAT-IP frontend property `idle_grace` is milliseconds, defaults to 1000 and
is clamped to 2000. Missing saved settings inherit the SAT-IP default; explicit
zero disables it. Other input types do not inherit this default. Repeated stops
do not extend a deadline; expiry rechecks subscribers.

Different-mux tuning, forced stops, disabled/deleted inputs or muxes, failed or
unfinished services and input without recent packets bypass retention. Without a
replacement, reception stops after the bounded grace. Thus zero subscriptions
can coexist briefly with an active SAT-IP session; an idle frontend status row
alone does not prove tuner retention.

This is not a new RTSP retuning algorithm. Same-mux handover avoids stop/start;
different-mux changes still incur setup latency and can be slower. No fixed
channel-switching time or improvement on every transition is promised.

## Diagnostic overrides

Player flags are read on the next tune from the private RAM file
`/run/satip-player/switch-experiments.json`. Missing, omitted or invalid values
use normal defaults. Save any existing file before a comparison.

| Flag | Effect when true |
|---|---|
| `persistent_relay` | Reuse the preloaded relay supervisor; false selects legacy per-channel startup/cleanup |
| `saved_audio` | Seed a previously verified in-memory mpv track ID; revalidate live German/surround policy before unpause |
| `bounded_probe` | File-local 0.5-second analysis / 128 KiB probing; can fail to discover required audio |
| `adaptive_coalescing` | Shorter grace for qualifying isolated menu confirmations; bounded burst handling remains |
| `redundant_ipc` | Skip unchanged successful writes of controller-owned decoder/filter/audio properties |

A saved satellite audio PID is not an mpv track ID. Audio hints are bounded,
metadata/layout/path-keyed and invalidated on stale verification. Property caches
are invalidated on player restart, YouTube and reader handover. External edits to
owned properties invalidate a comparison.

For a temporary legacy-relay diagnostic, run as root on the Pi, after saving
existing overrides (this changes playback on the next tune):

~~~sh
printf '%s\n' '{"persistent_relay":false}' > /run/satip-player/switch-experiments.json
chown satip-player:satip-player /run/satip-player/switch-experiments.json
chmod 600 /run/satip-player/switch-experiments.json
~~~

Restore the previous override file afterwards, or remove this exact temporary
file if none existed, then change channel. Reboot clears runtime overrides.
Native `idle_grace` is separate: set it through the root-only loopback backend
admin API on the actual enabled frontend UUID, never through player credentials.
Set zero to disable or 1000 for the normal default; this saved setting survives reboot.
Do not copy another device's adapter configuration.

## Deployment and comparisons

Application relay changes use [supervised deployment](operations.md#in-place-development-deployment).
Native grace requires a matching patched backend binary. Use a fresh Buildroot
output for native patch/profile changes; stale package output is not automatically
invalidated. Verify pinned source, patch manifest, target architecture and features
before using the deployer's `--tvheadend-binary` option. A successful target executable
preflight is not provenance or fresh-image qualification.

Copy the reviewed switching harness to the Pi or use a checkout there. Run as
root with healthy playback and no scan, preview, Wi-Fi trial, pending recovery,
physical controls or other validation. The following interrupts TV:

~~~sh
PYTHONPATH=/opt/satip SATIP_RUNTIME_ROOT=/run \
python3 validation/measure_switching_on_pi.py \
  --confirm-interrupt --rounds 2 --phase-blocks 2 \
  --phase-order baseline-first --settle-seconds 10 --navigation-delay 0.75 \
  --channels 'Das Erste HD' 'arte HD' 'RTL Television' 'VOX' 'ZDF HD' \
  --small-experiments persistent_relay
~~~

Use channels actually present in your favourites. Independent opposite-order
blocks limit route/warm-up bias. Stage-one presets and the relay baseline explicitly
disable relay reuse; this is a diagnostic baseline, not the normal default.
`combined` includes saved audio, adaptive coalescing and redundant IPC, not
bounded probing or persistent relay. Use `mux_grace` with the actual
`--frontend-uuid` and `--mux-grace-ms 1000` for native comparison; relay stays
on in both native phases. The harness restores prior flags, grace and channel
on ordinary cleanup, not after SIGKILL or power loss.

Correlate sampled advancing audio/video, post-readiness cache pauses, capture
ownership and complete RTSP traces; an internal switch timer alone is insufficient.
The passive session observer and finite-release check are documented in the
[tool index](../validation/README.md). Physical first picture, sound, lip sync,
rapid input, paused-channel changes, scan/preview handover, source failure and
endurance require separate acceptance. Do not convert historical timing results
into performance guarantees.
