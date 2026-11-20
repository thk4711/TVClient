# YouTube internals

**Audience: developers and advanced administrators.**

[User pairing guide](youtube.md) · [Validation](validation.md)

The receiver uses YouTube's undocumented Lounge protocol and the existing owned
mpv HDMI player. The integration is opt-in and is not certified Google Cast.

## QR implementation and checks

The bundled MIT-licensed qrcode-generator **1.4.4** renders the QR locally; no
external QR service receives the code. The QR and its link are cleared when
the API reports expiry, an invalid code, disabled reception, or a status failure,
and when Disable/Forget is requested. They have the same privacy implications
as the numeric code. No changes to the receiver protocol, identity or player
are required. The JS/CSS/HTML and encoder licence are included in image packaging.

The read-only browser check is `validation/check_youtube_qr_browser.cjs`. Set
`SATIP_QR_URL`, `CHROME_BINARY`, and a disposable `CHROME_USER_DATA_DIR`; supply
`SATIP_QR_USERNAME`/`SATIP_QR_PASSWORD` privately for authenticated deployments.
It intercepts YouTube requests at the browser network boundary and supplies
synthetic codes, without changing receiver pairing/playback. Optional
`SATIP_QR_DECODER` points to a reviewed local jsQR decoder for independent
decoding; neither Node nor that test decoder is needed on the Pi.

## Resolver, playback and security

- The pinned resolver is yt-dlp **2026.10.26**, without its optional JavaScript
  runtime/dependencies. Some videos cannot resolve on the current image. A
  compatible test video is `aqz-KE-bpKQ`; availability is not guaranteed.
- Only the player owns mpv. Resolution and Lounge traffic are asynchronous.
  Loading failure or a sustained playback stall returns to TV. YouTube loads
  request a three-second initial buffer and twenty-second read-ahead using
  file-local mpv options, leaving TV’s low-latency cache policy unchanged.
  This trades a short startup delay for a cushion against network jitter.
- The current FFmpeg lacks HTTPS. A random-token **127.0.0.1-only** media bridge
  fetches allowlisted Googlevideo HTTPS URLs with verified TLS and preserves
  single byte ranges for seeking. It accepts no arbitrary URL and forwards no
  client Authorization/cookies. TV credentials are cleared for external playback
  and restored before TV resumes. The bridge closes when playback ends.
- Keep codes private: linked phones can control this receiver through YouTube
  even outside your LAN. Do not expose the web UI to the internet. Enable web
  authentication; the API follows the deployment's existing login policy.
- Screen identity is stored in the player state directory as
  `youtube-identity.json` (0600). Preferences are management
  `youtube.json` (0640); transient code/status uses
  `/run/satip-status/youtube-status.json` (0640) on Buildroot.
  Never publish identity files, session tokens, pairing codes or signed media URLs.

## Installation without an image rebuild

Current Buildroot source selects the pinned pure-Python resolver package and
includes the receiver/web assets. Full fresh-image qualification remains incomplete. For an
existing image, deploy the complete current application using the
[supervised application update](operations.md#in-place-development-deployment).
Install the resolver separately: copy `scripts/install_youtube_resolver.py` from the repository
to a maintenance directory on the Pi, then run there:

~~~sh
python3 install_youtube_resolver.py
PYTHONPATH=/opt/satip-youtube python3 -m yt_dlp --version
~~~

The setup script downloads a checksum-pinned wheel over verified HTTPS, installs
only its Python package, sets readable directory/file modes, and refuses to
replace an existing destination. No pip, browser or Node.js is needed on the Pi.
Run as root on the Pi with permission to create `/opt/satip-youtube`.
Do not rerun provisioning or replace private device configuration.

## Live validation

Copy `validation/check_youtube_on_pi.py` from the repository to a maintenance
directory on the Pi. With the supervised player active, YouTube enabled/ready,
and no current YouTube, scan or preview session, run there:

~~~sh
PYTHONPATH=/opt/satip SATIP_RUNTIME_ROOT=/run python3 \
  check_youtube_on_pi.py --confirm-interrupt
~~~

It interrupts TV, starts the test video, checks advancing H.264/AAC playback,
cleared TV request headers, pause/resume/seek and return to TV. It does not start
a second mpv or access physical input. If it fails, use Settings → Disable to
restore TV; do not leave an unattended test running.

For physical acceptance, pair through the actual phone app and observe picture,
sound, phone controls, receiver controls and return to TV. Check cold boot,
network stalls and sustained playback separately. A synthetic Lounge sender and
advancing decoder counters do not establish app compatibility or audible output.
The current-source phone/video matrix, QR app handoff, cold boot and endurance
remain unqualified; see [validation limits](validation.md).
