/* ps2rt.h - minimal runtime for code emitted by r5900lift.py.
 * Portable C99. Assumes a little-endian host (x86-64, Android arm64, Switch). */
#ifndef PS2RT_H
#define PS2RT_H
#include <stdint.h>
#include <string.h>
#include <math.h>

typedef uint8_t u8;   typedef int8_t s8;
typedef uint16_t u16; typedef int16_t s16;
typedef uint32_t u32; typedef int32_t s32;
typedef uint64_t u64; typedef int64_t s64;

#define PS2_RAM_SIZE (32u * 1024u * 1024u)

typedef struct { u64 d[2]; } u128;           /* R5900 GPRs are 128-bit */

typedef struct Ctx {
    u128 r[32];
    u64 hi[2], lo[2];
    u8 *ram;                                 /* PS2_RAM_SIZE bytes */
    u8 *spr;                                 /* scratchpad at 0x70000000 (shared by all threads) */
    void *kthread;                           /* runtime thread record (kernel layer) */
    u8 dummy[16];                            /* sink for unmapped/hardware access */
    u32 f[32];                               /* FPU registers (raw float bits) */
    u32 facc;                                /* FPU accumulator (raw float bits) */
    u32 fcr31;                               /* FPU control/status; condition bit = 0x800000 */
    u32 cop0[32];                            /* COP0 registers (minimal model) */
    u32 sa;                                  /* funnel shift amount, in BITS (0..127) */
} Ctx;

typedef struct { u32 addr; void (*fn)(Ctx *); } PS2FuncEntry;
extern const PS2FuncEntry ps2_func_table[];  /* sorted by addr, from func_table.c */
extern const u32 ps2_func_count;
extern const u32 ps2_entry_addr;

#define G(n)  (ctx->r[n].d[0])
#define HI    (ctx->hi[0])
#define LO    (ctx->lo[0])
#define F(n)  (ctx->f[n])
#define SX32(x) ((u64)(s64)(s32)(x))

/* runtime entry points used by generated code */
void ps2_dispatch(Ctx *ctx, u32 addr);
void ps2_unimpl(Ctx *ctx, u32 pc, u32 word);
void ps2_syscall(Ctx *ctx, u32 pc);
u8  *ps2_hw_access(Ctx *ctx, u32 addr);

/* BREAK is only reached on a trapped condition (e.g. divide by zero): treat as fatal. */
static inline void ps2_break(Ctx *ctx, u32 pc) { ps2_unimpl(ctx, pc, 0x0Du); }

/* Address translation. Hardware registers (GS, DMA, VIF, ...) are NOT modelled
 * yet: they hit ps2_hw_access(), which logs the address. */
static inline u8 *ps2_ptr(Ctx *ctx, u32 a) {
    u32 p = a & 0x1FFFFFFFu;
    if (p < PS2_RAM_SIZE) return ctx->ram + p;
    if (a - 0x70000000u < 16384u) return ctx->spr + (a - 0x70000000u);
    return ps2_hw_access(ctx, a);
}

static inline u8  ps2_rd8 (Ctx *c, u32 a) { return *ps2_ptr(c, a); }
static inline u16 ps2_rd16(Ctx *c, u32 a) { u16 v; memcpy(&v, ps2_ptr(c, a), 2); return v; }
static inline u32 ps2_rd32(Ctx *c, u32 a) { u32 v; memcpy(&v, ps2_ptr(c, a), 4); return v; }
static inline u64 ps2_rd64(Ctx *c, u32 a) { u64 v; memcpy(&v, ps2_ptr(c, a), 8); return v; }
static inline void ps2_wr8 (Ctx *c, u32 a, u8  v) { *ps2_ptr(c, a) = v; }
static inline void ps2_wr16(Ctx *c, u32 a, u16 v) { memcpy(ps2_ptr(c, a), &v, 2); }
static inline void ps2_wr32(Ctx *c, u32 a, u32 v) { memcpy(ps2_ptr(c, a), &v, 4); }
static inline void ps2_wr64(Ctx *c, u32 a, u64 v) { memcpy(ps2_ptr(c, a), &v, 8); }

/* Unaligned word access (little-endian semantics) */
static inline u32 ps2_lwl(Ctx *c, u32 a, u32 old) {
    u32 w = ps2_rd32(c, a & ~3u), s = (3 - (a & 3)) * 8;
    return (w << s) | (old & ((1u << s) - 1));
}
static inline u32 ps2_lwr(Ctx *c, u32 a, u32 old) {
    u32 w = ps2_rd32(c, a & ~3u), s = (a & 3) * 8;
    return (w >> s) | (old & ~(0xFFFFFFFFu >> s));
}
static inline void ps2_swl(Ctx *c, u32 a, u32 v) {
    u32 al = a & ~3u, w = ps2_rd32(c, al), s = (3 - (a & 3)) * 8;
    ps2_wr32(c, al, (w & ~(0xFFFFFFFFu >> s)) | (v >> s));
}
static inline void ps2_swr(Ctx *c, u32 a, u32 v) {
    u32 al = a & ~3u, w = ps2_rd32(c, al), s = (a & 3) * 8;
    ps2_wr32(c, al, (w & ~(0xFFFFFFFFu << s)) | (v << s));
}

/* Division with MIPS edge-case results */
static inline void ps2_div(Ctx *ctx, u64 a, u64 b) {
    s32 x = (s32)a, y = (s32)b;
    if (y == 0)                       { LO = SX32(x < 0 ? 1 : -1); HI = SX32(x); }
    else if (x == INT32_MIN && y == -1) { LO = SX32(x); HI = 0; }
    else                              { LO = SX32(x / y); HI = SX32(x % y); }
}
static inline void ps2_divu(Ctx *ctx, u64 a, u64 b) {
    u32 x = (u32)a, y = (u32)b;
    if (y == 0) { LO = SX32(-1); HI = SX32(x); }
    else        { LO = SX32(x / y); HI = SX32(x % y); }
}

/* ---- 64-bit unaligned access (little-endian) ---- */
static inline u64 ps2_ldl(Ctx *c, u32 a, u64 old) {
    u64 w = ps2_rd64(c, a & ~7u); u32 s = (7 - (a & 7)) * 8;
    return (w << s) | (old & ((1ull << s) - 1));
}
static inline u64 ps2_ldr(Ctx *c, u32 a, u64 old) {
    u64 w = ps2_rd64(c, a & ~7u); u32 s = (a & 7) * 8;
    return (w >> s) | (old & ~(~0ull >> s));
}
static inline void ps2_sdl(Ctx *c, u32 a, u64 v) {
    u32 al = a & ~7u, s = (7 - (a & 7)) * 8; u64 w = ps2_rd64(c, al);
    ps2_wr64(c, al, (w & ~(~0ull >> s)) | (v >> s));
}
static inline void ps2_sdr(Ctx *c, u32 a, u64 v) {
    u32 al = a & ~7u, s = (a & 7) * 8; u64 w = ps2_rd64(c, al);
    ps2_wr64(c, al, (w & ~(~0ull << s)) | (v << s));
}

/* ---- FPU (APPROXIMATION of the R5900 FPU) ----
 * Real hardware has no NaN/Inf (overflow clamps to max float), flushes denormals
 * and rounds toward zero. ps2_fix() models the first two; rounding is NOT modelled. */
static inline float ps2_f(u32 b) { float v; memcpy(&v, &b, 4); return v; }
static inline u32 ps2_fix(float v) {
    u32 b, e; memcpy(&b, &v, 4); e = (b >> 23) & 0xFFu;
    if (e == 0xFFu) b = (b & 0x80000000u) | 0x7F7FFFFFu;
    else if (e == 0) b &= 0x80000000u;
    return b;
}
static inline u32 ps2_cvtws(float v) {
    if (v >= 2147483648.0f) return 0x7FFFFFFFu;
    if (v <= -2147483648.0f) return 0x80000000u;
    return (u32)(s32)v;
}
static inline void ps2_fc(Ctx *ctx, int c) {
    if (c) ctx->fcr31 |= 0x800000u; else ctx->fcr31 &= ~0x800000u;
}
static inline u32 ps2_cfc1(Ctx *ctx, u32 r) { return r == 31 ? ctx->fcr31 : (r == 0 ? 0x2E00u : 0u); }
static inline void ps2_ctc1(Ctx *ctx, u32 r, u32 v) { if (r == 31) ctx->fcr31 = v; }

/* ---- COP0 (minimal: plain storage, Count advances on read, EI/DI toggle Status.IE) ---- */
static inline u32 ps2_cop0_read(Ctx *c, u32 r) {
    r &= 31;
    if (r == 9) c->cop0[9] += 64;
    if (r == 15) return 0x2E20u;
    return c->cop0[r];
}
static inline void ps2_cop0_write(Ctx *c, u32 r, u32 v) { c->cop0[r & 31] = v; }
static inline void ps2_cop0_ei(Ctx *c) { c->cop0[12] |= 1u; }
static inline void ps2_cop0_di(Ctx *c) { c->cop0[12] &= ~1u; }

/* ---- second multiplier/divider pipeline (MMI: mult1/div1...) ---- */
static inline void ps2_div1(Ctx *ctx, u64 a, u64 b) {
    s32 x = (s32)a, y = (s32)b;
    if (y == 0)                         { ctx->lo[1] = SX32(x < 0 ? 1 : -1); ctx->hi[1] = SX32(x); }
    else if (x == INT32_MIN && y == -1) { ctx->lo[1] = SX32(x); ctx->hi[1] = 0; }
    else                                { ctx->lo[1] = SX32(x / y); ctx->hi[1] = SX32(x % y); }
}
static inline void ps2_divu1(Ctx *ctx, u64 a, u64 b) {
    u32 x = (u32)a, y = (u32)b;
    if (y == 0) { ctx->lo[1] = SX32(-1); ctx->hi[1] = SX32(x); }
    else        { ctx->lo[1] = SX32(x / y); ctx->hi[1] = SX32(x % y); }
}

/* ---- MMI (128-bit SIMD) subset. Lane encodings follow my memory of the manual:
 * verify against a reference before trusting results. ---- */
typedef union { u128 q; u64 d[2]; u32 w[4]; u16 h[8]; u8 b[16]; s32 sw[4]; s16 sh[8]; s8 sb[16]; } V128;
static inline s64 ps2_clamp(s64 v, s64 lo, s64 hi) { return v < lo ? lo : (v > hi ? hi : v); }
#define MMI_BIN(name, lane, T, N, EXPR) \
    static inline void ps2_##name(u128 *d, const u128 *s, const u128 *t) { \
        V128 a, b, r; memcpy(&a, s, 16); memcpy(&b, t, 16); \
        for (int i = 0; i < (N); i++) { T x = a.lane[i], y = b.lane[i]; (void)x; (void)y; \
            r.lane[i] = (T)(EXPR); } \
        memcpy(d, &r, 16); }
MMI_BIN(paddw, w, u32, 4, x + y)         MMI_BIN(psubw, w, u32, 4, x - y)
MMI_BIN(paddh, h, u16, 8, x + y)         MMI_BIN(psubh, h, u16, 8, x - y)
MMI_BIN(paddb, b, u8, 16, x + y)         MMI_BIN(psubb, b, u8, 16, x - y)
MMI_BIN(pcgtw, sw, s32, 4, x > y ? -1 : 0)   MMI_BIN(pcgth, sh, s16, 8, x > y ? -1 : 0)
MMI_BIN(pcgtb, sb, s8, 16, x > y ? -1 : 0)
MMI_BIN(pmaxw, sw, s32, 4, x > y ? x : y)    MMI_BIN(pmaxh, sh, s16, 8, x > y ? x : y)
MMI_BIN(pminw, sw, s32, 4, x < y ? x : y)    MMI_BIN(pminh, sh, s16, 8, x < y ? x : y)
MMI_BIN(pceqw, w, u32, 4, x == y ? 0xFFFFFFFFu : 0u)
MMI_BIN(pceqh, h, u16, 8, x == y ? 0xFFFFu : 0u)
MMI_BIN(pceqb, b, u8, 16, x == y ? 0xFFu : 0u)
MMI_BIN(paddsw, sw, s32, 4, ps2_clamp((s64)x + y, INT32_MIN, INT32_MAX))
MMI_BIN(psubsw, sw, s32, 4, ps2_clamp((s64)x - y, INT32_MIN, INT32_MAX))
MMI_BIN(paddsh, sh, s16, 8, ps2_clamp((s32)x + y, -32768, 32767))
MMI_BIN(psubsh, sh, s16, 8, ps2_clamp((s32)x - y, -32768, 32767))
MMI_BIN(paddsb, sb, s8, 16, ps2_clamp((s32)x + y, -128, 127))
MMI_BIN(psubsb, sb, s8, 16, ps2_clamp((s32)x - y, -128, 127))
MMI_BIN(padduw, w, u32, 4, (u64)x + y > 0xFFFFFFFFull ? 0xFFFFFFFFull : (u64)x + y)
MMI_BIN(psubuw, w, u32, 4, x > y ? x - y : 0)
MMI_BIN(padduh, h, u16, 8, (u32)x + y > 0xFFFFu ? 0xFFFFu : (u32)x + y)
MMI_BIN(psubuh, h, u16, 8, x > y ? x - y : 0)
MMI_BIN(paddub, b, u8, 16, (u32)x + y > 0xFFu ? 0xFFu : (u32)x + y)
MMI_BIN(psubub, b, u8, 16, x > y ? x - y : 0)
MMI_BIN(pabsw, sw, s32, 4, y < 0 ? (y == INT32_MIN ? INT32_MAX : -y) : y)
MMI_BIN(pabsh, sh, s16, 8, y < 0 ? (y == -32768 ? 32767 : -y) : y)

#define MMI_SHIFT(name, lane, T, N, MASK, EXPR) \
    static inline void ps2_##name(u128 *d, const u128 *t, u32 sa) { \
        V128 b, r; memcpy(&b, t, 16); sa &= (MASK); \
        for (int i = 0; i < (N); i++) { T y = b.lane[i]; r.lane[i] = (T)(EXPR); } \
        memcpy(d, &r, 16); }
MMI_SHIFT(psllh, h, u16, 8, 15, (u32)y << sa)   MMI_SHIFT(psrlh, h, u16, 8, 15, y >> sa)
MMI_SHIFT(psrah, sh, s16, 8, 15, y >> sa)
MMI_SHIFT(psllw, w, u32, 4, 31, y << sa)        MMI_SHIFT(psrlw, w, u32, 4, 31, y >> sa)
MMI_SHIFT(psraw, sw, s32, 4, 31, y >> sa)

#define MMI_LOAD V128 a, b, r; memcpy(&a, s, 16); memcpy(&b, t, 16); (void)a; (void)b;
static inline void ps2_pextlw(u128 *d, const u128 *s, const u128 *t) {
    MMI_LOAD r.w[0] = b.w[0]; r.w[1] = a.w[0]; r.w[2] = b.w[1]; r.w[3] = a.w[1]; memcpy(d, &r, 16); }
static inline void ps2_pextuw(u128 *d, const u128 *s, const u128 *t) {
    MMI_LOAD r.w[0] = b.w[2]; r.w[1] = a.w[2]; r.w[2] = b.w[3]; r.w[3] = a.w[3]; memcpy(d, &r, 16); }
static inline void ps2_pextlh(u128 *d, const u128 *s, const u128 *t) {
    MMI_LOAD for (int i = 0; i < 4; i++) { r.h[2*i] = b.h[i]; r.h[2*i+1] = a.h[i]; } memcpy(d, &r, 16); }
static inline void ps2_pextuh(u128 *d, const u128 *s, const u128 *t) {
    MMI_LOAD for (int i = 0; i < 4; i++) { r.h[2*i] = b.h[4+i]; r.h[2*i+1] = a.h[4+i]; } memcpy(d, &r, 16); }
static inline void ps2_pextlb(u128 *d, const u128 *s, const u128 *t) {
    MMI_LOAD for (int i = 0; i < 8; i++) { r.b[2*i] = b.b[i]; r.b[2*i+1] = a.b[i]; } memcpy(d, &r, 16); }
static inline void ps2_pextub(u128 *d, const u128 *s, const u128 *t) {
    MMI_LOAD for (int i = 0; i < 8; i++) { r.b[2*i] = b.b[8+i]; r.b[2*i+1] = a.b[8+i]; } memcpy(d, &r, 16); }
static inline void ps2_ppacw(u128 *d, const u128 *s, const u128 *t) {
    MMI_LOAD r.w[0] = b.w[0]; r.w[1] = b.w[2]; r.w[2] = a.w[0]; r.w[3] = a.w[2]; memcpy(d, &r, 16); }
static inline void ps2_ppach(u128 *d, const u128 *s, const u128 *t) {
    MMI_LOAD for (int i = 0; i < 4; i++) { r.h[i] = b.h[2*i]; r.h[4+i] = a.h[2*i]; } memcpy(d, &r, 16); }
static inline void ps2_ppacb(u128 *d, const u128 *s, const u128 *t) {
    MMI_LOAD for (int i = 0; i < 8; i++) { r.b[i] = b.b[2*i]; r.b[8+i] = a.b[2*i]; } memcpy(d, &r, 16); }
static inline void ps2_pcpyld(u128 *d, const u128 *s, const u128 *t) {
    MMI_LOAD r.d[0] = b.d[0]; r.d[1] = a.d[0]; memcpy(d, &r, 16); }
static inline void ps2_pcpyud(u128 *d, const u128 *s, const u128 *t) {
    MMI_LOAD r.d[0] = a.d[1]; r.d[1] = b.d[1]; memcpy(d, &r, 16); }
static inline void ps2_pcpyh(u128 *d, const u128 *s, const u128 *t) {
    MMI_LOAD for (int i = 0; i < 4; i++) { r.h[i] = b.h[0]; r.h[4+i] = b.h[4]; } memcpy(d, &r, 16); }
static inline void ps2_pand(u128 *d, const u128 *s, const u128 *t) {
    MMI_LOAD r.d[0] = a.d[0] & b.d[0]; r.d[1] = a.d[1] & b.d[1]; memcpy(d, &r, 16); }
static inline void ps2_por(u128 *d, const u128 *s, const u128 *t) {
    MMI_LOAD r.d[0] = a.d[0] | b.d[0]; r.d[1] = a.d[1] | b.d[1]; memcpy(d, &r, 16); }
static inline void ps2_pxor(u128 *d, const u128 *s, const u128 *t) {
    MMI_LOAD r.d[0] = a.d[0] ^ b.d[0]; r.d[1] = a.d[1] ^ b.d[1]; memcpy(d, &r, 16); }
static inline void ps2_pnor(u128 *d, const u128 *s, const u128 *t) {
    MMI_LOAD r.d[0] = ~(a.d[0] | b.d[0]); r.d[1] = ~(a.d[1] | b.d[1]); memcpy(d, &r, 16); }
/* QFSRV: {rs:rt} (256 bits, rs high) shifted right by SA bits, low 128 bits kept.
 * MTSAB/MTSAH/MTSA formulas are from memory of the manual: verify against a reference. */
static inline void ps2_qfsrv(u128 *d, const u128 *s, const u128 *t, u32 sa) {
    V128 a, b, r; u64 w[4]; u32 ws, bs; int i;
    memcpy(&a, s, 16); memcpy(&b, t, 16);
    w[0] = b.d[0]; w[1] = b.d[1]; w[2] = a.d[0]; w[3] = a.d[1];
    sa &= 127u; ws = sa / 64u; bs = sa % 64u;
    for (i = 0; i < 2; i++) {
        u64 lo = w[i + ws], hi = w[i + ws + 1];
        r.d[i] = bs ? (lo >> bs) | (hi << (64u - bs)) : lo;
    }
    memcpy(d, &r, 16);
}
/* PLZCW: for each of the two low words, count leading bits equal to the sign bit
 * (excluding the sign bit). Upper 64 bits of the destination are left unchanged. */
static inline void ps2_plzcw(u128 *d, const u128 *s) {
    V128 a, r; memcpy(&a, s, 16); memcpy(&r, d, 16);
    for (int i = 0; i < 2; i++) {
        u32 v = a.w[i], n = 0; if (v & 0x80000000u) v = ~v;
        for (int bit = 30; bit >= 0 && !((v >> bit) & 1u); bit--) n++;
        r.w[i] = n;
    }
    memcpy(d, &r, 16);
}

#endif
