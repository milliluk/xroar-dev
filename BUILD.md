# Building

Two binaries come out of this repo:

- **`bin/xroar-dev`**, the emulator, patched. Build the **headless** variant
  for automation and the **GUI** variant if a human wants to watch. (On
  Windows it is `bin/xroar-dev.exe`.)
- **`bin/lwasm`**, the 6809/6309 assembler, patched.

The sources are vendored, so no build step fetches them. The one exception is
a Windows GUI cross-build: Debian and Ubuntu package no SDL for the mingw host,
so it needs SDL3 downloaded from libsdl.org once. The Windows headless build
and every other path are offline.

## Quick start

    bash build/build-linux.sh            # headless, the automation binary
    bash build/build-linux.sh gui        # SDL2 window, for humans
    bash build/build-macos.sh            # SDL2, Apple silicon or Intel
    bash build/build-macos.sh headless   # null UI, the automation binary
    bash build/build-windows.sh          # MinGW cross-build from Linux/WSL
    bash build/build-windows.sh headless # MinGW console binary, no SDL
    bash build/build-msys2.sh            # native Windows, msys2 MINGW64 shell
    bash build/build-msys2.sh headless   # native Windows, no UI/SDL linkage
    bash build/build-lwasm.sh            # the assembler, all platforms

Each script extracts the tarball from `xroar/` (or `lwasm/`) into the repo
root, applies its patch series in filename order, runs `autoreconf`, configures
and builds. Re-running is safe: the extract directory is removed first, so a
build is always from pristine sources plus the series.

## What "headless" means and why you want it

The automation binary configures with the **null UI**: no X, no OpenGL, no
window. It still emulates fully, still runs `-input-script`, still takes
snapshots and screenshots. It just never asks for a display.

That matters because the alternative fails in a confusing way. A GUI build on
a box with no display starts, does nothing visible, and eventually times out,
which reads exactly like a broken script. Use the headless build for anything
unattended and you remove that whole class of confusion.

**`-headless` is the flag that says "this run is automation."** It supplies four
things: the null UI, null audio, no wall-clock throttle, and no emulated floppy
seek latency. The last two are the difference between a run that takes seconds
and one that takes minutes.

A build with no windowing UI compiled in can only run that way, so it **defaults
to `-headless`**, so you do not pass it. `-no-headless` puts the interactive
behaviour back. A GUI build defaults the other way and needs to be told, which
is what makes one binary usable for both:

    bin/xroar-dev -headless -machine coco3 -input-script run.txt

Everything `-headless` supplies is a *default*, never an override, so naming one
explicitly wins from either side of it: `-headless -ratelimit` and `-ratelimit
-headless` both keep the throttle. `-config-print-all` reports what a given
command line actually resolved to, which is the quickest way to settle an
argument about it.

Worth knowing why the throttle matters: real-time pacing comes from the *audio*
driver, not the video: the null audio backend calls `nanosleep`. "There is no
display to sync to" was never the reprieve it sounds like, and a headless run
without this would sit at 1× forever.

Windows follows the same rule: the headless build compiles the native UI out,
defaults to `-headless`, and needs no runtime flag. The optional SDL GUI build
still needs `-headless` when used for automation.

## Acceptance test

Every script ends by confirming the five capabilities that make this fork worth
having:

    emuext:          present
    script joystick: present
    -input-script:   present
    gdb target:      present
    trap-screenshot: present

**If a line says MISSING, the build is not usable for automation**. Configure
dropped a feature because a dependency was absent, which it does quietly.
`trap-screenshot` is the one that most often goes missing, and only on the
mingw cross-build, which has no libpng; every other path including native msys2
should show all five.

---

## Linux

Needs a C toolchain, `make`, autotools, and `libpng` for screenshots.

    sudo apt-get install -y build-essential autoconf automake libtool \
                            pkg-config texinfo libpng-dev
    bash build/build-linux.sh

For the GUI variant add SDL2:

    sudo apt-get install -y libsdl2-dev
    bash build/build-linux.sh gui

`texinfo` is not optional in the way it looks: `doc/` builds `xroar.info`
unconditionally, and without `makeinfo` the top-level `make` fails **after**
the binary has linked. You get a working binary and a failed build, which is a
confusing place to be.

### WSL specifically

`command -v` is not a dependency check under WSL. Windows interop puts
`/mnt/c/...` on `PATH`, so an msys2 or Git-for-Windows install answers the
probe with a `.exe` that cannot run under Linux. A real check rejects any
answer under `/mnt/`; the build scripts here do that. If you write your own,
copy the `have()` function out of `build/build-linux.sh`.

## macOS

    brew install autoconf automake libtool pkg-config sdl2 libpng texinfo
    bash build/build-macos.sh

Notes:

- Apple's `libtool` is not GNU libtool. Homebrew's `glibtoolize` is what
  `autoreconf` wants; the script exports `LIBTOOLIZE=glibtoolize` when it finds
  it, which is the difference between `autoreconf` working and a baffling
  failure about missing macros.
- Homebrew's prefix differs between Apple silicon (`/opt/homebrew`) and Intel
  (`/usr/local`). The script asks `brew --prefix` instead of assuming.
- macOS has `strsep`, so the portability patch that matters on Windows is inert
  here.

## Windows

Two paths, and they are for different jobs:

| | `build/build-windows.sh` | `build/build-msys2.sh` |
|---|---|---|
| Runs on | Linux or WSL | Windows, msys2 **MINGW64** shell |
| Builds with | mingw-w64 cross-compiler | msys2's native gcc |
| `-Werror` | **yes**, this is the gate | no |
| Screenshots | compiled out (no mingw libpng) | **present** (msys2 packages libpng) |
| Tested | the reference path | Windows 10 22H2, gcc 16.1.0, SDL3 3.4.12, 2026-08-02 |

**Adding a patch? Run the cross-build.** It is the `-Werror` gate and that is
its whole point. MinGW is the stricter target, and it is where a missing
declaration surfaces as a truncated pointer rather than as silence. It found
four such bugs the first time it was tried.

**Just want a working xroar.exe on the Windows box you are sitting at?** Use
the msys2 build. It also gets you screenshots, which the cross-build cannot.

The GUI builds use **SDL3, not SDL2**. The headless builds compile the native
Windows UI out entirely and do not need either SDL version.

### Cross-build, from Linux or WSL

    sudo apt-get install -y mingw-w64 autoconf automake libtool
    bash build/build-windows.sh headless

This produces a console-subsystem executable containing only the null UI and
audio paths, with no SDL import or companion DLL. For the GUI build, run the
script without `headless` and provide SDL3 as below.

Debian and Ubuntu do not package SDL for the mingw host, so it comes from
libsdl.org's own tarball:

    curl -LO https://github.com/libsdl-org/SDL/releases/download/release-3.4.12/SDL3-devel-3.4.12-mingw.tar.gz
    tar xzf SDL3-devel-3.4.12-mingw.tar.gz
    mkdir -p ~/mingw-sdl && cp -r SDL3-3.4.12/x86_64-w64-mingw32/* ~/mingw-sdl/
    MINGWROOT=~/mingw-sdl bash build/build-windows.sh

Two things to expect:

- **Screenshots compile out** unless you also provide a mingw `libpng`. The
  series does that cleanly, so `-trap-screenshot` reports "compiled out" rather
  than half-working, but the acceptance test will say MISSING, correctly.
- **`SDL3.dll` must sit beside `xroar.exe`.** The build copies it for you.

It also has an undocumented dependency worth knowing about: it regenerates
`src/windows32/resources.{h,rc}` with `tools/win2rc`, a perl script needing
`Text::Iconv` (`libtext-iconv-perl` on Debian). See the msys2 notes below,
which is where the reason surfaced.

### Native, under msys2

**Launch the `MSYS2 MINGW64` shortcut, not `MSYS2 MSYS`.** msys2 ships two
toolchains; the MSYS one targets its own POSIX layer and produces a binary
needing `msys-2.0.dll`, which is not a native Windows program. The script
checks `$MSYSTEM` and refuses rather than let you find out later. It matters
for more than the compiler: `python3` and `strings`, which the audit and
acceptance checks need, exist only on the MINGW64 path.

This is the line that actually worked:

    pacman -S --needed base-devel autoconf automake libtool texinfo patch \
        mingw-w64-x86_64-toolchain mingw-w64-x86_64-sdl3 \
        mingw-w64-x86_64-libpng mingw-w64-x86_64-pkgconf \
        mingw-w64-x86_64-libtre

    bash build/build-msys2.sh

Note **`sdl3` is lower-case**: msys2 renamed the SDL packages, and
`mingw-w64-x86_64-SDL3` is `target not found`.

For a true headless build, run:

    bash build/build-msys2.sh headless

Patch 3's `--disable-windows32-ui` separates Windows OS support from the
native UI. It omits dialog/resource/host-keyboard code, disables SDL detection,
links a console-subsystem executable, and defaults to `-headless` because no
windowing UI is compiled in. Run the result normally:

    bin/xroar-dev.exe -machine coco3 -input-script run.txt

The no-argument build remains the SDL3 GUI variant. It can also use the
`-headless` runtime flag, but still carries the GUI and SDL dependencies.

**`--with-tre`, the opposite of the cross-build.** `mingw-w64-x86_64-libsystre`
ships `/mingw64/include/regex.h` and is a hard dependency of `ncurses`, so it
is present on essentially any real msys2. Upstream's configure reads "`regex.h`
exists" as "libc has `regcomp`", a Linux-ism. On msys2 the header is there and
the symbols are not, so `--without-tre` links a binary referencing
`regcomp`/`regexec`/`regfree` and fails at `ld` out of `portalib/sdsx.c`.
`--with-tre` is correct in both worlds: with libtre it links `-ltre` and gets
full regex (better than the cross-build's degraded matcher); without it there
is no `regex.h` either, and patch 2's fallback is selected as intended.

**The `.exe` is a Windows-subsystem binary**, because `MINGW_LIBS` carries
`-mwindows`. Two consequences that look like bugs and are not:

- `cmd.exe` does **not wait** for it. `xroar-dev.exe -h > out.txt` from a
  prompt returns instantly with an empty file. Anything driving it must wait on
  the process properly with `subprocess.run` or `Start-Process -Wait`. msys2's bash
  waits correctly, which is why the build's own acceptance checks are fine.
- It attaches to the parent console for output rather than owning one.

**Runtime DLLs are copied beside the binary**, six of them (`SDL3`,
`libpng16-16`, `libtre-5`, `libiconv-2`, `libintl-8`, `zlib1`), which is the
entire reason to build natively rather than cross-compile. `ldd` works fine on
a native PE under msys2 and resolves them recursively; `ntldd` was not needed.

**A tree built under both WSL and msys2 has two binaries in `bin/`**, a Linux
ELF `xroar-dev` and `xroar-dev.exe`. `build/audit-options.sh` picks the right
one by `uname`; if you point a script at the bare name yourself on Windows you
will run the ELF, get no output, and see every option reported missing.

## The assembler

    bash build/build-lwasm.sh        # -> bin/lwasm

One patch is essential for anything symbolic: stock `lwasm` lets an
`opt nolist` in the source decide what `--map` and `--symbol-dump` contain, so
every direct-page symbol silently vanishes from the map. If you intend to feed
symbols to `-symbols` (patch 41), you need this patch or your symbol file is
quietly incomplete.

## Running it

**The build does not install anything.** Both binaries stay in their build
trees, which is deliberate. An automation rig should name the binary it built,
not whatever happens to be on `PATH`:

    bin/xroar-dev        the emulator   (bin/xroar-dev.exe on Windows)
    bin/lwasm            the assembler

Each build copies its result there, so you get one predictable path and a name
that cannot be mistaken for a system `xroar`. The autotools tree underneath
(`xroar-1.12.1/`) is scratch and is rebuilt from the tarball every time.

Run them from `bin/`. If you want them on `PATH`, symlink rather than copy so
a rebuild is picked up automatically:

    ln -sf "$PWD/bin/xroar-dev" /usr/local/bin/xroar-dev
    ln -sf "$PWD/bin/lwasm" /usr/local/bin/lwasm

`sudo make install` from inside `xroar-1.12.1/` also works (normal autotools
tree), and `--program-suffix=-dev` at configure time makes it install as
`xroar-dev`, but then you have two copies and no reminder which one your
scripts are using.

### First: ROM images

**XRoar will not do anything useful without firmware ROMs.** Put them where
XRoar looks:

| Platform | ROM directory |
|---|---|
| macOS | `~/Library/XRoar/roms/` |
| Linux / Unix | `~/.xroar/roms/` |
| Windows | `%LOCALAPPDATA%\XRoar\roms\` |

A small starter archive is kept with the other XRoar inputs under `xroar/`.

`-rompath DIR` overrides it. For a CoCo 3 you want `coco3.rom`, plus
`disk11.rom` if you want disks. A Dragon 32 wants `d32.rom`. XRoar prints the
CRC of every ROM it loads, so a wrong or truncated file is visible immediately.

### A human session

    bin/xroar-dev -machine coco3
    bin/xroar-dev -machine coco3 -load-fd0 mydisk.dsk

### An automated one

This is what the fork is for. Nothing here needs a display:

    bin/xroar-dev -machine coco3 \
        -load-fd0 mydisk.dsk \
        -input-script run.txt \
        -symbols obj/program.sym \
        -trap 'xpc=@MainLoop' -trap-snap out.sna -trap-screenshot out.png

No `-no-ratelimit` and no `-disk-fast`: a headless build is already `-headless`,
which supplies both. On a GUI build, add `-headless` and this same line works.

and `run.txt` is a plain text file of steps:

    wait 3000
    type RUN"PROGRAM"\r
    wait 2000
    move 160 96
    down
    wait 200
    up

Read `-h` for the full option list; `PATCHES.md` says which patch provides
what, so if an option is missing you can tell whether it was compiled out or
never existed.

### Sanity check without any ROMs

    bin/xroar-dev --version
    bash build/acceptance.sh bin/xroar-dev

The second is the same check the build runs, so you can re-confirm a binary
long after you built it.

There is a third, for whoever adds an option:

    bash build/audit-options.sh

It fails if this fork registers an option that `-h` does not print. Two
features shipped invisible that way before it existed.

## Troubleshooting

**Configure succeeds, a feature is missing.** Configure drops features when a
dependency is absent and says so only in passing. Read the acceptance lines,
not the exit code.

**`autoreconf` fails on macOS with missing macros.** GNU vs Apple libtool; see
above.

**Everything compiles, the link fails on SDL.** You have the headers but not
the libraries, or the wrong SDL major version. On Windows this is almost always
SDL2-instead-of-SDL3.

**Everything compiles, the link fails on `regcomp`/`regexec`/`regfree`** out of
`portalib/sdsx.c`. A `regex.h` is present without a libc that implements it,
which is msys2 via `libsystre`. Configure with `--with-tre`, not `--without-tre`.

**`Can't locate Text/Iconv.pm in @INC`**, after `portalib` builds and before
anything in `src/` compiles. `tools/win2rc` is regenerating
`src/windows32/resources.{h,rc}`, which upstream ships pre-built precisely so
it does not have to. This happens when a patch edits `resources.win` later in
its diff order than the two generated outputs: `patch` stamps each file as it
writes it, so the input lands newer than its own outputs and make regenerates
them. The contents are already correct, only the mtimes are backwards.
`build/build-msys2.sh` restamps the outputs after applying the series. msys2
packages no `Text::Iconv` in any repo, so installing your way out is not an
option there.

**A patch fails to apply.** Do not fix it by hand-editing the diff. See
`AGENT-NOTES.md`; that path corrupts the series in ways that surface five
patches later.
