
import os as _os
HERE = _os.path.dirname(_os.path.abspath(__file__))
WORK = _os.environ.get("UNDERSTUDY_VIDEO_WORK", _os.path.join(HERE, "work"))
import json, sys, os, numpy as np, soundfile as sf
from kokoro_onnx import Kokoro
k = Kokoro(_os.path.join(_os.environ.get("KOKORO_DIR", "."), "kokoro.onnx"), _os.path.join(_os.environ.get("KOKORO_DIR", "."), "voices.bin"))
items = json.load(open(sys.argv[1])); out = sys.argv[2]; voice = sys.argv[3] if len(sys.argv) > 3 else "af_heart"
speed = float(sys.argv[4]) if len(sys.argv) > 4 else 1.0
res = {}
for it in items:
    s, sr = k.create(it["text"], voice=voice, speed=speed, lang="en-us")
    # trim leading/trailing silence
    a = np.abs(s); idx = np.where(a > 0.01)[0]
    s = s[max(0, idx[0]-int(0.03*sr)): idx[-1]+int(0.08*sr)]
    sf.write(f"{out}/{it['id']}.wav", s, sr)
    res[it["id"]] = round(len(s)/sr, 2); print(it["id"], res[it["id"]], flush=True)
json.dump(res, open(f"{out}/durations.json", "w"), indent=1)
print("total", round(sum(res.values()), 1))
