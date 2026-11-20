# Device settings

**Audience: users.**

[Documentation index](README.md) · [Usage](usage.md)

After first-boot setup, open **http://<receiver-address>:8080/#settings**.
This is the main guide for changing receiver settings; you do not need to edit
configuration files or rebuild the image. Changes are saved across reboots.

New installations default to login-free home-LAN access, including device controls.
If you set a web password, sign in as **satip**. HTTP does not encrypt passwords.

For illustrated instructions and nontechnical troubleshooting, see the
[User manual](user-manual.md#changing-settings).

## Device

- **Hostname**: enter a lower-case name using letters, numbers and hyphens,
  without a hyphen at either end. Save, then reboot to apply the name. The
  hardware-derived satip-….local address normally remains unchanged; use the
  numeric address if a custom hostname does not resolve.
- **Timezone**: select your local timezone and choose **Save device settings**.
  The on-screen clock updates without restarting TV.

## Wi-Fi

Ethernet uses DHCP without setup. To connect to Wi-Fi:

1. Enable Wi-Fi. Enter **Network name**, **Wi-Fi password** and your two-letter
   **Country code**. **Scan for networks** helps you choose the network name.
   A blank password keeps the old password only when the network name is unchanged.
2. Choose **Try Wi-Fi settings**. The connection and TV may briefly pause.
3. Reconnect to the receiver, using its new DHCP address if needed. Choose
   **Keep settings** within **90 seconds** of starting the trial.

Without confirmation, the previous settings return automatically. **Revert now**
restores them immediately. An interrupted trial also rolls back after reboot.
Keep Ethernet connected when possible while testing Wi-Fi.

Confirmation needs a connected Wi-Fi link and an IPv4 address. You can disable
Wi-Fi only when Ethernet has a usable IPv4 address. The form supports DHCP and
password-protected WPA-PSK networks with UTF-8 network names and printable ASCII passwords, not open or
enterprise networks or static IP addresses.

## Passwords & access

The browser password and root/SSH password are **separate**.

- **Change web password**: enter and confirm a password to enable browser login, then sign in as satip.
- **Change root password**: enter and confirm the new root password. This changes
  SSH and console access, not the browser login.
- **Enable SSH**: allow or stop remote shell access. Enabling requires a saved
  root password or an authorized SSH key. Connect as root, not satip.

Password changes use your authenticated browser login and do not ask for the old
password. On password-protected installations, SSH changes, USB unmount and
maintenance actions ask for **the current web password**, not the root password.
Login-free installations do not ask for a password. Cancel or Escape closes a
dialog without applying it.

## Time shift and USB storage

USB storage enables HDMI pause and seek. Prefer a dedicated USB 3
SSD. See [Time shifting](timeshift.md#playback-controls) for playback controls and limits.

1. Connect a USB drive and choose **Choose device**. Check its model, capacity,
   serial number (if reported) and Linux disk name carefully.
2. Choose **Use existing ext4 filesystem** to keep an existing single ext4
   filesystem, or **Format & use device** to prepare the disk.
   **Formatting permanently erases the entire selected disk.** Confirm the
   device-specific warning; no typed phrase or password re-entry is required.
   Cancel leaves the disk unchanged.
3. Once mounted, the page shows the device, capacity and free space. Set
   **Storage limit (GiB)** and choose **Save storage limit**. The default is 8 GiB
   or the capacity of a smaller disk; valid limits run from 1 GiB to the device
   capacity, including fractional values. Saving trims old history without ending
   the rolling session; reducing the limit can move an old viewing position.
4. Choose **Safely remove USB** before unplugging. This briefly restarts TV,
   unmounts storage and remembers the disk/budget. Wait for successful confirmation.
5. **Forget USB configuration** is separate, available after safe removal. It
   clears the remembered selection/budget without erasing USB files.

Mounting requires at least 2 GiB free; formatting requires a disk of at least
3 GiB. The receiver refuses system/SD disks and disks already in use. The player
reserves about 1 GiB free space, so a limit equal to full capacity does not make
all of that space usable for buffering.

A saved disk reconnects automatically after reboot without formatting. Live TV uses
only a small RAM prebuffer, not continuous USB writes. Pause activates rolling USB
capture and preserves the current picture; Return to live stops USB writes and
discards that session. At a full active buffer, old segments are removed while the
session continues.
If a viewing position expires it advances to retained content; paused TV stays
paused. Channel changes/restarts discard history. This is not recording. Safe
removal prevents immediate remount until reconnection or explicit browser mounting.
Do not unplug after a failed unmount or use other disk tools during these operations.

## TV on-screen display

**Language** selects **English** or **Deutsch** for TV menus, programme-information
labels, playback messages, time-shift feedback, YouTube overlays, and the HDMI
startup/setup screen. English is the default. Select **Save settings** to apply
the language without restarting TV. Open menus/details and the current programme
banner update while retaining selection, scroll position and expiry time.
Other transient messages use the selected language when next shown.
Broadcast channel names, programme titles/descriptions and audio retain their
broadcast language. The English clock uses YYYY-MM-DD; German uses DD.MM.YYYY.
Both use the device timezone and a 24-hour clock.

The choice survives reboot. Early startup uses English until the saved setting
is readable, then switches to the selected language.

**Bottom bar timeout (seconds)** controls how long channel/programme and volume
bars stay visible. Choose 0.5–30 seconds and select **Save settings**. The change
applies to the next bar without restarting TV. Favourites and programme-details
panels stay open until you close them.

## YouTube link

- **Link with TV code**: show a five-minute pairing code for the phone's YouTube app.
- **Disable YouTube / return to TV**: stop YouTube reception but keep linked phones.
- **Forget linked phones**: disable reception and remove the pairing identity.

See [YouTube pairing](youtube.md#pair-and-use) for phone instructions and supported
videos. Keep pairing codes private: linked phones can control the receiver even
from another network.

## TV source

**Settings > TV source** separates **Current TV source**, **Configure a source**,
and **Activate a configured source**. The current summary shows the external
server/receiver name and address, the configured SAT-IP tuner and the backend's
currently receiving input. A saved address is not proof of a live connection:
unavailable and idle states are labelled explicitly. **Tuning, but no stream data
received** means a tuner was requested but is not delivering data: check that
its satellite cable is connected and the signal is working. Enigma2 allocates physical
tuners itself; the Pi cannot select or report its physical tuner.

For SAT-IP, choose a numbered tuner with a connected satellite cable on the
**configured server**, then select
**Apply SAT-IP tuner**. Confirm the brief playback/page restart. Channels,
favourites, satellite assignment and reception policy are retained; no channel
scan is started. The selection survives restarts. The already configured tuner
needs no action. Activate SAT-IP first if Enigma2 is currently active. Tuner
changes require an available server and no scan, import, browser Live TV,
Wi-Fi trial or maintenance operation. Linked/master-slave wiring and a tuner
without the configured satellite position are refused rather than guessed.
Failed changes restore the previous configuration; interrupted changes use
journaled recovery before TV starts.

In SAT-IP mode, **Change tuner** is the only configuration control below the
source-type selector. The server identity appears once in Current TV source;
there is no redundant SAT-IP discovery or server-selection block. Replacing
the SAT-IP server after setup is not supported by this page.

In Enigma2 mode, use **Find Enigma2 receivers**. The receiver-selection list
appears only when discovery has found receivers. Choosing a discovery result
does not activate it. Choose a result or enter its address/ports, then
**Load Enigma2 TV bouquets** and import 1–16 selected free-to-air bouquets on
Channels. Non-default OpenWebif ports require manual entry. Discovery and listing
do not interrupt playback; import temporarily pauses it, deduplicates overlapping
services and replaces available channels only on success. Failed/stopped imports
retain the previous lineup and matching favourites are preserved.

Choose **Activate SAT-IP source** or **Activate Enigma2 source** to use that
configured source's saved lineup. Activation is disabled for the already active
source or one not yet configured; the page explains why. Confirm the playback/page
restart; with login enabled, confirm using the current web password (also required
for tuner changes). Each source retains its own channels and favourite order.
See [Enigma2 reception](enigma2.md) for compatibility and import guidance.

## Channel backup

Open **Channels** and use the separate **Channel backup** card.

- **Download channel backup** saves all available channels, saved channel mappings
  and favourites in their saved order as a dated `satip-channels-….tar` file.
  Click **Save favourites** first if you want to include your browser edits.
- **Restore channel backup** accepts a channel backup up to **32 MiB**. Uploading
  validates the file without changing channels. Review the backup date and
  channel/favourite counts, then confirm replacement. On password-protected
  receivers, enter the **current web password**; login-free receivers do not ask.
  Cancel leaves channels unchanged.

Restore **replaces**, rather than merges, the available-channel list and saved
favourites, including their order. Unsaved browser edits are discarded when the
page reloads after restore. TV and the web service briefly stop during backup or
restore; keep the page open while it reconnects. The receiver does **not reboot**.
Finish channel searches, browser Live TV, Wi-Fi trials and pending recovery first.

These backups include the satellite multiplex/service records needed by the
channels, but **not** passwords, SSH keys, network preferences, SAT-IP server/tuner
selection, hostname, timezone, OSD, USB or YouTube settings, player volume, EPG
caches or media. These settings stay as they are at restore time. The restricted
playback channel-number range may widen only as needed for restored mappings;
this does not grant administrative access.

**SAT-IP channel backups can be restored after reflashing or on another Pi**
configured for Astra 19.2°E. The importer places multiplexes and services into
the destination’s channel network, keeping its SAT-IP server, tuner and reception
settings. A different internal network identity is accepted. The saved lineup
replaces the active lineup; inactive TV-source lineups are retained. Conflicting
backend identifiers or unrelated channels that would gain access are refused
rather than overwritten. Restored frequencies and services can become outdated;
rescan if a channel no longer receives a signal.

During first-boot setup, select your SAT-IP server and tuner, then choose
**Restore channel backup instead**. Review the date and counts and confirm;
setup finishes with the restored favourites, without a full channel search.
Cancel imports no channels and leaves the selected tuner ready for a scan or
another backup. Configure network access and reception on the new device first.

The plain-tar manifest remains version 1 and requires the pinned Tvheadend
revision and compatible persistent schema. New SAT-IP archives use version-2
channel metadata with explicit source/satellite information. Older version-1
SAT-IP archives are also accepted when their multiplex records identify Astra
19.2°E DVB-S/S2 reception. Blank multiplex orbital positions inherit the network;
a legacy backup imported into a different network must contain at least one
explicit Astra position and no conflicting position. If every position is blank,
use a new backup with explicit satellite metadata or rescan. An older software image may reject new channel metadata;
use the updated importer. This is not a general backend-format migration.
**Enigma2 channel restore still requires the same configured channel network**;
portable Enigma2 import is not supported. Channel-only and full configuration
files are deliberately not interchangeable.

The archive is not encrypted. Although it contains no access credentials, it
reveals your channel lineup; store it appropriately and transfer only on a trusted
LAN. A private pre-restore configuration is retained for recovery, and interrupted
activation uses the existing journaled rollback mechanism. Physical power-loss
qualification remains outstanding.

## Configuration backup

Use **Download backup** to save the receiver configuration as a dated
`satip-backup-….tar` file. Finish channel searches, browser Live TV, Wi-Fi trials
and pending recovery first. TV and the web service briefly pause while writers
are stopped for a consistent snapshot; the page reconnects and downloads the
file. Keep the page open until it reports completion.

**Backups are not encrypted and contain passwords, private SSH host keys,
authorized keys and YouTube pairing credentials.** Keep them in private storage;
do not publish them or attach them to an issue. HTTP also does not encrypt the
transfer: use only a trusted LAN.

To restore:

1. Choose **Restore backup** and select a backup made by this feature, up to
   **32 MiB**. The receiver validates it before changing settings.
2. Review the backup date and overwrite/reboot warning. Cancel leaves settings
   unchanged. Enter the **current web password** to confirm, not the password
   saved in the backup.
3. Wait for the receiver to restore and reboot, then reconnect and reload the page.
   Use the **restored** Wi-Fi/network settings and web password. The hostname,
   DHCP address, SSH enablement, password and fingerprint may change too.

The backup includes the supported device, network, input, OSD, time-shift and
YouTube settings; channels, favourites and catalogue; saved player state;
SAT-IP tuner/backend configuration; and access credentials. It excludes firmware,
boot partition files, arbitrary OS customizations, EPG programme caches, logs,
old scan/deployment backups, running sessions and USB media/buffers. Restoring a
saved USB selection never formats the disk and requires the matching disk to be
connected. Programme information repopulates after TV starts.

Version 1 supports completed **fresh appliance** installations with persistent
schema 1 and the same pinned Tvheadend revision. It does not import legacy/manual
archives, migrate incompatible backend versions, or restore a whole SD card.
After reflashing a compatible image, complete first-boot setup before using
Settings to restore. The configured SAT-IP server and selected input/USB hardware
must still be available; copying settings does not reassign physical tuners.

A private pre-restore configuration is retained on the receiver for recovery.
Interrupted activation before the commit rolls back at boot; committed activation
uses the restored configuration. This is not a firmware rollback mechanism or a
power-loss qualification guarantee. See [operations](operations.md#backups).

## Maintenance

- **Restart TV playback** restarts the HDMI player, not the tuner backend.
- **Reboot** restarts the receiver.
- **Shut down** stops services and powers down. A power cycle is needed to start again.

Finish channel searches, browser Live TV and pending channel recovery before
network or maintenance changes. Safe removal and forgetting storage are also blocked during a Wi-Fi trial.
Firmware updates, custom NTP servers, Bluetooth and radio-specific audio settings
are not available on this page.

## Recover a forgotten web password

You can reset the browser password yourself without erasing channels or settings.
This is an advanced recovery task, not part of ordinary first setup. It requires
root SSH or console access and changes only the web login.

If you already have root access, skip to the command below. Otherwise:

1. Power down the Pi before removing the microSD card. Use an available console
   or SSH connection to shut down cleanly if possible; avoid interrupting writes,
   scans, imports or backup/restore operations. Never remove a powered card.
2. Put the card in your computer. Open its visible boot/FAT partition and edit
   **satip-config.txt** as plain text. Preserve other entries; replace existing
   entries rather than adding duplicate keys. Set **ssh_enabled=yes** and
   **root_password=YOUR_NEW_ROOT_PASSWORD** with your chosen temporary root
   password. Remove obsolete entries such as **recovery_password_hash** or
   **ssh_authorized_key**; only the seven documented boot parameters are accepted.
   These recovery fields work after first setup as well.
3. Eject the card, put it back in the Pi and boot. Find its numeric address in
   your router if needed, then connect from your computer with
   **ssh root@RECEIVER_IP**. Check the host fingerprint rather than disabling
   host-key checking. Enter the root password you just set, not a web password.
4. In that root terminal on the Pi, run this command. It prompts privately for
   a new web password and its confirmation, so no password is placed in the
   command line or shell history:

~~~sh
PYTHONPATH=/opt/satip python3 -c 'import asyncio, getpass; from satip_appliance.device_client import request; p=getpass.getpass("New web password: "); c=getpass.getpass("Confirm web password: "); print(asyncio.run(request("web_password", {"password": p, "confirm": c}))["message"])'
~~~

5. Open the receiver in a new/private browser session and log in as **satip**
   with the new web password. The helper saves it; no image build is needed.
6. In Settings, replace any temporary root password and disable SSH if no longer
   needed. Check/remove plaintext recovery password entries from the boot file
   if they remain; store the card and credentials privately. Removing an initial
   recovery field does not delete the already saved password.

Do not edit or publish backend credential files to recover the browser login.
The [boot preferences reference](provisioning.md#allowlisted-preferences) gives
recovery-field details and restrictions. Firmware/SD-card replacement is not a
password-reset procedure.

## If something goes wrong

See [Troubleshooting](troubleshooting.md). Lost SSH access can be recovered through
the boot file; see [recovery preferences](provisioning.md#allowlisted-preferences).
Persistent file formats and privileged-helper details belong to the contributor
[configuration reference](configuration.md#device-preferences-and-helper), not routine setup.
