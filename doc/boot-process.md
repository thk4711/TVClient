# Boot process and startup optimization

**Audience: developers and advanced administrators.**

[Documentation index](README.md) · [HDMI splash](hdmi-startup-screen.md)

The appliance reduces time to TV by starting independent work concurrently,
reducing Python initialization, scanning known Wi-Fi frequencies first, and
retrying explicitly failed channel loads promptly. Readiness gates ensure that
playback starts when its network and backend dependencies are usable.

## Configuration summary

| Optimization | Configuration or implementation | Why it helps |
| --- | --- | --- |
| Direct firmware boot | Firmware loads Linux directly; `boot_delay=0` | Avoids an additional bootloader stage and an explicit firmware delay |
| Early asynchronous splash | Splash starts after `mount -a`, before `rcS` | Gives HDMI feedback while device discovery and service preparation continue |
| Reduced serial output | `loglevel=4` in the kernel command line | Avoids sending verbose boot messages through the 115200-baud console |
| Parallel startup | Background DHCP clients and independent supervisors | Local initialization overlaps Wi-Fi association and address acquisition |
| Reduced Python initialization | Deferred asynchronous imports, one service-control interpreter, precompiled bytecode | Starts network preparation and supervisors sooner |
| Targeted first Wi-Fi scan | Learned frequencies passed through `initial_freq_list` | Finds a known access point without first scanning every supported channel |
| Prompt failed-tune retry | Current-load mpv errors and Tvheadend subscription failure feedback | Avoids the full 20-second load deadline after a definitive failure |
| Continuous HDMI handoff | Retain the splash frame and queue the mpv overlay before opening its window | Keeps startup feedback visible through the transition into playback |

## Startup dependencies

The splash worker starts immediately after `mount -a`. HDMI EDID preparation
runs asynchronously alongside udev and is bounded to five seconds. The native
DRM renderer displays progress while data preparation, recovery provisioning
and service initialization proceed.

`S25satip-data` prepares persistent data, `S30satip-dev-provision` applies recovery
settings, and `S39satip-device` restores interrupted Wi-Fi configuration changes.
`S40network` launches background DHCP clients. `S90satip-services` starts the
Tvheadend, web and player supervisors in order, waiting for each supervisor
acknowledgement. There is no global network-online or NTP synchronization wait.

For a configured installation:

1. Tvheadend waits for a route to the saved numeric SAT-IP server address before
   launching with `--satip_xml`. This avoids a failed initial registration while
   Wi-Fi is still connecting.
2. The player waits for the local backend, a network address, the server
   description TCP port, and an active matching SAT-IP server with an enabled
   DVB-S frontend in the authenticated backend API.
3. The supervisor releases display control while retaining the splash frame.
   mpv opens its window with the startup overlay already queued.
4. Advancing playback position and the selected audio track confirm reception.
   The overlay briefly shows completion, then reveals TV.

These startup probes do not reserve a tuner. Fresh, unconfigured setup can start
its local backend and web interface without a route to a saved SAT-IP server.
See [services and persistence](buildroot-services-persistence.md) for data gates
and [HDMI handoff](hdmi-startup-screen.md#retaining-the-picture-during-handoff)
for display ownership.

## Boot settings

The image installs firmware settings from
`buildroot/external/board/satip/config.txt` into `/boot/config.txt`, and kernel
arguments from `buildroot/external/board/satip/cmdline.txt` into `/boot/cmdline.txt`.

| Setting | Purpose |
| --- | --- |
| `boot_delay=0` | Removes the explicit firmware boot delay |
| `disable_splash=1` | Suppresses the firmware rainbow screen; the appliance supplies its own startup display |
| `disable_fw_kms_setup=1` | Lets KMS select the configured HDMI connector and mode without a firmware-injected HDMI-A-1/60 Hz mode |
| `loglevel=4` | Limits console traffic while retaining full kernel messages in `dmesg` |
| `enable_uart=1` | Keeps the UART recovery console available |

CPU clocks and voltage use firmware defaults; the image adds no explicit
`initial_turbo` override. See the [Raspberry Pi clock documentation](https://www.raspberrypi.com/documentation/computers/config_txt.html#overclocking-options).

EEPROM boot order and bootloader diagnostic output belong to the board and are
separate from the SD-image configuration. When investigating startup latency,
check whether the board tries other boot sources before the SD card.

## Python and service initialization

Synchronous boot helpers defer asynchronous imports until asynchronous
functionality is needed. This applies to shared state access, backend requests
and device preparation. Short-lived boot commands therefore avoid loading the
event-loop stack just to read settings or prepare files.

`S90satip-services` calls `satip_appliance.supervisor all start` in one Python
interpreter. It starts Tvheadend, web and player supervisors in order, retaining
each acknowledgement and data gate. `all stop` performs the reverse sequence
with the existing process-group cleanup deadlines. Reusing one control
interpreter reduces repeated imports during startup and shutdown.

`buildroot/external/package/satip-app/satip-app.mk` precompiles application
bytecode with the matching Buildroot host Python using checked-hash invalidation.
The image installs root-owned cache directories as 0755 and bytecode files as
0644 so the non-root services can read them. Python checks the source hash
before using cached bytecode, so source updates invalidate stale caches. For
in-place updates, compile with the device Python as described in
[operations](operations.md#updates).

## Wi-Fi first-scan frequency hints

`satip_appliance/wifi_scan.py` maintains a root-only 0600 cache at
`/data/store/network/wifi-scan.json`. It contains up to three recently successful
frequencies and a SHA-256 fingerprint of `/etc/wpa_supplicant.conf`. A frequency
is learned only after authentication completes and DHCP confirms an address.
Changing the SSID, password, country or another configuration field invalidates
the previous hints.

Before starting wpa_supplicant, `S40network` checks the cache, observer availability
and current radio capabilities. With usable hints, it creates a root-only 0600
copy at `/run/satip-network/wpa-startup.conf`, adding `initial_freq_list`. For
example, learned frequencies of 2462 and 5180 MHz produce:

```ini
initial_freq_list=2462 5180
```

The installed wpa_supplicant 2.12 applies this list only to its first scan. This
reduces the work needed to find a known access point while allowing subsequent
full scans and automatic band selection. `/etc/wpa_supplicant.conf` remains the
canonical configuration. The cache and runtime copy are generated on the device;
the generic image contains no installation-specific frequencies or credentials.

Association retries and stale hints can make a particular boot slower.
Recovery to normal scanning is part of the design; frequency hints do not
guarantee a shorter connection time.

### Normal-scan fallback

- Missing, corrupt or configuration-mismatched caches select the canonical
  configuration directly.
- Explicit administrator `initial_freq_list`, `freq_list` and `scan_freq`
  policies are preserved.
- Frequencies reported disabled by `iw` are excluded. An unavailable radio query
  or absent recovery observer also selects the canonical configuration.
- If wpa_supplicant rejects the generated configuration at launch, the launcher
  retries with the canonical configuration.
- Radio permissions can change as the supplicant applies its country setting.
  Starting three seconds after hint preparation, the observer can request a
  normal `SCAN` when the supplicant is explicitly `DISCONNECTED`. It makes at
  most three attempts, at least one second apart, stopping after acceptance.
  The manual scan bypasses the first-scan hint.

An ongoing scan, association or authentication is allowed to finish. The
three-second recovery threshold applies to an idle failed scan, not to the
time allowed for the whole connection.

## DHCP address acquisition

Ethernet and Wi-Fi each use one background BusyBox DHCP client. Wi-Fi makes ten
one-second attempts with a two-second retry-cycle pause; Ethernet makes three
one-second attempts with a five-second pause. Short retry intervals allow a
client started before its link is ready to acquire an address promptly once
the link becomes usable. Neither client blocks the init chain.

The last DHCP-confirmed IPv4 address is stored separately for each interface in
`/data/store/network/{eth0,wlan0}.json` and requested with `udhcpc -r`. The cache
format is `{"version":2,"ip":"..."}`; version 1 records are accepted as address
hints and replaced after binding. The stock DHCP script remains authoritative
for addresses, routes and DNS, and the cache hook only records confirmed values.
Every boot waits for DHCP confirmation before using an address.

Wi-Fi DHCP uses the BusyBox `-S` option for syslog diagnostics. The image builds
wpa_supplicant without syslog support; its launcher does not use `-s`.

## Prompt recovery from failed channel loads

The controller normally allows 20 seconds to load a channel. Two explicit
failure signals let it retry sooner:

- An mpv `end-file` error matching the current playlist entry.
- A Tvheadend `Bad` subscription state matching the current supervised TV load.

Each load has a random HTTP User-Agent identifier. During that load, the root
player supervisor polls the authenticated subscription API at most every
250 ms with a 200 ms request timeout. Only one matching loopback `HTTP`/`pass`
subscription can produce failure feedback. This catches rejected tunes even
when Tvheadend keeps the HTTP stream open.

The request is bound to the player PID, supervisor generation, per-load token
and monotonic timestamp. The supervisor rereads it after the API call to reject
a cancelled or replaced load, then publishes a small credential-free response
in `/run/satip-tuning/status.json`. The player ignores stale responses and stops
requesting checks once a playback position is available. Slow `Testing` loads,
`Idle` states, packet-error counters and unavailable APIs retain the normal
load deadline. The controller owns retry and backoff.

This removes unnecessary waiting after a definitive failure. Successful playback
still requires the correct stream, advancing position and the selected audio
track. Admin credentials remain in the root supervisor.

## Startup status and diagnostics

The splash distinguishes Wi-Fi association, DHCP, server connection and tuner
registration. A wait exceeding 60 seconds in one phase becomes an actionable
warning and recovers automatically. During the first 60 seconds of the requested
startup channel, a failed load shows a channel-retry message. Later failures
distinguish rejected streams, load deadlines, stalled playback, missing audio
tracks and player/device problems.

The Wi-Fi observer records up to 64 state/frequency observations with monotonic
timestamps, then exits after association plus DHCP confirmation or approximately
90 seconds. Its control queries have a 200 ms timeout and independent reply
sockets. An `unavailable` sample means a query failed or timed out; it does not
by itself establish a link loss. Public diagnostics contain no SSID, BSSID or
credentials.

Read-only diagnostic files on the Pi:

```sh
cat /run/satip-splash.log
cat /run/satip-reception-stage
cat /run/satip-network/wifi-startup.json
cat /run/satip-network/wifi-scan.json
cat /run/satip-tuning/status.json
cat /run/satip-supervisor/tvheadend.supervisor.log
cat /run/satip-supervisor/player.supervisor.log
cat /run/satip-supervisor/player.log
grep udhcpc /var/log/messages
cat /proc/cmdline
```

`wifi-scan.json` reports the selected scan mode and frequencies;
`wifi-startup.json` reports connection progress and any explicit normal-scan
request. A missing tune-feedback file can simply mean no failure was reported.

Boot-stage timestamps measure seconds since Linux started. Monotonic time keeps
them valid when NTP adjusts the wall clock. Power-on-to-visible-picture timing
also includes firmware loading and display latency. Total startup remains
dependent on Wi-Fi conditions and tuner availability; stage improvements do not
establish a fixed cold-start time. See [validation](validation.md) for physical
acceptance and [operations](operations.md#updates) for deployment and rollback.
