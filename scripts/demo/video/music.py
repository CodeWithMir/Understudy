"""Original background music, synthesized from scratch (no samples, no third-party audio).

    python music.py promo out.wav [seconds]   upbeat 112 bpm track with drums, bass, arp and a hook
    python music.py bed   out.wav [seconds]   calm 84 bpm pad + soft arp bed for under narration
"""
import sys

import numpy as np
import soundfile as sf
from scipy.signal import butter, fftconvolve, sosfilt

SR = 48000
rng = np.random.default_rng(7)


def hz(m):
    return 440.0 * 2 ** ((m - 69) / 12)


N = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def note(name):  # 'A4' -> midi
    return 12 * (int(name[-1]) + 1) + N[name[0]] + (1 if "#" in name else 0)


def lp(x, fc, order=2):
    return sosfilt(butter(order, min(fc, SR / 2.2), "low", fs=SR, output="sos"), x)


def hp(x, fc, order=2):
    return sosfilt(butter(order, fc, "high", fs=SR, output="sos"), x)


def bp(x, lo, hi, order=2):
    return sosfilt(butter(order, [lo, hi], "band", fs=SR, output="sos"), x)


def saw(f, n, phase=0.0):
    t = np.arange(n) / SR
    return 2 * ((f * t + phase) % 1.0) - 1


def env_adsr(n, a, d, s, r):
    e = np.ones(n) * s
    na, nd, nr = int(a * SR), int(d * SR), int(r * SR)
    na = min(na, n)
    e[:na] = np.linspace(0, 1, na, endpoint=False)
    if nd > 0 and na < n:
        k = min(nd, n - na)
        e[na:na + k] = np.linspace(1, s, nd)[:k]
    if nr > 0:
        k = min(nr, n)
        e[n - k:] *= np.linspace(1, 0, k)
    return e


def add(buf, x, start, pan=0.0, gain=1.0):
    i = int(start * SR)
    if i >= buf.shape[0]:
        return
    x = x[: buf.shape[0] - i] * gain
    l, r = np.cos((pan + 1) * np.pi / 4), np.sin((pan + 1) * np.pi / 4)
    buf[i:i + len(x), 0] += x * l * 1.41
    buf[i:i + len(x), 1] += x * r * 1.41


def reverb(buf, secs=2.4, mix=0.22):
    n = int(secs * SR)
    t = np.arange(n) / SR
    out = np.zeros_like(buf)
    for ch in range(2):
        ir = rng.standard_normal(n) * np.exp(-t * 6.9 / secs)
        ir = lp(ir, 6000)
        ir[: int(0.012 * SR)] = 0
        ir /= np.sqrt(np.sum(ir ** 2))
        out[:, ch] = fftconvolve(buf[:, ch], ir)[: buf.shape[0]]
    return buf * (1 - mix) + out * mix


def delay(x, secs, fb=0.35, mix=0.3):
    d = int(secs * SR)
    x = np.concatenate([x, np.zeros(4 * d)])
    y = x.copy()
    for k in range(1, 5):
        g = fb ** k
        y[d * k:] += x[: len(x) - d * k] * g
    return x * (1 - mix) + y * mix


CHORDS = [  # vi - IV - I - V in C major
    ("A", ["A3", "C4", "E4"], "A1"),
    ("F", ["F3", "A3", "C4"], "F1"),
    ("C", ["G3", "C4", "E4"], "C2"),
    ("G", ["G3", "B3", "D4"], "G1"),
]
HOOK = [  # (beat offset in bar, note, length in beats) per chord bar
    [(0, "E5", .5), (1, "E5", .5), (1.5, "D5", .5), (2, "C5", 1), (3, "A4", 1)],
    [(0, "C5", .5), (1, "C5", .5), (1.5, "D5", .5), (2, "E5", 2)],
    [(0, "G5", 1), (1, "E5", 1), (2, "D5", .5), (2.5, "C5", .5), (3, "D5", 1)],
    [(0, "D5", 1), (1, "B4", 1), (2, "D5", 2)],
]


def pad(chord, dur, bright=1800, gain=0.16):
    n = int(dur * SR)
    x = np.zeros(n)
    for nm in chord:
        f = hz(note(nm))
        for det in (-0.07, 0.0, 0.07):
            x += saw(f * 2 ** (det / 12), n, rng.random())
    x = lp(x / (len(chord) * 3), bright, 2)
    return x * env_adsr(n, min(0.9, dur * 0.3), 0.3, 0.85, min(1.0, dur * 0.35)) * gain


def pluck(f, dur=0.35, bright=3500, gain=0.18):
    n = int(dur * SR)
    t = np.arange(n) / SR
    x = 0.6 * saw(f, n) + 0.4 * np.sin(2 * np.pi * f * t)
    x = lp(x, bright, 2) * np.exp(-t * 9)
    x[: 60] *= np.linspace(0, 1, 60)
    return x * gain


def lead(f, dur, gain=0.12):
    n = int((dur + 0.05) * SR)
    t = np.arange(n) / SR
    vib = 1 + 0.004 * np.sin(2 * np.pi * 5.5 * t) * np.clip(t * 3, 0, 1)
    ph = np.cumsum(f * vib) / SR
    x = np.sign(np.sin(2 * np.pi * ph)) * 0.5 + (2 * (ph % 1) - 1) * 0.5
    x = lp(x, 2600, 2)
    return x * env_adsr(n, 0.01, 0.12, 0.7, 0.08) * gain


def kick(gain=0.9):
    n = int(0.45 * SR)
    t = np.arange(n) / SR
    f = 45 + 85 * np.exp(-t * 30)
    x = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 7.5)
    x[: int(0.004 * SR)] += rng.standard_normal(int(0.004 * SR)) * 0.25
    return np.tanh(x * 1.6) * gain


def clap(gain=0.35):
    n = int(0.25 * SR)
    t = np.arange(n) / SR
    x = bp(rng.standard_normal(n), 900, 3200) * np.exp(-t * 22)
    return x * gain


def hat(gain=0.12, open_=False):
    n = int((0.18 if open_ else 0.05) * SR)
    t = np.arange(n) / SR
    x = hp(rng.standard_normal(n), 7000) * np.exp(-t * (18 if open_ else 70))
    return x * gain


def bass(f, dur, gain=0.32):
    n = int(dur * SR)
    t = np.arange(n) / SR
    x = np.sin(2 * np.pi * f * t) + 0.35 * lp(saw(f, n), 400)
    return np.tanh(x * 1.3) * env_adsr(n, 0.005, 0.08, 0.8, 0.04) * gain


def riser(dur, gain=0.18):
    n = int(dur * SR)
    t = np.arange(n) / SR
    x = rng.standard_normal(n)
    out = np.zeros(n)
    seg = int(0.05 * SR)
    for i in range(0, n, seg):
        fc = 400 + 7000 * (i / n) ** 2
        out[i:i + seg] = bp(x[i:i + seg], fc * 0.7, min(fc * 1.3, 20000))
    return out * np.linspace(0, 1, n) ** 2 * gain


def promo(total):
    bpm = 112
    beat = 60 / bpm
    bar = 4 * beat
    nbars = int(np.ceil(total / bar)) + 1
    buf = np.zeros((int((nbars * bar + 3) * SR), 2))
    duck = np.ones(buf.shape[0])
    # sections by bar index
    def sec(b):
        if b < 4: return "intro"
        if b < 8: return "build"
        if b < 16: return "main"
        if b < 20: return "break"
        return "main2"
    for b in range(nbars):
        s = sec(b)
        name, ch, root = CHORDS[b % 4]
        t0 = b * bar
        bright = 900 + 1400 * min(1, b / 6) if s in ("intro", "build") else (1500 if s == "break" else 2600)
        pg = {"intro": 0.8, "build": 0.4, "break": 0.75}.get(s, 0.13)
        add(buf, pad(ch, bar + 0.4, bright, pg), t0, -0.25)
        add(buf, pad([c[:-1] + str(int(c[-1]) + 1) for c in ch[:2]], bar + 0.4, bright * 0.8, 0.05), t0, 0.3)
        # arp 16ths
        tones = [note(c) + 12 for c in ch] + [note(ch[0]) + 24]
        pattern = [0, 1, 2, 3, 2, 1, 2, 3] * 2
        for i, k in enumerate(pattern):
            if s == "intro" and i % 2:
                continue
            add(buf, pluck(hz(tones[k]), 0.3, 2000 + 1500 * (s != "intro"), 0.34 if s in ("intro", "break") else 0.10),
                t0 + i * beat / 4,
                pan=(-0.4 if i % 2 else 0.4))
        if s in ("build", "main", "main2"):
            for i in range(8):
                add(buf, hat(0.09 if i % 2 else 0.05), t0 + i * beat / 2 + (beat / 2 if s == "build" else 0) * 0, 0.2)
            for i in range(8):     # bass 8ths, off-beat pump
                add(buf, bass(hz(note(root) + 12), beat / 2 * 0.9, 0.26 if s != "build" else 0.18), t0 + i * beat / 2)
        if s in ("main", "main2"):
            for i in range(4):
                add(buf, kick(), t0 + i * beat)
                k0 = int((t0 + i * beat) * SR)
                k1 = min(len(duck), k0 + int(beat * SR))
                duck[k0:k1] = np.minimum(duck[k0:k1], 1 - 0.55 * np.exp(-np.arange(k1 - k0) / SR * 9))
            for i in (1, 3):
                add(buf, clap(), t0 + i * beat, 0.05)
            add(buf, hat(0.06, True), t0 + 3.5 * beat, -0.2)
            gain_hook = 0.11 if s == "main" else 0.13
            for off, nm, ln in HOOK[b % 4]:
                add(buf, delay(lead(hz(note(nm)), ln * beat * 0.92, gain_hook), beat * 0.75, 0.3, 0.25),
                    t0 + off * beat, 0.1)
        if s == "build" and b == 7:
            add(buf, riser(bar), t0, 0)
        if s == "break" and b == 19:
            add(buf, riser(bar, 0.22), t0, 0)
        if s == "build":
            for i in range(4):
                if b >= 6:
                    add(buf, kick(0.5), t0 + i * beat)
    # sidechain pumping on everything except drums: approximate by ducking whole mix lightly
    buf *= (0.75 + 0.25 * duck)[:, None]
    return buf


def bed(total):
    bpm = 84
    beat = 60 / bpm
    bar = 4 * beat
    nbars = int(np.ceil(total / bar)) + 1
    buf = np.zeros((int((nbars * bar + 4) * SR), 2))
    for b in range(nbars):
        name, ch, root = CHORDS[b % 4]
        t0 = b * bar
        add(buf, pad(ch, bar + 0.8, 1100, 0.14), t0, -0.2)
        add(buf, pad([c[:-1] + str(int(c[-1]) + 1) for c in ch[1:]], bar + 0.8, 900, 0.05), t0, 0.3)
        add(buf, bass(hz(note(root) + 12), bar * 0.95, 0.12), t0)
        tones = [note(c) + 12 for c in ch] + [note(ch[0]) + 24]
        for i, k in enumerate([0, 2, 1, 3, 2, 1, 2, 3]):
            add(buf, pluck(hz(tones[k]), 0.6, 1600, 0.055), t0 + i * beat / 2, pan=(-0.5 if i % 2 else 0.5))
    return buf


def finish(buf, total, fade_in=1.5, fade_out=3.0):
    buf = reverb(buf, 2.6, 0.24)
    n = int(total * SR)
    buf = buf[:n]
    buf[: int(fade_in * SR)] *= np.linspace(0, 1, int(fade_in * SR))[:, None]
    buf[n - int(fade_out * SR):] *= np.linspace(1, 0, int(fade_out * SR))[:, None]
    buf = np.tanh(buf * 1.2) / np.tanh(1.2)
    buf /= np.max(np.abs(buf)) / 0.89
    return buf


if __name__ == "__main__":
    kind, out = sys.argv[1], sys.argv[2]
    total = float(sys.argv[3]) if len(sys.argv) > 3 else 64
    b = promo(total) if kind == "promo" else bed(total)
    sf.write(out, finish(b, total), SR, subtype="PCM_16")
    print("wrote", out, total, "s")
