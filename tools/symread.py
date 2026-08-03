"""symread.py -- read the .sym file that tools/gensym.py writes.

    import symread
    S = symread.load()
    S.phys('ClipCopy')                    # physical, the form everything prefers
    S.phys('OVERLAY6.link.ram:ClipCopy')  # when the bare name is ambiguous
    S.equ('DebugPort')                    # equates, no more awk over defs.asm
    S.dp('CursorX')                       # direct-page variable -> DPVar
    S.whereis(0x0AB72)                    # physical -> 'LoadFont+3 (OVERLAY5...)'
    S.line(0x0AB72)                       # physical -> 'type.asm:412'

Two things this refuses to do, both deliberate, because each is a wrong answer
the old tools gave silently:

**A bare name that two images define is an error, not a first-wins guess.**
Sixty-five names are defined twice in this tree, almost always as the
resident's dispatch thunk and the overlay's actual implementation. symbols.py
returns whichever map sorted first, which is the thunk -- so a breakpoint by
name lands one indirection short of the code you meant, in whatever bank was
mapped at the time, with no warning. Here it raises and names both candidates.
Qualify it with `IMAGE:NAME` and the ambiguity is gone.

**A stale symbol file is an error, not a plausible report.** Every address in
this tree moves on every build. load() checks the sha1 the generator recorded
for each image against what is on disk now and refuses a file that no longer
describes obj/. That is the same guard, for the same reason, as the one
cmdrive.run() puts on a warm snapshot.
"""

import hashlib
import os
import re
import bisect

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.environ.get("XROAR_DEV_ROOT", os.path.dirname(_HERE))


class AmbiguousSymbol(KeyError):
    """A bare name that more than one image defines."""


class StaleSymbolFile(RuntimeError):
    """obj/ has been rebuilt since the symbol file was written."""


class Sym:
    __slots__ = ("name", "image", "logical", "phys")

    def __init__(self, name, image, logical, phys):
        self.name, self.image, self.logical, self.phys = name, image, logical, phys

    def __repr__(self):
        return "Sym(%s, %s, logical=0x%04X, phys=0x%05X)" % (
            self.name, self.image, self.logical, self.phys)


class DPVar:
    __slots__ = ("name", "offset", "logical", "phys")

    def __init__(self, name, offset, logical, phys):
        self.name, self.offset, self.logical, self.phys = name, offset, logical, phys

    def __repr__(self):
        return "DPVar(%s, offset=0x%02X, logical=0x%04X, phys=0x%05X)" % (
            self.name, self.offset, self.logical, self.phys)


class SymbolFile:
    def __init__(self):
        self.build = "unknown"
        self.images = {}        # image -> (org, phys_base, size, sha1)
        self.by_name = {}       # name -> [Sym, ...]
        self.dpvars = {}        # name -> DPVar
        self.equates = {}       # name -> int
        self._sorted_phys = []  # for whereis()
        self._sorted_syms = []
        self._lines = None      # lazily loaded (physical -> 'file:line')
        self._line_keys = None

    # - - - lookup - - -

    def sym(self, name):
        """Sym for NAME or IMAGE:NAME. Raises on ambiguity or absence."""
        if ":" in name:
            image, _, bare = name.partition(":")
            for s in self.by_name.get(bare, ()):
                if s.image == image or s.image.split(".")[0] == image:
                    return s
            raise KeyError("%s is not defined in %s" % (bare, image))
        hits = self.by_name.get(name)
        if not hits:
            raise KeyError("no symbol %r in the sym file (rebuild? typo? "
                           "an equate rather than a symbol?)" % name)
        if len(hits) > 1:
            raise AmbiguousSymbol(
                "%r is defined in %d images: %s -- qualify it as IMAGE:%s. "
                "These are usually the resident's dispatch thunk and the "
                "overlay's implementation, which are different places."
                % (name, len(hits), ", ".join(sorted(h.image for h in hits)), name))
        return hits[0]

    def phys(self, name):
        return self.sym(name).phys

    def logical(self, name):
        return self.sym(name).logical

    def equ(self, name):
        try:
            return self.equates[name]
        except KeyError:
            raise KeyError("no equate %r in defs.asm" % name)

    def dp(self, name):
        try:
            return self.dpvars[name]
        except KeyError:
            raise KeyError("no direct-page variable %r in directpage.asm" % name)

    def candidates(self, name):
        """Every definition of NAME, for reporting an ambiguity to a human."""
        return list(self.by_name.get(name, ()))

    # - - - reverse - - -

    def whereis(self, phys, max_distance=0x400):
        """Nearest symbol at or below PHYS, as 'Name+off (IMAGE)', or None.

        The reverse direction is what turns a flight-recorder dump, a profiler
        histogram or an audit map from a column of hex into something about the
        program.
        """
        i = bisect.bisect_right(self._sorted_phys, phys) - 1
        if i < 0:
            return None
        s = self._sorted_syms[i]
        off = phys - s.phys
        if off > max_distance:
            return None
        return "%s+%d (%s)" % (s.name, off, s.image) if off else "%s (%s)" % (s.name, s.image)

    def line(self, phys):
        """'file:line' for PHYS from the .lines file, or None."""
        if self._lines is None:
            self._load_lines()
        return self._lines.get(phys)

    def _load_lines(self):
        self._lines = {}
        path = os.path.join(_objdir(), _project() + ".lines")
        if not os.path.exists(path):
            return
        for row in open(path, errors="replace"):
            if not row.startswith("L "):
                continue
            _, addr, where = row.split(None, 2)
            self._lines[int(addr, 16)] = where.strip()

    def _finish(self):
        pairs = sorted(((s.phys, s) for group in self.by_name.values() for s in group),
                       key=lambda p: p[0])
        self._sorted_phys = [p[0] for p in pairs]
        self._sorted_syms = [p[1] for p in pairs]


def _objdir():
    """Where the .sym/.lines pair lives. gensym.py resolves --obj through the
    same variable, so the writer and the reader cannot be pointed at different
    directories -- which they could, until XROAR_DEV_OBJ was honoured here and
    nowhere else and every load() raised."""
    return os.environ.get("XROAR_DEV_OBJ") or os.path.join(_ROOT, "obj")


def _project():
    """Basename of the .sym/.lines pair. Matches gensym.py's default so the
    writer and the reader agree without either being told twice."""
    return os.environ.get("XROAR_DEV_PROJECT", "program")


def _sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def load(path=None, check_stale=True):
    """Parse the symbol file. Raises StaleSymbolFile if obj/ has moved on."""
    if path is None:
        path = os.path.join(_objdir(), _project() + ".sym")
    if not os.path.exists(path):
        raise RuntimeError("%s does not exist -- run `python3 tools/gensym.py` "
                           "(or `make sym`) after building" % path)
    S = SymbolFile()
    for row in open(path, errors="replace"):
        row = row.rstrip("\n")
        if not row:
            continue
        kind, _, rest = row.partition(" ")
        if kind == "!build":
            S.build = rest.strip()
        elif kind == "!image":
            name, org, base, size, digest = rest.split()
            S.images[name] = (int(org, 16), int(base, 16), int(size), digest)
        elif kind == "S":
            name, image, logical, phys = rest.split()
            S.by_name.setdefault(name, []).append(
                Sym(name, image, int(logical, 16), int(phys, 16)))
        elif kind == "D":
            name, off, logical, phys = rest.split()
            S.dpvars[name] = DPVar(name, int(off, 16), int(logical, 16), int(phys, 16))
        elif kind == "E":
            name, value = rest.split()
            S.equates[name] = int(value, 16)
    S._finish()

    if check_stale:
        # Digests are checked against obj/, not against wherever this file was
        # copied to: the file describes the build tree, and a copy of it that
        # reported every image "missing" would be noise, not a staleness
        # signal.
        objdir = _objdir()
        bad = []
        for image, (_org, _base, _size, digest) in S.images.items():
            if digest == "-":
                continue
            for cand in (image, image + ".BIN"):
                p = os.path.join(objdir, cand)
                if os.path.exists(p):
                    if _sha1(p) != digest:
                        bad.append(image)
                    break
            else:
                bad.append(image + " (missing)")
        if bad:
            raise StaleSymbolFile(
                "%s describes a different build than obj/ holds now (%s). "
                "Re-run `python3 tools/gensym.py`. Every address in this tree "
                "moves on every build; a stale file is a wrong answer, not an "
                "old one." % (path, ", ".join(sorted(bad))))
    return S
