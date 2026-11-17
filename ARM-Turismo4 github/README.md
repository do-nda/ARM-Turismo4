# ps2recomp-proto

Prototype static recompiler: PS2 (MIPS R5900) ELF -> portable C99, plus a tiny runtime.
It contains no game code or data. You supply an ELF extracted from your own disc.

## Try it
    python3 tests/selftest.py                      # assemble, lift, compile, run, verify
    python3 r5900lift.py game.elf -o out           # lift your own ELF
    cc -O2 -o game out/generated.c out/func_table.c out/ps2rt.c
    ./game game.elf

## What works now
- ELF32 loader, recursive-descent function discovery, delay slots, branch-likely
- Integer ISA: ALU, shifts, 64-bit ops, mult/div (R5900 3-operand mult), loads/stores,
  unaligned lwl/lwr/swl/swr, 128-bit lq/sq, jal/jalr/jr, tail calls
- Indirect jumps via ps2_dispatch(); unresolved targets are written to
  missing_entries.txt. Re-run with `--entries missing_entries.txt` until none remain.
- lift_report.txt lists every unimplemented instruction class with counts and first address.

## Not done yet (in rough order of effort)
1. MMI (128-bit SIMD), COP1 (FPU), COP0, COP2/VU0 macro mode
2. VU1 microcode (record uploaded microprograms, translate ahead of time)
3. Real BIOS syscalls and threads (the syscalls in ps2rt.c are test placeholders)
4. DMA, VIF, GIF, IOP/file system, SPU2 audio
5. Graphics Synthesizer renderer (Vulkan) - the main blocker for Android/Switch
6. Jump-table detection (currently every indirect target becomes a mini-function)

## Porting to ARM
Generated code and runtime use only stdint.h/string.h/stdio.h/stdlib.h and assume a
little-endian host. Android: build with the NDK (arm64-v8a). Switch: devkitA64/libnx.
