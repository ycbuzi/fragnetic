"""FRAGROUTE YOLOX trainer -- fine-tune a FragPunk detector, export to ONNX.

STANDALONE dev tool (NOT bundled in the app). Run it on the 4070 when you're not
gaming. It turns the labelled dataset (produced by fragroute_dataset.export_yolo)
into a trained model and drops the ONNX + classes.txt into yolo/ so the app's
detector becomes FragPunk-smart.

Licensing stays commercial-clean: YOLOX is Apache-2.0 (NOT Ultralytics/AGPL).

PREREQUISITES (one-time, heavy -- ~2.5GB, GPU):
  # IMPORTANT: use Python 3.13, NOT 3.14 -- PyTorch has no 3.14 wheels yet.
  #   on this machine:  "C:\\Program Files\\Python313\\python.exe"
  py313 -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
  py313 -m pip install yolox onnx onnxruntime pycocotools
  # COCO-pretrained tiny weights (transfer-learn from these) -- already downloaded
  # to files/yolox_tiny.pth:
  #   https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.pth
  # RUN this script WITH 3.13:  "C:\\Program Files\\Python313\\python.exe" fragroute_train.py ...

WORKFLOW:
  1. Harvest + label frames   -> fragroute_dataset (review boxes, assign classes)
  2. fragroute_dataset.export_yolo()   -> dataset/{images,labels,classes.txt}
  3. python fragroute_train.py --data <dataset_dir> --epochs 100
       -> converts YOLO->COCO, writes a YOLOX Exp, trains, exports ONNX to yolo/

This script does the data-wrangling itself (tested, no GPU needed) and shells out
to YOLOX for the actual train + export (GPU). If torch/yolox aren't installed it
prints exactly what to install and stops -- it never fails silently.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _read_classes(data_dir):
    f = Path(data_dir) / "classes.txt"
    if not f.exists():
        raise SystemExit("No classes.txt in %s -- run fragroute_dataset.export_yolo() first." % data_dir)
    return [c.strip() for c in f.read_text(encoding="utf-8").splitlines() if c.strip()]


def yolo_to_coco(data_dir, val_frac=0.15):
    """Convert YOLO-format labels (labels/*.txt: cls cx cy w h, normalized) +
    images/ into two COCO JSONs (train/val). Returns (train_json, val_json, n).
    Pure stdlib + PIL; no GPU. This is the part that must be exactly right."""
    try:
        from PIL import Image
    except Exception:
        raise SystemExit("Pillow needed: pip install pillow")
    data = Path(data_dir)
    classes = _read_classes(data)
    img_dir, lbl_dir = data / "images", data / "labels"
    pairs = []
    for lbl in sorted(lbl_dir.glob("*.txt")):
        img = None
        for ext in (".jpg", ".jpeg", ".png"):
            cand = img_dir / (lbl.stem + ext)
            if cand.exists():
                img = cand
                break
        if img:
            pairs.append((img, lbl))
    if not pairs:
        raise SystemExit("No image/label pairs found under %s -- nothing to train on." % data)
    n_val = max(1, int(len(pairs) * val_frac)) if len(pairs) > 6 else 0
    splits = {"val": pairs[:n_val], "train": pairs[n_val:]}
    cats = [{"id": i, "name": c} for i, c in enumerate(classes)]
    out = {}
    for split, items in splits.items():
        coco = {"images": [], "annotations": [], "categories": cats}
        ann_id = 1
        for img_id, (img, lbl) in enumerate(items, 1):
            with Image.open(img) as im:
                W, H = im.size
            coco["images"].append({"id": img_id, "file_name": img.name, "width": W, "height": H})
            for line in lbl.read_text(encoding="utf-8").splitlines():
                p = line.split()
                if len(p) != 5:
                    continue
                c, cx, cy, bw, bh = int(p[0]), *[float(x) for x in p[1:]]
                x = (cx - bw / 2) * W
                y = (cy - bh / 2) * H
                w, h = bw * W, bh * H
                coco["annotations"].append({
                    "id": ann_id, "image_id": img_id, "category_id": c,
                    "bbox": [x, y, w, h], "area": w * h, "iscrowd": 0})
                ann_id += 1
        ann_dir = data / "annotations"
        ann_dir.mkdir(exist_ok=True)
        jp = ann_dir / ("instances_%s.json" % ("train2017" if split == "train" else "val2017"))
        jp.write_text(json.dumps(coco), encoding="utf-8")
        out[split] = jp
    # YOLOX expects images under <data>/train2017 and val2017 -- symlink/copy
    for split, items in splits.items():
        sub = data / ("train2017" if split == "train" else "val2017")
        sub.mkdir(exist_ok=True)
        for img, _ in items:
            dst = sub / img.name
            if not dst.exists():
                try:
                    os.link(img, dst)        # hardlink (cheap); copy if it fails
                except Exception:
                    shutil.copy2(img, dst)
    return out.get("train"), out.get("val"), len(pairs), len(classes)


_EXP_TEMPLATE = '''# Auto-generated YOLOX Exp for FRAGPUNK
import os
from yolox.exp import Exp as MyExp

class Exp(MyExp):
    def __init__(self):
        super().__init__()
        self.num_classes = {num_classes}
        self.depth = {depth}
        self.width = {width}
        self.input_size = ({input}, {input})
        self.test_size = ({input}, {input})
        self.mosaic_scale = (0.5, 1.5)
        self.data_dir = r"{data_dir}"
        self.train_ann = "instances_train2017.json"
        self.val_ann = "instances_val2017.json"
        self.max_epoch = {epochs}
        self.data_num_workers = 0   # 0 = Windows-safe (no dataloader multiprocessing hangs)
        self.eval_interval = 10
        self.exp_name = "fragpunk_yolox"
'''

# model size -> (depth, width, coco-weights filename)
_MODELS = {
    "nano":  (0.33, 0.25, "yolox_nano.pth"),
    "tiny":  (0.33, 0.375, "yolox_tiny.pth"),
    "s":     (0.33, 0.50, "yolox_s.pth"),
    "m":     (0.67, 0.75, "yolox_m.pth"),
}


def write_exp(data_dir, num_classes, epochs, input=416, depth=0.33, width=0.375):
    exp = Path(data_dir) / "fragpunk_exp.py"
    exp.write_text(_EXP_TEMPLATE.format(num_classes=num_classes, data_dir=str(data_dir),
                                        epochs=epochs, input=int(input), depth=depth, width=width),
                   encoding="utf-8")
    return exp


def _have(mod):
    try:
        __import__(mod)
        return True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="Fine-tune a FragPunk YOLOX detector.")
    ap.add_argument("--data", required=True, help="dataset dir (with images/, labels/, classes.txt)")
    ap.add_argument("--epochs", type=int, default=100)
    ap.add_argument("--model", default="tiny", choices=list(_MODELS), help="model size (tiny/s/m/nano)")
    ap.add_argument("--weights", default=None, help="COCO weights (defaults per --model)")
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--input", type=int, default=416, help="input size (416 fast, 640 better on small HUD)")
    ap.add_argument("--prepare-only", action="store_true", help="only convert data, don't train")
    args = ap.parse_args()

    depth, width, default_w = _MODELS[args.model]
    if not args.weights:
        args.weights = default_w
    print("== 1) Converting YOLO labels -> COCO (model=%s, %dpx, %d ep) ==" % (args.model, args.input, args.epochs))
    train_j, val_j, n, ncls = yolo_to_coco(args.data)
    print("   %d labelled images, %d classes. train=%s val=%s" % (n, ncls, train_j, val_j))
    exp = write_exp(args.data, ncls, args.epochs, input=args.input, depth=depth, width=width)
    print("   wrote Exp: %s" % exp)
    if args.prepare_only:
        return

    # --- training needs torch + yolox (GPU) ---
    missing = [m for m in ("torch", "yolox") if not _have(m)]
    if missing:
        print("\n[!] Training needs: %s" % ", ".join(missing))
        print("    pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121")
        print("    pip install yolox onnx")
        print("    + COCO weights: yolox_tiny.pth from the YOLOX releases page.")
        print("    Then re-run (the COCO conversion above is already done).")
        return
    if not os.path.exists(args.weights):
        print("\n[!] Missing %s (COCO-pretrained). Download yolox_tiny.pth from:" % args.weights)
        print("    https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.pth")
        return

    print("\n== 2) Training (GPU; run when not gaming) ==")
    train_cmd = [sys.executable, "-m", "yolox.tools.train", "-f", str(exp),
                 "-d", "1", "-b", str(args.batch), "--fp16", "-c", args.weights]
    print("   " + " ".join(train_cmd))
    rc = subprocess.call(train_cmd)
    if rc != 0:
        print("[!] training exited %d" % rc)
        return

    print("\n== 3) Export checkpoint -> ONNX -> yolo/ ==")
    out_root = Path("YOLOX_outputs") / "fragpunk_yolox"
    ckpt = out_root / "best_ckpt.pth"
    if not ckpt.exists():
        ckpt = out_root / "latest_ckpt.pth"
    onnx_out = Path(__file__).parent / "yolo" / "fragpunk_yolox.onnx"
    onnx_out.parent.mkdir(exist_ok=True)
    try:
        export_onnx_native(str(exp), str(ckpt), str(onnx_out), input_size=(args.input, args.input))
        # ship the class list next to the model so the app labels detections right
        shutil.copy2(Path(args.data) / "classes.txt", onnx_out.parent / "classes.txt")
        print("\nDONE -> %s (+ classes.txt). The app's detector is now FragPunk-trained." % onnx_out)
    except Exception as e:
        print("[!] ONNX export failed: %s" % e)


def export_onnx_native(exp_file, ckpt, out_path, input_size=(416, 416)):
    """Export a trained YOLOX checkpoint to ONNX using torch.onnx.export directly.
    yolox's own export_onnx tool calls the removed torch.onnx._export (broken on
    torch>=2.6), so we do it natively. decode_in_inference=False -> RAW grid
    outputs, which is exactly what fragroute_yolo._decode expects."""
    import torch
    from yolox.exp import get_exp
    exp = get_exp(exp_file)
    model = exp.get_model()
    ckpt_d = torch.load(ckpt, map_location="cpu")
    model.load_state_dict(ckpt_d["model"] if "model" in ckpt_d else ckpt_d)
    model.eval()
    if hasattr(model, "head"):
        model.head.decode_in_inference = False
    dummy = torch.zeros(1, 3, input_size[0], input_size[1])
    torch.onnx.export(model, dummy, out_path, input_names=["images"],
                      output_names=["output"], opset_version=11,
                      dynamic_axes=None)
    return out_path


if __name__ == "__main__":
    main()
