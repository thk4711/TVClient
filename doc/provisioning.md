# Provisioning and setup internals

**Audience: developers and advanced administrators.**

[Documentation index](README.md) · [First boot](buildroot-first-boot-setup.md) · [Configuration](configuration.md)

This is a contributor reference for boot-file validation, generated credentials
and the setup state machine. For everyday changes, use [Device settings](device-settings.md).
Edit only the flashed card, never the public template or image build inputs.

## Data initialization and migration

`S25satip-data` validates the actual labelled writable data mount before the
root provisioning module may initialize it. Published images contain a versioned
`.satip-factory-empty` marker. Their known-empty data initializes automatically,
with an owned `.fresh-stage` that can resume after interruption. Unmarked empty
partitions are not initialized; the old `initialize_data` option is no longer
supported. Existing stores, foreign files, incompatible schemas and interrupted
`.import-stage` transactions are not overwritten, formatted or automatically
deleted. [Snapshot-import rules](buildroot-services-persistence.md#first-import-and-backup-boundary)
still apply when migrating a private installation.

A fresh store is `unconfigured`. Imported data remains `imported-unqualified`
until separately qualified; the fresh setup path cannot authorize it.

## Allowlisted preferences

First setup requires no browser password. New installations default to login-free
home-LAN management. Users can enable browser login later by setting a password
in Settings; root credentials are independent of browser access.

The public boot file supports exactly the following seven parameters:

| Fields | Policy |
| --- | --- |
| `hostname` | Lower-case DNS label; default `satip-dev` |
| `timezone` | Installed zoneinfo name, default `Etc/UTC`; traversal refused |
| `ssh_enabled` | `yes` or `no`; SSH defaults off on a new card |
| `root_password` | Plaintext root password for SSH and console; hashed internally, no password-length rule; does not itself enable SSH |
| `wifi_country`, `wifi_ssid`, `wifi_psk` | All three together for WPA/WPA2 Personal; Ethernet uses asynchronous DHCP |

`parse_boot_config` and `read_config` enforce this public allowlist. The file
remains literal `key=value`, not shell: unknown/obsolete or duplicate keys,
oversized files and unsupported values are rejected before provisioning writes.
Diagnostics report only fixed reason codes and line numbers, never values.
Fields are printable ASCII except UTF-8 Wi-Fi network names. Wi-Fi SSID/password
spaces are preserved; other values are trimmed. Inline comments, shell expansion
and quoting do not apply.

Removed boot options are `initialize_data`, `setup_password`, `lan_access`,
`drm_connector`, `drm_mode`, `audio_device`, `keyboard_vendor`,
`keyboard_product`, `ir_profile`, `satip_xml`, `satellite`, `satip_source`,
`tuner_number`, `ssh_authorized_key` and `recovery_password_hash`.
Remove these entries from old card files before using them with a new image.

HDMI uses internal defaults and follows the connected Pi HDMI port, adjusting
the associated standard HDMI audio route. Playback mode fallback is limited to
supported 1080p refresh rates, not arbitrary resolutions/audio devices. Standard
USB keyboards and supported GPIO infrared work together automatically; an
additional USB remote can be selected in the browser. Select/discover the SAT-IP
server and tuner in browser setup, or choose Enigma2. Initial SAT-IP scanning
remains fixed to Astra 19.2E and defaults to source/position 1; the GUI does not
provide arbitrary satellite/position selection.

Saved settings and credential seeds are protected on data. The broader internal
`parse_config` schema remains available for existing persistent settings,
configuration backups/restores and generated password hashes. It does not make
legacy fields valid public boot inputs. Repeating provisioning reuses the original
browser/backend seed rather than changing credentials or hardware preferences.
Existing saved key-only access remains supported unless explicitly disabled.

Recovery fields (`ssh_enabled` and `root_password`) may be updated in the FAT
file and applied on the next boot, including after initial setup. Omitting these
fields retains saved settings; explicitly use `ssh_enabled=no` to disable SSH.
Other initial preferences are not a supported settings-update interface.
Timezone and narrow input rules regenerate in the writable slot; eudev reload
and a bounded input change-trigger apply them to already-enumerated devices.

## Protected backend bootstrap

`satip_appliance/provision.py` generates independent random administrator and
playback passwords and unique local account/profile record IDs. Root commits
the protected seed before writing fixed bootstrap files, then publishes a
completion marker last. An interrupted bootstrap reuses its seed; unexpected
backend files before completion cause safe refusal, not deletion.

The reviewed bootstrap targets Tvheadend commit
`37453bc3fe5f9e10f3428ebb1abdc613f8b07186`, database schema 24:

- Basic-compatible authentication (`digest=2`), no setup wizard or `-C` startup.
- `full_version` is read from the installed backend, preventing a spurious
  migration/backup of the newly seeded schema.
- Two named accounts/ACLs only, both constrained to `127.0.0.1/32`.
- Administrator: unrestricted channel metadata (`channel_min=0`, `channel_max=0`),
  with streaming disabled, so EPG remains visible beyond the initial lineup.
- Playback: basic streaming, enabled MPEG-TS `pass` profile only, one connection,
  bounded channel-number range, no admin/web UI/HTSP/DVR rights.
- Exact pinned `change_profiles`/channel-range/connection-limit flags; password
  records use the pinned backend encoding, not a claimed password encryption.
- Fixed installed public mux-definition path; no private database in the image.

The backend runs as UID 424, with HTTP/HTSP bound to loopback. The supervisor
adds only the validated protected `--satip_xml` argument when supplied.

## Setup UI and tuner ownership

`satip_appliance/web.py` selects either fresh setup or normal management on
port 8080; the two do not run simultaneously. Fresh setup needs neither
backend UUIDs, catalogue or browser login. Setup endpoints check the installation
state on every request and close when setup finishes.

Setup retains local hostname/address
checks refreshed per request (including delayed DHCP/address changes),
same-origin/action-header protections, 32 KiB/10-second request-body bounds, strict
selection models, secret-free status and no-store/security response headers.
Use a trusted LAN: HTTP Basic is **not encrypted**. TLS is not introduced here.

1. Open the URL or QR code shown on HDMI, without a password. Choose your
   connected controls by their friendly names and test arrow/OK buttons. Infrared
   and USB keyboards are always enabled together.
2. The page searches active LAN interfaces for SAT-IP servers automatically.
   **Search again** retries after Ethernet/Wi-Fi becomes ready. A single discovered
   server connects automatically; with several servers, selecting one in the
   dropdown connects it directly without a confirmation button. If no server replies,
   expand **Add server by description URL**, enter its private-LAN HTTP XML URL,
   click **Add server** to connect it directly. Ports 80, 8080 and 38400 are supported.
   Registration restarts the backend through its supervisor and waits for the
   selected server's actual DVB-S frontend identities. Progress and errors appear
   automatically; repeated manual refreshes are not needed to populate tuners.
   **Retry connection** appears after a connection failure. Once scanning has
   started, the retained server is shown as information, not a disabled dropdown.
3. Select the intended tuner. Astra 19.2°E is fixed; setup automatically selects
   a backend-provided transponder definition and preserves it on retry. The optional
   tuner-number preference sets the initial default but does not prevent an explicit
   choice of another available tuner; tuner 2 is not a universal default. Tuner choice
   remains editable between searches and is applied by the next **Scan**.
4. Click **Scan**; no additional authorization checkbox is required. Setup rejects existing local
   subscriptions, disables other frontends, establishes actual network/source
   assignments and reads back single-frontend ownership.
5. Scan the bundled starter transponders first, then the complete satellite list,
   using the existing bounded scan/lease/rollback mechanism. There is no ten-minute
   total deadline. The proposed lineup prefers HD and is editable before saving.
   Progress shows transponders processed, supported channels found, the current
   transponder and elapsed seconds. Playback and preview are unavailable in this
   installation state. **Stop search** retains channels already found.
6. Select favourites using checkboxes, then **Confirm favourites** after the scan
   finishes or is stopped. A second view contains only the confirmed favourites
   for mouse/touch drag-and-drop ordering. Polling preserves this confirmed order;
   **Change selection** permits edits while retaining the order of kept channels.
   **Save channels and watch TV** commits the ordered lineup. Mapping revalidates
   service/video/audio policy, reads real IDs and writes the valid library last.
7. Root independently validates fixed-path configuration, credentials, library,
   access policy and the actual network before publishing `configured`. The
   setup process exits gracefully and the same web supervisor restarts normal
   management; playback may then start through its owned supervisor.

Setup requests have browser-side timeouts and restore controls after failure.
Polling reconnects automatically; action errors remain visible across polls and
channel selections survive catalogue refreshes. Finish requires prepared state,
a selected lineup and no pending recovery. The page detects normal management
after the supervised restart and reloads into its Status page automatically.

`configured` records `origin=fresh` and `hardware_qualified=false`; it is not
the old imported-database `qualified` state or a claim of picture/audio acceptance.

Pending mux rollback blocks new scans and publication. Cancel/recovery use the
existing durable journal, not an extra streaming probe. Retrying the same setup
selection reuses its network and deduplicates transponder creation. Changing the
committed server or satellite definition is refused; changing the frontend between
scans reuses the same network and discovered channels. Tuner changes are refused
during scanning, pending recovery, competing subscriptions or lineup completion.
A preparing/interrupted selection cannot publish
completion until setup has completed its frontend preparation checks.
The temporary rollback journal during an active scan is normal and does not
trigger a recovery warning. **Restore tuner settings** appears only when a scan
is no longer active and its restoration journal remains unresolved.

This controls appliance subscriptions, not another computer directly using the
external tuner: the owner must keep that tuner free during setup.

### Discovery and registration implementation

`satip_appliance/discovery.py` sends three bounded SSDP searches per active
multicast-capable IPv4 interface, reads bounded XML descriptions, and deduplicates
servers by UDN. If SSDP finds none, it tries conventional description endpoints
(port 38400 `/description.xml`, port 80 `/desc.xml`) on at most 254 directly
connected hosts, only on private /24-or-smaller subnets. This supports tuners
whose SSDP service does not reply. It rejects document types/entities, redirects
and unsupported URLs.
The web process performs these reads without backend credentials. Buildroot enables
`BR2_PACKAGE_PYTHON3_PYEXPAT`; the offline application validator checks its installation.

The non-root setup process writes a fixed-schema `web/server-request.json` mailbox.
The backend supervisor independently validates it, checks setup/ownership state,
persists `identity/satip-server.json`, and restarts its owned process generation with
the validated `--satip_xml` URL. HTTP/HTSP remain on loopback. Registration results
are published in `web/server-result.json`; `web/server-selection.json` holds display
metadata. An interrupted metadata write is recoverable from the request. The root
selection survives reboot and remains active after setup finishes. Changing servers
after channel setup has committed is refused; retrying registration of the same
server remains available.

The pinned backend serializes server nodes as **`satip_client`**, even though its
C device class is named `satip_device_class`. Setup filters on the serialized name
and advertised device UUID, then uses the returned frontend UUIDs.

## Secret handling after provisioning

After all supplied boot preferences have been persisted, provisioning atomically
restores `/boot/satip-config.txt` from `/usr/share/satip/satip-config.txt`. Cleanup
status is recorded in `identity/boot-import-cleanup.json`; failure can be retried
from the advanced section of setup or at the next boot. Saved settings and
credentials are retained. This is not a factory reset or secure flash erasure.

The first-run `POST /api/setup/config/restore` endpoint accepts only `{}` and
retains the existing action-header, origin and hostname checks. The non-root web
service writes only a fixed-schema request ID to its mailbox; the idle root player
supervisor validates that request and the installation state, requires mounted
boot FAT and a root-owned, non-service-writable image template, then performs a flushed
atomic replacement. No user-supplied paths, template contents or shell commands
are accepted. The Buildroot application package installs the public template in the image.

Commenting, unlinking or reformatting flash **does not securely erase** plaintext
Wi-Fi/browser secrets or console password hashes. Protect the whole card and
private backups; do not publish or reuse them as generic installation images.
If secure disposal is required, use an appropriate media sanitization procedure
or destroy the media; do not claim ordinary file deletion achieves it.

## Acceptance boundary

Setup completion records configuration, not physical qualification. Verify a
current-source fresh image on the intended Pi: protected listeners/accounts, actual
tuner/source, scan/lineup, supervised transition, picture/audio, controls and reboot.
Private database migration is separate and remains unqualified. See
[validation](validation.md).
