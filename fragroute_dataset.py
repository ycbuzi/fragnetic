"""FRAGROUTE training-data pipeline -- harvest frames, auto-bootstrap labels,
export a YOLO dataset for fine-tuning a FragPunk-specific detector.

Flow (offline, runs when you're not gaming):
  1. HARVEST frames from your recorded clips and (optional) online gameplay videos
     via yt-dlp -> dataset/images/.
  2. BOOTSTRAP draft boxes with the generic YOLOX (COCO) detector -> a reviewable
     JSON per image (boxes proposed, class left BLANK for you to assign). This is
     the auto-assist; the human step is confirming/relabeling to FragPunk classes.
  3. EXPORT the reviewed annotations to YOLO-format labels + dataset.yaml so a
     YOLOX fine-tune can train on them, then ONNX-export back into yolo/.

Class vocabulary = yolo/fragpunk_taxonomy.txt (verified online, no guessing).
Pure stdlib + subprocess (ffmpeg, yt-dlp) + the fragroute_yolo module. The engine
sets DATASET_DIR / FFMPEG / YT_DLP. Nothing here touches a live match.
"""
import json
import os
import subprocess
from pathlib import Path

APP_DATASET_BUILD = "dataset-1"

DATASET_DIR = None         # set by engine; default <appdata>/dataset
FFMPEG = None              # set by engine -> ffmpeg.exe (for frame extraction)
YT_DLP = None              # optional path to yt-dlp.exe; else tries PATH
_NOWIN = {"creationflags": 0x08000000} if os.name == "nt" else {}


def _root():
    if DATASET_DIR:
        return Path(DATASET_DIR)
    import sys
    base = (Path(sys.executable).parent if getattr(sys, "frozen", False)
            else Path(__file__).parent)
    return base / "dataset"


def _images_dir():
    d = _root() / "images"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ann_dir():
    d = _root() / "annotations"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _taxonomy_file():
    """The verified class list (lancer:/weapon: prefixed lines), or None."""
    import sys
    base = (Path(sys.executable).parent if getattr(sys, "frozen", False)
            else Path(__file__).parent)
    for p in (base / "yolo" / "fragpunk_taxonomy.txt",):
        if p.exists():
            return p
    return None


def _custom_file():
    """User-added classes live next to the DATASET (never clobbered by rebuilds)."""
    return _root() / "custom_classes.txt"


def _custom_classes():
    """User-defined classes as [(name, group)]. Lines are 'group:name' (group
    defaults to 'custom')."""
    f = _custom_file()
    out = []
    if f.exists():
        try:
            for ln in f.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                grp, name = ("custom", ln)
                if ":" in ln:
                    grp, name = ln.split(":", 1)
                out.append((name.strip(), grp.strip() or "custom"))
        except Exception:
            pass
    return out


def add_class(name, group="custom"):
    """Add a user-defined class so it shows in the labeler + trains. Idempotent;
    rejects names already present in the base taxonomy or custom list."""
    name = (name or "").strip()
    if not name:
        return {"ok": False, "message": "empty name"}
    existing = set(taxonomy())
    if name in existing:
        return {"ok": True, "message": "already exists", "name": name}
    f = _custom_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "a", encoding="utf-8") as fh:
        fh.write("%s:%s\n" % ((group or "custom").strip(), name))
    return {"ok": True, "name": name, "group": group}


def taxonomy():
    """Ordered list of class names: the verified base taxonomy + any user-added
    custom classes (appended, so existing class indices stay stable)."""
    f = _taxonomy_file()
    out = []
    if f:
        for ln in f.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            if ":" in ln:
                ln = ln.split(":", 1)[1]
            out.append(ln.strip())
    for name, _grp in _custom_classes():
        if name not in out:
            out.append(name)
    return out


# --------------------------------------------------------------------------
# 1) HARVEST frames
# --------------------------------------------------------------------------
def extract_frames(video_path, fps=1, prefix=None, max_frames=2000):
    """Pull frames from one video at `fps` frames/sec into dataset/images/.
    Returns the count written. Uses ffmpeg (no game hook). Never raises."""
    if not FFMPEG or not video_path or not os.path.exists(video_path):
        return 0
    out = _images_dir()
    stem = prefix or Path(video_path).stem
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in stem)[:40]
    pattern = str(out / ("%s_%%05d.jpg" % safe))
    args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video_path),
            "-vf", "fps=%g" % fps, "-frames:v", str(int(max_frames)),
            "-q:v", "3", pattern]
    try:
        subprocess.run(args, timeout=1800, **_NOWIN)
    except Exception:
        return 0
    return len(list(out.glob("%s_*.jpg" % safe)))


def _find_ytdlp():
    if YT_DLP and os.path.exists(YT_DLP):
        return YT_DLP
    from shutil import which
    return which("yt-dlp") or which("yt-dlp.exe")


def harvest_youtube(url, fps=1, max_frames=2000):
    """Download ONE online gameplay video (yt-dlp) to a temp file, extract frames,
    delete the video. Returns frames written (0 if yt-dlp missing). Honest note:
    online footage is for PERSONAL training reference; prefer your own clips for a
    model you ship. Never raises."""
    yt = _find_ytdlp()
    if not yt:
        return {"ok": False, "message": "yt-dlp not installed (pip install yt-dlp)"}
    import tempfile
    tmp = os.path.join(tempfile.gettempdir(), "fragroute_dl.mp4")
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
        # cap resolution to 1080p, single file, no playlist
        subprocess.run([yt, "-f", "bv*[height<=1080]+ba/b[height<=1080]",
                        "--no-playlist", "-o", tmp, url], timeout=1800, **_NOWIN)
    except Exception as e:
        return {"ok": False, "message": "download failed: %s" % e}
    if not os.path.exists(tmp):
        return {"ok": False, "message": "yt-dlp produced no file"}
    n = extract_frames(tmp, fps=fps, prefix="yt_" + str(abs(hash(url)) % 10**8),
                       max_frames=max_frames)
    try:
        os.remove(tmp)
    except Exception:
        pass
    return {"ok": True, "frames": n}


_VID_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".flv", ".ts")


def _harvest_state():
    f = _root() / "_harvested.json"
    try:
        import json
        return f, (json.loads(f.read_text(encoding="utf-8")) if f.exists() else {})
    except Exception:
        return f, {}


def auto_harvest(folders=None, fps=0.5, settle_s=25, max_frames=150, bootstrap_new=True):
    """Watch folder(s) for NEW recordings (OBS, the app's clips, anywhere) and
    auto-import them as YOLO training frames. Capture-source agnostic. Tracks what
    it already processed (by path+mtime) so it never re-imports. Skips files still
    being written (modified within settle_s). Optionally bootstraps draft labels on
    the new frames. Returns a summary. Never raises."""
    import json
    import time as _t
    folders = [f for f in (folders or []) if f]
    state_f, state = _harvest_state()
    now = _t.time()
    new_vids = new_frames = 0
    seen = []
    for folder in folders:
        d = Path(folder)
        if not d.exists():
            continue
        try:
            paths = list(d.rglob("*"))
        except Exception:
            continue
        for p in paths:
            if p.suffix.lower() not in _VID_EXTS:
                continue
            try:
                st = p.stat()
            except Exception:
                continue
            if st.st_size < 100_000:            # too small to be a real clip
                continue
            if now - st.st_mtime < settle_s:    # still being written -> skip for now
                continue
            key = str(p.resolve())
            seen.append(key)
            if state.get(key) == int(st.st_mtime):
                continue                        # already imported (unchanged)
            n = extract_frames(str(p), fps=fps, max_frames=max_frames)
            if n:
                # ONLY mark processed when we actually extracted frames -- otherwise a
                # transient failure (e.g. ffmpeg not ready) would permanently skip the
                # recording. This is what left the match clips at 0 frames before.
                state[key] = int(st.st_mtime)
                new_vids += 1
                new_frames += n
    try:
        state_f.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass
    out = {"ok": True, "newVideos": new_vids, "newFrames": new_frames,
           "totalImages": len(list(_images_dir().glob("*.jpg")))}
    if new_frames and bootstrap_new:
        try:
            out["bootstrap"] = bootstrap()
        except Exception as e:
            out["bootstrapError"] = str(e)
    return out


def add_image(src_path, prefix="scan"):
    """Add ONE screenshot (a Vision/scan/map capture) to the labeling pool -- copies
    it into dataset/images/ as a jpg so it appears in the Label tab. Returns the new
    image name or None. Lets every screen grab the owner takes become a labelable frame."""
    try:
        if not src_path or not os.path.exists(src_path):
            return None
        import time as _t
        out = _images_dir() / ("%s_%d.jpg" % (prefix, int(_t.time() * 1000)))
        try:
            from PIL import Image
            Image.open(src_path).convert("RGB").save(str(out), quality=88)
        except Exception:
            import shutil
            out = out.with_suffix(os.path.splitext(src_path)[1] or ".png")
            shutil.copy2(src_path, str(out))
        return out.name
    except Exception:
        return None


def harvest(video_paths=None, youtube_urls=None, fps=1):
    """Harvest from local clips + optional online videos. Returns a summary."""
    total = 0
    for vp in (video_paths or []):
        total += extract_frames(vp, fps=fps)
    yt = []
    for url in (youtube_urls or []):
        r = harvest_youtube(url, fps=fps)
        yt.append(r)
        if r.get("ok"):
            total += r.get("frames", 0)
    return {"ok": True, "framesWritten": total, "images": len(list(_images_dir().glob("*.jpg"))),
            "youtube": yt}


# --------------------------------------------------------------------------
# 2) BOOTSTRAP draft labels (auto-assist; human confirms classes)
# --------------------------------------------------------------------------
def bootstrap(limit=None, conf_thr=0.3):
    """Run the generic detector over un-annotated frames and write a reviewable
    draft annotation per image. COCO 'person' boxes become draft boxes with the
    class left BLANK -- you assign the FragPunk class in review. Returns counts."""
    try:
        import fragroute_yolo
    except Exception:
        return {"ok": False, "message": "detector module unavailable"}
    if not fragroute_yolo.available():
        return {"ok": False, "message": "detector not ready (need onnxruntime + a model)"}
    imgs = sorted(_images_dir().glob("*.jpg"))
    done = 0
    for img in imgs:
        ann = _ann_dir() / (img.stem + ".json")
        if ann.exists():
            continue                       # already drafted/reviewed -- skip
        dets = fragroute_yolo.detect_image(str(img), conf_thr=conf_thr)
        # SELF-LEARNING: if a TRAINED FragPunk model is loaded, its detections are
        # real classes -> pre-fill the label so you just CONFIRM (fast). With the
        # generic COCO model, only 'person' boxes are kept with a blank label for
        # you to assign. So each train->bootstrap->confirm->retrain cycle needs
        # less manual work as the model learns.
        tax = set(c["name"] for c in classes_grouped())
        boxes = []
        for d in dets:
            lab = d["label"]
            if lab in tax:                      # trained model -> propose the class
                boxes.append({"box": d["box"], "draft": lab, "conf": d["conf"], "label": lab})
            elif lab == "person":               # COCO bootstrap -> user assigns
                boxes.append({"box": d["box"], "draft": lab, "conf": d["conf"], "label": None})
        try:
            from PIL import Image
            with Image.open(img) as im:
                w, h = im.size
        except Exception:
            w = h = None
        ann.write_text(json.dumps({"image": img.name, "w": w, "h": h,
                                   "boxes": boxes, "reviewed": False}, indent=0),
                       encoding="utf-8")
        done += 1
        if limit and done >= limit:
            break
    return {"ok": True, "drafted": done, "totalImages": len(imgs)}


def _iou(a, b):
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1, ix2, iy2 = max(ax1, bx1), max(ay1, by1), min(ax2, bx2), min(ay2, by2)
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    ua = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / ua if ua > 0 else 0.0


def autofill(conf_thr=0.5, iou_thr=0.5, add_missed=False):
    """SELF-LEARNING back-fill -- CONSERVATIVE by default so proposals are accurate,
    not noisy. For any box you left UNLABELED, propose a class ONLY when a
    high-confidence (>=conf_thr) detection strongly overlaps it (>=iou_thr). By
    default it does NOT add new boxes (add_missed=False) -- auto-added boxes are the
    main source of wrong proposals. Result: fewer proposals, but the ones it makes
    are trustworthy. Everything proposed is proposed=True + reviewed=False so YOU
    confirm. Underfit models only fire on their well-learned classes (UI/generic),
    which is exactly what we want -- it won't guess Lancers it doesn't know.
    Needs a trained model (skips generic COCO). Never raises."""
    try:
        import fragroute_yolo
    except Exception:
        return {"ok": False, "message": "detector unavailable"}
    if not fragroute_yolo.available():
        return {"ok": False, "message": "no detector model"}
    tax = set(c["name"] for c in classes_grouped())
    frames = filled = added = 0
    for ann_f in sorted(_ann_dir().glob("*.json")):
        try:
            a = json.loads(ann_f.read_text(encoding="utf-8"))
        except Exception:
            continue
        img = _images_dir() / a.get("image", "")
        if not img.exists():
            continue
        dets = [d for d in fragroute_yolo.detect_image(str(img), conf_thr=conf_thr)
                if d["label"] in tax]              # only real FragPunk classes (skip COCO)
        if not dets:
            continue
        boxes = a.setdefault("boxes", [])
        used, changed = set(), False
        # 1) fill UNLABELED existing boxes by best IoU match
        for b in boxes:
            if b.get("label"):
                continue
            best, bi = iou_thr, -1
            for i, d in enumerate(dets):
                if i in used:
                    continue
                ov = _iou(b["box"], d["box"])
                if ov >= best:
                    best, bi = ov, i
            if bi >= 0:
                b["label"] = dets[bi]["label"]; b["proposed"] = True
                used.add(bi); filled += 1; changed = True
        # 2) OPTIONAL: add confident detections that don't overlap any existing box.
        # OFF by default -- these auto-added boxes are the noisiest part of auto-fill.
        if add_missed:
            for i, d in enumerate(dets):
                if i in used:
                    continue
                if any(_iou(d["box"], b["box"]) >= iou_thr for b in boxes):
                    continue
                boxes.append({"box": d["box"], "label": d["label"], "conf": d["conf"], "proposed": True})
                added += 1; changed = True
        if changed:
            a["reviewed"] = False                  # needs your confirmation
            ann_f.write_text(json.dumps(a), encoding="utf-8")
            frames += 1
    return {"ok": True, "framesUpdated": frames, "boxesFilled": filled, "boxesAdded": added}


# --------------------------------------------------------------------------
# 3) EXPORT a YOLO dataset from REVIEWED annotations
# --------------------------------------------------------------------------
def export_yolo(min_count=0):
    """Turn reviewed annotations (boxes with a non-null FragPunk `label`) into
    YOLO-format label files + classes.txt + dataset.yaml for a YOLOX fine-tune.
    Only images with reviewed=true and >=1 labelled box are included.

    min_count: drop classes with FEWER than this many labeled examples (and reindex
    the rest). Data-starved classes can't be learned and just make the model
    collapse to the dominant class -- training only on well-supported classes gives
    a model that actually detects multiple things. Starved classes auto-graduate in
    once they pass the threshold. min_count=0 keeps every class."""
    root = _root()
    # --- pass 1: count labeled examples per class (reviewed frames only) ---
    counts = {}
    reviewed = []
    for ann_f in sorted(_ann_dir().glob("*.json")):
        try:
            a = json.loads(ann_f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not a.get("reviewed"):
            continue
        reviewed.append(a)
        for b in a.get("boxes", []):
            lab = b.get("label")
            if lab:
                counts[lab] = counts.get(lab, 0) + 1
    if not counts:
        return {"ok": False, "message": "no reviewed labels yet"}
    # --- choose kept classes (>= min_count), ordered by the taxonomy for stability ---
    tax = taxonomy()
    kept = [c for c in tax if counts.get(c, 0) >= max(0, min_count)]
    # include any labeled class not in the taxonomy too (custom that meets threshold)
    for c in counts:
        if c not in tax and counts[c] >= max(0, min_count):
            kept.append(c)
    if not kept:
        return {"ok": False, "message": "no class meets min_count=%d" % min_count}
    cidx = {name: i for i, name in enumerate(kept)}
    dropped = sorted([c for c in counts if c not in cidx], key=lambda c: -counts[c])
    lbl_dir = root / "labels"
    lbl_dir.mkdir(parents=True, exist_ok=True)
    # clear stale label files so dropped-class frames don't linger
    for old in lbl_dir.glob("*.txt"):
        try:
            old.unlink()
        except Exception:
            pass
    used = 0
    for a in reviewed:
        w, h = a.get("w"), a.get("h")
        rows = []
        for b in a.get("boxes", []):
            lab = b.get("label")
            if not lab or not w or not h or lab not in cidx:
                continue
            x1, y1, x2, y2 = b["box"]
            cx = ((x1 + x2) / 2.0) / w; cy = ((y1 + y2) / 2.0) / h
            bw = (x2 - x1) / float(w); bh = (y2 - y1) / float(h)
            rows.append("%d %.6f %.6f %.6f %.6f" % (cidx[lab], cx, cy, bw, bh))
        if rows:
            (lbl_dir / (Path(a["image"]).stem + ".txt")).write_text("\n".join(rows), encoding="utf-8")
            used += 1
    (root / "classes.txt").write_text("\n".join(kept) + "\n", encoding="utf-8")
    yaml = ("# FRAGPUNK YOLOX dataset (auto-generated)\n"
            "path: %s\ntrain: images\nval: images\nnc: %d\nnames: [%s]\n"
            % (str(root).replace("\\", "/"), len(kept),
               ", ".join('"%s"' % c for c in kept)))
    (root / "dataset.yaml").write_text(yaml, encoding="utf-8")
    return {"ok": True, "labelledImages": used, "classes": len(kept),
            "keptClasses": kept, "droppedClasses": dropped, "minCount": min_count,
            "datasetYaml": str(root / "dataset.yaml")}


# --------------------------------------------------------------------------
# Review/labeling accessors (used by the in-app labeler UI)
# --------------------------------------------------------------------------
def list_frames():
    """Every harvested frame with its review state + box count (for the labeler)."""
    out = []
    for im in sorted(_images_dir().glob("*.jpg")):
        ann = _ann_dir() / (im.stem + ".json")
        reviewed, nb = False, 0
        if ann.exists():
            try:
                a = json.loads(ann.read_text(encoding="utf-8"))
                reviewed = bool(a.get("reviewed"))
                nb = len([b for b in (a.get("boxes") or []) if b.get("label")])
            except Exception:
                pass
        out.append({"name": im.name, "reviewed": reviewed, "labels": nb})
    return out


def get_annotation(name):
    """The annotation for one frame (creating an empty one from the image if the
    frame was never drafted). Returns None if the image doesn't exist."""
    im = _images_dir() / name
    if not im.exists():
        return None
    ann = _ann_dir() / (Path(name).stem + ".json")
    a = None
    if ann.exists():
        try:
            a = json.loads(ann.read_text(encoding="utf-8"))
        except Exception:
            a = None
    if a is None:
        w = h = None
        try:
            from PIL import Image
            with Image.open(im) as i:
                w, h = i.size
        except Exception:
            pass
        a = {"image": name, "w": w, "h": h, "boxes": [], "reviewed": False}
    return a


def save_annotation(name, data):
    """Persist a reviewed annotation. `data` = {boxes:[{box:[x1,y1,x2,y2],label}], reviewed}."""
    im = _images_dir() / name
    if not im.exists():
        return {"ok": False, "message": "no such frame"}
    _ann_dir().mkdir(parents=True, exist_ok=True)
    if not data.get("w") or not data.get("h"):
        try:
            from PIL import Image
            with Image.open(im) as i:
                data["w"], data["h"] = i.size
        except Exception:
            pass
    data["image"] = name
    (_ann_dir() / (Path(name).stem + ".json")).write_text(json.dumps(data), encoding="utf-8")
    return {"ok": True}


def frame_path(name):
    """Absolute path to a harvested frame image (for serving), or None."""
    im = _images_dir() / name
    return str(im) if im.exists() else None


def delete_frame(name):
    """Remove a harvested frame (junk/menu/loading shots) + its annotation, so it
    never enters training. Returns {ok, remaining}."""
    if not name or "/" in name or "\\" in name:
        return {"ok": False, "message": "bad name"}
    im = _images_dir() / name
    ann = _ann_dir() / (Path(name).stem + ".json")
    removed = False
    for p in (im, ann):
        try:
            if p.exists():
                p.unlink(); removed = True
        except Exception:
            pass
    return {"ok": removed, "remaining": len(list(_images_dir().glob("*.jpg")))}


def classes_grouped():
    """Class palette for the labeler: [{name, group}] from the taxonomy file
    (group = 'lancer' | 'weapon' from the line prefix)."""
    f = _taxonomy_file()
    out = []
    if not f:
        return out
    for ln in f.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        grp = "other"
        if ":" in ln:
            grp, ln = ln.split(":", 1)
            grp = grp.strip()
        out.append({"name": ln.strip(), "group": grp})
    have = {c["name"] for c in out}
    for name, grp in _custom_classes():        # user-added classes (appended)
        if name not in have:
            out.append({"name": name, "group": grp})
    return out


def status():
    imgs = list(_images_dir().glob("*.jpg")) if _root().exists() else []
    anns = list(_ann_dir().glob("*.json")) if _root().exists() else []
    reviewed = 0
    for a in anns:
        try:
            if json.loads(a.read_text(encoding="utf-8")).get("reviewed"):
                reviewed += 1
        except Exception:
            pass
    return {"build": APP_DATASET_BUILD, "images": len(imgs),
            "drafted": len(anns), "reviewed": reviewed,
            "classes": len(taxonomy()), "ytdlp": bool(_find_ytdlp()),
            "note": "offline training-data pipeline; review labels before export"}
