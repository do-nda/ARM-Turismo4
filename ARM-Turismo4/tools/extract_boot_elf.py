#!/usr/bin/env python3
"""extract_boot_elf.py - extract the boot executable from a PS2 disc image you own.

Reads SYSTEM.CNF from the ISO9660 filesystem, finds the BOOT2 path, and writes
that file out. Pure Python, no dependencies. Supports 2048-byte-sector .iso files
and raw 2352-byte-sector images (.bin).

    python3 extract_boot_elf.py game.iso -o boot.elf
"""
import argparse
import hashlib
import re
import struct
import sys


class Image:
    def __init__(self, path):
        self.f = open(path, "rb")
        for ss, off in ((2048, 0), (2352, 16), (2352, 24)):
            self.f.seek(16 * ss + off)
            if self.f.read(6) == b"\x01CD001":
                self.ss, self.off = ss, off
                return
        sys.exit("error: no ISO9660 volume found (is this a PS2 data-track image?)")

    def sector(self, lba):
        self.f.seek(lba * self.ss + self.off)
        return self.f.read(2048)

    def read(self, lba, size):
        out = bytearray()
        while len(out) < size:
            out += self.sector(lba + len(out) // 2048)
        return bytes(out[:size])


def list_dir(img, lba, size):
    data, pos, entries = img.read(lba, size), 0, []
    while pos < len(data):
        n = data[pos]
        if n == 0:                       # padding to the next sector
            pos = (pos // 2048 + 1) * 2048
            continue
        rec = data[pos:pos + n]
        extent = struct.unpack_from("<I", rec, 2)[0]
        length = struct.unpack_from("<I", rec, 10)[0]
        name = rec[33:33 + rec[32]]
        if name not in (b"\x00", b"\x01"):
            entries.append((name.decode("latin-1").split(";")[0].upper(),
                            extent, length, bool(rec[25] & 2)))
        pos += n
    return entries


def find(img, path):
    pvd = img.sector(16)
    lba, size = struct.unpack_from("<I", pvd, 158)[0], struct.unpack_from("<I", pvd, 166)[0]
    parts = [p for p in re.split(r"[\\/]", path) if p]
    for i, part in enumerate(parts):
        want = part.split(";")[0].upper()
        hit = [e for e in list_dir(img, lba, size) if e[0] == want]
        if not hit:
            return None
        _, lba, size, is_dir = hit[0]
        if i < len(parts) - 1 and not is_dir:
            return None
    return lba, size


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("image")
    ap.add_argument("-o", "--output", default="boot.elf")
    ap.add_argument("--file", help="extract this path from the disc instead of the boot "
                    "executable, e.g. CORE.GT4")
    a = ap.parse_args()

    img = Image(a.image)
    if a.file:
        loc = find(img, a.file)
        if not loc:
            sys.exit(f"error: {a.file} not found on disc")
        data = img.read(*loc)
        open(a.output, "wb").write(data)
        print(f"extracted       : {a.file} ({len(data)} bytes)")
        print(f"sha256          : {hashlib.sha256(data).hexdigest()}")
        print(f"first 16 bytes  : {data[:16].hex(' ')}")
        print(f"written to      : {a.output}")
        return
    cnf = find(img, "SYSTEM.CNF")
    if not cnf:
        sys.exit("error: SYSTEM.CNF not found - not a bootable PS2 disc image")
    text = img.read(*cnf).decode("latin-1")
    m = re.search(r"BOOT2\s*=\s*cdrom0:\\?([^;\r\n]+)", text, re.I)
    if not m:
        sys.exit("error: no BOOT2 entry in SYSTEM.CNF (PS1 disc?)")
    boot = m.group(1).strip()
    loc = find(img, boot)
    if not loc:
        sys.exit(f"error: {boot} named in SYSTEM.CNF but not found on disc")
    data = img.read(*loc)
    open(a.output, "wb").write(data)

    print(f"boot executable : {boot}")
    print(f"size            : {len(data)} bytes")
    print(f"sha256          : {hashlib.sha256(data).hexdigest()}")
    if data[:4] == b"\x7fELF":
        ent = struct.unpack_from("<I", data, 24)[0]
        mach = struct.unpack_from("<H", data, 18)[0]
        print(f"format          : ELF, class={data[4]} machine={mach}, entry=0x{ent:08x}")
        if data[4] != 1 or data[5] != 1 or mach != 8:
            print("warning: not a 32-bit little-endian MIPS ELF; r5900lift will reject it")
    else:
        print(f"format          : NOT a plain ELF (first bytes: {data[:16].hex()})")
        print("The executable may be packed or wrapped. Send me this output.")
    print(f"written to      : {a.output}")


if __name__ == "__main__":
    main()
