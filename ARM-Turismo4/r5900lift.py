import re
#!/usr/bin/env python3
"""r5900lift.py - static MIPS R5900 (PS2 Emotion Engine) -> portable C99 recompiler."""
import argparse
import sys
import os
import struct
from collections import deque

BAD_OPS = {0x13, 0x1D, 0x30, 0x32, 0x34, 0x35, 0x38, 0x3A, 0x3B, 0x3C, 0x3D}
BAD_SPECIAL = {0x01, 0x05, 0x0E, 0x15, 0x1C, 0x1D, 0x1E, 0x1F, 0x30, 0x31, 0x32, 0x33,
               0x34, 0x35, 0x36, 0x37, 0x39, 0x3D}
VALID_REGIMM = {0, 1, 2, 3, 0x10, 0x11, 0x12, 0x13, 0x18, 0x19}
JR_RA = 0x03E00008


def is_bad_word(w):
    op = w >> 26
    if op in BAD_OPS:
        return True
    if op == 0:
        return (w & 63) in BAD_SPECIAL
    if op == 1:
        return ((w >> 16) & 31) not in VALID_REGIMM
    return False


class Elf:
    def __init__(self, blob):
        if len(blob) < 52 or blob[:4] != b"\x7fELF":
            raise ValueError("not an ELF file")
        if blob[4] != 1 or blob[5] != 1 or struct.unpack_from("<H", blob, 18)[0] != 8:
            raise ValueError("expected 32-bit little-endian MIPS ELF")
        self.entry = struct.unpack_from("<I", blob, 24)[0]
        phoff = struct.unpack_from("<I", blob, 28)[0]
        phentsize = struct.unpack_from("<H", blob, 42)[0]
        phnum = struct.unpack_from("<H", blob, 44)[0]
        self.segments, self.data, self.phys_data = [], {}, {}
        for i in range(phnum):
            off = phoff + i * phentsize
            p_type, p_offset, p_vaddr, p_paddr, p_filesz, p_memsz, p_flags, _ = struct.unpack_from(
                "<8I", blob, off
            )
            if p_type != 1:
                continue
            seg = blob[p_offset:p_offset + p_filesz]
            self.segments.append((p_vaddr, p_paddr, seg, p_memsz, p_flags))
            for j in range(len(seg) // 4):
                w = struct.unpack_from("<I", seg, j * 4)[0]
                va = p_vaddr + j * 4
                self.data[va] = w
                self.phys_data[va & 0x1FFFFFFF] = w

    def get_word(self, addr):
        return self.data[addr] if addr in self.data else self.phys_data.get(addr & 0x1FFFFFFF)


def sx16(imm):
    return imm - 0x10000 if (imm & 0x8000) else imm


def gpr_read(r):
    return "0" if r == 0 else f"G({r})"


def gpr_write(r, expr):
    return "" if r == 0 else f"G({r}) = {expr};"


def lift_insn(pc, w, unimpl_counts, syscalls_seen, prev_v1_imm, vu0_macro=False):
    op = (w >> 26) & 0x3F
    rs = (w >> 21) & 0x1F
    rt = (w >> 16) & 0x1F
    rd = (w >> 11) & 0x1F
    sa = (w >> 6) & 0x1F
    fn = w & 0x3F
    imm = w & 0xFFFF
    simm = sx16(imm)

    def unimpl(label):
        unimpl_counts.setdefault(label, [0, pc])[0] += 1
        return f"ps2_unimpl(ctx, 0x{pc:08x}u, 0x{w:08x}u);"

    if w == 0:
        return "", "normal", None
    if op == 0x02:
        return None, "j", ((pc + 4) & 0xF0000000) | ((w & 0x3FFFFFF) << 2)
    if op == 0x03:
        return None, "jal", ((pc + 4) & 0xF0000000) | ((w & 0x3FFFFFF) << 2)

    if op in (0x04, 0x05, 0x06, 0x07, 0x14, 0x15, 0x16, 0x17):
        tgt = (pc + 4 + (simm << 2)) & 0xFFFFFFFF
        likely = op >= 0x14
        base_op = op - 0x10 if likely else op
        s_expr, t_expr = gpr_read(rs), gpr_read(rt)
        cond = {
            0x04: f"({s_expr} == {t_expr})",
            0x05: f"({s_expr} != {t_expr})",
            0x06: f"((s64){s_expr} <= 0LL)",
            0x07: f"((s64){s_expr} > 0LL)",
        }[base_op]
        return (cond, likely, False), "branch", tgt

    if op == 0x01:
        if rt in (0x00, 0x01, 0x02, 0x03, 0x10, 0x11, 0x12, 0x13):
            tgt = (pc + 4 + (simm << 2)) & 0xFFFFFFFF
            cond = f"((s64){gpr_read(rs)} >= 0LL)" if (rt & 1) else f"((s64){gpr_read(rs)} < 0LL)"
            return (cond, bool(rt & 2), bool(rt & 0x10)), "branch", tgt
        if rt == 0x18:
            return f"ctx->sa = (((u32){gpr_read(rs)} ^ {imm}u) & 15u) * 8u;", "normal", None
        if rt == 0x19:
            return f"ctx->sa = (((u32){gpr_read(rs)} ^ {imm}u) & 7u) * 16u;", "normal", None
        return unimpl(f"REGIMM rt=0x{rt:02x}"), "normal", None

    if op == 0x00:
        if fn == 0x08:
            return (None, "jr_ra", None) if rs == 31 else (gpr_read(rs), "jr", None)
        if fn == 0x09:
            return (gpr_read(rs), rd), "jalr", None
        if fn == 0x0C:
            if pc in prev_v1_imm:
                syscalls_seen.add(prev_v1_imm[pc])
            return f"ps2_syscall(ctx, 0x{pc:08x}u);", "normal", None
        if fn == 0x0D:
            return f"ps2_break(ctx, 0x{pc:08x}u);", "normal", None
        if fn == 0x0F:
            return "/* sync */", "normal", None
        alu = {
            0x00: gpr_write(rd, f"SX32((u32){gpr_read(rt)} << {sa})"),
            0x02: gpr_write(rd, f"SX32((u32){gpr_read(rt)} >> {sa})"),
            0x03: gpr_write(rd, f"SX32((s32){gpr_read(rt)} >> {sa})"),
            0x04: gpr_write(rd, f"SX32((u32){gpr_read(rt)} << ({gpr_read(rs)} & 31u))"),
            0x06: gpr_write(rd, f"SX32((u32){gpr_read(rt)} >> ({gpr_read(rs)} & 31u))"),
            0x07: gpr_write(rd, f"SX32((s32){gpr_read(rt)} >> ({gpr_read(rs)} & 31u))"),
            0x0A: f"if ({gpr_read(rt)} == 0) {{ {gpr_write(rd, gpr_read(rs))} }}",
            0x0B: f"if ({gpr_read(rt)} != 0) {{ {gpr_write(rd, gpr_read(rs))} }}",
            0x10: gpr_write(rd, "HI"),
            0x11: f"HI = {gpr_read(rs)};",
            0x12: gpr_write(rd, "LO"),
            0x13: f"LO = {gpr_read(rs)};",
            0x14: gpr_write(rd, f"({gpr_read(rt)} << ({gpr_read(rs)} & 63u))"),
            0x16: gpr_write(rd, f"({gpr_read(rt)} >> ({gpr_read(rs)} & 63u))"),
            0x17: gpr_write(rd, f"(u64)((s64){gpr_read(rt)} >> ({gpr_read(rs)} & 63u))"),
            0x1A: f"ps2_div(ctx, {gpr_read(rs)}, {gpr_read(rt)});",
            0x1B: f"ps2_divu(ctx, {gpr_read(rs)}, {gpr_read(rt)});",
            0x20: gpr_write(rd, f"SX32((u32){gpr_read(rs)} + (u32){gpr_read(rt)})"),
            0x21: gpr_write(rd, f"SX32((u32){gpr_read(rs)} + (u32){gpr_read(rt)})"),
            0x22: gpr_write(rd, f"SX32((u32){gpr_read(rs)} - (u32){gpr_read(rt)})"),
            0x23: gpr_write(rd, f"SX32((u32){gpr_read(rs)} - (u32){gpr_read(rt)})"),
            0x24: gpr_write(rd, f"({gpr_read(rs)} & {gpr_read(rt)})"),
            0x25: gpr_write(rd, f"({gpr_read(rs)} | {gpr_read(rt)})"),
            0x26: gpr_write(rd, f"({gpr_read(rs)} ^ {gpr_read(rt)})"),
            0x27: gpr_write(rd, f"~({gpr_read(rs)} | {gpr_read(rt)})"),
            0x28: gpr_write(rd, "(u64)ctx->sa"),
            0x29: f"ctx->sa = (u32){gpr_read(rs)} & 127u;",
            0x2A: gpr_write(rd, f"(u64)((s64){gpr_read(rs)} < (s64){gpr_read(rt)})"),
            0x2B: gpr_write(rd, f"(u64)({gpr_read(rs)} < {gpr_read(rt)})"),
            0x2C: gpr_write(rd, f"({gpr_read(rs)} + {gpr_read(rt)})"),
            0x2D: gpr_write(rd, f"({gpr_read(rs)} + {gpr_read(rt)})"),
            0x2E: gpr_write(rd, f"({gpr_read(rs)} - {gpr_read(rt)})"),
            0x2F: gpr_write(rd, f"({gpr_read(rs)} - {gpr_read(rt)})"),
            0x38: gpr_write(rd, f"({gpr_read(rt)} << {sa})"),
            0x3A: gpr_write(rd, f"({gpr_read(rt)} >> {sa})"),
            0x3B: gpr_write(rd, f"(u64)((s64){gpr_read(rt)} >> {sa})"),
            0x3C: gpr_write(rd, f"({gpr_read(rt)} << {sa + 32})"),
            0x3E: gpr_write(rd, f"({gpr_read(rt)} >> {sa + 32})"),
            0x3F: gpr_write(rd, f"(u64)((s64){gpr_read(rt)} >> {sa + 32})"),
        }
        if fn in alu:
            return alu[fn], "normal", None
        if fn == 0x18:
            wr = f" {gpr_write(rd, 'LO')}" if rd else ""
            return (f"{{ s64 p = (s64)(s32){gpr_read(rs)} * (s64)(s32){gpr_read(rt)}; "
                    f"LO = SX32((u32)p); HI = SX32((u32)((u64)p >> 32));{wr} }}"), "normal", None
        if fn == 0x19:
            wr = f" {gpr_write(rd, 'LO')}" if rd else ""
            return (f"{{ u64 p = (u64)(u32){gpr_read(rs)} * (u64)(u32){gpr_read(rt)}; "
                    f"LO = SX32((u32)p); HI = SX32((u32)(p >> 32));{wr} }}"), "normal", None
        return unimpl(f"SPECIAL fn=0x{fn:02x}"), "normal", None

    if op in (0x08, 0x09):
        return gpr_write(rt, f"SX32({gpr_read(rs)} + (u64)({simm}LL))"), "normal", None
    if op == 0x0A:
        return gpr_write(rt, f"(u64)((s64){gpr_read(rs)} < {simm}LL)"), "normal", None
    if op == 0x0B:
        return gpr_write(rt, f"(u64)({gpr_read(rs)} < (u64)(s64){simm}LL)"), "normal", None
    if op == 0x0C:
        return gpr_write(rt, f"({gpr_read(rs)} & {imm}u)"), "normal", None
    if op == 0x0D:
        return gpr_write(rt, f"({gpr_read(rs)} | {imm}u)"), "normal", None
    if op == 0x0E:
        return gpr_write(rt, f"({gpr_read(rs)} ^ {imm}u)"), "normal", None
    if op == 0x0F:
        val = ((imm << 16) & 0xFFFFFFFF) | (0xFFFFFFFF00000000 if (imm & 0x8000) else 0)
        return gpr_write(rt, f"0x{val:x}ull"), "normal", None
    if op in (0x18, 0x19):
        return gpr_write(rt, f"({gpr_read(rs)} + (u64)(s64){simm}LL)"), "normal", None

    eff = f"(u32)({gpr_read(rs)} + (u64)({simm}LL))"
    mem_ops = {
        0x1A: gpr_write(rt, f"ps2_ldl(ctx, {eff}, {gpr_read(rt)})"),
        0x1B: gpr_write(rt, f"ps2_ldr(ctx, {eff}, {gpr_read(rt)})"),
        0x1E: (f"memcpy(&ctx->r[{rt}], ps2_ptr(ctx, {eff} & ~15u), 16);" if rt else ""),
        0x1F: f"memcpy(ps2_ptr(ctx, {eff} & ~15u), &ctx->r[{rt}], 16);",
        0x20: gpr_write(rt, f"(u64)(s64)(s8)ps2_rd8(ctx, {eff})"),
        0x21: gpr_write(rt, f"(u64)(s64)(s16)ps2_rd16(ctx, {eff})"),
        0x22: gpr_write(rt, f"SX32(ps2_lwl(ctx, {eff}, (u32){gpr_read(rt)}))"),
        0x23: gpr_write(rt, f"SX32(ps2_rd32(ctx, {eff}))"),
        0x24: gpr_write(rt, f"(u64)ps2_rd8(ctx, {eff})"),
        0x25: gpr_write(rt, f"(u64)ps2_rd16(ctx, {eff})"),
        0x26: gpr_write(rt, f"SX32(ps2_lwr(ctx, {eff}, (u32){gpr_read(rt)}))"),
        0x27: gpr_write(rt, f"(u64)ps2_rd32(ctx, {eff})"),
        0x28: f"ps2_wr8(ctx, {eff}, (u8){gpr_read(rt)});",
        0x29: f"ps2_wr16(ctx, {eff}, (u16){gpr_read(rt)});",
        0x2A: f"ps2_swl(ctx, {eff}, (u32){gpr_read(rt)});",
        0x2B: f"ps2_wr32(ctx, {eff}, (u32){gpr_read(rt)});",
        0x2C: f"ps2_sdl(ctx, {eff}, {gpr_read(rt)});",
        0x2D: f"ps2_sdr(ctx, {eff}, {gpr_read(rt)});",
        0x2E: f"ps2_swr(ctx, {eff}, (u32){gpr_read(rt)});",
        0x33: "/* pref */",
        0x37: gpr_write(rt, f"ps2_rd64(ctx, {eff})"),
        0x3F: f"ps2_wr64(ctx, {eff}, {gpr_read(rt)});",
    }
    if op in mem_ops:
        return mem_ops[op], "normal", None

    if op == 0x10:
        if rs == 0x00:
            return gpr_write(rt, f"SX32(ps2_cop0_read(ctx, {rd}))"), "normal", None
        if rs == 0x04:
            return f"ps2_cop0_write(ctx, {rd}, (u32){gpr_read(rt)});", "normal", None
        if rs == 0x10 and fn == 0x18:
            return "return;", "eret", None
        if rs == 0x10 and fn == 0x38:
            return "ps2_cop0_ei(ctx);", "normal", None
        if rs == 0x10 and fn == 0x39:
            return "ps2_cop0_di(ctx);", "normal", None
        if rs == 0x10 and fn in (0x01, 0x02, 0x06, 0x08):
            return "/* tlb */", "normal", None
        return unimpl(f"COP0 rs=0x{rs:02x} fn=0x{fn:02x}"), "normal", None

    if op == 0x31:
        return f"F({rt}) = ps2_rd32(ctx, {eff});", "normal", None
    if op == 0x39:
        return f"ps2_wr32(ctx, {eff}, F({rt}));", "normal", None
    if op == 0x11:
        if rs == 0x00:
            return gpr_write(rt, f"SX32(F({rd}))"), "normal", None
        if rs == 0x02:
            return gpr_write(rt, f"SX32(ps2_cfc1(ctx, {rd}))"), "normal", None
        if rs == 0x04:
            return f"F({rd}) = (u32){gpr_read(rt)};", "normal", None
        if rs == 0x06:
            return f"ps2_ctc1(ctx, {rd}, (u32){gpr_read(rt)});", "normal", None
        if rs == 0x08:
            tgt = (pc + 4 + (simm << 2)) & 0xFFFFFFFF
            cond = "((ctx->fcr31 & 0x800000u) != 0)" if (rt & 1) else "((ctx->fcr31 & 0x800000u) == 0)"
            return (cond, bool(rt & 2), False), "branch", tgt
        if rs == 0x10:
            fs, ft, fd = rd, rt, sa
            fpu_s = {
                0x00: f"F({fd}) = ps2_fix(ps2_f(F({fs})) + ps2_f(F({ft})));",
                0x01: f"F({fd}) = ps2_fix(ps2_f(F({fs})) - ps2_f(F({ft})));",
                0x02: f"F({fd}) = ps2_fix(ps2_f(F({fs})) * ps2_f(F({ft})));",
                0x03: f"F({fd}) = ps2_fix(ps2_f(F({fs})) / ps2_f(F({ft})));",
                0x04: f"F({fd}) = ps2_fix(sqrtf(fabsf(ps2_f(F({ft})))));",
                0x05: f"F({fd}) = F({fs}) & 0x7FFFFFFFu;",
                0x06: f"F({fd}) = F({fs});",
                0x07: f"F({fd}) = F({fs}) ^ 0x80000000u;",
                0x16: f"F({fd}) = ps2_fix(ps2_f(F({fs})) / sqrtf(fabsf(ps2_f(F({ft})))));",
                0x18: f"ctx->facc = ps2_fix(ps2_f(F({fs})) + ps2_f(F({ft})));",
                0x19: f"ctx->facc = ps2_fix(ps2_f(F({fs})) - ps2_f(F({ft})));",
                0x1A: f"ctx->facc = ps2_fix(ps2_f(F({fs})) * ps2_f(F({ft})));",
                0x1C: f"F({fd}) = ps2_fix(ps2_f(ctx->facc) + ps2_f(F({fs})) * ps2_f(F({ft})));",
                0x1D: f"F({fd}) = ps2_fix(ps2_f(ctx->facc) - ps2_f(F({fs})) * ps2_f(F({ft})));",
                0x1E: f"ctx->facc = ps2_fix(ps2_f(ctx->facc) + ps2_f(F({fs})) * ps2_f(F({ft})));",
                0x1F: f"ctx->facc = ps2_fix(ps2_f(ctx->facc) - ps2_f(F({fs})) * ps2_f(F({ft})));",
                0x24: f"F({fd}) = ps2_cvtws(ps2_f(F({fs})));",
                0x28: f"{{ float a = ps2_f(F({fs})), b = ps2_f(F({ft})); F({fd}) = ps2_fix(a > b ? a : b); }}",
                0x29: f"{{ float a = ps2_f(F({fs})), b = ps2_f(F({ft})); F({fd}) = ps2_fix(a < b ? a : b); }}",
                0x30: "ps2_fc(ctx, 0);",
                0x32: f"ps2_fc(ctx, ps2_f(F({fs})) == ps2_f(F({ft})));",
                0x34: f"ps2_fc(ctx, ps2_f(F({fs})) < ps2_f(F({ft})));",
                0x36: f"ps2_fc(ctx, ps2_f(F({fs})) <= ps2_f(F({ft})));",
            }
            if fn in fpu_s:
                return fpu_s[fn], "normal", None
        if rs == 0x14 and fn == 0x20:
            return f"F({sa}) = ps2_fix((float)(s32)F({rd}));", "normal", None
        return unimpl(f"COP1 rs=0x{rs:02x} fn=0x{fn:02x}"), "normal", None

    if op == 0x1C:
        if fn == 0x00:
            wr = f" {gpr_write(rd, 'LO')}" if rd else ""
            return (f"{{ s64 acc = ((s64)(s32)HI << 32) | (u32)LO; "
                    f"acc += (s64)(s32){gpr_read(rs)} * (s64)(s32){gpr_read(rt)}; "
                    f"LO = SX32((u32)acc); HI = SX32((u32)((u64)acc >> 32));{wr} }}"), "normal", None
        if fn == 0x01:
            wr = f" {gpr_write(rd, 'LO')}" if rd else ""
            return (f"{{ u64 acc = ((u64)(u32)HI << 32) | (u32)LO; "
                    f"acc += (u64)(u32){gpr_read(rs)} * (u64)(u32){gpr_read(rt)}; "
                    f"LO = SX32((u32)acc); HI = SX32((u32)(acc >> 32));{wr} }}"), "normal", None
        if fn == 0x04:
            return (f"ps2_plzcw(&ctx->r[{rd}], &ctx->r[{rs}]);" if rd else ""), "normal", None
        if fn in (0x10, 0x11, 0x12, 0x13, 0x1A, 0x1B):
            m1 = {
                0x10: gpr_write(rd, "ctx->hi[1]"),
                0x11: f"ctx->hi[1] = {gpr_read(rs)};",
                0x12: gpr_write(rd, "ctx->lo[1]"),
                0x13: f"ctx->lo[1] = {gpr_read(rs)};",
                0x1A: f"ps2_div1(ctx, {gpr_read(rs)}, {gpr_read(rt)});",
                0x1B: f"ps2_divu1(ctx, {gpr_read(rs)}, {gpr_read(rt)});",
            }
            return m1[fn], "normal", None
        if fn == 0x18:
            wr = f" {gpr_write(rd, 'ctx->lo[1]')}" if rd else ""
            return (f"{{ s64 p = (s64)(s32){gpr_read(rs)} * (s64)(s32){gpr_read(rt)}; "
                    f"ctx->lo[1] = SX32((u32)p); ctx->hi[1] = SX32((u32)((u64)p >> 32));{wr} }}"), "normal", None
        if fn == 0x19:
            wr = f" {gpr_write(rd, 'ctx->lo[1]')}" if rd else ""
            return (f"{{ u64 p = (u64)(u32){gpr_read(rs)} * (u64)(u32){gpr_read(rt)}; "
                    f"ctx->lo[1] = SX32((u32)p); ctx->hi[1] = SX32((u32)(p >> 32));{wr} }}"), "normal", None
        if fn == 0x20:
            wr = f" {gpr_write(rd, 'ctx->lo[1]')}" if rd else ""
            return (f"{{ s64 acc = ((s64)(s32)ctx->hi[1] << 32) | (u32)ctx->lo[1]; "
                    f"acc += (s64)(s32){gpr_read(rs)} * (s64)(s32){gpr_read(rt)}; "
                    f"ctx->lo[1] = SX32((u32)acc); ctx->hi[1] = SX32((u32)((u64)acc >> 32));{wr} }}"), "normal", None
        if fn == 0x21:
            wr = f" {gpr_write(rd, 'ctx->lo[1]')}" if rd else ""
            return (f"{{ u64 acc = ((u64)(u32)ctx->hi[1] << 32) | (u32)ctx->lo[1]; "
                    f"acc += (u64)(u32){gpr_read(rs)} * (u64)(u32){gpr_read(rt)}; "
                    f"ctx->lo[1] = SX32((u32)acc); ctx->hi[1] = SX32((u32)(acc >> 32));{wr} }}"), "normal", None
        sh_map = {0x34: "psllh", 0x36: "psrlh", 0x37: "psrah", 0x3C: "psllw", 0x3E: "psrlw", 0x3F: "psraw"}
        if fn in sh_map:
            return (f"ps2_{sh_map[fn]}(&ctx->r[{rd}], &ctx->r[{rt}], {sa});" if rd else ""), "normal", None
        mmi0 = {
            0x00: "paddw", 0x01: "psubw", 0x02: "pcgtw", 0x03: "pmaxw",
            0x04: "paddh", 0x05: "psubh", 0x06: "pcgth", 0x07: "pmaxh",
            0x08: "paddb", 0x09: "psubb", 0x0A: "pcgtb",
            0x10: "paddsw", 0x11: "psubsw", 0x12: "pextlw", 0x13: "ppacw",
            0x14: "paddsh", 0x15: "psubsh", 0x16: "pextlh", 0x17: "ppach",
            0x18: "paddsb", 0x19: "psubsb", 0x1A: "pextlb", 0x1B: "ppacb",
        }
        if fn == 0x08 and sa in mmi0:
            return (f"ps2_{mmi0[sa]}(&ctx->r[{rd}], &ctx->r[{rs}], &ctx->r[{rt}]);" if rd else ""), "normal", None
        mmi1 = {
            0x01: "pabsw", 0x02: "pceqw", 0x03: "pminw",
            0x05: "pabsh", 0x06: "pceqh", 0x07: "pminh",
            0x0A: "pceqb", 0x10: "padduw", 0x11: "psubuw", 0x12: "pextuw",
            0x14: "padduh", 0x15: "psubuh", 0x16: "pextuh",
            0x18: "paddub", 0x19: "psubub", 0x1A: "pextub",
        }
        if fn == 0x28:
            if sa in mmi1:
                return (f"ps2_{mmi1[sa]}(&ctx->r[{rd}], &ctx->r[{rs}], &ctx->r[{rt}]);" if rd else ""), "normal", None
            if sa == 0x1B:
                return (f"ps2_qfsrv(&ctx->r[{rd}], &ctx->r[{rs}], &ctx->r[{rt}], ctx->sa);" if rd else ""), "normal", None
        if fn == 0x09:
            if sa == 0x08:
                return (f"{{ ctx->r[{rd}].d[0] = HI; ctx->r[{rd}].d[1] = ctx->hi[1]; }}" if rd else ""), "normal", None
            if sa == 0x09:
                return (f"{{ ctx->r[{rd}].d[0] = LO; ctx->r[{rd}].d[1] = ctx->lo[1]; }}" if rd else ""), "normal", None
            if sa in (0x0E, 0x12, 0x13):
                nm = {0x0E: "pcpyld", 0x12: "pand", 0x13: "pxor"}[sa]
                return (f"ps2_{nm}(&ctx->r[{rd}], &ctx->r[{rs}], &ctx->r[{rt}]);" if rd else ""), "normal", None
        if fn == 0x29:
            if sa == 0x08:
                return f"{{ HI = ctx->r[{rs}].d[0]; ctx->hi[1] = ctx->r[{rs}].d[1]; }}", "normal", None
            if sa == 0x09:
                return f"{{ LO = ctx->r[{rs}].d[0]; ctx->lo[1] = ctx->r[{rs}].d[1]; }}", "normal", None
            if sa in (0x0E, 0x12, 0x13, 0x1B):
                nm = {0x0E: "pcpyud", 0x12: "por", 0x13: "pnor", 0x1B: "pcpyh"}[sa]
                return (f"ps2_{nm}(&ctx->r[{rd}], &ctx->r[{rs}], &ctx->r[{rt}]);" if rd else ""), "normal", None
        return unimpl(f"MMI funct=0x{fn:02x} sa=0x{sa:02x}"), "normal", None

    if op == 0x36:
        return (f"ps2_vu0_lqc2(ctx, {rt}, {eff});", "normal", None) if vu0_macro else (unimpl("LQC2"), "normal", None)
    if op == 0x3E:
        return (f"ps2_vu0_sqc2(ctx, {rt}, {eff});", "normal", None) if vu0_macro else (unimpl("SQC2"), "normal", None)
    if op == 0x12:
        if vu0_macro and w != 0x4A000000:
            return f"ps2_vu0_macro(ctx, 0x{w:08x}u);", "normal", None
        if rs >= 0x10:
            return unimpl(f"COP2 (VU0 macro) CO funct=0x{fn:02x} bits10_6=0x{sa:02x}"), "normal", None
        return unimpl(f"COP2(VU0 macro) rs/fmt=0x{rs:02x}"), "normal", None

    return unimpl(f"op=0x{op:02x}"), "normal", None


def compute_prev_v1(mem_words):
    out = {}
    for pc, w in mem_words.items():
        if w == 0x0000000C:
            for back in (4, 8):
                pw = mem_words.get(pc - back)
                if pw is not None:
                    op, rs, rt, imm = (pw >> 26) & 0x3F, (pw >> 21) & 0x1F, (pw >> 16) & 0x1F, pw & 0xFFFF
                    if rt == 3 and rs == 0 and op in (0x08, 0x09, 0x0D):
                        out[pc] = sx16(imm) if op in (0x08, 0x09) else imm
                        break
    return out


def is_branch_word(w):
    op, rs, rt = (w >> 26) & 0x3F, (w >> 21) & 0x1F, (w >> 16) & 0x1F
    return (
        op in (0x04, 0x05, 0x06, 0x07, 0x14, 0x15, 0x16, 0x17)
        or (op == 0x01 and rt in (0x00, 0x01, 0x02, 0x03, 0x10, 0x11, 0x12, 0x13))
        or (op in (0x11, 0x12) and rs == 0x08)
    )


def trace_function(entry, mem_words, code_lo, code_hi):
    visited, queue, direct_calls = set(), deque([entry]), set()
    while queue:
        pc = queue.popleft()
        while pc not in visited:
            w = mem_words.get(pc)
            if w is None:
                break
            visited.add(pc)
            op, rs, fn, simm = (w >> 26) & 0x3F, (w >> 21) & 0x1F, w & 0x3F, sx16(w & 0xFFFF)
            if op == 0x10 and rs == 0x10 and fn == 0x18:
                break
            if op in (0x02, 0x03):
                if pc + 4 in mem_words:
                    visited.add(pc + 4)
                tgt = ((pc + 4) & 0xF0000000) | ((w & 0x3FFFFFF) << 2)
                if op == 0x03:
                    direct_calls.add(tgt)
                    queue.append(pc + 8)
                break
            if op == 0x00 and fn in (0x08, 0x09):
                if pc + 4 in mem_words:
                    visited.add(pc + 4)
                if fn == 0x09:
                    queue.append(pc + 8)
                break
            if is_branch_word(w):
                if pc + 4 in mem_words:
                    visited.add(pc + 4)
                tgt = (pc + 4 + (simm << 2)) & 0xFFFFFFFF
                if code_lo <= tgt < code_hi and tgt in mem_words:
                    queue.append(tgt)
                queue.append(pc + 8)
                break
            pc += 4
    return visited, direct_calls


def emit_function(entry, func_entries, mem_words, code_lo, code_hi, unimpl_counts, syscalls_seen, prev_v1, vu0_macro):
    visited, labels, queue = set(), set(), deque([entry])
    while queue:
        pc = queue.popleft()
        while pc not in visited:
            w = mem_words.get(pc)
            if w is None:
                break
            visited.add(pc)
            op, rs, fn, simm = (w >> 26) & 0x3F, (w >> 21) & 0x1F, w & 0x3F, sx16(w & 0xFFFF)
            if op == 0x10 and rs == 0x10 and fn == 0x18:
                break
            if op in (0x02, 0x03):
                if pc + 4 in mem_words:
                    visited.add(pc + 4)
                tgt = ((pc + 4) & 0xF0000000) | ((w & 0x3FFFFFF) << 2)
                if op == 0x03:
                    labels.add(pc + 8)
                    queue.append(pc + 8)
                elif (tgt not in func_entries or tgt == entry) and code_lo <= tgt < code_hi and tgt in mem_words:
                    labels.add(tgt)
                    queue.append(tgt)
                break
            if op == 0x00 and fn in (0x08, 0x09):
                if pc + 4 in mem_words:
                    visited.add(pc + 4)
                if fn == 0x09:
                    labels.add(pc + 8)
                    queue.append(pc + 8)
                break
            if is_branch_word(w):
                if pc + 4 in mem_words:
                    visited.add(pc + 4)
                tgt = (pc + 4 + (simm << 2)) & 0xFFFFFFFF
                if code_lo <= tgt < code_hi and tgt in mem_words:
                    labels.add(tgt)
                    queue.append(tgt)
                labels.add(pc + 8)
                queue.append(pc + 8)
                break
            pc += 4

    out = [f"void f_{entry:08x}(Ctx *ctx) {{"]
    skip_next, lifted_insns = False, 0
    for pc in sorted(visited):
        if skip_next:
            skip_next = False
            continue
        w = mem_words[pc]
        lifted_insns += 1
        lbl = f"  L_{pc:08x}:; " if (pc in labels and pc != entry) else "  "
        stmt, kind, aux = lift_insn(pc, w, unimpl_counts, syscalls_seen, prev_v1, vu0_macro)
        if kind in ("normal", "eret"):
            out.append(f"{lbl}/* {pc:08x} */ {stmt}")
            continue
        dw = mem_words.get(pc + 4, 0)
        raw_dstmt, _, _ = lift_insn(pc + 4, dw, unimpl_counts, syscalls_seen, prev_v1, vu0_macro)
        dstmt = raw_dstmt if isinstance(raw_dstmt, str) else "/* branch in delay slot */"
        skip_next = True
        lifted_insns += 1
        if kind == "jr_ra":
            out.append(f"{lbl}/* {pc:08x} */ {{ {dstmt} return; }}")
        elif kind == "jr":
            out.append(f"{lbl}/* {pc:08x} */ {{ u32 t = (u32){stmt}; {dstmt} ps2_dispatch(ctx, t); return; }}")
        elif kind == "jalr":
            reg_expr, link_reg = stmt
            set_ra = f"{gpr_write(link_reg, f'SX32(0x{pc + 8:08x}u)')} " if link_reg else ""
            out.append(f"{lbl}/* {pc:08x} */ {{ u32 t = (u32){reg_expr}; {set_ra}{dstmt} ps2_dispatch(ctx, t); }} goto L_{pc + 8:08x};")
        elif kind == "jal":
            tgt = aux
            callee = f"f_{tgt:08x}(ctx)" if tgt in func_entries else f"ps2_dispatch(ctx, 0x{tgt:08x}u)"
            out.append(f"{lbl}/* {pc:08x} */ {{ G(31) = SX32(0x{pc + 8:08x}u); {dstmt} {callee}; }} goto L_{pc + 8:08x};")
        elif kind == "j":
            tgt = aux
            if tgt in func_entries and tgt != entry:
                out.append(f"{lbl}/* {pc:08x} */ {{ {dstmt} f_{tgt:08x}(ctx); return; }}")
            elif tgt in labels:
                out.append(f"{lbl}/* {pc:08x} */ {{ {dstmt} goto L_{tgt:08x}; }}")
            else:
                out.append(f"{lbl}/* {pc:08x} */ {{ {dstmt} ps2_dispatch(ctx, 0x{tgt:08x}u); return; }}")
        elif kind == "branch":
            cond, likely, link = stmt
            tgt = aux
            link_code = f"G(31) = SX32(0x{pc + 8:08x}u); " if link else ""
            taken_dst = "" if tgt == pc + 4 else (f"goto L_{tgt:08x};" if tgt in labels else f"ps2_dispatch(ctx, 0x{tgt:08x}u); return;")
            if likely:
                out.append(f"{lbl}/* {pc:08x} */ {{ int c = {cond}; {link_code}if (c) {{ {dstmt} {taken_dst} }} }} goto L_{pc + 8:08x};")
            else:
                out.append(f"{lbl}/* {pc:08x} */ {{ int c = {cond}; {link_code}{dstmt} if (c) {{ {taken_dst} }} }} goto L_{pc + 8:08x};")
    out.append("}\n")
    emitted = set(re.findall(r"\bL_([0-9a-fA-F]{8}):", "\n".join(out)))
    out = [
        re.sub(
            r"goto L_([0-9a-fA-F]{8});",
            lambda m: m.group(0) if m.group(1) in emitted else f"ps2_dispatch(ctx, 0x{m.group(1)}u); return;",
            line,
        )
        for line in out
    ]
    return "\n".join(out), lifted_insns


def write_if_changed(path, content):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                if f.read() == content:
                    return False
        except OSError:
            pass
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return True


def main():
    ap = argparse.ArgumentParser(description=(__doc__ or "r5900lift.py").split("\n")[0])
    ap.add_argument("elf")
    ap.add_argument("-o", "--output", default="out")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--split", type=int, default=0)
    ap.add_argument("--code-range", nargs=2, metavar=("LO", "HI"))
    ap.add_argument("--entries")
    ap.add_argument("--blobs-file")
    ap.add_argument("--add", action="append", default=[])
    ap.add_argument("--vu0-macro", action="store_true")
    a = ap.parse_args()

    elf = Elf(open(a.elf, "rb").read())
    mem_words = dict(elf.data)
    lo = elf.entry & ~3
    while lo - 4 in mem_words:
        lo -= 4
    hi = elf.entry & ~3
    while hi + 4 in mem_words:
        hi += 4
    hi += 4
    code_lo, code_hi = (int(a.code_range[0], 0), int(a.code_range[1], 0)) if a.code_range else (lo, hi)

    strong_seeds = {elf.entry} | {int(s, 0) for s in a.add}
    if a.entries and os.path.exists(a.entries):
        for line in open(a.entries):
            tok = line.strip().split()
            if tok and not tok[0].startswith("#"):
                strong_seeds.add(int(tok[0], 0))

    blob_ranges = []
    if a.blobs_file and os.path.exists(a.blobs_file):
        for line in open(a.blobs_file):
            tok = line.strip().split()
            if len(tok) >= 3 and not tok[0].startswith("#"):
                src, sz, dst = int(tok[0], 0), int(tok[1], 0), int(tok[2], 0)
                for off in range(0, sz, 4):
                    w = elf.get_word(src + off)
                    if w is not None:
                        mem_words[dst + off] = w
                strong_seeds.add(dst)
                blob_ranges.append((dst, dst + sz))

    ptr_seeds, jt_seeds = set(), set()
    if a.scan:
        for addr, val in sorted(elf.data.items()):
            if (val & 3) == 0 and code_lo <= val < code_hi and val in mem_words:
                w = mem_words[val]
                if w != 0 and not is_bad_word(w):
                    nxt, prv = elf.data.get(addr + 4, 0), elf.data.get(addr - 4, 0)
                    if (((nxt & 3) == 0 and code_lo <= nxt < code_hi) or
                            ((prv & 3) == 0 and code_lo <= prv < code_hi)):
                        jt_seeds.add(val)
                    else:
                        ptr_seeds.add(val)
        strong_seeds |= ptr_seeds | jt_seeds

    eff_lo = min([code_lo] + [b[0] for b in blob_ranges])
    eff_hi = max([code_hi] + [b[1] for b in blob_ranges])
    func_entries, covered_pcs, work = set(), set(), deque(sorted(strong_seeds))
    while work:
        fn_addr = work.popleft()
        if fn_addr in func_entries or fn_addr not in mem_words:
            continue
        if not any(blo <= fn_addr < bhi for blo, bhi in blob_ranges) and not (code_lo <= fn_addr < code_hi):
            continue
        func_entries.add(fn_addr)
        vis, calls = trace_function(fn_addr, mem_words, eff_lo, eff_hi)
        covered_pcs |= vis
        for c in calls:
            if c not in func_entries:
                work.append(c)

    jr_total = jr_kept = 0
    if a.scan:
        weak = []
        for pc in range(code_lo, code_hi, 4):
            if mem_words.get(pc) == JR_RA:
                jr_total += 1
                cand = pc + 8
                if code_lo <= cand < code_hi and cand in mem_words and mem_words[cand] != 0:
                    weak.append(cand)
        for cand in weak:
            if cand not in func_entries and cand not in covered_pcs:
                jr_kept += 1
                func_entries.add(cand)
                vis, calls = trace_function(cand, mem_words, code_lo, code_hi)
                covered_pcs |= vis
                for c in calls:
                    if c not in func_entries and code_lo <= c < code_hi and c in mem_words:
                        func_entries.add(c)

    sorted_funcs = sorted(func_entries)
    os.makedirs(a.output, exist_ok=True)
    repo_root = sys._MEIPASS if (getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")) else os.path.dirname(os.path.abspath(__file__))
    for rt_name in ("ps2rt.h", "ps2rt.c"):
        src_rt = os.path.join(repo_root, "runtime", rt_name)
        if os.path.exists(src_rt):
            write_if_changed(os.path.join(a.output, rt_name), open(src_rt, encoding="utf-8").read())

    prev_v1 = compute_prev_v1(mem_words)
    unimpl_counts, syscalls_seen, total_insns, func_bodies = {}, set(), 0, []
    for fn_addr in sorted_funcs:
        body, n_ins = emit_function(
            fn_addr, func_entries, mem_words, eff_lo, eff_hi,
            unimpl_counts, syscalls_seen, prev_v1, a.vu0_macro,
        )
        func_bodies.append(body)
        total_insns += n_ins

    write_if_changed(
        os.path.join(a.output, "generated.h"),
        '#include "ps2rt.h"\n\n' + "\n".join(f"void f_{x:08x}(Ctx *ctx);" for x in sorted_funcs) + "\n",
    )
    keep = {f"generated_{i // a.split:03d}.c" for i in range(0, len(func_bodies), a.split)} if (a.split and a.split > 0) else {"generated.c"}
    for fn in os.listdir(a.output):
        if fn.startswith("generated") and fn.endswith(".c") and fn not in keep:
            os.remove(os.path.join(a.output, fn))
    if a.split and a.split > 0:
        for idx in range(0, len(func_bodies), a.split):
            write_if_changed(
                os.path.join(a.output, f"generated_{idx // a.split:03d}.c"),
                '#include "generated.h"\n\n' + "\n".join(func_bodies[idx:idx + a.split]),
            )
    else:
        write_if_changed(
            os.path.join(a.output, "generated.c"),
            '#include "generated.h"\n\n' + "\n".join(func_bodies),
        )

    table_entries = sorted((fn_addr & 0x1FFFFFFF, fn_addr) for fn_addr in sorted_funcs)
    ft = [
        '#include "generated.h"', "",
        f"const u32 ps2_entry_addr = 0x{elf.entry:08x}u;",
        f"const u32 ps2_func_count = {len(table_entries)};",
        "const PS2FuncEntry ps2_func_table[] = {",
        *[f"  {{0x{k:08x}u, f_{fn:08x}}}," for k, fn in table_entries],
        "};\n",
    ]
    write_if_changed(os.path.join(a.output, "func_table.c"), "\n".join(ft))

    write_if_changed(
        os.path.join(a.output, "Makefile"),
        "CC ?= cc\nCFLAGS ?= -O1 -w\nLDFLAGS ?= -rdynamic -pthread\n"
        "SRC := $(wildcard generated*.c) func_table.c ps2rt.c\nOBJ := $(SRC:.c=.o)\n\n"
        "game: $(OBJ)\n\t$(CC) $(LDFLAGS) -o $@ $(OBJ) -lm\n\n"
        "%.o: %.c ps2rt.h\n\t$(CC) $(CFLAGS) -c $< -o $@\n\n"
        "clean:\n\trm -f $(OBJ) game\n",
    )

    unimpl_total = sum(v[0] for v in unimpl_counts.values())
    rep = [
        f"entry point      : 0x{elf.entry:08x}",
        f"functions        : {len(sorted_funcs)}",
        f"instructions     : {total_insns} ({total_insns - unimpl_total} lifted, {unimpl_total} unimplemented)",
    ]
    if a.code_range:
        rep.append(f"code range       : 0x{code_lo:08x}-0x{code_hi:08x}")
    if a.scan:
        rep.append(
            f"scan seeds       : {len(ptr_seeds)} pointer targets, {len(jt_seeds)} jump-table/vtable entries; "
            f"{jr_total} jr-ra boundaries ({jr_kept} kept, rest already inside known functions)"
        )
    if syscalls_seen:
        rep.append("\nBIOS syscall inventory:")
        rep.extend(f"  (0x{sc:04x})" for sc in sorted(syscalls_seen))
    if unimpl_counts:
        rep.append("\nUnimplemented instruction classes (count, first address):")
        for lbl, (cnt, fpc) in sorted(unimpl_counts.items(), key=lambda kv: -kv[1][0]):
            rep.append(f"  {cnt:7d}  0x{fpc:08x}  {lbl}")
    write_if_changed(os.path.join(a.output, "lift_report.txt"), "\n".join(rep) + "\n")

    print(f"entry point      : 0x{elf.entry:08x}")
    if a.code_range:
        print(f"code range       : 0x{code_lo:08x}-0x{code_hi:08x}")
    print(f"functions        : {len(sorted_funcs)}")
    print(f"instructions     : {total_insns} ({total_insns - unimpl_total} lifted, {unimpl_total} unimplemented)")
    print(f"written to       : {a.output}")


if __name__ == "__main__":
    main()
