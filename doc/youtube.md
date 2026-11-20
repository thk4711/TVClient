# YouTube “Link with TV code”

**Audience: users.** Pairing and compatibility reference. See the
[User manual](user-manual.md#youtube-from-a-phone-or-tablet) for the operating steps.

[Documentation index](README.md) · [YouTube settings](device-settings.md#youtube-link)

## Pair and use

1. In Settings → YouTube link, select **Link with TV code**. A 12-digit code
   appears in Settings and briefly on HDMI; it expires after five minutes.
2. In your phone/tablet's YouTube app, use Settings → Watch on TV → Enter TV code
   (wording varies). Enter the numeric code and select the linked receiver.
3. Choose a compatible public video. YouTube replaces HDMI television while playing.
4. Use phone play/pause, seek and volume, or the receiver's volume and Play/Pause
   controls. Play/Pause does nothing while a video is still loading.
5. Select **Disable YouTube / return to TV**, or press a receiver channel/navigation
   button to return to television. Searches and browser viewing also take priority.

Closing the phone app does not necessarily stop playback. Disable keeps linked
phones; **Forget linked phones** removes their pairing. Enabled state survives reboot,
but old pairing requests are not replayed.

### QR shortcut

Settings offers a QR/link shortcut beside the code. It may open YouTube help
rather than pairing, depending on the app. If it does not offer linking, enter
the numeric code using the steps above. This is not Google-account sign-in.
The QR is generated locally; no external QR service receives the code.

## Limits and privacy

This is an unofficial integration, not a certified Chromecast/AirPlay receiver.
YouTube can change or disable it. Only compatible public H.264/AAC videos up to
720p are supported. Some public videos cannot resolve; private, age-restricted
or members-only videos and automatic recommendations are unsupported.

Keep pairing codes private. Linked phones can control the receiver through
YouTube even outside your home network. **Disable** retains that pairing;
use **Forget linked phones** if you no longer trust a linked device.
Use the receiver's browser only on a trusted home LAN; never expose it to the internet.

Resolver installation, protocol/media-bridge details and acceptance checks are
in the developer [YouTube internals](youtube-internals.md) guide.
