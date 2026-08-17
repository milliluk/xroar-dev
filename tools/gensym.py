#!/usr/bin/env python3
"""gensym.py -- emit the one symbol file every harness tool should read.

Writes PROJECT.sym (symbols, direct-page variables, equates) and
PROJECT.lines (physical address -> source line). Both are build output;
neither is hand-edited, and both are regenerated whenever obj/ is.

Why this exists. Symbol resolution was two half-tools that disagreed:

  - symbols.py maps a name to a LOGICAL address, first obj/*.map wins. 67
    global names are defined in two images, almost always the resident's
    dispatch thunk and the overlay's actual implementation, and the resident map
    sorts first -- so a lookup returns the thunk. Breakpoint by name on any of
    them and you stop in the dispatcher, one indirection short of the code you
    meant, in whatever bank happened to be mapped. Nothing warns.
  - cmaddr.py maps a name to a PHYSICAL address. It is the tool the iron rule
    points at, and it is right, but it knows nothing about direct-page
    variables or equates.
  - Neither knows equates at all, which is why AGENTS.md and two runbooks each
    carry their own `awk` over defs.asm for DebugPort.
  - millilint and cmaddr each re-implement the linked-listing section
    relocation.

One file, generated once, with the ambiguity preserved rather than silently
resolved: a name defined twice appears twice, and the READER refuses a bare
lookup that matches more than one image (see cmsym.py). That turns 67 silent
wrong answers into 67 errors that name both candidates.

Format -- plain text, one record per line, physical first because physical is
what AGENTS.md tells everything to prefer:

    !gensym VERSION
    !build BUILDID
    !image NAME ORGLOGICAL PHYSBASE SIZE SHA1
    S NAME IMAGE LOGICAL PHYSICAL
    D NAME OFFSET LOGICAL PHYSICAL          direct-page variable
    E NAME VALUE                            equate from defs.asm
    M C PHYSLO PHYSHI                       code bytes, inclusive (version 2)
    M D PHYSLO PHYSHI                       initialized data bytes (version 2)

and, in the .lines file:

    L PHYSICAL FILE:LINE

Addresses are bare hex, no prefix, uppercase. Physical is 5 digits.

Version 2 adds the M records, classified from the LISTING rather than from
symbols or sections: M C is every byte an instruction mnemonic emitted, M D
every byte a data directive emitted, so an fcb table between two routines is
correctly D and not C. xroar's -protect-mode is the consumer. Symbols cannot
carry this distinction -- a label is just an address -- which is why it is a
new record and a version bump rather than a convention on S records.

M D means INITIALIZED data and nothing else. An `rmb` reservation emits no
bytes into an lwasm listing, so the classifier never sees one and no M D range
can cover it; a program's own stack, buffers and scratch all live in rmb space
and stay outside the class. That is deliberate and load-bearing: it is what
lets -protect-mode-stack fault on a stack that has wandered onto declared data
without faulting on every program whose stack sits in reserved space, which is
all of them.
"""

import argparse
import glob
import hashlib
import os
import re
import subprocess
import sys

GENSYM_VERSION = 2

# Which project this is generating for, and which map file holds the resident
# image's direct-page records. Both are environment-overridable so the tool is
# not wedded to one codebase: set XROAR_DEV_PROJECT=myprog and the outputs
# become myprog.sym / myprog.lines. --out and --lines still win over both.
PROJECT = os.environ.get("XROAR_DEV_PROJECT", "program")
RESIDENT_MAP = os.environ.get("XROAR_DEV_RESIDENT_MAP", "RESIDENT.map")

# The locked direct page. INIT0's MC3 bit pins $FE00-$FEFF to the top of
# physical RAM regardless of any MapDrawPage, so this pair is an architectural
# constant, not a build-specific address (see docs/memory-model.md).
DP_LOGICAL_BASE = 0xFE00
DP_PHYS_BASE = 0x7FE00

MAP_SYMBOL = re.compile(r"^Symbol:\s+(\S+)\s+\(([^)]*)\)\s*=\s*([0-9A-Fa-f]+)\s*$")
MAP_SECTION = re.compile(r"^Section:\s+(\S+)\s+\(\S+\)\s+load at ([0-9A-Fa-f]+)")
HOME_BLOCK = re.compile(
    r"DeclareHomeBlock\s+([A-Za-z_][A-Za-z0-9_]*)\s*,\s*([A-Za-z_][A-Za-z0-9_]*)\s*([-+]\s*\d+)?")
LST_ADDR = re.compile(r"^\s*([0-9A-Fa-f]{4})\s")
LST_LINE = re.compile(r"\(\s*(\S+?)\):(\d{5})")
SECT_DIR = re.compile(r"\):\d{5}\s+(?:\S+\s+)?(section|endsection)\b\s*(\S*)", re.IGNORECASE)
EQU = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s+equ\s+(\S+)", re.IGNORECASE)
RMB = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s+rmb\s+(\d+)", re.IGNORECASE)


def parse_number(tok):
    """lwasm literal to int, or None. $ is hex, % binary, bare is decimal."""
    tok = tok.strip()
    try:
        if tok.startswith("$"):
            return int(tok[1:], 16)
        if tok.startswith("%"):
            return int(tok[1:], 2)
        if tok.startswith("0x"):
            return int(tok, 16)
        return int(tok, 10)
    except ValueError:
        return None


def image_of(owner):
    """Map a map-file owner back to the image it belongs to.

    A LINKED map credits each symbol to the OBJECT it came from -- the bank's
    own `OVERLAY6.link.o`, or `resident-stub.o` for every resident routine the
    bank calls. Neither is an image name. The stub's copies are not
    definitions: the resident's own map defines those, so they are dropped by
    the caller and only the object-to-image translation happens here.
    """
    owner = owner.split("/")[-1]
    if owner.endswith(".link.o"):
        return owner[: -len(".link.o")] + ".link.ram"
    if owner.endswith(".BIN"):
        return owner[: -len(".BIN")]
    return owner


def build_id(root):
    """Short commit hash plus a dirty marker, or 'unknown' outside a repo."""
    try:
        out = subprocess.run(["git", "-C", root, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return "unknown"
        h = out.stdout.strip()
        st = subprocess.run(["git", "-C", root, "status", "--porcelain"],
                            capture_output=True, text=True, timeout=10)
        return h + ("-dirty" if st.stdout.strip() else "")
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def sha1_of(path):
    try:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return "-"


def equate_table(root):
    """{NAME: value} for tree equates, resolving one symbol through another.

    Not from the maps. defs.asm keeps `opt nolist` over its bulk to stay out of
    every listing, and lwasm drops a nolist symbol from --map too, so its
    equates are not there -- and listing all of defs.asm to get them floods
    every image's map with several hundred non-addresses instead. Reading the
    source is the cheaper trade.

    Handles what DeclareHomeBlock's arguments actually use: a literal, a
    reference to another equate, and the `Symbol*N` form the slot bases are
    written with. Anything else resolves to None and is simply absent, which
    the caller reports rather than guesses around.

    A name two files define with DIFFERENT right-hand sides is dropped, not
    resolved first-wins. First-wins is how a tool reports one file's constant
    as another file's address, and this table places code.
    """
    raw = {}
    for path in (sorted(glob.glob(os.path.join(root, "*.asm"))) +
                 sorted(glob.glob(os.path.join(root, "*.inc")))):
        for line in open(path, errors="replace"):
            m = EQU.match(line)
            if m:
                raw.setdefault(m.group(1), set()).add(m.group(2))
    ambiguous = set(n for n, rhs in raw.items() if len(rhs) > 1)
    raw = dict((n, sorted(rhs)[0]) for n, rhs in raw.items())

    resolved = {}

    def value_of(name, depth=0):
        if name in resolved:
            return resolved[name]
        if depth > 8 or name not in raw:
            return None
        v = evaluate(raw[name], depth + 1)
        if v is not None:
            resolved[name] = v
        return v

    def evaluate(expr, depth):
        """The arithmetic lwasm allows in an equate, as far as it is needed.

        + - * / and parentheses over numbers and other equates, left to right
        within a precedence level, integer division -- which is what lwasm
        does. It grew from a single-operator regex the day a sector-rounding
        equate (((size+n-1)/n)*n, the brush ops' loaded footprint) had to
        resolve: an address that cannot be looked up is an address someone
        types by hand, and the whole point of the file is that nobody does.
        Anything it cannot parse still resolves to None and is absent.
        """
        toks = re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\$[0-9A-Fa-f]+|%[01]+|"
                          r"[0-9]+|[()+\-*/]", expr.strip())
        if not toks or "".join(toks) != re.sub(r"\s+", "", expr.strip()):
            return None
        pos = [0]

        def peek():
            return toks[pos[0]] if pos[0] < len(toks) else None

        def take():
            tok = peek()
            pos[0] += 1
            return tok

        def primary():
            tok = take()
            if tok is None:
                raise ValueError
            if tok == "(":
                v = additive()
                if take() != ")":
                    raise ValueError
                return v
            if tok == "-":
                return -primary()
            n = parse_number(tok)
            if n is not None:
                return n
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", tok):
                v = value_of(tok, depth)
                if v is None:
                    raise ValueError
                return v
            raise ValueError

        def multiplicative():
            v = primary()
            while peek() in ("*", "/"):
                op = take()
                r = primary()
                if op == "/":
                    if r == 0:
                        raise ValueError
                    v //= r
                else:
                    v *= r
            return v

        def additive():
            v = multiplicative()
            while peek() in ("+", "-"):
                op = take()
                r = multiplicative()
                v = v + r if op == "+" else v - r
            return v

        try:
            v = additive()
        except (ValueError, RecursionError):
            return None
        return v if pos[0] == len(toks) else None

    for name in list(raw):
        value_of(name)
    for name in ambiguous:
        resolved.pop(name, None)
    return resolved, ambiguous


def image_table(objdir, root, warn):
    """{image: (window, block, size, binary)} from each image's DeclareHomeBlock.

    An image states where it lives ONCE, in source, with the directive the
    packer already resolves the boot image through:

        DeclareHomeBlock <logical window>,<Slot* equate>

    and a logical address A in that image is at physical

        block * MmuBlockBytes + (A - window)

    This replaces the `;@` comment tag on each `org` line, which was a second,
    hand-written statement of the same fact in a different notation. It was a
    hack three ways: three modules never got one, so the tools could not place
    them at all; nothing checked it against the directive it duplicated; and in
    a linked bank the tagged `org` line sits inside `ifndef LINKED` and is not
    even assembled, so the tools were reading a number off a line the build had
    skipped. The directive has none of those problems -- it is the thing that
    actually places the code.

    An image with no declaration falls back to the GIME's power-on map, where
    logical A is at 0x38|(A>>13). That is right for the DOS boot sector, which
    runs before anything remaps anything, and is stated rather than assumed.
    """
    equs, _ambiguous = equate_table(root)
    blocksize = equs.get("MmuBlockBytes", 0x2000)
    out = {}
    for lst in sorted(glob.glob(os.path.join(objdir, "*.lst"))):
        name = os.path.basename(lst)[:-4]
        window = block = None
        nblocks = 0
        for line in open(lst, errors="replace"):
            m = HOME_BLOCK.search(line)
            if not m:
                continue
            win, slot, addend = m.group(1), m.group(2), m.group(3)
            if win not in equs or slot not in equs:
                warn("%s: DeclareHomeBlock names %s,%s which the maps do not "
                     "define" % (name, win, slot))
                break
            nblocks += 1
            if window is None:
                window = equs[win]
                block = equs[slot] + (int(addend.replace(" ", "")) if addend else 0)
            # Keep counting: the resident declares one per block it spans, and
            # the count IS its logical extent. Contiguous blocks in ascending
            # slots make the first declaration's rule valid across all of them.
        for cand in (name, name + ".BIN"):
            p = os.path.join(objdir, cand)
            if os.path.exists(p):
                binary = p
                break
        else:
            binary = None
        if window is None:
            # No declaration: the power-on map. Only correct for code that runs
            # before the program takes over the MMU.
            warn("%s declares no home block; assuming the GIME power-on map "
                 "(right for a boot sector, wrong for anything else)" % name)
            out[name] = (0, 0x38 * blocksize, os.path.getsize(binary) if binary else 0,
                         binary, 8 * blocksize)
            continue
        out[name] = (window, block * blocksize,
                     os.path.getsize(binary) if binary else 0,
                     binary, max(nblocks, 1) * blocksize)
    return out


LST_EMIT = re.compile(r"^\s*([0-9A-Fa-f]{4})\s+([0-9A-Fa-f]{2}(?:[0-9A-Fa-f ]*[0-9A-Fa-f])?)\s")


def image_extent(objdir, image, linked):
    """(lo, hi) logical range the image actually EMITS bytes into, or None.

    Not the image's 8K block. Since llm/lwasm-patches/01 stopped `nolist`
    hiding symbols from the map, every unit that includes defs.asm exports
    defs.asm's transitively-included labels too -- the jump table, the direct
    page -- and a block-sized extent happily accepts them. BRUSHMODE is 209
    bytes and was collecting 684 symbols that way.

    A symbol belongs to the image that emits bytes where it points. For a
    linked bank the link map's sections say exactly that; for an assembled one
    the listing's own address column does.
    """
    if linked:
        mp = os.path.join(objdir, image + ".map")
        lo = hi = None
        try:
            for line in open(mp, errors="replace"):
                m = MAP_SECTION.match(line)
                if not m:
                    continue
                ad = int(m.group(2), 16)
                ln = int(line.rsplit("length", 1)[-1].strip().split()[0], 16)
                if ln == 0:
                    continue
                lo = ad if lo is None else min(lo, ad)
                hi = ad + ln if hi is None else max(hi, ad + ln)
        except (OSError, ValueError, IndexError):
            return None
        return None if lo is None else (lo, hi)

    lst = os.path.join(objdir, image + ".lst")
    lo = hi = None
    try:
        for line in open(lst, errors="replace"):
            m = LST_EMIT.match(line)
            if not m:
                continue
            ad = int(m.group(1), 16)
            nb = len(m.group(2).replace(" ", "")) // 2
            if nb == 0:
                continue
            lo = ad if lo is None else min(lo, ad)
            hi = ad + nb if hi is None else max(hi, ad + nb)
    except OSError:
        return None
    return None if lo is None else (lo, hi)


def section_bases(mapfile):
    """{section: load_addr} for a linked map; empty for an assembled one."""
    out = {}
    try:
        for line in open(mapfile, errors="replace"):
            m = MAP_SECTION.match(line)
            if m:
                out[m.group(1)] = int(m.group(2), 16)
    except OSError:
        pass
    return out


def collect_symbols(objdir, images, warn, numeric_equs):
    """[(name, image, logical, physical)], every definition, ambiguity intact."""
    rows = []
    for mp in sorted(glob.glob(os.path.join(objdir, "*.map"))):
        image = os.path.basename(mp)[:-4]
        if image not in images:
            # A map with no ;@ org tag has no physical rule, so nothing here
            # can be placed. Better to say so than to guess a base.
            warn("no home block for %s; its symbols are not in the sym file" % image)
            continue
        org, base, _size, _bin, extent = images[image]
        span = image_extent(objdir, image, ".link." in image)
        emit_lo, emit_hi = span if span else (org, org + extent)
        for line in open(mp, errors="replace"):
            m = MAP_SYMBOL.match(line)
            if not m:
                continue
            name, owner, addr = m.group(1), m.group(2), int(m.group(3), 16)
            # lwasm's mangled locals ('a@_101E'), and the linker's own
            # per-section markers ('\02code').
            if "@" in name or name.startswith("\\"):
                continue
            # The resident stub's copies are not definitions: they name the
            # resident's routines so the linker can bind them, and the
            # resident's own map is where those live.
            if image_of(owner) == "resident-stub.o" or owner.endswith("resident-stub.o"):
                continue
            # An address outside the image's own logical extent -- the
            # window its DeclareHomeBlock names, times however many blocks it
            # declares -- is not a label in it. Since defs.asm became listed,
            # every map also carries its several hundred equates: block
            # numbers, register-file addresses, the direct page. None of those
            # is a place in this image, and applying its org rule to them
            # would invent physical addresses nothing lives at.
            if not (emit_lo <= addr < emit_hi):
                continue
            # A numeric equate declared inside a section arrives here already
            # relocated by the section base, so it looks like a label near the
            # image's start and the test above cannot see it. The source says
            # what it really is. These become E records only.
            if name in numeric_equs:
                continue
            # The locked direct page is pinned to the top of physical RAM by
            # INIT0's MC3 bit, so it is NOT at org+offset inside any image.
            # Applying the image's linear rule to it would put every DP
            # variable at a physical address nothing lives at. These are D
            # records; collect_dp() takes them.
            if DP_LOGICAL_BASE <= addr < DP_LOGICAL_BASE + 0x100:
                continue
            rows.append((name, image, addr, base + (addr - org)))
    return rows


def collect_dp(root, objdir, warn):
    """[(name, offset, logical, physical)] for direct-page variables, from the map.

    These used to be unrecoverable, and the reason was not lwasm's fault line
    but ours. `defs.asm` opens with `opt nolist` to keep its own 45K out of
    every listing, and `incl directpage.asm` sat inside that region. lwasm ties
    the two together: a symbol defined while nolist is current is flagged
    symbol_flag_nolist, and map_symbols() then skips it. So no direct-page
    variable reached obj/*.map, and the only way to get an offset was to scan
    the listing for an instruction that referenced one -- which silently omits
    every variable nothing references by a plain direct access, and cannot see
    the ones aliased onto the shared arenas with `equ` at all.

    Listing those ~140 lines (defs.asm, around the incl) puts all of them in
    the map, emits no different byte, and makes this a lookup. The earlier
    source-walking attempt got 44 of 143 offsets wrong, because the direct page
    declares some variables with the label on its own line, unions `equ`
    aliases over two shared arenas, and has `ifndef MC6309` and
    `ifdef STACK_WATERMARK` blocks that make its layout differ by build target.
    """
    rows = []
    mp = os.path.join(objdir, RESIDENT_MAP)
    if not os.path.exists(mp):
        warn("no %s; no direct-page records" % RESIDENT_MAP)
        return rows
    for line in open(mp, errors="replace"):
        m = MAP_SYMBOL.match(line)
        if not m:
            continue
        name, addr = m.group(1), int(m.group(3), 16)
        if "@" in name or name.startswith("\\"):
            continue
        if not (DP_LOGICAL_BASE <= addr < DP_LOGICAL_BASE + 0x100):
            continue
        off = addr - DP_LOGICAL_BASE
        rows.append((name, off, addr, DP_PHYS_BASE + off))
    rows.sort(key=lambda r: (r[1], r[0]))
    return rows


def collect_equates(root, warn):
    """({name: value}, {names}) for every NUMERIC equate in the tree.

    This is the `awk '/^DebugPort/...' defs.asm` in AGENTS.md and two runbooks,
    done once and over the whole tree rather than one file.

    The second return value is the reason this scans everything. lwasm reports
    an `equ` declared inside a section RELOCATED by the section base -- a known
    map artifact, not a code bug (journal 2026-07-27). So `HandFriction equ 1`
    inside a bank's `code` section arrives in the map looking exactly like a
    label at the bank's second byte, and 267 constants were being written out
    as addresses. The `addr < org` filter cannot catch them: relocation has
    already moved them into range.

    The discriminator is the right-hand side, in source:

        equ 1 / equ $ff69       a constant.  Never an address.
        equ *                   the current location.  A real address.
        equ SomeOtherSymbol     an alias.  A real address.

    So only a purely numeric equate is treated as a constant, and only those
    are excluded from the symbol records. Symbolic ones are left alone rather
    than half-resolved: this is not an assembler.
    """
    values = {}
    conflicts = set()
    numeric = set()
    sources = sorted(glob.glob(os.path.join(root, "*.asm"))) + \
        sorted(glob.glob(os.path.join(root, "*.inc")))
    for path in sources:
        for line in open(path, errors="replace"):
            m = EQU.match(line)
            if not m:
                continue
            name, rhs = m.group(1), m.group(2)
            v = parse_number(rhs)
            if v is None:
                continue
            numeric.add(name)
            if name in values and values[name] != v:
                conflicts.add(name)
            values.setdefault(name, v)
    for name in sorted(conflicts):
        values.pop(name, None)
    if conflicts:
        warn("%d equate name(s) carry different values in different files and "
             "are omitted: %s" % (len(conflicts), ", ".join(sorted(conflicts)[:6])))
    return values, numeric


# What a listing line's mnemonic can be.  Instructions become M C ranges, data
# directives M D ranges; anything else -- macro call sites, mostly --
# classifies nothing and is tallied so silence cannot hide it.
#
# The two sets below are GENERATED from the vendored lwasm's own instruction
# table by tools/gen_mnemonics.py, and were typed by hand until that tool
# existed.  The hand-written version was missing 55 mnemonics -- every
# Q-register form, sbcd/sbcr, the E/F/W negates and shifts, all four tfr
# variants, the whole 6800-compatibility block -- and each one was a hole in
# the code map and a false -protect-mode-data fault waiting to happen.  A
# 6309 instruction table is not a thing to keep in your head.
#
# CRITICAL PROPERTY, and the reason M D is safe to enforce a stack check
# against: `rmb` and the other pure reservations emit NO bytes into an lwasm
# listing.  A reservation has no emit column, so the loop below never sees one
# (nb == 0 lines are skipped before the mnemonic is even read) and no M D range
# can cover reserved space.  A program's own stack, buffers and scratch live
# there, so they are outside the data class by construction -- which is what
# keeps -protect-mode-stack from faulting on every ordinary push.  `rmb` is
# still named below because it IS a data directive and the set doubles as the
# mnemonic vocabulary; if an assembler ever did emit fill bytes for it, those
# bytes would join the data class and every stack in reserved space would fault
# at once, so that is the thing to check first if this suddenly cries wolf.

# --- BEGIN GENERATED MNEMONICS (tools/gen_mnemonics.py) ---
# Generated from lwtools-4.25.tar.gz's lwasm/instab.c. DO NOT HAND-EDIT:
# re-run `python3 tools/gen_mnemonics.py` instead. 277 instructions,
# 84 data directives; `os9` is counted as an instruction because it
# emits SWI2 and a call number.

INSTRUCTIONS = frozenset("""
    aba abx adca adcb adcd adcr adda addb
    addd adde addf addr addw aim anda andb
    andcc andd andr asl asla aslb asld asr
    asra asrb asrd asrq band bcc bcs beor
    beq bge bgt bhi bhs biand bieor bior
    bita bitb bitd bitmd ble blo bls blt
    bmi bne bor bpl bra break brn bsr
    bvc bvs cba clc clf cli clif clr
    clra clrb clrd clre clrf clrq clrw clv
    cmpa cmpb cmpd cmpe cmpf cmpr cmps cmpu
    cmpw cmpx cmpy com coma comb comd come
    comf comq comw copy cpx cwai daa dec
    deca decb decd dece decf decw des dex
    dey divd divq eim eora eorb eord eorr
    exg exp expand hcf imp implode inc inca
    incb incd ince incf incw ins inx iny
    jmp jsr lbcc lbcs lbeq lbge lbgt lbhi
    lbhs lble lblo lbls lblt lbmi lbne lbpl
    lbra lbrn lbsr lbvc lbvs lda ldb ldbt
    ldd lde ldf ldmd ldq lds ldu ldw
    ldx ldy leas leau leax leay log lsl
    lsla lslb lsld lsle lslf lslq lsr lsra
    lsrb lsrd lsrq lsrw mul muld neg nega
    negb negd nege negf negq negw nop oim
    ora orb orcc ord orr os9 pshs pshsw
    pshu pshuw puls pulsw pulu puluw reset rhf
    rol rola rolb rold rolw ror rora rorb
    rord rorw rti rts sba sbca sbcb sbcd
    sbcr sec sef sei seif sev sex sexw
    sta stb stbt std ste stf stq sts
    stu stw stx sty suba subb subd sube
    subf subr subw swi swi2 swi3 sync tab
    tap tba tfm tfr tfrm tfrp tfrr tfrs
    tim tpa tst tsta tstb tstd tste tstf
    tstq tstw tsx txs wai
""".split())

DATA_DIRECTIVES = frozenset("""
    align bsz dephase dtb dts else emod end
    endc endif endm ends endsect endsection endstruct equ
    error exit exitm export extdep extern external fcb
    fcc fcn fcs fcz fdb fdbs fill fqb
    fzb if ifc ifdef ifeq ifge ifgt ifle
    iflt ifnc ifndef ifne ifopt ifp1 ifp2 ifpragma
    ifstr import incl include includebin includestr lib macr
    macro mod msg nam opt org pag page
    phase pragma reorg rmb rmd rmq rmw sect
    section set setdp setstr spc struct ttl use
    warning zmb zmd zmq
""".split())
# --- END GENERATED MNEMONICS ---


def collect_class_ranges(objdir, images, warn):
    """(code, data), each [(phys_lo, phys_hi)] inclusive and merged.

    code is every byte an instruction emitted; data every byte a data
    directive emitted. Classified from the LISTING, the one build output
    that says per byte which emitted it. Sections cannot say this (an fcb
    table inside a code section is still data) and symbols cannot either (a
    label is just an address). Continuation lines -- a long fcc or fdb
    spilling its bytes across several listing rows -- carry no source text,
    so they inherit the class of the line that started them.

    Reserved space (`rmb`) is in NEITHER list: it emits no listing bytes, so
    nothing here ever sees it. See DATA_DIRECTIVES above -- that absence is
    what makes the data class safe for -protect-mode-stack to enforce.

    Relocation is collect_lines()'s rule exactly: a linked bank's listing
    counts from the section base, an address below org is outside the image.
    """
    spans = []
    data_spans = []
    unknown_spans = []   # (name, phys_lo, phys_hi): lines no class claimed
    for image, (org, base, _size, _bin, _ext) in sorted(images.items()):
        lst = os.path.join(objdir, image + ".lst")
        if not os.path.exists(lst):
            continue
        bases = section_bases(os.path.join(objdir, image + ".map")) if ".link." in image else {}
        section = None
        last_class = None
        last_name = None
        for line in open(lst, errors="replace"):
            sd = SECT_DIR.search(line)
            if sd:
                section = sd.group(2) if sd.group(1).lower() == "section" else None
            me = LST_EMIT.match(line)
            if not me:
                last_class = None
                continue
            nb = len(me.group(2).replace(" ", "")) // 2
            if nb == 0:
                last_class = None
                continue
            ml = LST_LINE.search(line)
            if ml:
                src = line[ml.end():]
                # The cycle-count column an `opt c` build inserts: "(4)",
                # "(5+?)".  Positional, so it lands where a naive read
                # expects the source to start.
                mcyc = re.match(r"\s*\([0-9+*?]+\)", src)
                if mcyc:
                    src = src[mcyc.end():]
                toks = [t.rstrip(":").lower() for t in src.split()[:3]]
                # A macro expansion line carries the body's own line number
                # as one more bare-decimal column; it is never a mnemonic
                # or a label, so drop it before looking for either.
                if toks and toks[0].isdigit():
                    toks = toks[1:]
                # The mnemonic is the first token unless the first token is
                # a label, and a label is whatever precedes a token we DO
                # know.  Column position would also say which is which, but
                # it moves with the listing options and this does not.
                mnem = None
                if toks and (toks[0] in INSTRUCTIONS or toks[0] in DATA_DIRECTIVES):
                    mnem = toks[0]
                elif len(toks) > 1 and (toks[1] in INSTRUCTIONS or toks[1] in DATA_DIRECTIVES):
                    mnem = toks[1]
                if mnem in INSTRUCTIONS:
                    last_class = "C"
                elif mnem in DATA_DIRECTIVES:
                    last_class = "D"
                else:
                    last_class = "?"
                    last_name = toks[0] if toks else "?"
            if last_class in ("C", "D", "?"):
                addr = int(me.group(1), 16) + bases.get(section, 0)
                if addr < org:
                    continue
                p = base + (addr - org)
                if last_class == "C":
                    spans.append((p, p + nb - 1))
                elif last_class == "D":
                    data_spans.append((p, p + nb - 1))
                else:
                    unknown_spans.append((last_name, p, p + nb - 1))

    def merge(raw):
        raw.sort()
        out = []
        for lo, hi in raw:
            if out and lo <= out[-1][1] + 1:
                if hi > out[-1][1]:
                    out[-1] = (out[-1][0], hi)
            else:
                out.append((lo, hi))
        return out

    merged = merge(spans)
    merged_data = merge(data_spans)
    # A macro CALL SITE lists the same bytes its expansion lists, so most
    # unknown-mnemonic lines are covered by a range the expansion classified
    # and are fine.  Only bytes NO listing line classified are worth a
    # warning: executing those trips -protect-mode-data.  Coverage is tested
    # against both classes -- a macro that expands to fcb is classified, just
    # not as code.
    both = merge([s for s in spans] + [s for s in data_spans])

    def covered(lo, hi):
        import bisect
        i = bisect.bisect_right([m[0] for m in both], lo) - 1
        return i >= 0 and both[i][1] >= hi
    uncovered = {}
    for name, lo, hi in unknown_spans:
        if not covered(lo, hi):
            uncovered[name] = uncovered.get(name, 0) + (hi - lo + 1)
    if uncovered:
        top = ", ".join("%s(%d)" % kv for kv in sorted(uncovered.items())[:8])
        warn("unclassified emitters left %d byte(s) in no class "
             "(executing them trips -protect-mode-data; a stack landing on "
             "them does NOT trip -protect-mode-stack): %s"
             % (sum(uncovered.values()), top))
    return merged, merged_data


def collect_lines(objdir, images):
    """[(physical, 'file:line')] from every listing, section-relocated once.

    A linked bank's listing is an OBJECT listing: its address column counts
    from the section base, not from the image's org. The link map has the base.
    A line outside every section -- an equate, the jump table, anything above
    the first `section` directive -- is already absolute and must be left
    alone, which is the same rule millilint's linked_relocator follows.
    """
    rows = []
    for image, (org, base, _size, _bin, _ext) in sorted(images.items()):
        lst = os.path.join(objdir, image + ".lst")
        if not os.path.exists(lst):
            continue
        bases = section_bases(os.path.join(objdir, image + ".map")) if ".link." in image else {}
        section = None
        for line in open(lst, errors="replace"):
            sd = SECT_DIR.search(line)
            if sd:
                section = sd.group(2) if sd.group(1).lower() == "section" else None
            ma = LST_ADDR.match(line)
            ml = LST_LINE.search(line)
            if not ma or not ml:
                continue
            addr = int(ma.group(1), 16) + bases.get(section, 0)
            if addr < org:
                continue
            rows.append((base + (addr - org), "%s:%d" % (ml.group(1), int(ml.group(2)))))
    return rows


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # XROAR_DEV_OBJ, not just <root>/obj: symread.py resolves the .sym/.lines
    # pair through that variable, and for a while this side ignored it -- so
    # setting it sent the reader looking somewhere the writer never wrote, and
    # load() raised. One variable, both ends.
    ap.add_argument("--obj",
                    default=os.environ.get("XROAR_DEV_OBJ")
                            or os.path.join(root, "obj"))
    ap.add_argument("--out", default=None, help="default OBJ/<project>.sym")
    ap.add_argument("--lines", default=None, help="default OBJ/<project>.lines")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    objdir = args.obj
    out_path = args.out or os.path.join(objdir, PROJECT + ".sym")
    lines_path = args.lines or os.path.join(objdir, PROJECT + ".lines")

    warnings = []

    def warn(msg):
        warnings.append(msg)

    images = image_table(objdir, root, warn)
    if not images:
        sys.stderr.write("gensym: no tagged listings under %s -- build first\n" % objdir)
        return 1

    equ_values, numeric_equs = collect_equates(root, warn)
    # Every name the equate resolver can put a value on is a constant, not a
    # label -- including the expression forms (`MmuSlot3Base equ
    # MmuBlockBytes*3`) that the literal-only scan misses and that land inside
    # an image's own extent, so no address filter can catch them. `equ *` and
    # `equ SomeLabel` do not resolve here and correctly stay symbols.
    symbolic_equs, ambiguous_equs = equate_table(root)
    numeric_equs = set(numeric_equs) | set(symbolic_equs) | ambiguous_equs
    # Harmless on stock lwasm (defs.asm's equates are not in the maps at all,
    # because they are defined under `opt nolist`); essential the moment
    # llm/lwasm-patches/01 is adopted and they become visible.
    syms = collect_symbols(objdir, images, warn, numeric_equs)
    dps = collect_dp(root, objdir, warn)
    # The symbolic ones are written out too, and that is not a widening for its
    # own sake: an address stated once and derived everywhere else -- LowRamFreeA
    # equ DiskGranuleBufferEnd, AntWindowBase equ MmuSlot1Base -- is the form the
    # iron rule asks for, so the tree's most carefully derived addresses were
    # exactly the ones a reader could not look up. A numeric definition wins over
    # a symbolic one for the same name; conflicting numerics were already dropped
    # above and stay dropped.
    for name, value in symbolic_equs.items():
        equ_values.setdefault(name, value)
    equs = sorted(equ_values.items())
    lines = collect_lines(objdir, images)
    code_ranges, data_ranges = collect_class_ranges(objdir, images, warn)

    with open(out_path, "w") as f:
        f.write("!gensym %d\n" % GENSYM_VERSION)
        f.write("!build %s\n" % build_id(root))
        for image, (org, base, size, binary, _ext) in sorted(images.items()):
            f.write("!image %s %04X %05X %d %s\n"
                    % (image, org, base, size, sha1_of(binary) if binary else "-"))
        for name, image, logical, phys in sorted(syms, key=lambda r: (r[3], r[0])):
            f.write("S %s %s %04X %05X\n" % (name, image, logical, phys))
        for name, off, logical, phys in dps:
            f.write("D %s %02X %04X %05X\n" % (name, off, logical, phys))
        for name, value in equs:
            f.write("E %s %X\n" % (name, value))
        for lo, hi in code_ranges:
            f.write("M C %05X %05X\n" % (lo, hi))
        for lo, hi in data_ranges:
            f.write("M D %05X %05X\n" % (lo, hi))

    with open(lines_path, "w") as f:
        for phys, where in sorted(set(lines)):
            f.write("L %05X %s\n" % (phys, where))

    if not args.quiet:
        dupes = {}
        for name, image, _l, _p in syms:
            dupes.setdefault(name, set()).add(image)
        ambiguous = sum(1 for v in dupes.values() if len(v) > 1)
        code_bytes = sum(hi - lo + 1 for lo, hi in code_ranges)
        data_bytes = sum(hi - lo + 1 for lo, hi in data_ranges)
        print("gensym: %d symbols (%d names defined in more than one image), "
              "%d dp vars, %d equates, %d source lines, %d images, "
              "%d code ranges (%d bytes), %d data ranges (%d bytes)"
              % (len(syms), ambiguous, len(dps), len(equs), len(set(lines)),
                 len(images), len(code_ranges), code_bytes,
                 len(data_ranges), data_bytes))
        print("        %s" % out_path)
        print("        %s" % lines_path)
        for w in warnings:
            sys.stderr.write("gensym: warning: %s\n" % w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
