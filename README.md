# xroar-dev

[![build](https://github.com/milliluk/xroar-dev/actions/workflows/build.yml/badge.svg)](https://github.com/milliluk/xroar-dev/actions/workflows/build.yml)

A fork of [XRoar](https://www.6809.org.uk/xroar/) that turns a Dragon/Tandy
CoCo emulator into something an LLM can **drive, observe and reason about**
without a human at the keyboard.

Stock XRoar is built for a person: you watch a screen, you type, you judge what
happened with your eyes. None of that survives automation. An agent needs to
start a machine, put input in, run to a defined point, read memory and
registers out, and get a verdict as *data*. It is not a better emulator, it is
an emulator with a machine-readable surface.

It ships an assembler (`lwasm`, patched), so a repo pulled from here is a
complete edit → assemble → run → observe loop for 6809/6309 work.

Humans may also find many features useful. Read on, but this document is
mostly LLM-speak optimized for LLMs.

## What you get

| Capability | How |
|---|---|
| Scripted input, no keyboard | `-input-script`, a text file of move/click/type/wait steps |
| Stop at a defined point | `-trap pc=`, `xpc=`, `seconds=`, `cycle=`, `read=`, `write=` |
| Get state out | `-trap-snap`, `-trap-screenshot`, `-trap-history`, gdb target |
| Get data in | `-load-ram`, `-trap-load` (raw / DECB / hex / SREC) |
| Guest talks to host | `emuext` opcodes: the running program can log, assert, request a screenshot |
| Physical, not logical, addressing | GIME MMU translation everywhere it matters (see below) |
| Symbols instead of numbers | `-symbols FILE`, then `@Name` anywhere an address is accepted |
| Crash at the first wrong access | `-protect-mode`: code/data policy inferred from the build; report + RAM dump + exit 70 |
| A real call stack | `-backtrace` shadow call stack; `-trap-backtrace` dumps it, and every protect fault prints one |
| Odd input hardware | `-rat-mouse`, the Diecom RAT quadrature mouse, driven by any port source |
| Determinism | `-disk-fast`, `-trap-ratelimit-off`, clean signal handling |

[PATCHES.md](PATCHES.md) explains all 49 patches and what each one buys you.

## An important consideration for MMU systems

**Prefer physical addresses to logical ones.** A 16-bit `$XXXX` on a banked
machine is ambiguous: the same logical address is a different byte depending
on the MMU state at that instant, and the MMU changes constantly. An agent that
reasons in logical addresses will be wrong intermittenty.

This fork provides physical equivalents throughout: `xpc=` instead of `pc=`,
flat physical RAM peek/poke, physical watchpoints, a physical access audit map.
Use them by default. Logical addressing is correct only when you already know
the mapping and can prove it.

## Core instructions for an agent working here

1. **Read `AGENT-NOTES.md` before writing a patch or debugging a build.** It is
   short, and every rule in it was paid for.
2. **Build once, from `BUILD.md`.** macOS, Windows, Linux and headless are all
   documented; the headless build is the one to use for automation. Nothing is
   installed. The binaries stay in their build trees and you name them
   explicitly, which is what BUILD.md's "Running it" section covers. **You also
   need firmware ROMs**; that section says where they go.
3. **The structure is tarball + patch series, on purpose.** See below.
4. **When something misbehaves, suspect the harness before the emulator.** A
   scripted run has many more ways to be wired wrong than the emulator has to
   be broken.

## Recipes

Assume a headless build, a symbol file from `gensym.py`, and a disk that
autoruns the program under test.

### Time a region of code

```bash
bin/xroar-dev -machine coco3 -load-fd0 test.dsk -symbols out.sym \
    -trap 'xpc=@RenderStart' -trap-cycle-reset \
    -trap 'xpc=@RenderEnd'   -trap-cycle-print -trap-cycle-master
```

```
[trap-cycle-print] master=286363 cycles=17897 pc=0x8C4A addr=0x0000 value=0x00
```

Divide `master` by 14,318,180 for seconds: here, 20ms, one frame. **Use
`master`, not `cycles`, for anything you will convert to time**: a CPU cycle is
16 master ticks at 0.89MHz and 8 at 1.79MHz, so a `cycles` figure spanning a
speed change means nothing. Run it under `-headless`; the counts are
cycle-exact regardless of how fast the host chews through them.

Measure first and write the number down. "It feels slow" and "it copies 18,886
bytes per frame, 33ms, a 30fps ceiling" lead to different decisions, and the
second one also tells you when to stop.

### Find what is overwriting memory it shouldn't

The classic: something walks off the end of a buffer, or a routine's stack
frame gets clobbered, and the crash surfaces somewhere unrelated. Watch the
region and record how you got there.

```bash
bin/xroar-dev -machine coco3 -load-fd0 test.dsk -symbols out.sym \
    -trap-history 64 \
    -trap 'write=@StackTop-@StackLimit' -trap-byte hits.log -trap-snap crash.sna
```

`-trap-history N` keeps a ring of the last N executed **physical** PCs and dumps
it on any trap, so you get the path that led in rather than just the
instruction that did it. `-trap-byte` appends the offending byte to a file, so a
run that trips a hundred times leaves a readable list instead of a wall of
console noise.

If the first few writes are legitimate, a routine that initialises the region
before anyone should touch it, skip them rather than staring past them:

```bash
    -trap 'write=@Buffer-@BufferEnd' -trap-range 4 -trap-trace-n 200
```

acts only from the 4th trigger and turns on an instruction trace for 200
instructions from that point.

### Crash at the first wild access, not thirty seconds later

The watchpoint recipe above needs you to already suspect a region. Protected
memory mode needs nothing but the symbol file: the assembler listing says
which bytes are instructions and which are initialized data (gensym format 2's
`M C` and `M D` records — nothing is tagged in source), and the run dies the
moment the program treats code as data or data as code.

```bash
bin/xroar-dev -machine coco3 -load-fd0 test.dsk -symbols out.sym \
    -protect-mode -trap-history 64
```

Four checks, each its own flag when you want fewer: `-protect-mode-stack`
(an S-derived write landing where no stack belongs), `-protect-mode-write`
(any other write into code), `-protect-mode-read` (data read from code, the
classic wild pointer), `-protect-mode-data` (instruction fetch from non-code —
the PC jumped into the weeds). A named flag wins over `-protect-mode` from
either side, so `-protect-mode -no-protect-mode-read` is "everything but
reads".

The stack check has three ways to fire and the report says which. An S-derived
write onto **code** ("stack hit code") or onto **initialized data** ("stack hit
data") is one; the second needs no configuration, because `rmb` reservations
emit no listing bytes and so are in no `M D` range — a stack living in reserved
space, which is every stack, cannot trip it. The third is
`-protect-stack-floor ADDR` (physical, `@names` accepted): a stack that has
simply grown too far down, through reserved space nothing can classify, is
caught by the one number the build output cannot supply. Beware the classic
6809 trick of using `pshs`/`puls` or S-relative indexing as a fast data
pointer: that is an S-derived write too, and it will fault. `-protect-allow
LO-HI` over the region is the escape hatch.

A violation prints the faulting access with symbol and `file:line`, the
registers, the flight-recorder ring, a symbolized call-stack backtrace, dumps
flat RAM (`-protect-dump FILE`, default `protect-crash.ram`), and exits 70 —
distinct from exit 1 so a harness can tell "the program went wild" from
"xroar was misconfigured". Checks stay dormant until the first fetch of
mapped code, so the BASIC boot cannot false-trip them. `-trap-protect-on`
moves that start line — the checks then begin at *its* trap and nowhere
earlier, so `-trap 'xpc=@ToolEntry' -trap-protect-on` really does ignore
everything before `ToolEntry` — while `-trap-protect-off` stops them at its
trap, and `-protect-allow LO-HI` exempts bytes a program patches on purpose. Self-modifying code will trip `-protect-mode-write`
by design — that is what the exemption is for.

### Who calls this routine?

```bash
bin/xroar-dev -machine coco3 -load-fd0 test.dsk -symbols out.sym \
    -trap 'xpc=@DrawBrush' -trap-backtrace -trap-timeout 1
```

```
backtrace: 3 frame(s), innermost first:
backtrace: #0  in  0x74015 (DrawBrush (RESIDENT)) <- 0x74011 (ToolDown+4 (RESIDENT)) main.asm:412 s=3EFC
backtrace: #1  in  0x74011 (ToolDown (RESIDENT)) <- 0x7400C (MainLoop+12 (RESIDENT)) s=3EFE
```

The shadow call stack watches JSR/BSR/LBSR and interrupt entries as they
happen and unwinds by S, so it needs no frame pointers and survives `puls pc`
and LEAS tricks. It arms itself under `-protect-mode` or any
`-trap-backtrace`; `-backtrace` arms it alone, `-no-backtrace` keeps it off.

### See where the time actually goes

```bash
bin/xroar-dev -machine coco3 -load-fd0 test.dsk -symbols out.sym \
    -profile-range blit=@BlitStart-@BlitEnd \
    -profile-range fill=@FillStart-@FillEnd \
    -profile-hist 20 -profile-out profile.txt \
    -timeout 30
```

Ranges are physical and repeatable, so you can carve a program into named
regions and get fetches and cycles for each. `-profile-hist N` adds the top N
individual physical PCs by cycles, which is how you find the one instruction in
a loop that costs more than the rest of the routine.

### Find code that never ran

```bash
bin/xroar-dev -machine coco3 -load-fd0 test.dsk \
    -trap 'xpc=@MainLoop' -trap-audit-clear -trap-audit-on \
    -trap 'seconds=60'    -trap-audit-write audit.bin
XROAR_DEV_OBJ=obj python3 tools/auditreport.py audit.bin --uncovered main
```

The audit map records every physical byte the CPU touched. Clearing it at the
start of the region of interest is what makes the result mean "during the
test" rather than "since boot". The boot path otherwise lights up half the
map. `auditreport.py` turns the map into a list of routines that were never
executed, which is the honest version of "I think that code is dead."
(It finds the symbol file itself: `XROAR_DEV_OBJ` and `XROAR_DEV_PROJECT`
locate the `.sym`/`.lines` pair; see `tools/README.md`.)

`--diff OLDMAP` is the other half of this: run the map twice with different
input and report what the second run reached that the first did not. That
answers "did my new test case actually exercise anything new", which is a
question guessing is very bad at.

### Get state out at a defined point

```bash
bin/xroar-dev -machine coco3 -load-fd0 test.dsk -symbols out.sym \
    -trap 'xpc=@Failure' \
        -trap-snap fail.sna -trap-ram fail.ram -trap-screenshot fail.png \
        -trap-timeout 1
```

One trap, four actions. `-trap-ram` writes **flat physical RAM**, every bank,
including ones not currently windowed into any logical address, which is
exactly what a logical dump cannot give you. `-trap-timeout 1` quits a second
later so an unattended run does not sit there forever.

### Drive it, repeatably

```bash
bin/xroar-dev -machine coco3 -load-fd0 test.dsk \
    -input-script run.txt -init-delay 0.35 \
    -timeout 120
```

`run.txt` is plain text: `wait`, `type`, `move`, `down`, `up`, `key`. The
`-init-delay` matters more than it looks: a headless run is otherwise
*perfectly* repeatable, so a program that seeds anything from a timer at first
input gets the same value every single time. Add `-init-delay-random` to put the
variation back; the delay it draws is logged, so a run that finds something can
be repeated with `-init-delay-seed`.

### Let the guest talk

```bash
bin/xroar-dev -machine coco3 -load-fd0 test.dsk -emuext
```

With `-emuext`, `lwasm`'s emulator-extension opcodes are live and the 6809
program itself can log a string, assert a named condition, or request a
screenshot. This is the guest reporting, not the host inferring, and worth reaching
for before building an elaborate external observation rig.

### Attach a debugger

```bash
bin/xroar-dev -machine coco3 -load-fd0 test.dsk -gdb -gdb-pseudo-regs
```

Then from gdb, alongside the usual: `qxroar.xpc` for the physical address the
live PC maps to right now, `qxroar.block` and `qxroar.task` for the MMU state,
`qxroar.physmem:ADDR,LEN` to read flat physical RAM past the MMU entirely.
`-gdb-pseudo-regs` is upstream's and exposes SAM/PIA/GIME as named registers.

## Why tarball + patches, and not a git fork

Because it makes the fork *legible and extensible to a model*.

- Every change is a **named, self-contained, individually readable unit** with
  a commit message explaining why it exists. A model asked to add a feature can
  read the twenty patches nearest to it and imitate the house style exactly.
- The **upstream tarball is pristine**. There is never a question about what is
  ours and what is XRoar's. The answer is "every patch in `xroar/`".
- **Adding a capability means adding a patch**, which is the same shape as
  every existing one. A model that has read `PATCHES.md` can write patch 50
  without understanding the whole tree.
- Rebasing onto a new XRoar release is a defined, mechanical job: re-apply the
  series, fix what conflicts, regenerate. Not a merge no one can review.

The cost is that you must never hand-edit a diff. See `AGENT-NOTES.md`, which
explains how to generate one instead. That single rule is what keeps the
structure workable.

## Host-side tools

The emulator can *consume* a symbol file; something has to write one. `tools/`
is the host half, four dependency-free Python scripts:

| Tool | Pairs with | What it does |
|---|---|---|
| `gensym.py` | patches 41, 47 | Builds the `.sym` / `.lines` pair from `lwasm` output — `@MainLoop` instead of an address that moves every build, plus the code/data map `-protect-mode` enforces |
| `symread.py` | n/a | Reads that pair from Python: name → physical, and back |
| `auditreport.py` | patch 37 | Turns the binary access-audit map into "these routines were never executed" |
| `decb.py` | n/a | Creates and manipulates RS-DOS disk images |

They came out of a working 6809 project and were generalised; two environment
variables configure them for yours. See `tools/README.md`.

## Layout

| Path | Contents |
|---|---|
| `xroar/` | Pristine XRoar release tarball + the patch series, applied in filename order |
| `lwasm/` | lwtools tarball + its own patch series |
| `tools/` | Host-side Python tools (see `tools/README.md`) |
| `build/` | One script per platform, plus the acceptance and option-audit checks every build ends with |
| `.github/` | CI: every build path above, on every push |
| `BUILD.md` | Build instructions, all platforms |
| `PATCHES.md` | Every patch, what it does, and what it buys an agent |
| `CHANGELOG.md` | The shape of the history, high level |
| `MACHINE-COVERAGE.md` | Which machines each feature works on, and what is coco3-only on purpose |
| `AGENT-NOTES.md` | Rules and traps, written for whoever automates this next |
| `6809-NOTES.md` | 6809/6309 and lwasm traps: the things the datasheet states plainly and everyone gets wrong anyway |
| `COPYING` | GPL-3.0, for XRoar itself |
| `LICENSE.MIT` | MIT, for the patch series |

## License

XRoar itself is GPL-3.0-or-later (`COPYING`). Anything built from this repo is
a derivative of it, so the resulting binary is GPL-3.0-or-later as a whole,
whatever the individual patches say.

lwtools is GPL-3.0; its own copy of the text ships inside the tarball as
`lwtools-4.25/GPL3`.

Every patch in this series (`xroar/`, `lwasm/patches/`) and the scripts in
`tools/` are MIT (`LICENSE.MIT`). That file is the license text and nothing
else, so GitHub can recognise it; this paragraph is what it applies to.

This fork is unaffiliated with upstream XRoar and lwtools. Please report bugs
here unless you have reproduced them on stock XRoar or lwtools first.

Imagine the chaos Boisy will create with a tool like this.
