#!/usr/bin/env bash
# Build the patched XRoar on macOS (Apple silicon or Intel), SDL2 UI.
#
#   bash build/build-macos.sh              SDL2 window
#   bash build/build-macos.sh headless     null UI, for automation
#
# Output: bin/xroar-dev
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
VER=1.12.1
MODE="${1:-gui}"
JOBS="$(sysctl -n hw.ncpu 2>/dev/null || echo 4)"

command -v brew >/dev/null 2>&1 || {
    echo "Homebrew not found. Install it, then:" >&2
    echo "  brew install autoconf automake libtool pkg-config sdl2 libpng texinfo" >&2
    exit 2
}

# Homebrew's prefix differs between Apple silicon (/opt/homebrew) and Intel
# (/usr/local). Ask, do not assume.
BREW="$(brew --prefix)"
export PATH="$BREW/bin:$PATH"
export PKG_CONFIG_PATH="${PKG_CONFIG_PATH:-}:$BREW/lib/pkgconfig"

missing=()
for t in autoreconf pkg-config makeinfo; do
    command -v "$t" >/dev/null 2>&1 || missing+=("$t")
done
[ -e "$BREW/lib/pkgconfig/sdl2.pc" ] || [ "$MODE" = headless ] || missing+=(sdl2)
if [ ${#missing[@]} -gt 0 ]; then
    echo "MISSING: ${missing[*]}" >&2
    echo "  brew install autoconf automake libtool pkg-config sdl2 libpng texinfo" >&2
    exit 2
fi

# APPLE'S libtool IS NOT GNU libtool, and autoreconf wants the GNU one. Without
# this it fails with a baffling complaint about missing macros rather than
# anything mentioning libtool.
if command -v glibtoolize >/dev/null 2>&1; then
    export LIBTOOLIZE=glibtoolize
fi

# Apple deprecated the whole OpenGL API in 10.14 but still ships and supports
# it, so the only thing the 12 deprecation warnings tell you is that you are
# building on a Mac. GL_SILENCE_DEPRECATION is Apple's own documented switch
# for exactly this. Not a patch, because it is a property of the platform
# rather than of the source.
export CPPFLAGS="${CPPFLAGS:-} -DGL_SILENCE_DEPRECATION"

cd "$HERE"
echo ">> extracting xroar-$VER"
rm -rf "xroar-$VER"
tar xzf "xroar/xroar-$VER.tar.gz"
cd "xroar-$VER"

echo ">> applying $(ls "$HERE"/xroar/[0-9]*.patch | wc -l) patches"
for p in "$HERE"/xroar/[0-9]*.patch; do
    # --no-backup-if-mismatch: a hunk that lands at an offset is still an exact
    # context match, but patch would leave a .orig beside the file. Offsets are
    # normal here -- editing any early patch shifts every later one -- and the
    # backups are pure litter in a tree that is deleted and rebuilt anyway.
    patch -p1 -s --no-backup-if-mismatch < "$p" || { echo "FAILED: $(basename "$p")" >&2; exit 1; }
done


echo ">> autoreconf"
autoreconf -i

if [ "$MODE" = headless ]; then
    # --without-tre: see build-linux.sh. Pinned so the automation binary does
    # not change delimiter-matching behaviour based on what brew happens to
    # have installed.
    echo ">> configure (headless, null UI)"
    ./configure --without-tre --without-x --without-opengl \
                --without-sdl3 --without-sdl2 \
                --without-gtk3 --without-gtk2 --enable-filereq-cli
else
    echo ">> configure (SDL2 GUI)"
    ./configure --without-tre --without-sdl3 --with-sdl2 --enable-ui-sdl
fi

make -j"$JOBS"


# The deliverable is bin/xroar-dev, not the path inside the build tree:
# one predictable name, and it cannot be confused with a system xroar.
mkdir -p "$HERE/bin"
cp src/xroar "$HERE/bin/xroar-dev"

echo
echo ">> built: $HERE/bin/xroar-dev"
bash "$HERE"/build/acceptance.sh "$HERE/bin/xroar-dev"
