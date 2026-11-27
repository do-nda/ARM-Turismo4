#!/usr/bin/env python3
"""Self-test: assemble a small MIPS program (no game content), lift it to C,
compile it, run it, and check the output. Also exercises the missing-entry loop."""
import os
import struct
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = 0x00100000
Z, V0, V1, A0, A1, T0, T1, T2, T3, RA = 0, 2, 3, 4, 5, 8, 9, 10, 11, 31


class Asm:
    def __init__(self):
        self.w, self.labels, self.fix = [], {}, []

    def pc(self):
        return BASE + 4 * len(self.w)

    def label(self, n):
        self.labels[n] = self.pc()

    def raw(self, w):
        self.w.append(w & 0xFFFFFFFF)

    def r(self, fn, rs=0, rt=0, rd=0, sh=0):
        self.raw((rs << 21) | (rt << 16) | (rd << 11) | (sh << 6) | fn)

    def i(self, op, rs, rt, imm):
        self.raw((op << 26) | (rs << 21) | (rt << 16) | (imm & 0xFFFF))

    def br(self, op, rs, rt, lab):
        self.fix.append((len(self.w), lab, "b"))
        self.i(op, rs, rt, 0)

    def j(self, op, lab):
        self.fix.append((len(self.w), lab, "j"))
        self.raw(op << 26)

    def la(self, reg, lab):
        self.fix.append((len(self.w), lab, "hi"))
        self.i(0x0F, 0, reg, 0)
        self.fix.append((len(self.w), lab, "lo"))
        self.i(0x0D, reg, reg, 0)

    # tiny helpers
    def nop(self): self.r(0x00)
    def addiu(self, rt, rs, v): self.i(0x09, rs, rt, v)
    def addu(self, rd, rs, rt): self.r(0x21, rs, rt, rd)
    def lw(self, rt, off, base): self.i(0x23, base, rt, off)
    def show(self, reg):
        self.addu(A0, reg, Z); self.addiu(V1, Z, 0x7000); self.r(0x0C)

    def done(self):
        for idx, lab, kind in self.fix:
            t = self.labels[lab]
            w = self.w[idx]
            if kind == "b":
                w |= ((t - (BASE + 4 * idx + 4)) >> 2) & 0xFFFF
            elif kind == "j":
                w |= (t >> 2) & 0x3FFFFFF
            elif kind == "hi":
                w |= (t >> 16) & 0xFFFF
            else:
                w |= t & 0xFFFF
            self.w[idx] = w
        return self.w


def build():
    a = Asm()
    a.label("_start")
    # 1. loop with a delay slot that counts iterations (expect 55, 10)
    a.addiu(T0, Z, 0); a.addiu(T1, Z, 10); a.addiu(T2, Z, 0)
    a.label("loop")
    a.addu(T0, T0, T1); a.addiu(T1, T1, -1)
    a.br(0x05, T1, Z, "loop"); a.addiu(T2, T2, 1)
    a.show(T0); a.show(T2)
    # 2. jal + result computed in jr delay slot (expect 42)
    a.addiu(A0, Z, 21); a.j(0x03, "dbl"); a.nop(); a.show(V0)
    # 3. jalr through a register -> needs the missing-entry loop (expect 15)
    a.addiu(A0, Z, 5); a.la(T0, "triple"); a.r(0x09, T0, 0, RA); a.nop(); a.show(V0)
    # 4. branch-likely: not taken skips the delay slot, taken runs it (0 then 7)
    a.addiu(T0, Z, 1); a.addiu(T3, Z, 0)
    a.br(0x14, T0, Z, "l1"); a.addiu(T3, T3, 100)
    a.label("l1"); a.show(T3)
    a.br(0x15, T0, Z, "l2"); a.addiu(T3, T3, 7)
    a.label("l2"); a.show(T3)
    # 5. 64-bit shifts (expect 4660)
    a.i(0x0F, 0, T0, 0x1234); a.r(0x3C, 0, T0, T1, 0); a.r(0x3E, 0, T1, T2, 16); a.show(T2)
    # 6. R5900 3-operand mult (expect -42) and mfhi (expect -1)
    a.addiu(A0, Z, -7); a.addiu(A1, Z, 6); a.r(0x18, A0, A1, V0); a.show(V0)
    a.r(0x10, 0, 0, T0); a.show(T0)
    # 7. unaligned load with lwr/lwl (little-endian pair)
    a.la(A0, "data"); a.i(0x26, A0, T0, 1); a.i(0x22, A0, T0, 4); a.show(T0)
    # 8. 128-bit lq/sq copy, then read word 3 of the copy
    a.la(A0, "data"); a.la(A1, "data2"); a.i(0x1E, A0, T0, 0); a.i(0x1F, A1, T0, 0); a.lw(T1, 12, A1); a.show(T1)
    # 9. COP2 (VU0) word on a path that is analysed but never executed (must be reported)
    a.addiu(T0, Z, 1); a.br(0x05, T0, Z, "after"); a.nop(); a.raw(0x4A000000)
    a.label("after")
    a.addiu(V1, Z, 0x7001); a.r(0x0C)
    a.label("dbl"); a.r(0x08, RA); a.addu(V0, A0, A0)
    a.label("triple"); a.addu(T1, A0, A0); a.r(0x08, RA); a.addu(V0, T1, A0)
    while a.pc() % 16:
        a.nop()
    a.label("data")
    a.w += [0x33221100, 0x77665544, 0xBBAA9988, 0xFFEEDDCC]
    a.label("data2")
    a.w += [0, 0, 0, 0]
    return a


def write_elf(path, words, entry):
    body = struct.pack(f"<{len(words)}I", *words)
    ehdr = b"\x7fELF" + bytes([1, 1, 1, 0]) + bytes(8)
    ehdr += struct.pack("<HHIIIIIHHHHHH", 2, 8, 1, entry, 52, 0, 0, 52, 32, 1, 0, 0, 0)
    off = 52 + 32
    phdr = struct.pack("<8I", 1, off, BASE, BASE, len(body), len(body), 7, 16)
    open(path, "wb").write(ehdr + phdr + body)


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def s32(v):
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


def main():
    asm = build()
    words = asm.done()
    tmp = tempfile.mkdtemp(prefix="ps2rt_test_")
    elf = os.path.join(tmp, "test.elf")
    out = os.path.join(tmp, "out")
    write_elf(elf, words, asm.labels["_start"])
    lift = [sys.executable, os.path.join(ROOT, "r5900lift.py"), elf, "-o", out]

    def build_run():
        r = sh(lift + (["--entries", os.path.join(tmp, "missing_entries.txt")]
                       if os.path.exists(os.path.join(tmp, "missing_entries.txt")) else []))
        assert r.returncode == 0, r.stderr + r.stdout
        c = sh(["gcc", "-O2", "-Wall", "-Wno-unused-label", "-o", os.path.join(tmp, "game")]
               + [os.path.join(out, f) for f in ("generated.c", "func_table.c", "ps2rt.c")])
        assert c.returncode == 0, c.stderr
        return sh([os.path.join(tmp, "game"), elf], cwd=tmp)

    first = build_run()
    assert first.returncode == 3, f"expected unresolved-jump exit, got {first.returncode}\n{first.stderr}"
    print("run 1: unresolved indirect target logged ->",
          open(os.path.join(tmp, "missing_entries.txt")).read().split())
    second = build_run()
    assert second.returncode == 0, second.stderr
    got = [int(x) for x in second.stdout.split()]
    want = [55, 10, 42, 15, 0, 7, 4660, -42, -1,
            s32(0x44332211), s32(0xFFEEDDCC)]
    print("run 2 output:", got)
    assert got == want, f"\nwant {want}\ngot  {got}"
    rep = open(os.path.join(out, "lift_report.txt")).read()
    assert "COP2" in rep, rep
    print("PASS - all checks OK; lift report:\n" + rep)


if __name__ == "__main__":
    main()
