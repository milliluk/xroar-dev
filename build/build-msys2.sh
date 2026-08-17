#!/usr/bin/env bash
# Build the patched XRoar natively on Windows, under msys2.
#
#   bash build/build-msys2.sh              SDL3 window
#   bash build/build-msys2.sh headless     null UI/audio; no SDL or GUI linkage
#
# Output: bin/xroar-dev.exe, with its runtime DLLs beside it.
#
# Tested 2026-08-02 on Windows 10 22H2 under MINGW64, gcc 16.1.0, SDL3 3.4.12:
# both invocations build, all five acceptance lines present (trap-screenshot
# included -- msys2 packages libpng, so unlike the cross-build screenshots are
# NOT compiled out), and build/audit-options.sh reports 44/44 all clear.
#
# The cross-build (build-windows.sh, from Linux/WSL with mingw-w64) remains the
# -Werror gate and the reference. This script exists because "I am already on
# Windows" is a reasonable place to be, not because cross-building is wrong.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
VER=1.12.1
MODE="${1:-gui}"
JOBS="$(nproc 2>/dev/null || echo 4)"

case "$MODE" in
    gui|headless) ;;
    *) echo "usage: $0 [gui|headless]" >&2; exit 2 ;;
esac

# MINGW64, NOT MSYS. msys2 ships two toolchains: the MSYS one targets its own
# POSIX emulation layer and produces a binary that needs msys-2.0.dll and is
# not a native Windows program; the MINGW64 one targets Windows directly. They
# are selected by which shortcut you launched, and the failure is otherwise
# silent until something loads the wrong DLL. $MSYSTEM is how the shell says
# which it is.
case "${MSYSTEM:-}" in
    MINGW64|UCRT64|CLANG64) ;;
    "")  echo "Not an msys2 shell. Launch 'MSYS2 MINGW64' and re-run." >&2; exit 2 ;;
    *)   echo "Wrong msys2 shell: MSYSTEM=$MSYSTEM" >&2
         echo "Launch 'MSYS2 MINGW64' -- the MSYS shell builds against msys-2.0.dll," >&2
         echo "which is not a native Windows binary." >&2
         exit 2 ;;
esac

missing=()
command -v gcc        >/dev/null 2>&1 || missing+=("mingw-w64-x86_64-toolchain")
command -v autoreconf >/dev/null 2>&1 || missing+=("autoconf automake libtool")
command -v pkg-config >/dev/null 2>&1 || missing+=("mingw-w64-x86_64-pkgconf")
command -v patch      >/dev/null 2>&1 || missing+=("patch")
# doc/ builds xroar.info unconditionally: without makeinfo the top-level make
# fails AFTER src/xroar.exe links, so you get a binary and a failed build.
command -v makeinfo   >/dev/null 2>&1 || missing+=("texinfo")
if [ "$MODE" = gui ]; then
    # Lower-case sdl3: msys2 renamed the SDL packages.
    pkg-config --exists sdl3 2>/dev/null || missing+=("mingw-w64-x86_64-sdl3")
fi
if [ ${#missing[@]} -gt 0 ]; then
    echo "MISSING: ${missing[*]}" >&2
    echo >&2
    echo "  pacman -S --needed base-devel autoconf automake libtool texinfo patch \\" >&2
    echo "      mingw-w64-x86_64-toolchain mingw-w64-x86_64-sdl3 \\" >&2
    echo "      mingw-w64-x86_64-libpng mingw-w64-x86_64-pkgconf \\" >&2
    echo "      mingw-w64-x86_64-libtre" >&2
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

# Upstream SHIPS src/windows32/resources.{h,rc} pre-generated, precisely so that
# building the Windows UI does not need perl's Text::Iconv -- which msys2 does
# not package at all, in any repo. No patch in the series currently edits
# resources.win, so this is insurance rather than a fix: if one ever does, and
# `patch` writes .win after the two generated outputs, .win lands NEWER than
# them and make dutifully tries to regenerate. The contents would already be
# right, only the mtimes backwards. Without the restamp the build dies at
#   Can't locate Text/Iconv.pm in @INC
# after portalib builds and before anything in src/ compiles.
touch src/windows32/resources.h src/windows32/resources.rc

echo ">> autoreconf"
autoreconf -i

# --with-tre, NOT --without-tre. The cross-build passes --without-tre because a
# bare mingw sysroot has no POSIX regex.h; msys2 is not that. mingw-w64-x86_64-
# libsystre ships /mingw64/include/regex.h and is a hard dependency of ncurses,
# so it is present on essentially every real msys2 install. Upstream's configure
# reads "regex.h exists" as "libc has regcomp" -- a Linux-ism. On msys2 the
# header is there, the symbols are not, and --without-tre links a binary that
# references regcomp/regexec/regfree and dies at ld with undefined references
# out of portalib/sdsx.c. That is the whole failure and it took a full compile
# to reach.
#
# --with-tre is safe in BOTH worlds, which is why it is unconditional:
#   libtre present -> TRE_LIBS=-ltre, configure's link test passes, HAVE_TRE is
#     defined, pl-regex.h takes <tre/regex.h>, which macro-maps regcomp to
#     tre_regcomp. Full regex, better than the cross-build gets.
#   libtre absent  -> the link test fails, HAVE_TRE stays undefined, and there
#     is no regex.h either (it comes WITH tre), so patch 2's degraded delimiter
#     matcher is selected exactly as intended.
#
# No --host: under MINGW64 the native gcc already targets x86_64-w64-mingw32,
# which is exactly what --host asks for in the cross-build. Passing it here
# makes configure believe it is cross-compiling and disables its run tests.
#
# --without-opengl is redundant on this host (configure.ac already defaults
# with_opengl=no under MINGW) but is kept to match the cross-build's shape.
if [ "$MODE" = headless ]; then
    echo ">> configure (true headless; no Windows UI or SDL)"
    ./configure --with-tre --without-opengl \
      --disable-windows32-ui --without-sdl3 --without-sdl2
else
    echo ">> configure (SDL3 GUI)"
    ./configure --with-tre --without-opengl
fi

make ARFLAGS=cr -j"$JOBS"

mkdir -p "$HERE/bin"
cp src/xroar.exe "$HERE/bin/xroar-dev.exe"

# A native msys2 build links against the MINGW64 runtime DLLs, which live in
# /mingw64/bin and are on PATH inside the msys2 shell and NOWHERE from a plain
# cmd.exe. Copying them beside the binary is what makes bin/xroar-dev.exe
# runnable by double-clicking it, which is the only reason to build natively.
echo ">> collecting runtime DLLs"
copied=0
for dll in $(ldd src/xroar.exe 2>/dev/null | awk '/mingw64/ {print $3}'); do
    [ -f "$dll" ] || continue
    cp -u "$dll" "$HERE/bin/" && copied=$((copied+1))
done
echo "   $copied DLL(s) beside the binary"

echo
echo ">> built: $HERE/bin/xroar-dev.exe"
bash "$HERE"/build/acceptance.sh "$HERE/bin/xroar-dev.exe"
