/* ps2rt.c - prototype runtime: loads YOUR ELF into emulated RAM and runs the
 * recompiled entry function. Everything hardware-related is a stub. */
#include "ps2rt.h"
#include <stdio.h>
#include <stdlib.h>

static int load_elf(Ctx *ctx, const char *path) {
    FILE *f = fopen(path, "rb");
    if (!f) { perror(path); return -1; }
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    u8 *buf = malloc((size_t)n);
    if (!buf || fread(buf, 1, (size_t)n, f) != (size_t)n) { fclose(f); return -1; }
    fclose(f);

    u16 phentsize, phnum;
    u32 phoff;
    memcpy(&phoff, buf + 0x1C, 4);
    memcpy(&phentsize, buf + 0x2A, 2);
    memcpy(&phnum, buf + 0x2C, 2);
    for (u16 i = 0; i < phnum; i++) {
        u32 ph[8];                       /* type off vaddr paddr filesz memsz flags align */
        memcpy(ph, buf + phoff + (size_t)i * phentsize, sizeof ph);
        if (ph[0] != 1) continue;
        u32 dst = ph[2] & 0x1FFFFFFFu;
        if ((u64)dst + ph[5] > PS2_RAM_SIZE || (u64)ph[1] + ph[4] > (u64)n) {
            fprintf(stderr, "ps2rt: segment %u does not fit\n", i);
            free(buf);
            return -1;
        }
        memcpy(ctx->ram + dst, buf + ph[1], ph[4]);
        memset(ctx->ram + dst + ph[4], 0, ph[5] - ph[4]);   /* .bss */
    }
    free(buf);
    return 0;
}

void ps2_dispatch(Ctx *ctx, u32 addr) {
    u32 lo = 0, hi = ps2_func_count;
    while (lo < hi) {
        u32 m = (lo + hi) / 2;
        if (ps2_func_table[m].addr < addr) lo = m + 1; else hi = m;
    }
    if (lo < ps2_func_count && ps2_func_table[lo].addr == addr) {
        ps2_func_table[lo].fn(ctx);
        return;
    }
    FILE *f = fopen("missing_entries.txt", "a");
    if (f) { fprintf(f, "0x%08x\n", addr); fclose(f); }
    fprintf(stderr, "ps2rt: unresolved indirect target 0x%08x "
                    "(logged to missing_entries.txt; re-run r5900lift with --entries)\n", addr);
    exit(3);
}

void ps2_unimpl(Ctx *ctx, u32 pc, u32 word) {
    (void)ctx;
    fprintf(stderr, "ps2rt: unimplemented instruction 0x%08x at pc 0x%08x\n", word, pc);
    exit(4);
}

/* PLACEHOLDER syscalls for the self-test only (NOT the real PS2 BIOS table):
 * $v1 == 1 prints $a0 as a signed int, $v1 == 2 exits. */
void ps2_syscall(Ctx *ctx, u32 pc) {
    switch ((u32)G(3)) {
    case 1: printf("%d\n", (int)G(4)); break;
    case 2: exit(0);
    default:
        fprintf(stderr, "ps2rt: unhandled syscall %u at 0x%08x\n", (u32)G(3), pc);
        exit(5);
    }
}

u8 *ps2_hw_access(Ctx *ctx, u32 addr) {
    static u32 seen[64];
    static unsigned n;
    u32 page = addr >> 12;
    for (unsigned i = 0; i < n; i++)
        if (seen[i] == page) return ctx->dummy;
    if (n < 64) seen[n++] = page;
    FILE *f = fopen("hw_access_log.txt", "a");
    if (f) { fprintf(f, "0x%08x\n", addr); fclose(f); }
    fprintf(stderr, "ps2rt: unmodelled hardware/unmapped access near 0x%08x\n", addr);
    return ctx->dummy;
}

int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "usage: %s <your-game.elf>\n", argv[0]); return 1; }
    Ctx *ctx = calloc(1, sizeof *ctx);
    ctx->ram = calloc(1, PS2_RAM_SIZE);
    if (!ctx->ram || load_elf(ctx, argv[1]) != 0) return 1;
    G(29) = 0x01FFFF00u;                    /* initial $sp */
    ps2_dispatch(ctx, ps2_entry_addr);
    return 0;
}
