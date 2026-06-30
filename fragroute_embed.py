"""FRAGROUTE CLIP embedding suggester -- few-shot class ID for the labeler.

No off-the-shelf model knows FragPunk's Lancers/weapons, and training a classifier
needs lots of data. Instead we embed each crop with a CLIP image encoder (ONNX, so
it runs in the app via onnxruntime) and match a NEW crop to the user's OWN labeled
crops by cosine similarity (k-NN). The top-K nearest classes become one-click
suggestions in the labeler. It needs NO retraining -- the moment you label a crop
it's in the gallery. ~80% top-5 overall, ~65% on Lancers/weapons (improves as the
gallery grows).

Runs on the 1650 SUPER (DirectML) or CPU -- never the game GPU. Pure stdlib +
numpy + onnxruntime + PIL. Engine sets CLIP_DIR (sidecar with clip_vitb32.onnx).
"""
import os
import threading
from pathlib import Path

APP_EMBED_BUILD = "embed-1"

CLIP_DIR = None
_LOCK = threading.Lock()
_STATE = {"session": None, "tried": False, "provider": None, "error": None}
_GALLERY = {"emb": None, "labels": None, "count": -1}     # cached crop embeddings
# CLIP ViT-B/32 normalization
_MEAN = (0.48145466, 0.4578275, 0.40821073)
_STD = (0.26862954, 0.26130258, 0.27577711)
_SIZE = 224


def _base_dir():
    if CLIP_DIR:
        return Path(CLIP_DIR)
    import sys
    base = (Path(sys.executable).parent if getattr(sys, "frozen", False)
            else Path(__file__).parent)
    return base / "clip"


def find_model():
    d = _base_dir()
    if d.exists():
        for p in sorted(d.glob("*.onnx")):
            return str(p)
    return None


def available():
    if not find_model():
        return False
    try:
        import numpy, onnxruntime   # noqa: F401
        return True
    except Exception:
        return False


def _session():
    with _LOCK:
        if _STATE["session"] is not None:
            return _STATE["session"]
        if _STATE["tried"]:
            return None
        _STATE["tried"] = True
        m = find_model()
        if not m:
            _STATE["error"] = "no clip onnx"; return None
        try:
            import onnxruntime as ort
        except Exception as e:
            _STATE["error"] = str(e); return None
        # CPU by default for STABILITY -- DirectML can run on this machine's AMD
        # iGPU whose driver (amdxc64.dll) hard-crashes the process. CLIP on CPU is
        # ~150-300ms/crop (fine for labeling). Opt into GPU: FRAGROUTE_EMBED_DML=1.
        provs = []
        try:
            if os.environ.get("FRAGROUTE_EMBED_DML") == "1" and "DmlExecutionProvider" in ort.get_available_providers():
                provs.append(("DmlExecutionProvider", {"device_id": 1}))
        except Exception:
            pass
        provs.append("CPUExecutionProvider")
        try:
            so = ort.SessionOptions(); so.intra_op_num_threads = 2
            s = ort.InferenceSession(m, sess_options=so, providers=provs)
        except Exception as e:
            _STATE["error"] = "session: %s" % e; return None
        _STATE["session"] = s
        try:
            _STATE["provider"] = s.get_providers()[0]
        except Exception:
            pass
        return s


def _preprocess(pil_crop):
    """CLIP preprocess: resize shortest side to 224, center-crop, normalize. CHW."""
    import numpy as np
    im = pil_crop.convert("RGB")
    w, h = im.size
    s = _SIZE / min(w, h)
    im = im.resize((max(_SIZE, int(round(w * s))), max(_SIZE, int(round(h * s)))))
    w, h = im.size
    left, top = (w - _SIZE) // 2, (h - _SIZE) // 2
    im = im.crop((left, top, left + _SIZE, top + _SIZE))
    a = np.asarray(im, dtype=np.float32) / 255.0
    a = (a - np.array(_MEAN, dtype=np.float32)) / np.array(_STD, dtype=np.float32)
    return np.ascontiguousarray(a.transpose(2, 0, 1)[None])   # [1,3,224,224]


def embed(pil_crop):
    """Normalized CLIP embedding for a crop, or None."""
    s = _session()
    if s is None:
        return None
    try:
        import numpy as np
        e = s.run(None, {s.get_inputs()[0].name: _preprocess(pil_crop)})[0][0]
        n = float((e * e).sum() ** 0.5)
        return (e / n) if n > 0 else e
    except Exception as ex:
        _STATE["error"] = "embed: %s" % ex
        return None


def build_gallery(force=False):
    """Embed every labeled (non-proposed) crop into the k-NN gallery. Cached; only
    rebuilds when the labeled-crop count changes (or force). Returns count."""
    try:
        import numpy as np
        import fragroute_dataset as DS
    except Exception:
        return 0
    import glob
    import json
    from PIL import Image
    ann_dir = DS._ann_dir(); img_dir = DS._images_dir()
    items = []   # (image_name, box, label)
    for f in sorted(ann_dir.glob("*.json")):
        try:
            a = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        name = a.get("image") or (f.stem + ".jpg")
        for b in a.get("boxes", []):
            if b.get("label") and not b.get("proposed"):
                items.append((name, b["box"], b["label"]))
    if not force and _GALLERY["count"] == len(items) and _GALLERY["emb"] is not None:
        return len(items)
    embs, labs = [], []
    cache = {}
    for name, box, label in items:
        p = img_dir / name
        if not p.exists():
            continue
        try:
            if name not in cache:
                cache[name] = Image.open(p).convert("RGB")
            x1, y1, x2, y2 = box
            if x2 - x1 < 8 or y2 - y1 < 8:
                continue
            e = embed(cache[name].crop((x1, y1, x2, y2)))
            if e is not None:
                embs.append(e); labs.append(label)
        except Exception:
            continue
    with _LOCK:
        _GALLERY["emb"] = (np.stack(embs) if embs else None)
        _GALLERY["labels"] = labs
        _GALLERY["count"] = len(items)
    return len(labs)


def suggest(image_path, box, k=5):
    """Top-k DISTINCT class suggestions for a crop, ranked by similarity to the
    labeled gallery. Returns [] if unavailable/empty. Never raises."""
    if not available():
        return []
    try:
        import numpy as np
        from PIL import Image
        build_gallery()
        G, labs = _GALLERY["emb"], _GALLERY["labels"]
        if G is None or not labs:
            return []
        x1, y1, x2, y2 = box
        with Image.open(image_path) as im:
            q = embed(im.convert("RGB").crop((x1, y1, x2, y2)))
        if q is None:
            return []
        sims = G @ q
        order = np.argsort(sims)[::-1]
        out = []
        for idx in order:
            lab = labs[int(idx)]
            if lab not in out:
                out.append(lab)
            if len(out) >= k:
                break
        return out
    except Exception as e:
        _STATE["error"] = "suggest: %s" % e
        return []


def release():
    """Free the CLIP session + gallery RAM (~350MB) when not labeling (e.g. on
    match start). Reloads + rebuilds the gallery lazily on next suggest."""
    with _LOCK:
        _STATE.update(session=None, tried=False)
        _GALLERY.update(emb=None, labels=None, count=-1)


def status():
    return {"build": APP_EMBED_BUILD, "available": available(),
            "model": (Path(find_model()).name if find_model() else None),
            "provider": _STATE.get("provider"), "gallery": _GALLERY.get("count"),
            "error": _STATE.get("error")}
