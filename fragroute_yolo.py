"""FRAGROUTE offline object detector -- YOLOX (Apache-2.0) via ONNX Runtime (MIT).

OFFLINE ONLY. This runs object detection on RECORDED clips and on the app's OWN
UI captures -- never on a live in-match feed. It powers post-match VOD review
(where fights happened, time-on-target, peek timing) and makes the app's own OCR
more reliable by locating UI elements. It does NOT feed the player live combat
information.

Licensing is deliberately commercial-safe: YOLOX is Apache-2.0 and ONNX Runtime
is MIT. We do NOT use Ultralytics YOLOv5/v8/v11 (AGPL-3.0), which would force
open-sourcing the app or a paid license.

Sidecar layout (next to the exe, like llm/ sd/ stt/):
  yolo/
    *.onnx         -- a YOLOX model exported the standard way (raw outputs)
    classes.txt    -- optional label names, one per line (defaults to COCO-80)

Runs on the SECONDARY GPU (GTX 1650 SUPER) via the DirectML execution provider
when present, else CPU -- it never touches the 4070 that renders the game. Lazy:
nothing loads until the first analyze call. Degrades to "unavailable" cleanly if
onnxruntime / numpy aren't installed or no model is present. Pure stdlib + those.
"""
import os
import threading
from pathlib import Path

APP_YOLO_BUILD = "yolo-1"

YOLO_DIR = None            # set by the engine; default <module|exe>/yolo
_INPUT = (640, 640)        # YOLOX default input (h, w)
_LOCK = threading.Lock()
_STATE = {"session": None, "tried": False, "classes": None, "provider": None,
          "input": _INPUT, "error": None}

# COCO-80 fallback labels (used until a FragPunk-trained classes.txt is dropped in)
_COCO = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
]


def _base_dir():
    if YOLO_DIR:
        return Path(YOLO_DIR)
    import sys
    base = (Path(sys.executable).parent if getattr(sys, "frozen", False)
            else Path(__file__).parent)
    return base / "yolo"


def find_model():
    """The YOLOX .onnx model path, or None."""
    d = _base_dir()
    if d.exists():
        for p in sorted(d.glob("*.onnx")):
            return str(p)
    return None


def _load_classes():
    d = _base_dir()
    f = d / "classes.txt"
    if f.exists():
        try:
            names = [ln.strip() for ln in f.read_text(encoding="utf-8").splitlines()
                     if ln.strip()]
            if names:
                return names
        except Exception:
            pass
    return list(_COCO)


def available():
    """True if onnxruntime + numpy import and a model file exists. Never raises."""
    if not find_model():
        return False
    try:
        import numpy            # noqa: F401
        import onnxruntime      # noqa: F401
        return True
    except Exception:
        return False


def _ensure_session():
    """Lazily build the ORT session on the 1650 SUPER (DirectML) or CPU. Returns
    the session or None. Thread-safe; caches the result + any error."""
    with _LOCK:
        if _STATE["session"] is not None:
            return _STATE["session"]
        if _STATE["tried"]:
            return None
        _STATE["tried"] = True
        model = find_model()
        if not model:
            _STATE["error"] = "no .onnx model in the yolo folder"
            return None
        try:
            import onnxruntime as ort
        except Exception as e:
            _STATE["error"] = "onnxruntime not installed (%s)" % e
            return None
        # Prefer DirectML (runs on the 1650 SUPER without CUDA); fall back to CPU.
        avail = []
        try:
            avail = ort.get_available_providers()
        except Exception:
            avail = []
        # CPU by default for STABILITY. DirectML enumerates ALL DX adapters incl.
        # this machine's AMD iGPU, whose driver (amdxc64.dll) HARD-CRASHES the
        # process (0xc0000005) when ORT runs a model on it -- and adapter indexing
        # in the packaged exe isn't the same as from source, so device_id is not a
        # safe way to dodge it. CPU is slower but never crashes. Opt back into GPU
        # only once we can reliably select an NVIDIA adapter. (os.environ flag for
        # power users: FRAGROUTE_YOLO_DML=1)
        providers = []
        if os.environ.get("FRAGROUTE_YOLO_DML") == "1" and "DmlExecutionProvider" in avail:
            providers.append(("DmlExecutionProvider", {"device_id": 1}))
        providers.append("CPUExecutionProvider")
        try:
            so = ort.SessionOptions()
            so.intra_op_num_threads = 2          # keep CPU pressure low for the game
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            sess = ort.InferenceSession(model, sess_options=so, providers=providers)
        except Exception as e:
            _STATE["error"] = "session init failed: %s" % e
            return None
        _STATE["session"] = sess
        _STATE["classes"] = _load_classes()
        try:
            _STATE["provider"] = sess.get_providers()[0]
            shp = sess.get_inputs()[0].shape       # [1,3,H,W] when static
            if len(shp) == 4 and isinstance(shp[2], int) and isinstance(shp[3], int):
                _STATE["input"] = (int(shp[2]), int(shp[3]))
        except Exception:
            pass
        return sess


# --------------------------------------------------------------------------
# YOLOX pre/post-processing (standard demo pipeline -- raw-output export).
# --------------------------------------------------------------------------
def _preproc(pil_img, input_size):
    """Letterbox-resize to input_size with 114 padding, CHW float32. Returns
    (tensor, ratio). Standard YOLOX preproc: NO /255 and NO mean/std."""
    import numpy as np
    img = pil_img.convert("RGB")
    w0, h0 = img.size
    r = min(input_size[0] / h0, input_size[1] / w0)
    nw, nh = int(round(w0 * r)), int(round(h0 * r))
    resized = img.resize((nw, nh))
    padded = np.ones((input_size[0], input_size[1], 3), dtype=np.float32) * 114.0
    padded[:nh, :nw, :] = np.asarray(resized, dtype=np.float32)
    padded = padded.transpose(2, 0, 1)            # HWC -> CHW
    padded = np.ascontiguousarray(padded[None], dtype=np.float32)  # add batch
    return padded, r


def _decode(outputs, input_size, p6=False):
    """Decode raw YOLOX grid outputs into (cx,cy,w,h) at input scale."""
    import numpy as np
    grids, strides_out = [], []
    strides = [8, 16, 32] if not p6 else [8, 16, 32, 64]
    hsizes = [input_size[0] // s for s in strides]
    wsizes = [input_size[1] // s for s in strides]
    for hs, ws, st in zip(hsizes, wsizes, strides):
        xv, yv = np.meshgrid(np.arange(ws), np.arange(hs))
        grid = np.stack((xv, yv), 2).reshape(1, -1, 2)
        grids.append(grid)
        strides_out.append(np.full((1, grid.shape[1], 1), st))
    grids = np.concatenate(grids, 1)
    strides_out = np.concatenate(strides_out, 1)
    outputs = outputs.copy()
    outputs[..., :2] = (outputs[..., :2] + grids) * strides_out
    outputs[..., 2:4] = np.exp(outputs[..., 2:4]) * strides_out
    return outputs


def _nms(boxes, scores, iou_thr):
    import numpy as np
    x1, y1 = boxes[:, 0], boxes[:, 1]
    x2, y2 = boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0.0, xx2 - xx1)
        h = np.maximum(0.0, yy2 - yy1)
        inter = w * h
        ovr = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
        order = order[1:][ovr <= iou_thr]
    return keep


def detect_image(image_path, conf_thr=0.35, iou_thr=0.45, max_det=50):
    """Run YOLOX on ONE image. Returns a list of detections:
    [{label, conf, box:[x1,y1,x2,y2]}], in ORIGINAL-image pixel coords.
    Empty list on any failure (never raises)."""
    try:
        import numpy as np
        from PIL import Image
    except Exception:
        return []
    sess = _ensure_session()
    if sess is None or not image_path or not os.path.exists(image_path):
        return []
    try:
        img = Image.open(image_path)
        w0, h0 = img.size
        input_size = _STATE.get("input", _INPUT)
        tensor, ratio = _preproc(img, input_size)
        inp_name = sess.get_inputs()[0].name
        out = sess.run(None, {inp_name: tensor})[0]   # [1, N, 5+ncls]
        preds = _decode(np.asarray(out, dtype=np.float32), input_size)[0]
        boxes_xywh = preds[:, :4]
        obj = preds[:, 4:5]
        cls = preds[:, 5:]
        scores_all = obj * cls
        # xywh (center) -> xyxy, then back to original-image scale
        xyxy = np.empty_like(boxes_xywh)
        xyxy[:, 0] = boxes_xywh[:, 0] - boxes_xywh[:, 2] / 2.0
        xyxy[:, 1] = boxes_xywh[:, 1] - boxes_xywh[:, 3] / 2.0
        xyxy[:, 2] = boxes_xywh[:, 0] + boxes_xywh[:, 2] / 2.0
        xyxy[:, 3] = boxes_xywh[:, 1] + boxes_xywh[:, 3] / 2.0
        xyxy /= ratio
        cls_ids = scores_all.argmax(1)
        cls_scores = scores_all.max(1)
        m = cls_scores >= conf_thr
        if not m.any():
            return []
        b, s, c = xyxy[m], cls_scores[m], cls_ids[m]
        names = _STATE.get("classes") or _COCO
        dets = []
        for ci in np.unique(c):
            idx = np.where(c == ci)[0]
            keep = _nms(b[idx], s[idx], iou_thr)
            for k in keep:
                j = idx[k]
                x1 = float(max(0, min(w0, b[j, 0])))
                y1 = float(max(0, min(h0, b[j, 1])))
                x2 = float(max(0, min(w0, b[j, 2])))
                y2 = float(max(0, min(h0, b[j, 3])))
                label = names[int(ci)] if int(ci) < len(names) else str(int(ci))
                dets.append({"label": label, "conf": round(float(s[j]), 3),
                             "box": [round(x1), round(y1), round(x2), round(y2)]})
        dets.sort(key=lambda d: d["conf"], reverse=True)
        return dets[:max_det]
    except Exception as e:
        _STATE["error"] = "detect failed: %s" % e
        return []


def analyze_frames(frame_paths, conf_thr=0.35):
    """Run detection across an ordered list of clip frames (offline VOD review).
    Returns a summary: per-label counts, peak simultaneous count, and per-frame
    detections. Used by the coach to talk about a recorded clip -- not live."""
    if not frame_paths:
        return {"ok": False, "message": "no frames"}
    if not available():
        return {"ok": False, "message": "offline detector unavailable (add a YOLOX "
                "model to the yolo folder + install onnxruntime)"}
    counts, peak, per_frame = {}, 0, []
    for i, fp in enumerate(frame_paths):
        dets = detect_image(fp, conf_thr=conf_thr)
        per_frame.append({"frame": i, "dets": dets})
        peak = max(peak, len(dets))
        for d in dets:
            counts[d["label"]] = counts.get(d["label"], 0) + 1
    return {"ok": True, "frames": len(frame_paths), "labelCounts": counts,
            "peakObjects": peak, "perFrame": per_frame,
            "provider": _STATE.get("provider")}


def release():
    """Drop the loaded ONNX session to free RAM (e.g. when a match starts -- the
    offline detector isn't used in-match). Reloads lazily on next detect."""
    _STATE.update(session=None, tried=False)


def status():
    """Lightweight status for the Health tab / diagnostics."""
    return {
        "build": APP_YOLO_BUILD,
        "available": available(),
        "model": (Path(find_model()).name if find_model() else None),
        "provider": _STATE.get("provider"),
        "classes": len(_STATE.get("classes") or _load_classes()),
        "error": _STATE.get("error"),
        "note": "offline VOD/UI only -- never analyzes a live match",
    }
