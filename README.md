# ARM-Turismo4

Prototype static recompiler: PS2 (MIPS R5900) ELF -> portable C99, plus a tiny runtime.
It contains no game code or data. You supply an ELF extracted from your own disc.

## Workflow for a real GT4 disc
    python3 tools/extract_boot_elf.py game.iso -o boot.elf                 # bootstrap only (small)
    python3 tools/extract_boot_elf.py game.iso --file CORE.GT4 -o CORE.GT4 # the real game code
    python3 tools/core_probe.py CORE.GT4 --emit-elf core.elf               # unpack -> ELF
    python3 r5900lift.py core.elf -o out_core                              # lift the game
The bootstrap (`SCES_xxx.xx`) is the initial loader; the core engine lives in `CORE.GT4`,
which `core_probe.py` unpacks per the community format docs. Layer checks should be verified
against your real disc dump, and any mismatches or missing entries logged as you bring up
`CORE.GT4`.

## Try it
    python3 tests/selftest.py                      # assemble, lift, compile, run, verify
    python3 tests/test_extract.py                  # ISO extraction on synthetic images
    python3 tests/test_core.py                     # CORE unpacking on synthetic files
    python3 r5900lift.py game.elf -o out           # lift your own ELF
    cc -O2 -o game out/generated.c out/func_table.c out/ps2rt.c
    ./game game.elf

## What works now
- ELF32 loader, recursive-descent function discovery, delay slots, branch-likely
- Integer ISA: ALU, shifts, 64-bit ops, mult/div (R5900 3-operand mult), loads/stores,
  unaligned lwl/lwr/swl/swr, 128-bit lq/sq, jal/jalr/jr, tail calls
- Indirect jumps via `ps2_dispatch()`; unresolved targets are written to
  `missing_entries.txt`. Re-run with `--entries missing_entries.txt` until none remain.
- `lift_report.txt` lists every unimplemented instruction class with counts and first address.
- Broad MMI (128-bit SIMD), COP1 (FPU) approximation, COP0 basics, and initial COP2/VU0
  macro-mode / `CACHE` / `TGE` helpers in `ps2rt.c`
- Multi-threaded EE kernel prototype with priority scheduling, semaphores, OSD config,
  virtual VBlank/timer IRQs, and initial SIF RPC / DMAC / GS hardware HLE hooks (`ps2_sif_hle.c`,
  `ps2_hw_hle.c`, `ps2_linux_host.c`)

## Not done yet (in rough order of effort)
1. Complete DMAC/GS `FINISH` & thread wakeup synchronization in `boot.elf` without deadlock
2. Full IOP / SIF / CDVD disc (`GT4.VOL`) reads, `LoadExecPS2` handoff to `CORE.GT4`, memory card (`mc0:`), and pad/input RPCs
3. VU1 microcode (record uploaded microprograms, translate ahead of time)
4. Graphics Synthesizer renderer (GLES/Vulkan) — frame/Z-buffer management, CLUT/palette textures, blending, and host presentation
5. SPU2 audio output
6. Jump-table detection (currently every indirect target becomes a mini-function)

## Porting to ARM
Generated code and runtime use standard C headers (`stdint.h`, `string.h`, `stdio.h`,
`stdlib.h`, `math.h`) and assume a little-endian host.
- **Linux / Desktop testing:** build with `gcc` and `-lpthread -lm` (see `ps2_linux_host.c`).
- **Android (`arm64-v8a`):** build with the Android NDK (`PS2_ANDROID_LOGI`/`PS2_ANDROID_LOGE` hooks in `ps2rt.c`) and wire host window/input/audio for target devices.
- **Switch:** `devkitA64` / `libnx`.