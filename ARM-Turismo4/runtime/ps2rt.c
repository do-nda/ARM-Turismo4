/* ps2rt.c - prototype runtime: loads YOUR ELF into emulated RAM and runs the recompiled
 * entry function, with a minimal "run until something is unmodelled" kernel layer.
 *
 * Syscall numbers and semantics follow the community BIOS reference (ps2tek, "BIOS EE
 * Syscalls"). Struct layouts for thread/semaphore parameters are from memory of the
 * ps2sdk headers: the trace prints the raw words so they can be checked.
 *
 * Every syscall is traced to syscall_trace.log. Anything needing guest threads, the
 * IOP or real hardware stops the run with exit code 5/6/7 and a message. Set
 * PS2_LENIENT=1 to let unknown syscalls return 0 instead of stopping. */
#include "ps2rt.h"
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>

/* ------------------------------------------------------------------ tracing */
static FILE *g_trace;
static unsigned long g_trace_lines, g_syscalls, g_hw_count;
static u32 g_hw_last;
#define TRACE_LIMIT 20000UL
#define SYSCALL_LIMIT 3000000UL
#define HW_LIMIT 20000000UL

static void trace(const char *fmt, ...) {
    va_list ap;
    if (!g_trace || g_trace_lines++ >= TRACE_LIMIT) return;
    va_start(ap, fmt); vfprintf(g_trace, fmt, ap); va_end(ap);
    fputc('\n', g_trace); fflush(g_trace);
}

static void fatal(int code, const char *fmt, ...) {
    va_list ap;
    fprintf(stderr, "ps2rt: ");
    va_start(ap, fmt); vfprintf(stderr, fmt, ap); va_end(ap);
    fprintf(stderr, "\n(see syscall_trace.log for the sequence that led here)\n");
    if (g_trace) {
        fprintf(g_trace, "FATAL: ");
        va_start(ap, fmt); vfprintf(g_trace, fmt, ap); va_end(ap);
        fputc('\n', g_trace); fflush(g_trace);
    }
    exit(code);
}

/* ------------------------------------------------------------ syscall names */
static const char *sys_name(int n) {
    switch (n) {
#define N(num, name) case num: return name;
    N(0x01, "ResetEE") N(0x02, "SetGsCrt") N(0x04, "Exit") N(0x05, "_ExceptionEpilogue")
    N(0x06, "LoadExecPS2") N(0x07, "ExecPS2") N(0x10, "AddIntcHandler")
    N(0x11, "RemoveIntcHandler") N(0x12, "AddDmacHandler") N(0x13, "RemoveDmacHandler")
    N(0x14, "_EnableIntc") N(0x15, "_DisableIntc") N(0x16, "_EnableDmac")
    N(0x17, "_DisableDmac") N(0x20, "CreateThread") N(0x21, "DeleteThread")
    N(0x22, "StartThread") N(0x23, "ExitThread") N(0x24, "ExitDeleteThread")
    N(0x25, "TerminateThread") N(0x26, "iTerminateThread") N(0x29, "ChangeThreadPriority")
    N(0x2A, "iChangeThreadPriority") N(0x2B, "RotateThreadReadyQueue")
    N(0x2C, "_iRotateThreadReadyQueue") N(0x2D, "ReleaseWaitThread")
    N(0x2E, "iReleaseWaitThread") N(0x2F, "GetThreadId") N(0x30, "ReferThreadStatus")
    N(0x31, "iReferThreadStatus") N(0x32, "SleepThread") N(0x33, "WakeupThread")
    N(0x34, "iWakeupThread") N(0x35, "CancelWakeupThread") N(0x36, "iCancelWakeupThread")
    N(0x37, "SuspendThread") N(0x38, "iSuspendThread") N(0x39, "ResumeThread")
    N(0x3A, "iResumeThread") N(0x3B, "JoinThread") N(0x3C, "InitMainThread")
    N(0x3D, "InitHeap") N(0x3E, "EndOfHeap") N(0x40, "CreateSema") N(0x41, "DeleteSema")
    N(0x42, "SignalSema") N(0x43, "iSignalSema") N(0x44, "WaitSema") N(0x45, "PollSema")
    N(0x46, "iPollSema") N(0x64, "FlushCache") N(0x70, "GsGetIMR") N(0x71, "GsPutIMR")
    N(0x73, "SetVSyncFlag") N(0x74, "SetSyscall") N(0x76, "SifDmaStat")
    N(0x77, "SifSetDma") N(0x78, "SifSetDChain") N(0x7B, "ExecOSD") N(0x7D, "PSMode")
    N(0x7E, "MachineType") N(0x7F, "GetMemorySize") N(0x80, "_GetGsDxDyOffset")
    N(0x82, "_InitTLB") N(0x83, "FindAddress") N(0x85, "SetMemoryMode")
    N(0x86, "GetMemoryMode") N(0x87, "ExecPSX")
#undef N
    default: return NULL;
    }
}

/* ------------------------------------------------------------- kernel state */
typedef struct { u32 cause, func, arg; int id, dmac, live; } Handler;
static struct {
    u32 sp, heap_base, heap_end, gp, image_end;
    u32 intc_mask, dmac_mask, vsync_flag, vsync_csr;
    u64 gs_imr;
    Handler h[64];
    int nh, next_hid, next_tid;
    struct { int used; s32 count, max, init; } sema[256];
    struct { int used; u32 func, stack, stack_size, gp, prio; } th[256];
} K = { .next_hid = 1, .next_tid = 2 };

static u32 user_sys[256];   /* guest handlers installed with SetSyscall, by syscall number */

static void words(Ctx *c, u32 a, int n, char *out, size_t cap) {
    int i; size_t k = 0;
    out[0] = 0;
    for (i = 0; i < n && k + 12 < cap; i++)
        k += (size_t)snprintf(out + k, cap - k, "%08x ", ps2_rd32(c, a + 4u * (u32)i));
}

static u64 add_handler(u32 cause, u32 func, u32 arg, int dmac) {
    if (K.nh >= 64) return (u64)(s64)-1;
    K.h[K.nh].cause = cause; K.h[K.nh].func = func; K.h[K.nh].arg = arg;
    K.h[K.nh].dmac = dmac; K.h[K.nh].live = 1; K.h[K.nh].id = K.next_hid;
    K.nh++;
    return (u64)K.next_hid++;
}

static u64 remove_handler(u32 cause, int id, int dmac) {
    int i;
    for (i = 0; i < K.nh; i++)
        if (K.h[i].live && K.h[i].id == id && K.h[i].dmac == dmac && K.h[i].cause == cause) {
            K.h[i].live = 0; return 0;
        }
    return (u64)(s64)-1;
}

void ps2_syscall(Ctx *ctx, u32 pc) {
    s32 raw = (s32)G(3), n = raw < 0 ? -raw : raw;      /* negative = interrupt-safe variant */
    u32 a0 = (u32)G(4), a1 = (u32)G(5), a2 = (u32)G(6), a3 = (u32)G(7), a4 = (u32)G(8);
    u64 ret = 0;
    int ret64 = 0, known = 1;
    const char *name;
    char extra[160] = "";

    if (raw == 0x7000) { printf("%d\n", (int)G(4)); return; }   /* self-test only */
    if (raw == 0x7001) exit(0);                                  /* self-test only */
    if (++g_syscalls > SYSCALL_LIMIT)
        fatal(7, "over %lu syscalls: probably spinning on something unmodelled", SYSCALL_LIMIT);
    name = sys_name(n);

    if (n != 0x74 && n < 256 && user_sys[n]) {   /* the game replaced this syscall: run its code */
        trace("pc=0x%08x %s(%d) a0=%08x a1=%08x a2=%08x a3=%08x -> guest handler 0x%08x",
              pc, name ? name : "?", raw, a0, a1, a2, a3, user_sys[n]);
        ps2_dispatch(ctx, user_sys[n]);
        trace("    guest handler returned v0=%llx", (unsigned long long)G(2));
        return;
    }

    switch (n) {
    case 0x3C:  /* InitMainThread(gp, stack, stack_size, args, root) -> sp */
        K.gp = a0;
        K.sp = (a1 == 0xFFFFFFFFu) ? PS2_RAM_SIZE - a2 : a1 + a2;
        ret = K.sp;
        break;
    case 0x3D:  /* InitHeap(heap, size) -> end of heap */
        K.heap_base = K.image_end;
        K.heap_end = (a0 == 0xFFFFFFFFu) ? K.sp : a0 + a1;
        if (a0 != 0xFFFFFFFFu) K.heap_base = a0;
        ret = K.heap_end;
        break;
    case 0x3E: ret = K.heap_base; break;                          /* EndOfHeap (per reference) */
    case 0x64: break;                                             /* FlushCache */
    case 0x7F: ret = PS2_RAM_SIZE; break;                         /* GetMemorySize */
    case 0x7E: ret = 0; break;                                    /* MachineType */
    case 0x70: ret = K.gs_imr; ret64 = 1; break;                  /* GsGetIMR */
    case 0x71: K.gs_imr = ((u64)a1 << 32) | a0; break;            /* GsPutIMR (64-bit arg) */
    case 0x73: K.vsync_flag = a0; K.vsync_csr = a1; break;        /* SetVSyncFlag */
    case 0x74: if (a0 < 256) user_sys[a0] = a1; break;            /* SetSyscall: remember the guest handler */
    case 0x01: case 0x02: break;                                  /* ResetEE, SetGsCrt */
    case 0x10: case 0x12:                                         /* Add{Intc,Dmac}Handler */
        ret = add_handler(a0, a1, a3, n == 0x12); break;
    case 0x11: case 0x13: ret = remove_handler(a0, (int)a1, n == 0x13); break;
    case 0x14: ret = !((K.intc_mask >> (a0 & 31)) & 1); K.intc_mask |= 1u << (a0 & 31); break;
    case 0x15: ret = (K.intc_mask >> (a0 & 31)) & 1; K.intc_mask &= ~(1u << (a0 & 31)); break;
    case 0x16: ret = !((K.dmac_mask >> (a0 & 31)) & 1); K.dmac_mask |= 1u << (a0 & 31); break;
    case 0x17: ret = (K.dmac_mask >> (a0 & 31)) & 1; K.dmac_mask &= ~(1u << (a0 & 31)); break;
    case 0x2F: ret = 1; break;                                    /* GetThreadId: main thread */
    case 0x20: {                                                  /* CreateThread(ThreadParam*) */
        int id = K.next_tid++;
        words(ctx, a0, 9, extra, sizeof extra);
        if (id >= 256) { ret = (u64)(s64)-1; break; }
        K.th[id].used = 1; K.th[id].func = ps2_rd32(ctx, a0 + 4);
        K.th[id].stack = ps2_rd32(ctx, a0 + 8); K.th[id].stack_size = ps2_rd32(ctx, a0 + 12);
        K.th[id].gp = ps2_rd32(ctx, a0 + 16); K.th[id].prio = ps2_rd32(ctx, a0 + 20);
        ret = (u64)id;
        break;
    }
    case 0x40: {                                                  /* CreateSema(SemaParam*) */
        int id = 1;
        while (id < 256 && K.sema[id].used) id++;
        words(ctx, a0, 6, extra, sizeof extra);
        if (id >= 256) { ret = (u64)(s64)-1; break; }
        K.sema[id].used = 1; K.sema[id].max = (s32)ps2_rd32(ctx, a0 + 4);
        K.sema[id].init = K.sema[id].count = (s32)ps2_rd32(ctx, a0 + 8);
        ret = (u64)id;
        break;
    }
    case 0x41: if (a0 < 256) K.sema[a0].used = 0; break;          /* DeleteSema */
    case 0x42: case 0x43:                                         /* (i)SignalSema */
        if (a0 >= 256 || !K.sema[a0].used) { ret = (u64)(s64)-1; break; }
        if (K.sema[a0].count < K.sema[a0].max) K.sema[a0].count++;
        break;
    case 0x45: case 0x46:                                         /* (i)PollSema */
        if (a0 >= 256 || !K.sema[a0].used || K.sema[a0].count <= 0) ret = (u64)(s64)-1;
        else K.sema[a0].count--;
        break;
    case 0x44:                                                    /* WaitSema */
        if (a0 < 256 && K.sema[a0].used && K.sema[a0].count > 0) { K.sema[a0].count--; break; }
        trace("pc=0x%08x WaitSema(%u) would block", pc, a0);
        fatal(6, "WaitSema(%u) would block at pc 0x%08x: a thread scheduler is needed", a0, pc);
        break;
    case 0x22:                                                    /* StartThread */
        trace("pc=0x%08x StartThread(%u): func=0x%08x stack=0x%08x size=0x%x gp=0x%08x prio=%u",
              pc, a0, a0 < 256 ? K.th[a0].func : 0, a0 < 256 ? K.th[a0].stack : 0,
              a0 < 256 ? K.th[a0].stack_size : 0, a0 < 256 ? K.th[a0].gp : 0,
              a0 < 256 ? K.th[a0].prio : 0);
        fatal(6, "StartThread(%u) needs the guest thread scheduler (not implemented)", a0);
        break;
    case 0x32: case 0x33: case 0x34:                              /* Sleep/Wakeup */
        fatal(6, "%s needs the guest thread scheduler (not implemented)", name);
        break;
    case 0x04:
        trace("pc=0x%08x Exit(%d)", pc, (int)a0);
        fprintf(stderr, "ps2rt: guest called Exit(%d)\n", (int)a0);
        exit(0);
    case 0x23: case 0x24:
        trace("pc=0x%08x %s from the main thread: guest finished", pc, name);
        fprintf(stderr, "ps2rt: main thread called %s: guest finished\n", name);
        exit(0);
    case 0x06: case 0x07: case 0x7B:
        fatal(6, "guest wants to load another executable (%s)", name);
        break;
    case 0x76: ret = (u64)(s64)-1; break;                         /* SifDmaStat: "completed" */
    case 0x77: case 0x78:                                         /* SIF: IOP not modelled */
        fatal(6, "%s: the IOP/SIF is not modelled yet", name);
        break;
    default: known = 0; break;
    }

    if (!known) {
        trace("pc=0x%08x UNMODELLED syscall %d (0x%x) a0=%08x a1=%08x a2=%08x a3=%08x%s",
              pc, raw, (unsigned)n, a0, a1, a2, a3, name ? " [documented, not implemented]" : "");
        if (!getenv("PS2_LENIENT"))
            fatal(5, "unmodelled syscall %d (0x%x%s%s) at pc 0x%08x; set PS2_LENIENT=1 to "
                  "continue with a 0 result", raw, (unsigned)n, name ? " " : "", name ? name : "", pc);
    } else {
        trace("pc=0x%08x %s(%d) a0=%08x a1=%08x a2=%08x a3=%08x a4=%08x -> %llx %s", pc,
              name ? name : "?", raw, a0, a1, a2, a3, a4, (unsigned long long)ret, extra);
    }
    G(2) = ret64 ? ret : SX32(ret);
}

/* ---------------------------------------------------------- memory / loader */
#define HW_PAGES 64
static struct { u32 page; u8 *mem; } hw[HW_PAGES];
static int hw_n;

u8 *ps2_hw_access(Ctx *ctx, u32 addr) {
    int i;
    u32 page = addr >> 12;
    (void)ctx;
    if (addr == g_hw_last) {
        if (++g_hw_count > HW_LIMIT)
            fatal(7, "stuck polling hardware near 0x%08x (unmodelled register?)", addr);
    } else { g_hw_last = addr; g_hw_count = 0; }
    for (i = 0; i < hw_n; i++)
        if (hw[i].page == page) return hw[i].mem + (addr & 0xFFF);
    if (hw_n < HW_PAGES) {              /* registers read back what was written */
        FILE *f = fopen("hw_access_log.txt", "a");
        hw[hw_n].page = page; hw[hw_n].mem = calloc(1, 4096 + 16);
        if (f) { fprintf(f, "0x%08x\n", addr); fclose(f); }
        trace("hardware/unmapped page 0x%08x first touched (addr 0x%08x)", page << 12, addr);
        return hw[hw_n++].mem + (addr & 0xFFF);
    }
    return ctx->dummy;
}

static int load_elf(Ctx *ctx, const char *path) {
    FILE *f = fopen(path, "rb");
    long n;
    u8 *buf;
    u16 phentsize, phnum, i;
    u32 phoff;
    if (!f) { perror(path); return -1; }
    fseek(f, 0, SEEK_END); n = ftell(f); fseek(f, 0, SEEK_SET);
    buf = malloc((size_t)n);
    if (!buf || fread(buf, 1, (size_t)n, f) != (size_t)n) { fclose(f); return -1; }
    fclose(f);
    memcpy(&phoff, buf + 0x1C, 4); memcpy(&phentsize, buf + 0x2A, 2); memcpy(&phnum, buf + 0x2C, 2);
    for (i = 0; i < phnum; i++) {
        u32 ph[8];                       /* type off vaddr paddr filesz memsz flags align */
        u32 dst, end;
        memcpy(ph, buf + phoff + (size_t)i * phentsize, sizeof ph);
        if (ph[0] != 1) continue;
        dst = ph[2] & 0x1FFFFFFFu;
        if ((u64)dst + ph[5] > PS2_RAM_SIZE || (u64)ph[1] + ph[4] > (u64)n) {
            fprintf(stderr, "ps2rt: segment %u does not fit\n", i); free(buf); return -1;
        }
        memcpy(ctx->ram + dst, buf + ph[1], ph[4]);
        memset(ctx->ram + dst + ph[4], 0, ph[5] - ph[4]);   /* .bss */
        end = (ph[2] + ph[5] + 15u) & ~15u;
        if (end > K.image_end) K.image_end = end;
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
    {
        FILE *f = fopen("missing_entries.txt", "a");
        if (f) { fprintf(f, "0x%08x\n", addr); fclose(f); }
    }
    trace("UNRESOLVED indirect target 0x%08x", addr);
    fprintf(stderr, "ps2rt: unresolved indirect target 0x%08x (logged to missing_entries.txt; "
                    "re-run r5900lift with --entries missing_entries.txt)\n", addr);
    exit(3);
}

void ps2_unimpl(Ctx *ctx, u32 pc, u32 word) {
    (void)ctx;
    trace("UNIMPLEMENTED instruction 0x%08x at pc 0x%08x", word, pc);
    fprintf(stderr, "ps2rt: unimplemented instruction 0x%08x at pc 0x%08x\n", word, pc);
    exit(4);
}

int main(int argc, char **argv) {
    Ctx *ctx;
    if (argc < 2) { fprintf(stderr, "usage: %s <your-game.elf>\n", argv[0]); return 1; }
    g_trace = fopen("syscall_trace.log", "w");
    ctx = calloc(1, sizeof *ctx);
    ctx->ram = calloc(1, PS2_RAM_SIZE);
    if (!ctx->ram || load_elf(ctx, argv[1]) != 0) return 1;
    G(29) = 0x01FFFF00u;                    /* initial $sp (the kernel resets it via InitMainThread) */
    trace("start: entry 0x%08x, image end 0x%08x", ps2_entry_addr, K.image_end);
    ps2_dispatch(ctx, ps2_entry_addr);
    fprintf(stderr, "ps2rt: entry function returned\n");
    return 0;
}
