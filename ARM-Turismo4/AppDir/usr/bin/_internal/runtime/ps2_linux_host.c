#ifndef __ANDROID__
#include "ps2rt.h"
#include <stdio.h>
#include <stdlib.h>

static FILE *g_linux_iso = NULL;

int ps2_cdvd_read_sectors(u32 lsn, u32 sectors, u8 *dst_ram) {
if (!g_linux_iso) {
    const char *iso = getenv("GT4_ISO");
    if (!iso || !*iso) return -1;
    g_linux_iso = fopen(iso, "rb");
    if (!g_linux_iso) return -1;
}
if (fseeko(g_linux_iso, (off_t)lsn * 2048LL, SEEK_SET) != 0) return -1;
size_t want = (size_t)sectors * 2048u;
return (fread(dst_ram, 1, want, g_linux_iso) == want) ? 0 : -1;
}

void ps2_gs_submit_gif(Ctx *ctx, u32 addr, u32 qwc) { (void)ctx; (void)addr; (void)qwc; }
void ps2_gs_present_frame(void) {
static int f = 0;
if (++f == 1 || (f % 60) == 0) fprintf(stderr, "linux-host: frame %d\n", f);
}
#endif
