# Notes for whoever automates this next

Short on purpose. Every rule here was paid for once already.

## The three that will actually bite you

### 1. Never hand-edit a unified diff

A blank context line in a diff is a line containing **one space**, not an empty
line. Editors strip it, many tools normalise it, and the result applies as if
nothing were wrong until the fifth patch downstream fails with a message
pointing at the wrong file. Hunk headers (`@@ -122,4 +122,14 @@`) also carry
line counts that must match what you wrote.

Both of those were got wrong within twenty minutes of each other while
preparing this fork.

**Generate diffs, never author them:**

    # apply the series to a scratch tree
    tar xzf xroar/xroar-1.12.1.tar.gz && cd xroar-1.12.1
    for p in ../xroar/[0-9]*.patch; do patch -p1 -s < "$p"; done
    cp src/thing.c /tmp/thing.c.orig
    # ...edit src/thing.c with whatever you like...
    diff -u --label a/src/thing.c --label b/src/thing.c /tmp/thing.c.orig src/thing.c

Paste that output under a commit message. Then **verify from a pristine
tarball** that the whole series still applies before you believe any of it.

### 1a. Fix a patch by regenerating it, not by appending a fixup

When patch N turns out to be wrong, **fix patch N**. Do not append "fix what
patch N got wrong" as a new patch at the end. (Numbers are deliberately not
used in this example: renumbering the series would rewrite them and turn the
sentence into nonsense, which is exactly what happened the first time it was
written.) A series that accumulates fixups forces every reader to
reconstruct the real content of a file by replaying edits in order, which is
exactly the property the one-patch-one-idea structure exists to avoid, and it
compounds, because the fixup is itself something a later rebase has to carry.

The regeneration is mechanical:

    build the tree with patches 1..N-1        # snapshot the files N touches
    apply patch N
    apply the fix
    diff snapshot vs result                   # this is the new patch N

Keep the original commit message and extend it to explain the thing that was
wrong; that history is worth more inside the patch than in a separate one.

The exception is a fix to **upstream's** code, where there is no earlier patch
of ours to fold into. Those are genuinely new patches.

### 1b. Edit a patch in bytes, not as text

The series is plain UTF-8 today, but a diff that touches a Windows resource
script or any other non-UTF-8 file will carry raw high bytes, and a tool that
reads it as UTF-8 and writes it back replaces those with `U+FFFD`. The patch
then fails on one hunk in a file that has nothing to do with what you were
changing, and the error points at that file rather than at you.

So edit in bytes:

    b = open(p, "rb").read()
    open(p, "wb").write(b.replace(b"old", b"new"))

and check the whole series afterwards:

    python3 -c "b=open(P,'rb').read(); assert b'\xef\xbf\xbd' not in b"

`.gitattributes` marks `*.patch` as `-text` so git will not normalise them
either.

### 2. Build with `-Werror` at least once

This series spent months being built without it. When it was finally turned on
for a cross-compile, it immediately found four missing declarations, three of
them functions returning pointers, which an implicit declaration truncates to
32 bits on a 64-bit host. Those compile silently and crash later, at the moment
the feature is first used, which may be months after the patch landed.

The Windows build (`build/build-windows.sh`) passes `-Werror`. Run it when you
add a patch even if you do not want a Windows binary.

### 2a. An option with no `-h` line does not exist

    bash build/audit-options.sh

Run it after adding an option. It compares every option this fork registers
against what `-h` prints, and fails if one is missing.

It exists because this happened twice: `-input-script`, the central feature,
and the whole `-profile-*` profiler were both registered, both worked, and
neither appeared in the help. That is the worst way for a feature to be broken.
An agent discovers this binary by reading `-h`, so an option that is not
there has effectively been removed, and the reasonable response to not finding
it is to go debug the build.

### 3. Suspect the harness before the emulator

A scripted run has far more ways to be wired wrong than the emulator has to be
broken. Before concluding you have found an emulator bug:

- Is the coordinate you clicked still on the thing you meant? UI geometry moves.
- Is a stale snapshot being resumed against a newly built binary?
- Did a defensive retry click land somewhere meaningful *after* the dialog it
  was aimed at closed? (A real bug hunt lost a day to exactly this: a retry
  press aimed at a button hit a palette strip once the dialog was gone, and the
  program was blamed for changing a colour it never touched.)
- Is the feature actually compiled in? Check the acceptance lines.

## Working style that pays here

**Measure before optimising, and write the number down.** "It feels slow" and
"it copies 18,886 bytes per frame, 33ms, a 30fps ceiling" lead to different and
better decisions. The second one also tells you when to stop.

**Check whether the tree already solves your problem.** A portability fix here
was built twice the wrong way, a local helper and then a fallback in a shared
header, before the *link error from the second attempt* revealed that upstream
already had the mechanism, in `portalib`, with a documented idiom every caller
followed. The error message was more useful than either design. When something
looks like it needs a new mechanism, grep for the old one first.

**Prefer physical addresses.** Stated in the README, repeated here because it is
the single most common way to be intermittently wrong on a banked machine. Use
`xpc=` over `pc=`, flat physical peek/poke over logical reads, physical
watchpoints over logical ones.

**Say what you did not verify.** If you could not run the thing, say so plainly
rather than implying a test happened. This matters more than usual with an
emulator, because "it built" and "it works" are very far apart.

## Shell traps

- **`/bin/sh` may be dash.** No `$'...'` quoting, no `<(...)`, no brace
  expansion, no `shopt`. Wrap anything non-trivial in `bash -c '...'`. The
  input-script recipes use `$'\r'`, which dash silently mangles.
- **`command -v` lies under WSL.** `/mnt/c/...` is on `PATH`, so Windows `.exe`
  files answer probes for Linux tools. Reject any answer under `/mnt/`.
- **Background processes may not survive between tool calls.** If you launch
  the emulator and something that talks to it, launch both in one command.

## When you add a capability

Add a patch. Do not accumulate local changes. The series *is* the fork, and a
change that is not in a patch does not exist as far as the next rebase is
concerned.

Read the four or five patches nearest to what you are adding and imitate them:
the commit-message style in this series is unusually explicit about failure
modes on purpose, because those messages are frequently the only surviving
record of why something is the way it is.

Then: series applies from a pristine tarball, `-Werror` build passes,
acceptance lines all present.

## What this fork is not

It is not a better XRoar and it is not trying to become upstream. It is stock
XRoar plus a machine-readable surface. If you find a bug that reproduces on
**stock** XRoar, it belongs upstream, not here. Reproduce it there first, then
report it there.
