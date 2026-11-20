# Enigma2 source

**Audience: users.** Source compatibility and import reference. Follow the
[illustrated Enigma2 setup](user-manual.md#4b-enigma2--select-a-box-and-import-bouquets)
for the complete operating procedure.

[Documentation index](README.md) · [TV source settings](device-settings.md#tv-source)

## Scope and prerequisites

An Enigma2 receiver supplies channels it has already tuned and organised into
bouquets (channel groups). The Pi plays its network streams. No preliminary
SAT-IP scan is required; select Enigma2 directly during first setup.

Requirements:

- A configured Enigma2 receiver with OpenWebif on a trusted private IPv4 LAN.
- OpenWebif HTTP access without authentication; authenticated or HTTPS source
  access is unsupported. Do not weaken access protection on an untrusted network.
- Raw streaming, normally on port 8001; port 8002 transcoding is not used.
- Available source tuner capacity and genuinely free-to-air TV bouquets.
- Supported H.264/MPEG-2 HD/SD channels with German main audio.

UHD, radio, arbitrary IPTV URLs and encrypted/pay-TV channels are unsupported.
Select genuinely free bouquets: a box that decrypts a pay-TV channel can conceal
its original encryption status from stream inspection.

## Discover and import

During first setup, follow [step 4B](user-manual.md#4b-enigma2--select-a-box-and-import-bouquets).
After setup, use [TV source settings](device-settings.md#tv-source) to find or enter
a receiver, load its TV bouquets and import selected bouquets on Channels.

Discovery checks directly attached private networks of /24 or smaller on
OpenWebif port 80. For another port, enter the address/ports manually. Discovery
and bouquet listing do not interrupt TV or prove that a channel is playable.

Select 1–16 free-to-air TV bouquets. Import checks their streams and may take
many minutes; it temporarily pauses Pi playback and needs a free tuner on the
source box. It does not zap, put the box into standby or reconfigure it, but
streaming can compete with its viewing or recordings for tuner capacity.

A successful import replaces the Enigma2 available-channel list with validated
services, deduplicates overlapping bouquet entries and retains matching favourite
order. Failed/stopped imports keep the previous list. Channel search later
rechecks imported streams; it does not fetch new bouquets or search Astra frequencies.

## Activate and use

First setup saves the chosen source with your favourites. On a configured Pi,
choose **Activate Enigma2 source** in Settings after importing. Merely selecting
the source type or a discovered receiver does not switch playback. Activation
briefly restarts TV and reloads the page; a protected receiver asks for the current
web password.

SAT-IP and Enigma2 retain separate lineups; mixed-source favourites are unsupported.
The Enigma2 box allocates its tuner, so the Pi cannot choose its physical tuner.
Programme data may take time to appear; signal readings may need checking on the box.

For playback interruptions use the [manual's troubleshooting](user-manual.md#troubleshooting).
CLI commands, persistence and buffering are developer topics in
[Enigma2 internals](enigma2-internals.md).
