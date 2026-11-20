# SAT-IP Pi 4 development image

**Audience: developers and advanced administrators.**

This Buildroot profile builds an AArch64/glibc SD image for the Raspberry Pi 4.
It includes BusyBox init, eudev, VC4 KMS/V3D, mpv/FFmpeg, Tvheadend, Python
application services, self-contained first-boot setup, and an early HDMI splash.
It is a **development installation image**, not a qualified release or a
data-preserving firmware update. See [validation](../doc/validation.md).

## Image profile

- Direct Pi firmware boot; 128 MiB FAT, 768 MiB writable ext4 root, 256 MiB ext4
  data labelled `satip-data`. No automatic expansion, U-Boot/A/B, or SWUpdate.
- Separate non-root backend, player and web roles, supervised by root-owned code.
- H.264 `v4l2m2m-copy` using the included Raspberry Pi FFmpeg M2M backport;
  software MPEG-2 SD/BWDIF and exclusive HLS browser preview.
- Python XML/pyexpat and zlib runtime modules required by discovery and HTTPX.
- Ethernet DHCP, optional WPA2 Wi-Fi, chrony, and opt-in Dropbear recovery access.
- SAT-IP client and IPTV inputs for experimental [Enigma2 reception](../doc/enigma2.md);
  Tvheadend SAT-IP server and descrambling remain disabled.
- No embedded credentials, SSH identity, private network settings, or channel library.

[First boot](../doc/buildroot-first-boot-setup.md) explains setup and preferences;
[services/data](../doc/buildroot-services-persistence.md) explains lifecycle,
import and backup boundaries. The [HDMI splash](../doc/hdmi-startup-screen.md)
guide describes startup status and diagnostics.

## Native amd64 Debian build

Full compilation requires Linux x86_64/amd64 Debian. macOS can orchestrate remote
builds and configuration-only checks, but cannot compile this image locally.
Review/install prerequisites on the **build host**, not the television Pi:

~~~sh
sudo apt-get install build-essential bc bison flex libncurses-dev libssl-dev \
  rsync cpio unzip wget file git python3 gawk perl patch gzip bzip2 xz-utils \
  tar ca-certificates openssh-client
~~~

Use a non-root build account and Python 3.11 or newer for host-side helpers.
Record the exact Debian release, installed package versions, compiler, GNU make
and Python versions for each candidate. There is no qualified, pinned builder
image yet; the Linux/amd64/Debian preflight is not a host-version qualification.

Allow substantial disk/RAM and internet access for toolchain, kernel, Rust and
media dependencies. Minimum RAM/disk requirements and build duration have not
been measured for the current source. Check free space before starting, monitor
the first build, and reduce `--jobs` if memory is constrained. Record measurements
with the builder details rather than treating the default 16 jobs as a minimum.
The helper does not install packages, flash cards, or broadly clean outputs.

From a reviewed source root:

~~~sh
./buildroot/build.sh --configure-only
./buildroot/build.sh --jobs 16
~~~

Defaults: `~/embedded/satip/buildroot`, `~/embedded/satip/output/rpi4-64`,
shared downloads at `~/embedded/dl`. Override with absolute `BUILDROOT_DIR`,
`SATIP_OUTPUT_DIR` and `BR2_DL_DIR` environment variables.

The helper clones a missing checkout at Buildroot tag `2026.05.2` and verifies
peeled commit `72d9d4fa636a371ef9eb99c92a735ce9f6d829d5`. It requires clean
upstream source and never resets/deletes an existing checkout. It refuses
nonempty outputs it does not own. Repeat builds validate existing configuration
rather than overwrite interactive changes. A changed defconfig signature or
incompatible configuration requires a **fresh output directory**. Only the local
application and native splash package build trees are refreshed to avoid stale
source/assets. Application installation replaces its two owned package directories
and validates their exact source inventory, including rejection of obsolete files.
Documentation installation replaces `/usr/share/doc/satip` with the current public
inventory, including the [examples guide](../examples/README.md) when present.
Image assembly refreshes firmware `config.txt` and `cmdline.txt` from board sources
on every run, so firmware package stamps cannot retain old boot settings.

The default-enabled [stage-three Tvheadend idle-mux patch](../doc/channel-switching.md#same-transponder-mux-grace)
is a native package input. Image validation verifies its patched source hashes
and compiled objects. It requires a **fresh output**, even if the application
can otherwise be refreshed incrementally. SAT-IP frontends default to 1000 ms;
explicit saved zero remains a diagnostic override, and other input types are unchanged.

Other changed inputs, such as kernel/BusyBox fragments, third-party recipes or
patches, still require a fresh output; their Buildroot stamps are not a complete
content-based dependency graph. Always use a fresh output for a release candidate.
If initial configuration failed before `.config` was created, rerunning retries
initialization in the matching owned output. An existing `.config`, including an
invalid or partial one, is preserved: review it or choose a fresh output.

`--configure-only` uses `HOSTARCH=x86_64` for intended-host Kconfig checks;
this does not enable image compilation on an ARM Mac.

## Build, collect and verify with Python

The stdlib-only wrapper supports native and remote builds using the same helper:

~~~sh
# Adapt host and absolute paths for your build machine.
python3 scripts/build_image.py --execution remote \
  --host BUILD_USER@BUILD_HOST --remote-root /home/BUILD_USER/embedded/satip \
  --output-dir /home/BUILD_USER/embedded/satip/output/rpi4-64-current --jobs 16

# Resolve settings/input inventory without contacting a host or writing files.
python3 scripts/build_image.py --config scripts/build-image.ini.example --dry-run

# On native amd64 Debian, local execution is the default.
python3 scripts/build_image.py --jobs 16 --no-zip
~~~

Copy optional settings from `scripts/build-image.ini.example` to the ignored
`scripts/build-image.ini` and adapt them. CLI settings override INI values.
Remote SSH is non-interactive, keeps host-key verification, and defaults to port 22;
use `--ssh-port` for your route. Default jobs: 16.
The wrapper accepts `install_host_packages = false` (the default). Set it to
`true` or pass `--install-host-packages` to run `apt-get update` and install the
prerequisites listed above on the selected Debian amd64 **build host**, not the Pi
or the orchestration Mac. `--no-apt` overrides an enabled INI setting. Installation
requires root or non-interactive sudo (`sudo -n`); unavailable privileges or apt
failures stop the build. Compilation should still use a non-root account. The
build host must already have Python 3.11+ to run preflight checks. Dry runs report
the setting but never contact the host or install packages. The direct shell
helper remains package-installation-free.
The orchestration computer also needs Python 3.11+, rsync and an SSH client.
Establish the build account's authorized key and verify the build host fingerprint
through your normal trusted channel before invoking the wrapper. Check that
`ssh BUILD_USER@BUILD_HOST true` succeeds without interactive authentication.

`buildroot_dir`, `output_dir`, `download_cache`, and `artifacts_dir` are overridable.
The first three are build-host paths; artifacts are always local. Source, checkout,
output and cache must not overlap. Managed paths reject whitespace, colons,
traversal and symlinked ancestors.

Remote staging uses `<remote_root>/source` and an explicit rsync input allowlist,
not the whole repository. Never stage `.baseline-private/`, credentials, backups,
or captures. Input symlinks and private provisioning identity are rejected.
Staged hashes and produced metadata must match the exact input inventory.
Source ownership is bound to the originating repository path.

Unmarked nonempty staging is refused. `--adopt-source` permits one-time adoption
only when every reviewed input hash matches and there are no foreign files
(apart from tolerated caches/README). Removed inputs cause refusal, not remote
deletion: choose a fresh source/output pair. Configuration changes require a
fresh output even when normal builds are incremental.

Source/output locks prevent concurrent wrappers. Direct shell-helper calls do
not participate: do not run them concurrently. Crashes/failed remote builds retain
locks conservatively. Inspect the retained log and prove no build is running
before removing only the matching token file and empty lock directory.

## Artifacts and verification

Each run retains a unique `artifacts/satip-foundation-<UTC time>-<id>/` directory.
Existing artifacts are never overwritten. A completed set contains:

- Exactly one SD image: `sdcard.img.zip` with `zip_image = true` / `--zip`
  (default), or `sdcard.img` with `zip_image = false` / `--no-zip`.
- Standalone `rootfs.ext4`, `Image`, `bcm2711-rpi-4-b.dtb`, `satip-build.json`,
  and `SHA256SUMS` covering only the collected artifacts. `BUILD_SHA256SUMS`
  preserves the original build manifest, including the raw SD-image digest.
- `build.log`, `source-inputs.json` and `resolved.config`. Remote logs also remain
  under `<remote_root>/logs/`.
- ZIP mode also includes `ZIP_SHA256SUMS`. Member inventory, CRC and raw image
  digest are checked by streaming the archive without extracting a second image.
  Remote builds compress on the build host and transfer only the ZIP. Successful
  transfers remove the run-specific temporary remote ZIP files; interrupted runs
  may leave them under `<output_dir>/.satip-zip-<time>-<id>/`. The raw image remains
  in Buildroot's build-host output, not in the local ZIP artifact directory.
- `VERIFIED.json`, written last after checksums, source/configuration and ZIP checks.
  Missing this marker means the set is not successfully verified.

Hooks validate the pinned checkout, configuration, public provisioning template,
application/static hashes, Python dependencies and compiled media/backend features.
The FFmpeg validator requires matching backport/source hashes, compiled objects,
and the installed codec ELF; an enabled decoder flag alone is insufficient.
Metadata is also installed at `/etc/satip-build.json` and
`/etc/satip-media-build.json`. All builds are `release=false`; unknown/unborn/dirty
Git identity is not presented as a release commit.
The Python wrapper carries the originating repository's commit and dirty state
as public metadata bound to the staged input digest, without copying `.git` or
private files. Artifact verification checks that identity and rejects a changed
source/Git identity during the build. Direct shell builds use their local checkout
identity instead. Commit reviewed changes before building for traceable provenance.

Do not reuse an artifact solely because it exists: compare its manifest against
current inputs. Verification does not prove boot, tuner, picture, or sound.
The Python wheel/sdist are application distributions; neither is the complete
image source. For binary publication, follow [Releasing](../doc/releasing.md),
including corresponding sources, third-party notices and fresh-card acceptance.

### Reproducibility boundary

Pinned downloads and source hashes provide traceability. Byte-identical clean
builds have not been demonstrated: this profile does not enable
`BR2_REPRODUCIBLE`, and filesystem/archive metadata and builder paths may vary.
Do not describe checksum verification as reproducible-build proof. The release
guide describes the remaining builder/epoch/two-build work.

Never run `make legal-info` against a private development checkout. At the pinned
Buildroot revision, local-package source archiving uses the package's source root
without its normal rsync allowlist. `satip-app` uses the repository root, so ignored
private files and generated outputs could enter that archive. Collect legal/source
material from the isolated public checkout described in the release guide.

## Provision the flashed card

Manually confirm the destination before flashing **only the test card**. A whole
image flash replaces data. Keep the working card and private backups untouched.
For a published image, obtain checksums and their signature/attestation from the
same release and verify them as described in [Releasing](../doc/releasing.md#recipient-verification).
For your own build, require `VERIFIED.json` and check `SHA256SUMS` in its artifact
directory. In ZIP mode, extract the image for flashing; its raw digest is recorded
in `BUILD_SHA256SUMS`. Then use Raspberry Pi Imager's **Use custom**
image option to select `sdcard.img`; select the test SD card and enable write
verification. Use the project's FAT provisioning file rather than Imager's OS
customization options. Eject/reinsert the card if needed to mount its FAT partition.
No card edits are required for normal setup. The image's factory-empty data
initializes automatically. Ethernet uses DHCP; Wi-Fi can be configured using a
USB keyboard on HDMI. Open the displayed URL or scan its QR code. First setup is
password-free. See [first boot](../doc/buildroot-first-boot-setup.md).

For optional development recovery access, edit the flashed card's
satip-config.txt (never the source template):

~~~ini
ssh_enabled=yes
root_password=your-root-password
~~~

SSH is off by default on a new card. Root credentials are separate from the web
interface. Setting a web password in Settings enables browser authentication
(username satip). The boot file supports only hostname, timezone, ssh_enabled,
root_password, wifi_country, wifi_ssid and wifi_psk. Obsolete options are rejected.
Data initializes only with the image's factory-empty marker; unmarked or foreign
data is never initialized automatically.

Boot preferences are allowlisted literal key=value lines, with no shell expansion
or inline comments. Wi-Fi SSID/password spaces are preserved; other values are
trimmed. The boot file is cleaned up automatically after successful import. This
retains saved settings and credentials and is not secure flash erasure.

## Automatic service recovery

The image includes controller progress checks, bounded local web/backend health
probes, exact-generation restarts, a hardware watchdog and bounded persistent
recovery facts. See [automatic recovery](../doc/recovery.md) for deadlines, safe
diagnostics and limitations. These changes were tested in place on the existing
Pi; fresh-image and deliberate hardware-reset acceptance remain open.

## First boot and recovery

Arrange a viewing interruption before swapping cards. Keep the working card intact.
The image disables Bluetooth and dedicates PL011 on GPIO14/15 to `ttyAMA0`,
115200 8N1. With Pi power off, connect a **3.3 V TTL** adapter: ground and crossed
TX/RX, without its power pin. Never use 5 V/RS-232 or disturb GPIO23 IR wiring.

1. Check UART/console login and provisioned `ssh root@APPLIANCE_IP` recovery.
   Verify host fingerprints normally; failed/uninitialized data uses ephemeral RAM
   host keys. An existing forwarding endpoint may not follow the new image.
2. Check `uname -m`, `/etc/satip-build.json`, mounts and writable data.
3. Inspect `/dev/dri/by-path`, `modetest`, `aplay -l`, `v4l2-ctl --list-devices`,
   and selected keyboard/GPIO nodes/permissions. Enumeration is not output acceptance.
4. Open `http://APPLIANCE_IP:8080/` and follow [first setup](../doc/buildroot-first-boot-setup.md).
5. Check physical picture/audio, controls, OSD, exclusive preview, and graceful reboot.
6. If recovery fails, power down and restore the preserved working card; do not
   repair a test image by overwriting working-card data.

No boot-failure tests before recovery works. No guaranteed faster power-to-picture
claim. Physical and fresh-image acceptance limits are in [validation](../doc/validation.md).

## Local validation

~~~sh
python3 -m unittest discover -s tests -p 'test_buildroot*.py' -v
python3 -m unittest discover -s tests -p 'test_build_image.py' -v
python3 -m unittest discover -s tests -p 'test_source_manifest.py' -v
python3 -m unittest discover -s tests -p 'test_appliance.py' -v
python3 -m unittest discover -s validation -p 'test_buildroot_media.py' -v
~~~

Fake roots exercise parser/secret modes, retry, symlink refusal, configuration,
staging, metadata and ownership guards. Synthetic [media tools](../validation/README.md)
exercise codecs on local fixtures, not a tuner or HDMI. Run the full suites in
[development](../doc/development.md) for broader coverage.
