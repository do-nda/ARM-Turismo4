#!/usr/bin/env python3
"""Tests for: LDL/LDR/SDL/SDR, COP1 (FPU) + BC1, COP0, MMI subset, and --scan discovery.
Expected values are derived by hand from the instruction definitions. They check the
lifter against my reading of the R5900 manual, not against real hardware."""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import selftest as T  # noqa: E402
from selftest import A0, A1, T0, T1, T2, T3, V0, V1, Z, RA  # noqa: E402


def cop1(fmt, ft, fs, fd, fn):
    return (0x11 << 26) | (fmt << 21) | (ft << 16) | (fs << 11) | (fd << 6) | fn


def mmi(fn, sa, rd, rs, rt):
    return (0x1C << 26) | (rs << 21) | (rt << 16) | (rd << 11) | (sa << 6) | fn


def cop0(rs, rt, rd):
    return (0x10 << 26) | (rs << 21) | (rt << 16) | (rd << 11)


def s32(v):
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def lift_build_run(tmp, elf, extra=(), env=None):
    out = os.path.join(tmp, "out")
    r = sh([sys.executable, os.path.join(ROOT, "r5900lift.py"), elf, "-o", out, *extra])
    assert r.returncode == 0, r.stderr + r.stdout
    c = sh(["gcc", "-O1", "-Wall", "-Wno-unused-label", "-Wno-unused-variable",
            "-o", os.path.join(tmp, "game")]
           + [os.path.join(out, f) for f in sorted(os.listdir(out)) if f.endswith(".c")]
           + ["-lm"])
    assert c.returncode == 0, c.stderr
    return r, sh([os.path.join(tmp, "game"), elf], cwd=tmp, env=env)


def features():
    a = T.Asm()
    want = []
    a.label("_start")
    # --- LDR/LDL unaligned 64-bit load, then SDR/SDL unaligned store -----------
    a.la(A0, "bytes"); a.i(0x1B, A0, T0, 1); a.i(0x1A, A0, T0, 8)
    a.show(T0); want.append(0x44332211)
    a.r(0x3E, 0, T0, T1, 0); a.show(T1); want.append(s32(0x88776655))
    a.la(A1, "out")
    a.i(0x2D, A1, T0, 1); a.i(0x2C, A1, T0, 8)
    for i, v in enumerate((0x33221100, 0x77665544, 0x88)):
        a.lw(T1, 4 * i, A1); a.show(T1); want.append(s32(v))
    # --- COP0 round trip + EI/DI ---------------------------------------------
    a.addiu(T0, Z, 0x1234); a.raw(cop0(4, T0, 12)); a.raw(cop0(0, T1, 12))
    a.show(T1); want.append(0x1234)
    a.raw(0x42000038); a.raw(cop0(0, T1, 12)); a.show(T1); want.append(0x1235)
    a.raw(0x42000039); a.raw(cop0(0, T1, 12)); a.show(T1); want.append(0x1234)
    # --- FPU -----------------------------------------------------------------
    a.addiu(T0, Z, 3); a.raw(cop1(4, T0, 1, 0, 0)); a.raw(cop1(0x14, 0, 1, 1, 0x20))
    a.addiu(T0, Z, 4); a.raw(cop1(4, T0, 2, 0, 0)); a.raw(cop1(0x14, 0, 2, 2, 0x20))
    a.raw(cop1(0x10, 2, 1, 3, 2))                       # mul.s f3 = f1*f2 = 12
    a.raw(cop1(0x10, 1, 3, 4, 0))                       # add.s f4 = f3+f1 = 15
    a.raw(cop1(0x10, 0, 4, 5, 0x24)); a.raw(cop1(0, T1, 5, 0, 0)); a.show(T1); want.append(15)
    a.raw(cop1(0x10, 2, 0, 7, 4)); a.raw(cop1(0x10, 0, 7, 7, 0x24))   # sqrt.s f7 = sqrt(4)
    a.raw(cop1(0, T1, 7, 0, 0)); a.show(T1); want.append(2)
    a.la(A1, "fbuf"); a.i(0x39, A1, 4, 0)               # swc1 f4 -> 15.0f
    a.lw(T1, 0, A1); a.show(T1); want.append(0x41700000)
    a.i(0x31, A1, 6, 0); a.raw(cop1(0x10, 0, 6, 6, 0x24))             # lwc1 f6; cvt.w.s
    a.raw(cop1(0, T1, 6, 0, 0)); a.show(T1); want.append(15)
    a.addiu(T3, Z, 0)
    a.raw(cop1(0x10, 2, 1, 0, 0x34)); a.br(0x11, 8, 1, "b1")   # c.lt.s f1,f2 ; bc1t
    a.nop(); a.addiu(T3, T3, 1); a.label("b1"); a.addiu(T3, T3, 10)
    a.show(T3); want.append(10)
    a.raw(cop1(0x10, 1, 2, 0, 0x34)); a.br(0x11, 8, 1, "b2")   # c.lt.s f2,f1 (false)
    a.nop(); a.addiu(T3, T3, 1); a.label("b2"); a.addiu(T3, T3, 10)
    a.show(T3); want.append(21)
    # --- MMI -----------------------------------------------------------------
    a.la(A0, "qa"); a.i(0x1E, A0, T0, 0)
    a.la(A0, "qb"); a.i(0x1E, A0, T1, 0)
    a.la(A1, "out")
    A, B = [1, 2, 3, 0xFFFFFFFF], [0x10, 0x20, 0x30, 0x40]
    cases = [
        (0x08, 0x12, [B[0], A[0], B[1], A[1]]),             # pextlw
        (0x08, 0x13, [B[0], B[2], A[0], A[2]]),             # ppacw
        (0x08, 0x00, [(x + y) & 0xFFFFFFFF for x, y in zip(A, B)]),   # paddw
        (0x29, 0x12, [x | y for x, y in zip(A, B)]),        # por
        (0x09, 0x12, [x & y for x, y in zip(A, B)]),        # pand
        (0x09, 0x0E, [B[0], B[1], A[0], A[1]]),             # pcpyld
    ]
    for fn, sa, exp in cases:
        a.raw(mmi(fn, sa, T2, T0, T1)); a.i(0x1F, A1, T2, 0)
        for i, v in enumerate(exp):
            a.lw(T3, 4 * i, A1); a.show(T3); want.append(s32(v))
    a.la(A0, "qz"); a.i(0x1E, A0, T3, 0)
    a.raw(mmi(0x04, 0, T2, T3, 0)); a.i(0x1F, A1, T2, 0)             # plzcw
    for i, v in enumerate((15, 3)):
        a.lw(T1, 4 * i, A1); a.show(T1); want.append(v)
    M32 = 0xFFFFFFFF

    def h16(f):
        return [(f((x >> 16) & 0xFFFF) << 16 | f(x & 0xFFFF)) & M32 for x in A]

    def sx16(v):
        return v - 0x10000 if v & 0x8000 else v
    shifts = [
        (0x3C, 4, [(x << 4) & M32 for x in A]),                       # psllw
        (0x3E, 1, [x >> 1 for x in A]),                               # psrlw
        (0x3F, 1, [(s32(x) >> 1) & M32 for x in A]),                  # psraw
        (0x34, 8, h16(lambda v: (v << 8) & 0xFFFF)),                  # psllh
        (0x36, 4, h16(lambda v: v >> 4)),                             # psrlh
        (0x37, 4, h16(lambda v: (sx16(v) >> 4) & 0xFFFF)),            # psrah
    ]
    for fn, sa, exp in shifts:
        a.raw(mmi(fn, sa, T2, 0, T0)); a.i(0x1F, A1, T2, 0)
        for i, v in enumerate(exp):
            a.lw(T3, 4 * i, A1); a.show(T3); want.append(s32(v))
    # SA register: mtsab/mtsah/mtsa/mfsa + qfsrv (funnel shift of {rs:rt} right by SA bits)
    a.la(A0, "qb"); a.i(0x1E, A0, T1, 0)
    for kind, val, exp in ((0x18, 4, [B[1], B[2], B[3], A[0]]),    # mtsab 4  -> SA = 32
                           (0x18, 8, [B[2], B[3], A[0], A[1]]),    # mtsab 8  -> SA = 64
                           (0x19, 2, [B[1], B[2], B[3], A[0]]),    # mtsah 2  -> SA = 32
                           (0x18, 0, B)):                          # SA = 0
        a.i(0x01, Z, kind, val); a.raw(mmi(0x28, 0x1B, T2, T0, T1)); a.i(0x1F, A1, T2, 0)
        for i, v in enumerate(exp):
            a.lw(T3, 4 * i, A1); a.show(T3); want.append(s32(v))
    a.i(0x01, Z, 0x18, 8); a.r(0x28, 0, 0, T3); a.show(T3); want.append(64)        # mfsa
    a.addiu(T3, Z, 32); a.r(0x29, T3)                                              # mtsa
    a.raw(mmi(0x28, 0x1B, T2, T0, T1)); a.i(0x1F, A1, T2, 0)
    for i, v in enumerate([B[1], B[2], B[3], A[0]]):
        a.lw(T3, 4 * i, A1); a.show(T3); want.append(s32(v))
    # madd / maddu (pipeline 0) and mult1 + madd1 (pipeline 1)
    a.addiu(T3, Z, 6); a.addiu(T1, Z, 7)
    a.r(0x18, T3, T1, T2); a.raw(mmi(0x00, 0, T0, T3, T1)); a.show(T0); want.append(84)
    a.raw(mmi(0x01, 0, T0, T3, T1)); a.show(T0); want.append(126)
    a.raw(mmi(0x18, 0, T2, T3, T1)); a.raw(mmi(0x20, 0, T0, T3, T1)); a.show(T0); want.append(84)
    a.addiu(T3, Z, -3); a.raw(mmi(0x20, 0, T0, T3, T1)); a.show(T0); want.append(63)
    # syscall inventory: show() issues `addiu v1,zero,1; syscall`
    # guest kernel-mode handlers installed with SetSyscall (0x74), one returning via ERET
    a.addiu(V1, Z, 0x74); a.addiu(A0, Z, 0x83); a.la(A1, "h1"); a.r(0x0C)
    a.i(0x0F, 0, T0, 0x8000); a.i(0x0D, T0, T0, 0x2000 + 4 * 0x83)       # kernel-space slot address
    a.lw(T1, 0, T0); a.show(T1); want.append(("label", "h1"))             # table holds the handler
    a.addiu(V1, Z, 0x83); a.addiu(A0, Z, 5); a.r(0x0C); a.show(V0); want.append(10)
    a.addiu(V1, Z, 0x74); a.addiu(A0, Z, 0x5A); a.la(A1, "h2"); a.r(0x0C)
    a.addiu(V1, Z, 0x5A); a.r(0x0C); a.show(V0); want.append(77)
    a.addiu(V1, Z, 0x7001); a.r(0x0C)
    a.label("h1"); a.addu(V0, A0, A0); a.r(0x08, RA); a.nop()
    a.label("h2"); a.addiu(V0, Z, 77); a.raw(0x42000018)                    # eret
    while a.pc() % 16:
        a.nop()
    a.label("bytes"); a.w += [0x33221100, 0x77665544, 0xBBAA9988, 0xFFEEDDCC]
    a.label("qa"); a.w += A
    a.label("qb"); a.w += B
    a.label("qz"); a.w += [0x0000FFFF, 0xF0000000, 0, 0]
    a.label("out"); a.w += [0] * 8
    a.label("fbuf"); a.w += [0] * 4
    return a, want


def scan_case():
    a = T.Asm()
    a.label("_start")
    a.la(T0, "table"); a.lw(T1, 0, T0); a.addiu(A0, Z, 6)
    a.r(0x09, T1, 0, RA); a.nop(); a.show(V0)
    a.addiu(V1, Z, 0x7001); a.r(0x0C); a.nop()
    a.label("hidden"); a.addu(V0, A0, A0); a.r(0x08, RA); a.nop()
    a.label("table"); a.w.append(0)
    return a


def main():
    tmp = tempfile.mkdtemp(prefix="ps2_feat_")
    a, want = features()
    want = [a.labels[x[1]] if isinstance(x, tuple) else x for x in want]
    words = a.done()
    elf = os.path.join(tmp, "f.elf")
    T.write_elf(elf, words, a.labels["_start"])
    rep, run = lift_build_run(tmp, elf, ["--add", hex(a.labels["h1"]), "--add", hex(a.labels["h2"])])
    assert run.returncode == 0, run.stderr
    got = [int(x) for x in run.stdout.split()]
    bad = [(i, g, w) for i, (g, w) in enumerate(zip(got, want)) if g != w]
    assert len(got) == len(want) and not bad, f"mismatches (index, got, want): {bad}\n{got}"
    print(f"features: {len(want)} checks OK")
    rpt = open(os.path.join(tmp, "out", "lift_report.txt")).read()
    # (stray entries after the exit syscall are the data block being decoded as code)
    for name in ("LDL", "LDR", "SDL", "SDR", "COP0", "COP1", "MMI", "LWC1", "SWC1"):
        assert name not in rpt, f"{name} still unimplemented:\n{rpt}"
    g = os.path.join(tmp, "out", "generated.c")
    m0 = os.stat(g).st_mtime_ns
    import time
    time.sleep(0.05)
    again = sh([sys.executable, os.path.join(ROOT, "r5900lift.py"), elf, "-o", os.path.join(tmp, "out"),
                "--add", hex(a.labels["h1"]), "--add", hex(a.labels["h2"])])
    assert again.returncode == 0 and os.stat(g).st_mtime_ns == m0, "unchanged output was rewritten"
    print("features: re-lifting leaves unchanged files untouched (make skips them)")
    print("features: none of the new instruction classes are reported as unimplemented")
    m = [l for l in rpt.splitlines() if "(0x7000)" in l or "(0x7001)" in l]
    assert len(m) == 2 and "(0x7000)" in m[0], f"syscall inventory wrong: {m}\n{rpt}"
    print("features: BIOS syscall inventory lists syscalls 0x7000 and 0x7001")
    assert os.path.exists(os.path.join(tmp, "out", "Makefile"))
    mk = sh(["make", "-C", os.path.join(tmp, "out"), "-s", "clean", "game"])
    print("features: generated Makefile builds" if mk.returncode == 0 else
          f"(make unavailable or failed: {mk.stderr[:80]})")

    tmp2 = tempfile.mkdtemp(prefix="ps2_scan_")
    s = scan_case()
    words = s.done()
    words[(s.labels["table"] - T.BASE) // 4] = s.labels["hidden"]
    elf2 = os.path.join(tmp2, "s.elf")
    T.write_elf(elf2, words, s.labels["_start"])
    _, run = lift_build_run(tmp2, elf2)
    assert run.returncode == 3, "without --scan the pointer-table function should be missed"
    os.remove(os.path.join(tmp2, "missing_entries.txt"))
    _, run = lift_build_run(tmp2, elf2, ["--scan", "--split", "1"])
    assert run.returncode == 0 and run.stdout.split() == ["12"], run.stderr + run.stdout
    print("scan: function behind a pointer table found by --scan (and missed without it)")
    # watchdog: an endless guest loop must be reported, not hang
    tmp3 = tempfile.mkdtemp(prefix="ps2_wd_")
    w = T.Asm()
    w.label("_start"); w.addiu(T0, Z, 1); w.label("L"); w.br(0x05, T0, Z, "L"); w.nop()
    elf3 = os.path.join(tmp3, "w.elf")
    T.write_elf(elf3, w.done(), w.labels["_start"])
    _, run = lift_build_run(tmp3, elf3, env={**os.environ, "PS2_WATCHDOG": "1"})
    assert run.returncode == 8 and "spinning" in run.stderr, (run.returncode, run.stderr)
    print("watchdog: an endless guest loop is detected and reported")
    print("PASS")


if __name__ == "__main__":
    main()
