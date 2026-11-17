#!/usr/bin/env python3
"""r5900lift - prototype static recompiler: PS2 (MIPS R5900) ELF -> portable C.

Input : an ELF you dumped from your own disc (nothing game-specific is bundled).
Output: generated.c/.h, func_table.c, ps2rt.h/.c (runtime) and lift_report.txt.
The generated C is plain C99 (stdint/string only), so the same output builds
with the Android NDK (arm64-v8a), devkitA64 (Switch) or any desktop compiler.

Pipeline: load ELF -> discover functions (recursive descent) -> lift each
instruction to C -> emit. Anything not yet lifted becomes ps2_unimpl() and is
listed in lift_report.txt. Indirect jumps go through ps2_dispatch(); targets
the runtime cannot resolve are logged to missing_entries.txt, which you feed
back with --entries (the "discover missing functions" loop).
"""
import argparse
import collections
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def sx16(v):
    return v - 0x10000 if v & 0x8000 else v


def sx32(v):
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


def M(n):  # register read
    return "0" if n == 0 else f"G({n})"


def W(n, e):  # register write (writes to $zero vanish)
    return "" if n == 0 else f"G({n}) = {e};"


class Elf:
    def __init__(self, data):
        if data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 1:
            sys.exit("error: expected a 32-bit little-endian ELF")
        (_, machine, _, self.entry, phoff, _, _, _, phentsize,
         phnum) = struct.unpack_from("<HHIIIIIHHH", data, 16)
        if machine != 8:
            sys.exit("error: not a MIPS ELF (e_machine != 8)")
        segs = []
        for i in range(phnum):
            t, off, va, _pa, fsz, _msz, fl, _al = struct.unpack_from(
                "<8I", data, phoff + i * phentsize)
            if t == 1:
                segs.append((va, off, fsz, fl))
        exe = [s for s in segs if s[3] & 1] or segs

        def words(va, off, fsz):
            n = fsz // 4
            vals = struct.unpack_from(f"<{n}I", data, off)
            return {va + 4 * i: v for i, v in enumerate(vals)}

        self.code, self.data = {}, {}
        for va, off, fsz, _ in exe:
            self.code.update(words(va, off, fsz))
        for va, off, fsz, _ in segs:
            self.data.update(words(va, off, fsz))


class Stats:
    def __init__(self):
        self.total = 0
        self.ok = 0
        self.unimpl = collections.OrderedDict()  # name -> [count, first_pc]
        self.indirect = []

    def bad(self, name, pc):
        e = self.unimpl.setdefault(name, [0, pc])
        e[0] += 1


def describe(w):
    op = w >> 26
    if op == 0:
        return f"SPECIAL funct=0x{w & 63:02x}"
    if op == 1:
        return f"REGIMM rt=0x{(w >> 16) & 31:02x}"
    cop = {0x10: "COP0", 0x11: "COP1(FPU)", 0x12: "COP2(VU0 macro)"}
    if op in cop:
        return f"{cop[op]} rs/fmt=0x{(w >> 21) & 31:02x}"
    if op == 0x1C:
        return f"MMI funct=0x{w & 63:02x}"
    names = {0x1A: "LDL", 0x1B: "LDR", 0x2C: "SDL", 0x2D: "SDR", 0x31: "LWC1",
             0x39: "SWC1", 0x36: "LQC2", 0x3E: "SQC2"}
    return names.get(op, f"opcode 0x{op:02x}")


# ---------------------------------------------------------------- lifting
SP_D = {  # SPECIAL ops of the form rd = expr
    0x00: "SX32((u32){RT} << {sh})", 0x02: "SX32((u32){RT} >> {sh})",
    0x03: "SX32((s32){RT} >> {sh})",
    0x04: "SX32((u32){RT} << ({RS} & 31))",
    0x06: "SX32((u32){RT} >> ({RS} & 31))",
    0x07: "SX32((s32){RT} >> ({RS} & 31))",
    0x10: "HI", 0x12: "LO",
    0x14: "{RT} << ({RS} & 63)", 0x16: "{RT} >> ({RS} & 63)",
    0x17: "(u64)((s64){RT} >> ({RS} & 63))",
    0x20: "SX32({RS} + {RT})", 0x21: "SX32({RS} + {RT})",
    0x22: "SX32({RS} - {RT})", 0x23: "SX32({RS} - {RT})",
    0x24: "{RS} & {RT}", 0x25: "{RS} | {RT}", 0x26: "{RS} ^ {RT}",
    0x27: "~({RS} | {RT})",
    0x2A: "(u64)((s64){RS} < (s64){RT})", 0x2B: "(u64)({RS} < {RT})",
    0x2C: "{RS} + {RT}", 0x2D: "{RS} + {RT}",
    0x2E: "{RS} - {RT}", 0x2F: "{RS} - {RT}",
    0x38: "{RT} << {sh}", 0x3A: "{RT} >> {sh}",
    0x3B: "(u64)((s64){RT} >> {sh})",
    0x3C: "{RT} << {sh32}", 0x3E: "{RT} >> {sh32}",
    0x3F: "(u64)((s64){RT} >> {sh32})",
}
IMM_D = {  # I-type ops of the form rt = expr
    0x08: "SX32({RS} + {I})", 0x09: "SX32({RS} + {I})",
    0x18: "{RS} + {I}", 0x19: "{RS} + {I}",
    0x0A: "(u64)((s64){RS} < {simm}LL)", 0x0B: "(u64)({RS} < {I})",
    0x0C: "{RS} & {imm}u", 0x0D: "{RS} | {imm}u", 0x0E: "{RS} ^ {imm}u",
    0x20: "(u64)(s64)(s8)ps2_rd8(ctx, {A})", 0x24: "(u64)ps2_rd8(ctx, {A})",
    0x21: "(u64)(s64)(s16)ps2_rd16(ctx, {A})",
    0x25: "(u64)ps2_rd16(ctx, {A})",
    0x23: "SX32(ps2_rd32(ctx, {A}))", 0x27: "(u64)ps2_rd32(ctx, {A})",
    0x37: "ps2_rd64(ctx, {A})",
    0x1A: "ps2_ldl(ctx, {A}, {RT})", 0x1B: "ps2_ldr(ctx, {A}, {RT})",
    0x22: "SX32(ps2_lwl(ctx, {A}, (u32){RT}))",
    0x26: "SX32(ps2_lwr(ctx, {A}, (u32){RT}))",
}
ST = {
    0x28: "ps2_wr8(ctx, {A}, (u8){RT});", 0x29: "ps2_wr16(ctx, {A}, (u16){RT});",
    0x2B: "ps2_wr32(ctx, {A}, (u32){RT});", 0x3F: "ps2_wr64(ctx, {A}, {RT});",
    0x2C: "ps2_sdl(ctx, {A}, {RT});", 0x2D: "ps2_sdr(ctx, {A}, {RT});",
    0x2A: "ps2_swl(ctx, {A}, (u32){RT});", 0x2E: "ps2_swr(ctx, {A}, (u32){RT});",
}


MMI_TAB = {  # (funct, sa) -> helper name. Encodings from the R5900 manual, from memory:
    # verify against a reference before trusting.
    (0x08, 0x00): "paddw", (0x08, 0x01): "psubw", (0x08, 0x02): "pcgtw",
    (0x08, 0x03): "pmaxw", (0x08, 0x04): "paddh", (0x08, 0x05): "psubh",
    (0x08, 0x06): "pcgth", (0x08, 0x07): "pmaxh", (0x08, 0x08): "paddb",
    (0x08, 0x09): "psubb", (0x08, 0x0A): "pcgtb",
    (0x08, 0x10): "paddsw", (0x08, 0x11): "psubsw", (0x08, 0x12): "pextlw",
    (0x08, 0x13): "ppacw", (0x08, 0x14): "paddsh", (0x08, 0x15): "psubsh",
    (0x08, 0x16): "pextlh", (0x08, 0x17): "ppach", (0x08, 0x18): "paddsb",
    (0x08, 0x19): "psubsb", (0x08, 0x1A): "pextlb", (0x08, 0x1B): "ppacb",
    (0x28, 0x01): "pabsw", (0x28, 0x02): "pceqw", (0x28, 0x03): "pminw",
    (0x28, 0x05): "pabsh", (0x28, 0x06): "pceqh", (0x28, 0x07): "pminh",
    (0x28, 0x0A): "pceqb", (0x28, 0x10): "padduw", (0x28, 0x11): "psubuw",
    (0x28, 0x12): "pextuw", (0x28, 0x14): "padduh", (0x28, 0x15): "psubuh",
    (0x28, 0x16): "pextuh", (0x28, 0x18): "paddub", (0x28, 0x19): "psubub",
    (0x28, 0x1A): "pextub",
    (0x09, 0x0E): "pcpyld", (0x09, 0x12): "pand", (0x09, 0x13): "pxor",
    (0x29, 0x0E): "pcpyud", (0x29, 0x12): "por", (0x29, 0x13): "pnor",
    (0x29, 0x1B): "pcpyh",
}


def mmi(w):
    rs, rt, rd = (w >> 21) & 31, (w >> 16) & 31, (w >> 11) & 31
    sa, fn = (w >> 6) & 31, w & 63
    RS, RT = M(rs), M(rt)
    d, s_, t = f"&ctx->r[{rd}]", f"&ctx->r[{rs}]", f"&ctx->r[{rt}]"
    if fn == 0x04:
        return "" if rd == 0 else f"ps2_plzcw({d}, {s_});"
    if fn == 0x10:
        return W(rd, "ctx->hi[1]")
    if fn == 0x11:
        return f"ctx->hi[1] = {RS};"
    if fn == 0x12:
        return W(rd, "ctx->lo[1]")
    if fn == 0x13:
        return f"ctx->lo[1] = {RS};"
    if fn in (0x18, 0x19):
        sg = "s" if fn == 0x18 else "u"
        return (f"{{ {sg}64 p = ({sg}64)({sg}32){RS} * ({sg}64)({sg}32){RT}; "
                f"ctx->lo[1] = SX32(p); ctx->hi[1] = SX32(p >> 32); "
                f"{W(rd, 'ctx->lo[1]')} }}")
    if fn == 0x1A:
        return f"ps2_div1(ctx, {RS}, {RT});"
    if fn == 0x1B:
        return f"ps2_divu1(ctx, {RS}, {RT});"
    name = MMI_TAB.get((fn, sa))
    if name:
        return "" if rd == 0 else f"ps2_{name}({d}, {s_}, {t});"
    return None


def cop0(w):
    rs, rt, rd, fn = (w >> 21) & 31, (w >> 16) & 31, (w >> 11) & 31, w & 63
    if rs == 0:
        return W(rt, f"SX32(ps2_cop0_read(ctx, {rd}))")
    if rs == 4:
        return f"ps2_cop0_write(ctx, {rd}, (u32){M(rt)});"
    if rs == 0x10 and fn in (0x38, 0x39):
        return "ps2_cop0_ei(ctx);" if fn == 0x38 else "ps2_cop0_di(ctx);"
    return None


def cop1(w):
    fmt, ft, fs = (w >> 21) & 31, (w >> 16) & 31, (w >> 11) & 31
    fd, fn = (w >> 6) & 31, w & 63
    if fmt == 0x00:
        return W(ft, f"SX32(F({fs}))")                      # mfc1
    if fmt == 0x02:
        return W(ft, f"SX32(ps2_cfc1(ctx, {fs}))")          # cfc1
    if fmt == 0x04:
        return f"F({fs}) = (u32){M(ft)};"                   # mtc1
    if fmt == 0x06:
        return f"ps2_ctc1(ctx, {fs}, (u32){M(ft)});"        # ctc1
    if fmt == 0x14 and fn == 0x20:
        return f"F({fd}) = ps2_fix((float)(s32)F({fs}));"   # cvt.s.w
    if fmt != 0x10:
        return None
    a, b, acc = f"ps2_f(F({fs}))", f"ps2_f(F({ft}))", "ps2_f(ctx->facc)"
    t = {
        0x00: f"F({fd}) = ps2_fix({a} + {b});", 0x01: f"F({fd}) = ps2_fix({a} - {b});",
        0x02: f"F({fd}) = ps2_fix({a} * {b});", 0x03: f"F({fd}) = ps2_fix({a} / {b});",
        0x04: f"F({fd}) = ps2_fix(sqrtf(fabsf({b})));",
        0x05: f"F({fd}) = F({fs}) & 0x7FFFFFFFu;", 0x06: f"F({fd}) = F({fs});",
        0x07: f"F({fd}) = F({fs}) ^ 0x80000000u;",
        0x16: f"F({fd}) = ps2_fix({a} / sqrtf(fabsf({b})));",
        0x18: f"ctx->facc = ps2_fix({a} + {b});", 0x19: f"ctx->facc = ps2_fix({a} - {b});",
        0x1A: f"ctx->facc = ps2_fix({a} * {b});",
        0x1C: f"F({fd}) = ps2_fix({acc} + {a} * {b});",
        0x1D: f"F({fd}) = ps2_fix({acc} - {a} * {b});",
        0x1E: f"ctx->facc = ps2_fix({acc} + {a} * {b});",
        0x1F: f"ctx->facc = ps2_fix({acc} - {a} * {b});",
        0x24: f"F({fd}) = ps2_cvtws({a});",
        0x28: f"F({fd}) = ps2_fix({a} > {b} ? {a} : {b});",
        0x29: f"F({fd}) = ps2_fix({a} < {b} ? {a} : {b});",
        0x30: "ps2_fc(ctx, 0);", 0x32: f"ps2_fc(ctx, {a} == {b});",
        0x34: f"ps2_fc(ctx, {a} < {b});", 0x36: f"ps2_fc(ctx, {a} <= {b});",
    }
    return t.get(fn)


def simple(w, pc):
    """Lift a non-control-flow instruction. Returns C text, or None if unsupported."""
    op = w >> 26
    rs, rt, rd = (w >> 21) & 31, (w >> 16) & 31, (w >> 11) & 31
    sh, fn, imm = (w >> 6) & 31, w & 63, w & 0xFFFF
    simm = sx16(imm)
    f = dict(RS=M(rs), RT=M(rt), sh=sh, sh32=sh + 32, imm=imm, simm=simm,
             I=f"(u64)({simm}LL)")
    f["A"] = f"(u32)({f['RS']} + {f['I']})"
    RS, RT = f["RS"], f["RT"]
    if op == 0:
        if fn in SP_D:
            return W(rd, SP_D[fn].format(**f))
        if fn in (0x0A, 0x0B):  # movz / movn
            cmp_ = "==" if fn == 0x0A else "!="
            return "" if rd == 0 else f"if ({RT} {cmp_} 0) G({rd}) = {RS};"
        if fn == 0x0C:
            return f"ps2_syscall(ctx, 0x{pc:08x}u);"
        if fn == 0x0F:
            return ""  # sync
        if fn == 0x11:
            return f"HI = {RS};"
        if fn == 0x13:
            return f"LO = {RS};"
        if fn in (0x18, 0x19):  # R5900 mult/multu also write rd
            sg = "s" if fn == 0x18 else "u"
            return (f"{{ {sg}64 p = ({sg}64)({sg}32){RS} * ({sg}64)({sg}32){RT}; "
                    f"LO = SX32(p); HI = SX32(p >> 32); {W(rd, 'LO')} }}")
        if fn == 0x1A:
            return f"ps2_div(ctx, {RS}, {RT});"
        if fn == 0x1B:
            return f"ps2_divu(ctx, {RS}, {RT});"
        return None
    if op == 0x0F:  # lui, constant folded
        return W(rt, f"0x{(sx32(imm << 16)) & (2**64 - 1):x}ull")
    if op in IMM_D:
        return W(rt, IMM_D[op].format(**f))
    if op in ST:
        return ST[op].format(**f)
    if op == 0x1E:  # lq
        return "" if rt == 0 else (
            f"memcpy(&ctx->r[{rt}], ps2_ptr(ctx, {f['A']} & ~15u), 16);")
    if op == 0x1F:  # sq
        return f"memcpy(ps2_ptr(ctx, {f['A']} & ~15u), &ctx->r[{rt}], 16);"
    if op in (0x2F, 0x33):  # cache, pref
        return ""
    if op == 0x31:  # lwc1
        return f"F({rt}) = ps2_rd32(ctx, {f['A']});"
    if op == 0x39:  # swc1
        return f"ps2_wr32(ctx, {f['A']}, F({rt}));"
    if op == 0x10:
        return cop0(w)
    if op == 0x11:
        return cop1(w)
    if op == 0x1C:
        return mmi(w)
    return None


def ctrl(w, pc):
    """Classify a control-flow instruction; None if it is not one."""
    op = w >> 26
    rs, rt = (w >> 21) & 31, (w >> 16) & 31
    simm = sx16(w & 0xFFFF)
    tgt = (pc + 4 + (simm << 2)) & 0xFFFFFFFF
    RS, RT = M(rs), M(rt)
    if op == 0:
        fn = w & 63
        if fn == 8:
            return dict(kind="ret" if rs == 31 else "jr", rs=rs)
        if fn == 9:
            return dict(kind="jalr", rs=rs, rd=(w >> 11) & 31)
        return None
    if op == 1:
        if rt in (0, 1, 2, 3):
            cond = f"(s64){RS} < 0" if rt in (0, 2) else f"(s64){RS} >= 0"
            return dict(kind="cond", tgt=tgt, cond=cond, likely=rt in (2, 3))
        if rt == 0x11 and rs == 0:
            return dict(kind="bal", tgt=tgt)
        return None  # bltzal etc: reported as unimplemented
    if op == 0x11 and rs == 8 and rt in (0, 1, 2, 3):  # bc1f/bc1t (+likely)
        cond = ("!(ctx->fcr31 & 0x800000u)" if rt in (0, 2) else "(ctx->fcr31 & 0x800000u)")
        return dict(kind="cond", tgt=tgt, cond=cond, likely=rt in (2, 3))
    if op == 2:
        return dict(kind="j", tgt=((pc + 4) & 0xF0000000) | ((w & 0x3FFFFFF) << 2))
    if op == 3:
        return dict(kind="jal", tgt=((pc + 4) & 0xF0000000) | ((w & 0x3FFFFFF) << 2))
    if op in (4, 5, 6, 7, 0x14, 0x15, 0x16, 0x17):
        likely = op >= 0x14
        base = op & 7
        if base == 4 and rs == rt:
            return dict(kind="b", tgt=tgt)
        cond = {4: f"{RS} == {RT}", 5: f"{RS} != {RT}",
                6: f"(s64){RS} <= 0", 7: f"(s64){RS} > 0"}[base]
        return dict(kind="cond", tgt=tgt, cond=cond, likely=likely)
    return None


def analyze(entry, code, entries):
    flow, seen, calls = set(), set(), set()
    work = [entry]
    while work:
        pc = work.pop()
        while pc in code and pc not in flow:
            flow.add(pc)
            seen.add(pc)
            c = ctrl(code[pc], pc)
            if c is None:
                pc += 4
                continue
            seen.add(pc + 4)
            k = c["kind"]
            if k in ("ret", "jr"):
                break
            if k == "j":
                t = c["tgt"]
                if not ((t in entries and t != entry) or t not in code):
                    work.append(t)
                break
            if k == "b":
                work.append(c["tgt"])
                break
            if k in ("jal", "bal"):
                calls.add(c["tgt"])
                pc += 8
            elif k == "jalr":
                pc += 8
            else:  # cond
                work.append(c["tgt"])
                pc += 8
    return flow, seen, calls


def lift_function(entry, flow, code, entries, st):
    out, refs = {}, set()

    def lift_simple(w, pc, count):
        c = simple(w, pc)
        if count:
            st.total += 1
            if c is None:
                st.bad(describe(w), pc)
            else:
                st.ok += 1
        return c if c is not None else f"ps2_unimpl(ctx, 0x{pc:08x}u, 0x{w:08x}u);"

    def go(a):
        refs.add(a)
        return f"goto L_{a:08x};"

    def call(t):
        return f"f_{t:08x}(ctx);" if t in entries else f"ps2_dispatch(ctx, 0x{t:08x}u);"

    for pc in sorted(flow):
        w = code[pc]
        c = ctrl(w, pc)
        if c is None:
            out[pc] = lift_simple(w, pc, True)
            if pc + 4 not in flow:
                out[pc] += " return; /* ran off end of code */"
            continue
        st.total += 1
        st.ok += 1
        dl = lift_simple(code[pc + 4], pc + 4, False) if pc + 4 in code else ""
        nxt = pc + 8
        after = go(nxt) if nxt in flow else "return;"
        link = f"G(31) = 0x{pc + 8:x}u;"
        k = c["kind"]
        if k == "ret":
            out[pc] = f"{{ {dl} return; }}"
        elif k == "jr":
            st.indirect.append((pc, "jr"))
            out[pc] = (f"{{ u32 t = (u32){M(c['rs'])}; {dl} "
                       f"ps2_dispatch(ctx, t); return; }}")
        elif k == "jalr":
            st.indirect.append((pc, "jalr"))
            out[pc] = (f"{{ u32 t = (u32){M(c['rs'])}; {W(c['rd'], f'0x{pc + 8:x}u')} "
                       f"{dl} ps2_dispatch(ctx, t); }} {after}")
        elif k in ("jal", "bal"):
            out[pc] = f"{{ {link} {dl} {call(c['tgt'])} }} {after}"
        elif k == "j":
            t = c["tgt"]
            if (t in entries and t != entry) or t not in flow:
                out[pc] = f"{{ {dl} {call(t)} return; }}"
            else:
                out[pc] = f"{{ {dl} {go(t)} }}"
        elif k == "b":
            t = c["tgt"]
            taken = go(t) if t in flow else f"{call(t)} return;"
            out[pc] = f"{{ {dl} {taken} }}"
        else:  # cond
            t = c["tgt"]
            taken = go(t) if t in flow else f"{call(t)} return;"
            if c["likely"]:
                out[pc] = f"if ({c['cond']}) {{ {dl} {taken} }} {after}"
            else:
                out[pc] = (f"{{ int c = ({c['cond']}); {dl} "
                           f"if (c) {{ {taken} }} }} {after}")
    lines = [f"void f_{entry:08x}(Ctx *ctx) {{"]
    lo = min(flow)
    if lo != entry:
        lines.append("  " + go(entry))
    for pc in sorted(flow):
        lab = f"L_{pc:08x}:; " if pc in refs else ""
        lines.append(f"  {lab}/* {pc:08x} */ {out[pc]}")
    lines.append("}\n")
    return "\n".join(lines)


JR_RA = 0x03E00008


def plausible(w, pc):
    return w != 0 and (ctrl(w, pc) is not None or simple(w, pc) is not None)


def scan_seeds(elf, entries):
    """Heuristic function discovery for code only reachable indirectly.
    (a) the address right after a `jr ra` + delay slot, skipping nop padding;
    (b) words anywhere in the image that point at code and look like a function start."""
    code, data = elf.code, elf.data
    a_set, b_set = set(), set()
    for a, w in code.items():
        if w == JR_RA and a + 8 in code:
            s = a + 8
            while code.get(s) == 0:
                s += 4
            if s in code and plausible(code[s], s):
                a_set.add(s)
    lo, hi = min(code), max(code) + 4
    for v in data.values():
        if lo <= v < hi and v % 4 == 0 and code.get(v, 0) != 0:
            if (code.get(v - 8) == JR_RA or code.get(v - 4) == 0) and plausible(code[v], v):
                b_set.add(v)
    return a_set - entries, b_set - entries


def read_entries(path):
    vals = set()
    if path and os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line:
                vals.add(int(line, 16 if line.lower().startswith("0x") else 0))
    return vals


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("elf")
    ap.add_argument("-o", "--outdir", default="out")
    ap.add_argument("--entries", help="file of extra function entry addresses "
                    "(e.g. the runtime's missing_entries.txt)")
    ap.add_argument("--add", action="append", default=[],
                    help="extra entry address (hex), repeatable")
    ap.add_argument("--scan", action="store_true",
                    help="also discover functions heuristically (jr-ra boundaries and "
                    "function-pointer tables)")
    ap.add_argument("--split", type=int, default=0, metavar="N",
                    help="write N functions per generated_NNN.c file (use for big inputs)")
    a = ap.parse_args()

    elf = Elf(open(a.elf, "rb").read())
    entries = {elf.entry} | read_entries(a.entries) | {int(x, 16) for x in a.add}
    for e in sorted(entries):
        if e not in elf.code:
            print(f"warning: entry 0x{e:08x} is outside executable segments; ignored")
    entries = {e for e in entries if e in elf.code}
    seeds_msg = ""
    if a.scan:
        sa, sb = scan_seeds(elf, entries)
        entries |= sa | sb
        seeds_msg = (f"scan seeds       : {len(sa)} after jr-ra boundaries, "
                     f"{len(sb)} from pointer tables ({len(sa | sb)} unique)")
        print(seeds_msg)

    while True:  # grow the function set to a fixpoint
        funcs, new = {}, set()
        for e in sorted(entries):
            funcs[e] = analyze(e, elf.code, entries)
            new |= funcs[e][2]
        new = {t for t in new if t in elf.code} - entries
        if not new:
            break
        entries |= new

    st = Stats()
    os.makedirs(a.outdir, exist_ok=True)
    for old in os.listdir(a.outdir):  # stale output would cause duplicate symbols
        if old.startswith("generated") and old.endswith(".c"):
            os.remove(os.path.join(a.outdir, old))
    texts = [lift_function(e, funcs[e][0], elf.code, entries, st) for e in sorted(funcs)]
    cfiles = []
    if a.split > 0:
        for i in range(0, len(texts), a.split):
            name = f"generated_{i // a.split:03d}.c"
            open(os.path.join(a.outdir, name), "w").write(
                '#include "generated.h"\n\n' + "\n".join(texts[i:i + a.split]) + "\n")
            cfiles.append(name)
    else:
        open(os.path.join(a.outdir, "generated.c"), "w").write(
            '#include "generated.h"\n\n' + "\n".join(texts) + "\n")
        cfiles.append("generated.c")
    hdr = ['#include "ps2rt.h"', ""]
    hdr += [f"void f_{e:08x}(Ctx *ctx);" for e in sorted(funcs)]
    tbl = ['#include "generated.h"', "",
           f"const u32 ps2_entry_addr = 0x{elf.entry:08x}u;",
           f"const u32 ps2_func_count = {len(funcs)};",
           "const PS2FuncEntry ps2_func_table[] = {"]
    tbl += [f"  {{0x{e:08x}u, f_{e:08x}}}," for e in sorted(funcs)] + ["};"]
    for name, lines in (("generated.h", hdr), ("func_table.c", tbl)):
        open(os.path.join(a.outdir, name), "w").write("\n".join(lines) + "\n")
    for name in ("ps2rt.h", "ps2rt.c"):
        data = open(os.path.join(HERE, "runtime", name)).read()
        open(os.path.join(a.outdir, name), "w").write(data)

    rep = [f"entry point      : 0x{elf.entry:08x}",
           f"functions        : {len(funcs)}",
           f"instructions     : {st.total} ({st.ok} lifted, {st.total - st.ok} unimplemented)",
           f"indirect jumps   : {len(st.indirect)} (resolved at runtime via ps2_dispatch)"]
    if seeds_msg:
        rep.append(seeds_msg)
    rep += ["", "Unimplemented instruction classes (count, first address):"]
    for name, (n, pc) in sorted(st.unimpl.items(), key=lambda kv: -kv[1][0]):
        rep.append(f"  {n:7d}  0x{pc:08x}  {name}")
    open(os.path.join(a.outdir, "lift_report.txt"), "w").write("\n".join(rep) + "\n")
    print("\n".join(rep[:5 if seeds_msg else 4]))
    if st.unimpl:
        print(f"{len(st.unimpl)} unimplemented instruction classes -> see lift_report.txt")
    print("build: cc -O1 -o game " + " ".join(f"{a.outdir}/{c}" for c in cfiles[:2])
          + (" ... " if len(cfiles) > 2 else " ") + f"{a.outdir}/func_table.c "
          f"{a.outdir}/ps2rt.c -lm   (compile all {len(cfiles)} generated file(s))")


if __name__ == "__main__":
    main()
