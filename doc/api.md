# HTTP API reference

**Audience: developers and advanced administrators.**

[Documentation index](README.md) · [Security](security.md)

Source: [web.py](../satip_controller/web.py). This is the application API, not
Tvheadend's API. The appliance wrapper in
[satip_appliance/web.py](../satip_appliance/web.py) serves LAN HTTP on fixed
`0.0.0.0:8080`. Automatic OpenAPI/Swagger/ReDoc endpoints are disabled.

## Authentication and request rules

All UI, assets, API, and preview media follow `require_auth`. When true, supply
HTTP Basic **browser** credentials, not backend-admin credentials. Missing/invalid
login returns 401 with `WWW-Authenticate: Basic`. Open the appliance on a trusted
LAN; HTTP Basic is unencrypted and the bundled SSH disables forwarding.
When false, any reachable client can perform management/preview actions.
Configured Buildroot device controls follow the same login policy.

All non-GET/HEAD actions require `X-Satip-Action: 1`. If Origin is supplied, its
HTTP/HTTPS scheme and authority must match the request Host. Appliance setup and
configured management validate against current local IPv4 addresses and hostname
on every request, including addresses assigned after startup. Send JSON bodies with `Content-Type: application/json`.
Bodies are limited to 32 KiB, including chunked/misdeclared requests, with a
10-second receive deadline. Request models reject extra fields and type coercion
(e.g. strings in place of booleans). Responses are no-store; do not log Authorization headers or session tokens.

UUIDs below are 32 lowercase hexadecimal characters. Favourite/preview actions
use **service UUIDs**, while player status and the library use **channel UUIDs**.

## Endpoints

| Method / path | Request | Success / purpose |
|---|---|---|
| `GET /` | None | 200 HTML management UI |
| `GET /request.js` | None | 200 shared bounded JSON request helper |
| `GET /api/setup/status` | None | 200 `{"state":"configured"}` for setup-to-management transition |
| `GET /ui.js` | None | 200 local application script |
| `GET /app.js`, `GET /app.css` | None | 200 local navigation/dashboard script and shared theme |
| `GET /hls.min.js` | None | 200 bundled browser HLS library |
| `GET /youtube.js`, `GET /qrcode.js` | None | 200 YouTube controls and locally bundled QR encoder |
| `GET /api/status` | None | 200 scan/player/preview state and save acknowledgement |
| `GET /api/telemetry` | None | 200 read-only system, network and active-tuner measurements |
| `GET /api/channels` | None | 200 catalogue, ordered favourites, previewable services |
| `GET /api/settings` | None | 200 `{"version":1,"osd_timeout_ms":5000,"osd_language":"en"}` (default if not saved) |
| `PUT /api/settings` | `osd_timeout_ms`: integer 500–30000; `osd_language`: `"en"` or `"de"` | 200 saved versioned settings; persistent, applied without player restart |
| `GET /api/network` | None | 200 read-only DHCP mode and per-interface address state; no writable network startup preference |
| `POST /api/refresh` | No payload required | 200 `{"found": count}`; non-tuning backend refresh |
| `PUT /api/favourites` | `services`, `base_revision`, optional `save_id` | 200 `saved`, `save_id`, `library_revision` |
| `POST /api/scan` | `enabled_only`, `confirm_pause` | 202 `{"started": true}`; background search |
| `POST /api/scan/cancel` | No payload required | 200 scan status after cancellation |
| `POST /api/scan/recover` | No payload required | 200 idle/recovered scan status |
| `POST /api/preview` | `service`, `confirm_pause` | 202 session token and relative playlist URL |
| `POST /api/preview/heartbeat` | `session` | 200 preview status |
| `POST /api/preview/stop` | `session` | 200 stopped preview status |
| `GET /api/preview/{session}/{name}` | None | 200 session playlist/segment |

Status includes bounded current/next programme metadata from the shared EPG
cache; there is no separate browser EPG endpoint.

## Buildroot appliance controls

Older timeout-only `PUT /api/settings` requests preserve the saved language.
A missing language in an existing settings file defaults to English. Invalid
language values are rejected with 422.

Configured Buildroot adds `device_settings: true` to `GET /api/settings` and
provides `/device.js` plus `GET /api/device`, `GET /api/device/storage`, and `POST /api/device`.
Storage status lists USB disk identity tokens, model/capacity/serial, filesystems,
in-use status, mount/readiness state, mounted device identity (`device`, `device_id`),
capacity (`total_bytes`), saved budget (`max_bytes`), and free bytes; never credentials.
These routes are installed by the appliance web wrapper. All device endpoints
require HTTP Basic web credentials when browser authentication is enabled.
Status reports hostname/timezone, installed timezones, Wi-Fi/access state and an
optional trial ID/countdown, never passwords or password hashes.

POST accepts `{"action":"...","args":{...},"current_password":"..."}`.
On authenticated installations, the current password is mandatory for SSH, storage unmount and
maintenance actions below; it is the web password, not the root password.
Password changes follow the current login policy and require no password re-entry. Ordinary action-header, Origin,
body-size and Host checks also apply.

| Action | Exact args | Current password |
|---|---|---|
| `device` | `hostname`, `timezone` | No |
| `scan_wifi` | Empty object | No |
| `wifi` | `ssid`, `password`, `country` | No |
| `wifi_enabled` | `enabled`: boolean | No |
| `wifi_confirm`, `wifi_revert` | `id`: current trial ID | No |
| `web_password`, `root_password` | `password`, `confirm` | No |
| `ssh` | `enabled`: boolean | Yes |
| `satip_tuner` | `{"frontend":"32-character backend frontend UUID"}`; active SAT-IP, configured server only; queued restart, preserves lineup | Yes |
| `source_select` | `{"source":"satip"}` or `{"source":"enigma2"}`; source must already be imported | Yes |
| `restart_player`, `reboot`, `shutdown` | Empty object | Yes |
| `storage_mount` | `id`: listed disk identity token, `minutes`: integer 0–120 (0 disables the retained-history time limit), `gib`: number 1 up to device capacity in GiB | No |
| `storage_format` | Same as mount, plus `confirm`: boolean `true` after the user accepts the data-loss warning | No |
| `storage_limit` | `id`: mounted `device_id`, `gib`: number 1 up to mounted device capacity in GiB; disables the retained-history time limit | No |
| `storage_eject` | Empty object; safely unmounts and retains remembered device/budget | Yes |
| `storage_forget` | Empty object; requires unmounted storage, clears configuration only | Yes |

Success is 200 with a short message; Wi-Fi start also returns `trial_id`.
Maintenance is scheduled after the response, not completed by it. Authentication
failure is 401, incorrect/missing confirmation password 403, invalid request 422,
refused/failed settings 409 and unavailable helper 503. Invalid device requests
are sanitized rather than echoing secret-bearing request fields.

Storage formatting is synchronous (allow up to 180 seconds), destructive, and
revalidates the listed USB identity/usage before writing. Mounting never formats;
eject revokes buffering and restarts the owned player around unmounting. Storage
mutations are refused during scan/preview/recovery. See [time shifting](timeshift.md).
Player status includes secret-free `timeshift` state: enabled (active USB session),
available (ready to activate), mode (`off`, `ram`, `usb`), usb_writing (USB worker
still running), ram_bytes and ram_prebuffer_seconds, paused, delay,
retained seconds/bytes, rolling, warning, estimated position_expires_seconds,
and any pending boundary/storage notice. Storage status adds remembered and
safely_removed booleans; saved configuration survives safe removal.

Wi-Fi changes must be confirmed within 90 seconds or revert. Pending trials
serialize changes; active scan/preview/recovery blocks network and maintenance
actions. Device status additionally reports `tv_source.active` and
`tv_source.available` for the [Enigma2 source](enigma2.md).
`source_select` acknowledges a queued switch before stopping/restarting web and
HDMI playback; clients should reload once the selected source becomes active.
`satip_tuner` likewise acknowledges a queued tuner change; poll device status
until its message is `SAT-IP tuner applied.` or reports failure, then reload.
`GET /api/sources` includes `satip.name`, `address`, `url`, `connected`,
`configured_frontend`, `tuners` (UUID, server frontend number, configured flag),
and `receiving` (backend input labels with nonzero data rate) and `tuning`
(inputs requested but not delivering data). Receiving inputs are separate from saved
configuration. Status failures return an explicit error, not a connected flag.
See [appliance settings](device-settings.md) for limits and recovery.

## YouTube receiver

`GET /api/youtube` returns enabled/state/active/video/error and an unexpired
12-digit pairing code, never screen identity or session/media tokens.
`POST /api/youtube` accepts only `{"action":"pair"}`, `{"action":"disable"}`
or `{"action":"unlink"}` and returns 202 accepted. Actions are applied
asynchronously by the player. The existing login/Origin/action-header policy
applies; `/youtube.js` is an authenticated asset when login is enabled.
When YouTube owns HDMI, player status includes `source: "youtube"` and TV is
paused. The private media bridge is not a web API. See [YouTube](youtube.md).

## Buildroot first setup

While installation is `unconfigured`, `satip_appliance/web.py` serves setup on
port 8080. First-setup endpoints require no browser login and close once the
installation becomes configured. The same Host, Origin and action-header
checks apply.

| Method / path | Request | Success / purpose |
|---|---|---|
| `GET /api/setup/status` | None | Discovery/registration progress, selected server, actual tuners, mux lists and scan state |
| `POST /api/setup/discover` | Empty object | 200, state `searching`; coalesced background LAN discovery |
| `POST /api/setup/server/add` | `url` | 200 validated server description; adds a manual discovery result |
| `POST /api/setup/server/select` | `url`, `udn` from a discovery result | 202, state `connecting`; persist selection and register with Tvheadend |
| `POST /api/setup/scan` | `server_uuid`, `frontend_uuid` | 202; automatically select Astra transponders and start exclusive search |
| `POST /api/setup/cancel` | Empty object | Stop search, restore settings and retain discovered channels |
| `POST /api/setup/recover` | Empty object | Restore settings after an interrupted or failed scan |
| `POST /api/setup/finish` | `services`: 1–500 service UUIDs | 200 `{"state":"completion-pending","message":"Reload after management restarts"}`; map lineup and request root validation/transition |
| `POST /api/setup/config/restore` | Empty object | 200 `{"state":"restored"}` after root confirms public FAT template replacement; saved settings are retained |

Setup status includes the committed `frontend_uuid` and top-level
`preferred_tuner_number` (an initial UI default, not an enforced tuner restriction),
and `recovery_pending`. The latter is true only when a rollback journal remains
outside an active scan; a live scan normally has a temporary journal. `scan`
includes `completed`, `total`, `found`, `multiplex`, `mux_elapsed` (seconds on the
current transponder) and `failed_muxes` as available. Legacy scan clients may
still supply a validated `scanfile` and `confirm_scan`; the new UI requires neither.
Another available frontend on the same committed server may be selected for a
subsequent setup scan, while idle and before saving the lineup. Existing network
and candidate identities are retained; active scans, subscriptions and pending
recovery block changes.

`GET /`, `/setup.js`, `/request.js`, and `/app.css` serve the password-free first-run UI.
Finish does not itself authorize playback: root revalidates the prepared configuration
before activation. Poll status until normal management returns `state=configured`.
Config restoration accepts no paths or contents; failures return 409, and missing
root confirmation within ten seconds returns 504. See
[secret handling](provisioning.md#secret-handling-after-provisioning).

Poll status to obtain `discovery.state` (`idle`, `searching`, `complete`, `error`)
and `registration.state` (`idle`, `connecting`, `ready`, `error`). `discovery` also
contains `servers`, interface names and warnings. Server records contain `url`,
`udn`, `name`, `address`, `manufacturer`, `model`, `capabilities` and advertised
satellite `tuners` count. UDN is the device's `uuid:...` identity, distinct from
Tvheadend's 32-hex server/frontend identifiers. Only registered frontend UUIDs
are accepted for scanning. See [first setup](buildroot-first-boot-setup.md).

## Status and channel catalogue

`GET /api/status` returns these top-level fields:

- `scan`: state (idle/starting/pausing/running/stopping/complete/cancelled/error),
  optional progress `completed/total/found`, `multiplex`, `failed_muxes`,
  `partial_muxes`, `recovery_pending`, and redacted `error` as applicable.
- `player`: normally version, owner PID, requested/loading/playing channel UUIDs,
  paused, error, and channel name. Unavailable/stale player status can instead
  contain an error without the full schema; clients must handle this. Diagnostics
  include redacted `reason`, `retry_count`, `switch_seconds`, `state_error`, and
  bounded `health` properties (cache, dropped frames, A/V sync, hardware decoder).
  Missing/unsupported health values are null; no stream URLs or credentials appear.
- `player.timeshift`: enabled, paused, behind_live_seconds, available_seconds,
  timeline_seconds, bytes, notice; additive rolling, warning and
  position_expires_seconds (estimated countdown or null). Programme lists remain live EPG.
- `preview`: state, enabled, and channel/reason/error where applicable. It does
  **not** expose the owning session token.
  Active streams also identify their `channel_uuid`.
- `catalogue_revision`: process-local revision for catalogue reloads.
- `library_revision`: canonical library SHA-256, stable across web restarts.
- `last_favourites_save_id`: most recent successful save ID, or null.

Process-local catalogue revision/save acknowledgement reset on web restart; they are not durable event
history; `library_revision` does not reset. Completed/cancelled/error scan summaries are persisted separately.
Both `player` and `preview` include a `programmes` list with at most two entries
containing `title`, `start`, and `stop` (Unix seconds). Missing EPG yields an empty
list; future programmes can appear when no current programme is known.

### Receiver telemetry

`GET /api/telemetry` returns `version: 1` and `system`, `network`, `reception`,
`performance` sections (the latter is an additive extension). Each section has a `state`
(`ok`, `stale`, `unavailable`) and, when a
sample exists, Unix `sampled_at`. A source failure retains the previous sample
with its original timestamp. Unsupported individual fields are null. Collection
is cached for three seconds per source and shared across concurrent browsers.

- `system`: hostname/model/OS, uptime seconds, CPU percent and temperature °C,
  memory/storage bytes, optional power flags and allowlisted build identity.
- `network.interfaces`: interface name/link, address list, IPv4 default-route
  flag, link speed in Mbit/s, RX/TX byte/error/drop counters and sampled bit/s.
  Optional `wifi` contains SSID/state, dBm, MHz and negotiated link rates.
  When sysfs does not report a positive link speed, connected Wi-Fi uses its
  current TX PHY rate (RX if TX is unavailable). This is a radio link rate, not
  measured throughput; disconnected or unsupported readings remain null.
- `reception.streams`: backend tuner and transponder display names, signal and
  quality `{value, unit}`, bitrate bit/s, subscription count, backend error
  counters and changes since the last sample. Unknown scales return null,
  relative scales use %, and decibel scales use dBm for strength/dB for SNR.
  No active streams yields `activity: idle`, not an inferred zero signal.

- `performance`: total CPU percent (0–100), cores (name, percent, frequency_mhz),
  load_averages (1/5/15 minutes), memory_total/available/used/cached and
  swap_total/used (bytes), disks (whole physical device name,
  read_bytes_per_second, write_bytes_per_second), processes (up to 12 rows:
  pid, name, cpu_percent, memory_bytes), and processes_truncated. Process CPU
  is 100% per fully busy core, unlike total/per-core percentages. Memory used
  is total minus available; process memory is RSS. Process scans stop at 512
  entries or a 250 ms scan budget and do not read command lines/environments.
  CPU/disk/process rates need two samples; gaps over 15 seconds and counter
  resets return null rates. PID/start-time pairs guard against PID reuse.
  Browser graphs retain at most five minutes in memory, not on the appliance.

The endpoint never starts subscriptions, scans or changes tuner settings.
Performance collection is independently cached and failure-isolated like the
other sections, and runs under the unprivileged web account.

### Channel catalogue

`GET /api/channels` returns `channels` (available candidates), `favourites`
(ordered service UUIDs), `library_revision`, and `previewable` (mapped service UUIDs). Candidate rows
can include service/mux/name/video/audio/encryption metadata or fallback library
rows. Do not assume every row has identical optional metadata. Previously mapped
channels remain previewable after deselection from favourites. Mapped channel
names come from the saved library, matching the favourites OSD. Library entries
missing from a partial/stale catalogue are included so saved favourites remain
visible with their names and order intact.

## Favourite saves

Example body with illustrative identities:

~~~json
{
  "services": ["22222222222222222222222222222222"],
  "save_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "base_revision": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
}
~~~

Use the actual `library_revision` from `GET /api/channels` as `base_revision`;
the example hash above is illustrative. Missing preconditions return 428. A stale
revision returns 409 before backend mutation. Fetch the current catalogue and
explicitly reconcile/reload local edits before retrying. Successful saves return
the new revision. This protects concurrent browser edits, including web restarts.
The UI follows remote changes when clean and preserves dirty edits with an
explicit reload control on conflict.

`services` must have 1–500 unique, supported service UUIDs. `save_id`, when supplied,
is a 32-character lowercase hex request identifier. Poll status to confirm that
exact successful operation if its PUT response is lost. Another save ID is not
acknowledgement of this request. Retain newer local edits if late acknowledgement
arrives. Save creates/reuses mappings and commits the library last; backend changes
completed before an error may remain even when old application favourites are retained.

## Scan control

~~~json
{"enabled_only": false, "confirm_pause": true}
~~~

Defaults are false for both fields. `confirm_pause=true` is required; without it
scan returns 400. `enabled_only=true` restricts search to enabled muxes. Poll
status/catalogue for incremental results. Cancellation restores the current mux
and keeps discoveries; failed restore leaves recovery pending and TV paused.
Scan acceptance is asynchronous: a 202 does not prove tuning/search succeeded.
Refresh does not tune but is refused while another exclusive operation holds the lock.

## Preview sessions

~~~json
{"service": "22222222222222222222222222222222", "confirm_pause": true}
~~~

The UI starts directly without a popup but still sends `confirm_pause=true`.
A successful 202 response has this shape:

~~~json
{
  "session": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "url": "/api/preview/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/index.m3u8"
}
~~~

Use the token in heartbeat/stop JSON: `{"session": "..."}`. Poll preview state
for readiness; 202 does not mean the playlist already exists. Send heartbeats
well within 20 seconds; the bundled UI sends them independently every three
seconds plus request time, with a five-second request timeout. Successful media
requests also renew browser activity. There is no absolute session duration limit;
startup retains its 45-second bound. Physical keyboard/remote input stops streaming
and publishes a friendly reason; clients must not automatically reclaim it.
Do not attempt simultaneous HDMI/preview streams or multiple preview sessions.

Media names are limited to `index.m3u8` or `segment-` plus at least six digits
and `.ts`. Playlists use `application/vnd.apple.mpegurl`; segments use `video/mp2t`.
Session tokens do not replace configured browser authentication. Once stopped,
media is removed and a new session is required.

## Errors and concurrency

| Status | Typical meaning |
|---|---|
| 400 | Bad action confirmation/UUID/unsupported selection, or untrusted Host |
| 401 | Browser authentication required/invalid |
| 403 | Missing action header or rejected Origin |
| 404 | Unknown/unavailable preview media name |
| 408 | Request body receive deadline exceeded |
| 409 | Stale favourites revision, scan/edit/preview conflict, preview disabled, or wrong stop-session token |
| 410 | Preview session stopped/expired (including heartbeat/media access) |
| 413 | Request body exceeds 32 KiB or invalid declared length |
| 415 | Backup upload Content-Type is not application/octet-stream |
| 428 | Missing favourites `base_revision` precondition |
| 422 | Request model validation failure, such as empty list/bad token shape |
| 503 | Backend connection, favourite save, scan recovery, or preview startup unavailable |

Error bodies normally use `detail` (string or validation details). Do not rely on
raw backend exception text. Exclusive operations hold a single manager lock;
preview blocks scan, refresh, and favourite saves. Recovery-pending state can
also refuse mutation. HTTP success is not physical picture/audio acceptance.


## Configuration backup API (configured appliance only)

All routes follow the configured device/browser authentication policy.
POST requests require the action header and same-origin checks. Actions below use
`POST /api/device` with `{"action":"…","args":{"id":"<32 lowercase hex>"}}`:

| Action | Behavior |
|---|---|
| `backup_create` | Schedule a quiesced snapshot; returns `working` before interruption |
| `backup_status` | Return this operation's state, or `missing` for an expired/unknown ID |
| `backup_restore` | Activate this ID's validated upload and reboot; requires `current_password` |

Poll `backup_status` after create. TV/web briefly stop; transient network errors or
503 responses are expected. `ready` means the archive is available; `error` has a
secret-free message. The bundled UI waits at most seven minutes and does not
interpret acceptance as completion.

`POST /api/device/backup/upload/{id}` accepts raw archive bytes with
`Content-Type: application/octet-stream`; other content types return 415. It authenticates before streaming,
enforces a 32 MiB limit including chunked/misdeclared bodies, a 60-second receive
deadline and one upload at a time. Successful root validation returns
`{"id":"…","state":"validated","created_at":"…","message":"…"}` without
activating settings. The user may cancel or subsequently confirm restore using
the same ID and the current web password. When login is enabled, restore uses
`{"action":"backup_restore","args":{"id":"<32 lowercase hex>"},"current_password":"<current web password>"}`.
The password is top-level, not inside `args`; omit it on login-free devices.
`backup_validate` is the fixed helper
validation action; upload is the supported browser entry point.

`GET /api/device/backup/download/{id}` returns the ready archive as an attachment,
`application/octet-stream`, with no-store caching. Downloads and validated uploads
expire after ten minutes or reboot; a new operation replaces the previous result.
Neither route accepts an arbitrary filesystem path. The ordinary request-body
limit remains 32 KiB; only the dedicated streaming upload has the larger limit.

Active scans/preview, Wi-Fi trials, pending recovery or other configuration
maintenance return 409. Invalid/oversized uploads return 409/413; receive timeout
returns 408. Restore's current-password failure returns 403. During the snapshot
all other web mutations are refused; device-helper mutations are serialized or
refused until maintenance completes. After accepted restore reconnect with the
restored address/access settings and reload the page.

### First-run device onboarding

Setup routes require the unconfigured installation state, valid Host, and
`X-Satip-Action: 1`/same-origin checks for mutations; they do not require login.

- `GET /api/setup/inputs`: friendly device list, selected USB identity and latest navigation-button test.
- `POST /api/setup/input`: `{"id": "vvvv:pppp"}` or `{"id": "ir"}` selects a currently connected control.
- `GET /api/setup/preferences`: receiver name, timezone and installed timezone choices.
- `POST /api/setup/preferences`: `{"hostname": "satip", "timezone": "Europe/Berlin"}`.
- Setup status includes `starter`, an ordered list of discovered service UUIDs recommended for the editable lineup.
- Normal `/api/device` status includes `require_auth`; login-free devices do not require password re-entry. Setting a web password enables authentication for subsequent requests.


## Channel backup API (appliance)

Uses the same authenticated `POST /api/device` action envelope and random
32-character hexadecimal IDs as configuration backup:

| Action | Behavior |
|---|---|
| `backup_channels_create` | Schedule a quiesced channel snapshot; returns `working` |
| `backup_status` | Poll either backup kind; `kind` is `channels` or `configuration` |
| `backup_channels_restore` | Restore this ID's validated channel upload; returns `working`, then `restored` after activation; requires current-password confirmation when login is enabled |

`POST /api/device/channels/upload/{id}` streams raw
`application/octet-stream` bytes, authenticates before accepting them, and uses
the same 32 MiB, deadline, concurrency, action-header and origin limits as full
backup upload. It invokes the fixed helper action `backup_channels_validate`.
Successful validation returns `state: validated`, `kind: channels`,
`created_at`, `available` and `favourites` counts. Validation is read-only
with respect to the installed channels; restore requires a separate confirmation.

`GET /api/device/channels/download/{id}` returns a ready channel archive as a
`satip-channels-….tar` attachment with `Cache-Control: no-store`. Download and
restore reject IDs belonging to the other backup kind. Only one maintenance
operation runs at a time across both kinds; uploads/results expire as in full
backup. Poll through temporary web-service disconnects, then reload channel lists
after `restored`; there is no reboot or credential/network change.

Archive format `satip-channels`, version 1, uses the bounded plain-tar manifest,
checksums, pinned backend revision and persistent schema checks. Its exact
allowlist is `channels.json` (channel metadata version and network UUID; SAT-IP
version 2 also includes `source: satip` and `satellite: 19.2E`),
`management/library.json`, `web/candidates.json`, saved
`tvheadend/channel/config/<uuid>` mappings and
`tvheadend/input/dvb/networks/<network-uuid>/muxes/<mux-uuid>` records (including
embedded services). Network configuration, adapters, access credentials, hooks,
links and unrelated files are not imported. SAT-IP multiplex records are relocated
into the destination network; network UUID equality is not required. Validate
Astra 19.2°E DVB-S/S2 reception, global identifier collisions and playback access
before activation. Legacy version-1 SAT-IP metadata is accepted when the mux
records supply matching satellite/delivery information. Blank mux orbital fields
inherit the destination network. For legacy cross-network import, require at least
one explicit matching orbital position and reject any conflicting position.
Version-2 metadata supplies the satellite even if all mux orbital fields are blank.
Enigma2 still requires
the same configured network. Existing restricted playback ACL fields remain unchanged
except a safely checked increase of its channel-number maximum if required.

First setup exposes `POST /api/setup/channels/prepare` with the same tuner
selection payload as `/api/setup/scan`, but does not start a scan. After preparing
the tuner/network, use the channel upload, restore and `backup_status` endpoints
above, without login. All other device actions and full-configuration uploads
through this setup API are refused. No existing library is required. Restore
writes destination-generated web configuration and the normal setup-completion
marker; the root supervisor independently validates it before enabling TV.

Activation clones the stopped current persistent store, overlays channel
paths and the active source’s saved library/catalogue, reconstructs permissions,
and commits with the existing durable whole-tree
rename journal. Other current settings and operational history are retained;
channel restore recovers/clears the committed journal and restarts services rather
than rebooting. Free space is checked before removing a prior rollback tree.
Helper recovery distinguishes committed channel restores from full restores.
See [channel backup](device-settings.md#channel-backup) for user-facing limits.

## Source discovery and bouquet import (appliance)

The setup app and appliance management share these endpoints. The existing
local-host, request-size, action-header and authentication rules apply; first
setup remains login-free, configured management uses its configured login policy.

| Endpoint | Purpose |
|---|---|
| `GET /api/sources` | Active/configured sources, saved receiver metadata, SAT-IP device/tuner inventory and current receiving input, per-type discovery and import progress |
| `POST /api/sources/discover` | Start bounded LAN discovery; body `{"source":"satip"}` or `{"source":"enigma2"}` |
| `POST /api/sources/bouquets` | Read-only receiver validation and TV bouquet listing; body includes `address`, optional `web_port`/`stream_port` |
| `POST /api/sources/import` | Start exclusive stream validation; receiver fields plus `bouquets` (1–16 distinct names), optional `timeout` (5–90 seconds, default 20), and required `confirm_pause:true` |
| `POST /api/sources/cancel` | Stop import, restore network/mux settings and retain the previous lineup; body `{}` |
| `POST /api/sources/recover` | Restore interrupted import journals before retrying; body `{}` |

Discovery and bouquet listing do not tune. Import rejects concurrent scans,
browser viewing, Wi-Fi trials and device maintenance. Source activation remains
the existing password-confirmed `source_select` device action. The browser
source dropdown describes the source being configured, not necessarily the one
currently playing. Standalone management does not expose these appliance APIs.
