#!/usr/bin/env bash
# Build the patched lwasm (lwtools) 6809/6309 assembler.
#
#   bash build/build-lwasm.sh
#
# Output: lwasm/lwtools-4.25/lwasm/lwasm
#
# THE PATCH IS ESSENTIAL if you intend to use symbols. Stock lwasm lets an
# `opt nolist` anywhere in the source decide what --map and --symbol-dump
# contain, so direct-page symbols silently vanish from the map -- and a symbol
# file that is quietly incomplete is worse than none, because -symbols will
# resolve some names and not others with no indication which.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
VER=4.25
JOBS="$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)"

have() {
    local p
    p="$(command -v "$1" 2>/dev/null)" || return 1
    case "$p" in /mnt/*) return 1 ;; esac
    return 0
}
have make || { echo "MISSING: make" >&2; exit 2; }
have gcc  || { echo "MISSING: a C compiler" >&2; exit 2; }

cd "$HERE/lwasm"
echo ">> extracting lwtools-$VER"
rm -rf "lwtools-$VER"
tar xzf "lwtools-$VER.tar.gz"

echo ">> applying patches"
for p in "$HERE"/lwasm/patches/[0-9]*.patch; do
    echo "   $(basename "$p")"
    patch -p1 --no-backup-if-mismatch -d "lwtools-$VER" < "$p" || { echo "FAILED: $(basename "$p")" >&2; exit 1; }
done

make -C "lwtools-$VER" -j"$JOBS"

BIN="$HERE/lwasm/lwtools-$VER/lwasm/lwasm"
[ -x "$BIN" ] || { echo "build produced no lwasm binary" >&2; exit 1; }

mkdir -p "$HERE/bin"
cp "$BIN" "$HERE/bin/lwasm"

echo
echo ">> built: $HERE/bin/lwasm"
"$BIN" --version 2>&1 | head -1 || true
