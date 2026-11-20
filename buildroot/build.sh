#!/bin/sh
# Development foundation only. No apt, flashing, remote staging or cleanup.
set -eu
script=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH='' cd -- "$script/.." && pwd)
checkout=${BUILDROOT_DIR:-$HOME/embedded/satip/buildroot}
output=${SATIP_OUTPUT_DIR:-$HOME/embedded/satip/output/rpi4-64}
cache=${BR2_DL_DIR:-$HOME/embedded/dl}
configure_only=0
jobs=${JOBS:-}
while [ $# -gt 0 ]; do
    case "$1" in
        --configure-only) configure_only=1; shift ;;
        --jobs) jobs=${2:?jobs required}; shift 2 ;;
        --help|-h)
            echo "Usage: $0 [--configure-only] [--jobs N]"
            echo "Paths: BUILDROOT_DIR, SATIP_OUTPUT_DIR, BR2_DL_DIR (all absolute)"
            echo "Native amd64 Debian build; does not install packages or clean output."
            exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done
for path in "$checkout" "$output" "$cache" "$repo"; do
    case "$path" in /*) ;; *) echo "Build paths must be absolute" >&2; exit 1 ;; esac
    case "$path" in *' '*|*':'*) echo "Buildroot paths may not contain spaces or colons" >&2; exit 1 ;; esac
done
if [ "$configure_only" -eq 0 ]; then
    [ "$(uname -s):$(uname -m)" = Linux:x86_64 ] || {
        echo "Full builds require native amd64 Linux (Debian); use --configure-only for configuration checks" >&2
        exit 1
    }
    [ -f /etc/debian_version ] || { echo "Expected Debian build host" >&2; exit 1; }
    for tool in gcc g++ make git python3 rsync cpio unzip wget file bc bison flex perl tar gzip patch; do
        command -v "$tool" >/dev/null || { echo "Missing host tool: $tool" >&2; exit 1; }
    done
fi
[ -n "$jobs" ] || jobs=$(getconf _NPROCESSORS_ONLN)
case "$jobs" in ''|*[!0-9]*|0) echo "Invalid job count" >&2; exit 1 ;; esac
# Clone only a missing dedicated checkout; never reset someone else's tree.
if [ ! -e "$checkout" ]; then
    mkdir -p "$(dirname -- "$checkout")"
    git clone --depth 1 --branch 2026.05.2 \
        https://gitlab.com/buildroot.org/buildroot.git "$checkout"
fi
"$script/validate-checkout.sh" "$checkout"
defconfig=$script/external/configs/satip_rpi4_64_defconfig
signature=$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$defconfig")
# Refuse all unowned/nonempty output trees, especially PiSonic's configured output.
if [ -e "$output" ] && [ ! -f "$output/.satip-foundation-output" ]; then
    [ -d "$output" ] && [ -z "$(ls -A "$output")" ] || {
        echo "Refusing nonempty/unowned SAT-IP output directory" >&2; exit 1;
    }
fi
mkdir -p "$output" "$cache"
if [ -f "$output/.satip-foundation-output" ]; then
    [ "$(cat "$output/.satip-foundation-output")" = "$repo:$signature" ] || {
        echo "Output belongs to another tree/config; choose a fresh SATIP_OUTPUT_DIR" >&2; exit 1;
    }
else
    printf '%s:%s\n' "$repo" "$signature" > "$output/.satip-foundation-output"
fi
# A failed first make may leave an owned output without a configuration. Retry
# that initialization only; never overwrite an existing (even invalid) .config.
if [ ! -e "$output/.config" ] && [ ! -L "$output/.config" ]; then
    # Model the required amd64 host even for a configuration-only check on ARM macOS.
    # This does not make the native Bootlin binaries executable on this machine.
    make -C "$checkout" HOSTARCH=x86_64 O="$output" BR2_EXTERNAL="$script/external" \
        BR2_DL_DIR="$cache" satip_rpi4_64_defconfig
fi
python3 "$script/validate-config.py" "$output/.config" "$defconfig"
[ "$configure_only" -eq 0 ] || exit 0
# Lokale Paketstempel erkennen geänderte Python-/Grafikquellen nicht automatisch.
# Nur die beiden lokalen Pakete neu übernehmen.
make -C "$checkout" O="$output" BR2_EXTERNAL="$script/external" \
    BR2_DL_DIR="$cache" satip-app-dirclean satip-splash-dirclean
make -C "$checkout" O="$output" BR2_EXTERNAL="$script/external" \
    BR2_DL_DIR="$cache" BR2_JLEVEL="$jobs" -j"$jobs"
"$script/validate-artifacts.sh" "$output/images"
