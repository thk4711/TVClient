# Tvheadend and SAT-IP configuration

**Audience: developers and advanced administrators.**

[Documentation index](README.md) · [Configuration](configuration.md)

## Terminology and prerequisites

- **SAT-IP server:** external tuner converting satellite transport to IP.
- **Network:** Tvheadend's DVB satellite/network definition.
- **Mux:** transponder/multiplex carrying multiple broadcast services.
- **Service:** programme identified in that mux.
- **Channel:** stable mapped backend entry requested by the player.
- **Favourite:** application-ordered selection of mapped channels.

First setup targets Astra 19.2°E with a conventional LNB/multiswitch; the default
source/position is 1. Select an available frontend on your server during setup.
A tuner number is a device-specific choice, not a universal requirement.
Current [acceptance limits](validation.md) apply.

## Fresh backend setup

The image includes the pinned Tvheadend build. Root provisioning creates protected
backend accounts/configuration before startup; the authenticated first-boot UI
handles server discovery, tuner/source/network selection, transponder scanning
and initial channel mappings. Follow [first boot](buildroot-first-boot-setup.md).

Provisioning uses Basic-compatible authentication (`digest=2`), independent admin
and playback accounts and loopback-only ACLs. It does not use unrestricted
first-run `-C` mode. Setup disables other frontends and uncontrolled background
tuning for the single-tuner workflow. Actual UUIDs and audio PIDs are discovered
and saved rather than copied from the old example database.

Never hand-edit live backend database files as routine administration. Use the
appliance UI for supported changes. Advanced API work must use local protected
admin access and coordinate with scanning/playback ownership.

## Listeners and launch options

Application HTTP/API/streams use `http://127.0.0.1:9981`. HTSP also listens on
`127.0.0.1:9982` but is unused by appliance playback. Loopback TCP binding does
not imply there are no SSDP/discovery UDP sockets.

The root supervisor drops to the `tvheadend` account (UID 424) and launches:

~~~text
/usr/bin/tvheadend -c /data/store/tvheadend --bindaddr 127.0.0.1 --nosyslog
~~~

It adds the validated selected-server `--satip_xml` URL when available. The
process stays in the foreground and its logs go to
`/run/satip-supervisor/tvheadend.log`. The image removes upstream automatic
Tvheadend startup in favour of this supervisor. SAT-IP client support is enabled;
Tvheadend's own SAT-IP server is compiled out, so no `--satip_rtsp` option is used.
The image also enables IPTV for [Enigma2 reception](enigma2.md).
Its managed network uses raw HTTP service streams, one stream at a time, no
idle/startup scans, no automatic playlist and no descrambling. Root source
selection preserves the SAT-IP frontend configuration and changes only which
managed source network is enabled.

## Accounts and playback ACL

| Role | Intended rights |
|---|---|
| Backend administrator | Loopback UI/API administration needed by management |
| Playback | Loopback basic HTTP streaming, `pass` only, one connection, permitted channel-number range |
| Browser login | Application HTTP Basic login, not a Tvheadend account |

Playback has **no** admin, web UI, HTSP, or DVR rights. Preview uses the same
restricted playback account through a separate protected credential copy.
Both backend ACLs are constrained to `127.0.0.1/32` in the reference installation.
The backend must allow Basic authentication for mpv's private Authorization include.

Tvheadend stores displayed channel numbers scaled by one million: 1–3 means
`1000000–3000000` in the tested API. Do not confuse stored numbering with UUIDs
or favourites order. `playback_acl_uuid` identifies the access entry, not the user
password record. Grid reads must request sufficient limits and verify totals;
the default grid page may hide much of the network.

## Supported channel policy

[candidate()](../satip_controller/backend.py) fails closed. Offered services require:

- Enabled service with `encrypted=false`, no CA identifiers/components.
- Exactly one supported H.264 or MPEG-2 video stream.
- Reported dimensions no larger than 1920×1080; unknown dimensions can remain
  unknown until playback and do not establish compatibility.
- German `ger/deu/de` MPEG audio (MP2), type 0 preferred, type 1 fallback.
  Accessibility types 2/3 and unknown/foreign audio types are excluded.

HEVC/UHD, radio, AC3-only services, conditional access, and unsupported video are
not offered. Discovery eligibility is not physical playback qualification,
particularly for interlaced H.264 and type-1 fallback audio.

## Scan and favourite-save mutations

Scanning visits only muxes in `network_uuid`, optionally only enabled ones.
Full scans (including first setup) first supplement Astra 19.2°E networks from
**all** installed `/usr/share/dvb/dvb-s/Astra-*-19.2E` DVBv5 tables. Buildroot’s
Tvheadend dependency installs these tables. Repeated broadcaster section names
are separate transponders; overlapping spacecraft tables are deduplicated by
frequency/polarisation. Only Ku-band (10.7–12.75 GHz) definitions are imported;
Ka-band feeds in the tables cannot use the supported conventional LNB.
Missing muxes are created disabled with EPG off, under
the scan lease after playback pauses. Existing UUIDs, mappings, tuning and
flags are preserved; readback checks completeness. Enabled-only scans do not
expand the network, and other orbital positions are never supplemented.

This bypasses a Tvheadend scanfile identifier truncation/collision: the pinned
backend exposes four spacecraft tables under the same `dvbs/geo/dvb-s_> 19.2E:Astra-`
key. Selecting that key imports only one table, not the full orbital position.
The missing German SAT.1 / ProSieben / kabel eins SD transponder is
**12545 MHz H, DVB-S QPSK, 22000 kSym/s** (FEC auto). The installed tables
contain it; older incomplete networks can omit it. A full scan repairs
existing installations without resetting setup or replacing favourites.
Private `last-scan.json` includes `added_muxes` for full scans.

Before tuning, the player acknowledges its pause and backend subscriptions must
be zero. Each mux is temporarily enabled with scan requested and EPG disabled;
original enable/EPG fields are journaled first and restored afterward. Both
`scan_result=1` (OK) and `3` (OK partial) publish supported services.
Cancellation retains published discoveries and attempts restoration.

Failed restoration retains a journal and recovery pause. Recover using the web
interface/startup, not by deleting state. Avoid simultaneous backend administration
because rollback could overwrite intervening mux edits.

Saving favourites revalidates selected service metadata, reuses existing mappings,
creates only required new mappings, enables their muxes/passive EPG, and writes
the application library last. Deselected backend mappings are **not deleted**.
API snapshots precede mutation. A failed multi-step save retains old application
favourites but can leave completed backend changes for review/retry.

If the playback number range grows, all potentially exposed mappings are
revalidated; unknown/encrypted/unrelated unsupported mappings can block expansion.
Only the range maximum changes, not playback admin/profile/connection permissions.
The backend **administrator** must not share the initial 1–3 playback range:
the pinned bootstrap uses `channel_min=0`, `channel_max=0` (unrestricted metadata
visibility) for that loopback-only account, with streaming rights still disabled.
A restricted admin range can silently hide new channels and all of their EPG events,
even when administrative idnode/grid reads and broadcast collection succeed.

## Passive EIT and programme information

Broadcast EIT is collected while receiving a live mux. Mux EPG and mapped-channel
`epgauto` must be enabled, but the working frontend's `ota_epg` is disabled to
avoid dedicated EPG-only tuning. The tested backend can change mux `epg=1` to
`101` after identifying EIT; both represent enabled EPG in that build.

The web process reads bounded `epg/events/grid` results and publishes a secret-free
cache for the player. It does not open an extra stream to populate the cache.
Unvisited transponders can lack events and initial collection can exceed five
seconds. Keep system time/timezone correct for displayed schedules.

Buildroot enables hourly persistence of Tvheadend's full EPG database on the data
partition and flushes completed snapshots to storage. The player/web JSON cache
remains in RAM and is promptly warmed from the restored backend guide in bounded
batches at web startup and after significant clock corrections. Normal background
refresh then resumes. On a Pi without an RTC, programme information still needs
the correct time from Chrony; slow time synchronization is separate from missing
EPG persistence. See
[EPG persistence](buildroot-services-persistence.md#epg-persistence) for power-loss
limits and diagnostics.

Saving favourites enables passive EPG for mapped channels. Routine EPG refresh
requires no separate provisioning or maintenance script.

## Same-transponder handover

The pinned backend includes a default-enabled native `idle_grace` SAT-IP frontend property
for [stage-three session preservation](channel-switching.md#same-transponder-mux-grace).
It keeps an unsubscribed mux receiving for up to a bounded 0–2000 ms window,
allowing another service on that mux to reuse reception. No dummy subscription
or second playback connection is created. The source/image and installed-binary
SAT-IP default is **1000 ms**, the normal SAT-IP default. Missing saved
settings use that default; explicit zero disables it. Other input types are unchanged. Different-mux and forced stops bypass the
window, and failed/unfinished services or stale input do not retain a bad session.
See the linked guide for defaults, comparisons and deployment restrictions.
