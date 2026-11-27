#include "ps2rt.h"
#include <string.h>

extern void ps2_gs_submit_gif(Ctx *ctx, u32 addr, u32 qwc);
extern void ps2_gs_present_frame(void);

static u8 *g_last_hw_ptr = NULL;
static u32 g_last_hw_addr = 0;

static void walk_gif_dma_chain(Ctx *ctx, u32 ch_base) {
u32 chcr = ps2_rd32(ctx, ch_base + 0x00u);
u32 madr = ps2_rd32(ctx, ch_base + 0x10u);
u32 qwc  = ps2_rd32(ctx, ch_base + 0x20u) & 0xFFFFu;
u32 tadr = ps2_rd32(ctx, ch_base + 0x30u);
u32 mode = (chcr >> 2) & 3u;

if (mode == 0u) {
    if (madr && qwc) ps2_gs_submit_gif(ctx, madr, qwc);
} else if (mode == 1u && tadr) {
    for (int guard = 0; guard < 4096; guard++) {
        u32 tag_lo = ps2_rd32(ctx, tadr + 0u);
        u32 tag_hi = ps2_rd32(ctx, tadr + 4u);
        u32 tqwc   = tag_lo & 0xFFFFu;
        u32 id     = (tag_lo >> 28) & 7u;
        if (id == 1u || id == 7u) { /* cnt / end */
            if (tqwc) ps2_gs_submit_gif(ctx, tadr + 16u, tqwc);
            tadr += 16u + tqwc * 16u;
            if (id == 7u) break;
        } else if (id == 2u) { /* next */
            if (tqwc) ps2_gs_submit_gif(ctx, tadr + 16u, tqwc);
            tadr = tag_hi & 0x1FFFFFFFu;
        } else if (id == 3u || id == 4u) { /* ref / refs */
            if (tqwc && tag_hi) ps2_gs_submit_gif(ctx, tag_hi & 0x1FFFFFFFu, tqwc);
            tadr += 16u;
        } else {
            break;
        }
    }
}
ps2_wr32(ctx, ch_base + 0x00u, chcr & ~0x100u);
}

void ps2_hw_commit_pending(Ctx *ctx) {
if (!g_last_hw_ptr || !g_last_hw_addr) return;
u32 p = g_last_hw_addr & 0x1FFFFFFFu;
u32 val = 0;
memcpy(&val, g_last_hw_ptr, 4);
g_last_hw_ptr = NULL;
g_last_hw_addr = 0;

/* DMAC channel CHCR writes (bit 8 = STR) */
if ((p & 0xFFFF00FFu) == 0x10000000u && p >= 0x10008000u && p <= 0x1000E000u) {
    if (val & 0x100u) {
        if (p == 0x1000A000u) { /* DMAC ch 2: GIF */
            walk_gif_dma_chain(ctx, p);
        } else {
            u32 clr = val & ~0x100u;
            memcpy(ps2_hw_access(ctx, p), &clr, 4);
            g_last_hw_ptr = NULL;
        }
    }
} else if (p == 0x12001000u && (val & 0x8u)) {
    /* GS_CSR VSync acknowledgement -> swap OpenGL ES buffer */
    ps2_gs_present_frame();
}
}

void ps2_hw_on_access(Ctx *ctx, u32 addr, u8 *ptr) {
ps2_hw_commit_pending(ctx);
u32 p = addr & 0x1FFFFFFFu;
if (p == 0x12001000u) {
    /* GS_CSR: report VSync event (bit 3) + FIFO empty (bit 14) */
    u32 csr = 0x00004008u;
    memcpy(ptr, &csr, 4);
} else if ((p & 0xFFFF00FFu) == 0x10000000u && p >= 0x10008000u && p <= 0x1000E000u) {
    /* Ensure DMAC CHCR STR bit reads clear when idle */
    u32 cur = 0;
    memcpy(&cur, ptr, 4);
    cur &= ~0x100u;
    memcpy(ptr, &cur, 4);
}
g_last_hw_ptr = ptr;
g_last_hw_addr = addr;
}
