# Install the Raspberry Pi receiver

**Audience: users.** Hardware and image-writing reference; the
[User manual](user-manual.md#first-time-setup) contains the detailed setup procedure.

[Documentation index](README.md) · [First-boot checklist](buildroot-first-boot-setup.md)

## Requirements

- Raspberry Pi 4, suitable power supply and a microSD card large enough for the image.
- HDMI TV or AV receiver. The normal output is 1080p50; sound is stereo or surround
  according to the programme and connected device's supported PCM audio.
- A home network and either an external SAT-IP tuner connected to your satellite
  signal, or a compatible [Enigma2 receiver](enigma2.md) with existing channels.
  The Pi is a network client; it has no coaxial satellite tuner.
- A USB keyboard or supported GPIO infrared receiver and remote. Both work
  together; Wi-Fi-only first boot needs a USB keyboard. See [infrared hardware](infrared.md).
- A phone, tablet or computer with a browser on the same non-guest home network.
- Optional dedicated USB storage for [time shifting](timeshift.md).

SAT-IP first setup targets Astra 19.2°E. Free-to-air H.264 HD and MPEG-2 SD TV
with supported German main audio is supported; UHD, encrypted TV and recording
are not. See the [appliance overview](../README.md#supported-tv-and-viewing-modes).

## Obtain and write the image

Use the project's appliance image, not Raspberry Pi OS. If supplied zipped,
extract **sdcard.img** first. In Raspberry Pi Imager choose **Use custom**, select
the image and correct card, then write and verify it. Do not use Imager's OS
customisation. See the [illustrated flashing steps](user-manual.md#1-flash-the-microsd-card).

**Writing the image erases the entire selected card, including saved receiver settings.**
Keep a private configuration backup before replacing an existing installation;
see [backup compatibility](device-settings.md#configuration-backup).

If you need to build an image, the developer [Buildroot guide](../buildroot/README.md)
explains how. The current image profile is a development profile, not a fully
qualified release image.

## Connect and set up

With power disconnected, insert the card and connect HDMI, controls and Ethernet
if available. Then power on. No configuration-file edits or service installation
are required. Without Ethernet, use the USB keyboard to join Wi-Fi on the TV.

Open the exact browser URL or QR code shown on HDMI and follow the
[setup checklist](buildroot-first-boot-setup.md). Choose SAT-IP and scan, or
Enigma2 and import bouquets, then save your favourites to start TV. First setup
requires no password. Settings and favourites survive restarts.

## If setup does not work

Start with the [manual's troubleshooting checks](user-manual.md#troubleshooting).
SSH is optional advanced recovery access, not a prerequisite for installation.
For shell-based diagnosis, see the administrator [Troubleshooting](troubleshooting.md)
and [Operations](operations.md) guides.
