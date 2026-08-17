# Changelog

High-level only, one entry per commit, newest first. Each patch carries its own
rationale in its commit message, and `PATCHES.md` says what every patch is for;
this file is the shape of the history, not a substitute for either.

## 2026-08-17: protect mode, backtrace, new input devices

The series renumbered so each patch's number lives in one place (the filename
ordinal is the patch number and the `Subject:` agrees), five new patches, and
the tooling and documentation that go with them.

- **`-protect-mode` (patch 47)**: the run dies at the *first* wild access
  instead of thirty seconds later with a corrupted screen. Four checks, one
  flag each (`-protect-mode-stack` / `-write` / `-read` / `-data`). A violation
  reports the access with symbol and `file:line`, registers, the flight
  recorder and a backtrace, dumps flat RAM, and exits 70. Code and data are
  told apart from the assembler listing, so nothing is tagged in source. The
  stack check covers all three ways a stack goes wrong: onto code, onto
  declared data (gensym's new `M D` ranges, safe because `rmb` emits no listing
  bytes, so reserved space, where every stack lives, is in no `M` range), and
  past `-protect-stack-floor ADDR`, the one number the build cannot supply.
  `-trap-protect-on` defers arming past boot, which is the only thing it is
  for, and the arm banner describes the run it is announcing rather than the
  default it was composed from.
- **Shadow call stack (patch 48)**: `-trap-backtrace` dumps a symbolized call
  stack at any trap, and protect faults print one. No frame pointers required.
- **`gensym.py` becomes format 2**, emitting the code/data map protect mode
  enforces. Its instruction and directive sets are generated from the vendored
  lwasm's own `instab.c` by `tools/gen_mnemonics.py`, not typed by hand: the
  hand-written version was missing 55 mnemonics (every Q-register form,
  `sbcd`/`sbcr`, the E/F/W negates and shifts, all four `tfr` variants, the
  whole 6800-compatibility block), each one a hole in the code map. 277
  instructions and 84 directives now, with `--check` for CI. `os9` is a
  documented exception: it is a directive that emits `SWI2`, so it counts as
  code, or every OS-9 system call would look like data.
- **Input devices (patches 45, 46, 49)**: scripted joystick ports are
  independent, so a two-player test can drive them apart. The CoCo Max Hi-Res
  Input Module and the X-Pad GT-116 are emulated as cartridges (`-cart cocomax`
  / `-cart xpad`), both sourced from the right virtual joystick. And
  `-rat-mouse right|left` presents a port's source as a Diecom RAT: a pot that
  snaps between four discrete levels, one per wheel detent, which is what the
  device really is. The RAT is a filter over the port's axis readings rather
  than a joystick module, so an input script, the host mouse or a real stick
  all drive one unchanged. `-rat-rate` (default 150Hz) must stay below the rate
  the guest samples at, and absolute position does not survive the trip,
  because the position lives in the guest's decoder. Color Max Deluxe's RAT
  driver could not be exercised at all before this.
- **MC-10 flat RAM**: the internal and expansion RAM are two parts with
  incompatible organisations, so `-load-ram`, `qxroar.physmem`, the backtrace
  and the RAM dump now **span both** as one flat physical space (patches
  9/16/27/48). Renaming the parts, the obvious alternative, would have broken
  snapshots and still covered only the first bank.
- **Every feature on every machine it could work on**: an audit asked which
  features were CoCo 3-only that should not be, and eight were failing
  **silently** elsewhere. All eight fixed, each folded into the patch that
  introduced the feature: `emuext` SHOT, `-serial-out` and `-audit` on the
  Dragon family (18/28/37, with a loud warning on a Dragon 32/64/Pro instead of
  arming nothing); `qxroar.cycles` and a `T` stop packet from gdb on every
  machine, so `watch:` and `sym:` reach non-GIME clients (27/39/40/41); CPU and
  master cycle counts on the MC-10, so `cycle=` traps fire there (17); and a
  missing or CRC-invalid essential BASIC ROM fatal on every machine, not just
  the CoCo 3 (20). `MACHINE-COVERAGE.md` records what works where, and which
  restrictions are inherent rather than oversights. It was re-audited at 49
  patches with the new input devices executed on a CoCo 3 and a CoCo 1/2 rather
  than argued; the two partials it gained are both MC-10 hardware the machine
  does not have.

## 2026-08-04: `-disk-fast` compatibility

`-disk-fast` collapses the FDC's mechanical delays, but clamping *every* delay
turned the first-DRQ service window into a race: RSDOS services the first byte
of each sector write from a polling loop that needs slightly longer than one
byte time, so writes died on LOST_DATA depending on which tick DRQ landed on,
and one disk layout failed 3/3 while its neighbours passed. The two first-DRQ
windows are now scheduled unclamped; everything else still collapses.

## 2026-08-03: initial commit

XRoar 1.12.1 plus the patch series, `lwasm`, the host-side Python tools and the
per-platform build scripts.
