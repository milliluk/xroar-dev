# 6809 / 6309 and lwasm: the traps

This is not a tutorial and not a datasheet. It is the list of things the
datasheet states plainly and everyone gets wrong anyway, plus the lwasm
behaviours that are documented nowhere.

Every entry below cost a real debugging session on a real 6809/6309 project.
Where a rule reads as pedantic, that is what it looked like before it cost the
session.

The unifying shape of almost everything here: **the assembler accepts it, the
program runs, and the wrong thing happens somewhere else, later.** None of
these are build errors. That is why they are worth writing down and the ones
the compiler catches are not.

---

## Flags: what does not set them

**`LEAX` and `LEAY` set Z. `LEAU` and `LEAS` set nothing at all.** That
asymmetry is deliberate (s and u are stack pointers; adjusting one should not
disturb a flag you are branching on) and it catches everybody twice:

- `leau -1,u` / `bne` is a loop that never terminates on its count. It branches
  on whatever Z was left by something else entirely. If the counter must live
  in u, pair the `leau` with an explicit `cmpu` and surrender the byte u saved
  you.
- `leax -5,x` / `bpl` is worse, because LEAX *does* set a flag, just not that
  one. Z only: not N, not C, not V. The `bpl` reads a stale N from whatever
  came before and the branch is decided by an unrelated instruction. Seen in a
  bounds clamp that "worked" for months because a too-wide bound only made a
  walker walk farther.

A sign branch immediately after any `lea` is worth a hard look. So is any
`bcc`/`bcs` after one, which can never be right.

The reason counters end up in u in the first place is that **u is the cheaper
pointer**: y's load/store/compare forms carry a $10 prefix, so `LDY`/`STY`/`CMPY`
are a byte longer and a cycle slower than the u forms. When both registers are
free, u is the right pointer, right up to the moment you decrement it and
branch.

**`TFR` and `EXG` set no condition codes.** A `tst` after a `tfr` is not
redundant. The pattern that bites is:

    tfr x,a
    bpl @positive
    nega

which is correct only if the instruction *before* the `tfr` happened to leave
flags matching a's new value. It usually does, because the instruction before is
usually arithmetic on the same data, so the idiom reads as safe and mostly is.
One instance in a tree of many sat after a `clr` of an unrelated cell, which
always leaves N=0, so `bpl` always branched and `nega` never ran. A value of
-62 got squared as its unsigned byte, 194, and the pixel it decided landed
nowhere near where it belonged. Every other instance in the same file was
preceded by a real arithmetic op and was fine, which means finding it took
checking each occurrence by hand; the fix did not generalise.

**`INC` and `DEC` never touch carry**, on either CPU. That is a feature: a
`decb`/`bne` loop nests inside a `ror` carry chain with no cc juggling at all.
Written as `incb`/`cmpb` it does not, and the difference was most of a shift
routine's stack traffic.

On the 6309, **`DECE` and `DECW` do set Z** and make honest loop counters,
subject to the aliasing rule below.

---

## Indexed addressing

**Accumulator-offset indexing sign-extends.** `a,r` and `b,r` treat the
accumulator as two's complement, so a value of $80 or more indexes *below* the
base, not into the top half of a 256-byte table.

The canonical incident: an LZSS encoder's `prev[]` table, indexed by a position
low byte that is 0..255 by construction, written with `stb a,x`. The first
position with a high low byte wrote 128 bytes under the table, which was the
application stack, which held a live return address. The routine returned into
a blank page and the CPU marched through it executing $FF until something else
noticed. The read side had the same bug, and both had been reviewed against a
Python model, where a negative index is legal and means something else.

The fixes, in order of preference:

- **`ABX` is the only unsigned register-to-index add on the 6809.** If b holds
  a raw byte, use it.
- **`d,r` with a known-positive 16-bit index is safe**, which is why the
  `clra` / `ldb` / `lslb` / `rola` lookup shape works and a raw `b,x` does not.
- Bias both sides: `eorb #$80` against a base moved up 128.

**The auto-increment amount comes from the postbyte, never the operand size.**
`ldq ,x++` advances x by *2*, not 4: `,x++` is a mode, and there is no
`,x++++`. Every row of a tile-drawing loop written that way reads overlapping
source data and renders garbage. The shape that works is `ldq ,x` followed by
an explicit `leax 4,x`.

**Bracket-indirect through a hardware register is a read with side effects.**
`jsr [SomeVector]` where the vector can point into $FF00-$FFFF will *read* that
address, and PIA and GIME status reads eat pending interrupts. Range-check
before dereferencing anything that could hold an I/O address; do not rely on
the dereference being "just a fetch."

---

## The 6309 register file overlaps

d = a:b. w = e:f. q = d:w. Every alias is a loaded gun, and the assembler
cannot see any of them.

**Never carry a loop counter in w while touching e or f.** `lde ,y+` rewrites
w's high byte. A font expansion loop counted in w and read source bytes through
e; a source byte of $FF made w $FFxx every pass, the `decw`/`bne` exit could
never fire, and the store pointer marched 64K into the I/O page. The crash
write at $FF69 *was* the loop, several thousand iterations after the mistake.
If you need the byte twice, put it in a scratch cell and keep `decw`.

The same rule the other way: `ldq` clobbers d and w at once, and `tfr` chains
(`d,w` / `f,a` / `a,b`) are powerful in exactly the way that makes a dropped
link invisible. One missing `tfr d,y` in a variant block, where the main block was
register-perfect and the edge-case variant nobody reads was not, took a day. When
you translate a block to the 6309, **diff the register lifetimes line by line,
not the shape of the code.**

**`MULD` is signed 16x16 into q; `MUL` stays unsigned 8x8 into d.** q stores as
big-endian d:w, which happens to match a sane 32-bit product layout, so
`muld ,u` + `stq Product` drops straight in for a software multiply and is
roughly ten times faster. The catch is signedness and in-place operand
mutation: check every caller of the routine you are replacing, because software
multiplies frequently destroy their operands and callers frequently rely on it.

**`TFM` leaves w = 0 and both pointers advanced.** Reload w per row if the loop
reuses it. It is interruptible and resumes itself, so do *not* "protect" it
with `orcc`; that only adds latency to everything else. Direction matters:
`tfm u+,x+` with the destination overlapping the source by less than the
pattern length is the idiomatic replicate-a-seed fill, about a cycle a byte.
In-place left-to-right transforms are safe only when byte N+1 is read before it
is overwritten, say *why* in a comment, or the next rewrite will break it.

**v and the zero register are inter-register only** (`tfr`, `exg`, `addr`, and
friends). v as a loop-invariant 16-bit constant is the idiomatic win: keep a
mask or an ink word in v and blend with `eorr` / `andd` / `eord`. Operand order
is **r1 = r1 op r0**: `eorr v,d` means `d ^= v`, `addr e,a` means `a += e`,
`subr f,b` means `b -= f`. Get one backwards and a blend silently inverts.

---

## Native mode changes timing, not just speed

6309 native mode is not a uniform speedup, and code that measures the outside
world will notice. Extended addressing loses a cycle (`ldb $FF00` is 5 cycles
on the 6809 and 4 native), so a routine that samples a settling curve at
carefully chosen offsets samples *different points* after the port. Four of ten
blocks in one joystick scanner collapsed onto duplicate positions that way and
the axis read a count off, for months, with no other symptom.

For padding, use primitives whose timing you have checked in both modes. This
table was verified against lwtools 4.24 `lwasm` with `opt c,ct`, and covers no register,
memory, stack or cc effects:

| Instruction | Opcode  | 6809 | 6309 native |
|-------------|---------|-----:|------------:|
| `nop`       | `12`    | 2    | 1           |
| `brn *`     | `21 FE` | 3    | 3           |
| `leau ,u`   | `33 C4` | 4    | 4           |
| `leau 0,u`  | `33 40` | 5    | 5           |
| `tfr x,x`   | `1F 11` | 6    | 4           |
| `exg x,x`   | `1E 11` | 8    | 5           |

**`brn *`, `leau ,u` and `leau 0,u` are mode-neutral; `nop`, `tfr x,x` and
`exg x,x` are not.** In mode-sensitive code, build your delays out of the first
three, or write per-build variants behind the CPU guard. In 6309 native code
`brn *` is strictly better than `nop nop nop`: the same three cycles in two
bytes instead of three, and unlike the nops it stays three cycles if the build
ever goes back to emulation mode.

---

## Hand-built opcodes: the fcb skip idiom

Two entry points that load different immediates and share a tail get written as
a skip chain: the first entry loads its value, then a bare `fcb` swallows the
next entry's load as an operand.

**`fcb $8C` (`cmpx #`) swallows exactly TWO bytes.** In front of a 3-byte
`ldx #`, the third byte *executes*, as whatever opcode it happens to be. Use
**`fcb $10`** in front of a 3-byte load: the $10 prefix turns `8E xx xx` into
`ldy #xxxx`, which is harmless whenever y is dead.

Getting this wrong does not crash. In the case that produced this note, the
corrupted flow fell through, set its result flag, stored its attributes, and
drew nothing at all. Every readback said success and only a rendered frame
disagreed. Screenshot anything that "works" near hand-built opcodes.

---

## lwasm

**A blank line resets `@`-local label scope.** lwasm scopes reusable locals
(`row@`, `next@`) between non-local labels, and a bare empty line counts as one
of those boundaries. A local defined before a blank line is invisible after it,
so the reference dies as an *undefined symbol*, for a label plainly visible
two screens up, or, worse, silently binds a same-named local in another block.
For vertical space inside a routine that uses `@` locals, put a `*` or `;` in
column 1. Never an empty line. This is the single most common lwasm question
and the answer is always this.

**Turn `6800compat` off.** It lets 6800 mnemonics through silently. Some are
harmless, since `inx`/`dex` are 1:1 with `leax`, but `aba` expands to
`pshs b` / `adda ,s+`: twelve cycles and a stack touch you did not ask for,
inside what you believed was a one-instruction line. One shipped in a hot
blender loop. Without the pragma the same line is a build error, which is what
you want. Write the native form always: `leax 1,x`, `leas 1,s`, and `addr b,a`
(6309) or the explicit `pshs`/`adda` pair (6809).

Keep `6809conv` / `6309conv`. Those are essential convenience ops (`clrd`,
`lsrd`, `asrd`...) that assemble native on the 6309 and expand to the documented
two-op pair on the 6809. Their expansions are cycle-honest, and a dual-CPU
build depends on them.

**`opt nolist` used to remove symbols from `--map` and `--symbol-dump` too.**
Stock lwasm flags any symbol defined while `nolist` is current and then skips it
when writing the map. Put your equates file behind `opt nolist` to keep its bulk
out of the listings, the obvious thing to do, and every symbol it defines
silently vanishes from the machine-readable artifacts. In one tree that meant no
direct-page variable had *ever* appeared in a map, for years, and the only way to
get a DP offset was to scan listings for an instruction that referenced one.
`lwasm/patches/01` in this repo fixes it: `nolist` governs the listing, and the
map and symbol dump ignore it. Emitted bytes are unchanged (verified by sha1
across the whole change). **A tree built with stock lwasm resolves fewer names
and nothing says so**. If a symbol you know exists is missing from a map, ask
what the listing state was where it was defined before you suspect anything
else.

**`macro noexpand` is a lister flag, and it blinds every listing-based tool.**
It inserts markers that collapse an expansion onto the invocation line and
touches nothing lwasm emits (binaries are byte-identical either way). What you
lose is per-line addresses, bytes, cycle counts, and *substituted* operands,
such as `addw <Temp2+2` instead of `addw \1+2`. Any linter, cycle counter or auditor
that reads listings goes silent on those sites without saying so. Turn it off
unless a listing is genuinely unreadable.

**A macro whose body contains `ifne \1` cannot be *defined* inside a false
`ifdef`.** The skipped-region scanner still parses conditionals and dies on the
bare parameter. Define such macros above the CPU guard.

**The raw output format pads reserved space.** An `rmb 2048` in a module you
assemble to a raw image is 2048 real bytes on disk and real sectors at load
time, even though nothing has written them. To reserve without paying for it,
reserve by address: an `equ` plus an `org` step, with a guard (`iflt *-Base`)
so code cannot start below it.

**DECB output emits a segment per contiguous run and skips `rmb` gaps.** A
loader that assumes one contiguous block will load a sparse file wrong. Related:
**the map omits `rmb` labels entirely**, so a tool that resolves variables from
the map cannot see any variable declared in an `org`/`rmb` block. Walk the
listing for those, and verify the walk against emitted operands; an unnamed
`rmb 2` spacer shifts everything after it and nothing warns you.

**An `equ` defined while a section is open is tagged to that section**, and the
link map reports its value with the section base added. This looks exactly like
a bug and is not one: lwasm resolves an absolute equate at assembly time, the
operand needs no relocation, and the emitted byte is correct. Settled by
building both ways and diffing the linked images: byte identical. Moving such
equates above the first byte-emitting line is a legibility fix and keeps the map
honest; do not chase it as a defect, and do not trust a lint that reads the link
map to tell the difference.

**`setdp` does not choose an addressing mode**. An explicit `<` or `>` prefix
does. It tells the assembler which page a *bare* operand belongs to, so only
operands with no prefix, resolving into the direct page or into low page zero,
depend on it at all. Everything else is extended either way. lwasm also refuses
`setdp` for an object target, so any tree heading for `lwlink` has to lose it.
If you audit for the sites that care, classify by address, not by emitted form:
the only ones that matter are bare operands resolving into the page `setdp`
names (direct with it, extended without) or into page zero (extended with it,
direct without, and direct there reads a different byte, silently). Auditing by
diffing two assemblies does not work, because removing `setdp` lengthens instructions,
that pushes some branch out of range, the assembly errors, the listing truncates,
and every site past the error becomes invisible while the tool reports a
confident number.

**`set` exists**: a redefinable symbol, like `equ` but assignable again. It is
in the instruction table and not in `docs/lwasm.txt`. It is the whole trick
behind self-numbering tables: a `.inc` of one-line macro calls, included once
to compute equates and again to emit the vector table, where the offset *is* the
position and nobody ever writes a number.

**A failed assembly leaves the previous run's listing and map on disk.** lwasm
writes no map when assembly fails, and the stale `.lst` from the last good build
sits there looking current. Any tool that lints listings will happily report
clean on a build that did not happen. Read the assembler's exit code first,
every time; a green lint on its own proves nothing.

---

## CoCo 3 hardware: the GIME registers are write-only

`$FF90-$FF9F` cannot be read back. The only exceptions are `$FF92`/`$FF93`, the
interrupt status registers, and **reading those clears them**, which is its own
trap for anything that wants to peek at pending interrupts without consuming
them.

Everything else (INIT0, INIT1, the video mode and resolution registers, the
border, the video offset) returns the floating bus, which in practice is the
low byte of the address the read itself put there. `lda $FF91` comes back `$91`,
on hardware and in XRoar alike (`tcc1014.c`: "none of the other registers in
this region are readable").

That is a live trap, not a curiosity. A GIME revision detector read INIT1 to
build a "shadow", cleared one bit in it, and wrote the shadow back. The shadow
was `$91`. Bit 0 of INIT1 is TR, the MMU task select, and `$91` has it set, so
the detector quietly moved the machine onto MMU task 1, whose map nothing in
that program maintained, and every subsequent bank operation wrote through a
stale map into the wrong physical blocks. It stayed invisible for nine days
because the next routine to run happened to write a correct INIT1 for unrelated
reasons. Deleting *that* routine is what exposed it.

So: **never read a GIME register to find out what you wrote.** Write the value
you mean, from a named constant, once, at init. A shadow is a variable the
application maintains, not a read-back. And an invariant that holds only because
of what happens to run next is not an invariant. The code that needs the machine
in a state should be the code that puts it there.

---

## Two notes specific to this fork

**`emuext` `LOG` takes its argument on the next line.** `LOG label` is not valid
lwasm syntax for that opcode; write `LOG` alone, then `fdb label`. The inline
form assembles to just the two-byte opcode with no pointer, so the runtime reads
the *next instruction's* bytes as the format-string address and the probe
silently does nothing. Twenty minutes went into a "why is my probe not firing"
hunt that `-h` and the reference doc would each have ended immediately.

**Measure cycles with the trap harness, never by adding up instruction times on
paper.** `-trap-cycle-reset` / `-trap-cycle-print` around a region gives an
exact number in one run (see README's timing recipe), and the number is exact
under `-headless` regardless of how fast the host chews through it. Paper
estimates on this CPU are wrong in both directions, because indexed modes and the
native/emulation split see to that, and a paper review that "passes" a loop is
not evidence, as the register-aliasing story above demonstrates.
