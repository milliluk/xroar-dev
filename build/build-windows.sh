#!/usr/bin/env bash
# Cross-build the patched XRoar for Windows, from Linux or WSL, with MinGW.
#
#   bash build/build-windows.sh             # SDL3 GUI build
#   bash build/build-windows.sh headless    # no windowing or SDL dependency
#   MINGWROOT=~/mingw-sdl bash build/build-windows.sh    # SDL3 in a user prefix
#
# Output: bin/xroar-dev.exe (override with $XROAR_WIN_BIN).
#
# THIS IS THE BUILD THAT PASSES -Werror, deliberately. MinGW is the stricter
# target and it is where a missing declaration shows up as a truncated pointer
# rather than as silence. Run it when you add a patch even if you never intend
# to use the binary -- it found four such bugs the first time it was tried.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
VER=1.12.1
MODE="${1:-gui}"
HOSTTRIPLE="${HOSTTRIPLE:-x86_64-w64-mingw32}"
MINGWROOT="${MINGWROOT:-/usr/$HOSTTRIPLE}"
BIN="${XROAR_WIN_BIN:-$HERE/bin/xroar-dev.exe}"
JOBS="$(nproc 2>/dev/null || echo 4)"

case "$MODE" in
    gui|headless) ;;
    *) echo "usage: $0 [gui|headless]" >&2; exit 2 ;;
esac

# See build-linux.sh: /mnt/ answers are Windows binaries that cannot run here.
have() {
    local p
    p="$(command -v "$1" 2>/dev/null)" || return 1
    case "$p" in /mnt/*) return 1 ;; esac
    return 0
}

missing=()
have "$HOSTTRIPLE-gcc" || missing+=("mingw-w64")
have autoreconf        || missing+=("autoconf automake libtool")
if [ "$MODE" = gui ]; then
    # The native Windows UI is implemented on SDL3, not SDL2.
    [ -d "$MINGWROOT/include/SDL3" ] || missing+=("SDL3 dev libraries for $HOSTTRIPLE")
fi
if [ ${#missing[@]} -gt 0 ]; then
    echo "MISSING: ${missing[*]}" >&2
    cat >&2 <<'EOF'

  sudo apt-get install -y mingw-w64 autoconf automake libtool

The headless build needs no SDL. For the GUI build, Debian and Ubuntu do not
package SDL for the mingw host, so SDL3 comes from libsdl.org's own tarball:

  curl -LO https://github.com/libsdl-org/SDL/releases/download/release-3.4.12/SDL3-devel-3.4.12-mingw.tar.gz
  tar xzf SDL3-devel-3.4.12-mingw.tar.gz
  mkdir -p ~/mingw-sdl && cp -r SDL3-3.4.12/x86_64-w64-mingw32/* ~/mingw-sdl/
  MINGWROOT=~/mingw-sdl bash build/build-windows.sh
EOF
    exit 2
fi

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
autoreconf -fi

# --without-tre: MinGW has no POSIX regex.h; patch 02 supplies a matcher for
#   the fixed set of delimiter expressions XRoar actually uses.
# --without-opengl: skips the Mesa probe.
#
# SCREENSHOTS COMPILE OUT unless a mingw libpng is present under MINGWROOT.
# The series does that cleanly -- CLI and trap hooks go together, so
# -trap-screenshot reports "compiled out" rather than half-working -- and the
# acceptance check below will say MISSING, correctly.
if [ "$MODE" = headless ]; then
    echo ">> configure (mingw headless; no Windows UI or SDL)"
    ./configure --host="$HOSTTRIPLE" \
      --disable-windows32-ui --without-sdl3 --without-sdl2 \
      --without-tre --without-opengl
else
    echo ">> configure (mingw SDL3 GUI)"
    ./configure --host="$HOSTTRIPLE" \
      --without-tre --without-opengl \
      PKG_CONFIG_LIBDIR="$MINGWROOT/lib/pkgconfig" \
      SDL3_CFLAGS="-I$MINGWROOT/include" \
      SDL3_LIBS="-L$MINGWROOT/lib -lmingw32 -lSDL3 -mwindows"
fi

make ARFLAGS=cr -j"$JOBS" CFLAGS="-O2 -g -Werror"

mkdir -p "$(dirname "$BIN")"
cp src/xroar.exe "$BIN"
# The GUI executable needs the SDL3 runtime; headless has no non-system DLL.
if [ "$MODE" = gui ] && [ -f "$MINGWROOT/bin/SDL3.dll" ]; then
    cp "$MINGWROOT/bin/SDL3.dll" "$(dirname "$BIN")/"
fi

echo
echo ">> installed: $BIN"
# trap-screenshot is tolerated here: it needs a mingw libpng, which no distro
# packages, and the series compiles the CLI and the trap hooks out together so
# a build without it is consistent rather than half-wired. Everything else is
# still a hard failure.
bash "$HERE"/build/acceptance.sh "$BIN" trap-screenshot
