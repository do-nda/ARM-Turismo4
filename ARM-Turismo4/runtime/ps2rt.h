/* ps2rt.h - minimal runtime for code emitted by r5900lift.py.
 * Portable C99. Assumes a little-endian host (x86-64, Android arm64, Switch). */
#ifndef PS2RT_H
#define PS2RT_H
#include <stdint.h>
#include <string.h>

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
    u8 spr[16384];                           /* scratchpad at 0x70000000 */
    u8 dummy[16];                            /* sink for unmapped/hardware access */
} Ctx;

typedef struct { u32 addr; void (*fn)(Ctx *); } PS2FuncEntry;
extern const PS2FuncEntry ps2_func_table[];  /* sorted by addr, from func_table.c */
extern const u32 ps2_func_count;
extern const u32 ps2_entry_addr;

#define G(n)  (ctx->r[n].d[0])
#define HI    (ctx->hi[0])
#define LO    (ctx->lo[0])
#define SX32(x) ((u64)(s64)(s32)(x))

/* runtime entry points used by generated code */
void ps2_dispatch(Ctx *ctx, u32 addr);
void ps2_unimpl(Ctx *ctx, u32 pc, u32 word);
void ps2_syscall(Ctx *ctx, u32 pc);
u8  *ps2_hw_access(Ctx *ctx, u32 addr);

/* Address translation. Hardware registers (GS, DMA, VIF, ...) are NOT modelled
 * yet: they hit ps2_hw_access(), which logs the address. */
static inline u8 *ps2_ptr(Ctx *ctx, u32 a) {
    u32 p = a & 0x1FFFFFFFu;
    if (p < PS2_RAM_SIZE) return ctx->ram + p;
    if (a - 0x70000000u < sizeof(ctx->spr)) return ctx->spr + (a - 0x70000000u);
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

#endif
