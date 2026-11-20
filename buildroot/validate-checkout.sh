#!/bin/sh
set -eu
checkout=${1:?Buildroot checkout required}
expected=72d9d4fa636a371ef9eb99c92a735ce9f6d829d5
[ "$(git -C "$checkout" rev-parse HEAD)" = "$expected" ] || {
    echo "Buildroot must be at 2026.05.2 ($expected); no automatic reset performed" >&2
    exit 1
}
[ -z "$(git -C "$checkout" status --porcelain --untracked-files=all)" ] || {
    echo "Buildroot checkout is modified; preserve changes and use a clean dedicated checkout" >&2
    exit 1
}
printf '%s\n' "$expected"
