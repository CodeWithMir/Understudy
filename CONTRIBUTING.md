# Contributing to Understudy

Thanks for helping make process automation trustworthy.

## Set up

```bash
cp .env.example .env              # GEMINI_API_KEY optional - without it Understudy runs in offline mode
./run_demo.sh                     # Windows: run_demo.bat - installs everything on first run
pip install -r requirements-dev.txt
python -m pytest                  # the full suite; browser/Node/Java suites skip themselves when missing
```

UI: `cd understudy-ui && npm ci && npm run lint && npm run build`.

## Ground rules

1. **Gemini understands, deterministic checks decide.** A new AI capability is a prompt in
   [`core/wizard/prompts.py`](core/wizard/prompts.py) with an output schema, validation and a deterministic fallback.
   Run `python scripts/generate_prompt_docs.py` so ARCHITECTURE.md §8 stays verbatim.
2. **Nothing reaches a prompt unscreened.** New input channels go through `core/wizard/guardrails.py`.
3. **Measured, not claimed.** Behaviour changes come with tests; changes to the pipeline should keep
   `python scripts/benchmark.py` green.
4. **The pipeline contract is protected.** `core/pipeline_contract.py` defines the five pillars; change it only with a
   clear reason in the PR.
5. **User-visible changes** go in [CHANGELOG.md](CHANGELOG.md).

## Pull requests

Keep PRs focused, describe the *why*, and tick the checklist in the PR template. CI runs the backend tests, the UI
lint and build, and the Cloud Run image build.
