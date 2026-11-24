# ARM-Turismo4

# *THIS VERSION HAS NOT BEEN UPDATED YET AND IS STILL THE PAL VERSION OF THE RECOMPILER.*

This branch *will be* made with Gemini 4 Hydrogen (premium). 

The plan for this branch is that it will be rebuilt using *some* ported GT4Recomp code on Linux for Android in order to speed up development time rather than building it from the ground up as I have been so far.

A static recompilation toolchain that turns PlayStation 2 (MIPS R5900) executables into portable C,
with the long-term goal of running **Gran Turismo 4 natively on ARM devices (Android and Nintendo Switch)**.

> **Status: early research prototype.** The CPU side of the game recompiles and builds, and the game's
> startup code runs under a traced kernel layer. It does **not** render anything yet. There is no GS
> (graphics) emulation, no IOP/disc/controller/audio support and no ARM build has been tried.
> See [Status](#status).

## Legal

- This repository contains **no game code or data**. You must supply your own legally obtained disc image.
- Everything derived from the disc stays on your machine: the extracted executables (`*.elf`, `CORE.*`),
  the generated C (`out*/`) and the run logs. The included `.gitignore` excludes them. **Never commit them.**
- This project is independent and not affiliated with Sony, Polyphony Digital or any other rights holder.
- Licence: AGPL-3.0 license.

## How it works

```
disc image ──► extract_boot_elf.py ──► CORE.GT4 ──► core_probe.py ──► core.elf
                                                                          │
                              r5900lift.py  (discover functions, lift instructions to C)
                                                                          ▼
                       out_core/  generated_*.c + func_table.c + ps2rt.{c,h} + Makefile
                                                                          │
                                         make  ──►  ./game core.elf  (kernel layer + trace logs)
```

Retail GT4 is split into a small bootstrap (`SCES_xxx.xx`, only a loader) and `CORE.GT4`, which holds the
game itself. `core_probe.py` unpacks CORE (a 6-byte layer header, a raw-deflate stream, then a header with
sections and an entry point) and writes a standard ELF that the lifter can read. The generated C uses only
C99 and the standard library, so the same output is meant to build with the Android NDK (arm64-v8a),
devkitA64 (Switch) or any desktop compiler.

## Requirements

- Python 3.8+, a C compiler (gcc or clang) and `make`
- Developed and run on x86-64 Linux. The watchdog and `-rdynamic` link flag assume glibc.
- Expect the full build to take several minutes and a few GB of RAM: the real game produces about 1.2 million
  lifted instructions in 80+ generated source files.

## Quick start (real disc)

Run from the repository root. `game.iso` is your own image of the game.

```bash
python3 tools/extract_boot_elf.py game.iso --file CORE.GT4 -o CORE.GT4
python3 tools/core_probe.py CORE.GT4 --emit-elf core.elf
python3 r5900lift.py core.elf -o out_core --scan --split 400
make -C out_core -j8 CFLAGS=-O0
cd out_core && ./game ../core.elf
```

Useful variations:

| Goal | Command |
|---|---|
| Extract only the boot executable | `python3 tools/extract_boot_elf.py game.iso -o boot.elf` |
| Estimate where code ends and data begins | `python3 tools/find_code_range.py core.elf` |
| Update only the runtime in an existing build (skips the slow lift) | `python3 r5900lift.py --sync-runtime out_core` |
| Lift code the game copies around at runtime | add `--blob SRC SIZE DEST` (repeatable) |
| Feed back unresolved indirect jump targets | add `--entries missing_entries.txt` |

### Lifter options (`r5900lift.py ELF`)

| Option | Meaning |
|---|---|
| `-o DIR` | output directory (default `out`) |
| `--scan` | find more functions heuristically: code after `jr ra`, code-pointer words, and runs of pointers (jump tables, vtables) |
| `--split N` | write N functions per `generated_NNN.c` (use for big inputs) |
| `--code-range LO HI` | only treat addresses in `[LO, HI)` as code |
| `--blob SRC SIZE DEST` | lift `SIZE` bytes found at `SRC` in the image as code located at `DEST` |
| `--entries FILE` / `--add ADDR` | extra function entry points |
| `--sync-runtime DIR` | copy `runtime/ps2rt.{h,c}` into `DIR` and exit |

The lifter only rewrites output files whose content changed, so `make` rebuilds just what it needs.

## What the run produces

The runtime writes these files in the directory you run `./game` from:

| File | Content |
|---|---|
| `syscall_trace.log` | every BIOS syscall with its documented name and arguments, plus the reason the run stopped |
| `missing_entries.txt` | indirect jump targets the lifter did not know (feed back with `--entries`) |
| `hw_access_log.txt` | hardware or unmapped address pages the game touched |

`lift_report.txt` (written by the lifter) lists unimplemented instruction classes with counts and first
addresses, and an inventory of BIOS syscall stubs.

Exit codes: `3` unresolved indirect target, `4` unimplemented instruction, `5` unmodelled syscall,
`6` needs the thread scheduler or IOP, `7` runaway syscalls or hardware polling, `8` watchdog (no syscall for
a while, the guest call stack is printed). Environment variables: `PS2_LENIENT=1` lets unknown syscalls return 0,
`PS2_WATCHDOG=<seconds>` sets the watchdog timeout (default 20, `0` disables).

## Status

Measured on one NTSC retail disc:

- `CORE.GT4` unpacks cleanly (no encryption layer in the retail build), 3 sections, entry `0x00100008`,
  code in `0x100000`–`0x617a14`.
- The lifter finds about 33,000 functions and 1.24 million instructions, of which 99.86% are lifted.
  The remainder is mostly VU0 macro-mode (COP2) instructions.
- The generated code builds and links.
- The game runs through its startup code and the game's own libkernel setup, including guest-installed
  kernel-mode handlers, before stopping at the next unmodelled piece.

| Area | State |
|---|---|
| Disc/ISO extraction, CORE unpacking | done |
| Integer ISA, unaligned loads/stores, 128-bit `lq`/`sq`, delay slots, branch-likely | done |
| FPU (single precision, approximate), `BC1`, minimal COP0, `ERET`, `BC0` | done |
| MMI (128-bit SIMD): common subset, shifts, multiply-add, SA-register ops | partial |
| Function discovery: calls, `--scan`, jump tables, de-duplication, code range, runtime-copied blobs | done |
| Kernel layer: `InitMainThread`, `InitHeap`, semaphores, handler registration, guest `SetSyscall` handlers, fake kernel syscall table | partial |
| Guest thread scheduler (`StartThread`, blocking `WaitSema`, `SleepThread`) | not started |
| IOP / SIF RPC services (disc, pads, memory cards, sound) | not started |
| VU0 macro mode (COP2) | not started |
| VU1 microcode, DMA, GIF, GS renderer (Vulkan) | not started |
| Audio, input, file access, Android/Switch platform layer | not started |
| Build and test on ARM hardware | not started |

### Known inaccuracies

- The FPU uses host floats. It models the missing NaN/Inf and flushed denormals but **not** the hardware's
  round-toward-zero behaviour.
- Some MMI lane encodings and the `MTSAB`/`MTSAH`/`MTSA`/`QFSRV` formulas were written from memory of the R5900
  manual and checked only against hand-derived expectations. Verify against a reference before trusting results.
- The thread and semaphore parameter structure layouts in the runtime come from memory of the ps2sdk headers.
  The trace prints the raw words so a wrong layout is visible.
- The fake kernel syscall table base (`0x80002000`) is a guess. The game only needs the words to exist in range.

## Repository layout

```
r5900lift.py              the recompiler (ELF -> C)
runtime/ps2rt.{h,c}       runtime: CPU context, memory, FPU/MMI/COP0 helpers, kernel layer, trace, watchdog
tools/extract_boot_elf.py extract the boot executable or any file from a PS2 disc image
tools/core_probe.py       unpack CORE.* and emit an ELF
tools/find_code_range.py  estimate the code/data boundary in an ELF
tests/                    synthetic tests (no game data)
```

## Tests

All tests use synthetic inputs generated on the fly; none need game data. They need Python, gcc and make.

```bash
python3 tests/selftest.py        # assemble, lift, compile, run
python3 tests/test_extract.py    # ISO extraction (cooked and raw-sector images)
python3 tests/test_core.py       # CORE unpacking (both compression variants)
python3 tests/test_features.py   # unaligned access, FPU, COP0, MMI, SA ops, syscalls, watchdog
python3 tests/test_discovery.py  # dedupe, code range, jump tables, runtime-copied code
```

The expected values were derived by hand from the instruction definitions, so they check the lifter against
a reading of the manual, not against real hardware.

## Troubleshooting

- **The game seems stuck:** it is almost certainly spinning on something unmodelled. After 20 seconds without a
  syscall the watchdog prints the guest call stack (`f_<address>` frames, innermost first) and exits with code 8.
- **`unresolved indirect target`:** re-lift with `--entries missing_entries.txt`, or add the code with `--blob`
  if the game copies it into memory at runtime.
- **`unmodelled syscall`:** the trace names the syscall and its arguments. `PS2_LENIENT=1` continues with a 0 result.
- **A very slow or huge build:** use `CFLAGS=-O0`, keep `--split`, and rebuild with `make -j`.

## Contributing

The most useful contributions at this stage are verified instruction semantics (with a source), more of the
kernel layer, and results from running the toolchain on other discs and regions. When reporting a problem,
include the last lines of `syscall_trace.log` and, if relevant, `lift_report.txt`. Do not attach game files.

## Acknowledgements

This work relies on public community documentation: the PS2Tek hardware and BIOS reference, the ps2sdk
documentation, and the Gran Turismo Modding Hub's format notes. A related project, GT4Recomp, also explores
static recompilation of GT4 for PC. This repository is not affiliated with it.
