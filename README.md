# SAT-IP television receiver for Raspberry Pi 4

[![Checks](https://github.com/thk4711/TVClient/actions/workflows/checks.yml/badge.svg)](https://github.com/thk4711/TVClient/actions/workflows/checks.yml)
[![License: MIT AND Apache-2.0](https://img.shields.io/badge/license-MIT%20AND%20Apache--2.0-blue.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)](pyproject.toml)

Turn a Raspberry Pi 4 into a television receiver using an external SAT-IP server.
Connect the Pi to your TV or AV receiver over HDMI, then watch and control TV
with a configured USB keyboard or an Apple Remote. Use the browser interface
to set up the receiver, manage channels and favourites, and watch live TV.

The Pi receives TV over your network: it is a **SAT-IP client**, not a satellite
tuner or SAT-IP server. SAT-IP reception needs an external SAT-IP tuner connected
to your satellite signal. [Enigma2 reception](doc/enigma2.md) also supports
a compatible network receiver as an alternative source, including initial setup,
LAN discovery, browser bouquet import and separate source lineups.

## Features

- Watch live HD and SD television on your TV, with HDMI picture and sound.
- Faster switching between channels broadcast on the same satellite transponder.
- Automatic German surround audio over HDMI when the programme and connected
  device support multichannel PCM, with stereo fallback and working volume controls.
- Change channels, adjust volume, and browse favourites using a configured USB
  keyboard or an Apple Remote A1294 with a GPIO infrared receiver.
- See channel names, volume, the local date and time, and current programme
  information on screen. Browse current shows in the channel menu.
- Scan all installed Astra 19.2°E transponder tables, including the German
  free-to-air SAT.1, ProSieben and kabel eins transponder.
- Find channels and arrange your favourites in the browser, without interrupting
  TV playback when you save your favourites.
- Watch live TV in a browser as an alternative to HDMI viewing.
- Check receiver status, tuner activity, temperature, storage, and network
  connections from your phone, tablet, or computer. A lightweight
  [performance monitor](doc/usage.md#performance-monitor) opens on demand in a
  window with per-core CPU, RAM, network and disk graphs, plus the busiest processes.
- See the configured TV-source device and current SAT-IP tuner in Settings,
  change SAT-IP tuners without rescanning, and explicitly activate saved source lineups.
- Set the receiver name, timezone, Wi-Fi, passwords, and on-screen display
  timeout and language (English or German) through the browser settings.
  English is the image default, including the startup screen.
- Download and [restore channel-only backups](doc/device-settings.md#channel-backup)
  for available channels and favourite order, without changing device settings.
- Download and [restore configuration backups](doc/device-settings.md#configuration-backup)
  through the browser, including channels, favourites, network settings and access credentials.
- Automatic [recovery](doc/recovery.md) from frozen TV services, with a receiver
  restart when safe local recovery cannot complete.
- Guided first-boot setup supports SAT-IP servers or Enigma2 receivers.
  Discover a source, scan SAT-IP channels or import selected Enigma2 bouquets,
  then choose your favourites. Overlapping bouquet services are deduplicated.
  Settings survive restarts.
- [USB time shifting](doc/timeshift.md): pause and seek HDMI TV
  using a connected USB drive.
- [YouTube reception](doc/youtube.md): pair a phone with a TV code
  and play compatible public videos on HDMI.

## Getting started

For illustrated, self-service instructions from flashing the SD card to watching TV,
open the [User manual](doc/user-manual.md)
or the [printable HTML edition](doc/user-manual.html). The receiver also includes
a **User manual** link in its browser interface, available without internet access.

The appliance has been tested on real Raspberry Pi 4 hardware and runs its core
reception, playback, control and recovery features reliably in that setup. The
image profile is still a **development image**: there is no automatic firmware
update mechanism, and a full fresh-card/hardware qualification matrix is not yet
complete. Deploy it on a trusted LAN. See [validation limits](doc/validation.md).

You need:

- A Raspberry Pi 4, power supply, and an SD card with the appliance image.
- A TV or AV receiver connected over HDMI.
- Network access to an external SAT-IP tuner and its satellite signal, or a
  compatible Enigma2 receiver with OpenWebif and raw streaming enabled.
- A USB keyboard or supported GPIO infrared remote; both work together automatically.
  Wi-Fi-only first boot needs a USB keyboard. See [first boot](doc/buildroot-first-boot-setup.md).
- A browser on a phone, tablet, or computer for setup and channel management.

1. Write the appliance image to the SD card. See the
   [image guide](buildroot/README.md) if you need to build an image.
2. Connect HDMI and power. The image initializes its factory-empty data automatically.
3. Connect Ethernet, or use a USB keyboard to select Wi-Fi and enter its password
   on HDMI. The welcome screen shows a setup URL and matching QR code.
4. Follow the [first-boot setup](doc/buildroot-first-boot-setup.md) to open the
   password-free browser interface. Choose SAT-IP and scan its channels, or
   Enigma2 and import selected free-to-air TV bouquets. Validation may take more
   than ten minutes.
5. Choose your favourites and start watching. See [Usage](doc/usage.md) for
   controls, programme information, and browser viewing.

## Supported TV and viewing modes

The receiver supports free-to-air HD and SD channels using H.264 or MPEG-2, with
German main audio. First-boot channel setup targets Astra 19.2°E.

HDMI viewing, browser viewing, and channel searches use the tuner exclusively;
you cannot watch on HDMI and in a browser at the same time. Stopping browser
viewing returns control to HDMI; the physical keyboard or remote can also take
back control.

UHD/HEVC channels, encrypted channels, and recording are not supported.
USB time shifting lets you pause and seek HDMI television using a connected drive;
history is temporary, not a permanent recording. YouTube reception supports compatible public H.264/AAC
videos up to 720p. See the linked feature guides for details.

## Documentation

The manual and operating references below are for users. Image building, software
internals and acceptance procedures are separate developer/administrator guides.

| Task | Guide |
|---|---|
| Complete illustrated setup and operating manual | [User manual](doc/user-manual.md) · [Printable HTML](doc/user-manual.html) |
| Set up the receiver | [First boot](doc/buildroot-first-boot-setup.md) |
| Watch TV and manage channels | [Usage](doc/usage.md) |
| Change any browser setting | [Device settings](doc/device-settings.md) |
| Pause and seek live TV | [USB time shifting](doc/timeshift.md) |
| Play YouTube from a phone | [YouTube TV code](doc/youtube.md) |
| Diagnose a problem | [Troubleshooting](doc/troubleshooting.md) |
| Build an image | [Buildroot image guide](buildroot/README.md) |
| Develop or contribute | [Development and contributing](doc/development.md) |

The [documentation index](doc/README.md) includes further user and contributor
guides. Implementation details and test instructions are in the contributor
documentation rather than this appliance overview.

## Licence

The project's original code and documentation are licensed under the
[MIT License](LICENSE). Third-party components retain their own licences and
notices; see [third-party licensing](doc/development.md#publication-and-licensing).
