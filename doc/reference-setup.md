# Reference hardware and example settings

**Audience: developers and advanced administrators.**

[Documentation index](README.md) · [Configuration](configuration.md)

These reference identities explain the supplied defaults. They are not a live
installation inventory or portable configuration.

| Item | Reference identity |
|---|---|
| Pi | Raspberry Pi 4 Model B, aarch64 |
| Case / remote | Argon ONE V2 for Pi 4, including GPIO23 IR receiver; infrared Apple A1294 |
| Keyboard | USB VID:PID `04d9:0006`, multiple event nodes |
| SAT-IP | Telestar Digibit Twin |
| Signal | Astra 19.2°E, conventional LNB/multiswitch, source 1 |
| DRM | `/dev/dri/by-path/platform-gpu-card`, `HDMI-A-2`, `1920x1080@50` |
| Audio | `alsa/hdmi:CARD=vc4hdmi1,DEV=0`, stereo 48 kHz/s16 PCM |

For fitting the Argon case or wiring a separate three-pin 3.3 V receiver, see
[Infrared hardware](infrared.md). The wiring is VCC to physical pin 1 (3.3 V), GND
to pin 6, and OUT to pin 16 (GPIO23); power must be disconnected first. The
[Argon40 website](https://argon40.com/) identifies current case models.

Use stable device identities, not remembered card/event/rc numbers. Select a
working frontend on your server; tuner 2 is a reference preference, not a universal
requirement. The bundled Apple NEC32 keymap covers the observed remote ID
`0xaa`; other paired IDs may require relearning.

The generic image embeds no library or backend identity; setup reads actual
server/frontend/service IDs. The [library example](configuration.md#channel-library)
uses synthetic identifiers to illustrate the schema. Do not copy example
mappings over an existing library.

## Administration

Use your provisioned root recovery access and normal SSH host-key verification.
The appliance uses Dropbear with forwarding disabled; see
[image recovery](../buildroot/README.md#first-boot-and-recovery).

First setup is password-free. New installations default to login-free home-LAN
management; setting a web password enables authentication. See
[security](security.md), [validation](validation.md), [operations](operations.md), and
[Buildroot services/data](buildroot-services-persistence.md).
