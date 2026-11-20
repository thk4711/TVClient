# Backup qualification

**Audience: developers and advanced administrators.** Current acceptance procedure,
not a record of deployments or an assertion of complete hardware qualification.

[User backup instructions](device-settings.md#configuration-backup) · [API](api.md#configuration-backup-api-configured-appliance-only) · [Validation](validation.md)

## Scope

Configuration backup/restore and channel-only backup/restore are implemented.
Version 1 is tied to persistent schema 1 and the pinned backend revision.
SAT-IP channel restore accepts a different network identity, including a fresh
installation after source/tuner preparation. It relocates multiplex records into
the destination Astra 19.2°E DVB-S/S2 network and preserves reception bindings.
Version-2 channel metadata identifies source/satellite; legacy version-1 SAT-IP
metadata is supported with multiplex-level satellite/delivery checks. Enigma2
channel restore still requires the same configured network. Full restore reboots;
channel-only restore restarts services without reboot or changing device/access
preferences. Neither is a firmware update or generic cross-version importer.

## Automated checks

Run `tests/test_backup.py`, `tests/test_channel_backup.py` and the device API/UI
checks using isolated fake roots. Cover bounded plain tar and packed backend
records, manifest checksums, path/link/credential rejection, kind separation,
streaming size/deadline limits, authentication, cancellation, free-space checks,
permission reconstruction and interrupted rename/helper recovery. Also cover
different destination network UUIDs, legacy and version-2 SAT-IP metadata,
satellite/delivery mismatches, inherited blank orbital positions and legacy
satellite evidence, collisions with inactive networks, active source
snapshot consistency, first-setup streaming uploads and completion without an
existing library or scan, and interrupted first-setup import rollback.

## Live acceptance checklist

These actions interrupt TV or replace settings. Use a backed-up test installation,
independent root recovery access and private storage for credentials/archives.
Do not run concurrently with scan, preview, Wi-Fi trials or other maintenance.

1. Record compatible image/backend identity and baseline settings, channel order,
   access and configuration hashes without printing secrets.
2. Download both backup kinds through the real browser. Confirm writer quiescence,
   page reconnection and completed private downloads.
3. Upload malformed, oversized, incompatible and wrong-kind files. Confirm refusal
   and unchanged configuration. Upload a valid file and cancel; nothing is activated.
4. With web login enabled, test incorrect current-password refusal. On a login-free
   receiver, confirm that password re-entry is not required.
5. Make reversible channel/order changes, restore channel-only backup and verify
   replacement (not merge), preserved device credentials/preferences and no reboot.
6. Make reversible settings changes, restore full backup and reconnect after reboot
   with restored network/login/SSH identity. Confirm file permissions, saved source
   profiles, cleared activation journal and retained private pre-restore tree.
7. On a separate factory-fresh card, select a SAT-IP tuner and restore a legacy
   channel archive from another installation instead of scanning. Verify retained
   new credentials/network/tuner bindings, favourite order, setup completion,
   reconnect/reboot persistence and subsequent source switching.
8. Observe physical HDMI picture/audio and verify ordinary controls. A playing
   status response is not physical acceptance.

Use [the channel-backup exercise](../validation/exercise_channel_backup_on_pi.py)
only with its explicit interruption confirmation and reviewed prerequisites.

## Remaining qualification

Full current-source image/fresh-card restore, other compatible devices, actual
Wi-Fi/address/SSH-fingerprint migration, restored USB selection and linked-phone
control, full/read-only storage, interrupted helper/SD writes, physical power cuts
and endurance need separate qualification. Isolated rollback tests and successful
normal restore do not establish power-loss durability. Keep full backups private:
they contain passwords, SSH keys and pairing credentials and are not encrypted.
