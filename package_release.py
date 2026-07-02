"""Assemble a SHIPPABLE FRAGROUTE bundle (dev tool, run manually).

Ships everything SMALL the app needs to run -- the exe, the LGPL ffmpeg, WireGuard,
the runtime binaries (llama.cpp / sd.cpp / whisper.cpp), the CLIP onnx (no public
URL), neutral data + notices. EXCLUDES the ~20GB AI MODELS (the buyer downloads
those from the in-app Setup tab) and the developer's PERSONAL data (logs, rank,
VPN configs, skins). Result: release/Fragnetic/ ready to zip + sell.

Run:  py -3 package_release.py
"""
import os
import shutil
from pathlib import Path

HERE = Path(__file__).parent
DIST = HERE / "dist"
OUT = HERE / "release" / "Fragnetic"

# explicit single files to ship (app + tools + notices)
FILES = [
    (DIST / "Fragnetic.exe", "Fragnetic.exe"),
    (DIST / "ffmpeg.exe", "ffmpeg.exe"),            # must be the LGPL build
    (DIST / "wireguard.exe", "wireguard.exe"),
    (DIST / "README.md", "README.md"),
    (HERE / "THIRD_PARTY_NOTICES.txt", "THIRD_PARTY_NOTICES.txt"),
    (HERE / "EULA.md", "EULA.md"),
    (HERE / "PRIVACY.md", "PRIVACY.md"),
    (HERE / "REFUND.md", "REFUND.md"),
    (HERE / "DISCLAIMER.md", "DISCLAIMER.md"),
    (DIST / "clip" / "clip_vitb32.onnx", "clip/clip_vitb32.onnx"),  # generated, no URL -> ship
    (DIST / "yolo" / "fragpunk_taxonomy.txt", "yolo/fragpunk_taxonomy.txt"),
]
# whole binary folders to ship (runtime engines, NOT models)
BIN_DIRS = ["llm/vk", "llm/cpu", "sd/vk", "sd/cpu", "stt/bin"]
# never ship: big models + dev's personal data
MODEL_EXT = (".gguf", ".safetensors", ".ckpt", ".pth", ".bin")
SKIP_NAMES = {"sd-v1-5.safetensors", "sd_xl_base_1.0.safetensors", "fragpunk_yolox.onnx",
              "yolox_tiny.onnx", "ggml-base.en.bin"}
# the ONLY executables the app actually launches. Every other .exe in the binary
# folders (benchmarks, quantizers, tests, wchess, parakeet, talk-llama...) is dead
# weight and looks unprofessional in a sold product. ALL .dll are kept regardless
# (they're shared deps -- dropping one would break loading).
KEEP_EXE = {"llama-server.exe", "sd-cli.exe", "whisper-cli.exe", "main.exe"}


def _copy_file(src, rel):
    dst = OUT / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.exists():
        shutil.copy2(src, dst)
        return src.stat().st_size
    print("  [!] missing:", src)
    return 0


def _copy_bindir(rel):
    src = DIST / rel
    if not src.exists():
        print("  [!] missing bin dir:", src)
        return 0
    total = 0
    for root, _dirs, files in os.walk(src):
        for f in files:
            fl = f.lower()
            if fl.endswith(MODEL_EXT) or f in SKIP_NAMES or fl.endswith(".zip"):
                continue                                  # skip any stray models/archives
            if fl.endswith(".exe") and f not in KEEP_EXE:
                continue                                  # drop unused toolchain executables
            sp = Path(root) / f
            rp = OUT / rel / sp.relative_to(src)
            rp.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sp, rp)
            total += sp.stat().st_size
    return total


def _rmtree_retry(path, tries=5):
    # Windows often holds a transient lock on freshly-written exes (AV scan, Explorer
    # preview). Retry a few times before giving up.
    import stat
    import time

    def _onerr(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass
    for i in range(tries):
        try:
            shutil.rmtree(path, onerror=_onerr)
            if not path.exists():
                return
        except Exception:
            pass
        time.sleep(0.5 * (i + 1))
    if path.exists():
        raise RuntimeError("could not clear %s -- close any window/AV scanning it" % path)


def main():
    if OUT.exists():
        _rmtree_retry(OUT)
    OUT.mkdir(parents=True)
    total = 0
    print("Packaging release ->", OUT)
    for src, rel in FILES:
        total += _copy_file(src, rel)
    for rel in BIN_DIRS:
        total += _copy_bindir(rel)
    # a short buyer-facing setup note
    (OUT / "SETUP.txt").write_text(
        "FRAGROUTE\n========\n\n"
        "1. Run Fragnetic.exe (click YES on the admin prompt).\n"
        "2. Open System > Setup. It shows what's ready and lets you DOWNLOAD the AI\n"
        "   models (~20GB total) -- pick what you want; they fill in in the background.\n"
        "3. For clip audio: enable 'Stereo Mix' (Windows Sound > Recording) or a virtual\n"
        "   audio cable. An NVIDIA GPU enables fast recording + local AI.\n\n"
        "All processing is local. See THIRD_PARTY_NOTICES.txt for licenses.\n",
        encoding="utf-8")
    # report
    n = sum(1 for _ in OUT.rglob("*") if _.is_file())
    print("DONE: %d files, %.0f MB (models NOT included -- via in-app Setup)" % (n, total / 1048576))
    print("Zip release/Fragnetic/ and ship it; buyer downloads models on first run.")


if __name__ == "__main__":
    main()
