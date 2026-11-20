# Buildroot services and persistent data

**Audience: developers and advanced administrators.**

[Documentation index](README.md) · [Buildroot image](../buildroot/README.md)

This describes the current development profile. Physical and imported-database
acceptance limits are in [validation](validation.md).

## Packaging and lifecycle

`buildroot/external/package/satip-app/` installs immutable application/appliance
Python modules and all static assets under `/opt/satip`. Buildroot selects evdev,
FastAPI, uvicorn, httpx and transitive dependencies; no target pip. Both remote
staging and native package rsync use allowlists, excluding deployment examples,
credentials, backups and caches. The package generates checked-hash bytecode with
the matching Buildroot host Python and installs service-readable, root-owned
cache directories/files (0755/0644). The build helper refreshes only the local
application and native splash packages to avoid stale Python/assets/renderer code.
The project's original code and documentation use the MIT licence; bundled
components retain their own licences. See
[licensing](development.md#publication-and-licensing).

Fixed IDs: input 420, player 421, web 422, management 423, backend 424. Player
supplementary groups: `video`, `render`, `audio`, `satip-input`,
`satip-management`; web: `satip-management` besides its own group.
Backend has its own account. Validate actual device permissions on the intended Pi.

BusyBox startup: `S25satip-data` mounts/prepares data; `S30satip-dev-provision`
handles limited recovery provisioning; `S39satip-device` restores interrupted
Wi-Fi trials before networking; network/SSH start asynchronously;
`S90satip-services` starts supervisors only after successful data preparation.
Each consumer independently validates the gate and actual data device. Missing,
uninitialized or imported-unqualified data starts **no TV services**. A freshly
bootstrapped `unconfigured` store permits the backend and password-free setup web
service, but not the player. Playback starts only after fresh configuration is
validated or an imported store is separately qualified. Local TCP readiness uses
bounded probes/retries, not network-online/NTP waits. For configured playback,
the backend first waits for a route to its saved SAT-IP address; the player
supervisor also checks the server description port and authenticated read-only
hardware API for an active matching server and enabled DVB-S frontend before
releasing the splash. Fresh setup does not require that external backend route.
These are startup gates, separate from continuous local service health checks.
See [automatic recovery](recovery.md) and [boot timing](boot-process.md).

`satip_appliance/supervisor.py` uses Linux child-subreaper support and an owned
process group per generation. It sets supplementary groups, UID/GID, umask 0077
and no-new-privileges before exec of fixed binaries/root-owned Python code.
The player activates tty1, acquires its controlling terminal/foreground group,
then drops privileges. tty1 has no getty; tty2/UART/SSH remain recovery paths.
Actual non-root VT/DRM/audio/input behavior needs the Pi.

Service exit/crash/stop triggers exact group TERM, bounded grace (40 s web,
10 s others), KILL escalation and orphan reaping **before** restart. Backoff is
1–30 s, reset after a stable minute. PID start identity and Linux pidfds protect
stop against PID reuse. Restart waits for stop completion; no broad `killall`.
An unclean supervisor kill leaves a generation marker and refuses duplicate
restart: inspect its recorded group with recovery access or reboot the test card.
Frozen service generations are detected by controller progress or local HTTP
checks and recovered through the same group cleanup. The independent hardware
watchdog protects against loss of its feeder, not isolated root-supervisor
freezes/exits. See [automatic recovery and limits](recovery.md). Deliberately
escaping a process group/session is unsupported; existing mpv/FFmpeg launches do
not do so.
Per-generation RAM logs are capped at 1 MiB.

Upstream `S99tvheadend` and unrestricted `-C` bootstrap are removed. Replacement:
foreground `-c /data/store/tvheadend --bindaddr 127.0.0.1 --nosyslog`. Both HTTP
and HTSP use that bind address. An external make override retains the SAT-IP
client and disables server compilation. Source review found `--satip_rtsp 0`
means web configuration, **not disabled**, and is absent when compiled out; the
option is deliberately omitted. The offline validator requires CLIENT=1/SERVER=0.

## Service operations

Run as root on the Buildroot appliance. Read logs/state before changing services:

~~~sh
cat /data/store/installation.json
cat /run/satip-boot-error
cat /run/satip-boot-stage
tail -n 80 /run/satip-supervisor/player.log
tail -n 80 /run/satip-supervisor/player.supervisor.log
~~~

A supervisor running is not stream or HTTP readiness. Check the web Status page
and actual picture/audio separately. Missing log files can mean the corresponding
startup stage has not run.

After arranging an interruption and waiting for scans, preview and pending
recovery to finish, stop/start **owned** services through their supervisors:

~~~sh
# Example: restart only the player; this interrupts HDMI.
PYTHONPATH=/opt/satip python3 -m satip_appliance.supervisor player stop
PYTHONPATH=/opt/satip python3 -m satip_appliance.supervisor player start

# Stop the TV services before a reviewed update or TV-data backup.
/etc/init.d/S90satip-services stop
# Start again only after validating the mounted data/configuration.
/etc/init.d/S90satip-services start
~~~

Do not run raw mpv, another web service, or the supervisor without its start/stop
action alongside the active instance. Never clear generation, lease or recovery
markers to force a restart. Supervisor refusal requires inspection or supervised
card/reboot recovery, not broad process killing. Keep matching source, static assets,
launchers and runtime contracts together for code updates; preserve data and secrets.
The independent device helper and network processes remain active when these
three services stop; this alone does not quiesce every persistent-data writer.
See [updates and backups](operations.md) for the complete maintenance scope.
There is no automatic firmware updater. Whole-image reflashing replaces data.

## Persistent layout and contracts

Current development layout: 128 MiB FAT, 768 MiB writable root, 256 MiB ext4
data labelled `satip-data`; direct firmware boot, no A/B, expansion or SWUpdate.
A whole installation-image flash replaces data: never use it as a preservation update.

`/data/store` is the atomic first-import unit. Canonical `/data/config`,
`/data/player`, `/data/management`, `/data/web`, `/data/tvheadend`, network,
identity, input, update and operations are directory aliases into it. Conventional
`/etc/satip-player`, `/etc/satip-web`, `/var/lib/satip-player`,
`/var/lib/satip-management`, `/var/lib/satip-web`, `/etc/dropbear` use directory
links, **not JSON file symlinks**, so atomic replacement stays on data. Conflicting
slot paths are refused, not overwritten. Shared trees use setgid management;
credentials remain account-private and network/identity/update are root-only.

Preparation requires an actual writable ext4 mount matching the labelled block
device major/minor identity, separate from root, free space and an fsynced write
probe. No format, resize, guessed partition number or ordinary root `/data`
fallback exists. RAM readiness is written last; BusyBox continuing after a failed
init script cannot bypass it. Supervisors recheck mount/free space and stop their
generations on failure. Physical missing/read-only/full-media tests remain open.

Schema contract: version 1, reader_min=1, writer=1, rollback_min=1. Unknown
contracts are rejected without writes. Import stages all trees on the same
filesystem, fsyncs files/directories and atomically renames the complete store.
Interrupted `.import-stage` is retained; retries refuse it. Stop consumers, inspect
and privately archive that exact tree before deciding to remove/retry. If `store`
exists, never import over it: prepare it instead. Future schema writers require
explicit transactional migration, old-reader and rollback qualification. There is
no version-2 migration or firmware rollback restoration yet.

The appliance supervisors enable `SATIP_RUNTIME_ROOT=/run`. Without this environment,
ad hoc source-checkout tools use the configured persistent paths.
Group-readable status: `/run/satip-status`; private player sockets/auth:
`/run/satip-player`; HLS: `/run/satip-web`; EPG JSON and leases:
`/run/satip-management`. Leases use boot identity/monotonic expiry and reject
old-boot/wall-clock leases. Mux rollback remains private/durable;
`management/recovery.json` publishes the durable pause obligation, reconstructed
before consumers start. API snapshots retain at most 10 files/32 MiB total,
excluding recovery journals/credentials. Tvheadend's EPG database is durable, as
described below; its internal upgrade-backup RAM/retention policy remains
unimplemented/unqualified.

### EPG persistence

The backend supervisor sets `epgdb_periodicsave=1` before each backend generation
starts, preserving other grabber settings. Fresh configurations retain upstream's
default passive EIT/PSIP modules. Tvheadend then snapshots its in-memory guide
**every hour** to `/data/store/tvheadend/epgdb.v3` and reloads it at startup.
One hour is the shortest native interval in the pinned backend; periodic saving
is otherwise disabled by default. A clean service shutdown also saves the guide.
No extra tuner subscription or dedicated EPG scan is added.

Tvheadend writes a temporary file and renames it over the database. The supervisor
checks for completed database changes every two seconds and fsyncs the file and
its containing directory (also after clean shutdown), because the pinned backend's
writer does not fsync them itself. It does not flush the in-progress `.tmp` file
or rewrite unchanged snapshots. A failed flush revokes data readiness.

An abrupt power loss can still lose new/changed events since the last completed,
flushed snapshot (up to roughly an hour). Existing saved, unexpired schedules
survive; expired events are not an archive, and unvisited muxes still require
passive collection. Correct wall-clock time remains necessary. Prefer a clean
shutdown: software persistence cannot guarantee SD-card survival when power is
removed during a write. The application `/run/satip-management/epg.json` remains
a disposable cache rebuilt from Tvheadend, not the durable source.

At web-service startup, the application warms this RAM cache in batches of up to
four favourites, plus the requested channel if different. Requests are concurrent
and read-only; each completed batch is published without the normal two-second
polling delay. After the first sweep it resumes one favourite every two seconds
plus the requested channel. Individual failed replies retain their previous data
and do not block other channels. A wall-clock correction exceeding 30 seconds
relative to monotonic elapsed time triggers another warm-up, publishing fresh
timestamps even when programme text is unchanged. This handles a Pi starting at
1970 before Chrony synchronizes; it does not bypass the need for correct time.

Check configuration and logs without modifying a live backend database:

~~~sh
grep epgdb_periodicsave /data/store/tvheadend/epggrab/config
grep 'epgdb:' /run/satip-supervisor/tvheadend.log | tail -n 20
grep 'EPG database' /run/satip-supervisor/tvheadend.supervisor.log | tail -n 10
ls -l /data/store/tvheadend/epgdb.v3
~~~

To qualify persistence, observe a full hourly save cycle and check that a clean
backend restart reloads the guide before tuning. Physical power-loss behavior
requires a separate test on a disposable card; a successful reload after clean
shutdown does not establish power-loss durability.

### Identity and provisioning

Hostname, authorized keys and Wi-Fi inputs regenerate into the slot. Limited
development provisioning saves allowlisted input in protected identity storage
after explicit data initialization; imported hostname takes precedence over FAT.
Failed/uninitialized-data SSH recovery uses ephemeral RAM host keys: expect a
different fingerprint; do not disable host-key verification globally. Fresh
timezone/input preferences are regenerated by provisioning. Imported
timezone/device/IR activation and OpenSSH-to-Dropbear identity conversion remain
unqualified.

## First import and backup boundary

The import command accepts the legacy snapshot layout below; it is not the
current appliance backup layout. Current backups use the canonical data trees
described in [operations](operations.md#backups). Import requires private
**quiesced** snapshot trees, relative to snapshot root:

| Source | Destination in store |
| --- | --- |
| `etc/satip-player` | `config/player` |
| `etc/satip-web` | `config/web` |
| `var/lib/satip-player` | `player` |
| `var/lib/satip-management` | `management` |
| `var/lib/satip-web` | `web` |
| `var/lib/tvheadend/.hts/tvheadend` | `tvheadend` |

Optional explicit inputs: hostname, WPA supplicant file, Dropbear keys,
authorized_keys, rc_keymaps. No whole `/etc`, shadow database, devices, sockets,
symlinks or example library is imported. Export ordinary files rather than
links; unrelated snapshot symlinks are also refused. Recording trees/hooks require
separate review and currently cause import refusal. Preserve original UUIDs,
library, credentials and state; remove known application runtime files. This does
not invent backend UUIDs, normalize arbitrary config paths or initialize a new store.

On a separate Buildroot migration card, with services stopped and empty mounted
data, use `PYTHONPATH=/opt/satip python3 -m satip_appliance.persistent import
--source /private-snapshot --backend-revision ACTUAL_COMMIT --quiesced` (one line).
Alternatively, `PYTHONPATH=/opt/satip python3 -m satip_appliance.persistent
initialize` creates an empty **unconfigured** store. These are mutually exclusive
one-time operations. For a fresh empty store, prefer explicit FAT initialization
and the [first-boot UI](buildroot-first-boot-setup.md); that flow does not authorize
an imported database.

Do not set installation state to `qualified` merely to start services. First
validate protected credentials, expected config paths, application schema/library,
complete backend ACL restrictions, UUID mappings and devices, and qualify the
selected backend against a **copy** of the quiesced database. Keep the pristine
private snapshot before any backend writer runs. The selected backend commit is
`37453bc3fe5f9e10f3428ebb1abdc613f8b07186`, different from the Debian reference
revision `5fd594910`. Database compatibility is unqualified, separately from
application JSON compatibility.

The allowlist plus explicit identity/network/input inputs defines the
backup boundary. Stop all application/backend writers before
copying; exclude RAM and update payload staging; protect secret backups. The
configured appliance provides a bounded, quiesced [web configuration backup
and restore](device-settings.md#configuration-backup), separate from this legacy
import format. Its fixed-path activation journal is recovered before data
preparation; see [the configuration contract](configuration.md#configuration-backup-contract).

## Supervisor ownership backstop

S90 starts a root guardian per service, with the existing supervisor as its
direct child. Guardians are subreapers: a crashed supervisor's remaining media
processes are adopted and cleaned before replacement. Enabled guardians publish
root-owned RAM progress watched by the independent hardware-watchdog feeder.
Never remove generation or guardian expectation files to bypass a refusal.
The existing service-account and persistent-data contracts are unchanged.
See [recovery](recovery.md#root-supervisor-guardians) for deadlines and escalation.
