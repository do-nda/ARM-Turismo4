#include "ps2rt.h"
#include <string.h>

#ifdef __ANDROID__
#include <android/log.h>
#define SIF_LOGI(...) __android_log_print(ANDROID_LOG_INFO, "GT4Recomp", __VA_ARGS__)
#define SIF_LOGE(...) __android_log_print(ANDROID_LOG_ERROR, "GT4Recomp", __VA_ARGS__)
#else
#define SIF_LOGI(...) ((void)0)
#define SIF_LOGE(...) ((void)0)
#endif

extern int ps2_cdvd_read_sectors(u32 lsn, u32 sectors, u8 *dst_ram);

static u32 g_last_send_src = 0;
static u32 g_last_send_size = 0;
static u32 g_cd_tray_state = 0;
static u32 g_cd_disk_type = 0x14u; /* SCECdPS2DVD */
static u32 g_cd_status = 0x02u;    /* SCECdStatPause (ready) */
static u32 g_pad_buttons = 0xFFFFu; /* active-low: all released */

void ps2_pad_set_buttons(u32 buttons_active_low) {
g_pad_buttons = buttons_active_low & 0xFFFFu;
}

void ps2_sif_capture_dma(Ctx *ctx, u32 xfer_addr, u32 count) {
g_last_send_src = 0;
g_last_send_size = 0;
if (!xfer_addr || count < 2u) return;
u32 s0_src  = ps2_rd32(ctx, xfer_addr + 0x00u);
u32 s0_size = ps2_rd32(ctx, xfer_addr + 0x08u);
u32 s1_src  = ps2_rd32(ctx, xfer_addr + 0x10u);
u32 s1_size = ps2_rd32(ctx, xfer_addr + 0x18u);
if (s1_src && s1_size >= 0x20u) {
    u32 fcode = ps2_rd32(ctx, s1_src + 0x08u);
    if (fcode == 0x8000000Au) {
        g_last_send_src = s0_src;
        g_last_send_size = s0_size;
    }
}
}

static void write_pad_buffer(Ctx *ctx, u32 dst_addr) {
if (!dst_addr) return;
u8 pkt[32];
memset(pkt, 0, sizeof(pkt));
pkt[0] = 0x00; /* ok */
pkt[1] = 0x70; /* DualShock 2 mode (0x7 << 4) */
pkt[2] = (u8)(g_pad_buttons & 0xFFu);
pkt[3] = (u8)((g_pad_buttons >> 8) & 0xFFu);
pkt[4] = 0x80; /* RX center */
pkt[5] = 0x80; /* RY center */
pkt[6] = 0x80; /* LX center */
pkt[7] = 0x80; /* LY center */
for (u32 i = 0; i < 32u; i++) {
    ps2_wr8(ctx, dst_addr + i, pkt[i]);
}
}

int ps2_sif_handle_rpc_call(Ctx *ctx, u32 sid, u32 fno, u32 w9, u32 w10, u32 recv_buf, u32 recv_size) {
u32 send_buf = g_last_send_src ? g_last_send_src : w10;
(void)w9;

/* CDVD NCMD (0x80000595): async disc read commands */
if (sid == 0x80000595u) {
    if ((fno == 1u || fno == 2u || fno == 12u) && send_buf) {
        u32 lsn     = ps2_rd32(ctx, send_buf + 0x00u);
        u32 sectors = ps2_rd32(ctx, send_buf + 0x04u);
        u32 ee_dst  = ps2_rd32(ctx, send_buf + 0x08u);
        if (ee_dst && sectors > 0u && sectors <= 2048u) {
            u8 *dst_ptr = ps2_ptr(ctx, ee_dst);
            int rc = ps2_cdvd_read_sectors(lsn, sectors, dst_ptr);
            SIF_LOGI("CDVD read lsn=%u sectors=%u -> 0x%08x (rc=%d)", lsn, sectors, ee_dst, rc);
            if (recv_buf && recv_size >= 4u) {
                ps2_wr32(ctx, recv_buf, (rc == 0) ? 1u : 0u);
            }
            return 1;
        }
    }
    if (recv_buf && recv_size >= 4u) ps2_wr32(ctx, recv_buf, 1u);
    return 1;
}

/* CDVD SCMD (0x80000593): disc status, type, clock, media mode */
if (sid == 0x80000593u) {
    if (recv_buf && recv_size >= 4u) {
        if (fno == 0x01u && recv_size >= 8u) { /* CdReadClock */
            ps2_wr8(ctx, recv_buf + 0, 0x00);
            ps2_wr8(ctx, recv_buf + 1, 0x00);
            ps2_wr8(ctx, recv_buf + 2, 0x00);
            ps2_wr8(ctx, recv_buf + 3, 0x12);
            ps2_wr8(ctx, recv_buf + 4, 0x00);
            ps2_wr8(ctx, recv_buf + 5, 0x27);
            ps2_wr8(ctx, recv_buf + 6, 0x11);
            ps2_wr8(ctx, recv_buf + 7, 0x26);
        } else if (fno == 0x03u) { /* CdGetDiskType */
            ps2_wr32(ctx, recv_buf, g_cd_disk_type);
        } else if (fno == 0x04u) { /* CdGetError */
            ps2_wr32(ctx, recv_buf, 0u);
        } else if (fno == 0x05u) { /* CdTrayReq */
            ps2_wr32(ctx, recv_buf, g_cd_tray_state);
        } else if (fno == 0x0Bu) { /* CdStatus */
            ps2_wr32(ctx, recv_buf, g_cd_status);
        } else {
            ps2_wr32(ctx, recv_buf, 1u);
        }
    }
    return 1;
}

/* CDVD Init / SearchFile (0x80000592, 0x80000597, 0x8000059A, 0x8000059C) */
if ((sid & 0xFFFFFF00u) == 0x80000500u) {
    if (recv_buf && recv_size >= 4u) ps2_wr32(ctx, recv_buf, 1u);
    return 1;
}

/* PADMAN (0x80000100 / 0x80000101): DualShock 2 state */
if (sid == 0x80000100u || sid == 0x80000101u) {
    if (send_buf && fno == 1u) {
        u32 pad_dma_buf = ps2_rd32(ctx, send_buf + 0x10u);
        write_pad_buffer(ctx, pad_dma_buf);
    }
    if (recv_buf && recv_size >= 4u) {
        ps2_wr32(ctx, recv_buf + 0x00u, 1u);
        if (recv_size >= 0x14u) ps2_wr32(ctx, recv_buf + 0x10u, 1u);
    }
    return 1;
}

/* MCMAN / MCSERV (0x80000400): report slot 0 formatted PS2 card */
if (sid == 0x80000400u) {
    if (recv_buf && recv_size >= 4u) {
        ps2_wr32(ctx, recv_buf, (fno == 2u) ? 2u : 0u);
    }
    return 1;
}

/* SDR / LIBSD sound driver (0x80000701) */
if (sid == 0x80000701u) {
    if (recv_buf && recv_size >= 4u) ps2_wr32(ctx, recv_buf, 0u);
    return 1;
}

return 0;
}
