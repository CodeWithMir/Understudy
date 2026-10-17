"""Small video toolkit: frames->master, animated HTML cards, labelled segments, captions, audio mix."""
import os as _os
HERE = _os.path.dirname(_os.path.abspath(__file__))
WORK = _os.environ.get("UNDERSTUDY_VIDEO_WORK", _os.path.join(HERE, "work"))
import asyncio
import json
import os
import re
import subprocess

import numpy as np
import soundfile as sf
from PIL import Image, ImageDraw, ImageFont
from scipy.signal import resample_poly

FPS = 30
FONT = "/usr/share/fonts/opentype/inter/Inter-{}.otf"


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(" ".join(cmd)[:400] + "\n" + r.stderr[-2000:])
    return r


def font(weight, size):
    return ImageFont.truetype(FONT.format(weight), size)


def frames_to_video(rec_dir, out, w, h, crf=15):
    tl = json.load(open(os.path.join(rec_dir, "timeline.json")))
    fr = tl["frames"]
    lines = []
    for i, (t, fn) in enumerate(fr):
        nxt = fr[i + 1][0] if i + 1 < len(fr) else tl["end"]
        lines.append(f"file '{os.path.join(rec_dir, 'frames', fn)}'\nduration {max(nxt - t, 0.001):.4f}")
    lines.append(f"file '{os.path.join(rec_dir, 'frames', fr[-1][1])}'")
    lst = out + ".txt"
    open(lst, "w").write("\n".join(lines))
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst,
         "-vf", f"fps={FPS},scale={w}:{h}:flags=lanczos,format=yuv420p", "-c:v", "libx264", "-crf", str(crf),
         "-preset", "medium", out])
    return fr[0][0], {n: t for t, n in tl["markers"]}


async def _record_cards(specs, w, h):
    """specs: [(name, url, seconds, out_mp4)] - records CSS-animated HTML with the CDP screencast."""
    from playwright.async_api import async_playwright
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from rec_lib import Recorder
    async with async_playwright() as p:
        b = await p.chromium.launch()
        for name, url, secs, out in specs:
            ctx = await b.new_context(viewport={"width": w, "height": h})
            pg = await ctx.new_page()
            await pg.goto("about:blank")
            d = out + "_rec"
            R = Recorder(d)
            await R.stage(pg)
            await pg.goto(url)
            R.mark("start")
            await pg.wait_for_timeout(int(secs * 1000) + 300)
            await R.stop()
            await ctx.close()
            t0, m = frames_to_video(d, out + ".full.mp4", w, h, crf=14)
            start = max(0.0, m["start"] - t0)
            run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", out + ".full.mp4", "-t", f"{secs:.3f}",
                 "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", "-r", str(FPS), out])
        await b.close()


def record_cards(specs, w, h):
    asyncio.run(_record_cards(specs, w, h))


def segment(src, a, b, dur, out, overlay_png=None, fade_in=0.0, fade_out=0.0, max_speed=3.0, min_speed=0.8,
            extra_vf=""):
    """Cut [a,b] from src and fit it to `dur` seconds by a uniform speed change (clamped), freezing the last frame
    when the footage is too short."""
    L = b - a
    speed = min(max(L / dur, min_speed), max_speed)
    used = min(L, dur * speed)
    f = 1.0 / speed
    vf = f"setpts=(PTS-STARTPTS)*{f:.5f},fps={FPS},tpad=stop_mode=clone:stop_duration={dur + 1:.2f}"
    if extra_vf:
        vf += "," + extra_vf
    if fade_in:
        vf += f",fade=t=in:st=0:d={fade_in}"
    if fade_out:
        vf += f",fade=t=out:st={dur - fade_out:.3f}:d={fade_out}"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{a:.3f}", "-t", f"{used:.3f}", "-i", src]
    if overlay_png:
        cmd += ["-loop", "1", "-i", overlay_png, "-filter_complex", f"[0:v]{vf}[v];[v][1:v]overlay=0:0:format=auto[o]",
                "-map", "[o]"]
    else:
        cmd += ["-vf", vf]
    cmd += ["-t", f"{dur:.3f}", "-an", "-c:v", "libx264", "-crf", "15", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-r", str(FPS), out]
    run(cmd)
    return speed


def concat(parts, out):
    lst = out + ".txt"
    open(lst, "w").write("\n".join(f"file '{p}'" for p in parts))
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out])


# ---- captions --------------------------------------------------------------------------------------------------
def chunks(text, max_chars=78):
    """Split narration into caption-sized pieces at sentence / clause boundaries."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    out = []
    for p in parts:
        while len(p) > max_chars:
            cut = max(p.rfind(", ", 0, max_chars), p.rfind(": ", 0, max_chars))
            if cut < 25:
                cut = p.rfind(" ", 0, max_chars)
            out.append(p[: cut + 1].strip())
            p = p[cut + 1:].strip()
        if p:
            out.append(p)
    return out


def caption_events(text, start, dur):
    cs = chunks(text)
    weights = np.array([len(c) + 8 for c in cs], dtype=float)
    weights /= weights.sum()
    t = start
    ev = []
    for c, wgt in zip(cs, weights):
        d = dur * wgt
        ev.append((t, t + d, c))
        t += d
    return ev


def _ts_ass(t):
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _ts_srt(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_ass(events, path, w, h, size=40, margin_v=48, margin_lr=430, align=2):
    hdr = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Inter SemiBold,{size},&H00FFFFFF,&H00FFFFFF,&H40000000,&H40000000,0,0,0,0,100,100,0,0,3,14,0,{align},{margin_lr},{margin_lr},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [f"Dialogue: 0,{_ts_ass(a)},{_ts_ass(b - 0.04)},Cap,,0,0,0,,{t}" for a, b, t in events]
    open(path, "w").write(hdr + "\n".join(lines) + "\n")


def write_srt(events, path):
    out = []
    for i, (a, b, t) in enumerate(events, 1):
        out.append(f"{i}\n{_ts_srt(a)} --> {_ts_srt(b - 0.04)}\n{t}\n")
    open(path, "w").write("\n".join(out))


# ---- audio -----------------------------------------------------------------------------------------------------
def load48(path):
    x, sr = sf.read(path, always_2d=True)
    x = x.mean(axis=1) if x.shape[1] > 1 else x[:, 0]
    if sr != 48000:
        from math import gcd
        g = gcd(sr, 48000)
        x = resample_poly(x, 48000 // g, sr // g)
    return x


def narration_track(placements, total):
    """placements: [(wav_path, start_seconds)] -> mono float array at 48 kHz."""
    buf = np.zeros(int((total + 1) * 48000))
    for p, s in placements:
        x = load48(p)
        i = int(s * 48000)
        buf[i:i + len(x)] += x[: len(buf) - i]
    return buf[: int(total * 48000)]


def mix(voice, music_path, total, music_db=-21.0, duck_db=-9.0, music_only=None):
    """Voice (mono) + stereo music, music ducked while the voice speaks. Returns stereo array."""
    m, sr = sf.read(music_path, always_2d=True)
    n = int(total * 48000)
    if len(m) < n:
        m = np.concatenate([m, np.zeros((n - len(m), 2))])
    m = m[:n]
    v = voice[:n] if len(voice) >= n else np.concatenate([voice, np.zeros(n - len(voice))])
    vr = np.sqrt(np.mean(v[np.abs(v) > 1e-3] ** 2)) if np.any(np.abs(v) > 1e-3) else 0.1
    mr = np.sqrt(np.mean(m ** 2)) + 1e-9
    g = vr / mr * 10 ** (music_db / 20)
    # envelope of voice activity, smoothed (attack 60 ms, release 500 ms)
    act = (np.abs(v) > 0.01).astype(float)
    win = int(0.35 * 48000)
    act = np.convolve(act, np.ones(win) / win, mode="same") > 0.02
    env = act.astype(float)
    k = int(0.4 * 48000)
    env = np.convolve(env, np.ones(k) / k, mode="same")
    duck = 10 ** ((duck_db * env) / 20)
    mm = m * (g * duck)[:, None]
    out = mm + v[:, None]
    return out


def mux(video, audio_arr, out, srt=None, lufs=-16):
    wav = out + ".mix.wav"
    sf.write(wav, audio_arr, 48000, subtype="PCM_24")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", video, "-i", wav, "-map", "0:v", "-map", "1:a",
           "-af", f"loudnorm=I={lufs}:TP=-1.5:LRA=11", "-ar", "48000", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
           "-movflags", "+faststart", "-shortest", out]
    run(cmd)


def label_png(path, w, h, title, sub=None, pos="bl", scale=1.0):
    """Transparent full-frame PNG with a rounded label chip (and an optional small line under it)."""
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    f1 = font("Bold", int(26 * scale))
    f2 = font("Medium", int(19 * scale))
    tw = d.textlength(title, font=f1)
    pad_x, pad_y = int(22 * scale), int(14 * scale)
    cw, chh = int(tw + 2 * pad_x + 22 * scale), int(26 * scale + 2 * pad_y)
    sw = d.textlength(sub, font=f2) if sub else 0
    if pos == "bl":
        x, y = int(36 * scale), h - int(150 * scale)
    else:  # top-left
        x, y = int(36 * scale), int(36 * scale)
    d.rounded_rectangle([x, y, x + cw, y + chh], radius=int(14 * scale), fill=(8, 13, 26, 225),
                        outline=(56, 189, 248, 200), width=max(2, int(2 * scale)))
    d.ellipse([x + pad_x, y + chh // 2 - int(6 * scale), x + pad_x + int(12 * scale), y + chh // 2 + int(6 * scale)],
              fill=(45, 212, 191, 255))
    d.text((x + pad_x + int(22 * scale), y + pad_y - int(2 * scale)), title, font=f1, fill=(248, 250, 252, 255))
    if sub:
        sy = y + chh + int(10 * scale)
        d.rounded_rectangle([x, sy, x + int(sw + 28 * scale), sy + int(34 * scale)], radius=int(10 * scale),
                            fill=(8, 13, 26, 200))
        d.text((x + int(14 * scale), sy + int(6 * scale)), sub, font=f2, fill=(253, 230, 138, 255))
    im.save(path)
