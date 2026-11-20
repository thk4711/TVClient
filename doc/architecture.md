# Architecture

**Audience: developers and advanced administrators.**

[Documentation index](README.md) · [Configuration](configuration.md)

## Component responsibilities

The Pi is a SAT-IP **client appliance**. An external server receives the satellite
signal; Tvheadend controls tuning and exposes local MPEG transport streams.
Python coordinates playback rather than implementing SAT-IP or a video renderer.
[Enigma2 support](enigma2.md) uses the same backend: an OpenWebif
bouquet importer creates restricted raw HTTP IPTV inputs, while playback remains
loopback MPEG-TS. Separate source profiles retain lineups; only one is active.
OpenWebif EPG fills the existing cache when a service stream omits EIT.

~~~mermaid
flowchart LR
    RF[Satellite / LNB / multiswitch] --> SAT[SAT-IP server]
    SAT -->|RTP / RTCP over LAN| TVH[Tvheadend]
    TVH -.->|RTSP control| SAT
    TVH -->|Loopback HTTP MPEG-TS, profile pass| MPV[Persistent mpv child]
    MPV -->|OpenGL / DRM-KMS| HDMI[HDMI video and OSD]
    MPV -->|ALSA capability-aware PCM| AUDIO[HDMI audio]
    INPUT[USB keyboard / kernel-mapped GPIO IR] -->|evdev| CTRL[Python controller]
    CTRL -->|Unix JSON IPC| MPV
~~~

- **Tvheadend:** SAT-IP control, network/mux/service records, mapping, stream
  authentication, and broadcast EIT collection.
- **Player service:** input selection, channel lifecycle, state, OSD, retries,
  and supervision of one owned mpv process.
- **Web service:** FastAPI/uvicorn management, backend administration, scans,
  favourite saves, EPG cache publication, and optional FFmpeg browser preview.
- **Buildroot appliance layer:** fresh provisioning, SAT-IP discovery/registration,
  password-free first setup, persistent data gates, owned service supervision and splash.

No desktop/compositor runs in the direct HDMI playback path. The `pass` profile
transcodes neither video nor audio; transport-table rewriting means its output
need not be byte-identical to the incoming stream.

HDMI OSD uses mpv `osd-overlay` ASS events over JSON IPC: vector charcoal panels
and styled text on a scalable 1920×1080 canvas. A main layer holds banners or
persistent panels, and an independent volume layer leaves EPG scrolling intact.
Transient expiry is controller-owned and guarded against stale timers/reconnected
players. Provider text is escaped separately from trusted drawing/style tags.

## USB time shifting

Optional HDMI time shifting uses stream-copy TS segments on a root-helper-mounted
USB ext4 filesystem, not mpv's append-only disk cache. One backend subscription
feeds a raw live relay and a background RAM-only segment supervisor. Live mpv
playback does not wait for FFmpeg probing or segment completion; archive playback
concatenates retained segments over the same private loopback server. Archive
seeks reuse the existing reader’s seekable mpv RAM cache; cache misses reload
only that reader. Rapid input accumulates into one requested absolute position,
with one worker and one replaceable pending target rather than queued reloads.
First Pause
preserves the current raw frame and starts a separate copier from the bounded
RAM prebuffer to USB. Resume preserves the broadcast timestamp through a cached
exact archive seek. Return to live stops the copier, deletes USB history and returns
to the raw relay.
The RAM supervisor verifies tmpfs and bounds its short window independently of
USB I/O. The USB copier checks mount/readiness/free space, rolls its own history,
and applies a per-file size backstop. Controller death closes both lifetime pipes.
Duration/space limits roll history instead of returning to live. Expired viewing
positions move to retained content, preserving pause. Intentional pause/seek is
separate from management leases and guarded in stall detection.
See [storage, controls and limitations](timeshift.md).

The default [stage-two relay optimization](channel-switching.md#persistent-relay) reuses
a preloaded Python fork supervisor, with a separate capture process per channel.
It confirms upstream HTTP release before allowing a new generation, while
FFmpeg/file cleanup can finish outside the switching path. Old/new stream
sockets, tokens, queues and storage remain isolated. Stage two does not itself
preserve SAT-IP sessions. No runtime flag is needed after restart/reboot. An explicit private
`persistent_relay: false` override retains the legacy path for diagnostics.

The default [stage-three native patch](channel-switching.md#same-transponder-mux-grace)
keeps an unsubscribed Tvheadend mux alive for a bounded handover window. A new
same-mux channel reuses reception without another playback connection; other-mux
tuning and forced stops bypass the window. With no replacement, the tuner
releases within at most two seconds. Relay/audio generation isolation is unchanged.
SAT-IP frontends default to 1000 ms in the installed binary and future images.
Explicit saved zero disables retention; other input
types are unchanged. The known different-mux latency tradeoff is recorded in the guide.

## Management and preview

~~~mermaid
flowchart LR
    BROWSER[Browser] -->|HTTP UI / API| WEB[Web service]
    WEB -->|Admin API, loopback| TVH[Tvheadend]
    TVH -->|Restricted authenticated stream| PIPE[Python-owned pipe]
    PIPE --> FFMPEG[FFmpeg]
    FFMPEG --> HLS[Private HLS files in /run]
    HLS -->|Same-origin playlist / segments| BROWSER
    WEB --> CACHE[Secret-free EPG cache]
    CACHE --> CTRL[Player controller]
~~~

The web account has backend-admin credentials but no DRM/input/audio device
access. Preview uses a separate copy of restricted playback credentials.
FFmpeg receives bytes through stdin, not an authenticated URL in arguments.
HD H.264 video is copied; MPEG-2 SD is deinterlaced and encoded to H.264. Selected German audio
becomes stereo AAC. The browser uses bundled hls.js or native HLS.

## Single-tuner ownership

The reference backend has one usable tuner. HDMI viewing, scanning, and preview
are exclusive. This coordination is not a general tuner reservation against
external clients.

~~~mermaid
sequenceDiagram
    participant Web as Web manager
    participant Shared as Lease/status files
    participant Player as Player controller
    participant TVH as Tvheadend
    Web->>Shared: Publish scan or preview lease
    Player->>Shared: Read lease
    Player->>TVH: Stop current stream through mpv
    Player->>Shared: Publish paused status
    Web->>TVH: Wait for zero subscriptions
    Web->>TVH: Scan or preview
    Web->>Shared: Release lease after cleanup/restoration
    Player->>TVH: Resume requested channel
~~~

The manager's asyncio lock serializes scan, favourite mutations, and preview.
Non-tuning refresh is also blocked during exclusive operations. Unrelated backend
subscriptions prevent tuning rather than being terminated.

Physical input takes priority over browser streaming. Preview leases carry a
public generation identifier independent of the private session token. On a
physical key-down, the player publishes that generation to its web-readable
`local-takeover.json`. The web service watches it even during stream startup,
closes its stream/encoder, then expires the lease. The player waits for lease
release before applying the button action. Old requests cannot stop new sessions.
Leases use a boot ID and monotonic deadline, so wall-clock/NTP changes cannot
expire or extend ownership. Preview also records web/encoder process start
identities: a live owner retains the tuner while cleanup is pending even if the
deadline expires. Shared handover checks player acknowledgement and backend
subscriptions before tuning.
Browser streaming has no absolute time cap; abandoned streams still expire.

Before each scan mutation, original mux fields are journaled in
`mux-rollback.json`. Failed restoration leaves a persistent recovery flag;
playback stays paused even after lease expiry. Web startup or explicit recovery
retries restoration. Backend thread work is joined despite repeated caller
cancellation before rollback proceeds; repeated stop requests join the same
cleanup instead of cancelling it again. Preview uses a separate expiring lease and no mux journal.

## Playback lifecycle

The controller distinguishes **requested**, **loading**, and **playing** channel
UUIDs. Requests coalesce; generation checks reject obsolete loads. Once a tune
has started, further channel buttons update the requested selection without
interrupting SAT-IP setup. The in-flight load settles (confirmed advancing
playback with main audio, failure, or the load deadline), then only the latest
selection is loaded. Superseded playback is never published or saved as playing.
Management pauses and shutdown still interrupt promptly. This avoids servers
rejecting new RTSP SETUP requests after teardown during initial tuning. The previous
stream stops before changing decoder policy/filters and loading the next channel. MPEG-2 uses
double-rate BWDIF, cleared for H.264.

Readiness requires the requested URL, non-idle video/audio, selected main audio
PID, and advancing playback time. Unsolicited mpv events are not completion:
they can belong to old loads. IPC matches request IDs, serializes writes, and
bounds reply waits.

Failures retry with backoff capped at 30 seconds. The player reaps only its child
and refuses a live foreign runtime socket. Buildroot supervisors terminate/reap
the owned process generation before restart. An IPC timeout invalidates the
connection and enables software decoding for the controller lifetime; restarting
the service retries hardware. Input discovery retries independently of playback.

## Persistent and shared data

| Data | Writer | Readers |
|---|---|---|
| `state.json` | Player | Player only |
| `status.json` | Player | Web, administrators |
| `library.json` | Web | Player, web |
| Scan/preview leases and `epg.json` | Web | Player, web |
| Catalogue, scan summary, rollback/snapshots | Web | Web, administrators |
| mpv socket/auth and generated HLS | Owning service | Owning service only |

JSON writes use same-directory temporary files and atomic replacement. Durable
state/library/rollback writes include fsync; key durable writes run through a
serialized off-loop writer whose completion is joined before cancellation exits.
Transient status, leases and EPG avoid fsync and live under `/run` in deployed
services. Library/EPG readers cache parsed snapshots by file identity.
Private state saves after two seconds of settled changes and at shutdown. Failed
writes remain dirty, retry, and expose `state_write_failed` in player status.
The player reloads the library, retaining the last valid version if a replacement
is malformed. Favourite saves commit the library last; backend changes completed
before failure may remain. Saves compare the supplied canonical library revision
under the manager lock before mutation, preventing stale browser overwrites.

See [operations](operations.md) for paths/permissions, [Tvheadend](tvheadend.md)
for mutations, and [security](security.md) for trust boundaries.

## Browser Enigma2 source preparation

`satip_appliance/source_api.py` exposes shared first-setup/management discovery
and bouquet endpoints. `source_browser.py` probes bounded local IPv4 targets,
then stages one managed IPTV mux per normalized DVB service reference. Its
importer holds the normal manager edit lock and playback scan lease while
preparing, probing and publishing; playback/preview remain exclusive. It reuses
`Manager.scan` and the existing metadata/channel/ACL validation rather than
trusting bouquet names. Source APIs never call remote zap/configuration methods.

The importer stages its library under `web/enigma2-import`, journals network
enablement and active-lineup publication, and persists the Enigma2 profile only
after successful validation. Failed or cancelled scans leave disabled managed
muxes reusable on retry, not a replacement lineup. Root startup source recovery
replays fixed-path journals; browser recovery handles interrupted import state.
First setup copies the validated staged profile into its normal favourites flow;
root completion independently checks the resulting profile/lineup and refuses
pending import journals. Source activation remains the root-owned handover.

Startup routing/server readiness follows the active source profile. For Enigma2,
the root startup gate checks its receiver and enabled managed IPTV network; it
does not require an inactive SAT-IP description server or frontend. First-setup
receiver/network identity is durably committed before mux preparation so a
stopped/interrupted import resumes the same receiver without publishing an
incomplete lineup. Active-lineup publication has its own fixed-path rollback
journal in addition to per-mux and network restoration.
