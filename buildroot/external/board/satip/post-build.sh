#!/bin/sh
set -eu
target=${1:?target directory required}
board=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH='' cd -- "$board/../../../.." && pwd)
"$repo/buildroot/validate-checkout.sh" "$PWD"
python3 "$repo/buildroot/validate-config.py" "$BASE_DIR/.config" \
    "$repo/buildroot/external/configs/satip_rpi4_64_defconfig"
python3 "$board/install-input-map.py" "$target"
python3 "$board/install-hdmi-edid.py" "$target"
python3 "$board/install-splash.py" "$target" "$BASE_DIR"
python3 "$board/install-documentation.py" "$target"
python3 "$board/install-ca-trust.py" "$target"
python3 "$board/validate-media-build.py" "$target" "$BASE_DIR"
python3 "$board/validate-app-build.py" "$target" "$BASE_DIR"
python3 "$repo/buildroot/build-metadata.py" "$repo" "$PWD" "$BASE_DIR"
install -m 0644 "$BASE_DIR/satip-build.json" "$target/etc/satip-build.json"
mkdir -p "$target/boot"
# Reserve tty1 for controlling-terminal playback; keep serial and tty2 recovery.
sed -i '/^tty1::/d' "$target/etc/inittab"
grep -q '^tty2::' "$target/etc/inittab" || \
    printf '\ntty2::respawn:/sbin/getty -L tty2 0 vt100\n' >> "$target/etc/inittab"
# Never install upstream unrestricted first-run backend startup.
rm -f "$target/etc/init.d/S99tvheadend" "$target/etc/default/tvheadend"
for script in S25satip-data S30satip-dev-provision S39satip-device S40network S50dropbear S89satip-watchdog S90satip-services; do
    chmod 0755 "$target/etc/init.d/$script"
done
chmod 0755 "$target/usr/sbin/satip-dev-provision"
chmod 0755 "$target/usr/sbin/satip-device-helper"
grep -Fq '::respawn:/usr/sbin/satip-device-helper' "$target/etc/inittab" || \
    printf '\n::respawn:/usr/sbin/satip-device-helper\n' >> "$target/etc/inittab"
chmod 0755 "$target/usr/sbin/satip-dhcp-hook" \
    "$target/usr/share/udhcpc/default.script.d/50-satip-cache"
# Plaintext root-password provisioning requires the target hashing applet.
set -- "$BASE_DIR"/build/busybox-*/.config
[ "$#" -eq 1 ] && grep -q '^CONFIG_MKPASSWD=y$' "$1" || {
    echo "BusyBox mkpasswd missing; rebuild BusyBox with the current fragment" >&2
    exit 1
}
# Verify watchdog support in the compiled inputs, not just the fragments.
grep -q '^CONFIG_WATCHDOG=y$' "$1" || {
    echo "BusyBox watchdog missing; rebuild with the current fragment" >&2
    exit 1
}
set -- "$BASE_DIR"/build/linux-*/.config
[ "$#" -eq 1 ] && grep -q '^CONFIG_BCM2835_WDT=y$' "$1" && \
    ! grep -q '^CONFIG_WATCHDOG_NOWAYOUT=y$' "$1" || {
    echo "BCM2835 watchdog missing or cannot disarm; review kernel configuration" >&2
    exit 1
}
# Remove only the known upstream runtime link/empty directory. Preparation will
# establish a persistent directory link; failed-data recovery uses explicit RAM keys.
if [ -L "$target/etc/dropbear" ]; then
    [ "$(readlink "$target/etc/dropbear")" = /var/run/dropbear ] || exit 1
    rm "$target/etc/dropbear"
fi
mkdir -p "$target/etc/dropbear"
chmod 0700 "$target/etc/dropbear"
# Reject credentials/host keys accidentally included in the generic overlay.
[ ! -e "$target/root/.ssh/authorized_keys" ] || exit 1
[ -z "$(find "$target/etc/dropbear" -type f -print)" ] || exit 1
rmdir "$target/etc/dropbear"
# Buildroot installs an open-network example, enabling ctrl_interface for our
# selected option. Remove only that exact pinned example; reject custom data.
python3 - "$target/etc/wpa_supplicant.conf" "$PWD/package/wpa_supplicant/wpa_supplicant.conf" <<'PY'
from pathlib import Path
import sys
destination, upstream = map(Path, sys.argv[1:])
if destination.is_symlink():
    raise SystemExit("Linked Wi-Fi configuration refused")
if destination.exists():
    expected = upstream.read_bytes().replace(b"#ctrl_interface", b"ctrl_interface")
    if destination.read_bytes() != expected:
        raise SystemExit("Custom Wi-Fi configuration refused in generic image")
    destination.unlink()
PY
[ ! -e "$target/etc/satip-dev-provisioned" ] || exit 1
