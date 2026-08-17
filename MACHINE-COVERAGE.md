# Which machines each fork feature actually works on

Audited 2026-08-10 at 48 patches; the eight silent failures that audit found
were fixed 2026-08-11, each folded into the patch that introduced the feature
(AGENT-NOTES rule 1a), not appended as fixups.  **Re-audited 2026-08-17 at 49
patches**: patches 46 and 49 added since, and every claim below re-checked.
The question the audit asks: what is coco3-only that should not be?  The
Dragon family (dragon32/64/pro, coco, deluxecoco) shares `src/dragon/dragon.c`
and the same 6809 core as the CoCo 3, so anything that needs only `mem_class` / `get_sp` / `irq_depth` /
emuext opcodes could work there; the MC-10's 6803 core has none of those, so
most restrictions there are inherent.

**How far the evidence goes.**  `xroar/roms.tar.gz` carries CoCo ROMs only, and
patch 20 refuses to boot a machine without its essential BASIC ROM, so anything
claimed here is verified by running it on the **coco3** and on a **coco** (the
`dragon/dragon.c` path that the whole Dragon family shares), and by reading the
source plus the startup wiring everywhere else.  Dragon and MC-10 claims are
source- and startup-verified, never executed.  That boundary has not moved
since the first audit; it is the reason the MC-10 entries below say what they
say.

**The house patterns, for reference.** Degrade gracefully: `trap_fast_phys()`
returns the logical PC as the physical when there is no GIME (`xroar.c`), so
trap-history, the profiler, pbreak, physical watchpoints and the backtrace all
work on a flat-address machine.  Fail loudly: `protect_arm()` refuses any
machine but the coco3.  The failures worth fixing are the ones that do
neither: accept the option and silently do nothing.

## Fails silently today

Nothing known.  What this section used to list is below, under where each fix
landed.  Four conscious partials remain, listed at the end; the two added by
the 2026-08-17 re-audit are both MC-10 hardware the machine does not have, not
features that forgot a machine.

## Already correct

- **Degrades gracefully on every machine**: trap-history / `xpc=` /
  `xpcrange=`, `-profile-*`, physical watchpoints and pbreak (logical address
  stands in for physical on flat machines), backtrace (`bt_phys`), Dragon's
  `emustate` (answers `xpc`/`block`, refuses to fabricate `paddr`/`task`),
  and now `-audit` (patch 37): the Dragon family marks the map beside its
  physical-watchpoint checks with the same logical-as-physical stand-in, so
  a coco/dragon32/dragon64 run writes a real map instead of a well-formed
  all-zero file.
- **Fails loudly by design**: `-protect-mode` (coco3 only — its policy map is
  keyed on GIME physical addresses), backtrace on the MC-10 (6803 exposes no
  S/irq-depth), `-load-ram` / `-export-ram` / `-trap-ram` on a machine with
  no RAM part, `-serial-out` on a Dragon 32/64/Pro (patch 28: different
  serial hardware, so it warns instead of silently arming nothing), and a
  missing or CRC-invalid essential BASIC ROM on **every** machine (patch 20:
  the coco3-only Super ECB check now covers the Dragon family via
  `dragon_require_rom()` and the MC-10 inline — a machine that would execute
  garbage from its reset vector exits 1 instead of booting "healthy").
- **Genuinely machine-independent**: `-input-script`, screenshots,
  `-lp-file` (per-ROM addresses already fixed for both families),
  `dump_ram` on all three machines (the MC-10's, fixed in patch 16, now
  writes the flat space itself — banks in index order, absent banks
  zero-filled — so file offset == flat physical address, the property the
  coco3/dragon dumps always had; upstream's wrote RAM1's present banks in
  creation order 1,2,3,0, which matched no address space at all), emuext
  LOG/BREAK **and SHOT** on both
  6809 families (patch 18: the SHOT handler was never machine state; it lives
  in `debug.c` beside `emuext_log` and both families wire the delegate),
  `-serial-out` on the CoCo 1/2 / Deluxe CoCo (patch 28: same PIA1-A bit 1
  tap as the CoCo 3, config now `xroar_serial_out_*`), `qxroar.cycles`
  (patch 27: it was always a bare `event_current_tick` read; it just lived
  inside the GIME block), the gdb stop reply (patches 39/40/41: every machine
  sends a 'T' packet, so `watch:` and `sym:` reach a Dragon client; only the
  MMU pairs remain GIME-gated), the cycle counters on the MC-10 (patch 17:
  `xroar_cpu_cycles`/`xroar_master_cycles` are summed in `mc10_mem_cycle`, so
  `cycle=` traps and `-trap-cycle-print` count there too), and the flat-RAM
  lookup (patches 9/16/27/48: `-load-ram`, `qxroar.physmem` and the backtrace
  all resolve through one helper, `flat_ram_resolve` — part `RAM` on every
  machine but the MC-10, whose `RAM0`+`RAM1` pair spans as a single flat
  space, internal RAM at [0, size0) and the 16K expansion above it, absent
  banks staying honest holes.  Full MC-10 coverage, expansion included.  The
  parts were NOT renamed, and no rename was ever needed: their organisations
  differ (2K vs 4K banks), so they could never merge into one part — spanning
  was the real fix, renaming only ever cosmetic).
- **New since the last audit**: the CoCo Max Hi-Res Input Module and the X-Pad
  GT-116 (`-cart cocomax` / `-cart xpad`, patch 46) work on the CoCo 3 **and**
  on the whole Dragon family.  Both machines dispatch every CPU cycle through
  the cart's read hook with the full address (`coco3.c` and
  `dragon_cpu_cycle()`, which `dragonpro`, `immunity` and `deluxecoco` all
  delegate to), so the device is decoded wherever it sits: CoCo Max at `$ff90`,
  or at the documented hardware modification `$ff60` on a CoCo 3, where `$ff90`
  is a GIME register; X-Pad at `$ff60` on every machine, no modification
  needed.  Verified by running a 6809 probe that reads the port and LOGs what
  came back: on the coco3 and on a coco the axis tracks the input script
  (`7f` centre, `00`, `ff`), and the same probe built for `$ff90` reads a
  constant `1b` on the coco3 — the GIME register, not the cart, which is
  exactly why the relocation exists.
- **`-rat-mouse` (patch 49) is machine-independent by construction**: the RAT
  filter sits inside `joystick_read_axis()` (`joystick.c`), the one function
  every machine and every consumer reads an axis through — `coco3.c:1788`,
  `dragon/dragon.c:1632`, and the patch-46 carts alike.  Verified on both the
  coco3 and a coco through the CoCo Max path: with `-rat-mouse right` the port
  returns only the four RAT levels (`3e 62 aa 86`, MAME's `joy_rat_table[]`
  codes 15/24/42/33 presented as axis values) instead of tracking the source.
  The MC-10 is the exception, and inherently: see the partials below.
- **Genuinely inherent to the CoCo 3**: `qxroar.task/block/paddr/xpc`, the
  stop-reply MMU fields, DAT bank bits, and everything MC-10-side that needs
  the 6809 core — including `-audit`, whose bit composition reads the 6809
  core's access classification (`audit_bits_mc6809()` in `audit.h`); the
  6803 has none.

## Conscious partials (not silent, but not complete either)

What used to be the first entry here — the `RAM0` fallback reaching only the
MC-10's internal RAM — is gone: `flat_ram_resolve` (patch 9) spans the
`RAM0`+`RAM1` pair, so `-load-ram`, `qxroar.physmem` and the MC-10 `dump_ram`
now cover the whole machine.  It turned out full coverage never meant
renaming the parts: their organisations differ, so they could never merge,
and the snapshot format is untouched.

- `-audit` on the Dragon family marks only machines using the standard
  `cpu_cycle` path (coco, dragon32, dragon64).  Deluxe CoCo, Dragon
  Professional and iMMUnity override it and are not marked — exactly the
  machines where physical watchpoints already do not fire, so the audit map
  and the watchpoints keep one boundary.
- The MC-10 has no joystick hardware at all: nothing in `mc10.c` reads
  `joystick_read_axis`/`joystick_read_buttons`, because the machine has no
  analogue port to read.  So `-rat-mouse`, `-hires-joystick` and the joystick
  half of `-input-script` are all accepted there and do nothing.  This is
  inherent rather than an oversight, and it is listed as one item rather than
  four because no one of them can be fixed on its own — wiring a port the
  machine never had is the whole job.
- A `dragon-cart` on the MC-10 is dropped without a word: the machine takes
  `mc10-cart` parts only (`cart_arch`, upstream's mechanism), so
  `-cart cocomax` on an `mc10` produces no `[part:cocomax]` line and no
  warning.  Inherited by patch 46 rather than introduced by it, and it applies
  equally to every Dragon-family cart upstream ships.
- `-serial-out` on the MC-10 is still accepted and ignored: its 6803-driven
  serial is yet another arrangement, and nobody has asked for it.  Unlike the
  two MC-10 entries above it is not inherent — the machine does have serial —
  which makes it the one partial here that is a decision rather than a fact;
  wire it or make it warn if it ever matters.

## What the 2026-08-17 re-audit actually ran

Every one of these was executed against `bin/xroar-dev`, headless, on the
machines the ROM set can boot.  They are here so the next auditor knows which
lines above are evidence and which are reading.

- CoCo Max and X-Pad probed from a 6809 program on **coco3** and **coco**, with
  and without `-rat-mouse right` (patches 46 and 49; the numbers are quoted in
  the entries above).
- `-audit FILE,start=on` on a **coco** wrote a 2MB map with 1.6% of bytes
  marked — a real map, not the well-formed all-zero file that motivated the
  patch-37 fix.
- `-serial-out` captured `PRINT#-2` output on a **coco**; on a **dragon32** it
  printed "no PIA bit-banger serial on this machine; not capturing" and armed
  nothing; on an **mc10** it said nothing at all, still the one remaining
  accept-and-do-nothing.
- `-protect-mode` on a **coco** refused with "only wired for the CoCo 3 memory
  path (machine is coco)" — fail-loud, as designed.
- `-trap-backtrace` on a **coco** printed the expected single frame with the
  logical address standing in for the physical (`#0 in 0x04018 <- 0x0400F`),
  and the same trap on a **coco3** printed that frame plus its ROM caller.
- `-load-ram` placed the probe by physical address on both machines; a
  `format=` omission was refused loudly rather than loaded wrong.
