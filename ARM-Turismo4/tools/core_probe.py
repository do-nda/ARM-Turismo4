#!/usr/bin/env python3
"""core_probe.py - inspect and unpack a Gran Turismo CORE.* executable from your own disc.

Layer order follows the community docs (nenkai.github.io/gt-modding-hub, "CORE - PS2
Executables"): [optional encryption] -> compression -> main header + sections.
The docs are loose on details, so this tool TESTS each assumption against the real file
and reports what matched, instead of assuming. Send the printed report if it fails.

    python3 core_probe.py CORE.GT4                        # analyse only
    python3 core_probe.py CORE.GT4 --emit-elf core.elf    # build an ELF for r5900lift.py
    python3 core_probe.py CORE.GT4 --dump core.dec        # keep the decompressed bytes
"""
import argparse
import math
import struct
import sys
import zlib
from collections import Counter

PS2ZIP = b"\xff\xf7\xee\xc5"


def entropy(b):
    if not b:
        return 0.0
    n = len(b)
    return -sum(c / n * math.log2(c / n) for c in Counter(b).values())


def inflate(data, off):
    """Try raw deflate then zlib at `off`. Returns (method, bytes, complete) or None."""
    best = None
    for name, wbits in (("raw deflate", -15), ("zlib", 15)):
        try:
            d = zlib.decompressobj(wbits)
            out = d.decompress(data[off:])
        except zlib.error:
            continue
        if len(out) >= 64 and (d.eof or best is None):
            best = (name, out, d.eof)
            if d.eof:
                break
    return best


def candidates(data):
    c = []
    if data[:4] == PS2ZIP:
        c.append(("PS2ZIP at offset 0", 8, -struct.unpack_from("<i", data, 4)[0]))
    if data[6:10] == PS2ZIP:
        c.append(("6-byte layer header + PS2ZIP", 14, -struct.unpack_from("<i", data, 10)[0]))
    if len(data) > 6:
        c.append(("6-byte layer header (u16 flags, u32 size) + deflate", 6,
                  struct.unpack_from("<I", data, 2)[0]))
    c.append(("bare deflate at offset 0", 0, None))
    return c


def parse_main(buf):
    """Main header per docs: [u16 n][hash n bytes] x2, i32 section count, i32 entry,
    then sections of (i32 target, i32 size, data). Returns dict or None."""
    try:
        p = 0
        for _ in range(2):
            n = struct.unpack_from("<H", buf, p)[0]
            p += 2 + n
        count, entry = struct.unpack_from("<ii", buf, p)
        p += 8
        secs = []
        for _ in range(count):
            tgt, size = struct.unpack_from("<ii", buf, p)
            p += 8
            if size < 0 or p + size > len(buf):
                return None
            secs.append((tgt & 0xFFFFFFFF, size, p))
            p += size
    except struct.error:
        return None
    ok = 0 < count < 256 and p == len(buf) and all(s[1] > 0 for s in secs)
    return dict(entry=entry & 0xFFFFFFFF, sections=secs, consumed=p, ok=ok)


def build_elf(buf, main):
    secs = main["sections"]
    off = 52 + 32 * len(secs)
    hdr = b"\x7fELF" + bytes([1, 1, 1, 0]) + bytes(8)
    hdr += struct.pack("<HHIIIIIHHHHHH", 2, 8, 1, main["entry"], 52, 0, 0, 52, 32,
                       len(secs), 0, 0, 0)
    ph, body = b"", b""
    for tgt, size, src in secs:
        # only the section holding the entry point is executable; the rest is data
        flags = 7 if tgt <= main["entry"] < tgt + size else 6
        ph += struct.pack("<8I", 1, off + len(body), tgt, tgt, size, size, flags, 16)
        body += buf[src:src + size]
    return hdr + ph + body


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("core")
    ap.add_argument("--dump", help="write the decompressed layer to this file")
    ap.add_argument("--emit-elf", help="write a synthetic ELF built from the sections")
    a = ap.parse_args()

    data = open(a.core, "rb").read()
    print(f"file            : {a.core} ({len(data)} bytes)")
    print(f"first 32 bytes  : {data[:32].hex(' ')}")
    print(f"last 8 bytes    : {data[-8:].hex(' ')}")
    print(f"entropy (64 KB) : {entropy(data[:65536]):.2f} bits/byte "
          "(near 8 = compressed or encrypted)")

    buf = None
    for label, off, declared in candidates(data):
        r = inflate(data, off)
        if not r:
            print(f"[ ] {label}: inflate failed")
            continue
        method, out, complete = r
        match = "n/a" if declared is None else ("yes" if declared == len(out) else
                                                  f"NO (declared {declared})")
        print(f"[x] {label}: {method}, {len(out)} bytes, stream complete={complete}, "
              f"size matches header={match}")
        if complete:
            buf = out
            break
    if buf is None:
        print("\nNo compression layer matched. The file may be ENCRYPTED (the docs describe "
              "an encryption layer for the GT4 Online build) or use another layout.\n"
              "Please send the report above.")
        sys.exit(2)
    if a.dump:
        open(a.dump, "wb").write(buf)
        print(f"decompressed layer written to {a.dump}")

    print(f"\ndecompressed head: {buf[:48].hex(' ')}")
    main_hdr = parse_main(buf)
    if not main_hdr:
        print("Main header did not parse with the documented layout. Send the report above.")
        sys.exit(3)
    print(f"entry point     : 0x{main_hdr['entry']:08x}")
    print(f"sections        : {len(main_hdr['sections'])}")
    inside = False
    for tgt, size, src in main_hdr["sections"]:
        print(f"  target 0x{tgt:08x}  size {size:9d}  (file offset 0x{src:x})")
        inside |= tgt <= main_hdr["entry"] < tgt + size
    print(f"layout check    : {'consistent' if main_hdr['ok'] else 'INCONSISTENT'}; "
          f"entry inside a section: {inside}")
    if a.emit_elf:
        if not (main_hdr["ok"] and inside):
            print("not writing ELF: layout looks wrong")
            sys.exit(3)
        open(a.emit_elf, "wb").write(build_elf(buf, main_hdr))
        print(f"ELF written to  : {a.emit_elf}  (next: python3 r5900lift.py {a.emit_elf} -o out_core)")


if __name__ == "__main__":
    main()
