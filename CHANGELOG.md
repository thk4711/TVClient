# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.1] - 2026-11-21

### Release
- Publish the first downloadable Raspberry Pi 4 image distribution as the normal
  GitHub v0.1.1 release, with four uploaded assets: the unchanged image ZIP,
  one SHA-256 file, release notes and a consolidated corresponding-source/licence
  bundle. Keep the offline manual in the image and image qualification limits
  in the release notes.

### Changed
- Describe all current features as tested and supported within their compatibility
  limits; remove outdated feature labels from documentation, source messages and
  the Buildroot YouTube option, and include all features in the image release plan.
- Add opt-in installation of Debian build-host prerequisites with non-interactive
  privilege checks; dry runs and direct shell builds do not install packages.
- Compress remote SD images before transfer and collect exactly one image format
  (ZIP by default or raw), preserving raw and collected-artifact checksum manifests.
- Restore Astra 19.2°E SAT-IP channel backups into a different installation’s
  channel network while preserving its settings, credentials and tuner bindings.
  Accept existing version-1 archives and add explicit source/satellite metadata
  to new SAT-IP channel backups.
- Offer channel restore during first-boot setup after selecting a SAT-IP tuner,
  without requiring a full channel search. Report safe, specific backup refusals.
- Limit public boot-file preferences to hostname, timezone, SSH enablement,
  root password and the three Wi-Fi fields. Reject obsolete boot options while
  preserving legacy saved settings and configuration-backup compatibility.
- Require the image's factory-empty marker for automatic data initialization;
  remove the legacy boot-file initialization override and HDMI splash overrides.

### Fixed
- Update vulnerable zeroconf, Starlette, AnyIO, idna and certifi selections in
  application dependencies, Linux tests and the actual Buildroot image, with
  reviewed source/license hashes and installed-version verification.
- Distrust the compromised emSign ECC Root CA G3 in the image’s system CA store.
- Reject malformed/duplicate Host fields and use routed paths for device
  maintenance and channel-only versus complete configuration backup decisions.
- Preserve specific time-shift capture failure causes and terminal health counters,
  distinguishing interrupted TV input from USB problems. Bound automatic retries
  across repeated runtime failures instead of resetting backoff at process startup.
- Favourites drag no longer cancels when pointer capture is briefly lost before
  the first move, which could drop reordering on slower or touch devices.

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
