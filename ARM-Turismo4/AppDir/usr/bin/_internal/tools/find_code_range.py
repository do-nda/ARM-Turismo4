#!/usr/bin/env python3
"""find_code_range.py - estimate where machine code ends and data begins in an R5900 ELF.

Real code almost never contains reserved opcodes, trap instructions or unused REGIMM
codes; arbitrary data does so constantly. For each window of the image this measures the
fraction of such "bad" words, then picks the single boundary that best separates a clean
(code) prefix from a dirty (data) tail.

    python3 tools/find_code_range.py core.elf
    python3 r5900lift.py core.elf -o out_core --scan --split 400 --code-range 0x100000 0xHHHHHH
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import r5900lift as L  # noqa: E402

BAD_OPS = {0x13, 0x1D, 0x30, 0x32, 0x34, 0x35, 0x38, 0x3A, 0x3B, 0x3C, 0x3D}
BAD_SPECIAL = {0x01, 0x05, 0x0E, 0x15, 0x1C, 0x1D, 0x1E, 0x1F, 0x30, 0x31, 0x32, 0x33,
               0x34, 0x35, 0x36, 0x37, 0x39, 0x3D}
VALID_REGIMM = {0, 1, 2, 3, 0x10, 0x11, 0x12, 0x13, 0x18, 0x19}
JR_RA = 0x03E00008


def bad(w):
    op = w >> 26
    if op in BAD_OPS:
        return True
    if op == 0:
        return (w & 63) in BAD_SPECIAL
    if op == 1:
        return ((w >> 16) & 31) not in VALID_REGIMM
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("elf")
    ap.add_argument("--window", type=int, default=4096, help="window size in bytes")
    ap.add_argument("--threshold", type=float, default=0.02,
                    help="max bad-word fraction for a window to count as code")
    a = ap.parse_args()

    elf = L.Elf(open(a.elf, "rb").read())
    words = elf.data
    lo = elf.entry & ~3
    while lo - 4 in words:
        lo -= 4
    hi = elf.entry & ~3
    while hi + 4 in words:
        hi += 4
    hi += 4
    wsz = a.window
    n = (hi - lo + wsz - 1) // wsz
    frac, jr = [], []
    for i in range(n):
        ws = [words[x] for x in range(lo + i * wsz, min(hi, lo + (i + 1) * wsz), 4)]
        frac.append(sum(map(bad, ws)) / len(ws))
        jr.append(sum(1 for w in ws if w == JR_RA))
    code = [f < a.threshold for f in frac]

    # change point: windows [0,k) are code, [k,n) are data; maximise agreement
    best, best_k, run_code = -1, 0, 0
    data_after = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        data_after[i] = data_after[i + 1] + (0 if code[i] else 1)
    for k in range(n + 1):
        score = run_code + data_after[k]
        if score > best:
            best, best_k = score, k
        if k < n and code[k]:
            run_code += 1

    print(f"image region    : 0x{lo:08x}-0x{hi:08x} ({(hi - lo) // 1024} KiB, {n} windows of {wsz} bytes)")
    runs, start = [], 0
    for i in range(1, n + 1):
        if i == n or code[i] != code[start]:
            runs.append((start, i, code[start]))
            start = i
    print("runs (clean = looks like code, dirty = looks like data):")
    for s, e, c in runs[:40]:
        avg = sum(frac[s:e]) / (e - s)
        print(f"  0x{lo + s * wsz:08x}-0x{lo + e * wsz:08x}  {'clean' if c else 'dirty'}"
              f"  bad-word avg {avg * 100:5.1f}%  jr-ra count {sum(jr[s:e])}")
    if len(runs) > 40:
        print(f"  ... {len(runs) - 40} more runs (mixed regions)")
    boundary = lo + best_k * wsz
    slack = min(hi, boundary + wsz)  # one window of slack: over-estimating is harmless
    print(f"\nbest code/data boundary: about 0x{boundary:08x} "
          f"({sum(code[:best_k])}/{best_k} windows before it look like code, "
          f"{n - best_k - sum(code[best_k:])}/{n - best_k} after it look like data)")
    print(f"suggested: --code-range 0x{lo:x} 0x{slack:x}")
    print("Over-estimating is safe (the lifter only follows reachable code); under-estimating "
          "drops real code. If clean windows reappear after the boundary, inspect the runs above.")


if __name__ == "__main__":
    main()
