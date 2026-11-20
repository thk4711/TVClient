# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-11-20

Initial public release. The appliance turns a Raspberry Pi 4 into a SAT-IP
television client and has been tested on real Raspberry Pi 4 hardware.

### Added
- SAT-IP client reception of free-to-air HD and SD channels with HDMI picture
  and sound, including German multichannel audio with stereo fallback.
- Faster switching between channels on the same satellite transponder.
- Enigma2 receiver support as an alternative source, with LAN discovery and
  browser bouquet import.
- Browser management UI for setup, channels, favourites, settings and live
  browser viewing, with English and German on-screen display.
- Astra 19.2°E transponder scanning and favourite management.
- USB time shifting (pause and seek live HDMI TV).
- YouTube reception by pairing a phone with a TV code.
- USB keyboard and Apple Remote A1294 (GPIO infrared) control.
- Channel-only and full configuration backup and restore.
- Automatic recovery from frozen TV services.
- Guided first-boot setup for SAT-IP servers or Enigma2 receivers.
- Reproducible Buildroot image profile for the Raspberry Pi 4 (64-bit).

### Known limitations
- Development image: no automatic firmware update mechanism yet; deploy only on
  a trusted LAN. UHD/HEVC, encrypted channels and recording are not supported.

[Unreleased]: https://github.com/thk4711/TVClient/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/thk4711/TVClient/releases/tag/v0.1.0
