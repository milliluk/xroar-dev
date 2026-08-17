#!/usr/bin/env python3
"""gen_mnemonics.py -- regenerate gensym.py's mnemonic sets from lwasm itself.

    python3 tools/gen_mnemonics.py [--check]

gensym.py has to know which listing lines emitted INSTRUCTIONS and which
emitted DATA, because that distinction is what -protect-mode enforces and
nothing else in the build records it. The two sets used to be typed out by
hand, and a hand-typed set of a 6309 instruction table is wrong the day it is
written: the first audit of it found 55 missing mnemonics, including every
Q-register form (lslq lsrq asrq clrq comq negq tstq), sbcd/sbcr, the E/F/W
negates and shifts, all four tfr variants, and the whole 6800-compatibility
block. Every one of them would have been an unclassified emitter -- a hole in
the code map, and a false -protect-mode-data fault on execution.

The assembler already has the answer, in lwasm/instab.c, where every row names
its parse function: insn_parse_* for a real instruction, pseudo_parse_* for a
directive. So this reads the vendored lwtools tarball -- the same one
build-lwasm.sh builds, so the sets always describe the assembler this repo
ships -- and rewrites the marked block in gensym.py.

--check exits nonzero if gensym.py is out of date, for CI.

THE ONE EXCEPTION, and why it is not a special case you can drop: `os9` is a
pseudo-op that emits SWI2 plus a call number (lwasm/os9.c), so it is the only
directive that emits EXECUTABLE bytes. Classified with the other pseudos it
would make every OS-9 system call a byte the map calls data, and executing one
would trip -protect-mode-data. It is listed below rather than hardcoded in the
parse so that the next one like it has an obvious home.
"""

import argparse
import glob
import os
import re
import sys
import tarfile

# Pseudo-ops that emit executable bytes, so they belong with the instructions.
# See the module docstring before adding to this.
CODE_EMITTING_PSEUDOS = {"os9"}

BEGIN = "# --- BEGIN GENERATED MNEMONICS (tools/gen_mnemonics.py) ---"
END = "# --- END GENERATED MNEMONICS ---"

# A table row: { "name", { opcodes }, parsefn, ... }.  The parse function is
# the classifier; the opcode braces in between are why this spans lines.
ROW = re.compile(
    r'^\s*\{\s*"([a-z0-9?]+)"\s*,.*?\}\s*,\s*(insn_parse_\w+|pseudo_parse_\w+)',
    re.M | re.S)


def read_instab(root):
    """(source text, tarball basename) for the vendored lwtools' instab.c."""
    tarballs = sorted(glob.glob(os.path.join(root, "lwasm", "lwtools-*.tar.gz")))
    if not tarballs:
        sys.exit("gen_mnemonics: no lwasm/lwtools-*.tar.gz in %s" % root)
    path = tarballs[-1]
    with tarfile.open(path) as t:
        names = [n for n in t.getnames() if n.endswith("lwasm/instab.c")]
        if not names:
            sys.exit("gen_mnemonics: no lwasm/instab.c inside %s" % path)
        return t.extractfile(names[0]).read().decode("utf-8", "replace"), \
            os.path.basename(path)


def wrap(names, per_line=8, indent="    "):
    out, row = [], []
    for n in sorted(names):
        row.append(n)
        if len(row) == per_line:
            out.append(indent + " ".join(row))
            row = []
    if row:
        out.append(indent + " ".join(row))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if gensym.py is stale, changing nothing")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    src, tarball = read_instab(root)

    rows = ROW.findall(src)
    if not rows:
        sys.exit("gen_mnemonics: parsed no rows out of instab.c -- has the "
                 "table's shape changed?")
    # Every row that names a mnemonic must classify, or the sets are silently
    # short, which is the exact failure this tool exists to end.
    named = set(re.findall(r'^\s*\{\s*"([a-z0-9?]+)"', src, re.M))
    missed = named - {n for n, _ in rows}
    if missed:
        sys.exit("gen_mnemonics: %d row(s) did not classify: %s"
                 % (len(missed), " ".join(sorted(missed))))

    insn = {n for n, fn in rows if fn.startswith("insn_")}
    pseudo = {n for n, fn in rows if fn.startswith("pseudo_")}
    unknown_exception = CODE_EMITTING_PSEUDOS - pseudo
    if unknown_exception:
        sys.exit("gen_mnemonics: CODE_EMITTING_PSEUDOS names %s, which is not "
                 "a pseudo-op in this lwasm" % " ".join(sorted(unknown_exception)))
    insn |= CODE_EMITTING_PSEUDOS
    pseudo -= CODE_EMITTING_PSEUDOS

    block = "\n".join([
        BEGIN,
        "# Generated from %s's lwasm/instab.c. DO NOT HAND-EDIT:" % tarball,
        "# re-run `python3 tools/gen_mnemonics.py` instead. %d instructions,"
        % len(insn),
        "# %d data directives; `os9` is counted as an instruction because it"
        % len(pseudo),
        "# emits SWI2 and a call number.",
        "",
        "INSTRUCTIONS = frozenset(\"\"\"",
        wrap(insn),
        "\"\"\".split())",
        "",
        "DATA_DIRECTIVES = frozenset(\"\"\"",
        wrap(pseudo),
        "\"\"\".split())",
        END,
    ])

    target = os.path.join(here, "gensym.py")
    body = open(target).read()
    if BEGIN not in body or END not in body:
        sys.exit("gen_mnemonics: %s has no generated block markers" % target)
    start = body.index(BEGIN)
    stop = body.index(END) + len(END)
    updated = body[:start] + block + body[stop:]

    if args.check:
        if updated != body:
            sys.exit("gen_mnemonics: gensym.py is stale -- run "
                     "`python3 tools/gen_mnemonics.py`")
        print("gen_mnemonics: gensym.py is up to date (%d instructions, "
              "%d data directives)" % (len(insn), len(pseudo)))
        return 0

    if updated == body:
        print("gen_mnemonics: no change (%d instructions, %d data directives)"
              % (len(insn), len(pseudo)))
        return 0
    open(target, "w").write(updated)
    print("gen_mnemonics: updated %s from %s: %d instructions, "
          "%d data directives" % (target, tarball, len(insn), len(pseudo)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
