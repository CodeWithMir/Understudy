"""Social promo: 24 bars of the 112 bpm track (~53 s), cuts on the bar, in 16:9 and 9:16."""
import os as _os
HERE = _os.path.dirname(_os.path.abspath(__file__))
WORK = _os.environ.get("UNDERSTUDY_VIDEO_WORK", _os.path.join(HERE, "work"))
import os
import sys

from PIL import Image, ImageDraw

import vid

D = WORK
OUT = f"{D}/build_promo"
os.makedirs(OUT, exist_ok=True)
BAR = 4 * 60 / 112
MASTER = f"{D}/build_judge/master.mp4"
TAIL = 1.5

# (kind, bars, payload...)
SHOTS = [
    ("card", 2, "hook1"),
    ("card", 2, "hook2"),
    ("clip", 2, 8.0, 17.0, 1000, "1 · WATCH", "Record the task once. AI turns it into steps."),
    ("clip", 2, 21.0, 31.0, 900, "1 · WATCH", "Every step checked against the live page."),
    ("clip", 1, 57.0, 62.0, 850, "2 · BUILD", "Real Playwright code. Values in payload.json."),
    ("clip", 1, 74.5, 79.5, 900, "3 · GOVERN", "Over the limit? Nothing runs."),
    ("clip", 1, 82.5, 88.5, 900, "3 · GOVERN", "Fix it in place. Now it complies."),
    ("clip", 2, 91.0, 101.0, 860, "4 · EXECUTE", "Runs for real, in a restricted network."),
    ("clip", 1, 112.5, 117.5, 1180, "DONE", "The invoice is in the ledger."),
    ("clip", 1, 120.0, 126.0, 760, "5 · EXPORT", "Code, rules, evidence. Yours to keep."),
    ("clip", 1, 150.5, 156.5, 900, "MODEL ARMOR", "Prompt injection? Blocked in step one."),
    ("clip", 1, 172.0, 178.0, 1488, "MISSION CONTROL", "Admins see every AI call and every block."),
    ("card", 3, "stats"),
    ("card", 4, "end"),
]

TEAL, WHITE = (94, 234, 212, 255), (248, 250, 252, 255)


def wrap(d, text, f, maxw):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d.textlength(t, font=f) <= maxw:
            cur = t
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


def caption_h(path, tag, line):
    """16:9 lower-left caption panel (transparent PNG, panel only)."""
    im = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    f1, f2 = vid.font("ExtraBold", 30), vid.font("Bold", 50)
    lines = wrap(d, line, f2, 1000)
    w = max(max(d.textlength(l, font=f2) for l in lines), d.textlength(tag, font=f1)) + 80
    h = 40 + 40 + 62 * len(lines) + 26
    x, y = 60, 1080 - 70 - h
    d.rounded_rectangle([x, y, x + w, y + h], radius=26, fill=(3, 7, 18, 228), outline=(56, 189, 248, 170), width=2)
    d.rounded_rectangle([x + 40, y + 34, x + 46, y + 70], radius=3, fill=TEAL)
    d.text((x + 58, y + 32), tag, font=f1, fill=TEAL)
    for i, l in enumerate(lines):
        d.text((x + 40, y + 84 + 62 * i), l, font=f2, fill=WHITE)
    im.save(path)


def caption_v(path, tag, line):
    """9:16 headline block for the top of the frame (transparent PNG)."""
    im = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    f1, f2 = vid.font("ExtraBold", 40), vid.font("ExtraBold", 74)
    lines = wrap(d, line, f2, 960)
    y = 150
    tw = d.textlength(tag, font=f1)
    d.rounded_rectangle([60, y, 60 + tw + 56, y + 66], radius=33, fill=(8, 13, 26, 255), outline=(56, 189, 248, 200), width=3)
    d.text((88, y + 10), tag, font=f1, fill=TEAL)
    for i, l in enumerate(lines[:3]):
        d.text((60, y + 100 + 86 * i), l, font=f2, fill=WHITE)
    im.save(path)


def vertical_bg(path):
    """Static background + footer for the vertical clips."""
    import numpy as np
    W, H = 1080, 1920
    yy, xx = np.mgrid[0:H, 0:W]
    base = np.zeros((H, W, 3), float) + np.array([3, 7, 18])
    g1 = np.exp(-(((xx - 250) / 600) ** 2 + ((yy - 300) / 500) ** 2))[..., None] * np.array([20, 60, 90])
    g2 = np.exp(-(((xx - 850) / 600) ** 2 + ((yy - 1700) / 500) ** 2))[..., None] * np.array([10, 60, 55])
    im = Image.fromarray(np.clip(base + g1 + g2, 0, 255).astype("uint8")).convert("RGBA")
    d = ImageDraw.Draw(im)
    f = vid.font("Bold", 34)
    d.rounded_rectangle([60, 1822, 112, 1874], radius=13, fill=(14, 165, 233, 255))
    d.text((74, 1826), "U", font=vid.font("ExtraBold", 36), fill=WHITE)
    d.text((128, 1828), "Understudy", font=f, fill=WHITE)
    d.text((1020 - d.textlength("Watch once. Automate safely.", font=vid.font("Medium", 28)), 1834),
           "Watch once. Automate safely.", font=vid.font("Medium", 28), fill=(148, 163, 184, 255))
    im.convert("RGB").save(path)


def build(orient):
    W, H = (1920, 1080) if orient == "h" else (1080, 1920)
    parts = []
    cards = []
    total = 0.0
    cum = 0
    for i, sh in enumerate(SHOTS):
        # frame-exact cut points so every cut lands on the bar line (no drift over 14 cuts)
        f0 = round(cum * BAR * 30)
        cum += sh[1]
        f1 = round(cum * BAR * 30) + (round(TAIL * 30) if i == len(SHOTS) - 1 else 0)
        dur = (f1 - f0) / 30
        sh_out = f"{OUT}/{orient}_{i:02d}.mp4"
        if sh[0] == "card":
            c = f"{OUT}/{orient}_card_{sh[2]}.mp4"
            if not os.path.exists(c):
                cards.append((sh[2], f"file://{HERE}/promo.html?card={sh[2]}&o={orient}", dur, c))
        parts.append((sh, dur, sh_out))
        total += dur
    if cards:
        vid.record_cards(cards, W, H)
    bg = f"{OUT}/vbg.png"
    if orient == "v" and not os.path.exists(bg):
        vertical_bg(bg)
    files = []
    for i, (sh, dur, out) in enumerate(parts):
        last = i == len(parts) - 1
        if sh[0] == "card":
            c = f"{OUT}/{orient}_card_{sh[2]}.mp4"
            vid.segment(c, 0, dur, dur, out, fade_in=0.25 if i == 0 else 0, fade_out=1.4 if last else 0,
                        max_speed=1, min_speed=1)
        else:
            _, bars, a, b, cx, tag, line = sh
            L = b - a
            speed = L / dur
            png = f"{OUT}/{orient}_cap_{i:02d}.png"
            if orient == "h":
                caption_h(png, tag, line)
                crop = "crop=1440:810:250:0,scale=1920:1080:flags=lanczos," if tag == "DONE" else ""
                fc = (f"[0:v]{crop}setpts=(PTS-STARTPTS)/{speed:.5f},fps=30,tpad=stop_mode=clone:stop_duration=2[v];"
                      f"[1:v]format=rgba,fade=t=in:st=0:d=0.22:alpha=1[c];"
                      f"[v][c]overlay=x=0:y='max(0,(0.22-t))*160':format=auto:shortest=1,"
                      f"fade=t=in:st=0:d=0.12[o]")
                vid.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{a:.3f}", "-t", f"{L:.3f}", "-i", MASTER,
                         "-loop", "1", "-i", png, "-filter_complex", fc, "-map", "[o]", "-t", f"{dur:.3f}", "-an",
                         "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", "-r", "30", out])
            else:
                caption_v(png, tag, line)
                x0 = int(min(max(cx - 432, 0), 1920 - 864))
                fc = (f"[0:v]crop=864:1080:{x0}:0,scale=1080:1350:flags=lanczos,setpts=(PTS-STARTPTS)/{speed:.5f},"
                      f"fps=30,tpad=stop_mode=clone:stop_duration=2[v];"
                      f"[1:v]loop=-1:1:0,setpts=N/30/TB,fps=30[bg];"
                      f"[bg][v]overlay=0:470:shortest=1[b];"
                      f"[2:v]format=rgba,fade=t=in:st=0:d=0.22:alpha=1[c];"
                      f"[b][c]overlay=x='-max(0,(0.22-t))*200':y=0:format=auto,fade=t=in:st=0:d=0.12[o]")
                vid.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{a:.3f}", "-t", f"{L:.3f}", "-i", MASTER,
                         "-i", bg, "-loop", "1", "-i", png, "-filter_complex", fc, "-map", "[o]", "-t", f"{dur:.3f}",
                         "-an", "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", "-r", "30", out])
        files.append(out)
        print(orient, i, sh[0], round(dur, 2), flush=True)
    joined = f"{OUT}/{orient}_joined.mp4"
    vid.concat(files, joined)
    import numpy as np
    import soundfile as sf
    m, sr = sf.read(f"{D}/audio/promo_music.wav", always_2d=True)
    n = int(total * 48000)
    m = m[:n]
    fo = int(2.5 * 48000)
    m[-fo:] *= np.linspace(1, 0, fo)[:, None]
    name = "Understudy_promo_16x9.mp4" if orient == "h" else "Understudy_promo_9x16.mp4"
    vid.mux(joined, m, f"{D}/{name}", lufs=-14)
    print("total", round(total, 2))


if __name__ == "__main__":
    for o in (sys.argv[1:] or ["h", "v"]):
        build(o)
