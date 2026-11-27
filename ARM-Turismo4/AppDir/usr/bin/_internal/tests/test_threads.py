#!/usr/bin/env python3
"""Scheduler tests (synthetic programs, real syscall numbers):
  1. priority scheduling with a worker thread: start, block on a semaphore, preemption on
     signal, sleep/wakeup, thread exit, ChangeThreadPriority return values
  2. deadlock: every thread blocked and no VBlank handler -> clear report, exit code 6
  3. virtual VBlank: all threads blocked -> the game's INTC handler runs and signals the sema"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import selftest as T  # noqa: E402
from selftest import A0, A1, T0, V0, V1, Z  # noqa: E402

S0, S1, A2, A3 = 16, 17, 6, 7
STACK = 0x200000


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def sc(a, num):
    a.addiu(V1, Z, num)
    a.r(0x0C)


def build_run(tmp, elf, adds):
    out = os.path.join(tmp, "out")
    r = sh([sys.executable, os.path.join(ROOT, "r5900lift.py"), elf, "-o", out]
           + [x for ad in adds for x in ("--add", hex(ad))])
    assert r.returncode == 0, r.stderr + r.stdout
    c = sh(["gcc", "-O1", "-w", "-o", os.path.join(tmp, "game")]
           + [os.path.join(out, f) for f in sorted(os.listdir(out)) if f.endswith(".c")]
           + ["-lm", "-pthread"])
    assert c.returncode == 0, c.stderr
    return sh([os.path.join(tmp, "game"), elf], cwd=tmp, timeout=30)


def make_elf(tmp, a, patches=()):
    words = a.done()
    for label, off, value in patches:        # patch data words that hold addresses
        words[(a.labels[label] - T.BASE) // 4 + off] = value
    elf = os.path.join(tmp, "p.elf")
    T.write_elf(elf, words, a.labels["_start"])
    return elf


def data_tail(a, thread_label=None):
    while a.pc() % 16:
        a.nop()
    a.label("semaparam"); a.w += [0, 1, 0, 0, 0, 0]                   # max 1, initial 0
    a.label("threadparam"); a.w += [0, 0, STACK, 0x1000, 0, 1, 1, 0, 0]  # func patched later


def ordering_case():
    a = T.Asm()
    a.label("_start")
    a.la(A0, "semaparam"); sc(a, 0x40); a.addu(S0, V0, Z)               # sema id (1)
    a.la(A0, "threadparam"); sc(a, 0x20); a.addu(S1, V0, Z)             # thread id (2), prio 1
    a.addiu(T0, Z, 1); a.show(T0)
    a.addu(A0, S1, Z); a.addiu(A1, Z, 7); sc(a, 0x22)                   # StartThread: lower prio, no preempt
    a.addiu(T0, Z, 2); a.show(T0)
    a.addu(A0, S0, Z); sc(a, 0x44)                                      # WaitSema: blocks -> worker runs
    a.addiu(T0, Z, 3); a.show(T0)
    sc(a, 0x32)                                                         # SleepThread: worker runs again
    a.addiu(T0, Z, 4); a.show(T0)
    a.addiu(A0, Z, 1); a.addiu(A1, Z, 5); sc(a, 0x29); a.show(V0)       # ChangeThreadPriority: old 0
    a.addiu(A0, Z, 1); a.addiu(A1, Z, 3); sc(a, 0x29); a.show(V0)       # old 5
    a.addiu(V1, Z, 0x7001); a.r(0x0C)
    a.label("worker")                                                   # entry arg in a0 = 7
    a.addiu(T0, A0, 100); a.show(T0)                                    # 107
    a.addiu(A0, Z, 1); sc(a, 0x42)                                      # SignalSema(1): main preempts
    a.addiu(T0, Z, 200); a.show(T0)
    a.addiu(A0, Z, 1); sc(a, 0x33)                                      # WakeupThread(main): preempts
    a.addiu(T0, Z, 300); a.show(T0)
    a.r(0x08, 31); a.nop()                                              # return = ExitThread
    data_tail(a)
    return a, [("threadparam", 1, a.labels["worker"])]


def deadlock_case():
    a = T.Asm()
    a.label("_start")
    a.la(A0, "semaparam"); sc(a, 0x40)
    a.addiu(A0, Z, 1); sc(a, 0x44)                                      # nobody will ever signal
    a.addiu(V1, Z, 0x7001); a.r(0x0C)
    data_tail(a)
    return a, []


def vblank_case():
    a = T.Asm()
    a.label("_start")
    a.la(A0, "semaparam"); sc(a, 0x40)
    a.addiu(A0, Z, 2); a.la(A1, "handler"); a.addiu(A2, Z, -1); a.addiu(A3, Z, 0); sc(a, 0x10)
    a.addiu(A0, Z, 1); sc(a, 0x44)                                      # blocks -> idle -> VBlank
    a.addiu(T0, Z, 5); a.show(T0)
    a.addiu(V1, Z, 0x7001); a.r(0x0C)
    a.label("handler")
    a.addiu(A0, Z, 1); sc(a, 0x43); a.addu(V0, Z, Z); a.r(0x08, 31); a.nop()   # iSignalSema(1)
    data_tail(a)
    return a, []


def misc_case():
    a = T.Asm()
    a.label("_start")
    a.la(A0, "semaparam2"); sc(a, 0x40); a.addu(S0, V0, Z)
    a.addu(A0, S0, Z); a.la(A1, "buf"); sc(a, 0x47); a.show(V0)          # ReferSemaStatus -> 0
    a.la(A1, "buf")
    for off in (0, 4, 8):                                                 # count 3, max 5, init 3
        a.lw(T0, off, A1); a.show(T0)
    a.la(A0, "buf"); sc(a, 0x4B)                                          # GetOsdConfigParam
    a.la(A1, "buf"); a.lw(T0, 0, A1); a.show(T0)                          # default 0x14000
    a.addiu(V1, Z, 0x7001); a.r(0x0C)
    while a.pc() % 16:
        a.nop()
    a.label("semaparam2"); a.w += [0, 5, 3, 0, 0, 0]
    a.label("buf"); a.w += [0] * 8
    return a


def main():
    tmp = tempfile.mkdtemp(prefix="ps2_thr_")
    a, patches = ordering_case()
    elf = make_elf(tmp, a, patches)
    run = build_run(tmp, elf, [a.labels["worker"]])
    got = [int(x) for x in run.stdout.split()]
    want = [1, 2, 107, 3, 200, 4, 300, 0, 5]
    assert run.returncode == 0 and got == want, (run.returncode, got, want, run.stderr)
    print("scheduler: priorities, blocking, preemption, sleep/wakeup and exit behave as expected")

    tmp2 = tempfile.mkdtemp(prefix="ps2_thr_")
    a, _ = deadlock_case()
    run = build_run(tmp2, make_elf(tmp2, a), [])
    assert run.returncode == 6 and "deadlock" in run.stderr and "sema 1" in run.stderr, \
        (run.returncode, run.stderr)
    print("scheduler: deadlock is reported with each thread's state")

    tmp3 = tempfile.mkdtemp(prefix="ps2_thr_")
    a, _ = vblank_case()
    run = build_run(tmp3, make_elf(tmp3, a), [a.labels["handler"]])
    assert run.returncode == 0 and run.stdout.split() == ["5"], (run.returncode, run.stdout, run.stderr)
    print("scheduler: virtual VBlank wakes a thread blocked on a semaphore")
    tmp4 = tempfile.mkdtemp(prefix="ps2_thr_")
    run = build_run(tmp4, make_elf(tmp4, misc_case()), [])
    assert run.returncode == 0 and [int(x) for x in run.stdout.split()] == [0, 3, 5, 3, 0x14000], \
        (run.returncode, run.stdout, run.stderr)
    print("kernel: ReferSemaStatus and the OSD config call behave as expected")
    print("PASS")


if __name__ == "__main__":
    main()
