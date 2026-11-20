# First-boot setup

**Audience: users.** A checklist for first setup. Follow the linked
[User manual](user-manual.md#first-time-setup) steps for illustrations and detailed instructions.

[Installation requirements](installation.md) · [Device settings](device-settings.md)

| Step | What to do | Detailed instructions |
|---|---|---|
| 1 | Write and verify the appliance image on the correct microSD card; this erases it | [Flash the card](user-manual.md#1-flash-the-microsd-card) |
| 2 | Connect HDMI, controls and Ethernet, or use a USB keyboard for Wi-Fi on the TV | [Connect TV and network](user-manual.md#2-connect-the-tv-and-your-home-network) |
| 3 | Open the displayed URL or QR code from the same home network; no password is required | [Open browser setup](user-manual.md#3-open-setup-on-your-phone-or-computer) |
| 4 | Choose SAT-IP and scan, or Enigma2 and import selected free-to-air TV bouquets | [Choose a source](user-manual.md#4-choose-your-source-and-load-channels) |
| 5 | Select individual favourites; at least one is required | [Choose favourites](user-manual.md#5-select-your-favourites) |
| 6 | Arrange them and select Save channels and watch TV | [Arrange and start TV](user-manual.md#6-arrange-favourites-and-start-watching) |
| 7 | Set name, timezone and preferences; download a private configuration backup | [Set preferences](user-manual.md#7-set-the-receiver-name-timezone-and-preferences) |

## Network and controls

Ethernet uses automatic addressing (DHCP). Wi-Fi-only setup requires a USB
keyboard connected to the Pi; the infrared remote cannot enter a Wi-Fi password.
Visible WPA/WPA2 Personal networks are supported, including mixed WPA2/WPA3
networks accepting WPA2 passwords. Use Ethernet for enterprise, open or WPA3-only networks.

Use the exact **http://RECEIVER_ADDRESS:8080/** shown on TV. If the
hardware-derived **satip-….local** name does not resolve, use the numeric address.
Avoid guest networks that isolate devices. Standard USB keyboards and GPIO IR
work together automatically; a separate USB remote may need selection in setup.
See [infrared hardware](infrared.md) before fitting a sensor.

## Choose the correct source path

- **SAT-IP:** an external network tuner with a satellite signal. Initial scanning
  targets Astra 19.2°E. Select an available, connected tuner and leave it free;
  a full scan may take more than ten minutes. Follow [SAT-IP setup](user-manual.md#4a-sat-ip--select-a-tuner-and-scan).
- **Enigma2:** an already configured TV receiver with compatible OpenWebif and raw
  streaming. Import bouquets instead of scanning Astra or selecting a tuner number.
  Follow [Enigma2 setup](user-manual.md#4b-enigma2--select-a-box-and-import-bouquets).
  No preliminary SAT-IP setup is needed.

Wait for scan/import and any recovery to finish before saving. Check HDMI picture,
sound, channel and volume controls after TV starts. Reboot if you changed the
hostname, then check that favourites and settings remain saved.

## After setup

Use [Usage](usage.md) for reference, [Device settings](device-settings.md) for
setting restrictions, and the [manual's troubleshooting](user-manual.md#troubleshooting)
for problems. Do not reflash to change ordinary settings or favourites.
Developers should use [Provisioning](provisioning.md) and
[First-boot qualification](first-boot-validation.md), not edit files for routine setup.
