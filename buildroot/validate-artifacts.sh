#!/bin/sh
set -eu
images=${1:?images directory required}
for file in sdcard.img rootfs.ext4 Image bcm2711-rpi-4-b.dtb satip-build.json SHA256SUMS; do
    [ -s "$images/$file" ] || { echo "Missing artifact: $file" >&2; exit 1; }
done
(cd "$images" && sha256sum -c SHA256SUMS)
echo "Development installation image: $images/sdcard.img"
echo "Development only: packaged services and separate data; no A/B, qualified migration or SWU."
