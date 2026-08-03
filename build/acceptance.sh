#!/usr/bin/env bash
# Confirm the five capabilities that make this fork worth having are actually
# linked in. Configure DROPS features when a dependency is absent and says so
# only in passing, so a green build is not evidence of a usable binary -- these
# lines are.
#
#   bash build/acceptance.sh path/to/xroar[.exe] [tolerated-missing ...]
#
# Name a feature as an extra argument to say its absence is expected and not a
# failure -- the Windows cross-build does that for trap-screenshot, which needs
# a mingw libpng nobody packages. Anything NOT named is still a hard failure.
# NO `pipefail` HERE, deliberately. With it set, `strings BIN | grep -q PAT`
# reports FAILURE on success: grep -q exits the moment it matches, strings takes
# SIGPIPE, and pipefail promotes that to the pipeline's status. Every check
# false-negatives and the build looks catastrophically broken when it is fine.
# Cost half an hour the first time. The scan below reads strings ONCE into a
# variable and greps that, which sidesteps the whole question.
set -u

BIN="${1:?usage: acceptance.sh path/to/xroar}"
[ -f "$BIN" ] || { echo "no such binary: $BIN" >&2; exit 2; }

shift
TOLERATED=" $* "

SYMS="$(strings "$BIN" 2>/dev/null || true)"

fail=0
check() {  # check <label> <string to find>
    printf '   %-17s ' "$1:"
    if printf '%s' "$SYMS" | grep -qi -- "$2"; then
        echo present
    elif [ "${TOLERATED#* $1 }" != "$TOLERATED" ]; then
        echo "absent (expected on this platform)"
    else
        echo MISSING
        fail=1
    fi
}

# MATCH SOMETHING ONLY THAT FEATURE PUTS IN THE BINARY. These used to grep for
# "script", "gdb" and "emuext" -- substrings so short that other features
# satisfied them. "script" is a substring of "input-script" one line below, so
# the joystick check could not fail unless the -input-script check failed too;
# three of the five lines were reporting on each other rather than on the thing
# they name. Each pattern below is a string literal the feature's own code
# emits or registers -- NOT a symbol name, so stripping the binary does not
# turn the whole run red.
check "emuext"          "LOG command hit with -emuext"
check "script joystick" "Scripted synthetic pointer"
check "-input-script"   "input-script"
check "gdb target"      "gdb-target"
check "trap-screenshot" "trap-screenshot"

if [ $fail -ne 0 ]; then
    cat >&2 <<'EOF'

One or more features are absent. This binary is not usable for automation as
it stands. Almost always a missing build dependency that configure dropped
quietly -- trap-screenshot means no libpng. Install it and rebuild; do not
work around it, because the trap hooks compile out with the CLI and the
failure will resurface as a trap that silently never fires.
EOF
    exit 1
fi
