# Operations, maintenance, and recovery

**Audience: developers and advanced administrators.**

[Documentation index](README.md) · [Troubleshooting](troubleshooting.md)

## Local user manual

Setup and normal management include a **User manual** link that opens
`/user-manual.html` in a separate browser tab. The illustrated English guide,
stylesheet and seventeen visual assets are installed under `/usr/share/doc/satip/doc`.
It works without internet access and can be printed or saved as PDF through the
browser. Public technical references are served as plain-text Markdown.
All documentation routes follow the current application's authentication and
allowed-host policy; they never expose configuration, logs or private backups.

Use the existing application/documentation deployment to update both browser
links, `satip_controller/manual.py`, and public documents together. No SD-card
image build, first-boot reset, network change or source switch is required.
The deployment briefly stops TV services and retains a private rollback archive.
See [manual maintenance](development.md#maintaining-the-user-manual) for
regeneration and screenshot procedures.

## Administration

The Buildroot appliance uses BusyBox init and root-owned service supervisors.
Connect with `ssh root@APPLIANCE_IP` using your provisioned recovery access.
Run these read-only checks on the Pi:

~~~sh
cat /data/store/installation.json
cat /run/satip-boot-stage
cat /run/satip-boot-error
tail -n 50 /run/satip-supervisor/player.log
tail -n 50 /run/satip-supervisor/player.supervisor.log
tail -n 50 /run/satip-supervisor/web.log
tail -n 50 /run/satip-supervisor/tvheadend.log
netstat -lnt
~~~

An absent error/log file can mean that no error was recorded or the corresponding
stage has not run. Web normally listens on port 8080; backend ports 9981/9982
listen only on loopback. BusyBox `netstat` on this image has no `-p` option.
**A running supervisor is not HTTP or playback readiness.** Check the browser
Status page and actual picture/audio separately.

## Paths and permissions

Persistent directories below are under `/data/store`. Conventional `/etc` and
`/var/lib` paths are directory symlinks to them, allowing atomic file replacement
on the data filesystem. Modes below describe fresh provisioning and normal writes.

| Path | Owner:group / mode | Purpose |
|---|---|---|
| `/opt/satip` | root-owned, not service-writable | Application/appliance modules and static assets |
| `/usr/share/doc/satip` | root-owned; directories 0755, files 0644 | README, guides and licence notices |
| `/etc/satip-player` → `config/player` | satip-player:satip-player / 0700 | Player config and credentials; files 0600 |
| `/etc/satip-web` → `config/web` | satip-web:satip-web / 0700 | Web config and credentials; files 0600 |
| `/var/lib/satip-player` → `player` | satip-player:satip-management / 2750 | Player state; normal state/identity writes 0600 |
| `/var/lib/satip-management` → `management` | satip-web:satip-management / 2750 | Shared library/settings, normally 0640 |
| `/var/lib/satip-web` → `web` | satip-web:satip-web / 0700 | Private catalogue, rollback journal, snapshots and lock |
| `/data/store/tvheadend` | tvheadend:tvheadend / 0700 | Backend database, accounts and saved EPG |
| `/data/store/identity`, `network`, `operations` | root:root / 0700 | Device settings, network secrets and recovery records |
| `/run/satip-status` | satip-player:satip-management / 2750 | Web-readable player status/takeover, normally 0640 |
| `/run/satip-management` | satip-web:satip-management / 2750 | Leases and EPG cache, normally 0640 |
| `/run/satip-player` | satip-player:satip-player / 0700 | mpv socket, auth include and player runtime |
| `/run/satip-web` | satip-web:satip-web / 0700 | Private generated HLS |
| `/run/satip-supervisor` | root:root / 0700 | Owned-generation records, control files and logs |
| `/run/satip-tuning` | root:root / 0755 | Per-load failure feedback, normally 0644; no credentials |
| `/run/satip-network` | root:root / 0755 | DHCP/Wi-Fi diagnostics (0644); private first-scan supplicant copy (0600) |

Data preparation creates the runtime directories. Setgid directories preserve
the management group on atomic replacement. Player supplementary groups are
`video render audio satip-input satip-management`; web has `satip-management`
besides its own group. The selected-input rule grants access only to the intended
keyboard and GPIO receiver. See [persistent contracts](buildroot-services-persistence.md#persistent-layout-and-contracts).

Credential files are separate:

- `/etc/satip-player/credentials.json`: restricted backend playback.
- `/etc/satip-web/tvheadend-admin.json`: backend administration.
- `/etc/satip-web/login.json`: browser login.
- `/etc/satip-web/preview-credentials.json`: restricted playback copy for HLS.

Supervisors set `PYTHONPATH=/opt/satip` and `SATIP_RUNTIME_ROOT=/run`.
Ad hoc diagnostics need these too; otherwise source-checkout tools may look for
runtime files at configured persistent paths.

## Service behaviour

`/etc/init.d/S90satip-services` starts backend, web and player guardians in
that order and stops them in reverse order. Each root guardian owns one root
supervisor and adopts its descendants after a crash. The independent hardware
watchdog monitors enabled guardians; see [recovery](recovery.md#root-supervisor-guardians). Each validates the data gate.
Supervisors manage exact owned process groups, drop UID/GID and supplementary
privileges, set no-new-privileges, and reap children before restart. The player
supervisor activates tty1 before dropping to the player account. tty2/UART remain
console recovery paths. The root device helper is independently respawned by
BusyBox init; the watchdog has its own init script.

To restart only the player (interrupts HDMI):

~~~sh
PYTHONPATH=/opt/satip python3 -m satip_appliance.supervisor player stop
PYTHONPATH=/opt/satip python3 -m satip_appliance.supervisor player start
~~~

The same explicit `stop`/`start` actions exist for `web` and `tvheadend`.
The supervisor CLI has no `status` or `restart` action. To stop/start the three
TV services together:

~~~sh
/etc/init.d/S90satip-services stop
/etc/init.d/S90satip-services start
~~~

Read [service lifecycle](buildroot-services-persistence.md) and
[automatic recovery](recovery.md) for cleanup deadlines and failure handling.
Never remove generation, lease or rollback markers to bypass a refusal.

## Updates

For an in-place development update:

1. Review the local changes and run the affected [checks](development.md#automated-tests).
2. Record the exact files to install and back up their current device versions.
3. Finish scans, browser preview, Wi-Fi trials and pending recovery before changing
   runtime code. Stop affected services through their supervisors; a shared
   player/web contract change requires stopping both.
4. Copy matching application modules/static assets to `/opt/satip` and any changed
   launchers to their corresponding image paths. Use root ownership, 0644 for
   source/assets and 0755 for executable launchers. Update the same inputs in
   the local repository for the next image build.
5. Compare SHA-256 hashes. Compile the installed application with the device
   Python, then make its generated caches service-readable:

   ~~~sh
   python3 -m compileall -q --invalidation-mode checked-hash /opt/satip
   find /opt/satip -type d -name __pycache__ -exec chmod 0755 {} \;
   find /opt/satip -type f -name '*.pyc' -exec chmod 0644 {} \;
   ~~~

   Run the player check below, then start affected services and verify HTTP,
   playback and saved data.

Helper/supervisor/init changes need coordinated activation; restarting web alone
does not reload a root helper. The [application deployment tool](#in-place-development-deployment)
installs the complete current application, public documentation and its declared
launcher manifest with coordinated service/helper handling. It stops
and reloads the watchdog feeder after service cleanup, then starts the feeder
before the new guardians. This briefly suspends hardware-watchdog protection
while the development update is being installed. Configuration,
credentials, library and state remain on-device. Do not rerun initial
provisioning or copy reference examples over them.

Read-only checks as the player account, using BusyBox `su`:

~~~sh
su -s /bin/sh -c 'PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/opt/satip SATIP_RUNTIME_ROOT=/run python3 -m satip_controller --check' satip-player
su -s /bin/sh -c 'PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/opt/satip SATIP_RUNTIME_ROOT=/run python3 -m satip_controller.diagnostics' satip-player
~~~

These validate files and enumerate selected devices without starting another
player or grabbing input. They do not prove physical output.

### Web-only performance updates

The Performance panel is implemented by the controller's performance.py and
telemetry.py plus static/index.html, static/app.js and static/app.css. Matching
source and documentation must also remain in the local repository for the next
image build. It needs no extra image packages, helper or player changes.

For a compatible web-only update, back up precisely these installed paths,
compare the current device versions before copying, finish preview/scans and
pending maintenance, and stop/start only the web service through its supervisor.
Install source/assets with root ownership and mode 0644, compile using the
receiver Python with service-readable caches, compare SHA-256 hashes, and check
authenticated /api/telemetry plus the visible Status page. Do not replace unrelated
local/device work with a full application deployment merely for this panel.
Keep the private rollback archive and restart web with the previous files if
activation fails. Restarting web does not intentionally stop HDMI playback, but
browser sessions need to reconnect. Confirm physical picture/audio separately.

#### On-demand performance window

Status has an **Open performance monitor** button. Graphs and the process
table are rendered in a native modal dialog only after it is opened. Close or
Escape dismisses it and returns keyboard focus; navigation away from Status also
closes it. The existing five-second Status telemetry still collects bounded
five-minute browser history while the window is closed; no extra periodic
requests or backend changes are needed.

This window change is static-only: install matching `static/index.html`,
`static/app.js` and `static/app.css`, plus the updated public documentation.
Back up the affected paths and compare hashes before and after deployment.
No web/player/helper restart, Python compilation or image rebuild is required;
reload the browser to activate it. Do not use the full application deployment
tool for this compatible UI-only update. Check initially hidden graphs, opening,
Close/Escape, focus restoration, live refresh, reopening, mobile scrolling and
continued playback status. Physical HDMI picture/audio still needs acceptance.

Documentation-only updates go under `/usr/share/doc/satip` with repository-relative
paths and public directory/file modes from the table above. They need no service
restart. Compatible static-only UI updates need a browser reload.
The image build installs documentation using
[install-documentation.py](../buildroot/external/board/satip/install-documentation.py).
There is no data-preserving firmware updater; whole-image reflashing replaces data.

## Backups

For supported receiver settings, use [Settings → Configuration backup](device-settings.md#configuration-backup).
This creates a versioned configuration archive while the helper serializes device
changes and stops the three TV writers. The web page reconnects to download it.
Root network diagnostics may continue, but are excluded; confirmed network
preferences remain unchanged. Do not run concurrent manual configuration tools.

Restore validates before interruption, retains the previous canonical store at
`/data/.satip-restore-before`, stages at `/data/.satip-restore-stage`, and records
activation in `/data/.satip-restore.json`. Early provisioning recovers this fixed-path
journal before preparing data. These are private secret-bearing paths, not general
input directories. The previous store is retained until the next validated restore
needs a rollback slot; current operations/update history is preserved in the active
store but not exported. Restore needs free space for staging plus a copy of that
history; insufficient space leaves the previous installation active.

Do not remove journal/generation markers to bypass an error. If restore cannot
complete, use SSH/console/card recovery with services quiesced and review the
journal plus active/staged/previous trees. Never mix individual credential/backend
files from different installations. No automatic firmware or backend-version
migration is implied. Temporary export/upload files under `/run/satip-backup`
expire after ten minutes or reboot; persistent rollback data does not expire.

Configuration archives do not back up firmware, boot-card customizations or media.
For development updates or whole-card recovery, a separate private backup should cover:

- The exact application, launcher, init/udev and boot files affected by an update.
- Persistent configuration, credentials, library, player state, web catalogue and
  rollback journal, backend database/EPG, and root identity/network settings.
- Provisioning inputs on FAT, if still present, and the build/update identity.

Back up the canonical data trees rather than copying only their compatibility
symlinks. Keep archives outside the trees being archived. `/run` and USB time-shift
buffers are disposable and excluded. Protect archives containing credentials.

Stop all relevant writers or use an offline copy for a consistent data backup.
Stopping `S90satip-services` stops TV writers, but the init-respawned device helper
and network processes can still change settings. For a complete card backup,
shut down through Settings, wait for shutdown, and read the card on another
computer. See [import/backup contracts](buildroot-services-persistence.md#first-import-and-backup-boundary).
API snapshots are not full database or card backups; they retain at most ten
files/32 MiB, excluding rollback journals and credentials from pruning.

## Rollback

Restore the reviewed backup for the particular update after stopping its affected
writers. Restore matching modules, assets and launchers together, check schema
compatibility, then start services and verify data and playback. Compare later
user changes before restoring configuration. Old per-experiment archives are not
general rollback points for a newer installation.

For pending scan recovery use web startup or **Restore TV playback**, retaining
its journal. Favourite-save changes may need separate review because saving is
not a full backend transaction. Firmware edits must preserve unrelated settings;
`cmdline.txt` stays on one line.

If the image cannot boot, use the preserved working card or
[UART/card recovery](../buildroot/README.md#first-boot-and-recovery). There is no
A/B rollback. Recovery uses SSH, tty2 or UART; the image has no desktop to restore.

## In-place development deployment

Use [scripts/deploy_appliance.py](../scripts/deploy_appliance.py) from the reviewed
checkout with passwordless root SSH and normal host-key verification. An explicit
receiver IPv4 address is required. Start with the default dry run to inspect the
deployment manifest:

~~~sh
.venv/bin/python scripts/deploy_appliance.py --host <receiver-ipv4>
~~~

After reviewing the manifest and arranging interrupted viewing, apply it:

~~~sh
.venv/bin/python scripts/deploy_appliance.py --host <receiver-ipv4> --apply
~~~

The tool includes all current application Python modules and static assets, its
declared launcher list, and public documentation. It requires completed setup
and refuses active scans, preview, pending recovery or a Wi-Fi trial. It creates
a private device-side rollback archive under the operations directory and
verifies staged hashes and syntax with the target Python before interruption.

Applying an update stops all TV services and pauses the init-managed device
helper through a temporary readiness gate. It installs matching files, verifies
hashes, compiles target bytecode and runs the player configuration check before
restoring the gate and services. Installation failure triggers rollback. Inspect
the reported outcome and verify HTTP access, playback and saved data after the
transaction; retain the private backup for recovery.

This updates application code and the declared launchers, not the full firmware.
It does not install binary dependencies, change boot configuration or restart
networking. Changed network startup code takes effect on a later network start
or reboot. Native splash, media-stack or ext4-tool changes require a compatible
image/dependency update; see [time-shift dependencies](timeshift-internals.md#dependencies-and-validation).
Full fresh-image and disruptive-access qualification remain separate checks.

The older `scripts/deploy_device_settings.py` entry point delegates to this tool
for compatibility and has the same deployment scope.

### First-boot development deployment

`deploy_appliance.py` accepts `--splash-binary PATH` for the matching AArch64
renderer and `--mdns-dependencies PATH` for a pure-Python pip target containing
zeroconf 0.148.0 and ifaddr 0.2.0 (including their licence metadata). These files
are backed up and hash-verified alongside the application. Stage dependencies
with `SKIP_CYTHON=1 python3 -m pip install --no-compile --no-binary zeroconf
--target PATH zeroconf==0.148.0 ifaddr==0.2.0`. The native renderer recipe is
`buildroot/external/package/satip-splash/satip-splash.mk`; its three sources are
`splash.c`, `render.c`, and `qrcodegen.c`. A target-compatible compiler and
libdrm/FreeType development files are required. This does not rebuild an image.

For a repeatable fresh TV-configuration rehearsal on the development Pi:

~~~sh
ssh root@APPLIANCE_IP \
  "PYTHONPATH=/opt/satip SATIP_RUNTIME_ROOT=/run python3 -m satip_appliance.setup_rehearsal start"
# Open the HDMI setup URL, choose controls and complete a scan/lineup.
ssh root@APPLIANCE_IP \
  "PYTHONPATH=/opt/satip SATIP_RUNTIME_ROOT=/run python3 -m satip_appliance.setup_rehearsal restore"
~~~

This stops TV, saves the original store and selected slot files privately under
`/data/.satip-setup-rehearsal`, creates a fresh TV backend and restarts the welcome
screen. Network/SSH identity is retained so the remote session remains usable.
`status` reports whether a saved original exists. Interrupted rehearsals with an
`original` directory can be restored with the same command. Trial stores remain
available for diagnosis. This developer tool does not qualify factory-empty image
assembly or actual no-network Wi-Fi bootstrap; use the acceptance procedure in
[first-boot validation](first-boot-validation.md).
