"""Fragnetic demo-video builder.
Drives the running headless app (port 8796) with Playwright, screenshots a curated
feature tour using the owner's REAL data, composes on-brand 1600x900 frames with PIL,
and assembles a narrated-caption MP4 with ffmpeg (Ken Burns + fades). No credentials
needed: the app serves on 127.0.0.1 and we reveal the UI via its own _hideGate().
"""
import os, subprocess, sys
from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
URL = "http://127.0.0.1:8796/"
FF = os.path.join(HERE, "dist", "ffmpeg.exe")
OUT_DIR = os.path.join(HERE, "demo_frames")
RAW = os.path.join(OUT_DIR, "raw")
COMP = os.path.join(OUT_DIR, "comp")
SCENES = os.path.join(OUT_DIR, "scenes")
FINAL = os.path.join(HERE, "Fragnetic-demo.mp4")
W, H = 1600, 900

# (pane, title, subtitle, scroll-target-selector or None)
TOUR = [
    ("coach",   "AI Coach",                    "Private, on-device — chat, voice & vision. Nothing uploaded.", None),
    ("video",   "Recording & Highlights",      "Auto-capture, auto-montage, hardware-encoded on your GPU.", None),
    ("routing", "Region Intelligence",         "True ping to every region + a FragPunk-only split VPN.", None),
    ("locker",  "Skin Locker",                 "Auto-cropped gallery built from your own screenshots.", None),
    ("weapons", "Weapon Skins",                "Per-weapon catalog with your uploads, owned-flagged.", None),
    ("setup",   "System Check  ·  brand-new", "See exactly what your PC supports before you commit.", "#sysCheckCard"),
    ("__settings__", "Settings",              "Tune routing, capture, coach & appearance — all local.", None),
    ("health",  "Health Diagnostics",          "Every subsystem live — and provably zero FPS impact.", None),
]

BG = (11, 12, 18)
CYAN = (34, 211, 238)
PINK = (236, 72, 153)
WHITE = (238, 240, 245)
MUTE = (150, 156, 170)


def font(sz, bold=True):
    for name in (("segoeuib.ttf" if bold else "segoeui.ttf"), "arialbd.ttf", "arial.ttf"):
        p = os.path.join("C:/Windows/Fonts", name)
        if os.path.exists(p):
            return ImageFont.truetype(p, sz)
    return ImageFont.load_default()


def capture():
    os.makedirs(RAW, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True, args=["--disable-gpu", "--force-color-profile=srgb"])
        pg = b.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
        pg.goto(URL, wait_until="domcontentloaded")
        pg.wait_for_timeout(2500)
        pg.evaluate("try{_hideGate();}catch(e){}")
        # Represent what a REAL customer sees, not the owner's god-mode: force a trial
        # (Pro-preview) entitlement so admin-only chrome disappears (build tag, dev
        # readouts, the Label tab). We set ENT directly instead of calling
        # refreshEntitlement() so the owner-machine admin tier can't race back in.
        pg.evaluate("""()=>{try{
            window.ENT={tier:'trial',tierLabel:'Free trial',trialActive:true,trialDaysLeft:14,features:{},sources:['trial']};
            document.body.classList.remove('admin-mode');
            var tb=document.getElementById('acctTier'); if(tb){tb.textContent='Free trial';tb.className='tier-badge t-trial';}
            if(typeof refreshTabsForTier==='function')refreshTabsForTier();
            if(typeof applyFeatureLocks==='function')applyFeatureLocks();
        }catch(e){}}""")
        # dim the user's decorative match wallpaper so the FUNCTIONAL UI reads clearly on camera
        pg.add_style_tag(content="#appWallpaper{opacity:.10!important}"
                                 "#appWallpaperDim{display:block!important;background:rgba(7,6,10,.9)!important}")
        pg.wait_for_timeout(1200)
        for pane, _t, _s, scroll in TOUR:
            if pane == "__settings__":
                # open the Settings drawer over the routing pane, on the Routing & VPN tab
                pg.evaluate("()=>{try{switchTab('routing');openSettings();"
                            "if(typeof showSetTab==='function')showSetTab('routing');}catch(e){}}")
                pg.wait_for_timeout(1600)
                pg.screenshot(path=os.path.join(RAW, "settings.png"))
                pg.evaluate("()=>{try{closeSettings()}catch(e){}}")
                pg.wait_for_timeout(300)
                print("captured settings")
                continue
            pg.evaluate("(n)=>{try{switchTab(n)}catch(e){}}", pane)
            pg.wait_for_timeout(2600)
            if pane == "coach":
                # seed a REAL exchange so the tab shows the coach actually answering
                pg.evaluate("()=>{try{var i=document.getElementById('aiInput');"
                            "i.value='Give me one quick tip to improve my aim in FragPunk.';"
                            "aiSend();}catch(e){}}")
                try:
                    pg.wait_for_function(
                        "()=>{var b=document.querySelectorAll('#aiChat .bot');"
                        "return !window._aiBusy && b.length && b[b.length-1].textContent.trim()!=='\\u2026' "
                        "&& b[b.length-1].textContent.trim().length>20;}",
                        timeout=60000)
                except Exception:
                    pass
                pg.wait_for_timeout(700)
            if scroll:
                pg.evaluate("(s)=>{try{var e=document.querySelector(s);if(e)e.scrollIntoView({block:'center'})}catch(e){}}", scroll)
            else:
                pg.evaluate("()=>{try{var a=document.querySelector('.tab-pane.active');if(a)a.scrollTop=0;window.scrollTo(0,0);}catch(e){}}")
            pg.wait_for_timeout(900)
            pg.screenshot(path=os.path.join(RAW, pane + ".png"))
            print("captured", pane)
        b.close()


def _rounded(img, rad):
    from PIL import ImageOps  # noqa
    mask = Image.new("L", img.size, 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, img.size[0], img.size[1]], rad, fill=255)
    img.putalpha(mask)
    return img


def _wrap(draw, text, fnt, maxw):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=fnt) <= maxw:
            cur = t
        else:
            lines.append(cur); cur = w
    if cur:
        lines.append(cur)
    return lines


def wordmark(draw, x, y):
    f = font(30)
    draw.text((x, y), "FRAG", font=f, fill=CYAN)
    w = draw.textlength("FRAG", font=f)
    draw.text((x + w, y), "NETIC", font=f, fill=PINK)


def compose():
    os.makedirs(COMP, exist_ok=True)
    frames = []
    # title card
    title = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(title)
    for i in range(H):  # subtle vertical gradient
        c = int(11 + 10 * (i / H))
        d.line([(0, i), (W, i)], fill=(c, c + 1, c + 7))
    f1, f2, f3 = font(92), font(34, False), font(26, False)
    d.text((W/2, H/2 - 90), "FRAG", font=f1, anchor="mm", fill=CYAN)
    fw = d.textlength("FRAG", font=f1)
    d.text((W/2 + fw, H/2 - 90), "NETIC", font=f1, anchor="mm", fill=PINK)
    # recentre the two-tone wordmark
    title = Image.new("RGB", (W, H), BG); d = ImageDraw.Draw(title)
    for i in range(H):
        c = int(11 + 10 * (i / H)); d.line([(0, i), (W, i)], fill=(c, c + 1, c + 7))
    whole = d.textlength("FRAGNETIC", font=f1); sx = W/2 - whole/2
    d.text((sx, H/2 - 130), "FRAG", font=f1, fill=CYAN)
    d.text((sx + d.textlength("FRAG", font=f1), H/2 - 130), "NETIC", font=f1, fill=PINK)
    d.text((W/2, H/2 + 10), "your FragPunk companion", font=f2, anchor="mm", fill=WHITE)
    d.text((W/2, H/2 + 70), "Live demo — real data, running on a real PC", font=f3, anchor="mm", fill=MUTE)
    tp = os.path.join(COMP, "00_title.png"); title.save(tp); frames.append(tp)

    ft, fs = font(44), font(26, False)
    for idx, (pane, ttl, sub, _sc) in enumerate(TOUR, 1):
        fname = "settings" if pane == "__settings__" else pane
        shot = Image.open(os.path.join(RAW, fname + ".png")).convert("RGB")
        if shot.size != (W, H):
            shot = shot.resize((W, H))
        canvas = shot.copy()
        d = ImageDraw.Draw(canvas, "RGBA")
        # bottom caption gradient
        grad_h = 240
        for i in range(grad_h):
            a = int(235 * (i / grad_h))
            d.line([(0, H - grad_h + i), (W, H - grad_h + i)], fill=(7, 8, 12, a))
        # top accent bar (app already shows its own wordmark top-left)
        d.rectangle([0, 0, W, 6], fill=(*CYAN, 255))
        d.rectangle([0, 6, W, 40], fill=(7, 8, 12, 130))
        # accent bar + title + subtitle
        by = H - 150
        d.rounded_rectangle([40, by + 6, 48, by + 54], 4, fill=(*PINK, 255))
        d.text((66, by), ttl, font=ft, fill=WHITE)
        for j, ln in enumerate(_wrap(d, sub, fs, W - 130)):
            d.text((66, by + 62 + j * 34), ln, font=fs, fill=MUTE)
        d.text((W - 210, H - 44), "%d / %d" % (idx, len(TOUR)), font=font(20, False), fill=MUTE)
        cp = os.path.join(COMP, "%02d_%s.png" % (idx, pane)); canvas.save(cp); frames.append(cp)
        print("composed", pane)

    # outro
    outro = Image.new("RGB", (W, H), BG); d = ImageDraw.Draw(outro)
    for i in range(H):
        c = int(11 + 10 * (i / H)); d.line([(0, i), (W, i)], fill=(c, c + 1, c + 7))
    whole = d.textlength("FRAGNETIC", font=f1); sx = W/2 - whole/2
    d.text((sx, H/2 - 120), "FRAG", font=f1, fill=CYAN)
    d.text((sx + d.textlength("FRAG", font=f1), H/2 - 120), "NETIC", font=f1, fill=PINK)
    d.text((W/2, H/2 + 20), "Private. Local. No FPS cost.", font=font(32, False), anchor="mm", fill=WHITE)
    d.text((W/2, H/2 + 80), "ycbuzi.github.io/fragnetic", font=font(26, False), anchor="mm", fill=CYAN)
    op = os.path.join(COMP, "99_outro.png"); outro.save(op); frames.append(op)
    return frames


def _enc_ok(codec):
    try:
        subprocess.run([FF, "-hide_banner", "-encoders"], capture_output=True, text=True, errors="replace").stdout
        return True
    except Exception:
        return False


def encode(frames):
    os.makedirs(SCENES, exist_ok=True)
    vcodec = "h264_nvenc"
    scene_files = []
    for i, fp in enumerate(frames):
        dur = 3.0 if (i == 0 or i == len(frames) - 1) else 4.2
        out = os.path.join(SCENES, "s%02d.mp4" % i)
        fo = max(0.4, dur - 0.4)
        vf = ("zoompan=z='min(zoom+0.0007,1.06)':d=%d:s=%dx%d:fps=30,"
              "fade=t=in:st=0:d=0.4,fade=t=out:st=%.2f:d=0.4,format=yuv420p"
              % (int(dur * 30), W, H, fo))
        cmd = [FF, "-y", "-loop", "1", "-i", fp, "-t", "%.2f" % dur, "-r", "30",
               "-vf", vf, "-c:v", vcodec, "-b:v", "6M", "-pix_fmt", "yuv420p", out]
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if r.returncode != 0:
            # fallback to software encoder
            cmd[cmd.index("-c:v") + 1] = "libopenh264"
            r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
            if r.returncode != 0:
                print("scene encode failed", fp, r.stderr[-400:]); sys.exit(2)
        scene_files.append(out)
        print("scene", i)
    lst = os.path.join(SCENES, "list.txt")
    with open(lst, "w", encoding="utf-8") as fh:
        for s in scene_files:
            fh.write("file '%s'\n" % s.replace("\\", "/"))
    r = subprocess.run([FF, "-y", "-f", "concat", "-safe", "0", "-i", lst,
                        "-c", "copy", "-movflags", "+faststart", FINAL],
                       capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        print("concat failed", r.stderr[-500:]); sys.exit(3)
    print("WROTE", FINAL)


if __name__ == "__main__":
    step = sys.argv[1] if len(sys.argv) > 1 else "all"
    if step in ("all", "capture"):
        capture()
    if step in ("all", "compose", "encode"):
        fr = compose()
        if step != "compose":
            encode(fr)
