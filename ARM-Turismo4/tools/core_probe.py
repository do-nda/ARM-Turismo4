#!/usr/bin/env python3
"""core_probe.py - inspect and unpack a GT4 CORE.GT4 container into an R5900 ELF."""
import argparse
import struct
import sys
import zlib

PS2ZIP_MAGIC = b"\xff\xf7\xee\xc5"


def unpack_core(blob):
    if len(blob) < 16:
        return None, "file too small"
    ver, uncomp_size = struct.unpack_from("<HI", blob, 0)
    if ver not in (1, 2) or uncomp_size == 0 or uncomp_size > 64 * 1024 * 1024:
        return None, f"unrecognized outer header (ver={ver}, size={uncomp_size})"
    if blob[6:10] == PS2ZIP_MAGIC:
        if len(blob) < 14:
            return None, "truncated PS2ZIP header"
        neg_size = struct.unpack_from("<i", blob, 10)[0]
        if neg_size >= 0 or -neg_size > 64 * 1024 * 1024:
            return None, f"invalid PS2ZIP size ({neg_size})"
        kind, comp = "PS2ZIP", blob[14:]
    else:
        kind, comp = "raw-deflate", blob[6:]
    try:
        raw = zlib.decompress(comp, -15)
    except zlib.error as e:
        return None, f"DEFLATE error: {e}"
    if len(raw) != uncomp_size:
        return None, f"size mismatch: header={uncomp_size}, inflated={len(raw)}"
    return (kind, raw), None


def parse_sections(raw):
    pos = 0
    if pos + 2 > len(raw):
        return None, "truncated header 1"
    pos += 2 + struct.unpack_from("<H", raw, pos)[0]
    if pos + 2 > len(raw):
        return None, "truncated header 2"
    pos += 2 + struct.unpack_from("<H", raw, pos)[0]
    if pos + 8 > len(raw):
        return None, "truncated section directory"
    nsec, entry = struct.unpack_from("<iI", raw, pos)
    pos += 8
    if nsec <= 0 or nsec > 64 or entry == 0:
        return None, f"invalid section count ({nsec}) or entry (0x{entry:08x})"
    sections = []
    for i in range(nsec):
        if pos + 8 > len(raw):
            return None, f"truncated section {i} header"
        vaddr, sz = struct.unpack_from("<II", raw, pos)
        pos += 8
        if sz == 0 or pos + sz > len(raw):
            return None, f"invalid section {i} payload"
        sections.append((vaddr, raw[pos:pos + sz]))
        pos += sz
    if pos != len(raw) and any(b != 0 for b in raw[pos:]):
        return None, f"unexpected trailing bytes ({len(raw) - pos})"
    return (entry, sections), None


def emit_elf(path, entry, sections):
    ehdr_size, phdr_size, phnum = 52, 32, len(sections)
    ehdr = b"\x7fELF" + bytes([1, 1, 1, 0]) + bytes(8)
    ehdr += struct.pack(
        "<HHIIIIIHHHHHH",
        2, 8, 1, entry, ehdr_size, 0, 0x20924001,
        ehdr_size, phdr_size, phnum, 0, 0, 0,
    )
    offset = (ehdr_size + phnum * phdr_size + 15) & ~15
    phdrs = bytearray()
    payload = bytearray(offset - (ehdr_size + phnum * phdr_size))
    for vaddr, data in sections:
        phdrs += struct.pack(
            "<8I", 1, offset, vaddr, vaddr & 0x1FFFFFFF, len(data), len(data), 7, 16
        )
        payload += data
        offset += len(data)
        pad = (-offset) & 15
        if pad:
            payload += bytes(pad)
            offset += pad
    with open(path, "wb") as f:
        f.write(ehdr + phdrs + payload)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("core")
    ap.add_argument("--emit-elf", help="write reconstructed 32-bit MIPS ELF")
    a = ap.parse_args()

    unpacked, err = unpack_core(open(a.core, "rb").read())
    if err:
        print(f"status          : ENCRYPTED or unrecognized container ({err})")
        sys.exit(2)
    kind, raw = unpacked
    parsed, err = parse_sections(raw)
    if err:
        print(f"status          : ENCRYPTED or malformed layout ({err})")
        sys.exit(2)
    entry, sections = parsed
    print(f"container       : {kind}")
    print(f"uncompressed    : {len(raw)} bytes")
    print(f"entry point     : 0x{entry:08x}")
    print(f"sections        : {len(sections)}")
    for i, (vaddr, data) in enumerate(sections):
        print(f"  [{i}] vaddr=0x{vaddr:08x} size=0x{len(data):x}")
    print("layout check    : consistent")
    if a.emit_elf:
        emit_elf(a.emit_elf, entry, sections)
        print(f"written ELF     : {a.emit_elf}")


if __name__ == "__main__":
    main()
