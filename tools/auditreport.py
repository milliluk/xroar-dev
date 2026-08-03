#!/usr/bin/env python3
"""auditreport.py -- read an xroar audit map and say what it means.

    python3 tools/auditreport.py /tmp/cm_boot.audit
    python3 tools/auditreport.py MAP --uncovered OVERLAY6
    python3 tools/auditreport.py NEW --diff OLD

The map (xroar `-audit`, patch 36) is one byte per flat physical RAM address,
one bit per access class. Raw, it is two megabytes of hex. With a .sym file
it becomes statements about the program: which routines never ran, how deep the
stack got and where, what the interrupt handlers touched.

**Never executed is not dead code.** The map cannot tell a routine that is
unreachable from one this session simply did not exercise, and it never will --
that is what accumulating maps across runs (`-audit FILE,seed=FILE`) is for.
Nothing here says "dead"; it says "not covered", and the difference is the
whole discipline.
"""

import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "llm"))
import symread  # noqa: E402

HDR = 64
MAGIC = b"XRAUDIT\0"

READ, WRITE, EXEC, OPCODE, STACK_S, STACK_U, IRQ, PUSHPULL = (
    0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80)

FLAG_NAMES = [("read", READ), ("write", WRITE), ("exec", EXEC), ("opcode", OPCODE),
              ("S-derived", STACK_S), ("U-derived", STACK_U), ("interrupt", IRQ),
              ("push/pull", PUSHPULL)]


class AuditMap:
    def __init__(self, path):
        with open(path, "rb") as f:
            blob = f.read()
        if len(blob) < HDR or blob[:8] != MAGIC:
            raise SystemExit("%s: not an audit map" % path)
        fmt, legend, span, runs = struct.unpack("<4I", blob[8:24])
        self.path = path
        self.format = fmt
        self.legend = legend
        self.span = span
        self.runs = runs
        self.build = blob[24:HDR].split(b"\0")[0].decode(errors="replace")
        self.m = blob[HDR:HDR + span]
        if len(self.m) != span:
            raise SystemExit("%s: truncated map" % path)

    def count(self, bit, lo=0, hi=None):
        hi = self.span if hi is None else hi
        return sum(1 for b in self.m[lo:hi] if b & bit)

    def any_set(self, lo, hi, bit):
        return any(b & bit for b in self.m[lo:hi])


def runs_of(indices):
    """[(start, end_inclusive)] from a sorted list of addresses."""
    out = []
    if not indices:
        return out
    start = prev = indices[0]
    for i in indices[1:]:
        if i != prev + 1:
            out.append((start, prev))
            start = i
        prev = i
    out.append((start, prev))
    return out


def extents(S):
    """[(phys_lo, phys_hi_exclusive, Sym)] per image, symbol to next symbol.

    A routine's extent is taken as the gap to the next symbol in the same
    image, clipped to the image. That is approximate -- an unlabelled block of
    data after a routine counts as part of it -- but it needs no disassembler
    and it is the granularity a person actually asks about.
    """
    per_image = {}
    for group in S.by_name.values():
        for sym in group:
            per_image.setdefault(sym.image, []).append(sym)
    out = []
    for image, syms in per_image.items():
        org, base, size, _digest = S.images[image]
        end = base + size if size else base
        syms.sort(key=lambda s: s.phys)
        for i, sym in enumerate(syms):
            hi = syms[i + 1].phys if i + 1 < len(syms) else end
            if hi > sym.phys:
                out.append((sym.phys, hi, sym))
    return out


def report(amap, S, args):
    w = sys.stdout.write

    w("audit map  %s\n" % amap.path)
    w("  runs unioned   %d\n" % amap.runs)
    w("  build          %s\n" % (amap.build or "(not recorded -- pass -audit ...,build=ID)"))
    if amap.build and S.build != "unknown" and amap.build != S.build:
        w("  ** the symbol file describes build %s. Addresses in this report may\n"
          "     name the wrong code. Rebuild, or re-capture the map.\n" % S.build)
    w("\n")

    w("totals, bytes with each flag set\n")
    for name, bit in FLAG_NAMES:
        w("  %-11s %9d\n" % (name, amap.count(bit)))
    w("\n")

    # --- coverage per image -------------------------------------------------
    w("coverage by image\n")
    w("  %-22s %7s %8s %8s %10s\n" % ("image", "bytes", "executed", "pct", "instrs"))
    for image, (org, base, size, _d) in sorted(S.images.items(), key=lambda kv: kv[1][1]):
        if not size:
            continue
        lo, hi = base, base + size
        ex = amap.count(EXEC, lo, hi)
        op = amap.count(OPCODE, lo, hi)
        w("  %-22s %7d %8d %7.1f%% %10d\n" % (image, size, ex, 100.0 * ex / size, op))
    w("\n")

    # --- routines -----------------------------------------------------------
    ext = extents(S)
    never, partial, full = [], [], []
    for lo, hi, sym in ext:
        n = amap.count(EXEC, lo, hi)
        span = hi - lo
        if n == 0:
            never.append((sym, span))
        elif n < span:
            partial.append((sym, n, span))
        else:
            full.append((sym, span))
    w("routines (symbol to next symbol; an extent may include trailing data)\n")
    w("  fully covered   %5d\n" % len(full))
    w("  partly covered  %5d\n" % len(partial))
    w("  not covered     %5d\n" % len(never))
    w("\n")

    if args.uncovered:
        want = args.uncovered
        sel = [(s, n) for s, n in never if want in s.image]
        sel.sort(key=lambda r: -r[1])
        w("not covered in %s, largest first (%d of %d)\n"
          % (want, min(len(sel), args.top), len(sel)))
        for sym, span in sel[:args.top]:
            where = S.line(sym.phys) or ""
            w("  %-34s %5d bytes  %s\n" % (sym.name, span, where))
        w("\n")

    # --- stack --------------------------------------------------------------
    stack_s = [i for i, b in enumerate(amap.m) if (b & PUSHPULL) and (b & STACK_S)]
    w("stack, S push/pull traffic\n")
    if not stack_s:
        w("  none recorded\n")
    else:
        for lo, hi in runs_of(stack_s):
            near = S.whereis(lo, max_distance=0x2000)
            w("  %3d bytes deep   physical 0x%05X-0x%05X  block 0x%02X%s\n"
              % (hi - lo + 1, lo, hi, lo >> 13,
                 "  near %s" % near if near else ""))
        w("  (one run per region the stack lived in; the MMU moves it during boot.\n"
          "   Pillar 7: the permanent application stack is in low memory, and\n"
          "   overlay routines that remap the low window have destroyed it before.\n"
          "   A 'near' attribution to an overlay means the stack shared that\n"
          "   physical block at the time, not that the overlay owns it.)\n")
    su = [i for i, b in enumerate(amap.m) if (b & STACK_U) and not (b & PUSHPULL)]
    w("  U used as a general pointer, not a stack: %d bytes\n" % len(su))
    w("\n")

    # --- interrupt context --------------------------------------------------
    irq = [i for i, b in enumerate(amap.m) if b & IRQ]
    w("interrupt context\n")
    w("  %d bytes touched while a handler was active\n" % len(irq))
    if irq:
        blocks = sorted({i >> 13 for i in irq})
        w("  in %d physical block(s): %s\n"
          % (len(blocks), ", ".join("0x%02X" % b for b in blocks)))
        w("  (pillar 6: the IRQ walkers own their bank remaps. A block here that\n"
          "   no handler should reach is the thing to look at.)\n")
    w("\n")

    # --- suspicious ---------------------------------------------------------
    ronly = sum(1 for b in amap.m if (b & READ) and not (b & (WRITE | EXEC)))
    wonly = sum(1 for b in amap.m if (b & WRITE) and not (b & (READ | EXEC)))
    w("read/write asymmetry\n")
    w("  read, never written or executed   %8d  (ROM-shadow, constants, or a\n"
      "                                              read of uninitialised RAM)\n" % ronly)
    w("  written, never read or executed   %8d  (cleared buffers, or work that\n"
      "                                              nothing ever consumed)\n" % wonly)


def diff(new, old, S):
    w = sys.stdout.write
    if new.span != old.span or new.legend != old.legend:
        raise SystemExit("maps have different span or legend; refusing to diff")
    gained = [i for i in range(new.span)
              if (new.m[i] & EXEC) and not (old.m[i] & EXEC)]
    lost = [i for i in range(new.span)
            if (old.m[i] & EXEC) and not (new.m[i] & EXEC)]
    w("diff  %s  against  %s\n" % (new.path, old.path))
    w("  newly executed bytes  %d\n" % len(gained))
    w("  no longer executed    %d\n" % len(lost))
    if gained:
        w("\nroutines reached that the older map never reached:\n")
        seen = []
        for lo, hi, sym in sorted(extents(S)):
            if old.any_set(lo, hi, EXEC):
                continue
            if new.any_set(lo, hi, EXEC):
                seen.append(sym)
        for sym in seen[:60]:
            w("  %-34s %s\n" % (sym.name, sym.image))
        if len(seen) > 60:
            w("  ... and %d more\n" % (len(seen) - 60))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("map")
    ap.add_argument("--diff", metavar="OLDMAP",
                    help="report what this map reaches that OLDMAP did not")
    ap.add_argument("--uncovered", metavar="IMAGE",
                    help="list uncovered routines in the image whose name contains IMAGE")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--no-stale-check", action="store_true",
                    help="read the symbol file even if obj/ has been rebuilt since")
    args = ap.parse_args()

    S = symread.load(check_stale=not args.no_stale_check)
    amap = AuditMap(args.map)
    if args.diff:
        diff(amap, AuditMap(args.diff), S)
    else:
        report(amap, S, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
