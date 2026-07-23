"""FRAGROUTE first-run model installer -- downloads the big AI sidecars a buyer
can't ship inside a ~90MB exe. Pure stdlib (urllib). Streams with progress, places
files in the right sidecar folders, extracts the ffmpeg zip. The runtime BINARIES
(llama.cpp / sd.cpp / whisper.cpp) ship WITH the app package AND are listed here so
the app can SELF-HEAL them -- antivirus quarantine or a partial unzip otherwise kills
vision/voice/image-gen with no in-app way to recover. The CLIP onnx is still not here:
it's generated, with no public URL to fetch it from.

Engine sets BASE_DIR (the folder that holds llm/ sd/ yolo/ stt/ + the exe).
"""
import os
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

APP_SETUP_BUILD = "setup-1"

BASE_DIR = None
_LOCK = threading.Lock()
_PROG = {}            # key -> {status, pct, mb, totalMb}
_RUNNING = {"on": False}

# Each: key, label, folder (relative to BASE_DIR), filename, url, approxMB, kind, required
MANIFEST = [
    {"key": "llm_smart", "label": "Coach LLM (Qwen2.5-14B, smart)", "folder": "llm",
     "filename": "Qwen2.5-14B-Instruct-Q4_K_M.gguf", "approxMB": 8990, "required": False,
     "url": "https://huggingface.co/bartowski/Qwen2.5-14B-Instruct-GGUF/resolve/main/Qwen2.5-14B-Instruct-Q4_K_M.gguf"},
    {"key": "llm_mid", "label": "Coach LLM (Phi-3.5-mini, in-game -- smarter, fits 4GB)", "folder": "llm",
     "filename": "Phi-3.5-mini-instruct-Q4_K_M.gguf", "approxMB": 2282, "required": False,
     "sha256": "e4165e3a71af97f1b4820da61079826d8752a2088e313af0c7d346796c38eff5",
     "url": "https://huggingface.co/bartowski/Phi-3.5-mini-instruct-GGUF/resolve/main/Phi-3.5-mini-instruct-Q4_K_M.gguf"},
    {"key": "llm_fast", "label": "Coach LLM (Qwen2.5-1.5B, lightweight in-game)", "folder": "llm",
     "filename": "qwen2.5-1.5b-instruct-q4_k_m.gguf", "approxMB": 1120, "required": True,
     "sha256": "6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e",
     "url": "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf"},
    {"key": "vision", "label": "Vision model (Qwen2-VL-2B)", "folder": "llm",
     "filename": "Qwen2-VL-2B-Instruct-Q4_K_M.gguf", "approxMB": 990, "required": False,
     "sha256": "5745685d2e607a82a0696c1118e56a2a1ae0901da450fd9cd4f161c6b62867d7",
     "url": "https://huggingface.co/ggml-org/Qwen2-VL-2B-Instruct-GGUF/resolve/main/Qwen2-VL-2B-Instruct-Q4_K_M.gguf"},
    {"key": "vision_mmproj", "label": "Vision projector (mmproj)", "folder": "llm",
     "filename": "mmproj-Qwen2-VL-2B-Instruct-f16.gguf", "approxMB": 1330, "required": False,
     "sha256": "ecb20cabcdd8dbc277de06bd6eb980aeb2adfaaba9f199a434e328d205675d03",
     "url": "https://huggingface.co/ggml-org/Qwen2-VL-2B-Instruct-GGUF/resolve/main/mmproj-Qwen2-VL-2B-Instruct-f16.gguf"},
    {"key": "imagegen", "label": "Image gen (SDXL base 1.0)", "folder": "sd",
     "filename": "sd_xl_base_1.0.safetensors", "approxMB": 6617, "required": False,
     "sha256": "31e35c80fc4829d14f90153f4c74cd59c90b779f6afe05a74cd6120b893f7e5b",
     "url": "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors"},
    {"key": "detector", "label": "Detector base (YOLOX-tiny, generic)", "folder": "yolo",
     "filename": "yolox_tiny.onnx", "approxMB": 20, "required": False,
     "sha256": "427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7",
     "url": "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx"},
    {"key": "voice", "label": "Voice STT (whisper small.en -- great freeform speech)", "folder": "stt",
     "filename": "ggml-small.en.bin", "approxMB": 466, "required": False,
     "sha256": "c6138d6d58ecc8322097e0f987c32f1be8bb0a18532a3f88f734d1bbf9c41e5d",
     "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin"},
    # FAST voice model: the persistent whisper-server (fragroute_voice.find_fast_model)
    # prefers base.en for low-latency conversational voice (~3x faster decode than
    # small.en -- verified 4.5s->2.0s per turn). Without this, a fresh buyer install
    # only has small.en and voice replies are noticeably slower. No embedded hash yet
    # (not independently re-verified against the upstream file) -> size-check only,
    # same as the 14B/ffmpeg entries below.
    {"key": "voice_fast", "label": "Voice STT -- fast (whisper base.en, snappy replies)", "folder": "stt",
     "filename": "ggml-base.en.bin", "approxMB": 141, "required": False,
     "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin"},
    {"key": "ffmpeg", "label": "Recorder/Video (ffmpeg LGPL)", "folder": ".",
     "filename": "ffmpeg.exe", "approxMB": 110, "required": True, "kind": "zip_ffmpeg",
     "url": "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-lgpl.zip"},
    # Offline IP->city database so the Live Game tab can name ANY server (incl.
    # off-VPN / non-Alibaba-LB raw match IPs), no account, no online lookup.
    # DB-IP City Lite is CC BY 4.0 (commercial OK with attribution -- see NOTICES).
    # The free build is published monthly, so the URL carries YYYY-MM (resolved at
    # download time, with a fallback to the previous month if this month isn't up yet).
    {"key": "geoip", "label": "Server locator (DB-IP City Lite, off-VPN names)", "folder": "geo",
     "filename": "dbip-city-lite.mmdb", "approxMB": 55, "required": False, "kind": "gz_monthly",
     "url": "https://download.db-ip.com/free/dbip-city-lite-{ym}.mmdb.gz"},

    # ---- RUNTIME ENGINES -----------------------------------------------------------------
    # These ship inside the release zip (package_release.py), so a fresh buyer already has
    # them. They're ALSO here so the app can SELF-HEAL: antivirus quarantining llama-server.exe
    # /sd-cli.exe is common and used to silently kill vision/voice/image-gen with no in-app way
    # back. Kind 'zip_bindir' extracts a whole release zip into a binary FOLDER (exe + its
    # DLLs); the URL is resolved from the upstream GitHub release at download time.
    {"key": "engine_llm_gpu", "label": "Engine: coach + vision LLM (llama.cpp, GPU/Vulkan)",
     "folder": "llm/vk", "filename": "llama-server.exe", "approxMB": 45, "required": False,
     "kind": "zip_bindir", "repo": "ggml-org/llama.cpp", "match": ["bin-win-vulkan-x64"]},
    {"key": "engine_llm_cpu", "label": "Engine: coach + vision LLM (llama.cpp, CPU fallback)",
     "folder": "llm/cpu", "filename": "llama-server.exe", "approxMB": 20, "required": True,
     "kind": "zip_bindir", "repo": "ggml-org/llama.cpp", "match": ["bin-win-cpu-x64"]},
    {"key": "engine_stt", "label": "Engine: voice commands (whisper.cpp)",
     "folder": "stt/bin", "filename": "whisper-server.exe", "approxMB": 10, "required": False,
     "kind": "zip_bindir", "repo": "ggml-org/whisper.cpp", "match": ["whisper-bin-x64"]},
    {"key": "engine_sd_gpu", "label": "Engine: image gen (stable-diffusion.cpp, GPU/Vulkan)",
     "folder": "sd/vk", "filename": "sd-cli.exe", "approxMB": 25, "required": False,
     "kind": "zip_bindir", "repo": "leejet/stable-diffusion.cpp", "match": ["win-vulkan-x64"],
     "rename": {"sd.exe": "sd-cli.exe"}},
    {"key": "engine_sd_cpu", "label": "Engine: image gen (stable-diffusion.cpp, CPU fallback)",
     "folder": "sd/cpu", "filename": "sd-cli.exe", "approxMB": 25, "required": False,
     "kind": "zip_bindir", "repo": "leejet/stable-diffusion.cpp", "match": ["win-cpu-x64"],
     "rename": {"sd.exe": "sd-cli.exe"}},
    # The CLIP embedder has no upstream download -- we EXPORT it (open_clip ViT-B/32 ->
    # onnx) because no public URL serves this exact file. So it self-heals from OUR OWN
    # release assets: attach clip_vitb32.onnx to any release and this finds it (any_release
    # scans past releases, so shipping a new version doesn't require re-uploading it).
    # Owner-only tooling (the labeler is admin-gated), hence not in the recommended set.
    {"key": "clip", "label": "Label suggester (CLIP ViT-B/32 embeddings)", "folder": "clip",
     "filename": "clip_vitb32.onnx", "approxMB": 335, "required": False, "kind": "gh_file",
     "repo": "ycbuzi/fragnetic", "match": ["clip_vitb32"], "ext": ".onnx", "anyRelease": True},
]


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _base():
    if BASE_DIR:
        return Path(BASE_DIR)
    import sys
    return (Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent)


def _dest(item):
    return _base() / item["folder"] / item["filename"]


def is_present(item):
    p = _dest(item)
    try:
        if item.get("kind") == "zip_bindir":
            # A runtime-engine FOLDER (key exe + its DLLs). Do NOT size-band the exe here:
            # llama-server.exe is a ~9KB launcher stub whose real code lives in the DLLs next
            # to it, so a size check would reject a perfectly good install. Validate instead
            # that the key exe exists AND its dependency files were extracted alongside it.
            if not p.exists():
                return False
            try:
                return sum(1 for _ in p.parent.iterdir()) >= 3
            except Exception:
                return True
        # present + within a tight band of the expected size. dest is only ever created from a
        # fully-downloaded (and, where a hash exists, sha256-verified) file, so this mainly
        # guards on-disk corruption / manual truncation. 0.85 (was 0.6) rejects a grossly
        # truncated file while still tolerating approxMB being a rounded estimate.
        return p.exists() and p.stat().st_size >= item.get("approxMB", 0) * 1048576 * 0.85
    except Exception:
        return False


def recommend(prof=None):
    """Match the model catalog to THIS PC's hardware: which tier fits, and the
    recommended default download set. A 4GB card gets Phi-3.5 (not the 14B) and skips
    SDXL; a 12GB+ card gets the 14B + SDXL; a no-GPU box gets the 1.5B on CPU. This is
    how 'we scan the hardware -> we know which model size fits' becomes concrete."""
    try:
        import fragroute_hardware
        p = prof or fragroute_hardware.detect()
    except Exception:
        p = prof or {}
    vram = p.get("bestVramGB") or 0
    ram = p.get("ramGB") or 0
    # 'smart' coach LLM (runs out-of-game) sized to the card:
    if vram >= 10:   smart = "llm_smart"    # Qwen2.5-14B
    elif vram >= 4:  smart = "llm_mid"      # Phi-3.5-mini
    else:            smart = "llm_fast"     # Qwen2.5-1.5B (CPU-friendly)
    imagegen_ok = vram >= 8                 # SDXL is heavy; crawls under ~8GB
    # the runtime engines are not optional -- a model with no engine to run it is dead weight
    rec = {smart, "llm_fast", "vision", "vision_mmproj", "detector", "voice",
           "engine_llm_cpu", "engine_stt"}
    if vram > 0:
        rec.add("engine_llm_gpu")
    if imagegen_ok:
        rec.add("imagegen")
        rec.add("engine_sd_gpu" if vram > 0 else "engine_sd_cpu")
    fit = {}
    for it in MANIFEST:
        k = it["key"]
        if k == "llm_smart":   fit[k] = "good" if vram >= 10 else ("heavy" if vram >= 6 else "toobig")
        elif k == "llm_mid":   fit[k] = "good" if vram >= 4 else "heavy"
        elif k == "imagegen":  fit[k] = "good" if imagegen_ok else "heavy"
        else:                  fit[k] = "good"
    gpu = (p.get("primaryGpu") or {}).get("name") or "Your GPU"
    if vram >= 10:   note = "%s (%gGB) runs the smart 14B coach + SDXL image gen." % (gpu, vram)
    elif vram >= 8:  note = "%s (%gGB) runs Phi-3.5 + SDXL image gen." % (gpu, vram)
    elif vram >= 4:  note = "%s (%gGB) runs Phi-3.5; SDXL image gen needs 8GB+ (skipped -- would be slow)." % (gpu, vram)
    elif vram > 0:   note = "%s (%gGB) runs the lightweight 1.5B coach; heavy AI features will be slow." % (gpu, vram)
    else:            note = "No GPU detected -- the 1.5B coach runs on CPU; image gen/vision will be slow."
    return {"vramGB": vram, "ramGB": ram, "smartLlm": smart,
            "recommendedKeys": sorted(rec), "fit": fit, "note": note}


def status():
    rec = recommend()
    fit = rec.get("fit", {}); recset = set(rec.get("recommendedKeys", []))
    out = []
    for it in MANIFEST:
        pr = _PROG.get(it["key"]) or {}
        out.append({"key": it["key"], "label": it["label"], "approxMB": it["approxMB"],
                    "required": it.get("required", False), "present": is_present(it),
                    "fit": fit.get(it["key"], "good"), "recommended": it["key"] in recset,
                    "status": pr.get("status", ""), "pct": pr.get("pct", 0)})
    missingMB = sum(i["approxMB"] for i in MANIFEST if not is_present(i))
    recMissingMB = sum(i["approxMB"] for i in MANIFEST if i["key"] in recset and not is_present(i))
    return {"build": APP_SETUP_BUILD, "items": out, "running": _RUNNING["on"],
            "missingMB": missingMB, "recMissingMB": recMissingMB,
            "recommendedKeys": sorted(recset), "hardwareNote": rec.get("note"),
            "present": sum(1 for i in MANIFEST if is_present(i)),
            "total": len(MANIFEST)}


def _resolve_monthly_url(template):
    """Fill {ym} with a recent YYYY-MM that actually exists. DB-IP publishes the
    free DB monthly; early in a month the new file may not be up yet, so we try
    this month and fall back through the previous two."""
    import datetime
    now = datetime.datetime.utcnow()
    for back in range(0, 3):
        y, m = now.year, now.month - back
        while m <= 0:
            m += 12
            y -= 1
        url = template.replace("{ym}", "%04d-%02d" % (y, m))
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "FRAGROUTE-setup"})
            with urllib.request.urlopen(req, timeout=20) as r:
                if getattr(r, "status", 200) == 200:
                    return url
        except Exception:
            continue
    return None


GITHUB_LATEST = "https://api.github.com/repos/%s/releases/latest"
GITHUB_RELEASES = "https://api.github.com/repos/%s/releases"


def _resolve_github_asset(repo, match, ext=".zip", any_release=False):
    """Release asset of `repo` whose filename ends in `ext` and contains EVERY string in
    `match`. The engine binaries are republished on every upstream build with the build
    number baked into the filename (llama-b10091-bin-win-vulkan-x64.zip), so a hardcoded
    URL would rot within days -- resolve it at download time, same as _resolve_monthly_url
    does for the monthly GeoIP DB.

    any_release=True also scans older releases when the LATEST one doesn't carry the asset.
    Our own CLIP model lives on whichever release we attached it to, and requiring it to be
    re-uploaded to every future release would break self-heal the next time we ship."""
    import json as _json

    def _get(url):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "FRAGROUTE-setup",
                                                       "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return _json.loads(r.read().decode("utf-8", "ignore"))
        except Exception:
            return None

    def _pick(rel):
        for a in ((rel or {}).get("assets") or []):
            name = (a.get("name") or "").lower()
            if name.endswith(ext) and all(m.lower() in name for m in match):
                return a.get("browser_download_url")
        return None

    hit = _pick(_get(GITHUB_LATEST % repo))
    if hit or not any_release:
        return hit
    for rel in (_get(GITHUB_RELEASES % repo) or []):
        hit = _pick(rel)
        if hit:
            return hit
    return None


def _download(item):
    key = item["key"]
    dest = _dest(item)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _PROG[key] = {"status": "downloading", "pct": 0, "mb": 0, "totalMb": item["approxMB"]}
    kind = item.get("kind", "")
    is_zip = kind == "zip_ffmpeg"
    is_gz = kind == "gz_monthly"
    is_bindir = kind == "zip_bindir"
    is_ghfile = kind == "gh_file"          # plain file pulled from a GitHub release asset
    url = item.get("url")
    if is_gz:
        url = _resolve_monthly_url(item["url"])
        if not url:
            _PROG[key] = {"status": "error: database not available yet, try later", "pct": 0}
            return False
    if is_bindir or is_ghfile:
        # filename carries the upstream build number, so resolve it from the live release
        url = _resolve_github_asset(item["repo"], item.get("match") or [],
                                    ext=item.get("ext", ".zip"),
                                    any_release=bool(item.get("anyRelease")))
        if not url:
            _PROG[key] = {"status": "error: no matching release build found (try later)", "pct": 0}
            return False
    tmp = str(dest) + (".gz" if is_gz else ".zipdl" if (is_zip or is_bindir) else ".part")
    # PRE-FLIGHT disk-space check: fail FAST with a clear message instead of
    # downloading for 20 minutes and dying on "[Errno 28] No space left". zip/gz
    # keep the compressed temp AND the extracted output on disk at the same time,
    # so they need ~2x; direct downloads just need the file + a little headroom.
    try:
        import shutil as _sh
        need_mb = int(item.get("approxMB", 0) or 0)
        if is_zip or is_gz or is_bindir:
            need_mb *= 2
        need_mb += 512  # headroom for filesystem overhead / other writes
        free_mb = _sh.disk_usage(str(dest.parent)).free // 1048576
        if free_mb < need_mb:
            _PROG[key] = {"status": "error: need %d MB free, only %d MB available -- free up space and retry"
                          % (need_mb, free_mb), "pct": 0}
            return False
    except Exception:
        pass  # can't measure -> proceed; the write will still error safely if truly full
    try:
        # RESUME: if a previous attempt left a partial .part on disk, ask the server to
        # continue from where we stopped (HTTP Range) instead of re-downloading GBs --
        # crucial for big models on a slow/flaky connection. If the server ignores Range
        # (responds 200 not 206), we transparently restart from scratch. Monthly-gz URLs
        # are resolved fresh each time, so we don't try to resume those.
        resume_from = 0
        if not is_gz:
            try:
                if os.path.exists(tmp):
                    resume_from = os.path.getsize(tmp)
            except Exception:
                resume_from = 0
        headers = {"User-Agent": "FRAGROUTE-setup"}
        if resume_from > 0:
            headers["Range"] = "bytes=%d-" % resume_from
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as r:
            status = getattr(r, "status", None) or r.getcode()
            clen = int(r.headers.get("Content-Length", 0) or 0)
            if resume_from > 0 and status == 206:
                mode, done, total = "ab", resume_from, resume_from + clen   # server honored resume
            else:
                mode, done, resume_from, total = "wb", 0, 0, clen           # fresh / server ignored Range
            with open(tmp, mode) as f:
                while True:
                    chunk = r.read(1 << 20)      # 1MB
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    pct = int(100 * done / total) if total else 0
                    _PROG[key] = {"status": ("resuming" if resume_from else "downloading"), "pct": pct,
                                  "mb": round(done / 1048576), "totalMb": round((total or item["approxMB"] * 1048576) / 1048576)}
        # Truncation guard: a dropped or proxy-closed stream can hit EOF early WITHOUT raising,
        # so the read-loop above would treat a partial file as complete. If the server gave us
        # a size, require we actually got all of it. Raising (not returning) reuses the except
        # handler below: the plain-file .part is KEPT so the next run resumes via Range, and
        # zip/gz temps are cleaned. This catches truncation even for items that ship no sha256
        # (the hash check further down only runs when a hash is present).
        if total and done < total:
            raise RuntimeError("incomplete download: got %d of %d bytes (will resume)" % (done, total))
        if is_zip:
            _PROG[key]["status"] = "extracting"
            with zipfile.ZipFile(tmp) as z:
                member = next((n for n in z.namelist() if n.lower().endswith("/bin/ffmpeg.exe") or n.lower().endswith("ffmpeg.exe")), None)
                if not member:
                    raise RuntimeError("ffmpeg.exe not found in zip")
                with z.open(member) as src, open(dest, "wb") as out:
                    while True:
                        b = src.read(1 << 20)
                        if not b:
                            break
                        out.write(b)
            os.remove(tmp)
        elif is_gz:
            _PROG[key]["status"] = "extracting"
            import gzip
            import shutil
            with gzip.open(tmp, "rb") as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            os.remove(tmp)
        elif is_bindir:
            # Whole-folder engine: extract the release zip and flatten the directory that
            # actually holds the key exe into our bin folder. Upstream zips vary (files at the
            # root, or nested under build/bin/), so locate the exe rather than assuming a layout.
            _PROG[key]["status"] = "extracting"
            import shutil
            import tempfile
            outdir = dest.parent
            outdir.mkdir(parents=True, exist_ok=True)
            want = item["filename"]
            aliases = list((item.get("rename") or {}).keys())
            with tempfile.TemporaryDirectory() as td:
                with zipfile.ZipFile(tmp) as z:
                    z.extractall(td)
                src_dir = None
                for root, _dirs, files in os.walk(td):
                    if want in files or any(a in files for a in aliases):
                        src_dir = root
                        break
                if src_dir is None:
                    raise RuntimeError("%s not found in the release zip" % want)
                for f in os.listdir(src_dir):
                    sp = os.path.join(src_dir, f)
                    if os.path.isfile(sp):
                        shutil.copy2(sp, outdir / f)
                # upstream ships sd.exe; the app launches it as sd-cli.exe
                for src_name, dst_name in (item.get("rename") or {}).items():
                    sp, dp = outdir / src_name, outdir / dst_name
                    if sp.exists() and not dp.exists():
                        shutil.copy2(sp, dp)
            os.remove(tmp)
        else:
            os.replace(tmp, dest)
        # integrity check against the known-good hash (where we have one)
        want = item.get("sha256")
        if want:
            _PROG[key] = {"status": "verifying", "pct": 100, "mb": _PROG[key].get("mb", 0), "totalMb": item["approxMB"]}
            try:
                got = _sha256(dest)
            except Exception as e:
                got = "err:%s" % e
            if got != want:
                try:
                    os.remove(dest)
                except Exception:
                    pass
                _PROG[key] = {"status": "error: checksum mismatch (re-download)", "pct": 0}
                return False
        _PROG[key] = {"status": "done", "pct": 100, "mb": _PROG[key].get("mb", 0), "totalMb": item["approxMB"]}
        return True
    except Exception as e:
        _PROG[key] = {"status": "error: %s" % str(e)[:80], "pct": 0}
        # KEEP the partial .part on a network error so the NEXT attempt resumes it (Range)
        # instead of restarting a multi-GB download. A corrupt tail self-heals: the final
        # sha256 mismatches, the file is deleted, and the following retry starts clean. Only
        # zip/gz temps are removed -- those must be a COMPLETE archive to extract, and they're
        # small enough that re-downloading is cheap.
        if is_zip or is_gz or is_bindir:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
        return False


def download(keys=None):
    """Download the given keys (or all MISSING) in a background thread. Returns
    immediately; poll status() for progress. One run at a time."""
    with _LOCK:
        if _RUNNING["on"]:
            return {"ok": False, "message": "a download is already running"}
        _RUNNING["on"] = True

    def _work():
        try:
            if keys:
                items = [i for i in MANIFEST if i["key"] in keys]
            else:
                items = [i for i in MANIFEST if not is_present(i)]
            for it in items:
                if not is_present(it):
                    _download(it)
        finally:
            _RUNNING["on"] = False
    threading.Thread(target=_work, daemon=True).start()
    return {"ok": True, "started": True}
