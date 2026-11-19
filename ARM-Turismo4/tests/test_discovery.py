#!/usr/bin/env python3
"""Tests for function-discovery quality: dedupe of early-return seeds, --code-range, and
tools/find_code_range.py. All inputs are synthetic."""
import os
import random
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import selftest as T  # noqa: E402
from selftest import A0, T0, T1, T2, V0, V1, Z, RA  # noqa: E402


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def program():
    a = T.Asm()
    a.label("_start")
    a.addiu(A0, Z, 5); a.j(0x03, "F"); a.nop(); a.show(V0)        # F(5) = 10
    a.addiu(A0, Z, 0); a.j(0x03, "F"); a.nop(); a.show(V0)        # F(0) = 0
    a.la(T0, "G"); a.r(0x09, T0, 0, RA); a.nop(); a.show(V0)      # G() = 99 (indirect)
    a.addiu(V1, Z, 0x7001); a.r(0x0C)
    # F has an early return; the code after it is a branch target INSIDE F
    a.label("F"); a.br(0x04, A0, Z, "Lend"); a.nop()
    a.addu(V0, A0, A0); a.r(0x08, RA); a.nop()                     # early return
    a.label("Lend"); a.addiu(V0, Z, 0); a.r(0x08, RA); a.nop()
    a.label("G"); a.addiu(V0, Z, 99); a.r(0x08, RA); a.nop()
    a.label("junk")
    # data that looks like "jr ra; nop; <valid prologue>": a bogus weak seed
    a.w += [0x03E00008, 0, 0x27BDFFF0, 0x13000000, 0x1D000000, 0x30000000, 0x34000000]
    return a


def jumptable_program():
    a = T.Asm()
    a.label("_start")
    a.la(T0, "table"); a.addiu(T1, Z, 2); a.r(0x00, 0, T1, T1, 2)      # sll t1,t1,2
    a.addu(T0, T0, T1); a.lw(T2, 0, T0); a.r(0x08, T2); a.nop()          # jr t2 via table[2]
    for i, v in enumerate((10, 20, 30, 40)):
        a.label(f"C{i}"); a.addiu(A0, Z, v); a.j(0x02, "done"); a.nop()
    a.label("done"); a.show(A0); a.addiu(V1, Z, 0x7001); a.r(0x0C)
    while a.pc() % 16:
        a.nop()
    a.label("table"); a.w += [0, 0, 0, 0]
    return a


def build_and_run(tmp, elf, extra):
    out = os.path.join(tmp, "out")
    r = sh([sys.executable, os.path.join(ROOT, "r5900lift.py"), elf, "-o", out, *extra])
    assert r.returncode == 0, r.stderr + r.stdout
    nfunc = int(re.search(r"functions\s+: (\d+)", r.stdout).group(1))
    c = sh(["gcc", "-O1", "-w", "-o", os.path.join(tmp, "game")]
           + [os.path.join(out, f) for f in sorted(os.listdir(out)) if f.endswith(".c")]
           + ["-lm"])
    assert c.returncode == 0, c.stderr
    return nfunc, r.stdout, sh([os.path.join(tmp, "game"), elf], cwd=tmp)


def main():
    tmp = tempfile.mkdtemp(prefix="ps2_disc_")
    a = program()
    words = a.done()
    elf = os.path.join(tmp, "d.elf")
    T.write_elf(elf, words, a.labels["_start"])

    n, _, run = build_and_run(tmp, elf, ["--scan"])
    assert run.returncode == 0 and run.stdout.split() == ["10", "0", "99"], run.stderr + run.stdout
    assert n == 5, f"expected _start, F, G + 2 junk seeds = 5 (4 would mean a lost seed, 6 a duplicated F tail), got {n}"
    print("dedupe: early-return block inside F not emitted as a separate function")

    end = hex(a.labels["junk"])
    n2, out, run = build_and_run(tmp, elf, ["--scan", "--code-range", hex(T.BASE), end])
    assert n2 == 3 and "code range" in out, out
    assert run.returncode == 0 and run.stdout.split() == ["10", "0", "99"], run.stderr
    print("code-range: junk after the code region is ignored (3 functions)")

    # jump table: targets are case labels that no static flow reaches
    tmp3 = tempfile.mkdtemp(prefix="ps2_jt_")
    j = jumptable_program()
    jw = j.done()
    base = (j.labels["table"] - T.BASE) // 4
    for i in range(4):
        jw[base + i] = j.labels[f"C{i}"]
    elf3 = os.path.join(tmp3, "j.elf")
    T.write_elf(elf3, jw, j.labels["_start"])
    _, _, run = build_and_run(tmp3, elf3, [])
    assert run.returncode == 3, "without --scan the table target should be unresolved"
    os.remove(os.path.join(tmp3, "missing_entries.txt"))
    _, _, run = build_and_run(tmp3, elf3, ["--scan"])
    assert run.returncode == 0 and run.stdout.split() == ["30"], run.stderr + run.stdout
    print("jump-table: switch targets seeded from the table by --scan (and missed without it)")

    # find_code_range on clean code followed by random data
    rnd = random.Random(1)
    clean = [0x27BDFFF0, 0, 0x8FBF0000, 0x03E00008, 0] * 205 + [0] * 3     # 1028 words
    clean = (clean * 64)[:64 * 1024]
    junk = [rnd.getrandbits(32) for _ in range(64 * 1024)]
    img = os.path.join(tmp, "range.elf")
    T.write_elf(img, clean + junk, T.BASE)
    r = sh([sys.executable, os.path.join(ROOT, "tools", "find_code_range.py"), img])
    assert r.returncode == 0, r.stderr
    m = re.search(r"suggested: --code-range 0x([0-9a-f]+) 0x([0-9a-f]+)", r.stdout)
    assert m, r.stdout
    hi = int(m.group(2), 16)
    true_end = T.BASE + 4 * len(clean)
    assert true_end <= hi <= true_end + 2 * 4096, f"boundary 0x{hi:x} vs true 0x{true_end:x}"
    print(f"find_code_range: true boundary 0x{true_end:x}, suggested 0x{hi:x}")
    print("PASS")


if __name__ == "__main__":
    main()
