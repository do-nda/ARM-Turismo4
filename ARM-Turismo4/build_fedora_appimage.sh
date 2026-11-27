#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

ARCH="$(uname -m)"
if [[ "${ARCH}" != "x86_64" ]]; then
    echo "Error: Expected x86_64 host architecture, found ${ARCH}." >&2
    exit 1
fi

echo "==> [1/6] Installing Fedora build & Qt6/FUSE runtime dependencies via dnf..."
if command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y --skip-unavailable \
        python3 python3-pip python3-devel \
        gcc gcc-c++ cmake make binutils curl \
        fuse3 fuse3-libs desktop-file-utils \
        mesa-libGL libxkbcommon xcb-util-cursor \
        dbus-libs fontconfig freetype
fi

echo "==> [2/6] Generating Qt6 desktop GUI wrapper (gt4_recomp_gui.py)..."
cat > gt4_recomp_gui.py << 'EOF'
#!/usr/bin/env python3
import glob, os, runpy, subprocess, sys

def resource_root():
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))

def dispatch_subtool_or_cli():
    if len(sys.argv) >= 2 and sys.argv[1].endswith(".py") and os.path.isfile(sys.argv[1]):
        target = sys.argv[1]
        sys.argv = [target] + sys.argv[2:]
        runpy.run_path(target, run_name="__main__")
        sys.exit(0)
    if len(sys.argv) >= 3 and sys.argv[1] == "--run-tool":
        rel_script = sys.argv[2]
        target = os.path.join(resource_root(), rel_script)
        if not os.path.isfile(target):
            sys.stderr.write(f"Bundled tool not found: {rel_script}\n")
            sys.exit(1)
        sys.argv = [target] + sys.argv[3:]
        runpy.run_path(target, run_name="__main__")
        sys.exit(0)
    if len(sys.argv) >= 2 and sys.argv[1] == "--cli":
        target = os.path.join(resource_root(), "gt4_recomp_apk.py")
        sys.argv = [target] + sys.argv[2:]
        runpy.run_path(target, run_name="__main__")
        sys.exit(0)
    if len(sys.argv) >= 2 and sys.argv[1] == "--self-test":
        repo = resource_root()
        for tf in [
            "tests/selftest.py", "tests/test_core.py", "tests/test_extract.py",
            "tests/test_features.py", "tests/test_threads.py", "tests/test_discovery.py",
        ]:
            cmd = [sys.executable, "--run-tool", tf] if getattr(sys, "frozen", False) else [sys.executable, os.path.join(repo, tf)]
            if subprocess.run(cmd).returncode != 0:
                sys.exit(1)
        print("All 6 test suites passed!")
        sys.exit(0)

dispatch_subtool_or_cli()

from PySide6.QtCore import QProcess, QProcessEnvironment, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPalette, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

def autodetect_ndk():
    for env in ("ANDROID_NDK_HOME", "ANDROID_NDK_ROOT"):
        if os.environ.get(env) and os.path.isdir(os.environ[env]):
            return os.environ[env]
    hits = sorted(glob.glob(os.path.join(os.path.expanduser("~"), "Android", "Sdk", "ndk", "*")))
    return hits[-1] if hits else ""

def autodetect_sdk():
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(env) and os.path.isdir(os.environ[env]):
            return os.environ[env]
    d = os.path.join(os.path.expanduser("~"), "Android", "Sdk")
    return d if os.path.isdir(d) else ""

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ARM-Turismo4 — PS2 R5900 to Android ARM64 Recompiler")
        self.resize(960, 740)
        self.proc = None
        self._test_queue = []

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title = QLabel("ARM-Turismo4 Static Recompiler (Linux x86_64)")
        tf = QFont(); tf.setPointSize(14); tf.setBold(True); title.setFont(tf)
        sub = QLabel("Recompiles user-supplied PAL Gran Turismo 4 (SCES-51719) R5900 binaries into native ARM64 Android libraries/APKs.")
        sub.setStyleSheet("color: #9aa5b1;")
        layout.addWidget(title); layout.addWidget(sub)

        pg = QGroupBox("1. Disc Image & Build Directories")
        pf = QFormLayout(pg)

        self.iso_edit = QLineEdit()
        self.iso_edit.setPlaceholderText("/path/to/GT4Europe.iso")
        b_iso = QPushButton("Browse ISO..."); b_iso.clicked.connect(self.browse_iso)
        b_insp = QPushButton("Inspect ISO"); b_insp.clicked.connect(self.inspect_iso)
        r_iso = QHBoxLayout(); r_iso.addWidget(self.iso_edit); r_iso.addWidget(b_iso); r_iso.addWidget(b_insp)
        pf.addRow("PAL GT4 ISO / BIN:", r_iso)

        self.out_edit = QLineEdit(os.path.join(os.path.expanduser("~"), "ARM-Turismo4-build"))
        b_out = QPushButton("Browse..."); b_out.clicked.connect(lambda: self.browse_dir(self.out_edit, "Select Output Directory"))
        r_out = QHBoxLayout(); r_out.addWidget(self.out_edit); r_out.addWidget(b_out)
        pf.addRow("Output Directory:", r_out)

        self.ndk_edit = QLineEdit(autodetect_ndk())
        self.ndk_edit.setPlaceholderText("Optional: /home/user/Android/Sdk/ndk/<version>")
        b_ndk = QPushButton("Browse NDK..."); b_ndk.clicked.connect(lambda: self.browse_dir(self.ndk_edit, "Select Android NDK"))
        r_ndk = QHBoxLayout(); r_ndk.addWidget(self.ndk_edit); r_ndk.addWidget(b_ndk)
        pf.addRow("Android NDK Path:", r_ndk)

        self.sdk_edit = QLineEdit(autodetect_sdk())
        self.sdk_edit.setPlaceholderText("Optional: /home/user/Android/Sdk")
        b_sdk = QPushButton("Browse SDK..."); b_sdk.clicked.connect(lambda: self.browse_dir(self.sdk_edit, "Select Android SDK"))
        r_sdk = QHBoxLayout(); r_sdk.addWidget(self.sdk_edit); r_sdk.addWidget(b_sdk)
        pf.addRow("Android SDK Path:", r_sdk)
        layout.addWidget(pg)

        og = QGroupBox("2. R5900 Lifter & Discovery Options")
        ol = QVBoxLayout(og)
        r1 = QHBoxLayout()
        r1.addWidget(QLabel("Functions per C file (--split):"))
        self.split_spin = QSpinBox(); self.split_spin.setRange(0, 5000); self.split_spin.setSingleStep(50); self.split_spin.setValue(400)
        r1.addWidget(self.split_spin)
        self.scan_cb = QCheckBox("Pointer & Jump-Table Scan (--scan)"); self.scan_cb.setChecked(True); r1.addWidget(self.scan_cb)
        self.vu0_cb = QCheckBox("VU0 Macro-Mode Helpers (--vu0-macro)"); self.vu0_cb.setChecked(True); r1.addWidget(self.vu0_cb)
        r1.addStretch(); ol.addLayout(r1)

        r2 = QHBoxLayout()
        self.entries_edit = QLineEdit(); self.entries_edit.setPlaceholderText("Optional: missing_entries.txt")
        be = QPushButton("Entries..."); be.clicked.connect(lambda: self.browse_file(self.entries_edit, "Select missing_entries.txt"))
        self.blobs_edit = QLineEdit(); self.blobs_edit.setPlaceholderText("Optional: blobs.txt")
        bb = QPushButton("Blobs..."); bb.clicked.connect(lambda: self.browse_file(self.blobs_edit, "Select blobs.txt"))
        r2.addWidget(QLabel("Seeds:")); r2.addWidget(self.entries_edit); r2.addWidget(be)
        r2.addWidget(QLabel("Overlays:")); r2.addWidget(self.blobs_edit); r2.addWidget(bb)
        ol.addLayout(r2); layout.addWidget(og)

        act = QHBoxLayout()
        self.run_btn = QPushButton("Build Recompiled C99 / ARM64 APK"); self.run_btn.setMinimumHeight(38)
        self.run_btn.setStyleSheet("QPushButton { background-color: #2563eb; color: white; font-weight: bold; border-radius: 6px; }")
        self.run_btn.clicked.connect(self.start_build)
        self.stop_btn = QPushButton("Stop"); self.stop_btn.setMinimumHeight(38); self.stop_btn.setEnabled(False); self.stop_btn.clicked.connect(self.stop_process)
        self.test_btn = QPushButton("Run Self-Tests"); self.test_btn.setMinimumHeight(38); self.test_btn.clicked.connect(self.start_self_tests)
        self.open_btn = QPushButton("Open Output Folder"); self.open_btn.setMinimumHeight(38); self.open_btn.clicked.connect(self.open_output_dir)
        act.addWidget(self.run_btn, 2); act.addWidget(self.stop_btn, 1); act.addWidget(self.test_btn, 1); act.addWidget(self.open_btn, 1)
        layout.addLayout(act)

        sr = QHBoxLayout()
        self.stage_label = QLabel("Idle — ready."); self.stage_label.setStyleSheet("font-weight: bold;")
        self.progress = QProgressBar(); self.progress.setRange(0, 5); self.progress.setValue(0)
        sr.addWidget(self.stage_label, 2); sr.addWidget(self.progress, 3); layout.addLayout(sr)

        self.console = QPlainTextEdit(); self.console.setReadOnly(True)
        mono = QFont("Monospace"); mono.setStyleHint(QFont.StyleHint.TypeWriter); mono.setPointSize(9); self.console.setFont(mono)
        self.console.setStyleSheet("background-color: #0f172a; color: #e2e8f0; border-radius: 6px; padding: 8px;")
        layout.addWidget(self.console, 1)

    def append_log(self, text, color="#e2e8f0"):
        c = self.console.textCursor(); c.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat(); fmt.setForeground(QColor(color)); c.setCharFormat(fmt)
        c.insertText(text); self.console.setTextCursor(c); self.console.ensureCursorVisible()

    def browse_iso(self):
        p, _ = QFileDialog.getOpenFileName(self, "Select PAL GT4 ISO/BIN", os.path.expanduser("~"), "Disc Images (*.iso *.bin *.ISO *.BIN);;All Files (*)")
        if p: self.iso_edit.setText(p)

    def browse_dir(self, edit, title):
        p = QFileDialog.getExistingDirectory(self, title, edit.text() or os.path.expanduser("~"))
        if p: edit.setText(p)

    def browse_file(self, edit, title):
        p, _ = QFileDialog.getOpenFileName(self, title, os.path.expanduser("~"), "Text Files (*.txt);;All Files (*)")
        if p: edit.setText(p)

    def open_output_dir(self):
        d = self.out_edit.text().strip()
        os.makedirs(d, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(d))

    def _tool_cmd(self, rel, args):
        if getattr(sys, "frozen", False):
            return sys.executable, ["--run-tool", rel] + args
        return sys.executable, [os.path.join(resource_root(), rel)] + args

    def _set_busy(self, b):
        self.run_btn.setEnabled(not b); self.test_btn.setEnabled(not b); self.stop_btn.setEnabled(b)

    def stop_process(self):
        self._test_queue.clear()
        if self.proc and self.proc.state() != QProcess.ProcessState.NotRunning:
            self.proc.kill()
            self.append_log("\n[Stopped by user]\n", "#f87171")

    def inspect_iso(self):
        iso = self.iso_edit.text().strip()
        if not iso or not os.path.isfile(iso):
            QMessageBox.warning(self, "Missing ISO", "Select a valid PAL GT4 .iso or .bin file first.")
            return
        self.console.clear(); self.progress.setRange(0, 0); self.stage_label.setText("Inspecting ISO9660 filesystem...")
        prog, args = self._tool_cmd("tools/extract_boot_elf.py", [iso, "--list"])
        self._launch(prog, args, lambda code: (self.progress.setRange(0, 5), self.progress.setValue(0), self.stage_label.setText("Inspection done." if code == 0 else "Inspection failed.")))

    def start_build(self):
        iso, out_dir = self.iso_edit.text().strip(), self.out_edit.text().strip()
        if not iso or not os.path.isfile(iso):
            QMessageBox.warning(self, "Missing ISO", "Select a valid PAL GT4 .iso or .bin file first.")
            return
        args = [iso, "-o", out_dir, "--split", str(self.split_spin.value())]
        if self.ndk_edit.text().strip(): args += ["--ndk", self.ndk_edit.text().strip()]
        if self.entries_edit.text().strip(): args += ["--entries", self.entries_edit.text().strip()]
        if self.blobs_edit.text().strip(): args += ["--blobs", self.blobs_edit.text().strip()]
        if not self.scan_cb.isChecked(): args.append("--no-scan")
        if self.vu0_cb.isChecked(): args.append("--vu0-macro")
        self.console.clear(); self.progress.setRange(0, 5); self.progress.setValue(0)
        self.stage_label.setText("Running recompilation pipeline...")
        prog, fargs = self._tool_cmd("gt4_recomp_apk.py", args)
        self._launch(prog, fargs, self._on_build_done)

    def _on_build_done(self, code):
        if code == 0:
            self.progress.setValue(5); self.stage_label.setText("Build complete!")
            self.append_log("\n[SUCCESS] Build finished.\n", "#4ade80")
        else:
            self.stage_label.setText(f"Build failed (exit {code}).")
            self.append_log(f"\n[ERROR] Exited with code {code}.\n", "#f87171")

    def start_self_tests(self):
        self.console.clear()
        self._test_queue = [
            "tests/selftest.py", "tests/test_core.py", "tests/test_extract.py",
            "tests/test_features.py", "tests/test_threads.py", "tests/test_discovery.py",
        ]
        self.progress.setRange(0, len(self._test_queue)); self.progress.setValue(0)
        self._next_test()

    def _next_test(self):
        if not self._test_queue:
            self.stage_label.setText("All 6 test suites passed!"); self.append_log("\n[ALL TESTS PASSED]\n", "#4ade80"); self._set_busy(False); return
        nxt = self._test_queue.pop(0)
        self.stage_label.setText(f"Running {nxt}..."); self.append_log(f"\n=== {nxt} ===\n", "#38bdf8")
        prog, args = self._tool_cmd(nxt, [])
        self._launch(prog, args, self._on_test_done)

    def _on_test_done(self, code):
        if code != 0:
            self._test_queue.clear(); self.stage_label.setText(f"Self-test failed (exit {code})."); return
        self.progress.setValue(6 - len(self._test_queue))
        self._next_test()

    def _launch(self, prog, args, on_done):
        self._set_busy(True)
        self.proc = QProcess(self)
        env = QProcessEnvironment.systemEnvironment(); env.insert("PYTHONUNBUFFERED", "1")
        self.proc.setProcessEnvironment(env)
        self.proc.readyReadStandardOutput.connect(self._on_stdout)
        self.proc.readyReadStandardError.connect(self._on_stderr)
        self.proc.finished.connect(lambda code, _st: (self._set_busy(False) if not self._test_queue else None, on_done(code)))
        self.proc.start(prog, args)

    def _on_stdout(self):
        txt = bytes(self.proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        for line in txt.splitlines():
            for i in range(1, 6):
                if f"[{i}/5]" in line:
                    self.progress.setValue(i - 1); self.stage_label.setText(line.replace("==>", "").strip())
        self.append_log(txt, "#e2e8f0")

    def _on_stderr(self):
        self.append_log(bytes(self.proc.readAllStandardError()).decode("utf-8", errors="replace"), "#fbbf24")

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(24, 28, 36))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(235, 240, 245))
    pal.setColor(QPalette.ColorRole.Base, QColor(15, 23, 42))
    pal.setColor(QPalette.ColorRole.Text, QColor(235, 240, 245))
    pal.setColor(QPalette.ColorRole.Button, QColor(38, 45, 58))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(235, 240, 245))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(37, 99, 235))
    app.setPalette(pal)
    w = MainWindow(); w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
EOF
chmod +x gt4_recomp_gui.py

echo "==> [3/6] Setting up isolated Python build venv (.venv-appimage)..."
python3 -m venv .venv-appimage
source .venv-appimage/bin/activate
python -m pip install --upgrade pip wheel
python -m pip install "PySide6>=6.7.0" "pyinstaller>=6.6.0"

echo "==> [4/6] Freezing GUI + recompilation toolchain with PyInstaller..."
rm -rf build dist AppDir
pyinstaller \
    --noconfirm \
    --clean \
    --onedir \
    --windowed \
    --name gt4-recomp-gui \
    --add-data "tools:tools" \
    --add-data "runtime:runtime" \
    --add-data "tests:tests" \
    --add-data "android:android" \
    --add-data "r5900lift.py:." \
    --add-data "gt4_recomp_apk.py:." \
    gt4_recomp_gui.py

echo "==> [5/6] Assembling AppDir layout..."
mkdir -p AppDir/usr/bin
cp -a dist/gt4-recomp-gui/* AppDir/usr/bin/
cp appimage/AppRun AppDir/AppRun
chmod +x AppDir/AppRun
cp appimage/arm-turismo4.desktop AppDir/arm-turismo4.desktop
cp appimage/arm-turismo4.svg AppDir/arm-turismo4.svg

if command -v desktop-file-validate >/dev/null 2>&1; then
    desktop-file-validate AppDir/arm-turismo4.desktop || true
fi

echo "==> [6/6] Fetching appimagetool-x86_64.AppImage & packing ARM-Turismo4-x86_64.AppImage..."
APPIMAGETOOL="./appimagetool-x86_64.AppImage"
if [[ ! -x "${APPIMAGETOOL}" ]]; then
    curl -fL "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage" \
        -o "${APPIMAGETOOL}"
    chmod +x "${APPIMAGETOOL}"
fi

OUT_APPIMAGE="ARM-Turismo4-x86_64.AppImage"
rm -f "${OUT_APPIMAGE}"

if [[ -e /dev/fuse ]] && "${APPIMAGETOOL}" --version >/dev/null 2>&1; then
    ARCH=x86_64 "${APPIMAGETOOL}" AppDir "${OUT_APPIMAGE}"
else
    ARCH=x86_64 "${APPIMAGETOOL}" --appimage-extract-and-run AppDir "${OUT_APPIMAGE}"
fi

chmod +x "${OUT_APPIMAGE}"
echo "SUCCESS: Built ${SCRIPT_DIR}/${OUT_APPIMAGE}"
