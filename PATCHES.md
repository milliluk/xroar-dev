# The patch series

44 patches, applied in filename order. Each is self-contained and carries its
own rationale in the commit message. **Read the patch before changing what it
touches**; the message usually explains a failure mode that is not obvious from
the code.

**One number, in one place.** The filename ordinal *is* the patch number and
the `Subject:` line agrees with it: `0037-audit-...patch` is
`[PATCH 37/44]`, and a cross-reference to "patch 37" means that file. There
used to be a second, drifting number embedded in both, a leftover from
patches dropped during a rebase, and it is gone. If you add a patch, keep
this property: it is the difference between a reference you can follow and one
you have to decode.

## Read this first: what an agent actually needs

If you only absorb four, absorb these. They are what make the emulator
scriptable at all:

- **14 `-input-script`**: input without a keyboard.
- **17 / 21 / 23 trap actions and conditions**: stop at a defined point.
- **9 / 16 flat physical RAM**: get data in and out by physical address.
- **10 `emuext`**: let the *guest program* talk to the host.

Everything else is refinement of those four ideas, or a bug fix.

---

## Group 1: Build and portability

| # | What it does | Why an agent cares |
|---|---|---|
| 1 | `--without-opengl` as a real configure option | Headless boxes have no Mesa. Without this you cannot configure at all on a bare container. |
| 2 | Delimiter matching degrades when TRE/`regex.h` is missing | MinGW has no POSIX regex. Lets the Windows cross-build happen without cross-building a regex library. |
| 3 | `--disable-windows32-ui` builds a console-subsystem Windows binary with no SDL, dialogs, resources, or native UI code | Makes Windows headless at link time, not merely at runtime. |
| 32 | `--without-screenshot`, a real feature switch | Compiles the screenshot CLI *and* its trap hooks out together, so a build without libpng is consistent rather than half-wired. |
| 36 | Clean exit on `SIGTERM`/`SIGINT`/`SIGHUP` | An automated run gets killed routinely. Without this you lose the snapshot and any recording that was mid-write. |
| 4 | Drop a stray `@` from `doc/xroar.texi` | Upstream's: 26 makeinfo warnings on every build, meaning nothing. |
| 5 | `kAudioObjectPropertyElementMain` | Upstream's: renamed in macOS 12, deprecated warning on every Mac build. |

**1, 2, 4 and 5 fix upstream problems.** Patch 3 keeps the Windows build
separation beside its MinGW prerequisites, so the portability foundation is
together before feature work begins. A warning after patch 5 is one we
introduced.

**Only 4 and 5 are standalone, because a fix belongs in the patch that caused
it.** Building with `-Werror` for the first time, and on macOS, turned up six
defects in this series: four missing declarations (three of them returning
pointers, which an implicit declaration truncates on a 64-bit host), an
uncast `sprintf` buffer, and `-input-script` missing from `-h`. Each was folded
back into the patch that introduced it, not appended as a fixup. 4 and 5 stay
standalone only because they fix **upstream's** code, where there is no earlier
patch of ours to fold into.

**Fold, do not append.** A series that accumulates "fix the thing patch 32 got
wrong" patches makes every reader reconstruct the real state of a file by
replaying edits. Regenerate the original patch instead: rebuild the tree at
N-1, apply N, apply the fix, diff. `AGENT-NOTES.md` has the recipe.

**The series is offset-exact, and worth keeping that way.** Every patch applies
at its stated line numbers. `git apply`, which tolerates no drift at all,
accepts all 44 from a pristine tarball. That was not true before: folding fixups
into earlier patches moved `src/xroar.c` by about sixty lines and left sixteen
later patches applying at an offset, four of them with *fuzz*: `patch`
discarding context lines to make a hunk fit. Both are silent, and fuzz is the
one that can land a hunk in the wrong place.

Regenerating is mechanical and is the same replay used for folding: apply the
series into a git repo one patch per commit, then `git format-patch -44
--numbered --no-signature`. Check the result by applying it to a fresh tarball
and diffing that tree against one built with the old series; they must be
identical.

## Group 2: Emulation correctness

| # | What it does | Why an agent cares |
|---|---|---|
| 6 | 6309 native-mode cycle counts | Cycle-count assertions and any timing measurement are meaningless without it. |
| 15 | Hi-res (Tandy/CoCoMax3) joystick adaptor + DAC wiring | The high-resolution pointer interface real paint software uses. Scripted pointer work needs the same path the guest expects. |
| 29 | `-lp-file` serial hook address `$A2C1` → `$A2BF` | The old address was mid-instruction in some ROM revisions: hooking it wedged the machine after two bytes. |
| 30 | `-lp-file` on the CoCo 3 at all | It never worked there; BASIC runs from RAM, so a ROM-mapping test can never identify the routine. |
| 35 | Sanitise guest `LOG` output | Raw guest memory reaching a host terminal is a control-sequence injection waiting to happen. |

## Group 3: Physical addressing (the fork's spine)

Read the README's note on physical vs logical first. These are the patches that
make it real.

| # | What it does |
|---|---|
| 8 | GIME MMU logical→physical translation, exposed |
| 9 | `ram_load/peek/poke_flat`, flat physical RAM access |
| 16 | `-load-ram`, load raw / DECB / Intel hex / SREC at a physical address |
| 23 | `xpc=` trap condition (physical PC) + `-trap-history` flight recorder |
| 24 | `emuext` physical-PC profiler |
| 25 | `-trap-ram` writes flat physical RAM when a trap fires |
| 31 | Physical `read=`/`write=` watchpoints via GIME Z |
| 37 | Physical access-audit map with a CPU access log |
| 38 | `xpc=` rebuilt on the range mechanism so it composes with the rest |
| 39 | Physical instruction breakpoints, exposed to gdb |
| 40 | gdb reports the address a watchpoint stop *actually* hit |

## Group 4: Driving the machine

| # | What it does | Notes |
|---|---|---|
| 14 | **`-input-script`**, scripted keyboard and joystick | The core automation primitive. A text file of steps; drives the PIA matrix and the joystick axes the way hardware does, so every guest-side poll sees what it expects. |
| 42 | `-init-delay` holds the first scripted input, by any route | A headless run is otherwise perfectly repeatable, so a guest that seeds anything from a timer at first input gets the same value every time. `-init-delay-random` puts the variation back, and logs the value it drew so a run that finds something can be repeated. One deadline shared by the auto-typer and `-input-script`. |
| 12 | Hide the host cursor over the display | Screenshots stop containing the operator's mouse pointer. |
| 33 | `-zoom N` integer window scale at startup | Reproducible window size, so screenshot comparison is stable. |
| 11 | `-disk-fast` skips floppy access delays | Removes minutes of emulated seek time from every run. |
| 44 | **`-headless`**, one flag for an automation run | Supplies null UI, null audio, `-no-ratelimit` and `-disk-fast`. Each is a default and never an override, so naming one explicitly wins from either side of it. A build with no windowing UI defaults to it; `-no-headless` undoes it. Forgetting the throttle costs the whole difference between seconds and minutes, and it is the null *audio* driver that enforces real time, so "no display to sync to" never helped. |
| 26 | `-trap-ratelimit-off/on` from a trap | Run flat out for the boring part, real-time for the part being measured. |
| 13 | Soft reset reloads the snapshot | Makes a run repeatable from a known state without relaunching. |

## Group 5: Getting results out

| # | What it does |
|---|---|
| 10 | **`emuext`**, the guest→host `LOG` side-channel and named assertions. The running 6809 program can print, assert and request a screenshot. This is the guest talking, not the host guessing. |
| 17 | Trap *actions*: `-trap-load`, snapshots, and the action framework the rest hangs off. Also the cycle counters: `-trap-cycle-reset` / `-trap-cycle-print` / `-trap-cycle-print-reset`, and `-trap-cycle-master` for master clock ticks |
| 18 | `-trap-screenshot` |
| 19 | `-audio-record[-format/-source]` |
| 21 | `seconds=` / `cycle=` scheduled traps |
| 22 | `bp_add_range`; defers `-trap-snap` to a safe point |
| 27 | gdb GIME / flat-RAM / cycle queries. **Not** upstream's `-gdb-pseudo-regs`, and worth knowing why before extending either (see below) |
| 28 | `-serial-out` captures bit-banger serial |
| 41 | **`-symbols FILE`, then `@Name` wherever an address is accepted** |
| 43 | History and profile on one stream, surviving a machine change |
| 7 | gdb tolerates re-binding its port instead of dying |
| 20 | Loud failure on malformed loads |
| 34 | Never assume hex: only `0x`, loud errors otherwise |

**Patch 27 and upstream's `-gdb-pseudo-regs` are different mechanisms.** XRoar
1.12 added that option, which adds *parts* to the GDB target's register set:
SAM and both PIAs on a Dragon, the GIME and both PIAs on a CoCo 3, nothing on an
MC-10. They become real registers: they change the target description XML, they
need the flag, and `info registers` shows them.

Patch 27 adds a vendor query namespace instead, `qxroar.NAME` / `Qxroar.NAME`,
which is always available, changes no XML, and returns plain hex. What it
exposes is mostly what a register dump cannot give you: `xpc` and `block` (the
GIME's logical→physical translation of the PC), `paddr` (the physical address of
the last bus cycle), `physmem` (read and write flat physical RAM), `cycles`, and
the input setters. Those are derivations and side channels, not registers.

There used to be one genuine overlap: a `qxroar.sam` get/set doing by vendor
query what `-gdb-pseudo-regs` does properly as a register. **It has been
removed**: no name, no type, no target description, and a second way to spell
something that already had one. Use `-gdb-pseudo-regs` for SAM.

That is the rule for anything that is simply a chip register: SAM, either PIA,
the GIME's own `$FF9x`. It belongs in `-gdb-pseudo-regs`, which already has the
machine interface for it. The `qxroar.` namespace is for what a register dump
cannot express, and keeping that line clean is the whole reason the boundary is
written down here.

**Patch 40 is the quality-of-life one.** With a symbol file loaded you write
`-trap xpc=@MainLoop` instead of a number you re-derived by hand. Numbers move
between builds; names do not.

---

## The lwasm patches

`lwasm/patches/` carries two patches against lwtools 4.25. The first matters
more than a single patch usually would:

**`lwasm`: stop `nolist` deciding what `--map` and `--symbol-dump` contain.**
Stock `lwasm` lets an `opt nolist` anywhere in the source suppress symbols from
the *map*, not just from the listing. The two are unrelated outputs and only
one of them is a listing. The effect is that direct-page symbols, which are
exactly the ones a debugger wants, vanish silently from the map, and
`gensym.py` then writes a symbol file that is quietly incomplete.

That is worse than having no symbol file, because `-symbols` resolves some
names and not others with no indication which, so `@SomeVar` fails while
`@SomeRoutine` works and nothing explains why. If you intend to use patch 41 at
all, you need this one.

**`lwcc`: fix a self-assignment that loses skipped tokens.** `t2 = t2;` where
`t2 = t;` was meant, in the loop that collects whitespace before a macro's
opening paren, so the list head never advances and the tokens are never ungot.
The compiler finds it as `-Wself-assign`; it is a real bug, not noise. It is in
`lwcc`, lwtools' C preprocessor, **not** in `lwasm`, so nothing this repo does
exercises it. The fix is correct by inspection (the same function uses the
push idiom correctly ten lines later) but is unverified by running `lwcc`.

## Housekeeping

The series carries **no generated build output**: `Makefile.in`, `aclocal.m4`,
`config.h.in`, `configure` and `.deps/*.Po` are all regenerated by `autoreconf`
and `make`, so they are not in the patches. That was not always true: about
55,000 lines of such output were stripped out, patch 17 alone shrinking from
42,499 lines to 2,060, and it is worth keeping true. **When you regenerate a
patch, do not `git add -A`;** add the sources you actually changed.

## Adding a patch

1. Apply the series to a scratch tree.
2. Edit the tree.
3. `diff -u` the file you changed, or `git format-patch` if you made the
   scratch tree a repo.
4. Write a commit message that says **why**, and what breaks without it. The
   existing messages are the standard; several of them are the only record of
   a failure mode that took a day to find.
5. Number it after the last patch. Verify the whole series still applies from a
   pristine tarball, with `git apply`, not just `patch`, so drift cannot hide.
6. Build with `-Werror`.

**Never hand-edit a diff to do this.** See `AGENT-NOTES.md`.
