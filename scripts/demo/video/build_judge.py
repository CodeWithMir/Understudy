"""Assembles the 3-4 minute narrated judge video from the recording, the HTML cards, the narration and the bed."""
import os as _os
HERE = _os.path.dirname(_os.path.abspath(__file__))
WORK = _os.environ.get("UNDERSTUDY_VIDEO_WORK", _os.path.join(HERE, "work"))
import json
import os

import vid

D = WORK
OUT = f"{D}/build_judge"
os.makedirs(OUT, exist_ok=True)
W, H = 1920, 1080
narr = {it["id"]: it["text"] for it in json.load(open(f"{HERE}/narration_judge.json"))}
dur = json.load(open(f"{D}/tts_judge/durations.json"))
STANDIN = "AI answers: labelled offline stand-in"

SCENES = [
    dict(id="title", card="title", lead=1.2, tail=1.4),
    dict(id="steps", card="steps", lead=0.6, tail=1.2),
    dict(id="inputs", seg=("inputs", "watch"), label="STEP 1 · WATCH"),
    dict(id="watch", seg=("watch", "build"), label="STEP 1 · WATCH"),
    dict(id="build", seg=("build", "govern"), label="STEP 2 · BUILD"),
    dict(id="govern", seg=("govern", "govern_block"), label="STEP 3 · GOVERN"),
    dict(id="govern_block", seg=("govern_block", "execute"), label="STEP 3 · GOVERN"),
    dict(id="execute", seg=("execute", "ledger"), label="STEP 4 · EXECUTE"),
    dict(id="ledger", seg=("ledger", "export"), label="TARGET APP · PORTAL 1 LEDGER",
         vf="crop=1440:810:250:0,scale=1920:1080:flags=lanczos"),
    dict(id="export", seg=("export", "armor"), label="STEP 5 · EXPORT"),
    dict(id="armor", seg=("armor", "mission"), label="SECURITY · MODEL ARMOR"),
    dict(id="mission", seg=("mission", "end"), label="ADMIN · MISSION CONTROL"),
    dict(id="bench", card="bench", lead=0.6, tail=1.6),
    dict(id="closing", card="closing", lead=0.6, tail=2.6),
]

# 1. master video from the screencast
master = f"{OUT}/master.mp4"
import json as _j
if os.path.exists(master):
    _tl = _j.load(open(f"{D}/rec_judge/timeline.json")); t0 = _tl["frames"][0][0]; marks = {n: t for t, n in _tl["markers"]}
else:
    t0, marks = vid.frames_to_video(f"{D}/rec_judge", master, W, H)
marks = {k: v - t0 for k, v in marks.items()}

# 2. durations
for s in SCENES:
    s.setdefault("lead", 0.5)
    s.setdefault("tail", 0.7)
    s["dur"] = round(s["lead"] + dur[s["id"]] + s["tail"], 2)

# 3. cards
specs = [(s["card"], f"file://{HERE}/cards.html?card={s['card']}", s["dur"], f"{OUT}/card_{s['id']}.mp4")
         for s in SCENES if "card" in s and not os.path.exists(f"{OUT}/card_{s['id']}.mp4")]
vid.record_cards(specs, W, H)

# 4. segments
parts, t, placements, events = [], 0.0, [], []
for i, s in enumerate(SCENES):
    out = f"{OUT}/seg_{i:02d}_{s['id']}.mp4"
    if os.path.exists(out) and os.environ.get("REUSE") == "1":
        s["speed"] = 0
    elif "card" in s:
        vid.segment(f"{OUT}/card_{s['id']}.mp4", 0, s["dur"], s["dur"], out, fade_in=0.35 if i else 0.8,
                    fade_out=0.35 if i < len(SCENES) - 1 else 1.2, max_speed=1.0, min_speed=1.0)
        s["speed"] = 1.0
    else:
        a, b = (marks[x] for x in s["seg"])
        png = f"{OUT}/label_{s['id']}.png"
        vid.label_png(png, W, H, s["label"], STANDIN if s["id"] not in ("ledger",) else None)
        prev_card = "card" in SCENES[i - 1]
        nxt_card = i + 1 < len(SCENES) and "card" in SCENES[i + 1]
        s["speed"] = vid.segment(master, a, b, s["dur"], out, png, fade_in=0.3 if prev_card else 0,
                                 fade_out=0.3 if nxt_card else 0,
                                 extra_vf=s.get("vf", ""))
    parts.append(out)
    placements.append((f"{D}/tts_judge/{s['id']}.wav", t + s["lead"]))
    events += vid.caption_events(narr[s["id"]], t + s["lead"], dur[s["id"]])
    print(f"{s['id']:13s} start {t:6.1f}s  dur {s['dur']:5.1f}s  speed {s['speed']:.2f}", flush=True)
    t += s["dur"]
total = t

# 5. join, captions, audio
silent = f"{OUT}/joined.mp4"
vid.concat(parts, silent)
ass, srt = f"{OUT}/captions.ass", f"{D}/Understudy_judge_demo.srt"
vid.write_ass(events, ass, W, H, size=38, margin_lr=490)
vid.write_srt(events, srt)
capt = f"{OUT}/captioned.mp4"
vid.run(["ffmpeg", "-y", "-loglevel", "error", "-i", silent, "-vf", f"ass={ass}", "-c:v", "libx264", "-crf", "17",
         "-preset", "medium", "-pix_fmt", "yuv420p", "-r", "30", capt])
voice = vid.narration_track(placements, total)
audio = vid.mix(voice, f"{D}/audio/bed.wav", total, music_db=-20.0, duck_db=-8.0)
vid.mux(capt, audio, f"{D}/Understudy_judge_demo.mp4")
print("total", round(total, 1))
