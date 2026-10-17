# Understudy benchmark

Measured, not claimed: `scripts/benchmark.py` drives the real product through its HTTP API - Step 1 Watch, Step 2 Govern,
Step 3 Build (9.9 order; the run below was recorded with 9.5, when Build came first), Step 4 Execute in a real headless Chromium against the bundled demo ERP, Step 5 Export - several times per
scenario, and checks the ERP ledger itself after every run. Raw data: [`benchmark.json`](benchmark.json).

## Latest run - 2026-10-08T04:10:23Z
* Understudy 9.5.0 · framework `playwright-python` 1.56.0 · AI mode **LOCAL_HEURISTIC** · 3 runs per scenario · Python 3.13.16 · total 39s

| Scenario | Pass | First-run pass | Healed automatically | Passed thanks to a learned skill | Retries | Blocked by GOVERN | Duplicate records | Median end-to-end | Median execute+export |
|---|---|---|---|---|---|---|---|---|---|
| clean | 3/3 | 3/3 | 0 | 0 | 0 | 0 | 0 | 3.78s | 3.6s |
| modern_portal | 3/3 | 3/3 | 0 | 0 | 0 | 0 | 0 | 3.77s | 3.6s |
| selector_drift | 3/3 | 2/3 | 1 | 2 | 1 | 0 | 0 | 3.83s | 3.61s |
| policy_block | 3/3 | n/a | 0 | 0 | 0 | 3 | 0 | 0.22s | 0.0s |

**Model Armor**: blocked 10/10 attacks (100%), 0 false positives on 10 benign SOPs, 0.08 ms per input.

How to read it: a run passes when all five steps pass and its record is in the ERP ledger (checked by the benchmark, independently of Understudy). *policy_block* passes when GOVERN blocks the run and the ERP is unchanged (ledger changes = 0). *selector_drift* is expected to fail its first run and heal itself; the lesson is saved as a learned skill, so later runs of the same drift pass first time ("passed thanks to a learned skill"). Duplicate records counts extra ledger rows for the same run (idempotency).

## What the scenarios do

| Scenario | What happens | Passes when |
|---|---|---|
| `clean` | record + automate Portal 1 (classic AP form) | all 5 steps pass and the invoice is in the ledger exactly once |
| `modern_portal` | record + automate Portal 2 (redesigned single-page portal: other ids, labels and a file-upload control) | same |
| `selector_drift` | record on Portal 1, then the app is "redesigned": the run goes to Portal 2 with the recorded labels gone | the first run fails and self-healing repairs it; the fix is saved as a learned skill, so later runs pass first time |
| `policy_block` | the payload breaks the SOP's $5,000 spend limit | GOVERN blocks the run and the ledger is unchanged |
| `model_armor` | 10 prompt-injection / unsafe inputs and 10 benign SOPs through the guardrails | attacks blocked, benign SOPs pass |

## Conditions of this run

* Understudy 9.5.0, offline mode (`LOCAL_HEURISTIC`): no Gemini key and no Vertex AI in the benchmark environment, so
  the built-in planner, templates, rule parser and heuristic healer did the work. With a key or Vertex AI the same
  command measures the AI path (the report then says `ACTUAL_AI_API`).
* Tool: Playwright (Python), pinned by the admin to 1.56.0 (`--tool-version 1.56.0`) - the version whose Chromium is
  installed on the benchmark machine, which has no access to the browser download host.
* Unattended: self-healing in `automatic` mode, single-user mode (`UNDERSTUDY_USER_LOGIN=off`), the catalog tools
  approved (`UNDERSTUDY_DEFAULT_TOOLS=catalog`) - the benchmark sets these itself.
* The Model Armor corpus is small and was used for tuning the offline patterns, so treat that line as a regression
  check, not a detection rate. With Gemini available the semantic classifier is the main defence.
* The ERP is the bundled demo; real-world pass rates depend on your applications.

## Run it yourself

```bash
python scripts/benchmark.py                                   # 3 runs per scenario
python scripts/benchmark.py --runs 5 --scenarios clean,selector_drift
python scripts/benchmark.py --framework selenium-python       # another catalog tool
python scripts/benchmark.py --tool-version 1.56.0             # pin the admin's version of the tool
```

Reports go to `generated/benchmark/benchmark_<timestamp>.md` and `.json`. Copy them over this file and
`benchmark.json` to publish a new run.
