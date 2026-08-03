#!/usr/bin/env bash
# Build the patched XRoar on Linux.
#
#   bash build/build-linux.sh          headless (null UI) -- the automation binary
#   bash build/build-linux.sh gui      SDL2 window -- for a human to watch
#
# Output: bin/xroar-dev
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
VER=1.12.1
MODE="${1:-headless}"
JOBS="$(nproc 2>/dev/null || echo 4)"

# `command -v` IS NOT ENOUGH UNDER WSL. Windows interop puts /mnt/c/... on PATH,
# so an msys2 or Git-for-Windows install answers the probe with a .exe that
# cannot run here, the script declares the tool present, and the build dies
# minutes later inside something unrelated. A native tool answers with a path
# outside /mnt/, which is the whole test.
have() {
    local p
    p="$(command -v "$1" 2>/dev/null)" || return 1
    case "$p" in /mnt/*) return 1 ;; esac
    return 0
}

missing=()
have gcc        || missing+=(build-essential)
have autoreconf || missing+=("autoconf automake libtool")
have pkg-config || missing+=(pkg-config)
# doc/ builds xroar.info unconditionally: without makeinfo the top-level make
# fails AFTER src/xroar links, so you get a binary and a failed build.
have makeinfo   || missing+=(texinfo)
if [ ${#missing[@]} -gt 0 ]; then
    echo "MISSING: ${missing[*]}" >&2
    echo "  sudo apt-get install -y ${missing[*]}" >&2
    exit 2
fi

cd "$HERE"
echo ">> extracting xroar-$VER"
rm -rf "xroar-$VER"
tar xzf "xroar/xroar-$VER.tar.gz"
cd "xroar-$VER"

echo ">> applying $(ls "$HERE"/xroar/[0-9]*.patch | wc -l) patches"
for p in "$HERE"/xroar/[0-9]*.patch; do
    patch -p1 -s < "$p" || { echo "FAILED: $(basename "$p")" >&2; exit 1; }
done


echo ">> autoreconf"
autoreconf -i

if [ "$MODE" = gui ]; then
    echo ">> configure (SDL2 GUI)"
    ./configure --without-tre --without-sdl3 --with-sdl2 --enable-ui-sdl
else
    # Null UI: emulates fully, never asks for a display. A GUI build on a
    # display-less box starts, shows nothing and times out, which reads exactly
    # like a broken script -- this removes that whole class of confusion.
    # --without-tre for the same reason the GUI branch passes it, and it
    # matters MORE here: without it, whether delimiter matching uses libtre or
    # patch 2's degraded matcher depends on whether libtre happens to be
    # installed on the build host. That is a silent behaviour difference
    # between two machines running the same script, in the binary whose whole
    # job is reproducible automation.
    echo ">> configure (headless, null UI)"
    ./configure --without-tre --without-x --without-opengl \
                --without-sdl3 --without-sdl2 \
                --without-gtk3 --without-gtk2 --without-alsa --without-oss \
                --without-pulse --enable-filereq-cli
fi

make -j"$JOBS"


# The deliverable is bin/xroar-dev, not the path inside the build tree:
# one predictable name, and it cannot be confused with a system xroar.
mkdir -p "$HERE/bin"
cp src/xroar "$HERE/bin/xroar-dev"

echo
echo ">> built: $HERE/bin/xroar-dev"
bash "$HERE"/build/acceptance.sh "$HERE/bin/xroar-dev"
