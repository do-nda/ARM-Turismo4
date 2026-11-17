#!/usr/bin/env python3
"""Builds a tiny synthetic ISO9660 image (cooked and raw-sector variants) containing
SYSTEM.CNF plus a generated test ELF, then checks extract_boot_elf.py recovers it."""
import os
import struct
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import selftest  # noqa: E402


def rec(name, lba, size, flags=0):
    n = 33 + len(name)
    n += n % 2
    r = bytearray(n)
    r[0] = n
    struct.pack_into("<I", r, 2, lba); struct.pack_into(">I", r, 6, lba)
    struct.pack_into("<I", r, 10, size); struct.pack_into(">I", r, 14, size)
    r[25], r[32] = flags, len(name)
    r[33:33 + len(name)] = name
    return bytes(r)


def make_iso(elf_bytes, boot_name="SLUS_000.00"):
    cnf = f"BOOT2 = cdrom0:\\{boot_name};1\r\nVER = 1.00\r\nVMODE = NTSC\r\n".encode()
    root = (rec(b"\x00", 20, 2048, 2) + rec(b"\x01", 20, 2048, 2)
            + rec(b"SYSTEM.CNF;1", 21, len(cnf)) + rec(boot_name.encode() + b";1", 22, len(elf_bytes)))
    nsec = 22 + (len(elf_bytes) + 2047) // 2048
    img = bytearray(2048 * nsec)
    pvd = bytearray(2048)
    pvd[0:7] = b"\x01CD001\x01"
    pvd[156:156 + 34] = rec(b"\x00", 20, 2048, 2)[:34]
    img[16 * 2048:17 * 2048] = pvd
    img[20 * 2048:20 * 2048 + len(root)] = root
    img[21 * 2048:21 * 2048 + len(cnf)] = cnf
    img[22 * 2048:22 * 2048 + len(elf_bytes)] = elf_bytes
    return bytes(img)


def to_raw(iso):  # 2352-byte sectors, 16-byte header (mode 1 style)
    out = bytearray()
    for i in range(0, len(iso), 2048):
        out += bytes(16) + iso[i:i + 2048] + bytes(288)
    return bytes(out)


def main():
    asm = selftest.build()
    tmp = tempfile.mkdtemp(prefix="ps2_iso_test_")
    elf_path = os.path.join(tmp, "orig.elf")
    selftest.write_elf(elf_path, asm.done(), asm.labels["_start"])
    elf = open(elf_path, "rb").read()
    iso = make_iso(elf)
    for label, data in (("cooked", iso), ("raw2352", to_raw(iso))):
        src, dst = os.path.join(tmp, f"{label}.img"), os.path.join(tmp, f"{label}.elf")
        open(src, "wb").write(data)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "extract_boot_elf.py"),
                            src, "-o", dst], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert open(dst, "rb").read() == elf, f"{label}: extracted bytes differ"
        print(f"{label}: OK\n{r.stdout}")
    print("PASS")


if __name__ == "__main__":
    main()
