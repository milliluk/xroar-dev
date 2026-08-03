#!/usr/bin/env python3
"""decb.py - a small, dependency-free stand-in for ToolShed's decb.

It speaks the same command line as decb.exe and implements the full command
set documented on the ToolShed wiki
(https://sourceforge.net/p/toolshed/wiki/Documentation/):

    decb dskini [-3|-4|-8] [-h<num>] [-n<name>] [-s] <disk>...
    decb copy [-0|-1|-2|-3] [-a|-b] [-c] [-l] [-r] [-t] <src>... <target>
    decb dir <disk>[,][:<n>]...
    decb free <disk>[,][:<n>]...
    decb kill <disk>,<name>[:<n>]...
    decb list [-s] [-t] <file>...
    decb attr [-0|-1|-2|-3] [-a|-b] <disk>,<name>[:<n>]...
    decb rename <disk>,<oldname>[:<n>] <newname>

plus extensions ToolShed doesn't have: rawpack (this project's own
raw-sector packer), dir -d / copy -d (deleted-file listing and recovery),
wipe (zero all unused sectors), copyall (bulk extract), stat (per-file
chain detail), dump (sector hex dump), and check (RS-DOS fsck).

Images with a JVC header (any 1-255 byte remainder past whole sectors)
are read and written transparently everywhere except rawpack, as long as
the geometry is plain: 18 sectors/track, 1 side, 256-byte sectors, first
sector ID 1, no per-sector attributes. dskini never writes a header.

Paths on either side of a copy may be host files or image paths (disk,NAME);
an image path may carry an HDB-DOS drive suffix (disk,NAME:2 selects the
third 35-track drive in a multi-drive pack made with dskini -h).

Disk layout (single-sided, 18 sectors/track, 256 bytes/sector):
  - Track 17 is the directory track.
  - Track 17 sector 2 is the granule allocation table (GAT).
  - Track 17 sectors 3..11 hold 32-byte directory entries.
  - A granule is 9 sectors (2304 bytes). Two granules per track,
    skipping track 17. Granule g lives on track (g//2), bumped past 17.

A fresh disk is all 0xFF; file data is laid into granules and the tail of a
file's last sector keeps the 0xFF fill. Disk creation and binary file storage
match MAME imgtool byte for byte; the BASIC tokenizer (-t) instead matches the
real Disk Extended Color BASIC 2.1 ROM, verified against a disk it produced.

Deliberate divergences from ToolShed decb (checked against its source,
github.com/nitros9project/toolshed, 2026-07-02):
  - dskini keeps the all-0xFF fill above (ToolShed zeroes track 17 sector 1
    and the unused tail of the GAT sector; imgtool and this script don't).
  - Granule allocation is first-free ascending, matching imgtool (real DECB
    and ToolShed allocate nearest the directory track first).
  - An 80-track disk gets (80-1)*2 = 158 granules here; ToolShed initializes
    only 156 in the GAT for reasons its source doesn't explain.
  - The detokenizer uses this file's ROM-verified token tables. ToolShed's
    function table has a stray empty slot at index 40 that shifts LPEEK,
    BUTTON, HPOINT, ERNO and ERLIN off by one from the actual Super Extended
    BASIC values (LPEEK is 0xFF 0xA8 on real hardware). Using our tables also
    guarantees tokenize -> detokenize round-trips exactly.
  - copy -c scans the full 64K address space; ToolShed's stops at $FFFE
    and silently drops a byte loaded at $FFFF.
  - copy -c and list -s refuse input that isn't a DECB segmented binary;
    ToolShed marches through it and emits garbage records.
  - dir/free accept a bare image path and only read a trailing :N as an
    HDB-DOS drive suffix when N is all digits, so Windows paths with a
    drive letter (C:\\disks\\foo.dsk) work; ToolShed splits at the first
    colon it sees and would not.
"""

import os
import sys

VERSION = "2.0"

SECTOR = 256
SPT = 18                     # sectors per track
GRANULE_SECTORS = 9
GRANULE = GRANULE_SECTORS * SECTOR
DIR_TRACK = 17
GAT_SECTOR = 1               # 0-based sector index within the track (sector 2)
DIR_FIRST_SECTOR = 2         # 0-based; sector 3
DIR_LAST_SECTOR = 10         # 0-based; sector 11
ENTRY_SIZE = 32
FREE = 0xFF
DELETED = 0x00

TRACK_SIZES = {3: 35, 4: 40, 8: 80}

# HDB-DOS multi-drive packs (dskini -h): one 35-track disk per drive,
# back to back. The pack's disk name lives in track 17, sector 17
# (1-based), i.e. 0-based sector 16, as a NUL-terminated string.
DRIVE_SIZE = 35 * SPT * SECTOR          # 161280
HDB_NAME_SECTOR = 16                     # 0-based within the directory track
HDB_NAME_MAX = 9                         # ToolShed's MAX_DISKNAME_LEN
SKITZO_GRANULES = 34                     # dskini -s reserves granules 0..33


def die(msg):
    sys.stderr.write("decb: %s\n" % msg)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Color BASIC tokenizer (-t)
#
# This reproduces what a real Color Computer (Disk Extended Color BASIC 2.1)
# writes when you SAVE a tokenized program, verified byte for byte against a
# disk produced by the actual ROM. It is NOT what MAME imgtool's "cocobas"
# filter does; imgtool tokenizes keywords inside REM/' remarks, gets the
# header length and line links wrong, drops the implicit colon before ',
# and does not treat ? as PRINT.
#
# Rules:
#   - Inside double quotes: literal.
#   - After REM (0x82) or ' (0x83): literal to end of line.
#   - After DATA (0x86): literal until ':' ends the statement.
#   - ' (apostrophe) is stored as ':' + the ' token, i.e. 0x3A 0x83.
#   - ? is the PRINT abbreviation and is stored as the PRINT token 0x87.
#   - Otherwise: greedy prefix match, statement table before function table,
#     lowest index wins, with no word-boundary check (so embedded keywords
#     like AND inside ANDY do tokenize, which is genuine CoCo behaviour).
#
# Container: the file is 0xFF, then a 16-bit big-endian length equal to the
# size of the in-memory program image (the lines plus the trailing 00 00),
# i.e. the file size minus this 3-byte header. Each line begins with a 2-byte
# big-endian link holding the absolute address of the next line; the program
# loads at TXTTAB = 0x2601 on this machine.
# ---------------------------------------------------------------------------

TXTTAB = 0x2601              # Disk ECB program start; line links are absolute
TOK_PRINT = 0x87

# Statement-table indices that switch off tokenizing for the rest of a line
# or statement.
TOK_REM = 0x82
TOK_APOS = 0x83
TOK_DATA = 0x86

COCO_STATEMENTS = [
    "FOR", "GO", "REM", "'", "ELSE", "IF", "DATA", "PRINT", "ON", "INPUT",
    "END", "NEXT", "DIM", "READ", "RUN", "RESTORE", "RETURN", "STOP", "POKE",
    "CONT", "LIST", "CLEAR", "NEW", "CLOAD", "CSAVE", "OPEN", "CloseFile", "LLIST",
    "SET", "RESET", "ClearPage", "MOTOR", "SOUND", "AUDIO", "EXEC", "SKIPF", "TAB(",
    "TO", "SUB", "THEN", "NOT", "STEP", "OFF", "+", "-", "*", "/", "^", "AND",
    "OR", ">", "=", "<", "DEL", "EDIT", "TRON", "TROFF", "DEF", "LET", "LINE",
    "PCLS", "SetPixel", "PRESET", "SCREEN", "PCLEAR", "COLOR", "CIRCLE", "PAINT",
    "GET", "PUT", "DRAW", "CopyPage", "PMODE", "PLAY", "DLOAD", "RENUM", "FN",
    "USING", "DIR", "DRIVE", "FIELD", "FILES", "KILL", "LOAD", "LSET", "MERGE",
    "RENAME", "RSET", "SAVE", "WRITE", "VERIFY", "UNLOAD", "DSKINI", "BACKUP",
    "COPY", "DSKI$", "DSKO$", "DOS", "WIDTH", "PALETTE", "HSCREEN", "LPOKE",
    "HCLS", "HCOLOR", "HPAINT", "HCIRCLE", "HLINE", "HGET", "HPUT", "HBUFF",
    "HPRINT", "ERR", "BRK", "LOCATE", "HSTAT", "HSET", "HRESET", "HDRAW",
    "CMP", "RGB", "ATTR",
]

COCO_FUNCTIONS = [
    "SGN", "INT", "ABS", "USR", "RND", "SIN", "PEEK", "LEN", "STR$", "VAL",
    "ASC", "CHR$", "EOF", "ReadJoystick", "LEFT$", "RIGHT$", "MID$", "POINT",
    "INKEY$", "MEM", "ATN", "COS", "TAN", "EXP", "FIX", "LOG", "POS", "SQR",
    "HEX$", "VARPTR", "INSTR", "TIMER", "GetPixel", "STRING$", "CVN", "FREE",
    "LOC", "LOF", "MKN$", "AS", "LPEEK", "BUTTON", "HPOINT", "ERNO", "ERLIN",
]

# (shift, base, tokens) tried in order.
COCO_TABLES = [
    (0x00, 0x80, COCO_STATEMENTS),
    (0xFF, 0x80, COCO_FUNCTIONS),
]


def _split_basic_lines(text):
    """Yield lines the way the tokenizer reads them: break on \\r or \\n, so a
    CRLF leaves an empty line behind (which is then dropped because it does not
    start with a digit)."""
    line = []
    for ch in text:
        if ch in "\r\n":
            yield "".join(line)
            line = []
        else:
            line.append(ch)
    if line:
        yield "".join(line)


def _tokenize_body(raw, pos):
    """Tokenize the statements of one line (no link/lineno/terminator)."""
    out = bytearray()
    in_quotes = in_remark = in_data = False
    while pos < len(raw):
        ch = raw[pos]

        if in_remark:
            out.append(ord(ch) & 0xFF)
            pos += 1
            continue

        if in_data:
            out.append(ord(ch) & 0xFF)
            pos += 1
            if ch == ":":
                in_data = False
            continue

        if in_quotes:
            out.append(ord(ch) & 0xFF)
            pos += 1
            if ch == '"':
                in_quotes = False
            continue

        if ch == '"':
            in_quotes = True
            out.append(0x22)
            pos += 1
            continue

        if ch == '?':                       # abbreviation for PRINT
            out.append(TOK_PRINT)
            pos += 1
            continue

        token = None
        shift = value = 0
        for tshift, base, table in COCO_TABLES:
            for j, kw in enumerate(table):
                if raw.startswith(kw, pos):
                    token = kw
                    shift = tshift
                    value = (base + j) & 0xFF
                    pos += len(kw)
                    break
            if token is not None:
                break

        if token is not None:
            if shift:
                out.append(shift)
            elif value == TOK_APOS:          # ' is stored as ':' + ' token
                out.append(0x3A)
            out.append(value)
            if shift == 0:
                if value in (TOK_REM, TOK_APOS):
                    in_remark = True
                elif value == TOK_DATA:
                    in_data = True
        else:
            out.append(ord(ch) & 0xFF)
            pos += 1

    return out


def tokenize_cocobas(payload):
    text = payload.decode("latin1")
    program = bytearray()        # in-memory image: lines + 00 00, no FF/len header

    for raw in _split_basic_lines(text):
        if not raw or not raw[0].isdigit():
            continue
        pos = 0
        line_number = 0
        while pos < len(raw) and raw[pos].isdigit():
            line_number = (line_number * 10 + (ord(raw[pos]) - 48)) & 0xFFFF
            pos += 1
        while pos < len(raw) and raw[pos] in " \t\v\f":
            pos += 1

        body = _tokenize_body(raw, pos)
        line_len = 4 + len(body) + 1
        link = (TXTTAB + len(program) + line_len) & 0xFFFF
        program += bytes(((link >> 8) & 0xFF, link & 0xFF,
                          (line_number >> 8) & 0xFF, line_number & 0xFF))
        program += body
        program.append(0x00)

    program += b"\x00\x00"        # end of program

    length = len(program) & 0xFFFF
    return bytes((0xFF, (length >> 8) & 0xFF, length & 0xFF)) + bytes(program)


# ---------------------------------------------------------------------------
# Color BASIC detokenizer (copy -t from an image, list -t)
#
# Follows ToolShed's _decb_detoken semantics -- skip the FF/length header,
# walk line links until a zero link, print "<lineno> " then the statements,
# drop the colon the tokenizer inserts before ' (0x83) and ELSE (0x84),
# print "!" for a token with no table entry -- but uses this file's
# ROM-verified tables (see the module docstring for why).
# ---------------------------------------------------------------------------

def detokenize_cocobas(data):
    pos = 0
    if data[:1] == b"\xFF":
        if len(data) < 5:
            die("not a valid tokenized BASIC file (truncated header)")
        hdr_len = (data[1] << 8) | data[2]
        if hdr_len > len(data) - 3:
            die("not a valid tokenized BASIC file "
                "(header length %d exceeds file size)" % hdr_len)
        pos = 3

    out = bytearray()
    while pos + 2 <= len(data):
        link = (data[pos] << 8) | data[pos + 1]
        pos += 2
        if link == 0:
            break
        if pos + 2 > len(data):
            break                        # truncated line header; stop quietly
        line_number = (data[pos] << 8) | data[pos + 1]
        pos += 2
        out += b"%d " % line_number

        while pos < len(data):
            ch = data[pos]
            pos += 1
            if ch == 0x00:
                break
            if ch == 0xFF:               # function token follows
                if pos < len(data):
                    idx = data[pos] - 0x80
                    pos += 1
                    if 0 <= idx < len(COCO_FUNCTIONS):
                        out += COCO_FUNCTIONS[idx].encode("latin1")
                    else:
                        out += b"!"
                continue
            if ch >= 0x80:               # statement token
                idx = ch - 0x80
                if idx < len(COCO_STATEMENTS):
                    out += COCO_STATEMENTS[idx].encode("latin1")
                else:
                    out += b"!"
                continue
            if (ch == 0x3A and pos < len(data)
                    and data[pos] in (TOK_APOS, 0x84)):
                continue                 # drop ':' before ' and ELSE
            out.append(ch)
        out += b"\n"
    return bytes(out)


# ---------------------------------------------------------------------------
# End-of-line translation (copy -l), matching ToolShed's cococonv.c:
# host -> image scans for the first line ending to classify the file
# (CRLF: strip every LF; lone LF: LF -> CR; lone CR: already Disk BASIC),
# image -> host is a plain CR -> LF.
# ---------------------------------------------------------------------------

def native_to_decb(buf):
    for i, b in enumerate(buf):
        if b == 0x0D and i + 1 < len(buf) and buf[i + 1] == 0x0A:
            return buf.replace(b"\n", b"")
        if b == 0x0A:
            return buf.replace(b"\n", b"\r")
        if b == 0x0D:
            return buf
    return buf


def decb_to_native(buf):
    return buf.replace(b"\r", b"\n")


# ---------------------------------------------------------------------------
# DECB segmented ML binaries: 00 <len16> <addr16> <data> per segment,
# FF 00 00 <exec16> at the end. copy -c and list -s both parse this.
# ---------------------------------------------------------------------------

def parse_decb_segments(buf, what):
    """Parse a segmented binary. Returns (segment list, exec address).

    Refuses anything that isn't the real format; ToolShed would march
    through garbage and emit garbage.
    """
    segs = []
    pos = 0
    while True:
        if pos >= len(buf):
            die("%s: no postamble; not a DECB segmented binary" % what)
        t = buf[pos]
        if t == 0xFF:
            if pos + 5 > len(buf):
                die("%s: truncated postamble" % what)
            if buf[pos + 1] or buf[pos + 2]:
                sys.stderr.write("decb: %s: ignoring non-null postamble "
                                 "word %02X%02X\n"
                                 % (what, buf[pos + 1], buf[pos + 2]))
            return segs, (buf[pos + 3] << 8) | buf[pos + 4]
        if t != 0x00:
            die("%s: byte 0x%02X where a segment preamble should be; "
                "not a DECB segmented binary" % (what, t))
        if pos + 5 > len(buf):
            die("%s: truncated segment preamble" % what)
        length = (buf[pos + 1] << 8) | buf[pos + 2]
        addr = (buf[pos + 3] << 8) | buf[pos + 4]
        pos += 5
        if pos + length > len(buf):
            die("%s: segment at 0x%04X runs past end of file"
                % (what, addr))
        segs.append((addr, buf[pos:pos + length]))
        pos += length


def binconcat(buf):
    """copy -c: load all segments into a 64K image, then re-emit each
    maximal contiguous occupied run as one segment. Overlaps resolve
    the way RAM does: last write wins. Merges and normalizes; does not
    zero-fill gaps. ToolShed's version drops a byte loaded at $FFFF
    (both its loops stop at $FFFE); this one doesn't."""
    segs, exec_addr = parse_decb_segments(buf, "copy -c")
    ram = [-1] * 0x10000
    for addr, data in segs:
        for i, b in enumerate(data):
            ram[(addr + i) & 0xFFFF] = b

    out = bytearray()
    i = 0
    while i < 0x10000:
        if ram[i] < 0:
            i += 1
            continue
        j = i
        while j < 0x10000 and ram[j] >= 0:
            j += 1
        out += bytes((0x00, ((j - i) >> 8) & 0xFF, (j - i) & 0xFF,
                      (i >> 8) & 0xFF, i & 0xFF))
        out += bytes(ram[i:j])
        i = j
    out += bytes((0xFF, 0x00, 0x00,
                  (exec_addr >> 8) & 0xFF, exec_addr & 0xFF))
    return bytes(out)


def srec_encode(buf):
    """list -s: Motorola S-records from a DECB segmented binary.
    S1 data records, 32 bytes each, standard one's-complement
    checksums, S9 terminator carrying the exec address."""
    segs, exec_addr = parse_decb_segments(buf, "list -s")
    out = []
    for addr, data in segs:
        pos = 0
        while pos < len(data):
            chunk = data[pos:pos + 32]
            a = (addr + pos) & 0xFFFF
            count = len(chunk) + 3
            total = count + (a >> 8) + (a & 0xFF) + sum(chunk)
            out.append("S1%02X%04X%s%02X"
                       % (count, a,
                          "".join("%02X" % b for b in chunk),
                          (0xFF - total) & 0xFF))
            pos += 32
    total = 3 + (exec_addr >> 8) + (exec_addr & 0xFF)
    out.append("S903%04X%02X" % (exec_addr, (0xFF - total) & 0xFF))
    return ("\n".join(out) + "\n").encode("ascii")


def granule_track(g):
    t = g // 2
    if t >= DIR_TRACK:
        t += 1
    return t


def granule_offset(g, base=0):
    t = granule_track(g)
    half = 0 if (g % 2 == 0) else GRANULE_SECTORS
    return base + (t * SPT + half) * SECTOR


def track_offset(track, base=0):
    return base + track * SPT * SECTOR


def gat_offset(base=0):
    return track_offset(DIR_TRACK, base) + GAT_SECTOR * SECTOR


def num_granules(tracks):
    return (tracks - 1) * 2


def disk_tracks(data):
    return len(data) // (SPT * SECTOR)


# ---------------------------------------------------------------------------
# dskini
# ---------------------------------------------------------------------------

def cmd_dskini(args):
    tracks = 35
    disk = None
    disks = []
    hdbdrives = 1
    disk_name = None
    skitzo = False

    for a in args:
        if a.startswith("-") and len(a) > 1:
            flags = a[1:]
            i = 0
            while i < len(flags):
                c = flags[i]
                if c in ("3", "4", "8"):
                    tracks = TRACK_SIZES[int(c)]
                elif c == "h":           # HDB-DOS drives; eats rest of arg
                    try:
                        hdbdrives = int(flags[i + 1:])
                    except ValueError:
                        die("dskini: -h needs a drive count (e.g. -h124)")
                    if hdbdrives < 1:
                        die("dskini: -h drive count must be at least 1")
                    tracks = 35          # ToolShed forces 35-track drives
                    break
                elif c == "n":           # HDB-DOS disk name; eats rest of arg
                    disk_name = flags[i + 1:]
                    if len(disk_name) > HDB_NAME_MAX:
                        die("dskini: HDB-DOS disk name %r is too long "
                            "(%d chars max)" % (disk_name, HDB_NAME_MAX))
                    break
                elif c == "s":           # skitzo disk
                    skitzo = True
                else:
                    die("dskini: unknown option -%s" % c)
                i += 1
        else:
            disks.append(a)
            disk = a

    if disk is None:
        die("dskini: no disk image specified")
    if hdbdrives > 1 and tracks != 35:
        die("dskini: HDB-DOS packs are 35-track drives; -h can't be "
            "combined with -%d" % {40: 4, 80: 8}[tracks])

    drive = bytearray(b"\xFF" * (tracks * SPT * SECTOR))
    if skitzo:
        # Reserve granules 0..33 (0x00 = allocated, chained to granule 0)
        # so a dual-personality OS-9/DECB disk's OS-9 half stays untouched.
        goff = gat_offset()
        for g in range(SKITZO_GRANULES):
            drive[goff + g] = 0x00
    if disk_name is not None:
        noff = track_offset(DIR_TRACK) + HDB_NAME_SECTOR * SECTOR
        field = disk_name.encode("latin1") + b"\x00"
        drive[noff:noff + len(field)] = field

    for disk in disks:
        with open(disk, "wb") as f:
            for _ in range(hdbdrives):
                f.write(drive)


# ---------------------------------------------------------------------------
# copy
# ---------------------------------------------------------------------------

def split_image_path(arg):
    """Return (disk, name, drive) if arg is 'disk,NAME[:drive]', else None.

    drive is None unless an HDB-DOS :N suffix is present (ToolShed's
    'foo,bar.bin:1' syntax -- second 35-track drive in a multi-drive pack).
    """
    if "," not in arg:
        return None
    disk, _, name = arg.partition(",")
    drive = None
    if ":" in name:
        name, _, dstr = name.rpartition(":")
        drive = parse_drive_suffix(dstr)
        if drive is None:
            die("bad drive suffix %r in %r (want :<n> or :<n>+<sectors>)"
                % (dstr, arg))
    return disk, name, drive


def parse_drive_suffix(dstr):
    """':<n>' or ':<n>+<sectors>' (decimal or 0x hex sector offset,
    ToolShed's HDB-in-OS-9-partition syntax). Returns (drive, extra
    byte offset) or None if it doesn't parse."""
    off = 0
    if "+" in dstr:
        dstr, _, ostr = dstr.partition("+")
        try:
            off = int(ostr, 0) * SECTOR
        except ValueError:
            return None
    if not dstr.isdigit():
        return None
    return int(dstr), off


def load_disk(diskfile, drive):
    """Read a disk image; return (image, base, tracks) for the selected view.

    With an explicit drive number the view is that drive's 35-track slice of
    an HDB-DOS pack. Without one, an image that is an exact multiple of
    (and larger than) one 35-track disk is treated as an HDB-DOS pack and
    the view defaults to drive 0, matching ToolShed; anything else is a
    single disk spanning the whole file.
    """
    try:
        with open(diskfile, "rb") as f:
            image = bytearray(f.read())
    except OSError as e:
        die("cannot open disk image %s: %s" % (diskfile, e.strerror))

    # JVC header: any 1-255 byte remainder past whole sectors. Read and
    # written transparently; the header bytes ride along untouched in
    # the image buffer and base skips over them. Only plain geometry is
    # accepted: 18 sectors/track, 1 side, 256-byte sectors, first
    # sector ID 1, no per-sector attributes.
    hdr = len(image) % SECTOR
    if hdr:
        h = image[:hdr]
        if ((hdr >= 1 and h[0] != SPT) or (hdr >= 2 and h[1] != 1)
                or (hdr >= 3 and h[2] != 1) or (hdr >= 4 and h[3] != 1)
                or (hdr >= 5 and h[4] != 0)):
            die("%s has a JVC header with unsupported geometry "
                "(need 18 spt, 1 side, 256-byte sectors, first ID 1, "
                "no sector attributes)" % diskfile)
    data_len = len(image) - hdr

    if drive is not None:
        n, extra = drive
        base = hdr + n * DRIVE_SIZE + extra
        if base + DRIVE_SIZE > len(image):
            die("drive %d%s is beyond the end of %s"
                % (n, " (+%d bytes)" % extra if extra else "", diskfile))
        return image, base, 35

    if data_len % (SPT * SECTOR) != 0:
        die("%s is not a whole number of tracks" % diskfile)
    if data_len > DRIVE_SIZE and data_len % DRIVE_SIZE == 0:
        return image, hdr, 35            # HDB-DOS pack, default drive 0
    return image, hdr, data_len // (SPT * SECTOR)


def save_disk(diskfile, image):
    with open(diskfile, "wb") as f:
        f.write(image)


def entry_to_name(entry):
    """Directory entry bytes -> 'NAME.EXT' host-style string."""
    base = entry[0:8].decode("latin1").rstrip(" ")
    ext = entry[8:11].decode("latin1").rstrip(" ")
    return "%s.%s" % (base, ext) if ext else base


def iter_dir_entries(data, base=0):
    """Yield (offset, first_byte) for all 72 directory slots."""
    for sec in range(DIR_FIRST_SECTOR, DIR_LAST_SECTOR + 1):
        soff = track_offset(DIR_TRACK, base) + sec * SECTOR
        for e in range(0, SECTOR, ENTRY_SIZE):
            off = soff + e
            yield off, data[off]


def find_entry(data, name_field, base=0):
    """Return the absolute offset of the live entry named name_field, or None."""
    for off, first in iter_dir_entries(data, base):
        if first not in (FREE, DELETED):
            if data[off:off + 11] == name_field:
                return off
    return None


def chain_granules(data, first_gran, total_grans, base=0):
    """Walk a FAT chain; return (granule list, terminator byte)."""
    gat = gat_offset(base)
    grans = []
    g = first_gran
    while True:
        if g >= total_grans or len(grans) > total_grans:
            die("corrupt granule chain (loop or out-of-range granule %d)" % g)
        grans.append(g)
        nxt = data[gat + g]
        if nxt >= 0xC0:
            return grans, nxt
        g = nxt


def entry_geometry(data, entry, total_grans, base=0):
    """Chain facts for a live entry: (granules, sectors, byte size)."""
    grans, terminator = chain_granules(data, entry[13], total_grans, base)
    last_sectors = terminator & 0x3F
    total_sectors = (len(grans) - 1) * GRANULE_SECTORS + last_sectors
    if total_sectors == 0:
        return grans, 0, 0
    lss = (entry[14] << 8) | entry[15]
    if lss == 0 or lss > SECTOR:
        lss = SECTOR                     # DECB stores a full sector as 0x0100
    return grans, total_sectors, (total_sectors - 1) * SECTOR + lss


def read_entry_payload(data, entry, base=0, tracks=None):
    """Return the payload bytes for a live directory entry."""
    if tracks is None:
        tracks = disk_tracks(data)
    grans, _, size = entry_geometry(data, entry, num_granules(tracks), base)
    payload = bytearray()
    for g in grans:
        goff = granule_offset(g, base)
        payload += data[goff:goff + GRANULE]
    return bytes(payload[:size])


def read_file(data, name, base=0, tracks=None):
    """Return (payload bytes, file_type, ascii_flag) for disk file `name`."""
    off = find_entry(data, encode_name(name), base)
    if off is None:
        die("file %s not found on disk" % name.upper())
    entry = bytes(data[off:off + ENTRY_SIZE])
    return (read_entry_payload(data, entry, base, tracks),
            entry[11], entry[12])


def deleted_status(data, entry, total_grans, base=0):
    """Classify a deleted entry: (recoverable?, reason string)."""
    first = entry[13]
    if first >= total_grans:
        return False, "deleted, bad first granule (%d)" % first
    if data[gat_offset(base) + first] != FREE:
        return False, "deleted, granule %d reallocated" % first
    return True, "deleted, recoverable"


def find_deleted(data, name_field, base=0):
    """All deleted entries matching name_field on bytes 1-10.

    KILL zeroes the first character of the name, so it can't take part
    in the match; PROG.BAS and ?ROG.BAS find the same entries.
    """
    hits = []
    for off, first in iter_dir_entries(data, base):
        if first == DELETED and data[off + 1:off + 11] == name_field[1:11]:
            hits.append(off)
    return hits


def trim_recovered(raw, lss):
    """Best-evidence length trim for a recovered granule run.

    Returns (payload, method description). The chain and length died
    with the KILL; this reconstructs the length from what survives:
      1. tokenized BASIC: the FF/length header is exact
      2. DECB ML binary: segment preamble/postamble structure is exact
      3. anything else: strip the trailing disk-fill run (0xFF from
         dskini, 0x00 from wipe), then round up to the first size
         consistent with the entry's last-sector byte count
    """
    if raw[:1] == b"\xFF" and len(raw) >= 3:
        n = (raw[1] << 8) | raw[2]
        if 0 < n <= len(raw) - 3:
            return raw[:3 + n], "exact, from the tokenized BASIC header"

    if raw[:1] == b"\x00":
        pos = 0
        while pos + 5 <= len(raw) and raw[pos] == 0x00:
            pos += 5 + ((raw[pos + 1] << 8) | raw[pos + 2])
        if (pos + 5 <= len(raw) and raw[pos] == 0xFF
                and raw[pos + 1] == 0 and raw[pos + 2] == 0):
            return raw[:pos + 5], "exact, from the DECB binary postamble"

    t = len(raw)
    if raw and raw[-1] in (0xFF, 0x00):
        fill = raw[-1]
        while t > 0 and raw[t - 1] == fill:
            t -= 1
    if t == 0:
        return b"", "nothing left but disk fill"
    s = t + ((lss - t) % SECTOR)
    if s > len(raw):
        s = t
    return raw[:s], "approximate, trailing-fill trim rounded to the " \
                    "last-sector byte count"


def recover_deleted(data, name, base=0, tracks=None):
    """Recover a deleted file. Returns (payload, ftype, ascii, note)."""
    if tracks is None:
        tracks = disk_tracks(data)
    total_grans = num_granules(tracks)
    hits = find_deleted(data, encode_name(name), base)
    if not hits:
        die("copy: no deleted file matching %s (dir -d lists them)"
            % name.upper())
    if len(hits) > 1:
        die("copy: %d deleted entries match %s (first granules: %s); "
            "recover from a disk with fewer casualties"
            % (len(hits), name.upper(),
               ", ".join(str(data[o + 13]) for o in hits)))
    off = hits[0]
    entry = bytes(data[off:off + ENTRY_SIZE])

    ok, why = deleted_status(data, entry, total_grans, base)
    if not ok:
        die("copy: %s is not recoverable (%s)" % (name.upper(), why))

    # The free run can plough through the remains of OTHER deleted
    # files. Their first granules survive in the directory, so stop
    # the run at the first one that belongs to someone else.
    stops = set()
    for o, first_b in iter_dir_entries(data, base):
        if first_b == DELETED and o != off:
            stops.add(data[o + 13])

    gat = gat_offset(base)
    run = [entry[13]]
    g = entry[13] + 1
    while g < total_grans and data[gat + g] == FREE and g not in stops:
        run.append(g)
        g += 1

    raw = bytearray()
    for g in run:
        goff = granule_offset(g, base)
        raw += data[goff:goff + GRANULE]

    lss = (entry[14] << 8) | entry[15]
    if lss == 0 or lss > SECTOR:
        lss = SECTOR
    payload, method = trim_recovered(bytes(raw), lss)
    note = ("recovered %s: %d bytes from a %d-granule free run (%s)"
            % (entry_to_name(b"?" + entry[1:11]), len(payload),
               len(run), method))
    return payload, entry[11], entry[12], note


def encode_name(name):
    name = name.upper().replace("/", "").replace("\\", "")
    if "." in name:
        base, _, ext = name.rpartition(".")
    else:
        base, ext = name, ""
    if len(base) > 8 or len(ext) > 3:
        die("copy: filename %r does not fit 8.3" % name)
    field = base.ljust(8) + ext.ljust(3)
    return field.encode("latin1")


def find_free_granules(data, count, total_grans, base=0):
    gat_off = gat_offset(base)
    free = [g for g in range(total_grans)
            if data[gat_off + g] == FREE]
    if len(free) < count:
        die("copy: disk full (need %d granules, %d free)" % (count, len(free)))
    return free[:count]


def find_dir_entry(data, name_field, rewrite, base=0):
    """Return the absolute offset of the directory slot to use."""
    first_free = None
    for off, first in iter_dir_entries(data, base):
        if first not in (FREE, DELETED):
            if data[off:off + 11] == name_field:
                if not rewrite:
                    die("copy: file exists (use -r to overwrite)")
                return off
        elif first_free is None:
            first_free = off
    if first_free is None:
        die("copy: directory full")
    return first_free


def free_chain(data, first_gran, total_grans, base=0):
    gat_off = gat_offset(base)
    g = first_gran
    seen = 0
    while seen <= total_grans:
        nxt = data[gat_off + g]
        data[gat_off + g] = FREE
        if nxt & 0xC0 == 0xC0:
            break
        g = nxt
        seen += 1


def write_file(data, name, payload, ftype, ascii_flag, rewrite,
               base=0, tracks=None):
    if tracks is None:
        tracks = disk_tracks(data)
    total_grans = num_granules(tracks)
    gat_off = gat_offset(base)
    name_field = encode_name(name)

    size = len(payload)
    total_sectors = (size + SECTOR - 1) // SECTOR
    if total_sectors == 0:
        total_sectors = 0
    full_grans = total_sectors // GRANULE_SECTORS
    last_gran_sectors = total_sectors - full_grans * GRANULE_SECTORS
    if last_gran_sectors == 0 and full_grans > 0:
        full_grans -= 1
        last_gran_sectors = GRANULE_SECTORS
    gran_count = full_grans + (1 if last_gran_sectors else 0)
    if gran_count == 0:        # empty file occupies one granule, zero sectors
        gran_count = 1
        last_gran_sectors = 0

    last_sector_bytes = size - (total_sectors - 1) * SECTOR if size else 0

    dir_off = find_dir_entry(data, name_field, rewrite, base)
    if data[dir_off] not in (FREE, DELETED):
        free_chain(data, data[dir_off + 13], total_grans, base)

    grans = find_free_granules(data, gran_count, total_grans, base)

    # write the granule chain into the GAT
    for i, g in enumerate(grans):
        if i == len(grans) - 1:
            data[gat_off + g] = 0xC0 | (last_gran_sectors & 0x3F)
        else:
            data[gat_off + g] = grans[i + 1]

    # lay the file data into the granule sectors
    pos = 0
    for g in grans:
        goff = granule_offset(g, base)
        chunk = payload[pos:pos + GRANULE]
        data[goff:goff + len(chunk)] = chunk
        pos += GRANULE

    # build the directory entry
    entry = bytearray(b"\x00" * ENTRY_SIZE)
    entry[0:11] = name_field
    entry[11] = ftype
    entry[12] = ascii_flag
    entry[13] = grans[0]
    entry[14] = (last_sector_bytes >> 8) & 0xFF
    entry[15] = last_sector_bytes & 0xFF
    data[dir_off:dir_off + ENTRY_SIZE] = entry


def cmd_copy(args):
    ftype = None       # None = inherit (image source) or default (host: 2)
    ascii_flag = None  # None = inherit (image source) or default (host: 0x00)
    rewrite = False
    tokenize = False
    eol = False
    deleted = False
    concat = False
    positional = []

    for a in args:
        if a.startswith("-") and "," not in a and len(a) > 1:
            for c in a[1:]:
                if c in "0123":
                    ftype = int(c)
                elif c == "a":
                    ascii_flag = 0xFF
                elif c == "b":
                    ascii_flag = 0x00
                elif c == "r":
                    rewrite = True
                elif c == "t":
                    tokenize = True
                elif c == "l":
                    eol = True
                elif c == "d":
                    deleted = True
                elif c == "c":
                    concat = True
                else:
                    die("copy: unknown option -%s" % c)
        else:
            positional.append(a)

    if len(positional) < 2:
        die("copy: need at least one source and a target")

    target = positional[-1]
    sources = positional[:-1]
    tgt = split_image_path(target)

    # Work out the destination. An image target with an empty name, or a
    # host directory, is a "directory" target: each source keeps its own
    # filename. Anything else names a single destination file.
    if tgt is not None:
        dst_disk, dst_name, dst_drive = tgt
        dst_is_image = True
        dst_is_dir = (dst_name == "")
    else:
        dst_is_image = False
        dst_is_dir = os.path.isdir(target)
    if not dst_is_dir and len(sources) > 1:
        die("copy: two or more sources requires the target to be a "
            "directory (host dir or 'disk,')")

    dst_data = dst_base = dst_tracks = None
    if dst_is_image:
        dst_data, dst_base, dst_tracks = load_disk(dst_disk, dst_drive)

    for src in sources:
        s = split_image_path(src)
        per_ftype, per_ascii = ftype, ascii_flag
        if s is not None:
            src_disk, src_name, src_drive = s
            if src_name == "":
                die("copy: source %r names a disk, not a file on it" % src)
            # Same image file and drive as the destination: work on the
            # in-memory copy so earlier writes in this run are visible.
            if (dst_is_image
                    and os.path.abspath(src_disk) == os.path.abspath(dst_disk)
                    and src_drive == dst_drive):
                sdata, sbase, stracks = dst_data, dst_base, dst_tracks
            else:
                sdata, sbase, stracks = load_disk(src_disk, src_drive)
            if deleted:
                payload, src_ftype, src_ascii, note = recover_deleted(
                    sdata, src_name, sbase, stracks)
                sys.stdout.write("copy: %s\n" % note)
            else:
                payload, src_ftype, src_ascii = read_file(
                    sdata, src_name, sbase, stracks)
            if per_ftype is None:
                per_ftype = src_ftype
            if per_ascii is None:
                per_ascii = src_ascii
            base_name = entry_to_name(encode_name(src_name))
            src_is_image = True
        else:
            if deleted:
                die("copy: -d recovers deleted files from a disk image; "
                    "%s is a host path" % src)
            try:
                with open(src, "rb") as f:
                    payload = f.read()
            except OSError as e:
                die("copy: cannot open %s: %s" % (src, e.strerror))
            base_name = os.path.basename(src)
            src_is_image = False

        do_eol = eol
        if concat:
            payload = binconcat(payload)
        if tokenize:
            # Mirrors ToolShed: a leading 0xFF means already tokenized, so
            # detokenize (type 0, ASCII); otherwise tokenize (type 0,
            # binary) and skip EOL translation on the tokenized bytes.
            if payload[:1] == b"\xFF":
                payload = detokenize_cocobas(payload)
                per_ftype, per_ascii = 0, 0xFF
            else:
                payload = tokenize_cocobas(payload)
                per_ftype, per_ascii = 0, 0x00
                do_eol = False

        if do_eol:
            if not src_is_image and dst_is_image:
                payload = native_to_decb(payload)
            elif src_is_image and not dst_is_image:
                payload = decb_to_native(payload)

        if dst_is_image:
            name = base_name if dst_is_dir else dst_name
            write_file(dst_data, name, payload,
                       per_ftype if per_ftype is not None else 2,
                       per_ascii if per_ascii is not None else 0x00,
                       rewrite, dst_base, dst_tracks)
        else:
            out = (os.path.join(target, base_name) if dst_is_dir
                   else target)
            if os.path.exists(out) and not rewrite:
                die("copy: %s exists (use -r to overwrite)" % out)
            with open(out, "wb") as f:
                f.write(payload)

    if dst_is_image:
        save_disk(dst_disk, dst_data)


# ---------------------------------------------------------------------------
# dir / free / kill / list / attr / rename
# ---------------------------------------------------------------------------

def normalize_disk_arg(p):
    """ToolShed lets you say 'foo' or 'foo:2' for 'foo,' / 'foo,:2'.

    Unlike ToolShed's do_dir, a colon only counts as an HDB-DOS drive
    suffix when everything after it is digits, so a bare Windows path
    like C:\\disks\\foo.dsk stays intact instead of being split at the
    drive letter.
    """
    if "," in p:
        return p
    q = p.rfind(":")
    if q > 0 and parse_drive_suffix(p[q + 1:]) is not None:
        return p[:q] + "," + p[q:]
    return p + ","


def cmd_dir(args):
    show_deleted = False
    paths = []
    for a in args:
        if a.startswith("-") and "," not in a and len(a) > 1:
            for c in a[1:]:
                if c == "d":
                    show_deleted = True
                else:
                    die("dir: unknown option -%s" % c)
        else:
            paths.append(a)
    if not paths:
        die("dir: no disk image specified")
    for raw in paths:
        p = normalize_disk_arg(raw)
        disk, _, drive = split_image_path(p)
        data, base, tracks = load_disk(disk, drive)
        total_grans = num_granules(tracks)
        gat = gat_offset(base)

        sys.stdout.write("Directory of: %s\n\n" % p)

        # HDB-DOS disk name, if any (track 17, sector 17).
        noff = track_offset(DIR_TRACK, base) + HDB_NAME_SECTOR * SECTOR
        if data[noff] != 0xFF:
            name_bytes = bytes(data[noff:noff + SECTOR]).split(b"\x00", 1)[0]
            sys.stdout.write("%s\n" % name_bytes.decode("latin1"))

        for off, first in iter_dir_entries(data, base):
            if first == FREE:
                continue
            entry = data[off:off + ENTRY_SIZE]

            if first == DELETED:
                if not show_deleted:
                    continue
                if not any(entry[1:11]):
                    continue             # scrubbed by wipe, nothing there
                # KILL took the first character of the name with it.
                out = ["?"]
                for b in entry[1:8]:
                    out.append(chr(b) if 0x20 <= b <= 0x7E else "\\%o" % b)
                out.append(" ")
                for b in entry[8:11]:
                    out.append(chr(b) if 0x20 <= b <= 0x7E else "\\%o" % b)
                _, why = deleted_status(data, entry, total_grans, base)
                sys.stdout.write("%s  %d  %s  ?  [%s]\n"
                                 % ("".join(out), entry[11],
                                    {0x00: "B", 0xFF: "A"}.get(entry[12],
                                                               "?"),
                                    why))
                continue

            if entry[12] == 0x00:
                aflag = "B"
            elif entry[12] == 0xFF:
                aflag = "A"
            else:
                aflag = "?"

            # Count granules in the chain the way ToolShed's dir does,
            # but bail out on corrupt chains instead of spinning.
            gran_size = 1
            g = entry[13]
            while (g < total_grans and data[gat + g] < 0xC0
                   and gran_size <= total_grans):
                g = data[gat + g]
                gran_size += 1

            out = []
            for b in entry[0:8]:
                out.append(chr(b) if 0x20 <= b <= 0x7E else "\\%o" % b)
            out.append(" ")
            for b in entry[8:11]:
                out.append(chr(b) if 0x20 <= b <= 0x7E else "\\%o" % b)
            sys.stdout.write("%s  %d  %s  %d\n"
                             % ("".join(out), entry[11], aflag, gran_size))


def cmd_free(args):
    if not args:
        die("free: no disk image specified")
    for raw in args:
        p = normalize_disk_arg(raw)
        disk, _, drive = split_image_path(p)
        data, base, tracks = load_disk(disk, drive)
        total_grans = num_granules(tracks)
        gat = gat_offset(base)
        free_grans = sum(1 for g in range(total_grans)
                         if data[gat + g] == FREE)
        sys.stdout.write("Free granules: %d (%d bytes)\n"
                         % (free_grans, free_grans * GRANULE))


def cmd_kill(args):
    if not args:
        die("kill: no files specified")
    for raw in args:
        s = split_image_path(raw)
        if s is None or s[1] == "":
            die("kill: %r is not an image file path (disk,NAME)" % raw)
        disk, name, drive = s
        data, base, tracks = load_disk(disk, drive)
        off = find_entry(data, encode_name(name), base)
        if off is None:
            die("kill: file %s not found on %s" % (name.upper(), disk))
        first_gran = data[off + 13]
        data[off] = DELETED
        free_chain(data, first_gran, num_granules(tracks), base)
        save_disk(disk, data)


def cmd_list(args):
    detoken = False
    srec = False
    paths = []
    for a in args:
        if a.startswith("-") and "," not in a and len(a) > 1:
            for c in a[1:]:
                if c == "t":
                    detoken = True
                elif c == "s":
                    srec = True
                else:
                    die("list: unknown option -%s" % c)
        else:
            paths.append(a)
    if not paths:
        die("list: no files specified")

    for p in paths:
        s = split_image_path(p)
        if s is not None:
            disk, name, drive = s
            if name == "":
                die("list: %r names a disk, not a file on it" % p)
            data, base, tracks = load_disk(disk, drive)
            payload, _, _ = read_file(data, name, base, tracks)
        else:
            try:
                with open(p, "rb") as f:
                    payload = f.read()
            except OSError as e:
                die("list: cannot open %s: %s" % (p, e.strerror))
        if detoken:
            payload = detokenize_cocobas(payload)
        if srec:
            payload = srec_encode(payload)
        sys.stdout.buffer.write(decb_to_native(payload))
        sys.stdout.buffer.flush()


FILE_TYPE_NAMES = {
    0: "BASIC program",
    1: "BASIC data file",
    2: "Machine-language program",
    3: "Text editor file",
}


def cmd_attr(args):
    ftype = None
    ascii_flag = None
    paths = []
    for a in args:
        if a.startswith("-") and "," not in a and len(a) > 1:
            for c in a[1:]:
                if c in "0123":
                    ftype = int(c)
                elif c == "a":
                    ascii_flag = 0xFF
                elif c == "b":
                    ascii_flag = 0x00
                else:
                    die("attr: unknown option -%s" % c)
        else:
            paths.append(a)
    if not paths:
        die("attr: no files specified")

    for p in paths:
        s = split_image_path(p)
        if s is None or s[1] == "":
            die("attr: %r is not an image file path (disk,NAME)" % p)
        disk, name, drive = s
        data, base, tracks = load_disk(disk, drive)
        off = find_entry(data, encode_name(name), base)
        if off is None:
            die("attr: file %s not found on %s" % (name.upper(), disk))
        if ftype is not None or ascii_flag is not None:
            if ftype is not None:
                data[off + 11] = ftype
            if ascii_flag is not None:
                data[off + 12] = ascii_flag
            save_disk(disk, data)
        ft, dt = data[off + 11], data[off + 12]
        sys.stdout.write("File type:  %s (%d)\n"
                         % (FILE_TYPE_NAMES.get(ft, "???"), ft))
        sys.stdout.write("Data type:  %s (%d)\n"
                         % ({0xFF: "ASCII", 0x00: "Binary"}.get(dt, "???"),
                            dt))
        sys.stdout.write("\n")


def cmd_rename(args):
    paths = [a for a in args if not (a.startswith("-") and len(a) > 1
                                     and "," not in a)]
    if len(paths) != 2:
        die("rename: usage is rename <disk,OLDNAME> <NEWNAME>")
    s = split_image_path(paths[0])
    if s is None or s[1] == "":
        die("rename: %r is not an image file path (disk,NAME)" % paths[0])
    disk, old_name, drive = s
    new_name = paths[1]
    if "," in new_name:                  # allow 'disk,NEW' too; use the name
        new_name = new_name.partition(",")[2]

    data, base, tracks = load_disk(disk, drive)
    off = find_entry(data, encode_name(old_name), base)
    if off is None:
        die("rename: file %s not found on %s" % (old_name.upper(), disk))
    new_field = encode_name(new_name)
    if find_entry(data, new_field, base) is not None:
        die("rename: file %s already exists on %s" % (new_name.upper(), disk))
    data[off:off + 11] = new_field
    save_disk(disk, data)


def cmd_wipe(args):
    if not args:
        die("wipe: no disk image specified")
    for raw in args:
        p = normalize_disk_arg(raw)
        disk, _, drive = split_image_path(p)
        data, base, tracks = load_disk(disk, drive)
        total_grans = num_granules(tracks)
        gat = gat_offset(base)

        zeroed = 0
        for g in range(total_grans):
            b = data[gat + g]
            if b == FREE:
                lo, hi = 0, GRANULE_SECTORS
            elif b >= 0xC0:
                # terminal granule: sectors past the used count are slack
                lo, hi = min(b & 0x3F, GRANULE_SECTORS), GRANULE_SECTORS
            else:
                continue                 # mid-chain or hidden (GAT 0x00)
            for s in range(lo, hi):
                off = granule_offset(g, base) + s * SECTOR
                data[off:off + SECTOR] = b"\x00" * SECTOR
                zeroed += 1

        scrubbed = 0
        for off, first in iter_dir_entries(data, base):
            if first == DELETED and any(data[off + 1:off + ENTRY_SIZE]):
                data[off:off + ENTRY_SIZE] = b"\x00" * ENTRY_SIZE
                scrubbed += 1

        save_disk(disk, data)
        sys.stdout.write(
            "wipe: %s: zeroed %d unused sectors, scrubbed %d deleted "
            "directory entries\n" % (p, zeroed, scrubbed))




def sanitize_host_name(name):
    """DECB allows bytes in names that host filesystems won't."""
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in name)


def cmd_copyall(args):
    detoken = eol = rewrite = False
    positional = []
    for a in args:
        if a.startswith("-") and "," not in a and len(a) > 1:
            for c in a[1:]:
                if c == "t":
                    detoken = True
                elif c == "l":
                    eol = True
                elif c == "r":
                    rewrite = True
                else:
                    die("copyall: unknown option -%s" % c)
        else:
            positional.append(a)
    if len(positional) != 2:
        die("copyall: usage is copyall [-t] [-l] [-r] <disk> <hostdir>")

    p = normalize_disk_arg(positional[0])
    disk, _, drive = split_image_path(p)
    outdir = positional[1]
    data, base, tracks = load_disk(disk, drive)
    os.makedirs(outdir, exist_ok=True)

    count = 0
    for off, first in iter_dir_entries(data, base):
        if first in (FREE, DELETED):
            continue
        entry = bytes(data[off:off + ENTRY_SIZE])
        name = entry_to_name(entry[0:11])
        payload = read_entry_payload(data, entry, base, tracks)

        note = ""
        if detoken and entry[11] == 0 and entry[12] == 0x00 \
                and payload[:1] == b"\xFF":
            payload = detokenize_cocobas(payload)
            note = ", detokenized"
        elif eol and entry[12] == 0xFF:
            payload = decb_to_native(payload)
            note = ", EOL translated"

        out = os.path.join(outdir, sanitize_host_name(name))
        if os.path.exists(out) and not rewrite:
            die("copyall: %s exists (use -r to overwrite)" % out)
        with open(out, "wb") as f:
            f.write(payload)
        sys.stdout.write("  %-12s -> %s (%d bytes%s)\n"
                         % (name, out, len(payload), note))
        count += 1
    sys.stdout.write("copyall: %s: %d files\n" % (p, count))


def cmd_stat(args):
    if not args:
        die("stat: no files specified")
    for raw in args:
        s = split_image_path(raw)
        if s is None or s[1] == "":
            die("stat: %r is not an image file path (disk,NAME)" % raw)
        disk, name, drive = s
        data, base, tracks = load_disk(disk, drive)
        total_grans = num_granules(tracks)
        off = find_entry(data, encode_name(name), base)
        if off is None:
            die("stat: file %s not found on %s" % (name.upper(), disk))
        entry = bytes(data[off:off + ENTRY_SIZE])
        grans, sectors, size = entry_geometry(data, entry, total_grans, base)
        terminator = data[gat_offset(base) + grans[-1]]
        lss = (entry[14] << 8) | entry[15]

        w = sys.stdout.write
        w("%s on %s\n" % (entry_to_name(entry[0:11]), disk))
        w("  file type:    %d (%s)\n"
          % (entry[11], FILE_TYPE_NAMES.get(entry[11], "???")))
        w("  data type:    %s (%d)\n"
          % ({0xFF: "ASCII", 0x00: "binary"}.get(entry[12], "???"),
             entry[12]))
        w("  size:         %d bytes in %d sectors, %d granules\n"
          % (size, sectors, len(grans)))
        w("  last sector:  %d bytes used (entry says %d)\n"
          % (size - (sectors - 1) * SECTOR if sectors else 0, lss))
        w("  chain:        %s -> C%X (%d sectors in last granule)\n"
          % (" -> ".join(str(g) for g in grans), terminator & 0x3F,
             terminator & 0x3F))
        w("  granule map:\n")
        for g in grans:
            lsn = (granule_offset(g, base) - base) // SECTOR
            w("    granule %2d  track %2d  LSN %4d-%d\n"
              % (g, granule_track(g), lsn, lsn + GRANULE_SECTORS - 1))


def parse_dump_where(w, tracks):
    """'324' = LSN, '300-305' = LSN range, '17.2' = track.sector (1-based)."""
    total = tracks * SPT
    try:
        if "." in w:
            t_s, s_s = w.split(".", 1)
            t, s = int(t_s), int(s_s)
            if not (0 <= t < tracks and 1 <= s <= SPT):
                die("dump: track %d sector %d is off the disk" % (t, s))
            lsn = t * SPT + s - 1
            return lsn, lsn
        if "-" in w:
            a_s, b_s = w.split("-", 1)
            a, b = int(a_s), int(b_s)
        else:
            a = b = int(w)
    except ValueError:
        die("dump: bad location %r (want LSN, LSN-LSN, or track.sector)" % w)
    if not (0 <= a <= b < total):
        die("dump: LSN range %d-%d is off the disk (%d sectors)"
            % (a, b, total))
    return a, b


def cmd_dump(args):
    positional = []
    for a in args:
        if a.startswith("-") and len(a) > 1 and not a[1].isdigit():
            die("dump: unknown option %s" % a)
        positional.append(a)
    if len(positional) < 2:
        die("dump: usage is dump <disk> <lsn|lsn-lsn|track.sector>...")
    p = normalize_disk_arg(positional[0])
    disk, _, drive = split_image_path(p)
    data, base, tracks = load_disk(disk, drive)

    for w in positional[1:]:
        lo, hi = parse_dump_where(w, tracks)
        for lsn in range(lo, hi + 1):
            sys.stdout.write("LSN %d (track %d, sector %d)\n"
                             % (lsn, lsn // SPT, lsn % SPT + 1))
            soff = base + lsn * SECTOR
            for row in range(0, SECTOR, 16):
                chunk = data[soff + row:soff + row + 16]
                hexs = " ".join("%02X" % b for b in chunk)
                text = "".join(chr(b) if 0x20 <= b <= 0x7E else "."
                               for b in chunk)
                sys.stdout.write("%04X  %-47s  |%s|\n" % (row, hexs, text))


def cmd_check(args):
    if not args:
        die("check: no disk image specified")
    rc = 0
    for raw in args:
        p = normalize_disk_arg(raw)
        disk, _, drive = split_image_path(p)
        data, base, tracks = load_disk(disk, drive)
        total_grans = num_granules(tracks)
        gat = gat_offset(base)
        problems = []
        notes = []

        # 1. Every GAT byte must be free, a valid link, or a terminator.
        for g in range(total_grans):
            b = data[gat + g]
            if b == FREE or b < total_grans:
                continue
            if 0xC0 <= b <= 0xC0 + GRANULE_SECTORS:
                continue
            problems.append("GAT[%d] = 0x%02X is not a valid link, "
                            "terminator, or free marker" % (g, b))

        # 2. Walk every live file's chain.
        owner = {}
        seen_names = {}
        deleted = 0
        for off, first in iter_dir_entries(data, base):
            if first == FREE:
                continue
            entry = bytes(data[off:off + ENTRY_SIZE])
            if first == DELETED:
                if any(entry[1:11]):
                    deleted += 1
                continue
            name = entry_to_name(entry[0:11])
            if name in seen_names:
                problems.append("duplicate directory entry for %s" % name)
            seen_names[name] = True
            if entry[11] > 3:
                problems.append("%s: file type %d is out of range"
                                % (name, entry[11]))
            if entry[12] not in (0x00, 0xFF):
                problems.append("%s: data type 0x%02X is neither ASCII "
                                "nor binary" % (name, entry[12]))

            g = entry[13]
            visited = set()
            sectors = None
            while True:
                if g >= total_grans:
                    problems.append("%s: chain points at granule %d, "
                                    "past the disk" % (name, g))
                    break
                if g in owner:
                    problems.append("%s: granule %d is cross-linked "
                                    "with %s" % (name, g, owner[g]))
                    break
                if g in visited or len(visited) > total_grans:
                    problems.append("%s: granule chain loops at %d"
                                    % (name, g))
                    break
                visited.add(g)
                owner[g] = name
                b = data[gat + g]
                if b == FREE:
                    problems.append("%s: chain runs into free granule %d"
                                    % (name, g))
                    break
                if b >= 0xC0:
                    n = b & 0x3F
                    if n > GRANULE_SECTORS:
                        problems.append("%s: terminator 0x%02X claims %d "
                                        "sectors in the last granule"
                                        % (name, b, n))
                        break
                    sectors = (len(visited) - 1) * GRANULE_SECTORS + n
                    break
                g = b

            if sectors is not None:
                lss = (entry[14] << 8) | entry[15]
                if sectors > 0 and not (1 <= lss <= SECTOR):
                    problems.append("%s: last-sector byte count %d is "
                                    "out of range" % (name, lss))
                if sectors == 0 and lss != 0:
                    problems.append("%s: zero sectors but last-sector "
                                    "byte count %d" % (name, lss))

        # 3. Allocated granules no live file owns.
        orphans = [g for g in range(total_grans)
                   if data[gat + g] != FREE and g not in owner]
        i = 0
        while i < len(orphans):
            j = i
            while j + 1 < len(orphans) and orphans[j + 1] == orphans[j] + 1:
                j += 1
            run = orphans[i:j + 1]
            span = ("%d-%d" % (run[0], run[-1]) if len(run) > 1
                    else str(run[0]))
            plural = "s" if len(run) > 1 else ""
            if all(data[gat + g] == 0x00 for g in run):
                # GAT=00 is this tool's own reservation convention
                # (rawpack, blckbrd-style stashes). Reserved on purpose
                # is not corruption, so note it and don't fail on it.
                notes.append("reserved granule%s %s (GAT=00, "
                             "hidden-region convention)" % (plural, span))
            else:
                problems.append("orphaned granule%s %s" % (plural, span))
            i = j + 1

        for msg in problems:
            sys.stdout.write("  %s\n" % msg)
        for msg in notes:
            sys.stdout.write("  note: %s\n" % msg)
        tail = ", %d deleted entries" % deleted if deleted else ""
        if problems:
            sys.stdout.write("check: %s: %d problems%s\n"
                             % (p, len(problems), tail))
            rc = 1
        else:
            sys.stdout.write("check: %s: clean%s\n" % (p, tail))
    if rc:
        sys.exit(rc)


# ---------------------------------------------------------------------------
# rawpack
#
# Writes a raw, filesystem-free region onto an EXISTING DECB disk image
# (the same .dsk, not a separate disk) and marks the granules it uses so
# decb.py's own `copy` -- run afterward, for BOOT.BAS/LYNCHBRG.FNT/etc --
# doesn't allocate over it. Order relative to copy doesn't matter: rawpack
# checks the GAT first and refuses to write over any allocated granule, so
# either order is safe and a collision fails loudly instead of corrupting.
#
# Placement: fixed at granule 34, i.e. track 18 sector 1 -- the first
# granule after the directory track (17), so nothing here has to reason
# about the track-17 skip mid-region; the region starts clean, right after
# it. Sector-flat inside the region (LSN = track*SPT + sector0, no granule
# bookkeeping needed to walk it) since a custom loader may read it with
# plain sequential DiskCommand calls, not through the DECB filesystem -- but the
# START point is granule-aligned specifically so the GAT marking below
# lines up with whole granules.
#
# Layout within the region:
#   first sector      the map sector (see below), 256 bytes
#   rest              each file in the order given on the command line,
#                     back to back, each padded up to a whole sector
#                     (pad byte 0x00)
#
# Map sector format (all multi-byte fields big-endian, matching the header
# conventions it sits next to -- DECB binary headers and the rest of the
# 6809 world, where big-endian is simply how addresses are stored):
#   offset 0-1   magic "RM" (0x52 0x4D)
#   offset 2     format version (1)
#   offset 3     entry count N
#   offset 4..   N entries, 8 bytes each:
#     +0  module id      1 byte, 0-based index in command-line order
#     +1  start LSN      2 bytes (absolute, whole-disk LSN, not region-relative)
#     +3  sector count   1 byte  (whole sectors incl. padding)
#     +4  byte length    2 bytes (exact, so the loader can ignore pad bytes
#                                 in the last sector)
#     +6  load address   2 bytes
#   remaining bytes to fill out the 256-byte sector: 0x00
#
# This format is decb.py/rawpack's own invention for this project, not a
# standard -- there's nothing else in the wild to be compatible with.
#
# GAT marking: every granule the region touches (including a partially-used
# tail granule -- reserved WHOLE, since decb.py allocates at granule
# granularity and would otherwise happily hand out the unused remainder of
# that same granule to some other file) gets its GAT byte set to 0x00.
# 0x00 is not DECB's free marker (that's 0xFF) -- it reads as "allocated,
# chained to granule 0" to any GAT-respecting tool, which is exactly the
# trick a real disk (blckbrd.dsk, examined 2026-07-01) already uses to hide
# asset data outside the directory: reserved, but owned by no directory
# entry. decb.py's own find_free_granules only ever picks strictly-0xFF
# granules, so it already respects this with no changes needed there.

RAWMAP_MAGIC = b"RM"
RAWMAP_VERSION = 1
RAWMAP_HEADER_SIZE = 4
RAWMAP_ENTRY_SIZE = 8
RAWPACK_START_GRANULE = 34   # track 18 sector 1 -- first granule after the
                             # directory track; the GAT guard in cmd_rawpack
                             # verifies the region is clear on every run


def cmd_rawpack(args):
    disk = None
    start_granule = RAWPACK_START_GRANULE
    bare = False
    entries_in = []  # (path, addr)

    it = iter(args)
    for a in it:
        if a == "--bare":
            bare = True
        elif a == "--at-granule":
            try:
                start_granule = int(next(it), 0)
            except (StopIteration, ValueError):
                die("rawpack: --at-granule needs a granule number")
        elif disk is None:
            disk = a
        else:
            if ":" not in a:
                die("rawpack: expected FILE:ADDR, got %r" % a)
            path, _, addr_s = a.rpartition(":")
            try:
                addr = int(addr_s, 0)
            except ValueError:
                die("rawpack: bad address %r in %r" % (addr_s, a))
            if not (0 <= addr <= 0xFFFF):
                die("rawpack: address 0x%X out of 16-bit range in %r" % (addr, a))
            entries_in.append((path, addr))

    if disk is None:
        die("rawpack: no disk image specified")
    if not entries_in:
        die("rawpack: no FILE:ADDR arguments given")
    if not bare and \
            len(entries_in) > (SECTOR - RAWMAP_HEADER_SIZE) // RAWMAP_ENTRY_SIZE:
        die("rawpack: too many entries for one map sector (%d max)"
            % ((SECTOR - RAWMAP_HEADER_SIZE) // RAWMAP_ENTRY_SIZE))

    with open(disk, "rb") as f:
        image = bytearray(f.read())
    if len(image) % (SPT * SECTOR) != 0:
        die("rawpack: %s is not a whole number of tracks (rawpack does "
            "not support JVC-headered images; strip the header first)"
            % disk)
    tracks = disk_tracks(image)
    total_grans = num_granules(tracks)

    start_lsn = granule_offset(start_granule) // SECTOR
    total_sectors = tracks * SPT

    # Read every payload up front so the region's full extent is known
    # before a single byte is written.
    payloads = []
    # First sector of the region is the map, unless --bare: then the first
    # payload byte IS the region start. The DECB ROM's DOS command demands
    # that: it reads track 34 sector 1 to $2600 and checks for "OS" at the
    # first two bytes, so a boot region must not lead with map metadata.
    next_lsn = start_lsn + (0 if bare else 1)
    for path, addr in entries_in:
        with open(path, "rb") as f:
            payload = f.read()
        nsec = (len(payload) + SECTOR - 1) // SECTOR
        if nsec and next_lsn + nsec > total_sectors:
            die("rawpack: %s overflows the disk (LSN %d + %d sectors > %d total)"
                % (path, next_lsn, nsec, total_sectors))
        payloads.append((path, addr, payload, next_lsn, nsec))
        next_lsn += nsec
    end_lsn = next_lsn  # one past the last sector the region will use

    # The region is sector-flat: LSNs just count up, with no track-17 skip.
    # Granule LSNs DO skip track 17, so a region that overlaps the
    # directory track would write over the directory while the granule
    # marks below drift away from where the data actually went. The
    # default start (granule 34) can't hit this; --at-granule can.
    dir_first_lsn = DIR_TRACK * SPT
    dir_last_lsn = dir_first_lsn + SPT  # one past
    if start_lsn < dir_last_lsn and end_lsn > dir_first_lsn:
        die("rawpack: region (LSN %d-%d) overlaps the directory track "
            "(LSN %d-%d); pick a later --at-granule"
            % (start_lsn, end_lsn - 1, dir_first_lsn, dir_last_lsn - 1))

    # Guard: every granule the region touches must be free (0xFF) in the
    # GAT. This is what makes rawpack safe to run before OR after copy;
    # without it, a copy that had already allocated into this region
    # would be silently overwritten.
    gat_off = gat_offset()
    region_grans = []
    g = start_granule
    while g < total_grans and granule_offset(g) // SECTOR < end_lsn:
        if image[gat_off + g] != FREE:
            die("rawpack: granule %d in the target region is already "
                "allocated (GAT=0x%02X); rerun on a freshly dskini'd disk "
                "or move the region with --at-granule"
                % (g, image[gat_off + g]))
        region_grans.append(g)
        g += 1

    map_sector = bytearray(SECTOR)
    map_sector[0:2] = RAWMAP_MAGIC
    map_sector[2] = RAWMAP_VERSION
    map_sector[3] = len(entries_in)

    report = []
    for module_id, (path, addr, payload, entry_lsn, nsec) in \
            enumerate(payloads):
        length = len(payload)
        if nsec:
            off = entry_lsn * SECTOR
            image[off:off + length] = payload

        eoff = RAWMAP_HEADER_SIZE + module_id * RAWMAP_ENTRY_SIZE
        if bare:
            report.append((os.path.basename(path), module_id, entry_lsn, nsec, length, addr))
            continue
        map_sector[eoff + 0] = module_id
        map_sector[eoff + 1] = (entry_lsn >> 8) & 0xFF
        map_sector[eoff + 2] = entry_lsn & 0xFF
        map_sector[eoff + 3] = nsec & 0xFF
        map_sector[eoff + 4] = (length >> 8) & 0xFF
        map_sector[eoff + 5] = length & 0xFF
        map_sector[eoff + 6] = (addr >> 8) & 0xFF
        map_sector[eoff + 7] = addr & 0xFF
        report.append((os.path.basename(path), module_id, entry_lsn, nsec, length, addr))

    if not bare:
        image[start_lsn * SECTOR:start_lsn * SECTOR + SECTOR] = map_sector

    # Mark every granule the region touches, start through end, as used.
    # region_grans was computed (and verified free) by the guard above.
    marked = region_grans
    for g in marked:
        image[gat_off + g] = 0x00

    with open(disk, "wb") as f:
        f.write(image)

    sys.stdout.write("rawpack: %s (region starts LSN %d = granule %d%s)\n"
                      % (disk, start_lsn, start_granule,
                         ", bare: no map sector" if bare else ""))
    for name, module_id, entry_lsn, nsec, length, addr in report:
        sys.stdout.write(
            "  [%d] %-16s LSN %4d  +%-3d sectors  %5d bytes  -> addr 0x%04X\n"
            % (module_id, name, entry_lsn, nsec, length, addr))
    sys.stdout.write("  GAT: marked granules %d-%d as used (0x00)\n"
                      % (marked[0], marked[-1]) if marked else "  GAT: nothing marked\n")


# ---------------------------------------------------------------------------

USAGE = """\
usage: decb <command> [<args>]

Create and manipulate RS-DOS (Disk BASIC) disk images. Image paths are
<disk>,<name>; an optional :<n> suffix selects drive <n> of an HDB-DOS
pack made with dskini -h, and :<n>+<sectors> adds a sector offset for
HDB-DOS images inside OS-9 partitions. <disk>, alone (empty name) means
the image root. JVC-headered images are handled transparently.

commands:
  dskini   create an empty RS-DOS disk image
  copy     copy files to or from a disk image
  copyall  extract every file on a disk image to a host directory
  dir      list a disk image's directory
  stat     show a file's chain, sectors, and exact size
  free     show a disk image's free granule count
  kill     delete files from a disk image
  list     print a file's contents
  attr     show or change file attributes
  rename   rename a file on a disk image
  dump     hex dump sectors by LSN or track.sector
  check    fsck for RS-DOS: GAT vs directory, chains, orphans
  wipe     zero all unused sectors and scrub deleted directory entries
  rawpack  write a raw sector region and reserve its granules

examples:
  This section exists because the alternatives are hostile. ToolShed's
  decb.exe looks like a bunch of academics took their own homemade LSD
  then took design inspiration from ffmpeg. MAME's imgtool cannot be built by
  mortals. You typed DIR and SAVE in 1987. This section is for you.

  First, the comma. It is the whole trick. Left of the comma is a file
  on your PC. Right of it is a file inside the disk image.
  game.dsk,HELLO.BAS means HELLO.BAS on the disk image game.dsk.
  Everything below follows from that, albeit unclearly.

  Make a blank disk and look at it:
    decb dskini game.dsk
    decb dir game.dsk

  Put a BASIC program on a disk so the CoCo can LOAD and RUN it:
    decb copy -t hello.bas game.dsk,HELLO.BAS
  -t tokenizes your text file, which is what SAVE"HELLO" did on the
  real machine, and marks it type 0 for you. Skip -t and the CoCo
  loads your text file as if it were tokens. You get garbage.

  Put a BASIC program on as ASCII instead, what SAVE"HELLO",A did:
    decb copy -0 -a -l hello.bas game.dsk,HELLO.BAS
  -0 says BASIC program, -a sets the ASCII flag, -l turns PC line
  endings into the carriage returns BASIC expects. Loads slower on
  the CoCo but stays readable. When in doubt, use -t instead. It is
  what 1987 you meant.

  Get a BASIC program off a disk into readable text:
    decb copy -t game.dsk,HELLO.BAS hello.bas
  Same flag, other direction. Tokens go in, text comes out.

  Data files, the OPEN "O",#1,"SCORES" kind:
    decb copy -1 -a -l scores.txt game.dsk,SCORES.DAT
    decb copy -l game.dsk,SCORES.DAT scores.txt
  -1 is the data file type, -a because BASIC wrote them as ASCII,
  -l for line endings, both directions.

  Machine language needs no flags. Type 2 binary is the default:
    decb copy game.bin game.dsk,GAME.BIN

  Read a BASIC program without extracting anything:
    decb list -t game.dsk,HELLO.BAS

  Rescue an entire disk into a folder, BASIC detokenized, text fixed:
    decb copyall -t -l crusty.dsk rescued

  That file you killed in 1989:
    decb dir -d crusty.dsk
    decb copy -d crusty.dsk,PROG.BAS prog.bas

  Find out whether a disk image is lying to you:
    decb check crusty.dsk

  "file exists" errors: add -r to overwrite. Yes, every time.

flag cheat sheet:
  -0  BASIC program   -1 BASIC data   -2 machine language   -3 text
  -a  the ",A" in SAVE"X",A: stored as readable ASCII
  -b  stored as binary (the default)
  -t  tokenize or detokenize BASIC, the plain SAVE"X",A format
  -l  fix line endings between your PC and the CoCo
  -r  overwrite

Run 'decb <command> --help' (or -?) for individual command usage.
"""

HELP = {
    "dskini": """\
usage: decb dskini [-3|-4|-8] [-h<num>] [-n<name>] [-s] <disk>...

Create empty RS-DOS disk images.

options:
  -3|-4|-8  35 (default), 40, or 80 tracks
  -h<num>   make an HDB-DOS pack of <num> 35-track drives
  -n<name>  set the pack's HDB-DOS disk name (9 chars max)
  -s        reserve granules 0-33 for a skitzo (dual OS-9/DECB) disk
""",
    "copy": """\
usage: decb copy [-0|-1|-2|-3] [-a|-b] [-c] [-d] [-l] [-r] [-t] <src>... <target>

Copy files between the host and disk images, either direction.
Sources and target may be host paths or image paths (<disk>,<name>).
Multiple sources require a directory target: a host directory or '<disk>,'.

options:
  -0|-1|-2|-3  file type: 0 BASIC, 1 BASIC data, 2 machine language, 3 text
  -a|-b        data type: ASCII or binary
  -c           concatenate a segmented ML binary: load every segment
               into a 64K image (overlaps: last write wins) and re-emit
               each contiguous run as one segment
  -d           the image source names a DELETED file; recover it.
               The length is reconstructed (exactly for tokenized BASIC
               and DECB ML binaries, approximately otherwise) and the
               method is reported. PROG.BAS and ?ROG.BAS both match.
  -l           translate line endings (host <-> Disk BASIC)
  -r           overwrite an existing destination file
  -t           tokenize Color BASIC going in, detokenize coming out
""",
    "copyall": """\
usage: decb copyall [-t] [-l] [-r] <disk>[,][:<n>] <hostdir>

Extract every live file on a disk image into <hostdir> (created if
missing). Bytes are copied verbatim unless told otherwise.

options:
  -t  detokenize tokenized BASIC files (type 0, binary, FF header)
  -l  translate line endings on ASCII-flagged files
  -r  overwrite existing host files
""",
    "stat": """\
usage: decb stat <disk>,<name>[:<n>]...

Everything the directory knows about a file: type, exact size, sector
count, the full granule chain, and where each granule sits (track and
LSN range). The companion to dir the way a map is to a street name.
""",
    "dump": """\
usage: decb dump <disk>[,][:<n>] <where>...

Hex dump sectors. <where> is an LSN (324), an LSN range (324-326), or
track.sector with 1-based sectors (17.2 is the GAT).
""",
    "check": """\
usage: decb check <disk>[,][:<n>]...

fsck for RS-DOS. Cross-checks the GAT against the directory: invalid
GAT bytes, cross-linked and looping chains, chains running into free
granules, bad terminators, out-of-range last-sector counts, duplicate
names, and orphaned allocated granules. GAT=00 orphan runs are noted
as the hidden-region convention, not counted as problems; rawpack
regions and blckbrd-style stashes are supposed to look like that.
Exit 0 if clean, 1 if not.
""",
    "dir": """\
usage: decb dir [-d] <disk>[,][:<n>]...

List a disk image's directory: name, type, ASCII flag, granule count.

options:
  -d  also list deleted entries and whether each is recoverable.
      KILL destroys the first character of the name (shown as ?) and
      the granule chain (count shown as ?). Recoverable means the
      first granule has not been reallocated.
""",
    "free": """\
usage: decb free <disk>[,][:<n>]...

Show a disk image's free granule count.
""",
    "kill": """\
usage: decb kill <disk>,<name>[:<n>]...

Delete files from a disk image and free their granules.
""",
    "list": """\
usage: decb list [-s] [-t] <file>...

Print a file's contents; <file> is a host path or <disk>,<name>[:<n>].

options:
  -s  emit the file as Motorola S-records (S1 data, S9 exec address);
      the file must be a DECB segmented ML binary
  -t  detokenize tokenized Color BASIC
""",
    "attr": """\
usage: decb attr [-0|-1|-2|-3] [-a|-b] <disk>,<name>[:<n>]...

Show file attributes, or change them if any option is given.

options:
  -0|-1|-2|-3  file type: 0 BASIC, 1 BASIC data, 2 machine language, 3 text
  -a|-b        data type: ASCII or binary
""",
    "rename": """\
usage: decb rename <disk>,<oldname>[:<n>] <newname>

Rename a file on a disk image.
""",
    "wipe": """\
usage: decb wipe <disk>[,][:<n>]...

Zero every unused sector: all sectors of free granules and the slack
sectors past each file's last used sector, plus the surviving bytes of
deleted directory entries. Never touches the directory track, live
files, or hidden regions reserved in the GAT (rawpack data survives).
Destroys anything dir -d called recoverable, and slack bytes inside a
file's last used sector stay put; sectors are the unit here.
""",
    "rawpack": """\
usage: decb rawpack [--at-granule <g>] [--bare] <disk> <file>:<addr>...

Write a raw, filesystem-free sector region onto an existing dskini'd
disk image (default start: granule 34, track 18 sector 1) and mark its
granules in the GAT so copy won't allocate over it. Refuses to write
over allocated granules, so it can run before or after copy. See the
comment above cmd_rawpack for the map sector format.

options:
  --at-granule <g>  start the region at granule <g> instead of 34
  --bare            no map sector; payloads start at the region's first
                    byte. For boot tracks: the DECB DOS command reads
                    track 34 sector 1 and wants "OS" as the first two
                    bytes, so metadata cannot lead the region.
""",
}

COMMANDS = {
    "dskini": cmd_dskini,
    "copy": cmd_copy,
    "copyall": cmd_copyall,
    "dir": cmd_dir,
    "stat": cmd_stat,
    "dump": cmd_dump,
    "check": cmd_check,
    "free": cmd_free,
    "kill": cmd_kill,
    "list": cmd_list,
    "attr": cmd_attr,
    "rename": cmd_rename,
    "wipe": cmd_wipe,
    "rawpack": cmd_rawpack,
}


def main(argv):
    if len(argv) < 2:
        sys.stderr.write(USAGE)
        sys.exit(1)
    cmd = argv[1]
    args = argv[2:]

    if cmd in ("-V", "--version", "version"):
        sys.stdout.write("decb.py %s\n" % VERSION)
        sys.exit(0)

    if cmd in ("-?", "-h", "--help", "help"):
        if cmd == "help" and args and args[0] in HELP:
            sys.stdout.write(HELP[args[0]])
        else:
            sys.stdout.write(USAGE)
        sys.exit(0)

    if cmd not in COMMANDS:
        die("unknown command %r (run 'decb --help')" % cmd)

    if any(a in ("-?", "--help") for a in args):
        sys.stdout.write(HELP[cmd])
        sys.exit(0)

    COMMANDS[cmd](args)


if __name__ == "__main__":
    try:
        main(sys.argv)
    except BrokenPipeError:
        # Downstream (head, less) closed the pipe. Die quietly like a
        # unix tool, not with a traceback. Re-point stdout at devnull so
        # the interpreter's shutdown flush doesn't raise a second time.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
