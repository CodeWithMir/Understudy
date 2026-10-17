# Demo videos (recorded with Understudy 9.5.2)

Two videos, both recorded from the running product (not mock-ups):

| File | Length | For | Audio |
|---|---|---|---|
| `Understudy_judge_demo.mp4` (+ `.srt`) | 3:22, 1920x1080 | judges / reviewers | narration, quiet music bed, burned-in captions |
| `Understudy_promo_16x9.mp4` | 0:53, 1920x1080 | YouTube, LinkedIn, X | original background music (112 bpm), no voice |
| `Understudy_promo_9x16.mp4` | 0:53, 1080x1920 | Reels, Shorts, TikTok | same music |

## Honesty note

The machine that recorded these videos has no internet, so a **clearly labelled offline stand-in model** answered the
AI prompts in Gemini's place (`scripts/demo/serve.py --standin`, `scripts/demo/standin_model.py`). The app shows it as
*AI mode · stand-in model (no internet)* in every frame, the judge video carries an "AI answers: labelled offline
stand-in" tag on every app scene, and both videos say so at the end. It only knows the bundled sample recording and
describes what that recording really shows (it reports honestly that the address bar is not visible, so the user picks
Portal 1). Everything else - the live-page checks, the build agent's locator verification, the rule parser, the
compliance block, the restricted-network run, the ledger, the ZIP, Model Armor's pattern layer and Mission Control - is
the shipped product. **Re-record with real Gemini** (below) before a final submission if you can.

> **Version note (9.9).** The videos were recorded with an earlier version, where Build came before Govern. Since 9.9
> the steps are Watch → Govern → Build → Execute → Export: the payload table and the SOP rules are in Step 2, and the
> code generated in Step 3 carries the rules (`governance.json`, checked before step 1). Re-record (below) to show the
> current order; the scenes keep their content.

## Judge video - storyline (what is on screen and said)

| # | Scene | Narration (abridged) |
|---|---|---|
| 1 | Title card | Processes live in people's heads; one recording becomes automation you can trust |
| 2 | Five steps card | Watch, Build, Govern, Execute, Export; Gemini understands, deterministic checks decide |
| 3 | Step 1 inputs | Priya (AP) adds the 16-second sample recording, picks the approved Portal 1, loads the SOP |
| 4 | Step 1 Watch | Steps with video times and selectors verified on the live page; parameters; complexity; tool |
| 5 | Step 2 Build | She mistypes the amount as 7,500; Playwright suite; every locator confirmed on a fresh load |
| 6 | Step 3 Govern | SOP -> rules quoting their source sentence; confirm; compliance check |
| 7 | Govern blocks | 7,500 > $5,000 limit: nothing runs; she fixes it in place; the payload complies |
| 8 | Step 4 Execute | Own runtime, restricted network (only the portal reachable); 5/5 steps in ~4 s |
| 9 | Portal 1 ledger | The invoice is really in the target app's ledger |
| 10 | Step 5 Export | ZIP: code, payload, confirmed rules, compliance report, run log |
| 11 | Model Armor | "Ignore all previous instructions..." in the SOP is blocked in Step 1 |
| 12 | Mission Control | Telemetry, the interception and the layer that caught it, tools/sites, network mode |
| 13 | Benchmark card | 12/12 runs as expected (offline mode); 9/9 automations on two portals incl. a healed redesign; 3/3 over-limit invoices blocked, 0 ledger changes |
| 14 | Closing card | Cloud Run, Gemini on Vertex AI / AI Studio, Firestore; honesty note |

The full narration text is in `scripts/demo/video/narration_judge.json`; captions are in `Understudy_judge_demo.srt`.

## Promo - beat sheet (24 bars at 112 bpm, cuts on the bar)

Hook cards ("Your team does it by hand." / "Show it once.") -> Watch (2 shots) -> drop: Build, Govern block, fix,
Execute, ledger, Export, Model Armor, Mission Control (one bar each) -> break: "Measured, not claimed." stats -> end card
with the stand-in note. The music is synthesized from scratch by `scripts/demo/video/music.py` (no samples, no
third-party audio), so it is free to publish.

## Re-record (ideally with real Gemini, on the Cloud Run deployment)

For the submission, record against the deployed service: `./deploy_cloudrun.sh <project-id>` (Windows:
`deploy_cloudrun.bat <project-id>`), then point the recorder at it with `UNDERSTUDY_UI=https://<service-url>` and the
admin password the deploy printed (`UNDERSTUDY_ADMIN_PASSWORD=...`). Gemini then runs on Vertex AI, the header shows
the real model instead of the stand-in, and `python scripts/check_deployment.py <service-url>` gives you a screenshot-
ready proof that it runs on Cloud Run with Gemini. The stand-in note in the videos can then be dropped.


Needs Python with `playwright`, `numpy`, `scipy`, `soundfile`, `pillow`, `kokoro-onnx`, plus `ffmpeg` with libass and
the Kokoro model files (`kokoro-v1.0.int8.onnx` renamed to `kokoro.onnx`, and `voices-v1.0.bin` renamed to
`voices.bin`, from the kokoro-onnx GitHub releases) in `$KOKORO_DIR`.

```bash
cd scripts/demo/video
export UNDERSTUDY_VIDEO_WORK=$PWD/work KOKORO_DIR=~/kokoro UNDERSTUDY_ADMIN_PASSWORD='choose-one'
# 1. a clean Understudy: empty generated/, the backend (with Gemini configured: python ../serve.py;
#    offline: python ../serve.py --standin), the UI on :3000, a Studio user priya / Studio-Demo-2026!
#    (Mission Control > Users) and Playwright (Python) approved (UNDERSTUDY_DEFAULT_TOOLS=catalog)
python record_judge.py                                  # drives the UI, ~3.5 min, writes work/rec_judge/
python tts.py narration_judge.json work/tts_judge af_heart 0.92
python music.py bed work/audio/bed.wav 240
python music.py promo work/audio/promo_music.wav 53.5
python build_judge.py                                   # -> work/Understudy_judge_demo.mp4 + .srt
python build_promo.py h v                               # -> work/Understudy_promo_16x9.mp4 / _9x16.mp4
```

With real Gemini the answers differ a little from the stand-in's, so check the scene windows in `build_promo.py` (they
are seconds in `work/build_judge/master.mp4`) and the narration lines that quote the AI's output.

How it is made: the browser is captured with the Chrome DevTools screencast at 2560x1440 (page zoom 1.6, drawn cursor),
each scene is fitted to its narration by a uniform speed change, captions are libass, the voice is Kokoro `af_heart`,
the music bed ducks under the voice, and the mix is loudness-normalised (-16 LUFS judge, -14 LUFS promo).
