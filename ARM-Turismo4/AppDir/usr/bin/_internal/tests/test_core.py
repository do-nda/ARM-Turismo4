#!/usr/bin/env python3
"""Builds synthetic CORE-style files per the documented layout (no game data), runs
core_probe.py on them, then lifts and executes the emitted ELF. NOTE: this validates the
tool against my reading of the docs, not against a real CORE.GT4."""
import os
import struct
import subprocess
import sys
import tempfile
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import selftest  # noqa: E402


def raw_deflate(b):
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return c.compress(b) + c.flush()


def main_block(code_words, entry):
    code = struct.pack(f"<{len(code_words)}I", *code_words)
    # a second, non-code section so multi-section handling is exercised
    extra = bytes(range(256)) * 4
    m = struct.pack("<H", 64) + bytes(64) + struct.pack("<H", 64) + bytes(64)
    m += struct.pack("<ii", 2, entry)
    m += struct.pack("<ii", selftest.BASE, len(code)) + code
    m += struct.pack("<ii", 0x00400000, len(extra)) + extra
    return m


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def main():
    asm = selftest.build()
    words = asm.done()
    entry = asm.labels["_start"]
    block = main_block(words, entry)
    tmp = tempfile.mkdtemp(prefix="ps2_core_test_")

    plain = struct.pack("<HI", 1, len(block)) + raw_deflate(block)
    ps2zip = (struct.pack("<HI", 1, len(block)) + b"\xff\xf7\xee\xc5"
              + struct.pack("<i", -len(block)) + raw_deflate(block))
    for label, blob in (("layer+deflate", plain), ("layer+PS2ZIP", ps2zip)):
        src = os.path.join(tmp, f"{label}.core")
        elf = os.path.join(tmp, f"{label}.elf")
        open(src, "wb").write(blob)
        r = run([sys.executable, os.path.join(ROOT, "tools", "core_probe.py"), src,
                 "--emit-elf", elf])
        assert r.returncode == 0, r.stdout + r.stderr
        assert "layout check    : consistent" in r.stdout, r.stdout
        print(f"{label}: probe OK")
        out = os.path.join(tmp, f"out_{label}")
        lift = [sys.executable, os.path.join(ROOT, "r5900lift.py"), elf, "-o", out]
        for attempt in range(2):  # second pass applies missing_entries.txt
            extra = []
            me = os.path.join(tmp, "missing_entries.txt")
            if os.path.exists(me):
                extra = ["--entries", me]
            assert run(lift + extra).returncode == 0
            cc = run(["gcc", "-O1", "-w", "-o", os.path.join(tmp, "game")] +
                     [os.path.join(out, f) for f in ("generated.c", "func_table.c", "ps2rt.c")])
            assert cc.returncode == 0, cc.stderr
            g = run([os.path.join(tmp, "game"), elf], cwd=tmp)
            if g.returncode == 0:
                break
        assert g.returncode == 0, g.stderr
        assert g.stdout.split()[:2] == ["55", "10"], g.stdout
        print(f"{label}: lift + run OK")
        if os.path.exists(me):
            os.remove(me)

    bad = os.path.join(tmp, "garbage.core")
    open(bad, "wb").write(os.urandom(4096))
    r = run([sys.executable, os.path.join(ROOT, "tools", "core_probe.py"), bad])
    assert r.returncode == 2 and "ENCRYPTED" in r.stdout, r.stdout
    print("random/encrypted-looking input: rejected with a clear message")
    print("PASS")


if __name__ == "__main__":
    main()
