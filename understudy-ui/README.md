# Understudy web UI

Next.js 16 (App Router) + React 19 + Tailwind CSS 4. Two screens, both talk to the FastAPI backend (`server.py`, port 8000):

| Route | Screen | Code |
|---|---|---|
| `/wizard` | **Understudy Studio** - login / sign-up / profile, the project & session explorer and the 5-step flow (Watch, Govern, Build, Execute, Export) | `app/wizard/*`, `lib/wizard.ts`, `lib/studio.ts` |
| `/` | **Mission Control** - the admin console (telemetry, users, access & tools, environments, escalations, learned skills, code health, AI & security) | `app/page.tsx`, `app/*-panel.tsx`, `app/admin-*.tsx`, `app/synthesizer.tsx`, `app/install-assistant.tsx`, `lib/admin.ts` |

Shared: `app/wizard/ui.tsx` (cards, folds, badges, buttons), `app/theme-toggle.tsx` (dark / light), `lib/live.ts`
(WebSocket live events with polling fallback), `lib/api.ts` (backend base URL, Gemini key / model and Model Armor calls).

## Where the backend is

`NEXT_PUBLIC_UNDERSTUDY_API_BASE` (build time):

* unset - the browser calls `http://localhost:8000` (local runs, Docker Compose);
* `same-origin` - the UI and the backend share one origin; `next.config.ts` proxies `/api`, `/portal1-3`, `/erp-static`
  and `/healthz` to `UNDERSTUDY_BACKEND_INTERNAL_URL` (the Cloud Run image, `Dockerfile.cloudrun`);
* `https://...` - an explicit backend URL.

## Develop

```bash
npm ci
npm run dev          # http://localhost:3000 - start the backend separately (uvicorn server:app --port 8000)
npx tsc --noEmit     # type check
npm run lint         # ESLint (eslint-config-next)
npm run build        # production build (what the start scripts and Docker images serve with `npm start`)
```

The start scripts in the repository root (`run_demo.*`, `setup.*`) install, build and start everything; see the
root README.
