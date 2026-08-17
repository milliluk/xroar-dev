# Host-side tools

Five Python tools, no dependencies beyond the standard library. They are the
host half of features the patch series adds to the emulator. The emulator can
consume a symbol file, but something has to *write* one.

| Tool | Pairs with | What it does |
|---|---|---|
| `gensym.py` | patches 41 (`-symbols`), 47 (`-protect-mode`) | Builds the `.sym` / `.lines` pair from `lwasm` listings and maps |
| `symread.py` | n/a | Reads that pair from Python: name → physical address, and back |
| `auditreport.py` | patch 37 (`-audit`) | Turns the binary access-audit map into a readable coverage report |
| `decb.py` | n/a | Creates and manipulates RS-DOS (Disk BASIC) disk images |
| `gen_mnemonics.py` | `gensym.py` | Regenerates gensym's instruction/directive sets from the vendored `lwasm`'s own table |

## Configuring them for your project

Environment variables, because these came out of a specific codebase and
should not assume yours:

    XROAR_DEV_PROJECT        basename of the .sym/.lines pair (default: program)
    XROAR_DEV_OBJ            where build output lives (default: <repo>/obj)
    XROAR_DEV_RESIDENT_MAP   gensym only: the map file holding direct-page
                             records (default: RESIDENT.map)
    XROAR_DEV_ROOT           symread only: repo root, if the tools are not
                             being run from inside the checkout

Both the writer (`gensym.py`) and the reader (`symread.py`) resolve the object
directory through `XROAR_DEV_OBJ`, so pointing it somewhere moves both ends
together. `gensym.py --obj` overrides it for one run, and `--out`/`--lines`
override the individual output paths outright.

## gensym.py: the symbol file

    python3 tools/gensym.py --obj obj/

Writes `<project>.sym` and `<project>.lines`. Feed the first to the emulator:

    xroar -symbols obj/program.sym ...
    xroar -trap xpc=@MainLoop -trap-snap out.sna ...

**Why this is worth having.** Without it you write physical addresses you
re-derived by hand from a listing, and they move every build. With it you write
`@MainLoop` and stop caring.

**Why it is one file and not three.** It emits *physical* addresses, direct-page
variables and assembler equates in one format, because the alternative, a tool
per kind, produced tools that disagreed. The specific failure it was built to
end: a name defined in both a resident dispatch thunk and the overlay that
implements it resolved to whichever map sorted first, so a breakpoint by name
stopped in the dispatcher, one indirection short of the code you meant, and
nothing warned. The format keeps the ambiguity instead of resolving it: a name
defined twice appears twice, and `symread.py` refuses a bare lookup that
matches more than one image, naming both candidates. Silent wrong answers
became loud errors.

`.lines` maps a physical address to `file:line`. Loaded alongside, the gdb stub
reports source positions and the flight recorder names routines.

**Format 2 adds `M C` code ranges and `M D` data ranges** — every byte an
instruction mnemonic emitted, and every byte a data directive emitted,
classified from the listing itself, so an `fcb` table between two routines is
correctly `D` and not `C`. They are what `-protect-mode` (patch 47) enforces;
a format-1 file still loads for everything else, but protect mode refuses to
arm without the `C` ranges. Nothing is tagged in source to make this work.
A warning lists bytes that unrecognised emitters (unexpanded macros, mostly)
left in no class: executing those trips `-protect-mode-data`.

**`M D` means *initialized* data and nothing else, and that is load-bearing.**
An `rmb` reservation emits no bytes into an `lwasm` listing — the line has an
address column and no emit column — so the classifier never sees one and no
`M D` range can cover reserved space. A program's own stack, buffers and
scratch all live there, which is why `-protect-mode-stack` can fault on a
stack that has wandered onto declared data without faulting on every program
whose stack sits in an `rmb` block, i.e. all of them. Measured on Color Max
Deluxe: 445 `rmb`-declared labels, none inside any `M D` range. If that ever
stops being true, the stack check will cry wolf on every push, and the first
thing to check is whether the assembler started emitting fill bytes for `rmb`.

## symread.py: reading it back

    import symread
    S = symread.load()
    S.phys('MainLoop')                    # -> physical address
    S.phys('OVERLAY6.link.ram:ClipCopy')  # when the bare name is ambiguous
    S.whereis(0x0AB72)                    # -> 'LoadFont+3 (OVERLAY5...)'

`load()` checks the `.sym` against the build that produced it and complains if
it is stale. A symbol file that silently describes the previous build is worse
than none, because half the names still resolve.

## auditreport.py: what the run actually touched

    xroar -audit map.bin,build=$(git rev-parse --short HEAD) ...
    python3 tools/auditreport.py map.bin --uncovered OVERLAY6

The map is one byte per physical address, one bit per access class: read,
written, executed. Raw it is two megabytes of hex; with a `.sym` file it becomes
"these routines were never executed", which is the question worth asking.

`--diff OLDMAP` compares two runs, so you can ask what a change actually
reached. `--uncovered IMAGE` restricts the report to one image.

Maps accumulate: `-audit out.bin,seed=out.bin` unions this run into the last,
so coverage can be built across many runs. Seeding refuses a map whose legend
version differs and warns across build ids, rather than unioning addresses that
mean different things.

## decb.py: disk images

    python3 tools/decb.py dir disk.dsk
    python3 tools/decb.py copy disk.dsk file.bin,FILE.BIN
    python3 tools/decb.py kill disk.dsk,FILE.BIN

Creates and manipulates RS-DOS disk images. Needed because getting a program
onto a virtual floppy is otherwise a manual step, and an automated run cannot
have manual steps. `--help` lists the full command set.

## A note on provenance

These were extracted from a working 6809 project and generalised. If one of
them assumes something about your build layout that is not configurable
through the variables above, that is a bug in the generalisation. The
assumption is not required, it is a leftover.
