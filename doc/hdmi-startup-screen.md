# HDMI startup screen

**Audience: developers and advanced administrators.**

[Documentation index](README.md) · [Boot process](boot-process.md)

The Buildroot image displays a dark background, SAT-IP logo, reception icon and
progress bar while the receiver starts. Messages default to English.
Choose English or Deutsch in
[Settings → TV on-screen display](device-settings.md#tv-on-screen-display) for
both the startup screen and TV overlays. The early screen uses English until
the saved language is readable, then follows that choice through setup, loading,
and error states. The player uses the same language after the HDMI handoff.

## Startup sequence

1. Firmware hides the rainbow screen. Kernel messages go to ttyAMA0; the Linux
   logo and HDMI text cursor are hidden.
2. DRM, VC4 and their audio/firmware dependencies are built into the kernel.
   V3D, used for later GPU playback, remains a module.
3. Immediately after mount -a, BusyBox starts /usr/sbin/satip-boot-splash in the
   background. A worker prepares HDMI EDID while rcS/udev supplies dependencies,
   then starts the native renderer. EDID preparation uses the renderer's handoff
   lock and takes at most five seconds. An independent monitor tracks readiness.
4. The renderer identifies the VC4 KMS card by driver name, not a fixed card
   number. It draws into two DRM buffers at no more than ten updates per second.
5. Before launching the unprivileged player, its supervisor requests release.
   The renderer releases DRM control and its virtual-terminal descriptor while
   leaving its last framebuffer on screen. A process lock confirms release; a
   completion marker prevents a late splash restart.
6. The same renderer generates the player's mpv startup overlay in
   /run/satip-player, using bitmap layer 7 and a small progress strip on layer 8.
   Once the requested channel's playback position advances and the expected audio
   is selected, it briefly shows a full bar and “Ready”, then reveals TV.

The native renderer needs neither Python nor network readiness. The EDID helper
and status monitor use Python; EDID failure does not block rendering. Both splash
paths use FreeType and the installed fonts, without a desktop or display server.

## Progress and warnings

The bar shows startup milestones, **not remaining time**. It moves smoothly to
each observed milestone and keeps an activity highlight moving while waiting,
including during channel loading in mpv. Time alone never fills the bar.

| Milestone | Bar |
|---|---:|
| Linux / settings preparation | 8% / 24% |
| Network association / DHCP | 40% / 48% |
| Local TV service starting | 56% |
| SAT-IP connection / tuner registration | 64% / 72% |
| TV player starting | 80% |
| Channel requested / checking picture and sound | 86% / 94% |
| Playback confirmed | 100% |

Fast phases may be skipped. Progress retains the highest reached milestone
through retries and the player handoff. Warnings freeze the current fill;
recovery resumes it. Fresh setup reaches 100% when the browser setup interface
is ready, starting a separate progress sequence when TV is configured.

After a hard power cut, the external tuner may retain the previous RTSP session
until it expires. During the first minute of the player lifetime, before any
confirmed playback, reception failures (`load_failed` / `load_timeout`) retry with
waits of **1 s, 2 s, then at most 3 s**. The existing load deadlines still apply:
this does not cancel an in-flight SAT-IP SETUP. Channel changes do not extend the
one-minute window. Once that window expires, waits back off normally up to 30 s,
and an actionable playback warning replaces the waiting screen.

The waiting artwork reuses the installed renderer's localized “Connecting to
SAT-IP server …” state, without alternating it with “Reloading channel”. If the
artwork is unavailable, a localized “Waiting for SAT-IP tuner …” text is shown.
Other failure reasons retain their diagnostics and normal backoff. After first
confirmed playback, ordinary channel switching and recovery are unchanged.

The final full bar stays visible for roughly half a second. Completion runs
asynchronously, so input and playback monitoring continue; a new channel or
browser takeover dismisses it. The player refreshes only a narrow bitmap strip
at up to five updates per second, using the same native drawing code as the
early screen. State changes and the 100% frame are logged with Linux uptime.

| Phase or condition | Meaning / action |
|---|---|
| System, data, backend or first channel starting | Wait while the receiver starts and loads settings and TV |
| Wi-Fi association | Waiting for the wireless connection |
| Connected network without an address | Waiting for DHCP |
| No active network link | Check Ethernet or Wi-Fi |
| SAT-IP connection or tuner registration | Waiting for the external server or its frontend |
| Fresh installation ready for setup | Open the displayed browser address |
| Invalid boot configuration | Correct the indicated line in satip-config.txt and reboot |
| Data partition unavailable | Check the partition, free space and write access |
| Initialization not authorized | Factory-empty image marker is required; preserve existing data and check the image/data partition |
| Saved-data preparation failure | Check the error code and preparation logs |
| SSH/console setup failure | Check recovery provisioning and its logs |
| Imported, unqualified data | Follow the import qualification procedure |
| Backend or web timeout | Read the indicated service log |
| Network timeout | Check the LAN, router/DHCP and Wi-Fi configuration |
| Player timeout or sustained playback failure | Check the player log and HDMI configuration |
| Unreadable startup state or unresponsive monitor | Check the splash monitor log |
| First-channel reception retries | A steady “Connecting to SAT-IP server …” screen remains visible while the tuner becomes available |
| Sustained server or tuner failure | Check server reachability and Tvheadend frontend registration |

The browser address appears only after carrier, current-boot DHCP confirmation
matching the actual interface address, and the web port are available. It is
accompanied by a QR code encoding the same URL and instructions for a phone or
computer on the same home network. First setup requires no password.

If Ethernet is unavailable, HDMI offers connecting an Ethernet cable or a USB
keyboard. With a keyboard, select country/layout, scan and choose an SSID, enter
the masked Wi-Fi password, and wait for association and DHCP verification.
Ethernet hotplug is checked throughout. See [first boot](buildroot-first-boot-setup.md).
Wi-Fi passwords are never included in renderer commands or logs. Monitoring reads installation state
and checks local ports; it does not scan or reserve a tuner.

### Errors and timeouts

Configuration errors are separate from storage failures. The screen identifies
missing equals signs, unknown/duplicate keys, empty or unsupported values,
incomplete Wi-Fi fields and competing root-password fields. Line numbers start
at 1 and include comments and blank lines. Passwords and field values are omitted.

On an error or timeout, the bar turns yellow and stops animating. Backend and
web phases have 30-second readiness deadlines; data preparation, provisioning,
network and player phases have 60 seconds each. Each deadline starts on entry
to that phase. Late DHCP does not consume the next service's allowance. The
screen recovers when readiness arrives; browser setup has no deadline.

The native watchdog detects an absent or frozen Python monitor without replacing
an already displayed specific error. It cannot report failures before Linux/HDMI
initialization or during a complete system freeze. A timeout reports missing
readiness, not proof of a crashed process or defective SD card.

## HDMI selection and handoff

Initial defaults are HDMI-A-2 and 1920x1080@50. The monitor first reads explicit
boot preferences, then uses the prepared player configuration. Supported
preferences are listed in [provisioning](provisioning.md#allowlisted-preferences).

The early renderer uses kernel-advertised progressive modes up to 1920×1080,
choosing a suitable reported mode if the requested mode is unavailable. If only
the other HDMI port is connected, both splash and player select that port.
The standard vc4hdmi0/vc4hdmi1 ALSA audio route follows that choice when it matches
the original port; custom audio routes are preserved. For 1080p playback, the
player tries the preferred 50/60 Hz rate, then the other supported 1080p rate.
It does not automatically reduce resolution. Explicit 720p50 requires real
kernel/display support and is not provided by the fallback EDID.

Before rendering, a root helper checks each connected VC4 port. Valid real EDID
with progressive 1080p50 or 1080p60 is retained, including its audio capabilities.
Only missing/invalid EDID or the absence of both usable rates triggers
/lib/firmware/edid/satip-1080p.bin for the affected port. This fallback declares
1080p50/60 and stereo 48 kHz/16-bit PCM, assuming display/receiver support.
Disconnected ports are not forced connected. Explicit administrator
DRM firmware overrides are preserved. No permanent override is added to
cmdline.txt; preparation failure does not block TV startup.

EDID selection runs **only at boot**. Reboot after changing the display/receiver
chain to reconsider EDID. Connector/rate selection occurs at each mpv start.
Do not rerun the root HDMI helper while the renderer or player owns DRM.
Before handoff, the early renderer retries display discovery in the background;
after handoff it no longer intervenes.

### Retaining the picture during handoff

On the Pi image's Linux 6.12 kernel, `DRM_IOCTL_MODE_CLOSEFB` lets the renderer
release its framebuffer references without disabling the active picture. The
supervisor briefly holds a separate **non-master**, close-on-exec DRM descriptor
so Linux does not restore the console mode when the renderer exits. It closes
this descriptor ten seconds after launching the player, or when that generation
ends. The player receives no root descriptor, and the renderer's original
release lock and deadline still apply.

mpv starts without a window, receives the splash overlay, then opens its display
immediately. It draws on a VC4 hardware overlay plane above the retained primary
picture. Its initially transparent EGL buffer therefore reveals the existing
splash; the first opaque frame already contains the new splash. This avoids both
the console-mode detour and the blank initialization frame when display modes
match. The retained background uses one primary plane and roughly 8 MiB at 1080p
until the display is reset; it is covered by the player's opaque output. The
appliance's copy-decoding paths do not use a separate DRM-prime video plane.

Explicit stops, signals and console switching retain the restoring cleanup. If
the kernel cannot retain a framebuffer or the supervisor cannot hold the device,
startup falls back to the original restoring handoff. A genuine connector or
resolution/rate change can still make the TV resynchronize. Physical display
timing needs checking on the actual TV; nothing can be displayed before Linux
HDMI initialization and TV synchronization.

## Diagnostics

Run on the Pi:

~~~sh
cat /run/satip-boot-error
cat /run/satip-boot-stage
cat /run/satip-splash.log
cat /run/satip-splash-monitor.log
cat /run/satip-hdmi.log
cat /run/satip-hdmi.json
cat /run/satip-supervisor/player.log
cat /proc/cmdline
~~~

The boot error marker contains fixed codes such as config-duplicate:12 or
data-mount. The stage marker records the last preparation step started. Monitor
timeouts do not require an error marker from the blocked service. Supervisor
startup errors are in /run/satip-supervisor/<service>.supervisor.log, even if
the service itself could not start. Renderer timestamps use monotonic uptime.
UART and tty2 remain recovery paths; switching away from tty1 stops rendering.

The root supervisor waits about three seconds for release. A blocked renderer
causes a supervisor failure rather than competing DRM ownership. An absent or
already stopped renderer does not block playback. Failure to create the optional
mpv overlay does not stop TV.

## Source and tests

- buildroot/external/package/satip-splash/src/: native renderer and non-DRM preview.
- buildroot/external/board/satip/install-splash.py: inittab integration and build checks.
- satip_appliance/splash.py: readiness monitor and confirmed handoff.
- satip_controller/splash.py: matching overlay in mpv.
- tests/test_splash.py: process locks, startup states, failures and playback readiness.
- tests/test_splash_native.py: compiled-renderer pixels, strip alignment, activity
  and the full completion bar. On Linux, set SATIP_SPLASH_RENDERER to the compiled
  executable and run python3 -m unittest tests.test_splash_native. The executable
  needs fonts at its compiled FONTS path; these tests never open DRM.
- tests/splash_handoff_test.c: wrapped DRM calls verify retained-frame cleanup,
  ordinary stop, unsupported-kernel fallback, foreign frames and console switching.
  The native-splash CI job compiles and runs both native test suites.

Generate a preview on a host with a C compiler, FreeType and DejaVu Sans fonts:

~~~sh
src=buildroot/external/package/satip-splash/src
cc -std=c11 -Wall -Wextra -Werror $(pkg-config --cflags freetype2) \
  "$src/preview.c" "$src/render.c" $(pkg-config --libs freetype2) -lm -o preview
./preview /usr/share/fonts/truetype/dejavu 1920 1080 3 splash.ppm
~~~

State 5 previews setup with an example address; state 11 previews a reception
error. Preview addresses are not embedded in the image.

Physical acceptance must cover cold start, actual display timing, both HDMI
ports, 720p/1080p, late connection, fresh setup, missing network/signal, console
switching and handoff with picture and sound. Software tests do not replace
these checks. See [Validation](validation.md) and the [image guide](../buildroot/README.md).
