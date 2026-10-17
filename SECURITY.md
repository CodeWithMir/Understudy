# Security policy

Understudy runs AI-generated code against real applications, so security is part of the design rather than an
add-on. This page summarises the controls and how to report a problem.

## Built-in controls

| Layer | Control |
|---|---|
| Inputs | Every URL, SOP, recording, correction and AI transcription passes **Model Armor** (sanitise → signature → Gemini semantic classifier; optional Google Cloud Model Armor) before use |
| Indirect injection | Text read from web pages, videos and history digests is screened before it can enter a prompt; instructions hidden in a video block the run |
| AI output | Every Gemini answer is schema-validated; generated code passes a safety scan (no hard-coded values, secrets or dangerous calls) |
| Business rules | Confirmed SOP rules are enforced before step 1 of every run, inside Understudy and in the exported ZIP; rule violations are never "self-healed" |
| Execution | Restricted network (only the target is reachable; cloud metadata and private ranges denied), time and memory limits, scrubbed environment without keys or tokens |
| Credentials | Keyless Gemini on Vertex AI through a least-privilege service account; the admin password lives in Secret Manager; login lock-outs shared through Firestore |
| Human in the loop | Rules, self-healing fixes, new tools and setup fixes are approved by a person and logged in Mission Control |

Details: [ARCHITECTURE.md §7](ARCHITECTURE.md) (security architecture) and §7e (restricted-network execution). Known
limits, such as the application-level egress proxy, are listed in §11.

## Reporting a vulnerability

Please do **not** open a public issue for security problems. Use GitHub's
[private vulnerability reporting](../../security/advisories/new) for this repository, with:

- what you found and where (file, endpoint or step),
- how to reproduce it,
- the impact you expect.

We aim to acknowledge reports within 3 working days.

## Secrets

Never commit `.env`, keys or service-account files; `.gitignore` excludes them. Test fixtures use clearly fake keys.
