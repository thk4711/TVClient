# Configuration examples

These sanitized examples illustrate the internal player and web schemas. They
are not provisioning inputs and must not replace a configured receiver’s data.
Use the [first-boot guide](../doc/buildroot-first-boot-setup.md) for an appliance.

- [Player configuration](player-config.json): reference HDMI/audio paths and
  keyboard VID/PID; select values matching your hardware.
- [Web configuration](web-config.json): authenticated management with synthetic
  backend identifiers. Replace allowed hosts and identifiers only when developing
  a custom installation; normal setup generates them.

No passwords or real backend identities are included. Credential files are
separate and private. Synthetic libraries used by tests are under
[`tests/fixtures/`](../tests/fixtures/). See [configuration](../doc/configuration.md).
