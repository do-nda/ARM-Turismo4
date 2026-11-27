#!/usr/bin/env python3
"""gt4_recomp_apk.py - End-to-end PAL Gran Turismo 4 ISO -> Android ARM64 recompiler pipeline."""
import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import zipfile


def resource_root():
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def tool_cmd(rel_script, *args):
    """Build subprocess command that works both in raw Python and inside a frozen AppImage."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--run-tool", rel_script, *args]
    return [sys.executable, os.path.join(resource_root(), rel_script), *args]


def run_step(cmd, label):
    print(f"==> {label}", flush=True)
    subprocess.run(cmd, check=True)


def find_latest_tool(sdk_root, subdir_glob, binary_name):
    if not sdk_root or not os.path.isdir(sdk_root):
        return None
    matches = sorted(glob.glob(os.path.join(sdk_root, subdir_glob, binary_name)))
    for m in reversed(matches):
        if os.path.isfile(m) and os.access(m, os.X_OK):
            return m
    return None


def find_android_jar(sdk_root):
    if not sdk_root or not os.path.isdir(sdk_root):
        return None
    matches = sorted(glob.glob(os.path.join(sdk_root, "platforms", "android-*", "android.jar")))
    return matches[-1] if matches else None


def package_apk(sdk_root, manifest_path, so_path, out_dir):
    aapt2 = find_latest_tool(sdk_root, "build-tools/*", "aapt2")
    zipalign = find_latest_tool(sdk_root, "build-tools/*", "zipalign")
    apksigner = find_latest_tool(sdk_root, "build-tools/*", "apksigner")
    android_jar = find_android_jar(sdk_root)
    if not (aapt2 and zipalign and apksigner and android_jar):
        print(
            "==> Android SDK build-tools/platforms not found; skipping .apk packaging "
            "(libgt4recomp.so is ready).",
            flush=True,
        )
        return None

    apk_stage = os.path.join(out_dir, "apk_stage")
    shutil.rmtree(apk_stage, ignore_errors=True)
    os.makedirs(os.path.join(apk_stage, "lib", "arm64-v8a"), exist_ok=True)
    shutil.copy2(so_path, os.path.join(apk_stage, "lib", "arm64-v8a", "libgt4recomp.so"))

    unaligned_apk = os.path.join(out_dir, "gt4recomp-unaligned.apk")
    aligned_apk = os.path.join(out_dir, "gt4recomp-aligned.apk")
    final_apk = os.path.join(out_dir, "GT4Recomp-arm64-v8a.apk")

    assets_dir = os.path.join(out_dir, "apk_assets")
    os.makedirs(assets_dir, exist_ok=True)
    elf_src = os.path.join(out_dir, "extracted", "boot.elf")
    if os.path.isfile(elf_src):
        shutil.copy2(elf_src, os.path.join(assets_dir, "core.elf"))
    run_step(
        [aapt2, "link", "-o", unaligned_apk, "--min-sdk-version", "26", "--target-sdk-version", "35", "--version-code", "1", "--version-name", "0.1", "-I", android_jar, "--manifest", manifest_path] + (["-A", os.path.join(out_dir, "apk_assets")] if os.path.isdir(os.path.join(out_dir, "apk_assets")) else []),
        "Linking base APK manifest with aapt2",
    )
    java_src_dir = os.path.join(os.path.dirname(os.path.abspath(manifest_path)), "src")
    java_files = []
    if os.path.isdir(java_src_dir):
        for r, _, fs in os.walk(java_src_dir):
            for fn in fs:
                if fn.endswith(".java"):
                    java_files.append(os.path.join(r, fn))
    if java_files:
        dex_dir = os.path.join(out_dir, "dex_build")
        classes_dir = os.path.join(dex_dir, "classes")
        shutil.rmtree(dex_dir, ignore_errors=True)
        os.makedirs(classes_dir, exist_ok=True)
        run_step(["javac", "-source", "1.8", "-target", "1.8", "-classpath", android_jar, "-d", classes_dir] + java_files, "Compiling Java sources with javac")
        class_files = []
        for r, _, fs in os.walk(classes_dir):
            for fn in fs:
                if fn.endswith(".class"):
                    class_files.append(os.path.join(r, fn))
        d8_bin = find_latest_tool(sdk_root, "build-tools/*", "d8")
        assert d8_bin, "Android SDK d8 tool not found under build-tools"
        run_step([d8_bin, "--lib", android_jar, "--output", dex_dir] + class_files, "Converting classes to classes.dex with d8")
        subprocess.run(["zip", "-j", unaligned_apk, os.path.join(dex_dir, "classes.dex")], check=True)

    with zipfile.ZipFile(unaligned_apk, "a", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(
            os.path.join(apk_stage, "lib", "arm64-v8a", "libgt4recomp.so"),
            "lib/arm64-v8a/libgt4recomp.so",
        )

    run_step([zipalign, "-f", "-p", "4", unaligned_apk, aligned_apk], "Aligning APK with zipalign")

    keystore = os.path.join(out_dir, "debug.keystore")
    keytool = shutil.which("keytool")
    if not os.path.exists(keystore) and keytool:
        run_step(
            [
                keytool, "-genkeypair", "-v",
                "-keystore", keystore,
                "-storepass", "android",
                "-alias", "androiddebugkey",
                "-keypass", "android",
                "-keyalg", "RSA",
                "-keysize", "2048",
                "-validity", "10000",
                "-dname", "CN=Android Debug,O=Android,C=US",
            ],
            "Generating debug signing keystore",
        )

    if os.path.exists(keystore):
        run_step(
            [
                apksigner, "sign",
                "--ks", keystore,
                "--ks-pass", "pass:android",
                "--key-pass", "pass:android",
                "--out", final_apk,
                aligned_apk,
            ],
            "Signing GT4Recomp-arm64-v8a.apk",
        )
        return final_apk
    return aligned_apk


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("iso", help="Path to user-supplied PAL Gran Turismo 4 ISO/BIN")
    ap.add_argument("-o", "--out", default="build_gt4_android", help="Output working directory")
    ap.add_argument(
        "--ndk",
        default=os.environ.get("ANDROID_NDK_HOME", os.environ.get("ANDROID_NDK_ROOT", "")),
        help="Path to Android NDK (optional; builds arm64-v8a .so if provided)",
    )
    ap.add_argument(
        "--sdk",
        default=os.environ.get("ANDROID_HOME", os.environ.get("ANDROID_SDK_ROOT", "")),
        help="Path to Android SDK (optional; packages .apk if build-tools & platform exist)",
    )
    ap.add_argument("--split", type=int, default=400, help="Functions per C translation unit")
    ap.add_argument("--entries", default="", help="Optional missing_entries.txt")
    ap.add_argument("--blobs", default="", help="Optional blobs.txt")
    ap.add_argument("--code-range", nargs=2, metavar=("LO", "HI"), help="Manual R5900 code range override")
    ap.add_argument("--no-scan", action="store_true", help="Disable pointer/jump-table scan")
    ap.add_argument("--vu0-macro", action="store_true", help="Emit VU0 macro-mode C helpers")
    args = ap.parse_args()

    repo = resource_root()
    out_dir = os.path.abspath(args.out)
    ext_dir = os.path.join(out_dir, "extracted")
    lift_dir = os.path.join(out_dir, "lifted")
    os.makedirs(ext_dir, exist_ok=True)

    # 1. Extract BOOT2 ELF and CORE.GT4 from user's PAL ISO
    boot_elf = os.path.join(ext_dir, "boot.elf")
    core_gt4 = os.path.join(ext_dir, "CORE.GT4")
    run_step(
        tool_cmd("tools/extract_boot_elf.py", args.iso, "-o", boot_elf),
        "[1/5] Extracting BOOT2 ELF from PAL ISO",
    )
    try:
        run_step(
            tool_cmd("tools/extract_boot_elf.py", args.iso, "-o", core_gt4, "--file", "CORE.GT4"),
            "[1/5] Extracting CORE.GT4 from PAL ISO",
        )
    except subprocess.CalledProcessError:
        print("==> CORE.GT4 not found on disc; continuing with extracted boot ELF", flush=True)

    # 2. Unpack CORE.GT4 into core.elf (fall back to boot.elf if container format differs)
    core_gt4 = os.path.join(ext_dir, "CORE.GT4")
    core_elf = os.path.join(ext_dir, "core.elf")
    boot_elf = os.path.join(ext_dir, "boot.elf")
    target_elf = None
    if os.path.exists(core_gt4):
        try:
            run_step(
                tool_cmd("tools/core_probe.py", core_gt4, "--emit-elf", core_elf),
                "[2/5] Unpacking CORE.GT4 -> core.elf",
            )
            target_elf = core_elf
        except subprocess.CalledProcessError:
            print("==> CORE.GT4 header not recognized by core_probe.py; falling back to extracted boot ELF", flush=True)
    if not target_elf:
        if os.path.exists(boot_elf):
            target_elf = boot_elf
        else:
            elfs = [f for f in os.listdir(ext_dir) if f.endswith(".elf")]
            if not elfs:
                sys.exit("Error: No ELF extracted from ISO.")
            target_elf = os.path.join(ext_dir, elfs[0])

    # 3. Auto-detect R5900 code vs. rodata boundary (unless overridden)
    if args.code_range:
        code_lo, code_hi = args.code_range[0], args.code_range[1]
        print(f"==> [3/5] Using explicit code range {code_lo} .. {code_hi}", flush=True)
    else:
        print("==> [3/5] Detecting R5900 code/data boundary", flush=True)
        res = subprocess.run(
            tool_cmd("tools/find_code_range.py", target_elf),
            capture_output=True, text=True, check=True,
        )
        print(res.stdout.strip(), flush=True)
        m = re.search(r"--code-range\s+(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)", res.stdout)
        code_lo, code_hi = (m.group(1), m.group(2)) if m else (None, None)

    # 4. Lift R5900 machine code to portable C99
    lift_cmd = tool_cmd("r5900lift.py", target_elf, "-o", lift_dir, "--split", str(args.split))
    if not args.no_scan:
        lift_cmd.append("--scan")
    if code_lo and code_hi:
        lift_cmd += ["--code-range", code_lo, code_hi]
    if args.vu0_macro:
        lift_cmd.append("--vu0-macro")

    entries_path = args.entries or (os.path.join(os.getcwd(), "missing_entries.txt") if os.path.exists("missing_entries.txt") else (os.path.join(repo, "missing_entries.txt") if os.path.exists(os.path.join(repo, "missing_entries.txt")) else ""))
    blobs_path = args.blobs or (os.path.join(os.getcwd(), "blobs.txt") if os.path.exists("blobs.txt") else (os.path.join(repo, "blobs.txt") if os.path.exists(os.path.join(repo, "blobs.txt")) else ""))
    if entries_path and os.path.exists(entries_path):
        lift_cmd += ["--entries", entries_path]
    if blobs_path and os.path.exists(blobs_path):
        lift_cmd += ["--blobs-file", blobs_path]

    run_step(lift_cmd, "[4/5] Lifting R5900 instructions to C99")

    # 5. Cross-compile native arm64-v8a Android library & optionally sign APK
    if args.ndk:
        toolchain = os.path.join(args.ndk, "build", "cmake", "android.toolchain.cmake")
        cmake_build = os.path.join(out_dir, "cmake_arm64")
        shutil.rmtree(cmake_build, ignore_errors=True)
        run_step(
            [
                "cmake", "-S", os.path.join(repo, "android"), "-B", cmake_build,
                f"-DCMAKE_TOOLCHAIN_FILE={toolchain}",
                "-DANDROID_ABI=arm64-v8a",
                "-DANDROID_PLATFORM=android-26",
                f"-DLIFTED_DIR={lift_dir}",
            ],
            "[5/5] Configuring Android NDK arm64-v8a CMake project",
        )
        run_step(["cmake", "--build", cmake_build, "-j"], "[5/5] Compiling libgt4recomp.so (arm64-v8a)")
        so_path = os.path.join(cmake_build, "libgt4recomp.so")
        print(f"Done. Native library ready at: {so_path}", flush=True)
        if args.sdk:
            apk_path = package_apk(args.sdk, os.path.join(repo, "android", "AndroidManifest.xml"), so_path, out_dir)
            if apk_path:
                print(f"APK ready at: {apk_path}", flush=True)
    else:
        print(f"==> [5/5] NDK not provided; C99 output ready in: {lift_dir}", flush=True)


if __name__ == "__main__":
    main()
