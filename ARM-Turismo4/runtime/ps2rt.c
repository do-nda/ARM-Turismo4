#ifdef __ANDROID__
#include <android/log.h>
#include <pthread.h>
#define PS2_ANDROID_LOGE(...) __android_log_print(ANDROID_LOG_ERROR, "GT4Recomp", __VA_ARGS__)
#define PS2_ANDROID_LOGI(...) __android_log_print(ANDROID_LOG_INFO,  "GT4Recomp", __VA_ARGS__)
#define PS2_ANDROID_VLOGE(fmt, ap) __android_log_vprint(ANDROID_LOG_ERROR, "GT4Recomp", (fmt), (ap))
#define PS2_GUEST_STOP(code) do { PS2_ANDROID_LOGI("Guest stopped (code=%d)", (int)(code)); pthread_exit(NULL); } while (0)
#else
#define PS2_ANDROID_LOGE(...) ((void)0)
#define PS2_ANDROID_LOGI(...) ((void)0)
#define PS2_ANDROID_VLOGE(fmt, ap) ((void)0)
#define PS2_GUEST_STOP(code) exit(code)
#endif
/* ps2rt.c - prototype runtime: loads YOUR ELF into emulated RAM and runs the recompiled
 * entry function, with a minimal traced kernel layer and a guest thread scheduler.
 *
 * Syscall numbers and semantics follow the community BIOS reference (ps2tek, "BIOS EE
 * Syscalls"). Struct layouts for thread/semaphore parameters are from memory of the
 * ps2sdk headers: the trace prints the raw words so they can be checked.
 *
 * Guest threads are host threads, but only ONE runs at a time: control is handed over
 * explicitly (priority scheduling, no time slicing, like the EE kernel). If every thread is
 * blocked, a virtual VBlank interrupt is delivered to the game's registered handlers; if
 * nothing can wake a thread the run stops with a deadlock report.
 *
 * Everything is traced to syscall_trace.log. Anything unmodelled stops the run with exit code
 * 5/6/7. Set PS2_LENIENT=1 to let unknown syscalls return 0 instead of stopping. */
#define _POSIX_C_SOURCE 200809L
#include "ps2rt.h"
extern void ps2_sif_capture_dma(Ctx *ctx, u32 xfer_addr, u32 count);
extern int ps2_sif_handle_rpc_call(Ctx *ctx, u32 sid, u32 fno, u32 w9, u32 w10, u32 recv_buf, u32 recv_size);
extern void ps2_hw_on_access(Ctx *ctx, u32 addr, u8 *ptr);

#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#if defined(__GLIBC__)
#include <execinfo.h>
#include <signal.h>
#include <unistd.h>
#define PS2_WATCHDOG 1
#endif

/* ------------------------------------------------------------------ tracing */
static FILE *g_trace;
static unsigned long g_trace_lines, g_syscalls, g_hw_count;
static u32 g_hw_last;
#define TRACE_LIMIT   20000UL
#define SYSCALL_LIMIT 3000000UL
#define HW_LIMIT      20000000UL

static void trace(const char *fmt, ...) {
    va_list ap;
    if (!g_trace || g_trace_lines++ >= TRACE_LIMIT) return;
    va_start(ap, fmt); vfprintf(g_trace, fmt, ap); va_end(ap);
    fputc('\n', g_trace); fflush(g_trace);
}

static void fatal(int code, const char *fmt, ...) {
    va_list ap;
    fprintf(stderr, "ps2rt: ");
    va_start(ap, fmt); vfprintf(stderr, fmt, ap); va_end(ap); va_start(ap, fmt); PS2_ANDROID_VLOGE(fmt, ap); va_end(ap);
    fprintf(stderr, "\n(see syscall_trace.log for the sequence that led here)\n");
    if (g_trace) {
        fprintf(g_trace, "FATAL: ");
        va_start(ap, fmt); vfprintf(g_trace, fmt, ap); va_end(ap);
        fputc('\n', g_trace); fflush(g_trace);
    }
    PS2_GUEST_STOP(code);
}

#ifdef PS2_WATCHDOG
/* If no syscall happens for PS2_WATCHDOG seconds (default 20) the guest is almost certainly
 * spinning on something unmodelled: print the host call stack (guest functions are named
 * f_<address>) and exit with code 8. Needs -rdynamic for names; PS2_WATCHDOG=0 disables. */
static unsigned long g_wd_last = ~0UL;
static unsigned g_wd_secs = 20;
static void on_alarm(int sig) {
    static const char msg[] = "\nps2rt: no syscall for a while - the guest is probably spinning "
        "on something unmodelled.\nGuest call stack, innermost first "
        "(f_<guest address>):\n";
    void *bt[24];
    int n;
    (void)sig;
    if (g_syscalls == g_wd_last) {
        n = backtrace(bt, 24);
        if (write(2, msg, sizeof msg - 1) < 0) _exit(8);
        backtrace_symbols_fd(bt, n, 2);
        _exit(8);
    }
    g_wd_last = g_syscalls;
    alarm(g_wd_secs);
}
#endif

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
    N(0x46, "iPollSema") N(0x47, "ReferSemaStatus") N(0x48, "iReferSemaStatus")
    N(0x4A, "SetOsdConfigParam?") N(0x4B, "GetOsdConfigParam?")
    N(0x4C, "SetOsdConfigParam2_old") N(0x4D, "GetOsdConfigParam2_old")
    N(0x64, "FlushCache") N(0x6B, "GetGsVParam") N(0x6C, "SetGsHParam") N(0x6D, "GetGsHParam")
    N(0x6E, "SetOsdConfigParam2") N(0x6F, "GetOsdConfigParam2")
    N(0x70, "GsGetIMR") N(0x71, "GsPutIMR") N(0x72, "SetPgifHandler")
    N(0x73, "SetVSyncFlag") N(0x74, "SetSyscall") N(0x75, "SifStopDma") N(0x76, "SifDmaStat")
    N(0x77, "SifSetDma") N(0x78, "SifSetDChain") N(0x79, "SifSetReg")
    N(0x7A, "SifGetReg") N(0x7B, "ExecOSD") N(0x7C, "Deci2Call") N(0x7D, "PSMode")
    N(0x7E, "MachineType") N(0x7F, "GetMemorySize") N(0x80, "_GetGsDxDyOffset")
    N(0x82, "_InitTLB") N(0x83, "FindAddress") N(0x85, "SetMemoryMode")
    N(0x86, "GetMemoryMode") N(0x87, "ExecPSX")
#undef N
    default: return NULL;
    }
}

/* ------------------------------------------------------------- kernel state */
/* Fake kernel syscall table, visible to the guest (kernel addresses map onto low RAM).
 * SetSyscall(n, fn) stores fn at KSYSTAB + 4*n; dispatch reads handlers back from here.
 * The game's startup code scans kernel memory for these words to find and patch the slots,
 * so the table must really exist in guest memory. The base address is a guess. */
#define KSYSTAB       0x80002000u
#define IRQ_STACK_TOP 0x00070000u  /* stack used for interrupt handlers (kernel area) */

typedef struct { u32 cause, func, arg; int id, dmac, live; } Handler;
static struct {
    u32 sp, heap_base, heap_end, gp, image_lo, image_end;
    u32 intc_mask, dmac_mask, vsync_flag, vsync_csr, sif0_buf;
    u32 sif_regs[32];
    u64 gs_imr;
    u32 osd;  /* console OSD config word (language, aspect, ...) */
    u8 osd2[64];
    u32 mem_mode;
    Handler h[64];
    int nh, next_hid;
} K = { .image_lo = 0xFFFFFFFFu, .next_hid = 1 };

enum { TS_FREE, TS_DORMANT, TS_READY, TS_RUN, TS_WAIT };
typedef struct Thr {
    int id, st, wait_sema, wait_sleep;
    s32 prio, init_prio;
    u32 func, stack, stack_size, gp, attr, option, arg;
    unsigned long seq, wseq;
    int wakeups, started;
    Ctx *ctx;
    pthread_t th;
    pthread_cond_t cv;
} Thr;
typedef struct { int used; s32 count, max, init; } Sema;

static Thr T[256];      /* index = thread id; 1 = main thread */
static Sema S[256];
static Thr *cur;        /* the one running guest thread */
static unsigned long seqctr = 1, idle_spins, vblanks;
static int in_irq;
static pthread_mutex_t mx = PTHREAD_MUTEX_INITIALIZER;

static void words(Ctx *c, u32 a, int n, char *out, size_t cap) {
    int i; size_t k = 0;
    out[0] = 0;
    for (i = 0; i < n && k + 12 < cap; i++)
        k += (size_t)snprintf(out + k, cap - k, "%08x ", ps2_rd32(c, a + 4u * (u32)i));
}

static void zero_guest(Ctx *c, u32 addr, u32 len) {
    u32 k;
    for (k = 0; k + 4u <= len; k += 4u)
        ps2_wr32(c, addr + k, 0);
    if (k < len) {
        u32 rem = len - k;
        u32 mask = ~0u << (rem * 8u);
        ps2_wr32(c, addr + k, ps2_rd32(c, addr + k) & mask);
    }
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

static int fire_dmac_handlers(Ctx *ctx, u32 channel) {
    static int reentrancy = 0;
    int i, fired = 0, saved_irq;
    if (reentrancy) return 0;
    if (!((K.dmac_mask >> (channel & 31u)) & 1u)) return 0;
    reentrancy = 1;
    saved_irq = in_irq;
    in_irq = 1;
    for (i = 0; i < K.nh; i++) {
        Ctx hc;
        if (!K.h[i].live || !K.h[i].dmac || K.h[i].cause != channel) continue;
        hc = *ctx;
        hc.r[4].d[0] = (u64)channel;
        hc.r[5].d[0] = K.h[i].arg;
        hc.r[6].d[0] = 0;
        hc.r[29].d[0] = IRQ_STACK_TOP - 16u;
        trace("dmac: firing channel %u handler 0x%08x (arg=0x%08x)", channel, K.h[i].func, K.h[i].arg);
        ps2_dispatch(&hc, K.h[i].func);
        fired++;
    }
    in_irq = saved_irq;
    reentrancy = 0;
    return fired;
}

static struct { u32 client, sid; } g_rpc_clients[64];
static int g_rpc_client_n;

static void record_rpc_client(u32 client, u32 sid) {
    int i;
    u32 key = client & 0x1FFFFFFFu;
    for (i = 0; i < g_rpc_client_n; i++) {
        if (g_rpc_clients[i].client == key) { g_rpc_clients[i].sid = sid; return; }
    }
    if (g_rpc_client_n < 64) {
        g_rpc_clients[g_rpc_client_n].client = key;
        g_rpc_clients[g_rpc_client_n].sid = sid;
        g_rpc_client_n++;
    }
}

static u32 lookup_rpc_sid(u32 client) {
    int i;
    u32 key = client & 0x1FFFFFFFu;
    for (i = 0; i < g_rpc_client_n; i++)
        if (g_rpc_clients[i].client == key) return g_rpc_clients[i].sid;
    return 0;
}

static void handle_sif_dma(Ctx *ctx, u32 sdd_list, u32 count, int *woke_any) {
    static u32 iop_heap_ptr = 0x00180000u;
    u32 i;
    if (!sdd_list || count == 0 || count > 64u) return;
    for (i = 0; i < count; i++) {
        u32 ent  = sdd_list + 16u * i;
        u32 src  = ps2_rd32(ctx, ent + 0);
        u32 dst  = ps2_rd32(ctx, ent + 4);
        u32 size = ps2_rd32(ctx, ent + 8);
        u32 w0, daddr, fcode, opt;
        if (!src || size < 16u) continue;
        /* SifCmdHeader_t: +0x00 psize:8|dsize:24, +0x04 daddr, +0x08 fcode, +0x0C opt */
        w0    = ps2_rd32(ctx, src + 0x00u);
        daddr = ps2_rd32(ctx, src + 0x04u);
        fcode = ps2_rd32(ctx, src + 0x08u);
        opt   = ps2_rd32(ctx, src + 0x0Cu);

        if (fcode == 0x80000000u) { /* SIF_CMD_CHANGE_SADDR */
            u32 saddr = (size >= 20u) ? ps2_rd32(ctx, src + 0x10u) : 0;
            if (!saddr) saddr = opt;
            K.sif0_buf = saddr;
            trace("sif: EE SIF0 receive buffer set to 0x%08x (dst=0x%08x)", saddr, dst);
        } else if (fcode == 0x80000009u || fcode == 0x8000000Au || fcode == 0x8000000Cu) {
            /* SifRpcPktHeader_t: +0x10 rec_id, +0x14 pkt_addr, +0x18 rpc_id, +0x1C client */
            u32 rec_id   = (size >= 0x14u) ? ps2_rd32(ctx, src + 0x10u) : 0;
            u32 pkt_addr = (size >= 0x18u) ? ps2_rd32(ctx, src + 0x14u) : 0;
            u32 rpc_id   = (size >= 0x1Cu) ? ps2_rd32(ctx, src + 0x18u) : 0;
            u32 client   = (size >= 0x20u) ? ps2_rd32(ctx, src + 0x1Cu) : 0;
            u32 w8       = (size >= 0x24u) ? ps2_rd32(ctx, src + 0x20u) : 0;
            u32 w9       = (size >= 0x28u) ? ps2_rd32(ctx, src + 0x24u) : 0;
            u32 w10      = (size >= 0x2Cu) ? ps2_rd32(ctx, src + 0x28u) : 0;
            u32 w11      = (size >= 0x30u) ? ps2_rd32(ctx, src + 0x2Cu) : 0;
            u32 w12      = (size >= 0x34u) ? ps2_rd32(ctx, src + 0x30u) : 0;

            if (fcode == 0x80000009u) {
                record_rpc_client(client, w8);
                trace("sif-rpc: BIND sid=0x%08x client=0x%08x", w8, client);
            } else if (fcode == 0x8000000Au) {
                u32 sid = lookup_rpc_sid(client);
                trace("sif-rpc: CALL sid=0x%08x fn=%u recv=0x%08x rsize=0x%x client=0x%08x",
                      sid, w8, w11, w12, client);
                if (w11 && w12 > 0 && w12 <= 0x200000u) {
                    zero_guest(ctx, w11, w12);
                    if (w12 >= 4u) {
                        if (sid == 0x80000003u && w8 == 1u) { /* SifAllocIopHeap */
                            ps2_wr32(ctx, w11, iop_heap_ptr);
                            iop_heap_ptr += 0x00040000u;
                        } else if (sid == 0x80000006u || sid == 0x80000001u ||
                                   sid == 0x80000592u || sid == 0x80000593u || sid == 0x80000595u) {
                            ps2_wr32(ctx, w11, 1u);
                        }
                    }
                }
            } else {
                trace("sif-rpc: RDATA iop_src=0x%08x ee_dst=0x%08x size=0x%x client=0x%08x",
                      w8, w9, w10, client);
                if (w9 && w10 > 0 && w10 <= 0x200000u)
                    zero_guest(ctx, w9, w10);
            }

            if (K.sif0_buf) {
                /* Write SifRpcRendPkt_t into EE's SIF0 receive buffer and fire SIF0 (DMAC ch 5) */
                ps2_wr32(ctx, K.sif0_buf + 0x00u, 0x40u);        /* psize=0x40, dsize=0 */
                ps2_wr32(ctx, K.sif0_buf + 0x04u, 0u);           /* daddr */
                ps2_wr32(ctx, K.sif0_buf + 0x08u, 0x80000008u);  /* SIF_CMD_RPC_END */
                ps2_wr32(ctx, K.sif0_buf + 0x0Cu, 0u);           /* opt */
                ps2_wr32(ctx, K.sif0_buf + 0x10u, rec_id);
                ps2_wr32(ctx, K.sif0_buf + 0x14u, pkt_addr);
                ps2_wr32(ctx, K.sif0_buf + 0x18u, rpc_id);
                ps2_wr32(ctx, K.sif0_buf + 0x1Cu, client);
                ps2_wr32(ctx, K.sif0_buf + 0x20u, fcode);        /* cid: original command */
                ps2_wr32(ctx, K.sif0_buf + 0x24u, 0x00123450u);  /* fake IOP server ptr */
                ps2_wr32(ctx, K.sif0_buf + 0x28u, 0x00123500u);  /* fake IOP buff ptr */
                ps2_wr32(ctx, K.sif0_buf + 0x2Cu, 0x00123600u);  /* fake IOP cbuff ptr */
                ps2_wr32(ctx, K.sif0_buf + 0x30u, 0u);
                ps2_wr32(ctx, K.sif0_buf + 0x34u, 0u);
                ps2_wr32(ctx, K.sif0_buf + 0x38u, 0u);
                ps2_wr32(ctx, K.sif0_buf + 0x3Cu, 0u);
                if (fire_dmac_handlers(ctx, 5) > 0 && woke_any)
                    *woke_any = 1;
            }
        } else if (fcode & 0x80000000u) {
            trace("sif-cmd: fcode=0x%08x psize=0x%x dsize=0x%x daddr=0x%08x dst=0x%08x",
                  fcode, w0 & 0xFFu, w0 >> 8, daddr, dst);
        }
    }
}

/* ---------------------------------------------------------------- scheduler */
static const char *st_name(int st) {
    switch (st) {
    case TS_DORMANT: return "dormant"; case TS_READY: return "ready";
    case TS_RUN:     return "running"; case TS_WAIT:  return "waiting";
    default:         return "free";
    }
}

static void deadlock(const char *why) {
    char buf[1024];
    size_t k = 0;
    int i;
    buf[0] = 0;
    for (i = 1; i < 256 && k + 80 < sizeof buf; i++)
        if (T[i].st != TS_FREE && T[i].st != TS_DORMANT)
            k += (size_t)snprintf(buf + k, sizeof buf - k, " [thread %d (func 0x%08x) %s%s%d]",
                i, T[i].func, st_name(T[i].st),
                T[i].st == TS_WAIT ? (T[i].wait_sema ? " on sema " : " in sleep ") : " ",
                T[i].st == TS_WAIT && T[i].wait_sema ? T[i].wait_sema : 0);
    fatal(6, "deadlock: %s.%s", why, buf);
}

static Thr *pick(void) { /* highest priority (lowest number), then FIFO */
    Thr *b = NULL;
    int i;
    for (i = 1; i < 256; i++) {
        Thr *t = &T[i];
        if (t->st != TS_READY && t->st != TS_RUN) continue;
        if (!b || t->prio < b->prio || (t->prio == b->prio && t->seq < b->seq)) b = t;
    }
    return b;
}

static void make_ready(Thr *t) { t->st = TS_READY; t->seq = ++seqctr; t->wait_sema = t->wait_sleep = 0; }

static void thread_exit(Thr *t, int del);

static void *thread_main(void *p) {
    Thr *t = (Thr *)p;
    pthread_mutex_lock(&mx);
    while (cur != t) pthread_cond_wait(&t->cv, &mx);
    pthread_mutex_unlock(&mx);
    ps2_dispatch(t->ctx, t->func);  /* returns when the guest thread function returns */
    trace("thread %d: entry function returned (implicit ExitThread)", t->id);
    thread_exit(t, 0);
    return NULL;
}

static void run_thread(Thr *n) { /* called with mx held, cur already set to n */
    if (!n->started) {
        pthread_attr_t at;
        pthread_attr_init(&at);
        pthread_attr_setdetachstate(&at, PTHREAD_CREATE_DETACHED);
        pthread_attr_setstacksize(&at, 16u * 1024u * 1024u);
        n->started = 1;
        if (pthread_create(&n->th, &at, thread_main, n) != 0)
            fatal(7, "cannot create a host thread for guest thread %d", n->id);
        pthread_attr_destroy(&at);
    } else {
        pthread_cond_signal(&n->cv);
    }
}

static void switch_to(Thr *self, Thr *n) {
    if (n == self) { self->st = TS_RUN; return; }
    if (self->st == TS_RUN) self->st = TS_READY; /* preempted: keeps its queue position */
    trace("sched: thread %d -> thread %d", self->id, n->id);
    pthread_mutex_lock(&mx);
    n->st = TS_RUN; cur = n;
    run_thread(n);
    while (cur != self) pthread_cond_wait(&self->cv, &mx);
    pthread_mutex_unlock(&mx);
    self->st = TS_RUN;
}

/* Every thread is blocked: deliver a virtual VBlank (INTC causes 2 and 3) to the game's
 * handlers. They normally signal a semaphore or wake a thread. */
static void idle(Thr *self) {
    int i, fired = 0, cause;
    if (++idle_spins > 200000UL)
        deadlock("no thread ever became runnable (events from the IOP or hardware are not modelled)");
    in_irq = 1;
    for (cause = 0; cause < 32; cause++) {
        u32 t_base = 0, saved_mode = 0;
        if (cause != 2 && cause != 3 && !((K.intc_mask >> cause) & 1u)) continue;
        /* EE Timers 0..3 correspond to INTC causes 9..12 (base 0x10000000 + 0x800 * idx).
         * Pulse EQUF (0x400) and OVFF (0x800) so libkernel SetAlarm handlers advance and fire. */
        if (cause >= 9 && cause <= 12) {
            u32 comp;
            t_base = 0x10000000u + 0x800u * (u32)(cause - 9);
            saved_mode = ps2_rd32(self->ctx, t_base + 0x10u);
            comp = ps2_rd32(self->ctx, t_base + 0x20u);
            ps2_wr32(self->ctx, t_base + 0x00u, comp);
            ps2_wr32(self->ctx, t_base + 0x10u, saved_mode | 0xC00u);
        }
        for (i = 0; i < K.nh; i++) {
            Ctx hc;
            if (!K.h[i].live || K.h[i].dmac || K.h[i].cause != (u32)cause) continue;
            hc = *self->ctx;
            hc.r[4].d[0] = (u64)cause;
            hc.r[5].d[0] = K.h[i].arg;
            hc.r[6].d[0] = 0;
            hc.r[29].d[0] = IRQ_STACK_TOP - 16u;
            ps2_dispatch(&hc, K.h[i].func);
            fired++;
        }
        if (t_base) {
            u32 cur_mode = ps2_rd32(self->ctx, t_base + 0x10u);
            ps2_wr32(self->ctx, t_base + 0x10u, cur_mode & ~0xC00u);
        }
    }
    in_irq = 0;
    if (K.vsync_flag) ps2_wr32(self->ctx, K.vsync_flag, 1);
    vblanks++;
    if (!fired)
        deadlock("every thread is blocked and no enabled INTC handler is registered");
    if (vblanks <= 3)
        trace("idle: virtual IRQ tick #%lu delivered to %d handler(s)", vblanks, fired);
}

static void reschedule(Thr *self) {
    Thr *n;
    for (;;) { n = pick(); if (n) break; idle(self); }
    idle_spins = 0;
    switch_to(self, n);
}

static void preempt(Thr *self) { /* after making another thread ready */
    Thr *n;
    if (in_irq) return;
    n = pick();
    if (n && n != self) switch_to(self, n);
}

static void thread_exit(Thr *t, int del) {
    Thr *n;
    if (t->id == 1) {
        trace("main thread exited: guest finished");
        PS2_ANDROID_LOGI("main thread exited: guest finished");
        PS2_GUEST_STOP(0);
    }
    t->st = del ? TS_FREE : TS_DORMANT;
    t->started = 0;
    for (;;) { n = pick(); if (n) break; idle(t); }
    idle_spins = 0;
    trace("sched: thread %d exited -> thread %d", t->id, n->id);
    pthread_mutex_lock(&mx);
    n->st = TS_RUN; cur = n;
    run_thread(n);
    pthread_mutex_unlock(&mx);
    pthread_exit(NULL);
}

static int th_status(Thr *t) {
    switch (t->st) {
    case TS_RUN: return 0x01; case TS_READY: return 0x02;
    case TS_WAIT: return 0x04; default: return 0x10;
    }
}

/* ------------------------------------------------------------------ syscalls */
void ps2_syscall(Ctx *ctx, u32 pc) {
    s32 raw = (s32)G(3), n = raw < 0 ? -raw : raw;  /* negative = interrupt-safe variant */
    u32 a0 = (u32)G(4), a1 = (u32)G(5), a2 = (u32)G(6), a3 = (u32)G(7), a4 = (u32)G(8);
    u64 ret = 0;
    int ret64 = 0, known = 1;
    const char *name;
    char extra[160] = "";
    Thr *self = (Thr *)ctx->kthread;
    Thr *t;

    if (raw == 0x7000) { printf("%d\n", (int)G(4)); fflush(stdout); return; } /* self-test only */
    if (raw == 0x7001) { fflush(stdout); exit(0); } /* self-test only */
    if (++g_syscalls > SYSCALL_LIMIT)
        fatal(7, "over %lu syscalls: probably spinning on something unmodelled", SYSCALL_LIMIT);
    name = sys_name(n);

    {
        u32 h = n < 256 ? ps2_rd32(ctx, KSYSTAB + 4u * (u32)n) : 0;
        if (n != 0x74 && h) {  /* the game replaced this syscall: run its code */
            trace("pc=0x%08x %s(%d) a0=%08x a1=%08x a2=%08x a3=%08x -> guest handler 0x%08x",
                  pc, name ? name : "?", raw, a0, a1, a2, a3, h);
            ps2_dispatch(ctx, h);
            trace("  guest handler returned v0=%llx", (unsigned long long)G(2));
            return;
        }
    }

    switch (n) {
    case 0x3C: /* InitMainThread(gp, stack, stack_size, args, root) -> sp */
        K.gp = a0;
        K.sp = (a1 == 0xFFFFFFFFu) ? PS2_RAM_SIZE - a2 : a1 + a2;
        T[1].gp = a0; T[1].stack = a1; T[1].stack_size = a2;
        ret = K.sp;
        break;
    case 0x3D: /* InitHeap(heap, size) -> end of heap */
        K.heap_base = K.image_end;
        K.heap_end = (a0 == 0xFFFFFFFFu) ? K.sp : a0 + a1;
        if (a0 != 0xFFFFFFFFu) K.heap_base = a0;
        ret = K.heap_end;
        break;
    case 0x3E: ret = K.heap_base; break;           /* EndOfHeap (per reference) */
    case 0x64: break;                              /* FlushCache */
    case 0x6B: case 0x6C: case 0x6D: ret = 0; break; /* GetGsVParam, SetGsHParam, GetGsHParam */
    case 0x72: ret = 0; break;                     /* SetPgifHandler */
    case 0x7C: ret = (u64)(s64)-1; break;          /* Deci2Call: -1 = no DECI2 debug host */
    case 0x7D: ret = 0; break;                     /* PSMode: 0 = PS2 */
    case 0x7F: ret = PS2_RAM_SIZE; break;          /* GetMemorySize */
    case 0x7E: ret = 0; break;                     /* MachineType */
    case 0x70: ret = K.gs_imr; ret64 = 1; break;   /* GsGetIMR */
    case 0x71: K.gs_imr = ((u64)a1 << 32) | a0; break; /* GsPutIMR (64-bit arg) */
    case 0x73: K.vsync_flag = a0; K.vsync_csr = a1; break; /* SetVSyncFlag */
    case 0x74: /* SetSyscall: store in the fake table */
        if (a0 < 256) ps2_wr32(ctx, KSYSTAB + 4u * a0, a1);
        break;
    case 0x01: case 0x02: break;                   /* ResetEE, SetGsCrt */
    case 0x10: case 0x12: /* Add{Intc,Dmac}Handler */
        ret = add_handler(a0, a1, a3, n == 0x12); break;
    case 0x11: case 0x13: ret = remove_handler(a0, (int)a1, n == 0x13); break;
    case 0x14: ret = !((K.intc_mask >> (a0 & 31)) & 1); K.intc_mask |= 1u << (a0 & 31); break;
    case 0x15: ret = (K.intc_mask >> (a0 & 31)) & 1; K.intc_mask &= ~(1u << (a0 & 31)); break;
    case 0x16: ret = !((K.dmac_mask >> (a0 & 31)) & 1); K.dmac_mask |= 1u << (a0 & 31); break;
    case 0x17: ret = (K.dmac_mask >> (a0 & 31)) & 1; K.dmac_mask &= ~(1u << (a0 & 31)); break;

    /* ---- threads ---- */
    case 0x2F: ret = (u64)self->id; break;         /* GetThreadId */
    case 0x20: { /* CreateThread(ThreadParam*) */
        int id = 2;
        words(ctx, a0, 9, extra, sizeof extra);
        while (id < 256 && T[id].st != TS_FREE) id++;
        if (id >= 256) { ret = (u64)(s64)-1; break; }
        memset(&T[id], 0, sizeof T[id]);
        pthread_cond_init(&T[id].cv, NULL);
        T[id].id = id; T[id].st = TS_DORMANT;
        T[id].func = ps2_rd32(ctx, a0 + 4); T[id].stack = ps2_rd32(ctx, a0 + 8);
        T[id].stack_size = ps2_rd32(ctx, a0 + 12); T[id].gp = ps2_rd32(ctx, a0 + 16);
        T[id].init_prio = T[id].prio = (s32)ps2_rd32(ctx, a0 + 20);
        T[id].attr = ps2_rd32(ctx, a0 + 28); T[id].option = ps2_rd32(ctx, a0 + 32);
        ret = (u64)id;
        break;
    }
    case 0x21: /* DeleteThread */
        if (a0 >= 2 && a0 < 256 && T[a0].st == TS_DORMANT) { T[a0].st = TS_FREE; }
        else ret = (u64)(s64)-1;
        break;
    case 0x22: { /* StartThread(id, arg) */
        Ctx *c;
        if (a0 < 2 || a0 >= 256 || T[a0].st != TS_DORMANT) { ret = (u64)(s64)-1; break; }
        t = &T[a0];
        c = calloc(1, sizeof *c);
        c->ram = ctx->ram; c->spr = ctx->spr; c->kthread = t;
        c->r[4].d[0] = a1; c->r[28].d[0] = t->gp;
        c->r[29].d[0] = (u64)(t->stack + t->stack_size - 16u);
        t->ctx = c; t->arg = a1; t->started = 0; t->wakeups = 0; t->prio = t->init_prio;
        make_ready(t);
        trace("pc=0x%08x StartThread(%u): func=0x%08x stack=0x%08x size=0x%x gp=0x%08x prio=%d arg=0x%x",
              pc, a0, t->func, t->stack, t->stack_size, t->gp, t->prio, a1);
        preempt(self);
        break;
    }
    case 0x23: case 0x24: /* ExitThread, ExitDeleteThread */
        trace("pc=0x%08x %s from thread %d", pc, name, self->id);
        thread_exit(self, n == 0x24);
        break;
    case 0x25: case 0x26: /* TerminateThread */
        if (a0 >= 2 && a0 < 256 && T[a0].st != TS_FREE && T[a0].st != TS_DORMANT && &T[a0] != self) {
            T[a0].st = TS_DORMANT; T[a0].started = 0; ret = a0;
        } else ret = (u64)(s64)-1;
        break;
    case 0x29: case 0x2A: /* (i)ChangeThreadPriority(id, prio) */
        t = a0 ? (a0 < 256 ? &T[a0] : NULL) : self;
        if (!t || t->st == TS_FREE) { ret = (u64)(s64)-1; break; }
        ret = (u64)(u32)t->prio;
        t->prio = (s32)a1;
        if (t->st == TS_READY || t->st == TS_RUN) preempt(self);
        break;
    case 0x2B: case 0x2C: { /* (i)RotateThreadReadyQueue(prio) */
        s32 pr = a0 ? (s32)a0 : self->prio;
        Thr *front = NULL;
        int i;
        for (i = 1; i < 256; i++)
            if ((T[i].st == TS_READY || T[i].st == TS_RUN) && T[i].prio == pr &&
                (!front || T[i].seq < front->seq)) front = &T[i];
        if (front) front->seq = ++seqctr;
        ret = (u64)(u32)pr;
        preempt(self);
        break;
    }
    case 0x2D: case 0x2E: /* (i)ReleaseWaitThread */
        if (a0 >= 1 && a0 < 256 && T[a0].st == TS_WAIT) { make_ready(&T[a0]); preempt(self); }
        else ret = (u64)(s64)-1;
        break;
    case 0x30: case 0x31: /* (i)ReferThreadStatus(id, ptr) */
        t = a0 ? (a0 < 256 ? &T[a0] : NULL) : self;
        if (!t || t->st == TS_FREE) { ret = (u64)(s64)-1; break; }
        if (a1) {
            ps2_wr32(ctx, a1 + 0,  (u32)th_status(t)); ps2_wr32(ctx, a1 + 4,  t->func);
            ps2_wr32(ctx, a1 + 8,  t->stack);          ps2_wr32(ctx, a1 + 12, t->stack_size);
            ps2_wr32(ctx, a1 + 16, t->gp);             ps2_wr32(ctx, a1 + 20, (u32)t->init_prio);
            ps2_wr32(ctx, a1 + 24, (u32)t->prio);      ps2_wr32(ctx, a1 + 28, t->attr);
            ps2_wr32(ctx, a1 + 32, t->option);
            ps2_wr32(ctx, a1 + 36, t->st == TS_WAIT ? (t->wait_sema ? 2u : 1u) : 0u);
            ps2_wr32(ctx, a1 + 40, (u32)t->wait_sema); ps2_wr32(ctx, a1 + 44, (u32)t->wakeups);
        }
        ret = (u64)th_status(t);
        break;
    case 0x32: /* SleepThread */
        if (in_irq) fatal(6, "SleepThread called from an interrupt handler");
        if (self->wakeups > 0) { self->wakeups--; break; }
        self->st = TS_WAIT; self->wait_sleep = 1; self->wait_sema = 0;
        trace("pc=0x%08x SleepThread: thread %d blocks", pc, self->id);
        reschedule(self);
        break;
    case 0x33: case 0x34: /* (i)WakeupThread */
        if (a0 < 1 || a0 >= 256 || T[a0].st == TS_FREE || T[a0].st == TS_DORMANT) { ret = (u64)(s64)-1; break; }
        t = &T[a0];
        if (t->st == TS_WAIT && t->wait_sleep) { make_ready(t); preempt(self); }
        else t->wakeups++;
        break;
    case 0x35: case 0x36: /* (i)CancelWakeupThread */
        t = a0 ? (a0 < 256 ? &T[a0] : NULL) : self;
        if (!t) { ret = (u64)(s64)-1; break; }
        ret = (u64)(u32)t->wakeups; t->wakeups = 0;
        break;
    case 0x37: case 0x38: case 0x39: case 0x3A: break; /* (i)SuspendThread, (i)ResumeThread */

    /* ---- semaphores ---- */
    case 0x40: { /* CreateSema(SemaParam*) */
        int id = 1;
        while (id < 256 && S[id].used) id++;
        words(ctx, a0, 6, extra, sizeof extra);
        if (id >= 256) { ret = (u64)(s64)-1; break; }
        S[id].used = 1; S[id].max = (s32)ps2_rd32(ctx, a0 + 4);
        S[id].init = S[id].count = (s32)ps2_rd32(ctx, a0 + 8);
        ret = (u64)id;
        break;
    }
    case 0x41: if (a0 < 256) S[a0].used = 0; break;  /* DeleteSema */
    case 0x42: case 0x43: { /* (i)SignalSema */
        Thr *w = NULL;
        int i;
        if (a0 >= 256 || !S[a0].used) { ret = (u64)(s64)-1; break; }
        for (i = 1; i < 256; i++)
            if (T[i].st == TS_WAIT && T[i].wait_sema == (int)a0 && (!w || T[i].wseq < w->wseq)) w = &T[i];
        if (w) { make_ready(w); preempt(self); }
        else if (S[a0].count < S[a0].max) S[a0].count++;
        break;
    }
    case 0x45: case 0x46: /* (i)PollSema */
        if (a0 >= 256 || !S[a0].used || S[a0].count <= 0) ret = (u64)(s64)-1;
        else S[a0].count--;
        break;
    case 0x47: case 0x48: /* (i)ReferSemaStatus(id, ee_sema_t*) */
        if (a0 >= 256 || !S[a0].used) { ret = (u64)(s64)-1; break; }
        {
            u32 waiting = 0;
            int i;
            for (i = 1; i < 256; i++)
                if (T[i].st == TS_WAIT && T[i].wait_sema == (int)a0) waiting++;
            ps2_wr32(ctx, a1 + 0,  (u32)S[a0].count); ps2_wr32(ctx, a1 + 4,  (u32)S[a0].max);
            ps2_wr32(ctx, a1 + 8,  (u32)S[a0].init);  ps2_wr32(ctx, a1 + 12, waiting);
            ps2_wr32(ctx, a1 + 16, 0);                ps2_wr32(ctx, a1 + 20, 0);
        }
        break;
    case 0x4A: K.osd = ps2_rd32(ctx, a0); break;   /* SetOsdConfigParam */
    case 0x4B:                                     /* GetOsdConfigParam */
        ps2_wr32(ctx, a0, K.osd);
        snprintf(extra, sizeof extra, "[wrote OSD config 0x%08x]", K.osd);
        break;
    case 0x4C: case 0x6E: {                        /* SetOsdConfigParam2(src, size, offset) */
        u32 i, sz = a1 > 64u ? 64u : a1, off = a2 > 64u ? 64u : a2;
        if (off + sz > 64u) sz = 64u - off;
        for (i = 0; i < sz; i++) {
            u32 w = ps2_rd32(ctx, (a0 + i) & ~3u);
            K.osd2[off + i] = (u8)(w >> (((a0 + i) & 3u) * 8u));
        }
        break;
    }
    case 0x4D: case 0x6F: {                        /* GetOsdConfigParam2(dst, size, offset) */
        u32 i, sz = a1 > 64u ? 64u : a1, off = a2 > 64u ? 64u : a2;
        if (off + sz > 64u) sz = 64u - off;
        for (i = 0; i < sz; i++) {
            u32 addr = a0 + i, aligned = addr & ~3u, sh = (addr & 3u) * 8u;
            u32 w = ps2_rd32(ctx, aligned);
            w = (w & ~(0xFFu << sh)) | ((u32)K.osd2[off + i] << sh);
            ps2_wr32(ctx, aligned, w);
        }
        break;
    }
    case 0x44: /* WaitSema */
        if (a0 >= 256 || !S[a0].used) { ret = (u64)(s64)-1; break; }
        if (S[a0].count > 0) { S[a0].count--; break; }
        if (in_irq) fatal(6, "WaitSema(%u) would block inside an interrupt handler", a0);
        self->st = TS_WAIT; self->wait_sema = (int)a0; self->wait_sleep = 0; self->wseq = ++seqctr;
        trace("pc=0x%08x WaitSema(%u): thread %d blocks", pc, a0, self->id);
        reschedule(self);
        break;

    case 0x04:
        trace("pc=0x%08x Exit(%d)", pc, (int)a0);
        PS2_ANDROID_LOGI("guest called Exit(%d)", (int)a0);
        PS2_GUEST_STOP(0);
    case 0x06: case 0x07: case 0x7B:
        fatal(6, "guest wants to load another executable (%s)", name);
        break;
    case 0x75: ret = 0; break;                     /* SifStopDma: "stopped" */
    case 0x76: ret = (u64)(s64)-1; break;          /* SifDmaStat: "completed" */
    case 0x77: {                                   /* SifSetDma: return non-zero transfer ID */
        static u32 dma_id = 1;
        int woke_any = 0;
        if (a0 && a1) {
            u32 t0 = ps2_rd32(ctx, a0 + 0), t1 = ps2_rd32(ctx, a0 + 4);
            u32 t2 = ps2_rd32(ctx, a0 + 8), t3 = ps2_rd32(ctx, a0 + 12);
            snprintf(extra, sizeof extra, "[xfer0: %08x %08x %08x %08x]", t0, t1, t2, t3);
        }
        ret = dma_id++;
        if (!dma_id) dma_id = 1;
        G(2) = SX32(ret);
        trace("pc=0x%08x %s(%d) a0=%08x a1=%08x a2=%08x a3=%08x a4=%08x -> %llx %s", pc,
              name ? name : "?", raw, a0, a1, a2, a3, a4, (unsigned long long)ret, extra);
        handle_sif_dma(ctx, a0, a1, &woke_any);
        if (woke_any) preempt(self);
        return;
    }
    case 0x78: break;                              /* SifSetDChain: no-op */
    case 0x79:                                     /* SifSetReg(reg, val) */
        K.sif_regs[a0 & 31u] = a1;
        ret = 1;
        break;
    case 0x7A:                                     /* SifGetReg(reg) */
        if (a0 == 0x80000002u)
            ret = K.sif_regs[2] ? K.sif_regs[2] : 0x00070000u;
        else if ((a0 & 0x80000000u) && K.sif_regs[a0 & 31u])
            ret = K.sif_regs[a0 & 31u];
        else
            ret = 0x00070000u;
        break;
    case 0x80: case 0x82: break;                   /* _GetGsDxDyOffset, _InitTLB */
    case 0x85: K.mem_mode = a0; break;             /* SetMemoryMode */
    case 0x86: ret = K.mem_mode; break;            /* GetMemoryMode */
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
        if (hw[i].page == page) {
            if ((addr & 0x1FFFFFFFu) == 0x1F402005u || (addr & 0x1FFFFFFFu) == 0x1F402016u)
                hw[i].mem[addr & 0xFFF] = 0x40;
            { u8 *p_hw = hw[i].mem + (addr & 0xFFF); ps2_hw_on_access(ctx, addr, p_hw); return p_hw; }
        }
    if (hw_n < HW_PAGES) { /* registers read back what was written */
        FILE *f = fopen("hw_access_log.txt", "a");
        hw[hw_n].page = page; hw[hw_n].mem = calloc(1, 4096 + 16);
        if (page == 0x1F402u || page == 0xBF402u) {
            hw[hw_n].mem[0x005] = 0x40;
            hw[hw_n].mem[0x00A] = 0x00;
            hw[hw_n].mem[0x016] = 0x40;
        }
        if (f) { fprintf(f, "0x%08x\n", addr); fclose(f); }
        trace("hardware/unmapped page 0x%08x first touched (addr 0x%08x)", page << 12, addr);
        { u8 *p_hw = hw[hw_n++].mem + (addr & 0xFFF); ps2_hw_on_access(ctx, addr, p_hw); return p_hw; }
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
        u32 ph[8]; /* type off vaddr paddr filesz memsz flags align */
        u32 dst, end;
        memcpy(ph, buf + phoff + (size_t)i * phentsize, sizeof ph);
        if (ph[0] != 1) continue;
        dst = ph[2] & 0x1FFFFFFFu;
        if ((u64)dst + ph[5] > PS2_RAM_SIZE || (u64)ph[1] + ph[4] > (u64)n) {
            fprintf(stderr, "ps2rt: segment %u does not fit\n", i); free(buf); return -1;
        }
        memcpy(ctx->ram + dst, buf + ph[1], ph[4]);
        memset(ctx->ram + dst + ph[4], 0, ph[5] - ph[4]); /* .bss */
        if (dst < K.image_lo) K.image_lo = dst;
        end = (ph[2] + ph[5] + 15u) & ~15u;
        if (end > K.image_end) K.image_end = end;
    }
    free(buf);
    return 0;
}

/* The game copies pieces of its own code into kernel memory at runtime and then calls them.
 * Those bytes are in the image, so find them there and write a line for r5900lift's
 * --blobs-file: SRC SIZE DEST. */
static void blob_hint(Ctx *ctx, u32 addr) {
    u32 key = addr & 0x1FFFFFFFu, s, n, k, nz = 0;
    if (key + 64 > PS2_RAM_SIZE || key >= K.image_lo) return; /* only kernel-area targets */
    for (k = 0; k < 32; k++) nz |= ctx->ram[key + k];
    if (!nz) return;
    for (s = K.image_lo; s + 32 <= K.image_end; s += 4) {
        FILE *f;
        if (memcmp(ctx->ram + s, ctx->ram + key, 32) != 0) continue;
        for (n = 0; key + n + 4 <= PS2_RAM_SIZE && s + n + 4 <= K.image_end && n < 0x20000u; n += 4)
            if (memcmp(ctx->ram + s + n, ctx->ram + key + n, 4) != 0) break;
        f = fopen("blobs.txt", "a");
        if (f) { fprintf(f, "0x%08x 0x%x 0x%08x\n", s, n, addr); fclose(f); }
        fprintf(stderr, "ps2rt: 0x%08x holds code the game copied from the image (0x%x bytes from "
                "0x%08x). Re-lift with --blobs-file blobs.txt\n", addr, n, s);
        trace("blob hint: --blob 0x%08x 0x%x 0x%08x", s, n, addr);
        return;
    }
}

void ps2_dispatch(Ctx *ctx, u32 addr) {
    u32 key = addr & 0x1FFFFFFFu; /* KUSEG/KSEG0/KSEG1 aliases share one function */
    u32 lo = 0, hi = ps2_func_count;
    while (lo < hi) {
        u32 m = (lo + hi) / 2;
        if (ps2_func_table[m].addr < key) lo = m + 1; else hi = m;
    }
    if (lo < ps2_func_count && ps2_func_table[lo].addr == key) {
        ps2_func_table[lo].fn(ctx);
        return;
    }
    {
        FILE *f = fopen("missing_entries.txt", "a");
        if (f) { fprintf(f, "0x%08x\n", addr); fclose(f); }
    }
    trace("UNRESOLVED indirect target 0x%08x", addr);
    blob_hint(ctx, addr);
    PS2_ANDROID_LOGE("UNRESOLVED indirect target 0x%08x", addr);
    PS2_GUEST_STOP(3);
}

static inline u32 ps2_ppac5_w(u32 w) {
    return ((w >> 3) & 0x1Fu)
         | (((w >> 11) & 0x1Fu) << 5)
         | (((w >> 19) & 0x1Fu) << 10)
         | (((w >> 31) & 0x1u) << 15);
}
static inline u64 ps2_ppac5_d(u64 d) {
    return (u64)ps2_ppac5_w((u32)d) | ((u64)ps2_ppac5_w((u32)(d >> 32)) << 32);
}
static inline u32 ps2_pext5_w(u32 w) {
    return ((w & 0x1Fu) << 3)
         | (((w >> 5) & 0x1Fu) << 11)
         | (((w >> 10) & 0x1Fu) << 19)
         | (((w >> 15) & 0x1u) << 31);
}
static inline u64 ps2_pext5_d(u64 d) {
    return (u64)ps2_pext5_w((u32)d) | ((u64)ps2_pext5_w((u32)(d >> 32)) << 32);
}

static int ps2_try_emulate_mmi(Ctx *ctx, u32 pc, u32 word) {
    u32 op = (word >> 26) & 0x3Fu;
    u32 rs = (word >> 21) & 0x1Fu;
    u32 rt = (word >> 16) & 0x1Fu;
    u32 rd = (word >> 11) & 0x1Fu;
    u32 sa = (word >> 6) & 0x1Fu;
    u32 fn = word & 0x3Fu;
    u64 s0, s1, t0, t1, d0 = 0, d1 = 0;

    if (op != 0x1Cu) return 0; /* MMI only */
    s0 = rs ? ctx->r[rs].d[0] : 0;
    s1 = rs ? ctx->r[rs].d[1] : 0;
    t0 = rt ? ctx->r[rt].d[0] : 0;
    t1 = rt ? ctx->r[rt].d[1] : 0;

    if (fn == 0x08u) { /* MMI0 */
        switch (sa) {
        case 0x1Eu: /* PEXT5 rd, rt */
            d0 = ps2_pext5_d(t0);
            d1 = ps2_pext5_d(t1);
            break;
        case 0x1Fu: /* PPAC5 rd, rt */
            d0 = ps2_ppac5_d(t0);
            d1 = ps2_ppac5_d(t1);
            break;
        default:
            return 0;
        }
    } else if (fn == 0x09u) { /* MMI2 */
        switch (sa) {
        case 0x08u: { /* PADDW rd, rs, rt */
            u32 w0 = (u32)s0 + (u32)t0;
            u32 w1 = (u32)(s0 >> 32) + (u32)(t0 >> 32);
            u32 w2 = (u32)s1 + (u32)t1;
            u32 w3 = (u32)(s1 >> 32) + (u32)(t1 >> 32);
            d0 = (u64)w0 | ((u64)w1 << 32);
            d1 = (u64)w2 | ((u64)w3 << 32);
            break;
        }
        case 0x09u: { /* PSUBW rd, rs, rt */
            u32 w0 = (u32)s0 - (u32)t0;
            u32 w1 = (u32)(s0 >> 32) - (u32)(t0 >> 32);
            u32 w2 = (u32)s1 - (u32)t1;
            u32 w3 = (u32)(s1 >> 32) - (u32)(t1 >> 32);
            d0 = (u64)w0 | ((u64)w1 << 32);
            d1 = (u64)w2 | ((u64)w3 << 32);
            break;
        }
        case 0x10u: /* PEXTLW rd, rs, rt */
            d0 = (t0 & 0xFFFFFFFFull) | ((s0 & 0xFFFFFFFFull) << 32);
            d1 = (t0 >> 32) | ((s0 >> 32) << 32);
            break;
        case 0x1Bu: { /* PPACH rd, rs, rt */
            u16 h[8];
            h[0] = (u16)(t0 & 0xFFFFu);
            h[1] = (u16)((t0 >> 32) & 0xFFFFu);
            h[2] = (u16)(t1 & 0xFFFFu);
            h[3] = (u16)((t1 >> 32) & 0xFFFFu);
            h[4] = (u16)(s0 & 0xFFFFu);
            h[5] = (u16)((s0 >> 32) & 0xFFFFu);
            h[6] = (u16)(s1 & 0xFFFFu);
            h[7] = (u16)((s1 >> 32) & 0xFFFFu);
            d0 = (u64)h[0] | ((u64)h[1] << 16) | ((u64)h[2] << 32) | ((u64)h[3] << 48);
            d1 = (u64)h[4] | ((u64)h[5] << 16) | ((u64)h[6] << 32) | ((u64)h[7] << 48);
            break;
        }
        case 0x1Cu: /* PPACW rd, rs, rt */
            d0 = (t0 & 0xFFFFFFFFull) | ((t1 & 0xFFFFFFFFull) << 32);
            d1 = (s0 & 0xFFFFFFFFull) | ((s1 & 0xFFFFFFFFull) << 32);
            break;
        case 0x1Fu: { /* PPACB rd, rs, rt */
            int i;
            for (i = 0; i < 4; i++) {
                u8 tb = (u8)((i < 2 ? (t0 >> (i * 32)) : (t1 >> ((i - 2) * 32))) & 0xFFu);
                u8 sb = (u8)((i < 2 ? (s0 >> (i * 32)) : (s1 >> ((i - 2) * 32))) & 0xFFu);
                d0 |= (u64)tb << (i * 8);
                d0 |= (u64)sb << ((i + 4) * 8);
            }
            for (i = 0; i < 4; i++) {
                u8 tb = (u8)((i < 2 ? (t0 >> (i * 32 + 16)) : (t1 >> ((i - 2) * 32 + 16))) & 0xFFu);
                u8 sb = (u8)((i < 2 ? (s0 >> (i * 32 + 16)) : (s1 >> ((i - 2) * 32 + 16))) & 0xFFu);
                d1 |= (u64)tb << (i * 8);
                d1 |= (u64)sb << ((i + 4) * 8);
            }
            break;
        }
        default:
            return 0;
        }
    } else if (fn == 0x29u) { /* MMI3 */
        switch (sa) {
        case 0x08u: { /* PADDUW rd, rs, rt */
            int i;
            for (i = 0; i < 4; i++) {
                u64 sv = (i < 2) ? ((s0 >> (i * 32)) & 0xFFFFFFFFull) : ((s1 >> ((i - 2) * 32)) & 0xFFFFFFFFull);
                u64 tv = (i < 2) ? ((t0 >> (i * 32)) & 0xFFFFFFFFull) : ((t1 >> ((i - 2) * 32)) & 0xFFFFFFFFull);
                u64 r = sv + tv;
                if (r > 0xFFFFFFFFull) r = 0xFFFFFFFFull;
                if (i < 2) d0 |= r << (i * 32); else d1 |= r << ((i - 2) * 32);
            }
            break;
        }
        case 0x09u: { /* PSUBUW rd, rs, rt */
            int i;
            for (i = 0; i < 4; i++) {
                u64 sv = (i < 2) ? ((s0 >> (i * 32)) & 0xFFFFFFFFull) : ((s1 >> ((i - 2) * 32)) & 0xFFFFFFFFull);
                u64 tv = (i < 2) ? ((t0 >> (i * 32)) & 0xFFFFFFFFull) : ((t1 >> ((i - 2) * 32)) & 0xFFFFFFFFull);
                u64 r = (sv > tv) ? (sv - tv) : 0ull;
                if (i < 2) d0 |= r << (i * 32); else d1 |= r << ((i - 2) * 32);
            }
            break;
        }
        case 0x0Cu: /* PEXTUW rd, rs, rt */
            d0 = (t1 & 0xFFFFFFFFull) | ((s1 & 0xFFFFFFFFull) << 32);
            d1 = (t1 >> 32) | ((s1 >> 32) << 32);
            break;
        default:
            return 0;
        }
    } else {
        return 0;
    }

    if (rd != 0) {
        ctx->r[rd].d[0] = d0;
        ctx->r[rd].d[1] = d1;
    }
    (void)pc;
    return 1;
}

void ps2_unimpl(Ctx *ctx, u32 pc, u32 word) {
    if (ps2_try_emulate_mmi(ctx, pc, word)) return;
    trace("UNIMPLEMENTED instruction 0x%08x at pc 0x%08x", word, pc);
    PS2_ANDROID_LOGE("UNIMPLEMENTED instruction 0x%08x at pc 0x%08x", word, pc);
    PS2_GUEST_STOP(4);
}


static float g_vu0_vf[32][4];
void ps2_vu0_lqc2(Ctx *ctx, u32 rt, u32 addr) {
    memcpy(g_vu0_vf[rt & 31u], ps2_ptr(ctx, addr & ~15u), 16);
}
void ps2_vu0_sqc2(Ctx *ctx, u32 rt, u32 addr) {
    memcpy(ps2_ptr(ctx, addr & ~15u), g_vu0_vf[rt & 31u], 16);
}
void ps2_vu0_macro(Ctx *ctx, u32 word) {
    (void)ctx;
    u32 fd = (word >> 6) & 31u, fs = (word >> 11) & 31u, ft = (word >> 16) & 31u;
    u32 fn = word & 63u;
    for (int i = 0; i < 4; i++) {
        if ((word >> (21 + i)) & 1u) {
            if (fn == 0x28u) g_vu0_vf[fd][i] = g_vu0_vf[fs][i] + g_vu0_vf[ft][i];
            else if (fn == 0x2Cu) g_vu0_vf[fd][i] = g_vu0_vf[fs][i] - g_vu0_vf[ft][i];
            else if (fn == 0x2Au) g_vu0_vf[fd][i] = g_vu0_vf[fs][i] * g_vu0_vf[ft][i];
        }
    }
}

int main(int argc, char **argv) {
    Ctx *ctx;
    if (argc < 2) { fprintf(stderr, "usage: %s <your-game.elf>\n", argv[0]); return 1; }
    g_trace = fopen("syscall_trace.log", "w");
    ctx = calloc(1, sizeof *ctx);
    ctx->ram = calloc(1, PS2_RAM_SIZE);
    ctx->spr = calloc(1, 16384);
    if (!ctx->ram || !ctx->spr || load_elf(ctx, argv[1]) != 0) return 1;
    G(29) = 0x01FFFF00u; /* initial $sp (the kernel resets it via InitMainThread) */
    /* Default OSD config: English, 4:3, RGB, version 2 (bit layout from memory: unverified).
     * Override with PS2_OSD_CONFIG=0x... */
    K.osd = getenv("PS2_OSD_CONFIG") ? (u32)strtoul(getenv("PS2_OSD_CONFIG"), NULL, 0) : 0x00014000u;
    T[1].id = 1; T[1].st = TS_RUN; T[1].prio = T[1].init_prio = 0; T[1].started = 1;
    T[1].ctx = ctx; T[1].func = ps2_entry_addr;
    pthread_cond_init(&T[1].cv, NULL);
    cur = &T[1]; ctx->kthread = &T[1];
    trace("start: entry 0x%08x, image end 0x%08x", ps2_entry_addr, K.image_end);
#ifdef PS2_WATCHDOG
    {
        const char *e = getenv("PS2_WATCHDOG");
        if (e) g_wd_secs = (unsigned)atoi(e);
        if (g_wd_secs) {
            struct sigaction act;
            memset(&act, 0, sizeof act);
            act.sa_handler = on_alarm;
            sigemptyset(&act.sa_mask);
            sigaction(SIGALRM, &act, NULL); /* not signal(): strict POSIX makes it one-shot */
            alarm(g_wd_secs);
        }
    }
#endif
    ps2_dispatch(ctx, ps2_entry_addr);
    fprintf(stderr, "ps2rt: entry function returned\n");
    return 0;
}
