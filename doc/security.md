# Security and trust boundaries

**Audience: developers and advanced administrators.**

[Documentation index](README.md) · [Configuration](configuration.md)

## Network exposure

The appliance serves its browser interface on LAN port 8080. First setup is
password-free. New installations default to login-free home-LAN management,
including device controls and backups. A reachable LAN client can configure the
receiver, change favourites, start scans and control playback. Setting a web
password enables authentication for subsequent requests. Existing authenticated
installations retain their policy.

Use a trusted LAN. The application serves HTTP, not TLS; HTTP Basic encodes
credentials without encrypting them. The bundled Dropbear disables SSH forwarding.
Do not forward the browser or backend ports to the internet.

The appliance validates Host against its current local IPv4 addresses and hostname
on each request. Same-origin checks when Origin is supplied and the required
`X-Satip-Action: 1` header protect browser actions, but do not replace login.
There is no per-user management role system. CSP, no-store, frame denial and
MIME-sniffing headers provide additional browser protections.

Tvheadend HTTP/HTSP stay loopback-only. The web account's backend administrator
rights allow mapping, mux and ACL-range changes; the browser never receives
those credentials. Root passwords are hashed on-device; browser/Wi-Fi secrets
remain protected data. Optional FAT preferences are cleaned up after successful
import; ordinary flash-file deletion is not secure erasure.

## Credential separation

- Player reads restricted playback credentials only.
- Web reads backend-admin credentials, separate browser login, and a separate
  copy of restricted playback credentials for preview.
- Web cannot read the player's private credential file or saved state, and
  player cannot read backend-admin credentials.
- Shared library/status/lease/EPG files contain no passwords.

Use the [credential schema](configuration.md#credentials) and
[permissions](operations.md#paths-and-permissions). Changing a backend password
requires updating its protected copies consistently; browser login is independent.
Do not print credentials in terminal transcripts, chat, screenshots, or issue reports.

mpv loads an Authorization header from private `auth.conf`. Its arguments contain
only the include path, not the header. Child output is suppressed because verbose
option logging can reveal it. FFmpeg receives an authenticated stream through a
Python-owned pipe, not a password-bearing URL. Avoid raw IPC/config/request dumps
and environment/proxy changes that bypass these boundaries.

## Appliance device controls

Configured Buildroot `/api/device` follows the receiver's browser-login policy. See [Device settings](device-settings.md#passwords--access)
for password and action-confirmation requirements. The web service uses a UID-checked
Unix socket to a fixed-action root helper; it receives no arbitrary command or
path interface. Persistence and recovery-seed handling are documented in
[Configuration](configuration.md#device-preferences-and-helper).

USB formatting replaces the entire selected disk's partition table. It requires
a boot/hotplug-specific USB identity and an explicit device-specific confirmation
dialog, without typed disk-name confirmation or password re-entry. Mounted, swap,
system/SD, and device-mapper-backed disks are refused.
Mounts are root-owned with `nosuid,nodev,noexec`; the buffer is player-private.
The player independently checks the actual USB mount, never falling back to SD
storage. Do not run concurrent root disk-management tools or expose these controls
over untrusted HTTP. See [time shifting](timeshift.md) for data-loss and acceptance limits.

## Configuration backups

Backup export, upload, download and restore follow the receiver's browser-login
policy. On authenticated installations restore additionally requires the current
web password. The browser overwrite/reboot confirmation applies in either mode. Action/origin checks
apply to uploads and actions. Download uses a fixed, expiring file and operation ID,
not an arbitrary path supplied by a browser.

Backups are unencrypted, secret-bearing archives. Their checksums detect damage,
not authenticity: restore only trusted files. The root helper validates the
version/schema/backend, required records, account relationships, configuration
paths and a fixed playback-configuration file allowlist. Enigma2
IPTV records are restricted to the saved private receiver address/stream port
and validated DVB service references; arbitrary URL/pipe inputs, retransmission
URLs, custom headers/environments and automatic playlists are rejected. Archive ownership/modes are ignored;
symlinks, hard links, devices, compressed outer archives, unknown paths and
external backend hooks are refused. Packed backend settings have separate
size, field-count and nesting bounds.

Upload authenticates before streaming to an exclusive private file, with a
32 MiB byte limit, 60-second receive deadline and one upload at a time per web
process. Root validation/staging and rollback data are private. The temporary
root-owned download is readable by the web account only for explicit export;
this intentionally exposes the complete backup through the device-control route,
not through the static asset tree. Temporary downloads/validated uploads expire
after ten minutes or disappear on reboot. A privileged local operator is still
trusted and must not edit configuration concurrently with maintenance.

## Files, devices, and privileges

Root owns application source and service launchers. Fresh provisioning creates
service-owned 0600 configuration/credential files in private data directories.
Root independently validates generated settings before activation. Service accounts
write their state/runtime trees; shared files grant the other service only the
access needed for library/status/lease/EPG exchange. Private runtime directories
contain the mpv control socket, auth include and generated HLS; they are not
public static web roots.

The player can read GPIO infrared, USB keyboard event nodes and a configured
additional USB remote. Other input device types retain their normal permissions.
Infrared and USB keyboards remain enabled together; keyboard detection checks
the USB bus and text-key capabilities. Web has no player device-group permissions.
Root supervisors activate the player VT, set supplementary groups and UID/GID,
then launch fixed commands with no-new-privileges. The writable root filesystem
and Unix permissions are not a read-only mount sandbox or private device namespace.
See [actual paths and permissions](operations.md#paths-and-permissions).

The appliance does not harden the whole OS: SSH, Wi-Fi, other local services,
physical console access, and firewall policy remain administrator responsibilities.

## Publishing or reporting problems

Never publish credentials, Tvheadend password records, private backups, transport
captures, browser profiles, or generated preview media. Backend UUIDs/LAN
addresses are not passwords, but machine identifiers, fingerprints, local account
names, and backup paths should be reviewed for privacy.

Test fixtures contain fake passwords, not real credentials; distinguish fixtures
from actual secrets. `.gitignore` is not a comprehensive secret policy: inspect
candidate files before publishing. An ignore rule does not remove a tracked secret.
See [publication and licensing](development.md#publication-and-licensing).

## Source discovery and OpenWebif access

Appliance source APIs inherit the existing authentication, local-host and action
header rules; they are not enabled in standalone management. Enigma2 discovery
probes only directly attached private IPv4 subnets of /24 or smaller, at most
254 hosts with bounded concurrency, and returns at most 16 receivers. Manual
entry accepts private IPv4 addresses and numeric ports, not arbitrary URLs.
OpenWebif reads reject redirects, proxy environment use, XML entities/DTDs,
oversized responses and unbounded downloads. Remote names are rendered as text.

Only read-only about/services/EPG APIs and validated raw DVB stream references
are used; no zap, standby, credentials, custom headers, shell/pipe inputs or
source settings mutation is exposed. Bouquets are not proof of free-to-air
status: every imported stream still passes the existing metadata policy, with
the decryption-metadata caveat described in [Enigma2](enigma2.md).
