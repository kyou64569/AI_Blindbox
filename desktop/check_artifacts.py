# -*- coding: utf-8 -*-
"""Check whether each desktop artifact actually carries the current frontend.

PyInstaller copies ``frontend/demo.html`` into the bundle at build time.  An exe
built on an earlier day keeps that older UI forever until it is rebuilt -- which
looks exactly like "my UI changes did not take effect".  That is precisely what
happened with ``dist/AIBlindBox.exe`` built on 11-14 while demo.html was edited
on 11-17: the packaged copy was 120586 bytes instead of 143384.

Run this after every build (build.cmd does it automatically).

Exit code 0 = every artifact current, 1 = at least one stale / unreadable.
Output is ASCII-only on purpose: cmd.exe uses the GBK codepage and Chinese text
from Python would come out as mojibake in the build window.
"""
from __future__ import annotations

import datetime
import hashlib
import sys
from pathlib import Path

DESKTOP = Path(__file__).resolve().parent
DIST = DESKTOP / "dist"
SRC = DESKTOP.parent / "frontend" / "demo.html"
WANT = "frontend\\demo.html"


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _from_exe(exe: Path):
    """Single-file build: the frontend lives inside the CArchive."""
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except Exception as exc:  # pragma: no cover - depends on environment
        return None, "CArchiveReader unavailable (%s)" % exc
    try:
        arch = CArchiveReader(str(exe))
    except Exception as exc:
        return None, "cannot read archive (%s)" % exc
    name = None
    for entry in arch.toc:
        if entry.replace("/", "\\").lower() == WANT:
            name = entry
            break
    if name is None:
        return None, "frontend/demo.html not bundled"
    data = arch.extract(name)
    if isinstance(data, tuple):
        data = data[1]
    return data, None


def _from_onedir(exe: Path):
    """One-dir build: the frontend sits next to the exe under _internal/."""
    candidate = exe.parent / "_internal" / "frontend" / "demo.html"
    if not candidate.is_file():
        return None, "missing %s" % candidate
    return candidate.read_bytes(), None


def main() -> int:
    if not SRC.is_file():
        print("source frontend/demo.html missing: %s" % SRC)
        return 1
    src_bytes = SRC.read_bytes()
    src_hash = _md5(src_bytes)
    print("source frontend/demo.html: %d bytes  md5=%s" % (len(src_bytes), src_hash[:8]))
    print("")

    artifacts = []
    if DIST.is_dir():
        artifacts += [(p, _from_exe) for p in sorted(DIST.glob("*.exe"))]
        artifacts += [(p, _from_onedir) for p in sorted(DIST.glob("*/AIBlindBox.exe"))]
    if not artifacts:
        print("no artifacts found under %s" % DIST)
        return 1

    stale = 0
    width = max(len(str(p.relative_to(DESKTOP))) for p, _ in artifacts)
    for exe, reader in artifacts:
        rel = str(exe.relative_to(DESKTOP))
        try:
            mtime = datetime.datetime.fromtimestamp(exe.stat().st_mtime).strftime("%m-%d %H:%M")
        except Exception:
            mtime = "?"
        data, err = reader(exe)
        if err:
            print("  UNKNOWN  %-*s  %s  %s" % (width, rel, mtime, err))
            stale += 1
            continue
        digest = _md5(data)
        if digest == src_hash:
            print("  CURRENT  %-*s  %s  md5=%s" % (width, rel, mtime, digest[:8]))
        else:
            print(
                "  STALE    %-*s  %s  md5=%s  bundled=%d bytes, expected %d"
                % (width, rel, mtime, digest[:8], len(data), len(src_bytes))
            )
            stale += 1

    print("")
    if stale:
        print("WARNING: %d artifact(s) do NOT carry the current frontend." % stale)
        print("Those exes will show the OLD UI. Rebuild with:")
        print('    build.cmd            (one-dir)')
        print('    build.cmd onefile    (single file)')
        print('    build.cmd all        (both)')
        return 1
    print("All artifacts carry the current frontend.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
