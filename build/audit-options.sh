#!/usr/bin/env bash
# Every option this fork ADDS must appear in -h.
#
#   bash build/audit-options.sh [path/to/xroar-dev]
#
# Twice now an option has been registered with no help line: -input-script, the
# fork's central feature, and the whole -profile-* profiler. Both worked
# perfectly and were invisible, which is the worst failure mode for a tool an
# agent is supposed to discover by reading -h -- the reasonable conclusion on
# not finding one is that it was compiled out, and you go debug the build.
#
# The check compares the options registered in the PATCHED xroar.c against the
# options registered in the PRISTINE one (so it only judges what we added), and
# then against what -h actually prints.
set -u

HERE="$(cd "$(dirname "$0")/.." && pwd)"
VER=1.12.1
# Windows builds land as xroar-dev.exe. Defaulting to the bare name is wrong
# there for a reason that hides itself well: a tree that has ALSO been built
# under WSL has a Linux ELF at bin/xroar-dev, msys2 runs it, it produces no
# output, and every option this fork adds is reported missing from -h. That
# reads exactly like the build dropped every feature this fork adds.
case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) BIN="${1:-$HERE/bin/xroar-dev.exe}" ;;
    *)                    BIN="${1:-$HERE/bin/xroar-dev}" ;;
esac
SRC="$HERE/xroar-$VER/src/xroar.c"

[ -f "$BIN" ] || { echo "no binary: $BIN (build first)" >&2; exit 2; }
[ -f "$SRC" ] || { echo "no patched source: $SRC (build first)" >&2; exit 2; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
tar xzf "$HERE/xroar/xroar-$VER.tar.gz" -C "$TMP" || exit 2

"$BIN" -h > "$TMP/help.txt" 2>&1

python3 - "$SRC" "$TMP/xroar-$VER/src/xroar.c" "$TMP/help.txt" <<'PYEOF'
import re, sys

# Only real option registrations. XC_ENUM_INT entries are VALUES of an option
# (e.g. -trap-screenshot-when next|now) and are not options themselves -- an
# earlier version of this check counted them and reported false positives.
#
# [A-Z0-9_], NOT [A-Z_]: the macro names carry DIGITS. XC_SET_INT1 and
# XC_SET_INT0 register the whole -trap-audit-*/-trap-audio-*/-trap-ratelimit-*
# family, and a character class without 0-9 silently skips every one of them --
# seven options the fork adds, unaudited, by the check whose entire job is to
# notice exactly that. It read "40/40 all clear" while auditing 40 of 47.
OPT = re.compile(r'XC_(?:SET|CALL|ALIAS)[A-Z0-9_]*\(\s*"([a-z0-9][a-z0-9-]*)"')

def opts(p):
    return set(OPT.findall(open(p, encoding='utf-8', errors='replace').read()))

patched, pristine, helppath = sys.argv[1:4]
ours = opts(patched) - opts(pristine)
shown = set(re.findall(r'^\s+-([a-z0-9][a-z0-9-]*)',
                       open(helppath, encoding='utf-8', errors='replace').read(), re.M))

# No exemptions. This fork has no aliases -- every option it adds is spelled
# exactly one way, and every one of them must be in -h.
missing = sorted(ours - shown)
print(f"options this fork adds: {len(ours)}")
print(f"documented in -h:       {len(ours) - len(missing)}")
if missing:
    print(f"\nMISSING FROM -h ({len(missing)}):")
    for m in missing:
        print("   -" + m)
    print("\nAdd a help line, and fold it into the patch that added the option.")
    sys.exit(1)
print("\nall clear")
PYEOF
