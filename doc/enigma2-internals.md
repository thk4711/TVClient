# Enigma2 internals

**Audience: developers and advanced administrators.**

[User source guide](enigma2.md) · [Architecture](architecture.md) · [API](api.md#source-discovery-and-bouquet-import-appliance)

Tvheadend remains the common backend: the importer creates restricted raw HTTP
IPTV inputs. Source discovery and bouquet listing do not tune; import probes
streams under the same exclusive ownership policy as SAT-IP scanning.

## Inspect and import on the Pi

These commands run on the Pi, not on the Enigma2 receiver. Substitute the actual
source address; no private device identity is included in future images.

~~~sh
export PYTHONPATH=/opt/satip SATIP_RUNTIME_ROOT=/run
python3 -m satip_appliance.tv_source discover --address ENIGMA2_IP

# Read-only preview: no tuning or persistent changes.
python3 -m satip_appliance.tv_source import --address ENIGMA2_IP

# Probe both free bouquets, interrupting Pi HDMI/browser playback.
# The previous source resumes afterward; the VU+ local channel is unchanged.
python3 -m satip_appliance.tv_source import --address ENIGMA2_IP --apply

# Select the imported source; browser/HDMI services restart in place.
python3 -m satip_appliance.tv_source select enigma2 --apply

# Return to the retained SAT-IP lineup without rescanning.
python3 -m satip_appliance.tv_source select satip --apply
~~~

For a short proof, add repeated `--channel` options, for example:

~~~sh
python3 -m satip_appliance.tv_source import --address ENIGMA2_IP \
  --channel 'Das Erste HD' --channel 'ZDF HD' --apply
~~~

Use repeated `--bouquet` options for explicitly selected free bouquets;
`--web-port` and `--stream-port` override port defaults. Per-channel probe
timeout defaults to 20 seconds and accepts 5–90 seconds. A full import can
take many minutes when old bouquet entries no longer broadcast. Repeating an
import reuses the network/mux identities and retains matching favourites.
The importer changes only its managed network, not the Enigma2 database.
Targeted imports using `--channel` retain the current catalogue/favourite order
and append the requested channels. Every explicitly requested channel must pass
validation before the updated lineup is published; otherwise the old lineup is
retained. For the VU+ Favourites bouquet, for example:

~~~sh
python3 -m satip_appliance.tv_source import --address ENIGMA2_IP \
  --bouquet 'Favourites (TV)' --channel 'RTL Television' --channel 'SAT.1' \
  --timeout 30 --apply
~~~

## Persistence, EPG and recovery

Source profiles are retained in `/data/store/web/tv-source-satip.json` and
`tv-source-enigma2.json`. They hold separate channel libraries/catalogues and
actual backend identities. The active network remains in the generated web
configuration. Selection enables only the chosen managed network; the existing
SAT-IP frontend configuration is preserved. Imported IPTV networks permit one
stream and disable idle/startup scanning. The existing player, preview and
time-shift pipeline always uses restricted loopback Tvheadend streaming.

OpenWebif EPG is a fallback when the per-service stream omits broadcast EIT.
Bounded read-only queries fill the existing secret-free programme cache without
opening another tuner stream. Queries are throttled per channel, and outages
must not stop HDMI playback.

Source selection journals the previous library/configuration and network enable
flags. On an error it restores them. Normal all-service startup recovers retained
source/Enigma2 scan journals after Tvheadend starts and before web/player startup. To recover an interrupted selection, run:

~~~sh
PYTHONPATH=/opt/satip SATIP_RUNTIME_ROOT=/run \
  python3 -m satip_appliance.tv_source recover
~~~

Do not delete scan journals or edit live Tvheadend database files. Stop preview,
search, configuration restoration and Wi-Fi trials before importing/selecting.
Root source operations stop the owned HDMI/web services and watchdog feeder,
retain Tvheadend for API work, then restart the services and feeder.

Configuration backups include both source profiles and strictly allowlisted
IPTV network records. Restore rejects arbitrary IPTV/pipe URLs, custom headers,
environments and unmanaged networks. Channel-only backups stay bound to the
active source's exact network identity and preserve source/device settings.

## Live startup buffering

Live channel loads hold output paused while selecting verified normal German
and capability-aware surround audio. File-local options are `cache=yes`,
`cache-pause=yes`, `cache-pause-initial=yes` and `cache-pause-wait=1`.
The explicit startup gate additionally requires at least one finite second of
selected-stream demuxer cache. Missing/invalid/insufficient readings keep polling
within the existing bounded load deadline; track discovery alone cannot open it.

The policy applies to both SAT-IP and Enigma2, direct and RAM-relay live loads.
Time-shift reader handovers and YouTube keep their own options. It can delay first
picture to avoid an immediate audio-cache refill pause; it does not guarantee
uninterrupted playback during later network/source outages or fix video rendering
drops. Physical picture, sound, lip sync and endurance need separate acceptance.
See [buffering checks](../validation/README.md#short-pauses-after-channel-switches)
and [advanced troubleshooting](troubleshooting.md#brief-freeze-after-changing-channel).

## Build and in-place deployment

The image profile enables `BR2_PACKAGE_TVHEADEND_IPTV=y`; compiled-feature
validation requires IPTV as well as SAT-IP client, with SAT-IP server disabled.
Use a fresh Buildroot output for the changed profile. For an existing Pi, build
only the matching pinned AArch64 backend with the current native patch, then:

~~~sh
python3 scripts/deploy_appliance.py --host PI_IP --apply \
  --tvheadend-binary /absolute/path/to/matching/tvheadend
~~~

This deploys the repository's application and public documentation alongside
the backend, retains a private rollback archive and verifies file hashes. It
interrupts TV, not networking, and does not reflash or reprovision device data.
The deployer checks ELF architecture and executes the staged backend on the Pi
before replacing the working binary. This catches missing target-library symbols
(such as OpenSSL engine APIs). The standalone test build requires
`-DOPENSSL_NO_ENGINE` to match the Pi library; Buildroot uses its target headers.
The deployer does not check provenance: verify the pinned source,
patch manifest and build feature definitions before supplying a binary.

## Qualification

Use [Validation](validation.md) for present limits and the
[validation tools](../validation/README.md) for browser/CLI checks. Repeat direct
Enigma2 first setup, cancellation/retry, selected bouquet scope, source activation,
favourite preservation, reboot recovery and supported HD/SD playback. Software
readiness and a successful HTTP request do not establish physical HDMI acceptance.
