"""Produce CLEAN, shippable reference assets (no personal data) for packaging.

The runtime dist\fragroute_icons.json accumulates the user's OWN uploads -- most
importantly a custom 'wallpaper' slot (their personal background image). Bundling that
into the exe / release ships the owner's wallpaper to every customer. This strips it,
keeping ONLY generic reference slots (rank emblems, weapon-type glyphs, built-in
wallpaper presets). Weapon-skins are NOT shipped at all (a customer starts empty).

Writes ship_assets\fragroute_icons.json. Build + package scripts bundle THIS, never the
runtime file.
"""
import json, os, re

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "dist", "fragroute_icons.json")
OUTDIR = os.path.join(HERE, "ship_assets")
OUT = os.path.join(OUTDIR, "fragroute_icons.json")

# reference slots that are the SAME for every user (safe to ship). Everything else --
# above all a bare 'wallpaper' (the user's custom upload) -- is personal and dropped.
_REF = re.compile(r"^(rank:|type:|wallpaper:)")


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    slots = {}
    if os.path.exists(SRC):
        try:
            slots = (json.load(open(SRC, encoding="utf-8")).get("slots") or {})
        except Exception:
            slots = {}
    kept, dropped = {}, []
    for k, v in slots.items():
        if _REF.match(k):
            kept[k] = v
        else:
            dropped.append(k)
    json.dump({"slots": kept}, open(OUT, "w", encoding="utf-8"), separators=(",", ":"))
    print("ship icons: kept %d reference slots, DROPPED personal: %s" % (len(kept), dropped or "none"))
    # hard guard: never let a bare 'wallpaper' (custom upload) through
    assert "wallpaper" not in kept, "personal wallpaper slot leaked into ship assets!"
    print("wrote", OUT)


if __name__ == "__main__":
    main()
