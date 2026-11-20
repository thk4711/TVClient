#!/bin/sh
set -eu
board=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
images=${1:?images directory required}
# Firmware package stamps do not track edits to these external files. Refresh
# them for every image, including incremental builds with an unchanged defconfig.
python3 - "$board" "$images" <<'PY'
from pathlib import Path
import shutil
import sys
board, images = map(Path, sys.argv[1:])
for name in ("config.txt", "cmdline.txt"):
    source = board / name
    destination = images / "rpi-firmware" / name
    if any(path.is_symlink() for path in (source, destination, destination.parent)):
        raise SystemExit("Linked firmware configuration refused")
    if not source.is_file() or not source.stat().st_size:
        raise SystemExit("Missing firmware configuration source")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(0o644)
    if destination.read_bytes() != source.read_bytes():
        raise SystemExit("Firmware configuration differs from source")
PY
for file in Image bcm2711-rpi-4-b.dtb rootfs.ext4 \
    rpi-firmware/start4.elf rpi-firmware/fixup4.dat \
    rpi-firmware/config.txt rpi-firmware/cmdline.txt \
    rpi-firmware/overlays/disable-bt.dtbo \
    rpi-firmware/overlays/vc4-kms-v3d.dtbo rpi-firmware/overlays/gpio-ir.dtbo; do
    [ -s "$images/$file" ] || { echo "Missing image input: $file" >&2; exit 1; }
done
cp "$board/satip-config.txt" "$images/satip-config.txt"
cp "$BASE_DIR/satip-build.json" "$images/satip-build.json"
# Buildroot owns the build directory; do not delete a shared genimage tree.
temp=$(mktemp -d "$BUILD_DIR/satip-genimage.XXXXXX")
mkdir "$temp/root"
mkdir "$temp/root/data"
printf 'satip-fresh-data-v1\n' > "$temp/root/data/.satip-factory-empty"
trap 'rm -rf "$temp"' EXIT HUP INT TERM
"$HOST_DIR/bin/genimage" --rootpath "$temp/root" --tmppath "$temp/work" \
    --inputpath "$images" --outputpath "$images" --config "$board/genimage.cfg"
(cd "$images" && sha256sum sdcard.img rootfs.ext4 Image bcm2711-rpi-4-b.dtb satip-build.json > SHA256SUMS)
