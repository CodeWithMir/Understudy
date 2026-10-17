# Understudy

**Version 9.9.1.** Turn a screen recording, the address of **any web app or public website** and plain-English rules
into a **governed, tested, downloadable** automation suite in five guided steps. It is built for developers who have
never written an automation script or dealt with SOP governance - and for the admin who decides which tools, sites
and versions they may use.

```
 1 WATCH  ─►  2 GOVERN  ─►  3 BUILD  ─►  4 EXECUTE  ─►  5 EXPORT  ─►  .zip  (only when all five pass)
 AI watches    you check     AI writes     it runs for    the passing run is
 your video    the data and  the code -    real, and      packaged: code, data,
 and pages     your SOP      with your     heals itself   rules, evidence
               rules         rules in it   when it fails
```

| Screen | URL | Who it's for |
|---|---|---|
| **Understudy Studio** | http://localhost:3000/wizard | Developers: log in (username or email; optional two-step sign-in with an emailed code), sign up (email verified by a code), forgot password, Profile; projects and sessions in a workspace explorer; the five steps; a notification bell |
| **Mission Control** | http://localhost:3000 | Admins: telemetry of every user action and AI call · Studio users and email · access & tools (allowed / blocked URLs, run network, the zero-trust tool registry with the AI install assistant, setup agent and smoke-test review, sandbox, package managers, self-healing) · environments (runtime versions per tool) · escalations (runtime-help requests) · learned skills · code health · Gemini key / model, Google Cloud and Model Armor |
| Demo ERP | http://localhost:8000/portal1 · `/portal2` · `/portal3` | Sample targets: a classic AP form, a redesigned single-page portal, and a form whose ids and field order change on every load |

**What it does, in one paragraph.** In **Watch** Gemini reads the recording (frames and narration), lists the pages it
visits, reconstructs the steps and the values typed, and checks every page live - only the checks this automation
needs (sign-in, uploads, dialogs, changing ids ...) - to compute an **Automation Difficulty Score** out of 10 from ten
parameters you can tune, and to recommend **traditional** tools (a generated script) or **agentic** tools (an AI agent
that drives the page at run time). In **Govern** you review the payload table, and your SOP (any language) becomes
rules you confirm; a payload that breaks them is blocked. In **Build** an agent writes the suite for the tool you picked
(Playwright, Selenium, Cypress, Puppeteer, Robot Framework, REST Assured, WebdriverIO or any tool your admin approved)
and proves every locator on fresh page loads - or, for an agentic tool, Gemini writes an agentic script the agent
performs at run time. **Either way the confirmed SOP rules are part of the script** (9.9): it ships with
`governance.json` and checks `payload.json` against it before step 1, every time it runs. **Execute** runs the
suite with the tool's own runtime in a sandbox with a restricted network; when it fails, self-healing diagnoses it,
shows you the fix and runs again (or asks the admin for a package, version or runtime). An agentic run uses the Gemini
your admin set up (Vertex AI or the Mission Control key) - users are never asked for a key. **Export** packages the run
that passed. Every input passes Model Armor first; every AI call and fallback is in Mission Control's telemetry.

**Built on Google Cloud.** Every agent runs on Gemini through Google's current **Gen AI SDK (`google-genai`)** - on
**Vertex AI** with Application Default Credentials (no API key) or on AI Studio with a key. On **Cloud Run**, sessions,
the admin policy, learned skills, telemetry and logins live in **Cloud Firestore**, and recordings go through **Cloud
Storage** once so Vertex AI reads them from `gs://`. One command deploys it (`./deploy_cloudrun.sh <project-id>`) and
one connects a local install (`./scripts/gcp_setup.sh <project-id>`).

**Submission requirements - where to see them in this repository.**

| Requirement | How Understudy meets it | Evidence |
|---|---|---|
| A working prototype, not a mock-up | The five steps run end to end against a real web app: the recording becomes code that really fills the form, and the record appears in the target's ledger | `scripts/benchmark.py` (checks the ledger itself), `docs/BENCHMARK.md`, 690+ tests, `docs/DEMO_VIDEO.md` |
| Uses Google AI models | **Gemini** (google-genai SDK) does the work in every step: it watches the recording (video understanding through the Files API / `gs://`), writes the suite, extracts the SOP rules, reviews compliance, heals failed runs, names sessions, scores difficulty and recommends traditional vs agentic automation, screens inputs (Model Armor's semantic stage) and plans tool installs - and, for agentic runs, decides every browser action at run time - 28 prompts, all in `core/wizard/prompts.py` and verbatim in ARCHITECTURE.md §8 | Mission Control > Telemetry shows every Gemini request and answer; `/api/health` reports the model |
| Agentic | Agentic automation as a product feature (9.8): Understudy's Gemini agent performs the recorded steps on the live page, choosing each action from what it sees and checking the result on a screenshot. Agents that act, check and retry: the build agent proves its locators on the live page and rewrites the code until they hold; self-healing diagnoses a failed run, fixes it and runs again; the setup agent installs and smoke-tests tools; code health patches its own start-up warnings and rolls back if a check fails | ARCHITECTURE.md §6 (self-healing), §7d (code health), §7l (setup agent), §7m (build agent) |
| Deployed on Google Cloud (Cloud Run) | `deploy_cloudrun.sh` (Cloud Shell, macOS, Linux) or `deploy_cloudrun.bat` / `.ps1` (Windows) deploys **one Cloud Run service** with Gemini on **Vertex AI** (service account, no API key), **Firestore** (state shared by every instance), **Cloud Storage** (recordings), **Secret Manager** (admin password), built by **Cloud Build** into **Artifact Registry**; optional **Google Cloud Model Armor** | `python scripts/check_deployment.py <url>` checks a deployed URL against these requirements (the deploy scripts run it at the end) |

**Learns, then works offline.** Every validated AI answer is kept as a *learned skill*. Without a key, without Vertex
AI, with a used-up quota or no network, Understudy re-uses what it learned for the same or a similar problem (labelled
"Learned skill" with its match score) and only then falls back to its built-in rules (section 5.1).

**Measured, not claimed.** `python scripts/benchmark.py` runs the real product end to end and checks the ERP ledger
itself (section 8.1, [docs/BENCHMARK.md](docs/BENCHMARK.md)).

More: [ARCHITECTURE.md](ARCHITECTURE.md) (design, every agentic prompt verbatim) · [CHANGELOG.md](CHANGELOG.md) (release
history) · [docs/TOOL_MATRIX.md](docs/TOOL_MATRIX.md) · [docs/DEMO_VIDEO.md](docs/DEMO_VIDEO.md).

---

## 1. Quick start

**Google Cloud Run - the hosted deployment (use this for the submission).** From Cloud Shell, macOS or Linux:
```bash
./deploy_cloudrun.sh <project-id> [region]      # default region us-central1
```
From Windows (with the Google Cloud CLI installed and `gcloud auth login` done):
```bat
deploy_cloudrun.bat <project-id> [region]
```
It enables the APIs, creates Firestore, an Artifact Registry repo, a recordings bucket (auto-deleted after 7 days), a
least-privilege service account (Vertex AI User, Cloud Datastore User, Storage Object Admin on that bucket only) and a
generated Mission Control password in Secret Manager, builds `Dockerfile.cloudrun` with Cloud Build and deploys **one
container with one public URL**: Next.js serves the UI and proxies `/api` and the demo portals to the backend inside
the container. It prints the URL and the admin login (the password only on the first deployment) and then checks the
live URL with `scripts/check_deployment.py`. CI can use `cloudbuild.yaml` instead. Details: path E below and
ARCHITECTURE.md §12.

**On your own machine (development)** - the Gemini key is optional here: without one, everything still runs in
*offline mode*, but the video itself can't be interpreted (section 5). To use Gemini on Vertex AI locally, run
`./scripts/gcp_setup.sh <project-id>` (Windows: `scripts\gcp_setup.bat <project-id>`) once.

**Docker**
```bash
cp .env.example .env            # optional: GEMINI_API_KEY=..., UNDERSTUDY_ADMIN_TOKEN=...
./deploy_docker.sh              # Windows: deploy_docker.bat   (= docker compose up --build -d)
```
The backend image includes Node.js 24 LTS, so the JavaScript tools (Cypress, Playwright JS, Puppeteer, WebdriverIO)
work out of the box. Set `UNDERSTUDY_INSTALL_JAVA=true` before building to preinstall Java + Maven (Understudy can also
download a JDK / Maven itself, section 2.2.1). Every setting in `.env` reaches the backend container; `generated/`,
`uploads/` and `skills/` are mounted, so state survives a rebuild. Without Docker Compose the script builds the
all-in-one `Dockerfile` instead.

**Connect Google Cloud** (Gemini on Vertex AI, Firestore, Cloud Storage) for a native or Docker install: run
`./scripts/gcp_setup.sh <project-id>` (Windows: `scripts\gcp_setup.bat <project-id>`) once. The full walkthrough,
including the API-key option, is in **"Google Cloud set-up, step by step"** below.

**Native** (Python 3.10+, Node.js 20.9+ - Node 24 LTS recommended; the scripts check both)

| OS | Command |
|---|---|
| Windows | `run_demo.bat` (or `./run_demo.ps1`) |
| macOS / Linux | `./run_demo.sh` |

The first run (`setup.sh` / `setup.bat`, called by the launchers) creates `venv/`, installs `requirements-gcp.txt`
(= `requirements.txt` + the optional Google Cloud clients) and Playwright Chromium, installs the UI's packages with
`npm ci` and builds it. All of that is redone automatically once after you upgrade Understudy (the scripts compare the
version in `core/wizard/__init__.py` with a marker), so an old build is never served. The backend is started first and
the UI only once `/api/health` answers. The launcher then checks the toolchains the automation tools need on this machine (Node/npx for Cypress,
Playwright JS and Puppeteer; Java/Maven for REST Assured; an installed Chrome or Edge for Selenium and Robot Framework)
and prints what's missing. The backend prints a status banner with the version, AI status, the approved tools with
their versions and whether each can run, and the URLs.

Then:
1. Open **http://localhost:3000** (Mission Control), log in as `admin` with the password the start script showed
   (first start only; show it again: `python scripts/admin_password.py show`). Open **Access & tools** and add
   a tool with the AI install assistant (a fresh install has none, section 2.2) - or start with
   `UNDERSTUDY_DEFAULT_TOOLS=catalog` in `.env` to approve the catalog tools for a demo. Then open **Users** and
   create an account with an email (or let people **Sign up** in the Studio; section 2.0.1).
2. Open **http://localhost:3000/wizard**, log in with that account, create a **project** (name + description) and a
   **session** (no name needed - the AI names it after Step 1).
3. In Step 1 tick **No recording yet? Use the sample invoice-entry video**, click **Load example** to fill in a sample
   SOP and **Analyse**. With AI the page addresses are read from the recording (without AI too, for a recording that
   was analysed with AI before - Understudy re-uses the addresses it learned); otherwise click the pre-approved
   **Portal 1 - Legacy Accounts Payable (demo)** target (or type the URL of any public website).

For a single-user local install you can switch the Studio login off with `UNDERSTUDY_USER_LOGIN=off` (projects and
sessions still work, without an owner).

---

## Google Cloud set-up, step by step

Understudy runs without any cloud account (offline mode). Connect Google when you want the AI to actually watch the
recording, or want sessions to survive restarts and to run on Cloud Run. Pick the path that fits; each one builds on
the one before.

| Path | You get | You need | Time |
|---|---|---|---|
| **A. Gemini API key** | the AI agents (video understanding, code, rules, self-healing) | a Google account | 2 min |
| **B. Vertex AI** | the same agents on *your* Google Cloud project, **no API key** (enterprise quotas, data stays in your project) | a project with billing + the `gcloud` CLI | 10 min |
| **C. + Firestore** | sessions, admin policy, learned skills, telemetry and logins survive restarts and are shared by every server | path B | +2 min |
| **D. + Cloud Storage** | recordings stored once in a bucket; Vertex AI reads them from `gs://` (required for videos over 18 MB on Vertex AI) | path B | +2 min |
| **E. Cloud Run** | the whole app hosted on Google Cloud at one public HTTPS URL, using B + C + D | path B | 15 min |

The helper script does B, C and D in one go (section "Paths B-D: the one-command way"); the manual commands are
below it if you prefer to see each step.

### Step 0 - prerequisites (paths B-E)

1. **Google Cloud project.** Open https://console.cloud.google.com/projectcreate, create a project and note its
   **project ID** (for example `understudy-demo-123`, not the display name). `gcloud projects list` shows it later.
2. **Billing.** Vertex AI, Firestore and Cloud Storage need a billing account linked to the project:
   https://console.cloud.google.com/billing/linkedaccount. New accounts usually get free trial credit, and Firestore
   and Cloud Storage have free tiers, but Gemini calls are paid per use. Check the current prices on the Vertex AI,
   Firestore and Cloud Storage pricing pages, and set a budget alert under Billing > Budgets & alerts.
3. **Google Cloud CLI (`gcloud`).** Install it from https://cloud.google.com/sdk/docs/install (Windows installer,
   macOS/Linux archive), open a **new** terminal and run `gcloud init` (log in, choose the project).
   No installation at all? Use **Cloud Shell** (the `>_` icon in the console). It has `gcloud` built in and is
   enough for path E.
4. **Region.** `us-central1` has the widest Gemini model choice and is the default. Users in India can keep data in
   Mumbai with `asia-south1` (Firestore and the bucket are created there); if a Gemini model is not offered in that
   region, set `UNDERSTUDY_VERTEX_LOCATION=global` (or `us-central1`) in `.env`. Only the AI calls use that location;
   your data stays in Mumbai.

### Path A - Gemini API key (no Google Cloud project needed)

1. Create a key at https://aistudio.google.com/apikey.
2. Start Understudy (`run_demo.*` or `deploy_docker.*`), open Mission Control (`http://localhost:3000`, log in), go to
   **AI, security & account**, paste the key, choose a model and click **Save**. The key is checked with Google first.
   *Alternative:* put `GEMINI_API_KEY=...` in `.env` before starting.
3. Click **Check Google Cloud set-up**: "Gemini access" and "Gemini call" should say PASS.

### Paths B-D: the one-command way

From the Understudy folder, after Step 0:

| OS | Command |
|---|---|
| macOS / Linux / Cloud Shell | `./scripts/gcp_setup.sh <project-id> [region]` |
| Windows | `scripts\gcp_setup.bat <project-id> [region]` (or `powershell -ExecutionPolicy Bypass -File scripts\gcp_setup.ps1 <project-id>`) |

Add `--no-firestore` / `--no-bucket` (PowerShell: `-NoFirestore` / `-NoBucket`) to skip path C or D, and
`--model-armor` (PowerShell: `-ModelArmor`) to also set up **Google Cloud Model Armor** (section 2.5.1). The script is
safe to run again. It:
1. checks `gcloud` and your login (opens the browser if needed);
2. selects the project and stops with a link if billing is off;
3. enables the APIs: `aiplatform.googleapis.com`, `firestore.googleapis.com`, `storage.googleapis.com`;
4. creates **Application Default Credentials**, the login Understudy uses instead of an API key
   (`gcloud auth application-default login`, browser), and sets the quota project;
5. creates the Firestore database (Native mode) and the bucket `<project-id>-understudy-recordings` (files deleted
   automatically after 7 days);
6. gives your account the roles Understudy needs (Vertex AI User, Cloud Datastore User, Storage Object Admin;
   project owners already have them);
7. writes the settings into `.env` and installs the Google Cloud Python clients;
8. runs the **real** check: a tiny Gemini call, a Firestore write/read/delete and a Cloud Storage
   upload/download/delete. Anything that fails is printed with the command that fixes it.

Then start Understudy as usual: `run_demo.*` (native) or `deploy_docker.*` (Docker). The start-up output shows:
```
[CHECK] Google Cloud / Gemini (settings from .env; no network calls):
   [OK  ] Gemini access              Vertex AI, project understudy-demo-123, location us-central1; model ...
   [OK  ] State store                Cloud Firestore (shared by every instance)
   [OK  ] Google credentials         your Google account (gcloud application-default login); default project ...
```
In Mission Control the Gemini card shows **AI platform: Vertex AI**, **State store: Cloud Firestore** and the number of
recordings uploaded vs. reused, and the header shows **Vertex AI · <model>**.

### Paths B-D by hand (what the script does)

**B - Vertex AI**
```bash
gcloud config set project <project-id>
gcloud services enable aiplatform.googleapis.com
gcloud auth application-default login                       # opens the browser; the login Understudy uses
gcloud auth application-default set-quota-project <project-id>
# only if your account is not a project owner/editor:
gcloud projects add-iam-policy-binding <project-id> --member=user:<you@example.com> --role=roles/aiplatform.user
```
`.env`:
```env
GOOGLE_GENAI_USE_VERTEXAI=true
GOOGLE_CLOUD_PROJECT=<project-id>
GOOGLE_CLOUD_LOCATION=us-central1
# optional: UNDERSTUDY_VERTEX_MODEL=gemini-2.5-flash   (force a model; otherwise Mission Control's model is used, and
#           if Vertex AI doesn't offer it in the region, UNDERSTUDY_VERTEX_FALLBACK_MODEL - default gemini-2.5-flash)
```
No API key is needed. If one is also set, Vertex AI wins (Mission Control then hides the key field). Mission
Control's model list comes from **Vertex AI** (9.9): the Gemini models Google publishes on Vertex AI for your project
(`publishers/google/models`, filtered to models that generate text - no Imagen, Veo, embedding, speech or
image-output models), recommended first; **Refresh list** loads it again, and the card says where the list came from
(or why it fell back to the built-in suggestions, e.g. a missing permission). `UNDERSTUDY_VERTEX_MODEL` still
overrides the choice; a model Vertex AI doesn't serve in your region falls back to `gemini-2.5-flash`.

**C - Firestore**
```bash
gcloud services enable firestore.googleapis.com
gcloud firestore databases create --location=us-central1 --type=firestore-native
# only for non-owners:  --role=roles/datastore.user
pip install -r requirements-gcp.txt                         # the setup scripts already do this
```
`.env`: `UNDERSTUDY_STORE=firestore` (optional: `UNDERSTUDY_FIRESTORE_DATABASE`, `UNDERSTUDY_FIRESTORE_PREFIX`).
Understudy creates its collections (`understudy_sessions`, `understudy_admin_policy`, `understudy_skills`,
`understudy_telemetry`, `understudy_admin_auth`) by itself. No index setup is needed.

**D - Cloud Storage**
```bash
gcloud services enable storage.googleapis.com
gcloud storage buckets create gs://<project-id>-understudy-recordings --location=us-central1 --uniform-bucket-level-access
echo '{"rule":[{"action":{"type":"Delete"},"condition":{"age":7}}]}' > lifecycle.json
gcloud storage buckets update gs://<project-id>-understudy-recordings --lifecycle-file=lifecycle.json
# only for non-owners:  --role=roles/storage.objectAdmin
```
`.env`: `UNDERSTUDY_GCS_BUCKET=<project-id>-understudy-recordings`

Check: `python scripts/gcp_check.py` (Windows: `venv\Scripts\python.exe scripts\gcp_check.py`), or Mission Control >
AI, security & account > **Check Google Cloud set-up**.

### Docker with Google Cloud

Run the setup script (or at least `gcloud auth application-default login`) on the machine first, then
`./deploy_docker.sh` / `deploy_docker.bat`. When `.env` switches on Vertex AI, Firestore or a bucket, the deploy
script adds `docker-compose.gcp.yml`, which mounts your credentials file **read-only** into the backend container.
It is never copied into the image. Check inside the container:
`docker compose exec understudy-backend python scripts/gcp_check.py`.

**Service account instead of your own login** (servers, CI, shared machines):
```bash
gcloud iam service-accounts create understudy-local --display-name="Understudy"
for r in roles/aiplatform.user roles/datastore.user roles/storage.objectAdmin; do
  gcloud projects add-iam-policy-binding <project-id> \
    --member=serviceAccount:understudy-local@<project-id>.iam.gserviceaccount.com --role=$r; done
gcloud iam service-accounts keys create ~/understudy-sa.json \
  --iam-account=understudy-local@<project-id>.iam.gserviceaccount.com
```
`.env`: `GOOGLE_APPLICATION_CREDENTIALS=/full/path/to/understudy-sa.json`. Keep the file outside the Understudy
folder and never commit or share it. Some organisations block key creation; use your own login instead.

### Path E - Cloud Run (hosted)

1. Open **Cloud Shell** (or a terminal with `gcloud`). On Windows you can also run `deploy_cloudrun.bat` (or
   `.\deploy_cloudrun.ps1`) in the Understudy folder after installing the Google Cloud CLI and `gcloud auth login`;
   it does the same steps (except `--model-armor`, which you can switch on later in Mission Control).
2. Get the code there: upload the Understudy zip (Cloud Shell menu > Upload) and `unzip` it, or `git clone` your
   copy. Then `cd understudy`.
3. Run `./deploy_cloudrun.sh <project-id> [region]`. It enables the APIs, creates Firestore, the image repository,
   the recordings bucket and a least-privilege service account, builds the image with Cloud Build (about 10 minutes
   the first time) and deploys. No API key is involved. Add `--model-armor` to also prepare Google Cloud Model Armor
   (API, template, `roles/modelarmor.user` for the service account) and switch it on (section 2.5.1).
4. Open the printed URL: `/wizard` is Understudy and `/` is Mission Control. The admin password is **generated on the
   first deployment** (9.7), kept in Secret Manager as `understudy-admin-password` and printed once - the published
   default is never used on the public URL. Read it again any time with
   `gcloud secrets versions access latest --secret=understudy-admin-password`; change it by adding a new version
   (`printf 'MyStr0ngPassw0rd' | gcloud secrets versions add understudy-admin-password --data-file=-`) and
   redeploying. Use your own secret with `UNDERSTUDY_ADMIN_PASSWORD_SECRET=<name>`. Only the service account can read
   it, and it never appears in the image or in the service's plain environment variables.
5. The script ends with `python scripts/check_deployment.py <url>` (run it again any time): the Studio, Mission
   Control and the demo portal answer, the service runs on Cloud Run, Gemini is configured (Vertex AI), state is in
   Firestore. In Mission Control, **Check Google Cloud set-up** checks the project side (APIs, roles, bucket).

Update: run `./deploy_cloudrun.sh` again (new revision). Cost control: the service keeps one instance warm
(`--min-instances=1`, so recordings start fast) and allows `UNDERSTUDY_MAX_INSTANCES` (default 3). Lower it with
`gcloud run services update understudy --region=<region> --min-instances=0`. Remove everything with
`gcloud run services delete understudy --region=<region>` (and the bucket / Firestore data if you want).
Limits: with the recordings bucket the deploy script creates, recordings of any size up to 250 MB go straight to
Cloud Storage. Without a bucket Cloud Run accepts at most 32 MB per upload; Step 1 then shows "up to 31 MB" and stops
a larger file before it is sent.

**Tools on Cloud Run (9.9).** An instance's disk is temporary and not shared, so what a tool setup *downloaded* (a JDK,
Maven, Node.js ...) is gone after a restart, a new revision or on a second instance - while the tool's approval and
run settings (in Firestore) stay. Understudy now handles this itself: at start-up, and again before any tool setup,
install or run, it downloads whatever the shared settings point at and this instance lacks (checksum-verified, no AI,
no admin step), and paths keep working when the re-download is a newer patch release. The Cloud Run image also
includes Java + Maven by default (`--build-arg INSTALL_JAVA=false` leaves them out), so Java tools need no download
at all. Mission Control's "needs java" badge now counts the tool's own run settings.

### Troubleshooting

| Symptom (start-up check, Mission Control or the AI error) | Fix |
|---|---|
| `Your default credentials were not found` | `gcloud auth application-default login` (or set `GOOGLE_APPLICATION_CREDENTIALS`) |
| `... API has not been used in project ... / SERVICE_DISABLED` | `gcloud services enable aiplatform.googleapis.com firestore.googleapis.com storage.googleapis.com` |
| `PERMISSION_DENIED` / 403 | grant the role the check names (`roles/aiplatform.user`, `roles/datastore.user`, `roles/storage.objectAdmin`) |
| `The database (default) does not exist` | `gcloud firestore databases create --location=<region> --type=firestore-native` |
| `The specified bucket does not exist` | create it (path D) or fix `UNDERSTUDY_GCS_BUCKET` |
| `Publisher Model ... was not found` | the model isn't offered in that region: Understudy falls back to `gemini-2.5-flash`; set `UNDERSTUDY_VERTEX_MODEL` or `UNDERSTUDY_VERTEX_LOCATION=global` |
| `Request failed (413)` / "too large for the server to receive" when uploading a recording | Cloud Run refuses request bodies over 32 MB. With `UNDERSTUDY_GCS_BUCKET` set (`deploy_cloudrun.sh` does it) the browser uploads the recording straight to Cloud Storage - any size up to `UNDERSTUDY_MAX_MEDIA_MB` (250). Without a bucket, keep recordings under 32 MB or set the bucket and redeploy |
| `Billing ... disabled` | link a billing account to the project |
| `quota project` warning | `gcloud auth application-default set-quota-project <project-id>` |
| `RESOURCE_EXHAUSTED` / 429 | wait a minute or request more quota (IAM & Admin > Quotas) |
| `API key not valid` (path A) | create a new key at https://aistudio.google.com/apikey |
| Docker: `set UNDERSTUDY_GCP_CREDENTIALS_FILE` | start with `deploy_docker.*` (it fills it in) after `gcloud auth application-default login` |
| Mission Control shows **State store: Local files (Firestore unavailable)** / banner says `LOCAL FILES - Firestore is switched on but unavailable` | Understudy keeps working on local files and retries Firestore every minute; fix what the check reports (usually credentials), no restart needed |
| Settings in `.env` seem ignored | restart Understudy (it reads `.env` at start); variables set in the shell or Docker take precedence |

---

## 2. Admin guide (Mission Control)

Mission Control (`/`) is the admin console, and it is **password protected**.

> **Admin login (9.7.1 - the same everywhere: local, Docker and Cloud Run)**
> Username: **`admin`**
> Password: **unique per installation** - generated on the **first start**, saved, and shown **once**.

| Started with | Saved in | Show the password again |
|---|---|---|
| `run_demo.bat` / `.ps1` / `.sh`, `python server.py` | `.env` (`UNDERSTUDY_ADMIN_PASSWORD`) | `python scripts/admin_password.py show` (Windows: `venv\Scripts\python.exe scripts\admin_password.py show`) |
| `deploy_docker.sh` / `.bat` | `.env` (passed into the containers) | `grep UNDERSTUDY_ADMIN_PASSWORD .env` (Windows: `findstr UNDERSTUDY_ADMIN_PASSWORD .env`) |
| `deploy_cloudrun.sh` / `.ps1` / `.bat` | Secret Manager `understudy-admin-password` | `gcloud secrets versions access latest --secret=understudy-admin-password` |

* Every later start reuses the saved password - nothing is generated again, so restarting never changes it.
* A new saved password: `python scripts/admin_password.py reset` (used from the next start; Cloud Run: add a secret
  version and redeploy). Your own: put it in `.env` as `UNDERSTUDY_ADMIN_PASSWORD=...` before starting.
* The published fallback `Understudy@2026` is only used if nothing could be saved (e.g. a read-only container started
  by hand without the setting); the banner and Mission Control then show a **default password** warning.
* The password fields have an **eye** button to show or hide what you typed.
* To use your own, open **AI, security & account > Admin account** and set a new password (and optionally a new
  username). It takes effect immediately and signs out other admin sessions.
* A password changed in Mission Control is kept **in memory only** and lasts until the next restart (Cloud Run: the
  next revision); then the saved password above applies again.
* Five wrong logins in a row lock logins for 60 seconds (on Cloud Run the counters are shared by every instance).
  Sessions last 12 hours or until you log out.
* **Theme (7.2).** The **Dark / Light** button at the top right of every page (also on the login screens) switches the
  whole application. The choice is stored in the browser (`localStorage`, key `understudy_theme`), shared by Mission
  Control and the Studio, and applied before the page paints, so there is no flash. Dark is the default.
* The Gemini key, model and security-alert endpoints are admin-only too. Scripts can keep using a static
  `UNDERSTUDY_ADMIN_TOKEN` (sent as `X-Admin-Token`).

The header shows whether AI is active, the model in use, the **inbox bell** (8.0: the number of tool requests that need
you; a green dot means live updates over a WebSocket, grey means it is polling), who is signed in and the Dark / Light
theme switch. New requests, questions from the AI and finished setups also appear as **toasts** in the bottom right -
click one to jump to the request. Below it, five tiles show sessions, AI
calls (answered / failed), heuristic fallbacks, blocked inputs and pending tool requests. The sections are a menu on the
left (an *Icons only* mode; a scrollable row on phones), and every box folds (*Collapse all / Expand all*):

| Tab | What's there |
|---|---|
| **Telemetry** | the live activity log (2.0) |
| **Users** | Studio accounts: create, ask to reset, block / unblock, delete; self-signup and its email domains; email status (2.0.1, 2.0.2) |
| **Access & tools** | tools, versions, per-tool access, enablement requests, the AI install assistant and setup agent (2.2, 2.2.6, 2.2.7, 2.3) · **package managers & build tools** (2.2.1) · **tool registry** and **sandbox** (2.2.2, 2.2.3) · target URL lists (2.1) · network (2.1.2) · self-healing (2.4) |
| **Environments** | every runtime version on the server (Java, Python, Node.js, Ruby, Go, .NET, Maven, Gradle), the version each tool uses and needs, downloads (2.4.2) |
| **Escalations** | runtime-help requests from users' runs: apply the AI's fix or your own, for everyone or one user, and re-run (2.4.3) |
| **Learned skills** | lessons Understudy wrote from real errors, and applies before the next run (2.6) |
| **Code health** | AI self-heal of start-up warnings (deprecations in Understudy's own code) and runtime / dependency health (2.7) |
| **AI, security & account** | Gemini key and model (2.5) · Google Cloud check · **Google Cloud Model Armor** (2.5.1) · Model Armor interceptions · admin account |

### 2.0 Telemetry: what users do and what the AI was asked
Every event is listed in order and can be expanded:

* **User**: session started (URL, SOP, recording), tool and version picked, payload edited (secret values shown as
  `***`), SOP edited, rules confirmed, code generated, compliance check, run started, retry approved or declined, ZIP
  downloaded.
* **AI request**: the model, the masked key, the attached recording, and **the exact system prompt and user prompt**
  sent to Gemini.
* **AI response**: the raw text Gemini returned, the JSON Understudy accepted, and anything dropped by validation, with
  the duration.
* **AI error**: why a call failed, in plain words (invalid key, quota, model not available, timeout, unusable answer)
  and the raw error.
* **Heuristic fallback**: which step fell back to local code, why, and **what the local heuristic produced** (the step
  plan, parameters, rules, template code or diagnosis).
* **Pipeline, Execution, Runtime, Model Armor, Admin**: step progress, every automation log line, dependency installs
  and runtime repairs, blocked inputs, and admin changes.

Every event of a run shows its **context line `[User] | [Project Name] | [Session Name]`** (7.0), and you can filter
by **user**, **project**, session, event type or text (the API takes `?user=` and `?project=`). The automation's own
log lines carry the same prefix, e.g. `[10:42:07] [alice] | [Supplier invoices] | [Run 2] | STEP 3 OK: Enter the Total
Amount` - in the Step 4 live log, the ZIP's `evidence/execution_log.txt`, the dependency install log and the
backend's console log. (The prefix is stripped again before logs are given to the AI or used as
a learned-skill fingerprint, so names never influence a diagnosis.)

The feed only follows new events while you're at the bottom. Scroll up and it
stays where you are; a **N new events** button takes you back down. **Pause** freezes it. API keys are never recorded.
The log is also appended to `generated/wizard/activity.jsonl` (`UNDERSTUDY_ACTIVITY_LOG`).

### 2.0.1 Studio users (7.0, email and OTP since 8.1)
Nobody can use the Studio (`/wizard` and every `/api/wizard`, `/api/projects`, `/api/history` call) without logging in.
Accounts come from two places, and **one email address can belong to only one account**:

* **Users > Create user** (admin): a username (3-40 letters, digits, `_ . @ -`; `admin` and a few others are
  reserved), the user's **email (required)** and optionally a password (8+ characters with letters and digits).
  * With a password: tell the user yourself; it is **never shown or returned again**.
  * **Without a password (recommended):** the welcome email tells the user to choose **Forgot password** on the login
    screen and set their own with an emailed code. You never know their password.
  * Either way a welcome email (username, how to log in - never the password) is sent.
* **Self-signup** (Studio login screen > **Sign up**): username, email, password and confirmation. A 6-digit code is
  sent to the email; the account is created only when the code is entered (so nobody can register someone else's
  address). **Users > Allow self-signup** turns it on or off at once (saved in the admin policy, so it survives
  restarts and applies to every Cloud Run instance); when off, the Studio's Sign up tab disappears and only accounts
  you create can log in. `UNDERSTUDY_SIGNUP=on|off` is only the default until you use the switch. **On Cloud Run
  the default is off** (`deploy_cloudrun.sh` sets `UNDERSTUDY_SIGNUP=off` unless you deploy with
  `UNDERSTUDY_SIGNUP=on`), because the service is reachable from the internet.
* **Self-signup email domains (8.2).** Under the switch, **Self-signup email domains** limits who may register: add
  `companyname.com` (or several; `*.companyname.com` includes its subdomains) and **Save domains** - from then on only
  addresses ending in `@companyname.com` can sign up; anyone else gets *"Sign-up is limited to email addresses ending
  in @companyname.com"* (the Studio's sign-up form shows the rule and checks it as you type, the server checks it again
  when the code is sent and when it is entered). Leave the list empty to allow any domain. Accounts you create in
  Mission Control are not limited. `UNDERSTUDY_SIGNUP_DOMAINS=companyname.com` sets the default until you save a list;
  **Use server default** goes back to it. API: `PUT /api/admin/signup/domains {domains: [...] | [] | null}`;
  `GET /api/auth/options` returns `signup_domains`.

What the admin can do with an account (and what not):

| Action | Effect | Email to the user |
|---|---|---|
| **Ask to reset** | asks the user to set a new password | a notice (no code, no password) |
| **Block** (reason required) | the user can't log in or reset the password; every open session ends at once | the reason + "contact your administrator at `UNDERSTUDY_ADMIN_CONTACT`" |
| **Unblock** | the account works again (old tokens stay dead) | "your account is active again" |
| **Delete** (reason required, type the name to confirm) | removes the account; projects and sessions are kept for audit under `alice~deleted-<timestamp>`; the email address becomes free | the reason + the admin contact |
| **Add email** | only for accounts created before 8.1 that have none; once set it can't be changed from Mission Control (that would let an admin take the account over with a reset) | - |
| Set or see a password | **not possible** (there is no such endpoint) | - |

* **One email = one account, also with several servers.** Each address is reserved with an atomic "create only if
  it doesn't exist" (a `user_emails/<hash>` claim: an exclusive file create locally, Firestore's `create()` on Cloud
  Run), so two instances registering the same address at the same moment can't both succeed; usernames are created
  the same way. Deleting an account releases its address; claims for accounts created before 8.1.1 are added at
  start-up.
* The Users table shows username, email (✓ when verified by a code), projects / sessions, last login and status
  (`active`, `blocked` with the reason). **Find by username or email** filters it.
* If a mail can't be delivered, the action still happens and Mission Control says the mail was not sent (and why).
* Passwords are stored only as salted PBKDF2-SHA256 hashes; accounts live in the durable store (local files, or
  Firestore on Cloud Run) and survive restarts. Five wrong logins lock that username for 60 s (thirty from one address
  lock the address); tokens last 12 h; logout revokes a token everywhere, also after a restart.
* A user only ever sees their own projects and sessions; anything else answers *not found*. The token is sent as the
  `X-User-Token` header; only the recording and ZIP downloads also accept `?access_token=` (a `<video>` / link cannot
  send headers).
* API: `GET/POST /api/admin/users`, `POST /api/admin/users/{u}/block {reason}`, `POST /api/admin/users/{u}/unblock`,
  `DELETE /api/admin/users/{u} {reason}`, `POST /api/admin/users/{u}/password-reset`, `PUT /api/admin/users/{u}/email`,
  `GET /api/admin/mail`, `POST /api/admin/mail/test {to}`, `GET/PUT /api/admin/signup {enabled: true|false|null}`
  (null = back to `UNDERSTUDY_SIGNUP`). Users: see section 3, Step 0.
* `UNDERSTUDY_USER_LOGIN=off` switches the login requirement off (single-user local mode, older test suites).

### 2.0.2 Email set-up (free) - OTP codes and account notices (8.1)
Understudy sends mail with Python's standard `smtplib` through **any SMTP server** - nothing to install. Codes and
notices go out for: signup verification, Profile > Reset password, Forgot password, welcome, block, unblock, delete,
"ask to reset" and "your password was changed". Configure it in `.env` (or the environment / Docker / Cloud Run) and
restart; **Users > Email** in Mission Control shows the mode and has **Send test**. The SMTP password stays in `.env`:
it is never shown in Mission Control, logged, sent to telemetry or put in the exported ZIP.

**Option A - Gmail (free, about 500 mails a day).**
1. Turn on 2-Step Verification for the Google account: <https://myaccount.google.com/security>.
2. Create an **App Password**: <https://myaccount.google.com/apppasswords> → name it "Understudy" → copy the
   16-character password (the spaces don't matter).
3. In `.env`:
   ```env
   UNDERSTUDY_SMTP_HOST=smtp.gmail.com
   UNDERSTUDY_SMTP_PORT=587
   UNDERSTUDY_SMTP_SECURITY=starttls
   UNDERSTUDY_SMTP_USER=you@gmail.com
   UNDERSTUDY_SMTP_PASSWORD=<your-16-character-app-password>
   UNDERSTUDY_MAIL_FROM=you@gmail.com
   UNDERSTUDY_ADMIN_CONTACT=it-admin@yourcompany.com     # shown in "blocked"/"deleted" mails
   ```
4. Restart Understudy, open Mission Control > Users > Email > **Send test**.
   Google Workspace accounts work the same way (an admin may have to allow App Passwords).

**Option B - Brevo (free plan, 300 mails a day, any sender domain you verify).**
1. Sign up at <https://www.brevo.com/>, then **Settings > SMTP & API > SMTP**: note the *login* and generate an
   *SMTP key*.
2. Add and verify your sender address (**Senders, domains & dedicated IPs**).
3. `.env`: `UNDERSTUDY_SMTP_HOST=smtp-relay.brevo.com`, `UNDERSTUDY_SMTP_PORT=587`, `UNDERSTUDY_SMTP_USER=<login>`,
   `UNDERSTUDY_SMTP_PASSWORD=<SMTP key>`, `UNDERSTUDY_MAIL_FROM=<verified sender>`.

**Not Outlook.com:** Microsoft retired password-based SMTP (basic authentication) for personal Outlook.com accounts
in September 2025; they need OAuth 2.0, which this mailer doesn't do. Any company SMTP relay that accepts a username
and password works like the options above; use `UNDERSTUDY_SMTP_SECURITY=ssl` for port 465, `none` only for a relay
on a trusted network. A step-by-step version of this section is kept as the "Free SMTP Setup for Understudy OTP
Emails" guide.

**Without SMTP (development):** the mode is `outbox` - mails are appended to `generated/mail/outbox.jsonl` on the
server instead of being sent. Read them with `python scripts/mail_outbox.py` (codes are only in that file; the API
and Mission Control never return a code). Mission Control and the Studio's signup / forgot screens show that mail
isn't being delivered. `UNDERSTUDY_MAIL_MODE=off` switches mail off entirely.

**Docker:** put the values in `.env`; `docker-compose.yml` passes them through. **Cloud Run:** export
`UNDERSTUDY_SMTP_HOST`, `_PORT`, `_USER`, `UNDERSTUDY_MAIL_FROM`, `UNDERSTUDY_ADMIN_CONTACT` before
`./deploy_cloudrun.sh`, and store the password in Secret Manager:
```bash
printf '%s' '<your-app-password>' | gcloud secrets create understudy-smtp-password --data-file=-
UNDERSTUDY_SMTP_PASSWORD_SECRET=understudy-smtp-password ./deploy_cloudrun.sh <project-id>
```
Cloud Run allows outbound SMTP on port 587 / 465 (port 25 is blocked).

### 2.1 Target URL lists: allowed and blocked (6.2)
Both lists live in the **Access & tools** tab and apply to the analysis and inspection browser, redirects and the
network traffic of the generated automation itself (section 2.1.2).

**Allowed list (Target URL exceptions).** Understudy blocks internal, private and unresolvable addresses by default
(SSRF protection), for example `10.x.x.x`, `192.168.x.x`, `localhost`, or `erp-uat.corp` that the server can't resolve.
To let users test against such an app, add its URL:

* The URL's **host** is then allowed on any port and path.
* Every entry is shown to users as a one-click target in Understudy's Step 1.
* **Portal 1** (`http://localhost:8000/portal1`) and **Portal 2** (`http://localhost:8000/portal2`) are in the list
  by default (Portal 3 is on the same host, so it is reachable too). You can remove them.
* Cloud-metadata and link-local addresses (such as `169.254.169.254`) can **never** be added. A browser pointed at them
  could leak the server's cloud credentials.
* Exceptions don't switch off prompt-injection screening of the URL text.

**Blocked list (Blocked URLs).** The opposite: hosts Understudy must never touch.

* Add a link such as `https://social.example.com`; every port and path of that host is refused. Use
  `*.tracker.example.net` to block every sub-domain (the bare `tracker.example.net` is not covered).
* A blocked host fails Step 1 ("on the blocked list set by your admin ... Reason: ..."), is aborted if a page
  redirects or loads something from it, and is refused again right before a run starts (you may have blocked it after
  Step 1). A blocked target is not retried by self-healing: no retry can change an admin policy.
* **Blocked beats everything**: allowed-list entries, `UNDERSTUDY_ALLOWED_PRIVATE_HOSTS` and the private-network switch.
* The built-in never-allowed addresses (cloud metadata, link-local) are listed under the panel; there is nothing to add.
* Changes apply from the next request (and on every Cloud Run instance within a few seconds).

**A link can't be on both lists.** Adding a link to one list while the other list contains it - or contains a wildcard
that covers it, or (for a wildcard) a host it would cover - is refused, in the UI (the Add/Block button is disabled and
the reason is shown) and in the API (HTTP 400, "A link cannot be both allowed and blocked - remove it from the other
list first"). To move a link, remove it from one list, then add it to the other. Hand-edited policy files that
break the rule are handled safely: *blocked wins*.

### 2.1.2 Restricted network for automation runs (6.2)
Generated automation is code, and code can open connections. Every run now gets its own small **egress proxy**
(`core/wizard/egress.py`) and the child process is started with `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` pointing at
it. The proxy decides per destination host:

| Rule | Restricted (default) | Public internet |
|---|---|---|
| Blocked list, cloud metadata, link-local | denied | denied |
| The run's target host and the allowed list | allowed | allowed |
| Any other host | **denied** | allowed only if it resolves to a public address |
| Private / loopback / reserved ranges not on the allowed list | denied | denied |

Choose the mode in **Network for automation runs** (Access & tools). The proxy connects to the exact IP it validated, so
a DNS-rebinding trick can't swap it afterwards. The run's live log shows `EGRESS BLOCKED: <host> - <why>`, Step 4 shows
a "Network policy" line (mode, reachable hosts, refused requests), and the result is stored in `execution.egress`.
The built-in Playwright driver applies the same decision to every request it makes. The child environment never holds
the Gemini key, the admin token or cloud credentials. Set `UNDERSTUDY_EGRESS=off` only to debug.

*What this is and isn't.* It is an application-level control that covers everything honouring the proxy variables
(Python requests/urllib/httpx, curl, Chromium through Playwright/Selenium/Puppeteer/Cypress, Java tools). It does not
stop a program that deliberately opens raw sockets, and Chromium never proxies loopback. For hard isolation run
executions in their own Cloud Run job / container with a VPC egress firewall (see ARCHITECTURE section 7e).

### 2.2 Tool registry, enablement requests and the AI tool synthesizer (8.0)
**Zero trust, nothing pre-built (8.2).** Users can only automate with tools that are **ACTIVE** in the registry, in the
versions you allow. A fresh install has **none** - not even Playwright. (An existing `admin_policy.json` keeps the tools
it already has. `UNDERSTUDY_DEFAULT_TOOLS=playwright` pre-seeds Playwright (Python) as 8.0/8.1 did, `catalog` the 8
catalog tools for demos and older test suites.) Every tool or version gets in only through the AI install assistant
and the synthesizer below, and only after its AI-written smoke test was reviewed by you and passed. The one exception
is **Understudy Gemini Agent**, the built-in agentic tool (always active).

**Agentic tools on Understudy's agent (9.9).** Access & tools > **Agentic tool on Understudy's agent**: set up more
agentic tools on the same agent as the built-in one, with the browser stack you choose - like a traditional stack:
* **Browser library** - Playwright or Selenium WebDriver (the agent runtime is Python);
* **Browser** - Playwright: Chromium, Chrome, Edge, Firefox, WebKit; Selenium: Chrome, Edge, Firefox;
* **Versions** - of the library (a plain version such as `4.25.0` pins it) and of `google-genai`;
* **Extra packages** (pip, up to 8), a **default Gemini model** and a **name**.

**Set up and smoke-test** installs it in the sandbox and runs Understudy's **agent smoke test**: the agent opens a test
page, types a name, clicks Go and checks the result - in self-test mode, so no Gemini key is needed and there is no
AI-written test to review. The tool is **ACTIVE** only when it passes (a failure stops with a question, like any setup);
users then see it in the agentic column next to Understudy Gemini Agent. Its runs and its ZIP get
`UNDERSTUDY_AGENT_DRIVER` / `UNDERSTUDY_AGENT_BROWSER`; the decisions, guard rails and secret masking are the same.

**The AI install assistant (8.2).** Access & tools > **AI install assistant**: type a tool (*Selenium*, *Playwright*,
*Cypress*, *Karate* ...) and an optional note (*"we only use Java"*, *"BDD with Cucumber"*), then **Ask the AI**
(prompt `TOOL_STACK_SUGGEST`). The request card shows *Choose stack - your turn*:
1. **Language** - every language the tool officially supports; the AI's recommendation has a star.
2. **Package managers on your server** - Maven, Gradle, pip, npm, npx, ... for that language. The ones found on this
   server are pre-ticked; untick what you don't have, tick what you do. Understudy installs with pip (also for Poetry /
   Pipenv stacks), npm (npx, Yarn, pnpm), Maven, Gradle, RubyGems, Go modules and NuGet. A package manager you switched
   off in **Package managers & build tools** (below) is shown locked; one Understudy has no installer for (e.g. sbt) is
   shown as *not supported yet*.
3. **Components and versions** - the core package (always installed) and the companion frameworks it is combined with,
   each with a one-line purpose: for Selenium + Java e.g. Selenium WebDriver, TestNG *or* JUnit 5 (alternatives - ticking
   one unticks the other), Cucumber, WebDriverManager. Each ticked component has a version picker filled with the
   **real versions from its package registry** (Maven Central, PyPI, npm), the AI's recommendation pre-selected.
4. **Prepare the setup plan** - Understudy turns your choice into the tool spec (package names only from the AI's
   suggestion and checked against the ecosystem's grammar, versions validated, the run command checked against the
   runner allow-list) and the AI prepares the plan (Phase A below). *Ask again* with a note re-asks the AI; *Change the
   stack* on a plan goes back to the picker.
A request a **user** sent (e.g. their SOP needs Cypress) offers the same **Ask the AI for the stack** button (or *Skip -
prepare the setup directly*). When your composed stack gets another id than the tool the user asked for (e.g. `cypress`
→ `cypress-javascript`), the request's id is kept as an alias, so the waiting session and the SOP requirement resolve to
it. API: `POST /api/admin/tool-setup {name, instruction, assistant: true}`, `POST /api/admin/tool-requests/{id}/stack/suggest`,
`GET /api/admin/tool-requests/{id}/stack/versions?language=&package_manager=`, `POST /api/admin/tool-requests/{id}/stack/select`.

**How a request reaches you.**
* **The SOP requires a tool** ("Use Robot Framework for this API test", "Automate it with Selenium 4.24"). The WATCH
  agent reports it (`sop_tool_requirement`, checked against the SOP text so the model can't invent one) and a
  deterministic reader backs it up (`core/wizard/tool_requirements.py`). If the tool or version isn't ACTIVE, Step 1
  and Step 2 show an **Unsupported Framework** warning with an **Enablement Request** form. Sending it sets the session
  to **PENDING ADMIN APPROVAL** - Build and Execute are refused (409) - and pushes a notification to your inbox. The
  user can also stop waiting and pick an active tool; that choice needs an explicit confirmation and is recorded in the
  session's activity log.
* **A user picks or types a tool that isn't active** - also when the SOP names no tool and the AI didn't suggest it; a
  version can be typed with the name (`Cypress 13.6`, `cypress@13.6.6`, `selenium==4.25`) - or asks for a version you
  haven't allowed (as before).
* **You add one yourself:** the *AI install assistant* above.
A blocked tool or version can't be requested. One request per tool is shared by every session that asked; the version
the SOP named is proposed first (a partial version such as `13.6` means the newest published `13.6.x`).

**The synthesizer, in three phases** (each request card shows the lifecycle *Requested → Choose stack → AI plan → Install
→ Review test → Smoke test → Active*):
1. **Phase A - the plan and its manifest.** *Prepare setup with AI* (prompt `TOOL_SETUP_PLAN`) shows how the tool will
   be downloaded and installed, the exact package-manager commands, the files Understudy writes, the change to the
   registry (a diff), the versions users may pick, how automation code will be written and the **manifest**: OS
   dependencies (checked binaries or variables such as `java` or `JAVA_HOME`), environment variables, the interpreter
   and the **runner wrapper** (`run_tool.sh` / `run_tool.cmd`) Understudy generates from it - never
   free-form AI code. Nothing is installed yet.
2. **Phase B - your constraints.** Type instructions in plain words and click *Revise plan*, for example *"download
   from https://nexus.corp/repository/pypi-group/simple"*, *"use the wheels in /opt/wheels"*, *"use the local
   python3.10"* or *"only allow 4.25.0"*. Each instruction passes Model Armor; URLs, folders, interpreters and versions
   are validated before they are used, and the conversation keeps every step.
3. **Phase C - install, smoke test review, clarification loop.** *Approve & set up* installs the tool in the sandbox
   (2.2.3); the AI then writes the smoke test and the setup **pauses for your review** (2.2.4) - it runs only after you
   approve it. If any step fails - a missing OS dependency, a blocked
   download, a native build error, a failing smoke test - **the setup stops**. The chat shows the command, its exit code
   and the captured stdout / stderr, and the AI (prompt `TOOL_SETUP_CLARIFY`; built-in diagnosis without AI) asks one
   concrete question with buttons, for example:
   > *Admin, the installation of Selenium Java failed because Java isn't available (JAVA_HOME is not set) in this
   > local environment. Should I download OpenJDK dynamically, or can you provide the local path?*
   >
   > [Download OpenJDK 17 (Eclipse Temurin)] [I'll give you the JDK path] [I fixed it on the server - try again]

   Click an option or answer in your own words (*"JAVA_HOME is /opt/jdk-17"*, *"use https://nexus.corp/repository/npm/"*,
   *"go through http://proxy.corp:3128"*). The AI turns the answer into checked changes (prompt `TOOL_SETUP_RESOLVE`;
   a deterministic reader without AI) - environment variables, a package source, a `PATH` entry, a runtime download, the
   sandbox mode, a version - and the setup **resumes** from the start with them. It can also ask a follow-up question
   or stop. Downloaded runtimes (Temurin JDK 17/21, Node.js 20/22) are verified against their published SHA-256
   checksums and kept in `generated/runtimes`. After 8 rounds the setup stops for good; prepare a new plan or reject.
   There is **no "add without a verified install"** any more: a tool is ACTIVE only after its smoke test passed.

When the smoke test passes, the tool becomes **ACTIVE** with the versions you chose, every session that was waiting for
it continues by itself (the tool is selected, the session leaves PENDING), and the users get a live update. **Reject**
(with an optional note) releases the waiting sessions with your note.

### 2.2.1 Package managers & build tools (8.2)
Access & tools > **Package managers & build tools** lists every package manager the assistant can offer - pip, Poetry,
Pipenv, npm, npx, Yarn, pnpm, **Maven**, **Gradle**, RubyGems, Bundler, Go modules, NuGet - with the programs each one needs
and whether this server has them (path and version), plus the JDK Maven / Gradle builds will use.
* **On / off switch.** Only switched-on package managers can be picked in the assistant; switching one off affects new
  tool setups (tools that are already ACTIVE keep working). Everything Understudy can install with is on by default.
* **npm tools see their own packages.** Before a script runs, its folder gets a `node_modules` link (a junction on
  Windows) to the tool's isolated environment, removed again afterwards. Without it, test runners that load the tool
  themselves found two copies (Playwright Test: *"did not expect test() to be called here ... No tests found"*).
* **Missing on the server?** For Gradle, Maven, Node.js and Java the card offers **Download and register**: Gradle 8 or 9
  (newest, from `services.gradle.org`, SHA-256 verified), Apache Maven 3 (newest, from Maven Central, SHA-512 verified),
  Node.js 20/22 LTS, Temurin JDK 17/21. The download goes to `generated/runtimes` - nothing is installed system-wide -
  and is shown live. Ruby, Go and .NET must be installed on the server yourself.
* **Already installed somewhere else?** **Registered toolchains > Use this folder**: pick Gradle / Maven / Java (JDK) /
  Node.js and type its folder (its home, its `bin` folder or the program itself, e.g. `C:\Tools\gradle-8.10`).
* **Registered toolchains** are put in front of `PATH`, and `GRADLE_HOME` / `MAVEN_HOME` / `JAVA_HOME` are set, for every
  install, smoke test and automation run - a registered JDK therefore also fixes a `JAVA_HOME` that points at an old JDK.
  Remove a registration with the bin icon.
* The setup chat can download them too: when a setup fails because `gradle` or `mvn` is missing, the AI offers
  *Download Gradle* / *Download Apache Maven* (registered for the whole server); a user's run that fails the same way is
  repaired by the same download (2.4.1).

**Gradle tools** use `build.gradle` + `settings.gradle` written by Understudy (never by the AI): every approved package as
a test dependency from Maven Central (or the admin's repository), **TestNG** when it is in the stack, otherwise the
**JUnit Platform** (with the matching `junit-platform-launcher`), `options.release` for the JDK Gradle uses (same rule as
pom.xml), and an `understudyResolve` task that downloads every jar at install time. Tests live under `src/test/java` and
run with `gradle --no-daemon --console=plain test`; the exported ZIP's `run.sh` / `run.bat` do the same.

Adding a package manager Understudy has no installer for (sbt, Bazel, Gradle Kotlin DSL-only builds, ...) still needs a
code change: an ecosystem in `core/wizard/frameworks.py` and a manifest + install plan in `core/wizard/dependency_resolver.py`
(see ARCHITECTURE §7i). API: `GET /api/admin/package-managers`, `PUT /api/admin/package-managers/{id} {enabled}`,
`POST /api/admin/toolchains/download {key}`, `PUT /api/admin/toolchains {kind, path}`, `DELETE /api/admin/toolchains/{kind}`.

### 2.2.2 Tool registry
The **Tool registry** card lists every tool with its status: `ACTIVE`, `REQUESTED`, `CHOOSE_STACK` (8.2: pick the
stack), `PROPOSED`, `INSTALLING`, `SMOKE_REVIEW` (8.2: review the AI's smoke test), `SMOKE_TESTING`, `NEEDS_ADMIN` (the AI
asked you something), `FAILED`, `REJECTED`. Active tools show their versions and their last smoke test; **Re-run smoke
test** runs the exact test you reviewed again in the current sandbox (useful after a server change) - no AI needed.
API: `GET /api/admin/tool-registry`, `POST /api/admin/tools/{id}/smoke`.

**Access (9.0).** Each approved tool has an **Access** button in Access & tools:
* **All users** (the default).
* **Selected users**: tick the Studio users who may use it (with a search box). Everyone else sees the tool locked, with
  "available to selected users only", and the server refuses it (`403`) if they pick it anyway.
* **No user** (9.9): nobody can pick or run the tool, but it stays approved and installed, with its versions and
  settings - switch it back to All users or Selected users any time. Users see it locked as "turned off by your
  admin"; a session already using it is stopped with that message at its next build or run.

**Delete** removes the tool for everyone and deletes its isolated environments on the server (sessions that used it
are told the next time they run). Without Studio accounts (`UNDERSTUDY_USER_LOGIN=off`) there is no one to restrict,
so All users / Selected users make no difference - but **No user** still turns the tool off. API:
`PUT /api/admin/tools/{id}/access` `{"mode": "all"|"selected"|"none", "users": [...]}`, `DELETE /api/admin/tools/{id}`.

### 2.2.3 Sandbox
Tool installs, smoke tests and automation runs all go through one sandbox runner (`core/wizard/sandbox/`), the local
process sandbox. Understudy runs on **Google Cloud Run**, which has no Docker daemon (no privileged containers), so there
is no Docker sandbox (removed in 9.9.1; a Docker / Auto mode saved by an older version is read as local):
* Each tool and version gets its own folder (`npm --prefix <env>`, `python -m venv`, Maven local repository).
* Processes start with a minimal, allow-listed environment (no Gemini key, admin token or cloud credentials), an OS
  time-out, and a watchdog that kills the whole process group when its memory goes over the limit.
* Everything runs inside the Cloud Run instance, which Google already isolates in its own gVisor sandbox. Automation
  runs reach the network only through Understudy's per-run egress proxy (2.1.2); tool installs and smoke tests have the
  instance's network (restrict it with Cloud Run VPC egress and firewall rules if you need to).

The **Sandbox** card sets the limits: memory, run time-out and install time-out. API: `GET/PUT /api/admin/sandbox`
(`{"memory_mb": ..., "timeout_s": ..., "install_timeout_s": ...}`).

### 2.2.4 Smoke test - written by the AI, reviewed by you (8.2)
After an install the AI writes a small test for exactly the stack you selected (prompt `TOOL_SMOKE_TEST`) - every
component is exercised, e.g. a TestNG `@Test` plus a Cucumber feature for *Selenium + TestNG + Cucumber*. **There are no
built-in templates any more**; without Gemini the setup stops and asks you to configure it. The test opens a dummy page
served by the sandbox itself, types, clicks, asserts the result and prints `UNDERSTUDY_SMOKE_OK` (API tools call a dummy
JSON endpoint). Understudy writes the dependency manifest itself (e.g. `pom.xml`, marked *Understudy* in the review) -
the AI's own `pom.xml` / `package.json` / `requirements.txt` are ignored - so the test proves exactly what users will
install.

The request then waits in **Review test**: the card shows every file with the AI's note on what it proves. **Approve &
run smoke test** runs it (the install is reused, not repeated); or type what should change (*"use assertEquals from
TestNG"*, *"add a Cucumber feature file"*) and **Ask the AI to rewrite** - it comes back for review (up to 6 rewrites;
your comments pass Model Armor). Before anything runs the files are checked: no subprocesses, `eval`, other hosts,
credentials, other environment variables, or `Cypress.env()`. **Exit code 0 and the marker = PASSED** → the tool becomes
ACTIVE; anything else sends the stack trace to the clarification loop, which offers **Rewrite the smoke test** (you review
the new one) or **Run the same smoke test again** (after you fixed the server). For Maven tools the card also shows the
JDK Maven uses and the Java release `pom.xml` targets; both are pinned for every later run (2.4.1).

**Cypress 15.10+ / 16.** Cypress 16 removed `Cypress.env()` (*"Cypress.env() is no longer supported ... The call accessed
was: SMOKE_URL"*, the 8.1.1 report) and its replacements (`cy.env()`, `Cypress.expose()`) don't exist before 15.10. Smoke
tests and generated suites therefore read environment variables only in `cypress.config.js` (Node side) and hand them to
the spec through a task (`cy.task('understudyEnv')`), which works with every Cypress version. `Cypress.env()` /
`Cypress.expose()` / `cy.env()` are rejected in smoke tests and are a blocking finding in generated suites.

### 2.2.5 Live inbox
Mission Control and the Studio listen on WebSockets (`/api/admin/ws`, `/api/wizard/ws`; the first message carries the
token). Requests, AI questions, finished setups and paused / resumed sessions arrive at once. Where WebSockets can't
pass (for example behind the Next.js proxy on Cloud Run) both fall back to polling (`GET /api/admin/inbox`,
`GET /api/wizard/events`) - the dot on the bell shows which one is in use.

### 2.2.6 Setup repair agent (8.5)
When a tool setup fails (install or smoke test), Gemini doesn't give one guess and stop: the **setup repair agent**
stays on the problem until the setup passes, with you approving every change.

* It investigates with **read-only checks in the tool's sandbox** - a dry run of the test (`robot --dryrun`,
  `py_compile`, `node --check`), the packages really installed, the real keyword / API signatures of the **installed**
  library version, runtime versions - and **checks its own fix before you see it** (a corrected test must pass
  Understudy's safety check and its dry run; otherwise the agent gets the result and corrects itself).
* You see the fix with the **complete test** and what it checked: **Approve** (Understudy applies it, re-runs the setup
  and, if something else fails, the agent continues with the full history), **Not right - look again** with a hint, or
  **Stop**.
* When the same error comes back, a **stronger model** is offered as a button (*Let <model> look*; setting, default
  `gemini-pro-latest`) - the agent never switches models by itself.
* Understudy's own certain fixes come first (a missing package, a missing init step such as `rfbrowser init`, a
  misplaced test file). In *automatic* mode (Access & tools > Self-healing > Setup agent) the agent applies
  package-only fixes itself; a new test or another tool version always waits for you. Every fix that made a setup
  pass is remembered as a lesson for future setups.
* Before installing anything, a setup checks that each runtime the tool needs **works for what the build really uses**
  (a JDK with `javac` for Maven / Gradle, `npm` for Node.js, `gem` for Ruby, an SDK for .NET, a `JAVA_HOME` that is
  verified, not trusted). A runtime that doesn't work is replaced by a working install Understudy already has; if there
  is none, the setup stops and asks before downloading one (*"to set up X I need Java (JDK) - may I download Eclipse
  Temurin JDK 21?"*). What you chose or approved in a setup is never replaced automatically.

### 2.2.7 Per-tool access (9.0)
For every approved tool, **Access & tools** has an **Access** setting: **All users**, **Selected users** (a
checklist), or **No user** (turned off for everyone, still installed). The built-in Understudy Gemini Agent is not
listed in Mission Control: it is always available to every user and has nothing to set. Other users see the tool as locked and can request it. **Delete** removes the tool for everyone and frees
its isolated environments on the server.

### 2.3 Tool versions
Click **Versions** on an approved tool to see the versions of its package available right now in its registry (PyPI,
npm, Maven Central, RubyGems, NuGet, Go proxy), newest first, with the latest version and the one installed on the
server. Tick the versions users may use and star the default, then **Save versions**.

* Users get the default version. If you allow several, they see a version picker after choosing the tool that lists
  **exactly** the versions you allowed (marking your default). Their pick - not your default - goes into the generated
  manifest, the sandbox install plan, the pre-run install and the ZIP; switching versions re-plans the sandbox (each
  version gets its own isolated environment), and a version outside your list is refused (403). If a slow install of
  the previous pick finishes after the user switched, its result is discarded (7.2 fix - before, a late failure of the
  old version could overwrite the new version's READY status).
* With nothing ticked there's **no restriction**: users can pick **any stable version from the package registry** in
  the version picker, or leave "Recommended" (the vetted version range). A version that doesn't exist in the registry
  is refused.
* **One version everywhere (7.0 fix).** The version you set as default, or the one the user picked, is the version that
  is (1) written into the generated `requirements.txt` / `package.json` / `pom.xml` / `Gemfile` / `go.mod` / `.csproj`,
  (2) installed in the run's isolated sandbox environment, and (3) pinned in the exported ZIP. Before 7.0 the AI's own
  manifest could replace the pinned one, so the ZIP shipped another (or no) version and packages the generated code
  imported were never installed. Now:
  * the AI no longer writes manifests; Understudy writes them from the approved spec. Extra packages the generated code
    really imports (e.g. `python-dotenv`, `@playwright/test`) are added **only if they are on the trusted list** -
    otherwise the AI code is rejected and the vetted template is used;
  * **before every run** the sandbox is checked: the exact pinned versions must be installed (another Cloud Run
    instance, a restart or a cleaned cache may have lost them) - anything missing or different is installed first, and
    the run log says so;
  * if you remove the user's version from the allowed list after they picked it, the next run stops with a clear
    *policy* message (no self-healing retry) and asks them to pick an allowed version;
  * the ZIP pins **every** package to the version that was actually installed and verified, and adds
    `config/dependency_lock.json` plus a "Dependency versions" table in its README.
* If the registry can't be reached from the server, the list is marked as partial.

### 2.4 Self-healing retries (the user approves every fix - 6.3)
Choose how fixes are applied in **Self-healing retries** (Access & tools):

* **Ask the user before every retry (default, recommended).** When a run fails, Understudy diagnoses it and prepares
  a fix: new steps, instructions for the code generator, and the **regenerated script**. Nothing runs yet. The user
  sees **what went wrong**, **what the AI changed** (step by step) and the **script diff**. They then:
  * **Approve & retry** - exactly the reviewed code runs, after the SOP rules are re-checked;
  * **Ask the AI to revise** - tell it what it got wrong ("step 2 should use #vendor-legal-name", "don't click Save
    Draft, click Submit", "wait 3 seconds after submit") or answer the question the AI asked when it wasn't sure.
    The AI re-diagnoses with **all** corrections so far and shows a new fix, up to 5 revisions per retry;
  * **Stop here.**
* **Retry automatically.** The pre-6.3 behaviour: fixes are applied and re-run without asking.

In both modes the retry limit here (1–50, default **5**) caps the runs, applies to running sessions too, and users can
stop at any time. If the chosen tool's runtime still can't start after every repair (for example no browser is
installed), the retry card asks the user to **tell the AI what's different on this machine** (8.2 - e.g. *"JDK 17 is in
C:\Program Files\Java\jdk-17"*, *"Chrome is at /opt/google/chrome/chrome"*) - the AI plans the next repair with it and
shows it for approval - or to fix the machine and retry. The built-in Playwright driver is offered as a fallback only
when you have activated Playwright (Python) (8.2: nothing is pre-built). Unattended runs (CI, the benchmark) set
`UNDERSTUDY_SELF_HEAL_APPROVAL=automatic`.

**No dead ends (8.2).** Two situations used to stop with *"Self-healing couldn't fix it - after 0 automatic attempts"*
and no way to tell the AI anything:
* **The run saved a record, but not every value matched** (field report: a Selenium + TestNG run typed the invoice date
  into a date field and Chrome stored `60923-02-20`). The duplicate guard still never re-runs by itself, but the retry
  card now says so, shows a **"Tell the AI what went wrong"** box and **Let the AI fix it**: the fix is prepared and shown
  for review with a warning that the fixed run saves the record once more. (Every *Run again* in 8.1 silently created
  another duplicate.)
* **The admin's retry limit is reached** - the same card, so the user can correct the AI; a correction starts a fresh
  budget.
Self-healing also recognises two mistakes of generated code from the run itself and tells the code generator: date /
time fields must be set with JavaScript (typing is locale-dependent), and a success text that is not in the step plan
(the run waited for *"Successful Committed"*, which the page never shows) must not be awaited. The BUILD prompt has the
same two rules, so new suites avoid both.

### 2.4.1 When the tool itself can't start (runtime repair, 8.2)
A run that dies before its first step is an ENVIRONMENT failure: Understudy keeps the user's tool and repairs its
runtime - one repair per approved retry, shown with exactly what it changes (environment variables, PATH folders, a
download, a pom.xml release):

| Cause (from the log) | Repairs, in order |
|---|---|
| **Java / compiler mismatch** - `invalid target release: 17`, `release version 17 not supported`, `UnsupportedClassVersionError`, `JAVA_HOME is not defined correctly` | 1. a JDK path the user typed in their correction · 2. every JDK on the machine that is new enough (`JAVA_HOME` + `PATH` for the run) · 3. compile for the JDK Maven uses (`maven.compiler.release` in pom.xml - the ZIP gets the same pom) · 4. download Eclipse Temurin 17/21 (SHA-256 verified) into `generated/runtimes` |
| Browser / driver not found, driver mismatch | the user's browser path · installed Chrome, Edge, Chromium, Playwright's Chromium · hide stale chromedriver copies · reinstall |
| Playwright / Cypress / Puppeteer browser missing | the tool's own browser download |
| `node` / `npx` missing | download Node.js 22 LTS (verified) |
| `gradle` / `mvn` missing | download Gradle 8 / Apache Maven 3 (verified) |
| Missing or broken package / unknown | reinstall in a fresh environment, then **the AI** |

**Why the 8.1.1 Selenium Java run failed** (*"Fatal error compiling: error: invalid target release: 17"*, screenshot from
the field): Maven compiles with the JDK in `JAVA_HOME`, not the `java` first on `PATH`. On that machine `JAVA_HOME`
pointed at an older JDK while the generated pom.xml asked for Java 17, so compilation failed before the first step - and
8.1.1's repair ladder only knew "reinstall the packages", which can't fix a JDK, so every AI retry ended at *"Runtime
repairs exhausted"*. 8.2 fixes it at the root and in self-healing: (1) pom.xml targets the JDK Maven really uses (17 when
it is 17+, otherwise that JDK's release - found with `java -XshowSettings:properties`, which also sees through Windows'
`javapath` shims); (2) the admin's smoke test runs with that same JDK and pom, and both are pinned in the tool's registry
entry, so users run exactly what the smoke test proved; (3) the JDK ladder above.

When the rules don't recognise a failure, have tried everything, or the user typed a correction, the **RUNTIME_REPAIR**
agent picks one action from the same closed set (an allow-listed variable pointing at a path Understudy found or the
user typed, a PATH folder, a runtime download, a compiler release, reinstall, browser download - or "none" with the
exact steps for a human). Its answer is validated before the user sees it; it never repeats a repair.

### 2.4.2 Environments: runtime versions per tool (8.4)
The **Environments** tab shows **every runtime version on the server** before you decide what a tool needs: each Java
(JDK), Python, Node.js, Ruby, Go, .NET SDK, Maven and Gradle install Understudy finds (PATH, `/usr/lib/jvm`, pyenv, nvm,
sdkman, Program Files ...), its exact version, path and source (installed on the server, downloaded by Understudy,
registered), which one is the default and which tools use it, plus the browsers.

* **Several versions side by side.** *Add* downloads another one (official builds, checksum verified) without changing
  what any tool uses, and a **dropdown per tool** switches it to any installed version - no paths to type; the tool's
  smoke test re-runs right away. Python downloads are standalone CPython builds, so a tool can run on Python 3.12 while
  Understudy stays on its own Python (the tool's isolated environment is rebuilt with the chosen interpreter).
* **Any version the official sources offer (9.9).** The *Add* buttons are no longer a fixed list: Understudy reads
  the release lines live - Java from Adoptium (Temurin 8, 11, 17, 21, 25 ...), Node.js from its release schedule,
  Python by CPython's yearly cadence (standalone builds), .NET from Microsoft's release index, Gradle and Maven from
  their own sites (cached 6 h; the built-in list when a source can't be reached). Supported lines are buttons (LTS
  marked); **end-of-life** lines (Node.js 18, Python 3.9, an old non-LTS JDK ...) are in an *Older versions* list and
  ask you to confirm, because they get no security fixes. The download is the same: official archive, checksum
  checked before anything is unpacked. In a setup chat you can name any line ("download JDK 11"). On Cloud Run a
  download is refused up front when the instance hasn't enough free memory for it (its disk is held in memory), with
  the `--memory` command to fix that.
* **Download sources / mirrors (9.9).** The official sites are fixed in the code (Adoptium, nodejs.org, the
  standalone Python builds, Maven Central, Gradle, Microsoft, go.dev). If the server may only use an internal mirror
  (Artifactory, Nexus ...), **Environments > Download sources** lets you set one per source: https only, no user name
  or password in the address (use anonymous read access or allow the server's network address), **Test** fetches a
  small known file through it, and every change is in the audit log. The same files and published checksums are
  expected - a mirror that serves a different file is refused before anything is unpacked. Version lists come from
  the mirror too. (Ruby on Linux and conda packages still come from conda-forge / GitHub.)
* **Version requirements per tool** ("Java 17+", "Python 3.9-3.12", "Node.js 18+") come from Understudy's rules, from the
  AI stack assistant (recorded when a tool becomes ACTIVE, with the versions its smoke test passed with) or from you.
  They are **checked before every run**: a run that doesn't meet them is held with a one-click fix (switch to an
  installed version that fits, or download one) that the user sends to you, or the user can *Run anyway*.
* **AI-guided runtime installs (8.3).** When a package manager or tool needs a program the server doesn't have (ruby and
  gem, go, dotnet, java, node, mvn, gradle), **Let the AI set it up** picks the official, checksum-verified build for
  this OS, downloads it into `generated/runtimes`, registers it for every install, smoke test and run, starts every
  program once and explains any failure.

### 2.4.3 Escalations: runtime-help requests (8.3, 9.3)
Some failures can't be fixed inside the user's code: a package the tool needs, a version you haven't allowed, a missing
runtime, a proxy or network setting. Self-healing then sends a **runtime-help request** with the AI's diagnosis and the
change it proposes. It appears live in the **Escalations** tab (and the inbox bell):

* **Confirm** applies the AI's change in one click (e.g. *"Add robotframework-jsonlibrary & notify the user"*,
  *"Allow Playwright 1.63.0"*), or your own with **Fix it yourself**: add a package to the tool (pip, npm incl. scoped
  packages, Maven / Gradle coordinates, gems, Go modules, NuGet), allow a version, a run variable, a PATH folder, a host
  that skips the run proxy, the network mode - and optionally **re-run the user's session**.
* **For everyone or for one user (9.3).** Packages, allowed versions, run variables and PATH folders can apply to
  **Everyone using <tool>** or **Only <user>**; server settings (proxy bypass, run network, switching or downloading a
  runtime) always apply to everyone. **Fixes for single users** lists every per-user fix, with **Remove**.
* **Reject** (with a reason) or **Ask the user** a question. Every decision reaches the user as a Studio notification
  (bell) and by email.

### 2.5 Gemini key and model
The key and model you set in **AI, security & account > Gemini API key & model** are used by every AI call in Understudy:
* video analysis (including the File API upload)
* tool resolution
* payload preparation
* code generation
* SOP rule extraction
* the compliance review
* self-healing
* the Model Armor classifier

A key you change takes effect on the next call, with no restart needed. The key is saved in the backend's `.env` so it
survives restarts. It's shown only masked, and it's never recorded in telemetry, never put in the ZIP and never passed
to the generated automation's process. If an AI call fails (for example Google rejects the key with
`API_KEY_INVALID`), users see that reason in Understudy and the full request and error appear in Telemetry.

The admin policy is stored in `generated/admin_policy.json` (set `UNDERSTUDY_ADMIN_POLICY_PATH` to change this) and
survives restarts.

### 2.5.1 Google Cloud Model Armor (7.2)
Understudy's **built-in** Model Armor always screens every input (SOP text, payload values, URLs, tool names, video
transcripts, previous-session context): text clean-up, attack patterns and, when Gemini is set up, the
`MODEL_ARMOR_CLASSIFIER` prompt. **AI, security & account > Google Cloud Model Armor** adds Google's managed service
in front of it - every one of those texts is also sent to `sanitizeUserPrompt` of a Model Armor *template* in your
Google Cloud project. Only the text being checked is sent; the server signs in with its Google Cloud identity (the
Cloud Run service account, or your `gcloud auth application-default login` locally), so no key is stored.

1. **Project / location / template id** - prefilled with your Vertex AI project and region and `understudy-inputs`.
2. **Create template in Google Cloud** (optional) - creates the template with the filters you tick: prompt injection &
   jailbreak, malicious URLs, responsible AI (on by default) and basic sensitive data (off by default: it would also
   block payloads that legitimately contain account numbers), at the confidence you choose. The server's identity
   needs `roles/modelarmor.admin` for this; or create the template in the console (Security > Model Armor) or with
   `scripts/model_armor_setup.sh`.
3. **Test connection** - sends a harmless business sentence and a textbook prompt injection. It passes when the first is
   allowed and the second blocked; otherwise it shows the fix (enable `modelarmor.googleapis.com`, grant
   `roles/modelarmor.user`, create the template, loosen a too-strict template, ...).
4. Tick **Screen every input with Google Cloud Model Armor**, choose what happens when Google can't be reached and
   **Save** (saving runs the test first):
   * **Fail open** (recommended) - the built-in stages still protect the input; a warning is recorded.
   * **Fail closed** - every user input is blocked until Model Armor answers again. Saving fail-closed while the test
     can't reach Google asks for confirmation.

The setting is stored in the admin policy (Firestore on Cloud Run, so every instance uses it at once) and overrides
`UNDERSTUDY_MODEL_ARMOR_TEMPLATE` / `UNDERSTUDY_MODEL_ARMOR_FAIL_CLOSED`; **Use the environment setting** hands control
back to them. The card shows how many texts were screened, blocked and how often Google was unreachable since start,
every change is in Telemetry, and **Check Google Cloud set-up** includes a live Model Armor check when it is on. A match
blocks the input and names the filter (stage `STAGE_1B_CLOUD_MODEL_ARMOR`).

Set-up from the command line instead: `./scripts/gcp_setup.sh <project-id> --model-armor` (local install),
`./deploy_cloudrun.sh <project-id> [region] --model-armor` (Cloud Run; `UNDERSTUDY_MODEL_ARMOR_ADMIN=1` also grants
the service account `roles/modelarmor.admin` for the panel's create button) or `scripts/model_armor_setup.sh`.

### 2.6 Learned skills
Every error Understudy hits is written down as a lesson, as a `SKILL.md` file in the `skills/` folder of the machine
running Understudy (`UNDERSTUDY_SKILLS_DIR`). When self-healing fixes the error, the fix is saved in the lesson:

* the runtime settings that worked, such as `CHROME_BINARY` for Selenium
* selectors that changed on the target page
* a wait the application needed
* the self-healer's notes

Before the next run with the same **tool** (runtime lessons) or on the same **target** (page lessons), Understudy
applies those fixes automatically, so the same error doesn't happen again. The user sees what was applied, and the ZIP
contains the relevant skills under `skills/`. In this tab you can read each skill and delete it. Skills never contain
payload values or secrets.

### 2.6.1 Learned AI answers (used offline)
The same tab lists every **validated AI answer** Understudy keeps for offline use (section 5.1): which step asked, a
short label, the model it was learned with, the date and how often it was re-used offline. Open one to see the
question fingerprint and the stored answer; delete one, or **Forget all**. `UNDERSTUDY_RECALL=off` switches recall off.
API: `GET /api/admin/recall`, `GET|DELETE /api/admin/recall/{id}`, `DELETE /api/admin/recall`.

### 2.7 Code health (AI self-heal for start-up warnings)
Understudy starts its backend with deprecation and future warnings switched on and lists every warning attributed to
its own code (a clean 9.5 install has none - the list fills when a dependency upgrade deprecates something Understudy
uses). Once a Gemini key is set (at start-up, or when you save
one), the AI proposes the smallest fix for each warning in Understudy's own code. Each fix is **tested on a copy of
the code first**. It must:

* compile
* add no unsafe calls (processes, network, eval, deletion)
* make the warning disappear
* keep the backend starting

Verified fixes are applied by clicking **Apply verified fix**. Turn on **Apply verified fixes automatically** to have
them applied as soon as they pass. **Restarting the backend** loads a fix.

**Did the AI change my code, what exactly, why - and how do I undo it? (7.1)**
* **At a glance.** While any AI change is in place, Mission Control shows an amber line on every tab ("The AI has
  changed Understudy's own code: 1 change is in place (core/x.py) - See what changed / revert"), the **Code health**
  tab carries an "N AI changes" badge, and the top of the tab says either *the AI has not changed any code* or how
  many changes there are, how many were made by **auto-apply without an admin click**, which are still in place and
  whether the backend must be restarted to load them.
* **Before applying.** A proposal shows the exact lines it would change (original line numbers → new line numbers,
  old text in red, new text in green) and a unified diff, with "Nothing has been changed yet".
* **After applying - AI code changes.** Every applied fix is listed, newest first, with:
  * its live status: **AI change in place** (the file is exactly as the AI left it), **reverted**, **edited since**
    (someone changed the file afterwards) or **file missing**;
  * who applied it: `admin 'alice'` or **AI (auto-apply - no admin click)**, and when;
  * the warning that triggered it, the AI's **diagnosis (why)**, what the AI says it did, model, strategy, risk and the
    verification results (compiled · no unsafe calls · warning gone · backend imported);
  * **Lines changed**: each changed block with its line numbers before and after, plus the unified diff;
  * the **backup**: `generated/code_health/backups/<change id>/` holds the original file at its own path plus
    `CHANGE.md` (why / what / how to undo, readable without Understudy), `change.json` and `fix.diff`. **Original
    file** downloads it.
* **Revert** restores the original from the backup (then restart the backend). If the file was edited after the AI
  fix, Revert is refused with an explanation and **Revert anyway** appears: it first saves the current file to
  `backups/<change id>/before-revert/`, then restores the original - nothing is lost.
* **When the backend can't start** (so Mission Control is unreachable) use the terminal:
  ```bash
  python scripts/code_health.py list            # every AI change, status, who applied it, backup path
  python scripts/code_health.py show <id>       # why + lines changed + diff
  python scripts/code_health.py revert <id>     # restore (add --force if the file was edited since)
  python scripts/code_health.py revert-all      # undo every change still in place
  ```
* Every apply and revert is also in **Telemetry** (Admin events, with the diff in the detail).
* API: `GET /api/admin/code-health/history`, `GET …/history/{id}`, `GET …/history/{id}/backup`,
  `POST …/history/{id}/rollback[?force=true]` (409 with `needs_force` when the file was edited since).

The pipeline contract (`core/pipeline_contract.py`) is **protected**: warnings from it are reported but never
changed. Warnings raised inside third-party packages are listed for information only.

**Runtimes & dependencies on this machine.** The same tab also lists install and start-up warnings that don't come
from Understudy's code:

| Warning you may see | What Understudy checks |
|---|---|
| Python end-of-life notices, e.g. *"Python 3.10 will no longer be distributed …"* | the backend's Python version against end-of-life dates (live from endoflife.date, built-in table offline). It flags versions within 180 days of the date or past it; Python 3.10 ends 2026-10-31 |
| Node.js end-of-life | `node --version` against the same data |
| `npm WARN EBADENGINE` | every web-UI package in `package-lock.json` whose `engines.node` range this Node.js doesn't satisfy, skipping packages built for other operating systems |
| `npm WARN deprecated eslint@9…` | packages marked deprecated in `package-lock.json`, and whether each is a direct dependency |
| broken Python requirements | `pip check` |

These need a newer runtime or dependency, which can't be verified by editing a file. So each warning gets **upgrade
advice** instead of a patch: step-by-step commands for your OS covering the installer, recreating the venv or
`node_modules`, rebuilding and checking. With a Gemini key the advice comes from the AI (prompt `ENV_UPGRADE_ADVICE`);
without one, built-in steps are shown. Understudy never upgrades Python, Node.js or packages by itself. The backend's
start-up banner also shows the Python and Node.js versions with an end-of-life note.

---

## 3. Developer guide: your first automation, step by step

### Step 0 — Log in (or sign up), pick a project, start a session (7.0, accounts 8.1)
* **Log in** at `/wizard` with your **username or email** and password.
* **No account?** **Sign up** (if your admin left self-signup on): username, email, password and confirmation. Enter
  the 6-digit code we email you (valid 10 minutes; **Send a new code** after a minute) and you are logged in. One email
  address can have only one account.
* **Forgot password?** (next to Log in / Sign up): enter your registered email. If it belongs to an account, a code
  valid for **10 minutes** is emailed; enter it, then choose and confirm a new password. Every open session ends, and
  you log in with the new password. A blocked account can't reset its password - contact your admin.
* **Profile** (click your name at the top right): shows your **username** and **email**. **Reset password** sends a
  code to your registered email → enter it → **New password** + **Confirm password** → you are signed out everywhere
  and log in again. Your admin can never see or set your password.
* **Two-step sign-in** (Profile, 9.6): **Turn on - email me a code** mails a code first, so a mailbox that doesn't
  work can't lock you out; enter it and two-step sign-in is on. From then on, after the right password the login asks
  for a 6-digit code we email you (valid 10 minutes, 5 tries, **Send a new code** after a minute) - someone who knows
  your password still can't get in without your mailbox. **Turn off** needs your password. Lost access to your
  mailbox? Your admin can turn it off for you (Mission Control > Users > **2-step off**). You get a mail whenever it
  is turned on or off. Without SMTP set up, the codes land in the server's outbox (`python scripts/mail_outbox.py`).
* **Projects** group the runs of one process or application. **New project** needs a *Project Name* and a *Project
  Description*. A project page lists its sessions with their status (not started, step N, needs help, ZIP ready); you can delete a session, or the whole project (type its name to confirm).
* **New session** needs nothing (9.1): it opens straight in Step 1 as "New session", and once Step 1 has understood
  your recording the AI gives it a name. Click the name in the breadcrumb to rename it. A session is **one complete
  run of the 5 steps**. The breadcrumb `user · Projects › project › session` takes you back at any time, and a
  page refresh returns to where you were.
* **Workspace explorer (8.0).** The left sidebar shows your workspace as a tree: **projects** (folder icon; hover for
  the description) and their **sessions** with a status badge - `DRAFT`, `ANALYSED`, `BUILT`, `GOVERNED`, `RUNNING`,
  `PENDING` (waiting for your admin), `FAILED`, `VERIFIED` (all five steps passed, the ZIP is ready). Click a session to open it in the wizard. **+** creates a
  project, or a session in a project. Right-click a session (or use **…**) for **Open**, **Duplicate** (a new session
  with the same recording, URL, SOP, analysis and payload - opened at Step 2 when the original was analysed), **View Execution Logs** (run,
  dependency and self-healing logs, with copy) and **Delete**. The tree works with the keyboard (arrows, Enter, Home /
  End), collapses with the button in the header (remembered per browser) and becomes a drawer on small screens.
  Badges update live.

### Step 1 — Watch: show the AI what you do
1. **Recording.** Drag in a screen recording (MP4/WebM/MOV/MKV/AVI, or audio MP3/WAV/M4A, up to 250 MB) of yourself
   doing the task once. Talking through it helps the AI.
2. **Page addresses (optional with AI, 9.4).** Leave them empty: the AI reads the pages you visit from the recording,
   and after the analysis you check them in **Pages the automation visits** (edit, add, remove, re-order, or **Watch
   again** with a note). Build waits until you click **These pages are right**. Type an address only if it is not
   visible in the recording. Without AI, the start page is required. Internal or private addresses only work once your
   admin has added them in Mission Control.
3. **SOP / rules (optional).** Write the business rules in plain English, for example:
   ```
   Approved vendors: Acme Industrial Supplies, Globex Corp.
   The spend limit per invoice is $5,000.
   Invoices must be dated between 2026-01-01 and 2026-12-31.
   Only Finance Clerk or AP Manager roles may submit invoices.
   ```
   You can also give values directly (`Vendor Name: Globex Corp`).
4. **Context of previous sessions (optional, 7.0).** Search your earlier sessions **by name** and click **Add** - as
   many as you like - or click **Add project** to add every analysed session of a project. The WATCH agent then gets
   a compact digest of each: the steps with the selectors that worked (or were repaired by self-healing), parameter
   names and types, how success was verified, the SOP rules, self-healing lessons and your earlier corrections. It
   re-uses what fits the current page (same keys for the same fields, proven selectors, lessons applied up front) and
   lists what it re-used under the step plan. **No values from earlier runs are ever passed on**, every digest passes
   Model Armor (a poisoned one is left out and reported), and the current recording and page always win. Offline, the
   built-in planner re-uses proven selectors and parameter names from sessions on the same site.
5. Click **Analyse with AI**. Every input first passes Model Armor. You then see what the AI understood (a summary and
   numbered steps). Each step shows **on page** when its element was found on the live target page, or **later page**
   when it belongs to a page that only appears after an earlier step; *Checked against the live page* lists any selector
   Understudy corrected and the field rules it read from the page (section 5.2).
6. **Did the AI get it right?** If something is wrong or missing, explain it in your own words and click
   **Explain & re-analyse**. For example: "You missed that I pick the cost centre before saving", or
   "Vendor Name: Globex Corp".
   * Your explanation passes Model Armor.
   * The AI re-watches the recording with **all** your explanations so far, like self-healing keeps its notes, and
     updates the steps, values and verification.
   * Repeat until it's right, then click **Yes, that's right**.
   * Without an AI key, `Field: value` lines and "verify on <url>" are still applied.
7. **How complex is this automation?** Above the tool cards Understudy shows a 1-10 score and a level (Simple,
   Moderate, Complex, Very complex), what makes it harder and how each challenge is handled, and how well each tool
   fits. It comes from an **automation-aware inspection** of every page the steps visit: an agent picks only the checks
   this automation needs (elements the steps use, sign-in, file upload, dialogs, iframes, shadow DOM, CAPTCHA,
   late rendering, new tabs, repeated rows), each with its reason, and decides per page how many fresh loads are needed
   to see whether ids, names or field order change (*AI decided* / *Rules decided*). **What Understudy checked** lists
   each check and what it found. Nothing is ever clicked or submitted. Pages whose ids change on every load (Portal 3,
   rpachallenge.com) get label-anchored locators; ids and positions that move are never used.
8. **Automation difficulty (9.8; 9.9: Gemini chooses the parameters).** Under the complexity box Understudy shows an
   **Automation Difficulty Score** out of 10. Nothing in the list is fixed: Gemini **watches the recording** the way an
   enterprise automation CoE assesses a use case and decides the **ten parameters that matter most for this process** -
   from Understudy's library of candidate parameters (each with what the inspection, the steps and the SOP measured) or
   its own process-specific ones (e.g. "Approval chain across two systems"). The parameters are grouped in five
   weighted categories - System & data readiness, Process characteristics, AI & automation risk, Scalability &
   cost-to-serve, Organizational readiness. Your example slides and toolkit are the reference for how the parameters
   are chosen, not an output. Every row shows why it matters here, the moment in the video (**▶ 0:42**), the
   evidence, the measured and the AI value (0-10), the weight and its contribution; process-specific parameters are
   badged. The score is a weighted cubic mean, so one hard parameter isn't hidden by easy ones; a blocker (CAPTCHA,
   2FA) of 7+ lifts it to at least 0.8 x its value. The assessment decides **one thing: traditional automation or
   agentic automation**; when you pick a tool of the other kind, Understudy tells you. Without Gemini, the ten most relevant candidates are chosen by rule (*Rules decided*).
   * **Tune it.** If the AI misjudged the page, move a value slider, change a weight or untick **Applies** (a parameter
     that doesn't apply drops out and its weight is spread over the rest) - the new score is previewed - and click
     **Recalculate**. The server recomputes the score and redoes the recommendation; **Back to assessed
     values** undoes your changes. Every recalculation is in your session's activity log, and the build agent gets the
     tuned values.
   * **Traditional or agentic?** The recommendation says which kind of tool fits this workflow, how confident it is,
     why (naming the parameters that decide it) and what speaks for the other kind. Below it, **Traditional tools**
     (generated code: deterministic, free per run) and **Agentic tools** (an AI agent at run time: adapts to changed
     layouts, uses your organisation's Gemini and costs a call per step) list their tools with a fit rating. Every
     agentic tool runs on **Understudy's own agent**. **Understudy Gemini Agent** (Playwright + Chromium) is **built
     into Understudy**, so an agentic tool is available even before your admin sets one up - nothing to approve, and it
     can't be removed or blocked. Once your admin sets up agentic tools of their own on the same agent (e.g. Playwright
     + Chromium, Selenium + Edge), **those** are listed and the built-in one is not (a session already using it keeps
     it). **Use** picks a tool; a traditional tool your admin hasn't approved shows **Request**.
9. Choose a tool from the framework cards:
   * Cards marked **approved by admin** can be picked straight away.
   * Cards with a **lock** need approval. Click one to send a request to your admin, with an optional reason.
   * Type any other tool name and click **Use this tool**. If it isn't approved yet, you're offered the same request.
   * The card updates by itself when the admin decides.
10. When you pick an approved tool, its dependencies install in the background. The set-up card and install log show
   **the exact version in use** for each package, and where it comes from: the tool's isolated environment or the
   backend's shared one. For example, `Version in use: playwright 1.46.0 (requested ==1.46.0; isolated environment)`.
   The same list goes into the ZIP's `config/environment.json`. Choose the version in the version
   picker under the cards:
   * any version from the package registry, if your admin set no restriction
   * one of the versions your admin allows, otherwise

   If Understudy learned fixes from earlier errors with this tool or target, a blue note lists what it applied.
11. If the AI couldn't be used, the amber banner says **why** (for example, the key was rejected or the quota is used
   up). The plan then comes from the target page's form.
12. **A tool that isn't offered (8.0).** If the SOP names no tool and you want one that isn't among the cards, type it
    in **Another tool?** - with a version if you need one, e.g. `Cypress 13.6` - and click **Use this tool**. If it isn't
    active yet, send the request: the session pauses exactly as below, and after the admin decides, Step 1 shows the
    decision (and the admin's note).
13. **Unsupported Framework (8.0).** When your SOP requires a tool (or version) your admin hasn't activated, an amber
    *Unsupported Framework* warning quotes the SOP line. **Submit enablement request** (with your reason) pauses the
    session - **PENDING ADMIN APPROVAL** - and a blue banner follows the admin's progress (*Requested → AI setup proposed
    → Installing in the sandbox → Smoke test → Active*). When the tool is active the session continues by itself with it
    selected; if the admin rejects it, the note is shown and you can choose another tool. *Stop waiting and pick an
    approved tool* is always possible, but because the SOP named a tool you confirm the switch and it is recorded.

### Step 2 — Govern: check the data and the SOP rules
* The **payload table** lists every value the automation will type, and you can edit it:
  * change values, types or labels
  * add or delete rows (rows used by a recorded step can't be deleted)

  **SOP** badges mark values that your rules constrain. Secrets are hidden, never exported and passed only as
  environment variables.
* **Page rules (7.0):** under a value you see what the target page itself accepts - *required*, the options of a
  dropdown (the value becomes a picker), number ranges, length limits, a format pattern. A value the page would reject
  is refused when you save the table, instead of failing in the browser. These rules also go into the ZIP's
  `payload.schema.json` (JSON Schema).
* The AI turns your SOP into a checklist of rules, each quoting the sentence it came from. Fix any rule that's wrong,
  then tick **Yes — the AI understood my SOP correctly** and **Save & confirm rules**.
* Click **Run compliance check**. For each problem, either **Fix payload** inline (type the new value; the check runs
  again at once) or choose **Update SOP instead** (the SOP editor opens; the rules are re-read and must be confirmed
  again before the next check). You can't continue - and Execute is refused - until there are zero violations.
* **The SOP can be in any language (7.2).** With Gemini, `GOVERN_EXTRACT_RULES` reads any language and explains each
  rule in English while quoting the original sentence. Without AI, the built-in parser understands the common rule
  phrases of English, Spanish, French, German, Portuguese, Italian and Hindi (digits in any script, `5.000,00` amounts,
  `31.12.2026` dates) and tells you which language it detected. If it can't read a rule from your SOP (another
  language, no AI), the compliance check shows a blocking **SOP-NOT-UNDERSTOOD** problem until you add the rules by
  hand, update the SOP, or set up Gemini - it never passes silently.
* **Why Govern comes before Build (9.9):** the rules you confirm here are written into the code in Step 3, so the code
  is generated after they are settled. A new **value** only needs the check again (the code holds no values); a changed
  **rule**, SOP or payload key means the code is generated again.

### Step 3 — Build: generate the code - with your SOP rules in it
* Click **Generate automation code**. The **build agent** (9.0) writes the suite for the tool you picked, at the level the
  complexity calls for (label-only locators, finding elements again after a submit, frame and new-tab handling ...),
  then Understudy checks it: every locator the code uses must find exactly one visible element of the right kind with
  the expected label on as many fresh loads of the live page as the inspection decided. A locator that breaks, an
  unsafe pattern or a syntax error goes back to Gemini with its previous code (up to 3 rounds) instead of falling back
  to a template. The code reads every value from `payload.json`, and a safety scan confirms it has no hardcoded values,
  secrets or dangerous calls.
* **Govern is part of the script (9.9).** Every generated suite - traditional or agentic - contains `governance.json`
  (the rules you confirmed) and a fixed checker in the script's language: `understudy_governance.py` (Python tools,
  Robot Framework, the agentic tools), `understudy_governance.js` (Playwright JS, Puppeteer, Cypress, WebdriverIO) or
  `UnderstudyGovernance.java` (Java tools; no extra dependency). The script calls it right after loading the payload
  and before step 1: it prints `GOVERN OK: <n> SOP rule(s) checked` or `GOVERN FAILED: <why>` and stops before it
  touches the application - in Understudy and wherever you run the ZIP. The agentic runtime checks the rules before
  Gemini or the browser starts. Code the AI wrote without the check is sent back (*GOVERNANCE_MISSING*); for a language
  without a checker the AI implements the same rules. The **SOP rules in the script** card lists them.
* **The suite view** shows every file of the suite with **`payload.json`** next to the code and `governance.json`: your
  Step 2 data exactly as the script reads it (secrets are not in it - they are `UNDERSTUDY_SECRET_<KEY>` environment
  variables). Change it there in Step 2, or edit the file in the ZIP to run with other data.

### Step 4 — Execute: run it for real
Click **Run automation** and watch every `STEP n OK / FAILED` line in the live log. The run always uses **the tool you
chose**. If the tool can't start on the server (for example Node isn't installed for Cypress), the run fails with the
exact reason and what to install. It is never silently replaced by a different tool.

**Agentic tools (9.8; 9.9: an agentic script).** With **Understudy Gemini Agent**, Step 3 shows Gemini writing an
**agentic script**, `run_agent.py`, that you can read like any generated script - but it holds no selectors. Each line
is an instruction to the agent (`agent.step(3, "Enter the invoice date", action="fill", value_key="invoice_date",
hints=[...], expect=...)`), plus branches on what the page shows (`if agent.sees("Is a duplicate warning shown?"):
...`). There is no final screenshot check: the run passes when every step passed. Every time it runs, Gemini looks at the live page and decides how to do each step,
so it adapts when the layout changes. Understudy checks the script before you see it: only the agent's API, no other
imports, no addresses or values written in, every recorded step present (Gemini gets two tries; otherwise Understudy
writes the script from the steps). The runtime library ships next to it as `understudy_agent.py`. **No key to enter (9.9):** inside Understudy the agent
uses the Gemini your admin set up - Vertex AI with the service account on Google Cloud, or the Mission Control key -
and the model chosen there (an agentic tool your admin built may name its own). Step 4 shows which (*"The agent asks
your organisation's Gemini … Vertex AI · gemini-…"*), or that Gemini isn't set up yet (then the run is refused). The
run itself holds **no** Google key or credential: Understudy starts a small relay on `127.0.0.1` for that run, the
agent sends its questions there with a token that only works while the run lasts, and Understudy forwards them to
Gemini (size limits and a call budget per run). The run's network still reaches only your pages, and its log shows
how many Gemini calls the agent made. Outside Understudy, the exported ZIP runs with your own key
(`UNDERSTUDY_SECRET_GEMINI_API_KEY`, see its README). The agent logs every decision
(`AGENT step 3: fill 'Invoice Date' <- invoice_date (...)`), types secret values itself (the model never sees them),
and retries a failed action with a screenshot. The tool's browser stack comes from the tool (the built-in one:
Playwright + Chromium; one your admin set up: e.g. Selenium + Edge) as `UNDERSTUDY_AGENT_DRIVER` /
`UNDERSTUDY_AGENT_BROWSER`, also in the ZIP's `.env.example`.

If a run fails, a **Review the fix** card appears. It shows what went wrong, what the AI changed in the steps and the
script (as a colour diff), and any question the AI has. Approve it, or type what the AI got wrong and click **Ask the
AI to revise**. Nothing runs again until you approve.

### Step 5 — Export: download the ZIP
* When Step 4 passes, Understudy packages the run as `understudy_<tool>_<session>.zip`. The ZIP contains:
  * the code;
  * `payload.json`, `.env.example` and `config/environment.json` (it includes the build agent's trace: rounds, locators
    verified, page loads);
  * the dependency manifest and the launchers `run.sh` (macOS / Linux) and `run.bat` + `run.ps1` (Windows);
  * your SOP, rules and compliance report;
  * the run log;
  * a `README.md` **written by Gemini** (9.9) for this package: how to install and run it (traditional or agentic),
    every environment variable it needs with its meaning (`TARGET_URL`, `HEADLESS`, the `UNDERSTUDY_SECRET_*` values,
    and for agentic tools `UNDERSTUDY_SECRET_GEMINI_API_KEY` and the model), how to change the data and what the output
    means. Understudy checks it: no secret values, no API keys, no links to other sites; any variable or the run command
    it left out is added in a section marked *checked by Understudy*. Without Gemini, Understudy's own README is used.
    Step 5 previews it.

  **Re-create ZIP** builds it again.
* **A report of every run from the ZIP (9.9.1).** Each `bash run.sh` / `run.bat` run writes `results/<run id>/`:
  `understudy-report.html` (pass/fail, the SOP rules check, every step with its time, what failed, the data used and
  the full log - one self-contained page), `understudy-report.json` (the same for CI or dashboards), `run.log` (console
  output with a time per line, secret values masked) and, for pytest suites, `junit.xml`. `results/latest.html` opens
  the newest report, and reports the tool writes itself (Maven Surefire, Gradle, Robot Framework, Playwright) are
  linked from the page. Nothing extra to install: bash + POSIX awk on macOS / Linux, PowerShell on Windows
  (`core/wizard/run_report.py`).
* Since 9.0 Understudy does not re-open the target page after the run, and there is no admin verification. The run
  itself decides: every step prints `STEP n OK`, and the process exits non-zero on failure.
* **Self-healing happens in Step 4.** If the run fails, Understudy fixes itself; with your admin's default setting you
  review the fix first. The heal agent gets:
  * the error, the failing step and the log;
  * the code that failed;
  * a fresh probe of the page.

  It rewrites the code and re-checks its locators on fresh page loads before the next run.
* **If your tool's runtime can't start** (browser not found, driver mismatch, missing package), self-healing **repairs
  that tool** and runs it again. It does not switch tools. Each attempt tries one repair, and the **Runtime repairs**
  card lists them. When something only the admin can do is needed (install a package, allow a version), it goes to
  the admin as a runtime-help request.
* **Learned skills:** what fixed the run is saved as a lesson, and the next run applies it before the first attempt.

---

## 4. Running the downloaded ZIP

```
understudy_playwright-python_wiz-1234abcd.zip
├── README.md                 ← what it does, how to run it, the values and the enforced rules
├── run_automation.py         ← your automation (the file name and layout depend on the tool)
├── payload.json              ← the data: edit this to run with new values
├── payload.schema.json
├── .env.example              ← TARGET_URL (+ TARGET_URL_<n> per page, 9.4) + UNDERSTUDY_SECRET_<KEY>= lines (+ browser hints)
├── config/environment.json   ← tool, runtime, dependency plan, where the code came from
├── requirements.txt          ← or package.json / pom.xml / Gemfile / go.mod / .csproj
├── run.sh  /  run.bat        ← installs the dependencies, then runs
├── governance/               ← sop.txt, rules.json (confirmed), compliance_report.json
└── evidence/                 ← evidence_log.json (all five step results), EVIDENCE.md, execution_log.txt
```

```bash
unzip understudy_*.zip -d my-automation && cd my-automation
cp .env.example .env          # check TARGET_URL, fill in any secrets
bash run.sh                   # Windows: run.bat
```

The run prints `STEP n OK` or `STEP n FAILED: …` and exits non-zero on failure, so it can go straight into CI. To run
it with other data, edit `payload.json` or set `UNDERSTUDY_PAYLOAD=/path/to/other.json`.

You can also download the ZIP from the API: `GET /api/wizard/sessions/{id}/export` (with `X-User-Token`, or
`?access_token=`). It returns **403** until all five steps have passed.

---

## 5. AI mode vs. offline mode

| | With `GEMINI_API_KEY` (AI mode) | Without a key (offline mode) |
|---|---|---|
| Learned skills | Every validated answer is remembered | Re-used for the same or similar problems first (section 5.1), labelled "Learned skill" |
| Watch | AI watches the video and audio together with the SOP and the page's fields | A learned process for the same page, else the plan is inferred from the target page's form. **The video isn't interpreted** (the UI says so) |
| Tools | Any tool name can be resolved and requested | Catalog tools can be requested; other names need AI to resolve |
| Code | AI writes it for any tool, and it must pass the safety scan (otherwise the template is used) | Vetted templates for 8 tools |
| Governance | AI rules plus the rule parser, merged | Rule parser |
| Inspect | AI picks the checks each page needs and how often to reload it | Rules pick them from the steps |
| Self-heal | AI diagnoses the failure and rewrites the code against a fresh probe of the page | Heuristic healer and the runtime-repair ladder |

The model is chosen in Mission Control. Every AI call is tagged `ACTUAL_AI_API` in telemetry and every fallback
`LOCAL_HEURISTIC`. Mission Control's Telemetry shows the full request and response of each call.

**The agents (prompt registry `core/wizard/prompts.py`; exact text in ARCHITECTURE.md §8):**

| Prompt | Step | What the agent does | If it fails or is unavailable |
|---|---|---|---|
| `MODEL_ARMOR_CLASSIFIER` | every input | semantic check: is this text trying to steer the AI? | regex stage + local intent heuristic |
| `WATCH_URLS` | 1 | lists the page addresses the recording visits, in order, with the moment in the video | the addresses learned from the same recording in an earlier AI session; otherwise the user types the start page |
| `WATCH_ANALYZE` | 1 | watches the recording with the SOP, the pages' elements and the chosen previous-session context: steps (with the moment in the video), parameters, tool ideas, what it re-used | plan from the page's form (+ proven selectors / names from the chosen sessions) |
| `WATCH_REFINE` | 1 | re-watches with every correction the user typed | `Field: value` corrections |
| `INSPECTION_PLAN` | 1 | picks the read-only checks each page needs (sign-in, upload, dialogs, iframes, changing ids ...) | rules from the steps |
| `INSPECTION_RELOAD` | 1 | decides per page how many fresh loads are needed, and how often the build agent confirms locators | rules from the first load |
| `AUTOMATION_COMPLEXITY` | 1 | explains the challenges and how each is handled (the score shown is the difficulty score) | the evidence-based baseline |
| `AUTOMATION_DIFFICULTY` | 1 | watches the recording and decides the ten parameters that matter for this process (library candidates or its own) with category, weight, value, why and video time; classifies the use case, recommends traditional or agentic, rates every tool | the ten most relevant candidates + routing rules |
| `AUTOMATION_ROUTING` | 1 | redoes the recommendation after the user tuned the parameters (their numbers are final) | routing rules |
| `SESSION_TITLE` | 1 | names the session | the WATCH process name |
| `FRAMEWORK_RESOLVE` | 1 | turns any typed tool name into an installable, runnable spec | catalog match |
| `BUILD_PARAMETERIZE` | 2 | the editable payload table | WATCH parameters |
| `BUILD_GENERATE_SUITE` | 2 | the automation code for the chosen tool, in rounds until every locator passes on fresh page loads | vetted templates (8 tools) |
| `AGENTIC_SCRIPT` | 2 | the agentic script `run_agent.py` for Understudy Gemini Agent (agent steps and branches - no selectors, no final check); checked against the agent API and the recorded steps, two tries | written from the recorded steps |
| `GOVERN_EXTRACT_RULES` | 3 | SOP prose (any language) into checkable rules | rule parser (7 languages) |
| `GOVERN_COMPLIANCE_REVIEW` | 3 | second-opinion review (advisory) | deterministic rule engine decides |
| `VERIFY_SELF_HEAL` | 4 | diagnoses a failed run and rewrites the steps / code (the user approves by default) | heuristic healer |
| `RUNTIME_REPAIR` | 4 | repairs the tool's runtime when the rules don't recognise a failure (compiler / JDK, browser, driver ...) | the rule-based repair ladder |
| `EXPORT_README` | 5 | the ZIP's README: how to install and run this package, every environment variable, the data | Understudy's own README |
| `TOOL_STACK_SUGGEST` | admin | AI install assistant: companion frameworks, languages, package managers for a tool | none (needs AI) |
| `TOOL_SETUP_PLAN` | admin | the setup plan and manifest for a requested tool or version | catalog / registry facts, offline plan |
| `TOOL_SMOKE_TEST` | admin | writes the smoke test the admin reviews before it runs | none (needs AI) |
| `TOOL_SETUP_CLARIFY` | admin | reads a failed install / smoke test and asks the admin one question with options | built-in diagnosis |
| `TOOL_SETUP_RESOLVE` | admin | turns the admin's answer into checked changes | deterministic reading of the answer |
| `SETUP_AGENT_STEP` | admin | the setup repair agent: read-only checks in the sandbox, then a fix the admin approves | one-shot clarification + built-in diagnosis |
| `RUNTIME_INSTALL_ADVICE` | admin | picks the official, checksum-verified runtime build to download (Ruby, Go, .NET, JDK, Node.js, Maven, Gradle) | built-in mapping |
| `CODE_HEALTH_FIX` | admin | removes a start-up warning (verified on a copy) | scan only |
| `ENV_UPGRADE_ADVICE` | admin | upgrade plan for end-of-life runtimes / npm / pip warnings | built-in steps |

Before any fallback in the last column runs, Understudy first tries a **learned answer** from an earlier AI session
(section 5.1); only when none fits does the fallback run.

If an AI answer is only partly valid (for example one malformed framework suggestion), Understudy keeps the valid
parts and drops the rest, and says so in telemetry. If the answer is not valid JSON or a required part is missing or
of the wrong type, Understudy gives Gemini **one repair round**: its own answer (as data) and the exact errors, with
the expected format. Only if that second answer also fails - or the call itself fails (network, quota, key) - does it
use a learned skill or fall back to local code. Mission Control shows each repair as **AI repair round**.

### 5.1 Offline: learned skills instead of guessing
Whenever an AI answer passes validation, Understudy remembers it (`core/wizard/recall.py`): the step that asked, a
**fingerprint of the question** - normalised words, the page's form fields, the step plan's shape, the numbers in the
SOP, the recording's SHA-256 - and the answer, with secret values removed. The raw SOP, page HTML and correction text
are not stored in the fingerprint.

When AI can't answer (no key, AI switched off, quota, network, an invalid answer), every agent first asks the learned
answers. If one fits closely enough it is **re-used, adapted to the new inputs, and labelled `LEARNED_SKILL`** - a
violet "Learned skill" badge, a note with the match score, the model and date it was learned, and a `RECALL` event in
Telemetry. Only if nothing fits does the deterministic fallback run, exactly as before. A re-used answer passes every
gate an AI answer passes (Model Armor on transcribed text, parameter screening, the code scanner and syntax check, rule
normalisation, the self-heal guard).

| Step | Re-used when | Adapted how |
|---|---|---|
| Watch (`WATCH_ANALYZE`) | same page (host + path) and the form + SOP are alike (score >= 0.72); the same recording scores highest | another recording: the learned process is kept, but its moments, evidence, transcript and typed values are dropped - values come from this session's SOP and page |
| Watch correction (`WATCH_REFINE`) | same page, near-identical correction (>= 0.90) | - |
| Tool set-up (`FRAMEWORK_RESOLVE`) | same tool name (spelling/case may differ) | - |
| Parameters (`BUILD_PARAMETERIZE`) | same framework, step plan and keys (>= 0.85) | this session's values and sources |
| Code (`BUILD_GENERATE_SUITE`) | same framework and parameter keys, near-identical step plan (>= 0.95) | re-checked by the safety scan and syntax check |
| Rules (`GOVERN_EXTRACT_RULES`) | **the same SOP** (case, spacing and sentence order may differ) | rules for unknown fields dropped; the rule parser still runs and both are merged |
| Self-heal (`VERIFY_SELF_HEAL`) | same host, similar error, steps and page (>= 0.80) | the usual self-heal guard |
| Upgrade advice | same finding, OS and shell | - |
| Compliance review, code-health fix | only the identical question (these answers depend on values) | - |
| Model Armor | only the identical text, and only a remembered **block** verdict (fail closed) | - |

A changed SOP limit, date or vendor list is never answered from memory - the rule parser reads it. With
`UNDERSTUDY_STORE=firestore` the memories live in Firestore (collection `recall`), so every Cloud Run instance re-uses
what any instance learned, and an admin's delete applies everywhere. `UNDERSTUDY_RECALL=off` disables the feature.

### 5.2 Any website, not just the demo portals (7.0)
Nothing in the AI path is tied to the bundled ERP portals. For whatever URL you give it, Understudy:

* **Reads the real page.** Discovery collects every element a person can use - inputs, textareas, dropdowns (with their
  options), checkboxes, radios (each option gets its own `name+value` selector), buttons, **links**, ARIA widgets
  (`role=button/textbox/combobox/...`) and rich-text editors (`contenteditable`) - with labels, the form each belongs to
  (`action`, `method`) and the rules the page declares (`required`, `min`/`max`, `minlength`/`maxlength`, `pattern`,
  input type). Pages whose form is built by JavaScript (React/Angular/Vue/Next shells) are rendered in headless
  Chromium automatically (`UNDERSTUDY_DISCOVERY_RENDER=auto|always|never`); the richer inventory wins. Write calls the
  page's own scripts make to **its own host** (`fetch`, `axios`, jQuery) become the optional API plan; calls to other
  hosts are ignored. The inventory given to the AI is ranked (form controls first, navigation menus last and capped)
  and bounded (≈15k characters), so a site with 300 menu links still fits.
* **Tells the AI not to assume anything.** `WATCH_ANALYZE`/`WATCH_REFINE` say the plan, parameter names, types and the
  payload schema must come only from this recording, page and SOP - never from an assumed business domain - and give
  explicit grounding, multi-page and payload-schema rules (ARCHITECTURE.md §8).
* **Grounds every step on the page** (`watch.ground_analysis`): a selector that is on the page is kept (the element's
  other selectors become fallbacks); one that isn't is replaced by the page's real selector when the same element is
  found by id/name/test-id or by its label (the AI's guess stays as a fallback, and the correction is reported); a step
  whose element is not on the start page is marked **later page** (multi-page flows) and relies on its label at run
  time and on self-healing. `fill` on a `<select>` becomes `select`, and so on.
* **Derives the payload schema from the page** (`core/wizard/payload_schema.py`): each grounded parameter gets the
  page's constraints and the matching type (dropdown → choice from its options, checkbox → boolean, number/date/email,
  password → secret); a value differing only in case is snapped to the page's option. Step 2 refuses values the page
  would reject; the ZIP's `payload.schema.json` is a real JSON Schema with `enum`, `minimum`/`maximum`,
  `maxLength`, `pattern` and `required`.

`tests/test_dependency_lock_generalization.py` runs the whole pipeline offline against a small site Understudy has never
seen (a volunteer sign-up form with a dropdown, a number range and a list page) - discovery, grounding, page rules
(a value of 12 for "max 8" is refused), govern, build, a real browser run and the ZIP's schema.

---

## 6. Configuration

```env
GEMINI_API_KEY=                               # optional
GEMINI_MODEL=gemini-flash-latest
UNDERSTUDY_ADMIN_USERNAME=admin               # Mission Control login
UNDERSTUDY_ADMIN_PASSWORD=                    # empty: generated on the first start, saved here, shown once
                                              #   (show: python scripts/admin_password.py show); a UI change lasts until restart
UNDERSTUDY_ADMIN_PASSWORD_GENERATE=on         # off = don't generate (falls back to the published default)
UNDERSTUDY_ADMIN_TOKEN=                       # optional static X-Admin-Token for scripts/CI
UNDERSTUDY_AGENT_RELAY_MAX_CALLS=400         # 9.9: Gemini calls one agentic run may make through Understudy's relay
UNDERSTUDY_RUNTIME_RESTORE=on                 # 9.9: re-download runtimes earlier tool setups downloaded when missing (Cloud Run)
UNDERSTUDY_AGENT_MAX_CALLS=80                 # 9.8: Gemini calls one agentic run may make (UNDERSTUDY_AGENT_MAX_TRIES=3 per step)
UNDERSTUDY_USER_LOGIN=required                # required (Studio users must log in) | off (single-user)
UNDERSTUDY_SIGNUP=on                          # default for self-signup; Mission Control > Users > Allow self-signup wins
UNDERSTUDY_MAIL_MODE=                         # smtp | outbox | off (default: smtp when UNDERSTUDY_SMTP_HOST is set, else outbox)
UNDERSTUDY_SMTP_HOST=                         # e.g. smtp.gmail.com / smtp-relay.brevo.com (section 2.0.2)
UNDERSTUDY_SMTP_PORT=587
UNDERSTUDY_SMTP_SECURITY=starttls             # starttls | ssl | none
UNDERSTUDY_SMTP_USER=
UNDERSTUDY_SMTP_PASSWORD=                     # Gmail: an App Password. Never shown, logged or exported
UNDERSTUDY_MAIL_FROM=                         # default: UNDERSTUDY_SMTP_USER
UNDERSTUDY_MAIL_FROM_NAME=Understudy
UNDERSTUDY_ADMIN_CONTACT=                     # address in "blocked" / "deleted" mails (default: UNDERSTUDY_MAIL_FROM)
UNDERSTUDY_MAIL_OUTBOX=                       # outbox file (default generated/mail/outbox.jsonl)
UNDERSTUDY_DISCOVERY_RENDER=auto              # auto (render JS-built pages in Chromium) | always | never
UNDERSTUDY_ADMIN_POLICY_PATH=                 # default generated/admin_policy.json
UNDERSTUDY_DEPENDENCY_INSTALL=auto            # auto | dry-run (plan only) | off
UNDERSTUDY_DEFAULT_TOOLS=none                 # 8.2 fresh-install registry: none (default - nothing pre-built) | playwright | catalog
UNDERSTUDY_SIGNUP_DOMAINS=                    # 8.2: e.g. companyname.com - only these email domains may self-sign-up
UNDERSTUDY_ACTIVITY_LOG=                      # default generated/wizard/activity.jsonl (Mission Control telemetry)
UNDERSTUDY_TELEMETRY_MAX_CHARS=60000          # max characters kept per telemetry field (prompts, responses)
UNDERSTUDY_INSTALL_JAVA=false                 # Docker build: true adds Java + Maven (REST Assured)
UNDERSTUDY_ALLOWED_PRIVATE_HOSTS=             # optional operator-level allow-list (normally use Mission Control)
UNDERSTUDY_ALLOW_PRIVATE_NETWORKS=false       # true = allow any private IP target
UNDERSTUDY_NETWORK_MODE=restricted            # restricted | public: where automation runs may connect (also in Mission Control)
UNDERSTUDY_EGRESS=on                          # on | off: per-run egress proxy (off only for debugging)
UNDERSTUDY_MODEL_ARMOR_TEMPLATE=              # optional: projects/<id>/locations/<region>/templates/<id> (Google Cloud Model Armor)
UNDERSTUDY_MODEL_ARMOR_FAIL_CLOSED=false      # true = block input when the managed service is unreachable
UNDERSTUDY_SELF_HEAL_APPROVAL=ask             # ask (user approves each fix) | automatic; the Mission Control switch wins
UNDERSTUDY_MAX_HEAL_REVISIONS=5               # how often the AI may revise one fix from user corrections
UNDERSTUDY_MAX_SELF_HEAL_RETRIES=5            # automatic self-healing retries per run (1-50); change it in Mission Control
UNDERSTUDY_SKILLS_DIR=                        # learned skills folder (default ./skills)
UNDERSTUDY_CODE_HEALTH=on                     # scan start-up warnings (on/off)
UNDERSTUDY_CODE_HEALTH_AUTO_APPLY=false       # apply verified AI fixes automatically (also a switch in Mission Control)
UNDERSTUDY_MAX_MEDIA_MB=250
UNDERSTUDY_RUN_TIMEOUT_S=300
UNDERSTUDY_INSTALL_TIMEOUT_S=900
UNDERSTUDY_STEP_TIMEOUT_MS=8000
UNDERSTUDY_SETUP_AGENT=on                     # setup repair agent for failed tool setups (Mission Control wins)
UNDERSTUDY_SETUP_AGENT_MODEL=gemini-pro-latest  # the stronger model the setup agent offers the admin
UNDERSTUDY_SETUP_FIX_APPROVAL=ask             # AI fixes for tool setups: ask | automatic
UNDERSTUDY_BUILD_AGENT_ROUNDS=3               # build agent rounds until the code's locators pass on fresh loads
UNDERSTUDY_PAGE_PROBE=on                      # live page probe / inspection before code is written
UNDERSTUDY_RECALL=on                          # re-use learned AI answers when the AI is unavailable
UNDERSTUDY_FORCE_HEURISTIC=0                  # 1 = switch every AI call off (rules and learned skills only)
```

**Google Cloud** (set by `deploy_cloudrun.sh`; all optional elsewhere)
```env
GOOGLE_GENAI_USE_VERTEXAI=true                # Gemini on Vertex AI with Application Default Credentials (no key)
GOOGLE_CLOUD_PROJECT=my-project               # (or UNDERSTUDY_VERTEX_PROJECT)
GOOGLE_CLOUD_LOCATION=us-central1             # (or UNDERSTUDY_VERTEX_LOCATION)
UNDERSTUDY_STORE=firestore                    # local (default: generated/store/*.json) | firestore
UNDERSTUDY_FIRESTORE_DATABASE=(default)
UNDERSTUDY_FIRESTORE_PREFIX=understudy_       # collection prefix
UNDERSTUDY_GCS_BUCKET=                        # recordings in Cloud Storage (needed on Vertex AI for videos > 18 MB)
UNDERSTUDY_SESSION_SECRET=                    # optional: pin the admin-token signing secret (e.g. from Secret Manager)
UNDERSTUDY_VERTEX_LOCATION=                   # AI calls only (e.g. global) when the data region lacks a model
UNDERSTUDY_VERTEX_MODEL=                      # force a Vertex AI model (e.g. gemini-2.5-flash)
UNDERSTUDY_VERTEX_FALLBACK_MODEL=gemini-2.5-flash  # used when the selected model isn't offered by Vertex AI
GOOGLE_APPLICATION_CREDENTIALS=               # service-account key file instead of `gcloud auth application-default login`
```
Every setting can live in `.env` (`.env.example` lists them): the backend reads the whole file at start,
`docker-compose.yml` passes it to the container, and variables set in the shell, Docker or Cloud Run take precedence.
`pip install -r requirements-gcp.txt` adds the Firestore and Cloud Storage clients (the Cloud Run image has them).

---

## 7. Security (Model Armor on every input)

* **Accounts and codes (8.1):**
  * codes are 6 random digits (`secrets`), valid **10 minutes**, single use; only an HMAC of the code is stored, at
    most 5 wrong tries, then the code is void; a verified code gives a 10-minute one-time *ticket* for the password
    step, so steps can't be skipped or replayed
  * one code per minute per address and purpose, 8 per hour per address, 30 per hour per client address
  * the API never returns a code and Mission Control never shows one or a password, so an admin can't take an account
    over; an admin can't change an existing account's email either
  * a password change ends every session of that user and is confirmed by email; blocking ends every session at once
  * block / delete reasons pass Model Armor (they are shown to users); the signup's password hash (never the
    password) waits in the code challenge until the email is verified

* **Media:**
  * type allow-list, magic-byte check (catches disguised files), size cap, safe file names, SHA-256 fingerprint
  * text the AI transcribes from the video is screened, so instructions hidden in a recording block the run
* **SOP text, payload fields, tool names and request reasons:**
  * Unicode normalisation, invisible-character removal, rejection of bidi-override tricks
  * Model Armor regex patterns, extended with fake delimiters and roles, prompt leaking, persona hijacks, governance
    bypass and command injection
  * the semantic classifier
* **Optional managed layer:** turn on **Google Cloud Model Armor** in Mission Control (section 2.5.1) or set
  `UNDERSTUDY_MODEL_ARMOR_TEMPLATE` to also screen every text with it (`sanitizeUserPrompt`, ADC auth, nothing stored). A match blocks the input and names the filter; if the
  service is down the built-in stages still protect (or block, with `UNDERSTUDY_MODEL_ARMOR_FAIL_CLOSED=true`).
* **URLs:**
  * SSRF protection (DNS resolution, private and internal ranges blocked unless on the admin allowed list, metadata
    and link-local never allowed, no credentials, every redirect re-checked)
  * the admin **blocked list** (with `*.domain` wildcards) beats every exception; a link can't be on both lists
  * AI-proposed URLs must stay on the target host
* **Tools:**
  * admin whitelist and access requests
  * argv-only commands and strict package-name grammar
* **Prompts:** untrusted content is fenced as data, outputs are schema-validated, and parameter values are never sent
  to the code generator.
* **Execution:** per-run **egress proxy / restricted network** (section 2.1.2), no shell and timeouts. The environment is the server's own **minus every secret-looking variable**
  (API keys, tokens, passwords, cloud credentials), so your API key never reaches third-party code but system settings
  like `PROGRAMFILES` and `PATHEXT` do. Generated code is scanned before it's accepted.
* **Telemetry:** keys and tokens are redacted from every event, and secret payload values are logged as `***`.
* **Mission Control:** username/password login (PBKDF2-hashed, in memory, resets to the default on restart), 12-hour
  **signed stateless tokens** (HMAC-SHA256; a password change ends every other session, logout revokes), lockout after
  5 failed attempts. On Cloud Run the hashed credentials, signing secret and revocations are shared through Firestore
  per revision, so every instance agrees; a new deployment starts again from the password in Secret Manager (generated
  by the deploy script). Failed-login counters are shared too. Admin-only: the admin API, the Gemini key/model and
  security alerts (the interception feed is kept in Firestore on Cloud Run).
* **Corrections to the AI's understanding:** screened by Model Armor (including the semantic classifier) and fenced
  as data in the WATCH_REFINE prompt.
* **Retries and duplicates:** since 9.0 Understudy does not re-check the target page after a run, so a run that saved
  its record and then failed can save it again on the retry. With the default *ask* self-healing you approve every
  retry; point automations at a test environment (section 11).
* **Code health fixes:** tested on a copy of the code; edits that add process, network, eval or deletion calls are
  rejected; the pipeline contract is never changed; backups and rollback for every fix.

Blocks appear in Mission Control's Model Armor panel as `Understudy - <channel>`.

---

## 8. Testing

```bash
pip install -r requirements-dev.txt          # = requirements-gcp.txt + pytest + httpx
python -m playwright install chromium
python -m pytest                             # every suite in tests/ (about 3 minutes)
python -m pytest tests/test_wizard_e2e_integration.py -q    # one suite
```
Suites that need a live backend start their own on a free port (`tests/wizard_helpers.LiveServer`); suites that need
Playwright Chromium or Node.js skip themselves when it is missing. The AI paths run against a deterministic fake
model, and every suite uses a throw-away admin policy, store, skills folder and telemetry log
(`tests/wizard_helpers.py`, `tests/conftest.py`). Live checks against real services are opt-in:
`UNDERSTUDY_LIVE_TOOL_TESTS=1` (tool matrix) and the real-registry installs that skip when PyPI / npm are unreachable.

| Suite | Proves |
|---|---|
| `test_email_uniqueness` | **8.1.1 one email = one account across instances**: 12 store objects racing an exclusive create (one wins); 8 simulated instances (no shared lock, pre-checks blinded) creating the same email - exactly one account, 7×409; two signups for one address verified at the same moment; the same username created concurrently; all on the local store and on Firestore (`create()` that fails when the document exists); delete releases the address; a half-created claim blocks for 2 minutes, then is reclaimed; backfill for older accounts (idempotent); `deploy_cloudrun.sh` defaults signup to off |
| `test_two_factor` | **9.6 two-step sign-in**: off by default; turning it on mails a code first (never in the API answer); after the right password the login answers `two_factor_required` without a token and the mailed code finishes it (single use); a wrong password sends no code; 5 wrong codes void the challenge; a sign-in code can't become a password-reset ticket; turning it off needs the password; the admin can turn it off (notice mail); a user blocked meanwhile is refused at the code step |
| `test_model_discovery` | **9.5.1**: Mission Control's model list comes from the key's live `models.list` (a local stand-in for the Gemini API), not the built-in suggestions; models that can't generate content are left out. **9.9 on Vertex AI**: the list comes from Vertex AI's publisher models without a key (Gemini text models only, recommended first), the selected model stays selectable, a Vertex list never serves a key (and the other way round), `/api/config/models` says `source: vertex`, a listing error falls back to the suggestions and is shown to the admin |
| `test_email_accounts` | **8.1 email + OTP accounts**: admin creation needs a valid, unique email (case-insensitive), sends a welcome mail and never returns or mails a password; an account without a password sets it through Forgot password; login with the email; the admin can't set a password (no such endpoint), "Ask to reset" mails a notice without a code, and no code ever reaches telemetry; legacy accounts get an email once; **signup** creates the account only after the emailed code (wrong code counts down, single use, a second pending signup for the same email replaces the first), username / email uniqueness, password confirmation, signup off; **Forgot password** checks the email, mails a 10-minute code, verify → ticket → new + confirm password, sessions end, the old password fails, a "password changed" mail, tickets single use; codes expire after 10 minutes and lock after 5 wrong tries; per-purpose cooldown, hourly cap, resend voids the old code; **Profile** shows username + email, resets with a code, another user's code is refused (403), the same password is refused, signed out everywhere; **Block** needs a reason, mails it with the admin contact, ends sessions, blocks login and resets; **Unblock** mails; **Delete** needs a reason, mails it, frees the email; reasons pass Model Armor; a failed mail is reported while the action stands; a failed code mail leaves no challenge; a **real SMTP round trip** through a local SMTP server; outbox and off modes; the SMTP password never in the status; the admin mail endpoints |
| `test_v82_ai_install` | **8.2**: nothing pre-built; the AI install assistant end to end (suggest → real registry versions → select → plan → install → AI smoke test → admin review → ACTIVE; the AI's manifests ignored); composing Selenium + Java + TestNG + Cucumber (alternatives refused, unsafe names / run commands dropped, Gradle locked); a user's request resolving to the composed stack (alias); the JDK probe, pom release, the `invalid target release: 17` ladder (user path → installed JDK → compile release → Temurin download), validated AI repairs, reviewed repairs applied exactly; the Cypress template without `Cypress.env()`; signup email domains (start and finish) |
| `test_v8_tool_registry` | **8.0 zero-trust registry and HITL synthesizer** (8.2: a fresh install has no tool; smoke tests are AI-written and reviewed): `none` / `playwright` / `catalog` modes; SOP tool detection (AI hint must be backed by the SOP); an unsupported SOP tool pauses the session (Build 409), notifies admin and user over WebSockets and the inbox; switching tools needs confirmation; the manifest (OS deps, env vars, runner wrapper; unsafe env refused); Phase B instructions (Nexus index, local interpreter); a failed install halts with captured output and a question; answers (JDK download with checksum, `JAVA_HOME` path, proxy) resume it; never forced; a passing smoke test activates the tool and resumes every waiting session; rejection releases them; smoke-test file checks; local sandbox limits (time-out, memory watchdog, restricted env, path traversal); the sandbox is always local (Docker / Auto refused, an old saved Docker mode read as local); duplicate session; execution logs; the SOP version carried into the request and resolved against the registry; the telling error line; explorer badges for older index rows |
| `test_tool_setup` | **the AI tool setup** (7.2b, 8.0): an empty registry, or the catalog pre-approved by the operator; request → plan (sources, commands, files, diff, versions) → admin instruction → approve → install → smoke test → ACTIVE → the user selects it; Model Armor screens admin instructions; an unsafe AI spec falls back to the catalog; the offline plan reads the admin's instructions; a failed install asks the admin and is never forced; unknown typed tools still reach the admin; reject, plan-first, blocked tools / versions, allowed vs. blocked never overlap; version requests; every ecosystem; a **real** install of `requests` 2.31.0 into the sandbox when PyPI is reachable |
| `test_model_armor_admin` | **7.2 Google Cloud Model Armor from Mission Control** (fake Google transport): off by default with the Vertex project suggested; admin-only endpoints; turning it on runs the connection test, then every input is screened by Google first (only the text sent) and counted; turning it off stops all calls even when the env var sets a template, and "use the environment setting" hands back control; the setting survives a policy reload; invalid project/location/template refused; fail-closed with an unreachable service needs confirmation (409) and then blocks, fail-open falls back to the built-in stages; the connection test's fixes for a disabled API, a missing role, a missing template, no credentials and too strict / too lax templates; template creation sends the exact FilterConfig, needs `roles/modelarmor.admin`, tolerates "already exists"; the Google Cloud set-up check reports it |
| `test_sop_violation_remedies` | **7.2 SOP not followed, in any language**: the same SOP in English, Spanish, French, German, Portuguese, Italian and Hindi; a payload that breaks it is flagged with the rule and both remedies; **Fix payload** (new value → check passes → Execute allowed) and **Update SOP instead** (raised limit → rules re-read → must be re-confirmed → check passes) through the real HTTP API; Execute is refused (409) while Govern fails; a Spanish vendor rule; a Japanese SOP without AI is a blocking `SOP-NOT-UNDERSTOOD` problem whose remedy (a hand-added rule) works; with AI the prompt says the SOP may be in any language; language detection and numerals |
| `test_version_choice` | **7.2 the user picks among several admin-allowed versions**: the picker lists exactly the admin's versions and default; the user's pick reaches the spec, plan and manifest; switching re-plans; a version outside the list is 403; the pre-run check installs the chosen version when the sandbox has another; a slow install of the previous pick never overwrites the new pick; **live** (PyPI reachable): `requests` 2.31.0 and 2.32.3 really installed in turn into the isolated sandbox, the lost sandbox is reinstalled before the run, the run passes, the ZIP pins the pick |
| `test_tool_matrix` | **one sample test case per configured tool** (Playwright Python & JS, Puppeteer, Cypress, Selenium, Robot Framework, Python Requests, REST Assured) against the same sample site (Portal 1): the admin pins one exact version, the user picks the tool without a version, and that version must be in the generated manifest, the sandbox install plan and the ZIP. With `UNDERSTUDY_LIVE_TOOL_TESTS=1` it also installs for real (installed version must equal the pin), runs the suite **with the tool itself** (not the built-in driver), verifies the ERP record and the ZIP; a step the machine can't do is a skip with the reason (`scripts/tool_matrix.py`, report in `docs/TOOL_MATRIX.md`) |
| `test_code_health_audit` | **7.1 AI code-change audit**: "no change" state; a proposal shows real line numbers and a diff and changes nothing; an applied change records who (admin / auto-apply), why, verification, lines and diff, and writes the backup with `CHANGE.md`, `change.json`, `fix.diff`; status in place → reverted; a file edited after the fix needs force and the edited version is saved first; the terminal CLI lists, shows and reverts without a server; the admin API (history, backup download, 409 + forced revert, login required) |
| `test_users_projects_history` | **7.0 users, projects, history, run context** with login required: every Studio endpoint needs a user token (the admin token is not one); only the admin creates accounts (generated password shown once, duplicates/reserved names/weak passwords refused, only a salted hash stored); lockout; disable, reset and logout end sessions; projects need name + description, are private (404 for others) and Model Armor screened; named sessions inside projects, unique per project, draft → Step 1 → analysis, a second Step 1 refused, recording only with the owner's token; rename re-labels sessions; delete cascades. History: search by name (usable sessions first, owner-scoped), session + whole-project context reaches `WATCH_ANALYZE` as a fenced untrusted block **without any earlier values**, drafts skipped, foreign/unknown refs refused, a poisoned digest is left out by Model Armor, offline re-use of proven selectors and parameter names (same site only). Every execution line, telemetry event and legacy telemetry action carries `[User] \| [Project] \| [Session]`; Mission Control filters by user/project. Review fixes: a Studio session is reachable only through the wizard API by its owner or an admin (the pre-wizard run endpoints are gone); a re-created username inherits nothing and old tokens stay dead; URL tokens only on downloads; malformed context refs are a 400; a failed one-call create leaves no named draft; history digests scrub payload values and query strings from free text |
| `test_dependency_lock_generalization` | **7.0 version fix**: an AI suite with its own unpinned manifest ends up with `playwright==1.44.0` (the user's pick) + the trusted extra it imports, in the suite, the sandbox plan and the ZIP (pinned to the installed versions, `dependency_lock.json`, README table); the admin default applies when the user picks none; an untrusted import falls back to the template; npm keeps `"type": "module"`, drops lock files; gem versions pinned; the sandbox is verified before a run and repaired (force reinstall) when a version is wrong or missing, untouched when right; a version the admin no longer allows stops the run (POLICY, no retry). **Any site**: discovery of links, ARIA widgets, radios with unique selectors, contenteditable, constraints, forms, same-origin API calls only; ranked/bounded prompt inventory; JS-built pages rendered; grounding repairs selectors by label/id, marks later-page steps, fixes action/type, snaps option case; payload-schema checks (incl. ReDoS-shaped patterns skipped); Step 2 refuses values the page rejects and ignores client-sent constraints; a **real browser e2e on a site that is not a sample portal** to the ZIP |
| `test_admin_policy` | default Portal 1/2 exceptions; private and unresolvable hosts blocked until an admin adds them; metadata never excepted; persistence; tool whitelist enforcement; request → approve/reject → select; typed tools go through the admin, and approval covers their packages; revocation; **login required** for the admin API, key/model and alerts; default credentials, change password (other sessions end), restart falls back to the default, nothing written to disk, lockout; static API token |
| `test_heal_approval` | **6.3 user-approved self-healing**: a failure produces a proposal (what went wrong, step changes, script diff) and runs nothing; approve runs **exactly the reviewed code** (no regeneration) after governance re-check; stale/missing proposals refused; Stop; every retry asks again and the admin limit holds; offline corrections ("step N should use …", "wait N seconds") change the steps and the script; the AI receives all corrections fenced as untrusted data and can ask a question; an injected correction is blocked by Model Armor and the proposal stays open; revision cap; a runtime repair is proposed, not applied, until approval; automatic mode keeps the old loop; admin endpoint; HTTP flow. Plus a real-browser e2e (`test_wizard_e2e_integration`): Portal 2 redesign → review → correction → approve → ZIP |
| `test_url_policy_and_egress` | the **blocked list** (host, wildcard, port/case normalisation, bad input, old policy files); **allowed + blocked can never overlap** (both directions, wildcards, moving a link, metadata, idempotent duplicates, API 400 messages, login required); *blocked wins* in a hand-edited policy; network-mode setting; the egress decision table; a real proxy forwarding the allowed host and refusing everything else (plain HTTP and CONNECT); a real child process unable to reach metadata; the pre-run policy check |
| `test_v62_semantic_armor_sandbox` | soft (synonym/spelling) recall matching and its explanation, hard gates unchanged; the optional Google Cloud Model Armor stage (not configured = no-op, template validation, match blocks and names the filter, only the text is sent, outage falls back or fails closed); a **real Chromium run** of Portal 1 under the restricted network (no denials) and a step that tries metadata / an unlisted host (refused, reported) |
| `test_gemini_key_propagation` | sets the key and model through Mission Control's API, then runs a full session (video upload, all Understudy prompts incl. the Model Armor classifier, real execution, failure, AI self-heal, ZIP) with a recording Gemini SDK stand-in. Every call used the Mission Control key and model, a rotated key applies from the next call, the key never reaches the automation process, and Mission Control telemetry holds every prompt and response but never the key |
| `test_telemetry_and_runtime_repair` | telemetry of user actions, prompts, responses, errors and fallbacks (with the heuristic's output); key redaction; plain-English AI errors; the Watch banner shows the real reason; tolerant validation; the telemetry API; the Windows Selenium diagnosis and repair ladder; the secret-free full environment; `.cmd` launcher resolution; tool versions from registries, admin allowed/default versions and the user's version choice |
| `test_clarify_versions_codehealth` | environment health: Python 3.10 / Node 18 end-of-life findings, npm semver `engines` checks, EBADENGINE and deprecated from a lockfile (other-OS packages skipped), AI and built-in advice, advice kept across rescans; installed-version report; a pinned Playwright downloads its browser with its own Python. WATCH corrections: full correction history sent to WATCH_REFINE, Model Armor blocks an injected correction, offline `Field: value` refinement, the tool choice survives while later steps reset, API. Any registry version when unrestricted; the admin list when restricted; non-existent versions refused. Code health on a throw-away copy of the code: a deprecated-API warning (a fixture module added only to the copy) is found, the AI fix is verified, applied, resolved and rolled back, unsafe/wrong/broken edits are rejected, the pipeline contract is protected, auto-apply, no AI means scanning only, the admin API needs login |
| `test_wizard_guardrails` | injection, SSRF, disguised media, payload and tool-name screening |
| `test_wizard_media_to_payload` | media analysis feeds the payload table and `payload.json` (never the code); injection hidden in a video is blocked; unsafe AI code is rejected |
| `test_wizard_governance` | SOP rules flag invalid payloads accurately; confirm-before-check; fix-payload and update-SOP loops |
| `test_wizard_dependencies_and_export` | dependency resolution for any ecosystem or tool, ZIP contents, no secrets, 403 gating |
| `test_wizard_self_healing` | failures heal **automatically without asking**, up to the admin limit (default 5, 1–50); a new run gets a new budget; Stop; healing, re-govern; the limit on a runtime failure asks instead of giving up; runtime/driver failures (such as Selenium's `NoSuchDriverException` on Windows) **repair Selenium** (Chrome → Edge → reinstall → re-run) in offline and AI mode; the built-in driver runs only when the user chooses it; a missing toolchain is never swapped for Playwright; **learned skills**: the Selenium runtime fix and a selector fix are saved as `SKILL.md`, shipped in the ZIP and applied before the next session's first run; unresolved errors recorded; signatures normalised and redacted |
| `test_wizard_e2e_integration` | real HTTP, real mock ERP, real browser: full flow to the ZIP (gated until all five steps pass); the exported script runs on its own; real selector-drift self-healing |
| `test_gemini_transport` | the `google-genai` transport: AI Studio vs Vertex AI selection (Vertex needs only a project, no key), client arguments, JSON mode/system prompt/timeout, a recording uploaded **once** and reused by WATCH, corrections and self-healing, re-upload when Google expired it, no sharing across keys, Vertex inline bytes vs Cloud Storage `gs://` (and the >18 MB rule) |
| `test_gcp_setup` | the whole `.env` is applied (shell/Docker win, empty values ignored, comments kept on update); the Google Cloud check: nothing configured is only a warning, missing project/credentials fail with the exact fix and skip the live calls, a working Vertex AI + Firestore + Cloud Storage set-up passes and cleans up its test data, Google errors are translated into commands; the CLI and the login-protected Mission Control endpoint; the Vertex AI model fallback; the start scripts and Docker files install the clients, run the check and mount credentials read-only |
| `test_offline_recall` | learned AI answers: fingerprints, adaptation to new inputs, value-dependent answers only for the identical question, Model Armor re-uses only block verdicts, shared through Firestore and a forget applies everywhere |
| `test_portal_static` | the demo ERP portals serve their self-hosted fonts and styles (no CDN) |
| `test_v83_fixes` | **8.3**: checksum-verified runtime downloads (gem, Go, RubyInstaller), the AI runtime advisor, the setup doctor (WebdriverIO runners, entry files, `rfbrowser init`), automatic known fixes, human-in-the-loop setup fixes, user-side repairs (MSB1011, version switch), runtime-help escalations and the admin's own fixes, the run proxy never routing localhost; 9.0: no verification escalation |
| `test_v84_environments` | **8.4 Environments**: runtime discovery, side-by-side installs, the per-tool version switch with its smoke test, Python downloads, version requirements checked before every run |
| `test_v841_robot_syntax` | **8.4.1** Robot Framework layout repair (keyword alone on a line, separators) and its lint |
| `test_v853_admin_add_package` | **8.5.3** the admin adds a package to a tool (pip, npm incl. scoped, Maven / Gradle, gems, Go, NuGet) from an escalation or *Fix it yourself* |
| `test_v85_setup_agent` | **8.5 setup repair agent**: read-only checks, self-checked fixes, the admin approves, history kept, stronger model only on request, lessons saved |
| `test_v90_agentic` | **9.0**: Export-only Step 5, the agentic build (locators proven on fresh loads, rewrite rounds), agentic self-healing, pages that change on every load (Portal 3 / rpachallenge), the complexity summary, per-tool access (all users, selected users, **no user** (9.9) - refused for everyone, also without accounts; the built-in tool's access can't be changed) |
| `test_v91_session_names` | **9.1** sessions named by the AI after Step 1, unique per project, renames win |
| `test_v93_escalation_scope` | **9.3** an escalation fix for everyone or only for the user who asked (packages, versions, run variables, PATH) |
| `test_v94_multi_url_inspection` | **9.4** several page addresses, automation-aware inspection plans, AI / rule reload decisions, complexity factors |
| `test_v94_pages_from_video` | **9.4** page addresses read from the recording (`WATCH_URLS`), completed, screened, edited and confirmed |
| `test_v94_runtime_checks` · `test_v94_runtime_fallback` | **9.4** every runtime a setup needs is checked for what the build uses (JDK with `javac`, `npm`, `gem`, a .NET SDK ...), a working install replaces a broken one, and a download is asked for before installing |
| `test_v98_difficulty_agentic` | **9.8/9.9** difficulty score (cubic mean, blocker floor, N/A reweighting); the decision is only traditional or agentic; ten parameters chosen offline by rule and by Gemini from the video; tuning; solution fit; the agentic-script gate and Gemini's two rounds; the offline script; the ZIP README check; an end-to-end agentic run on the organisation's Gemini through the relay (no user key; refused when Gemini isn't set up) with an `agent.sees` branch; agent guard rails; agentic templates |
| `test_v99_agentic_builder` | **9.9** agentic tools on Understudy's agent: stack composition and refused input; the agent smoke test on Playwright + Chromium and Selenium + Chrome (self-test, no key); Selenium guard rails and secret masking; the admin builder through the API (versions, duplicate name, users see it next to the built-in agent, agentic script + runtime, `.env.example`); a real install + smoke test; no final screenshot check |
| `test_v99_governed_scripts` | **9.9** Govern before Build and inside every script: the Python, JavaScript and Java checkers agree with the rule engine on the same cases (limits incl. `5.1k`, exclusive minimums, date formats, regex, denied values, required secrets from the environment); every template calls the check before step 1; a rule-breaking payload stops a generated script and the agent before step 1 (before Gemini starts); the executor needs `GOVERN OK` before step 1 and never self-heals a rule violation |
| `test_v99_ai_repair` | **9.9 one repair round**: broken JSON and a schema mismatch are fixed on the second answer (the repair prompt carries the exact error, the quoted answer, the schema and the original task; `AI_REPAIR` recorded); a good first answer makes one call; a second bad answer falls back (or uses a learned skill) - never a third call; network / quota errors and an error on the repair call are not retried; the quoted answer is truncated; the API key never reaches the prompt or the log; item lists drop bad items without a repair, and a missing list is repaired with the whole `{"rules": [...]}` shape |
| `test_v99_browser_dependency` | **9.9** a setup that needs a browser: `google-chrome` / `chrome.exe` / Edge / Firefox names map to the browser; Chrome in Program Files (not on PATH) passes; a `CHROME_BIN` the admin gave wins; no Chrome with Selenium 4.11+ passes (Selenium Manager downloads one), with older Selenium it fails saying where it looked; other binaries unchanged; the setup agent is told Chrome is installed |
| `test_v99_cloud_run_fixes` | **9.9** the agent's relay (answers only with the run's token, passes the screenshot, size / budget limits, 429 / 503 kept, useless after the run, the token passes the sandbox but is no payload secret, the runtime's `RelayBrain` bypasses the proxy); the organisation's Gemini decides availability; the real upload limit (Cloud Run without / with Cloud Storage, local, `/api/wizard/info`); downloaded runtimes restored from the shared settings, re-registered and mapped onto a newer release, a failed download reported, nothing done without downloads; "needs java" counts the tool's own run settings |
| `test_v99_runtime_catalog` | **9.9** any release line is a download key (jdk-11, node-18, python-3.9, gradle-7, dotnet-6, maven-4; nonsense refused); the catalogue from Adoptium / the Node.js schedule / CPython's cadence / .NET / Gradle / Maven (fake sources) with LTS and end-of-life marks; unreachable sources fall back to the built-in list, offline mode skips the network; an end-of-life download is refused (409, `details.eol`) until confirmed, also through `/api/admin/environments/download`; supported lines first; real (faked-network) downloads of JDK 11, Node.js 18 (checksum still enforced) and Maven 4; the Cloud Run memory guard |
| `test_v99_runtime_mirrors` | **9.9** mirrors: only https without credentials (a trailing / added), unknown sources refused; official -> mirror and back; changes audited and removable; a JDK downloaded entirely through the mirror (the official site unreachable) and verified; a tampered mirror refused; a mirror that rewrites links to itself passes the location check; version lists through the mirror; the admin API (list, save, test before saving, admins only) |
| `test_v99_direct_upload` | **9.9** recordings over Cloud Run's 32 MB limit (fake Cloud Storage): an upload session bound to the page's origin, the browser's PUT, Step 1 reads the upload once and the staging object is deleted; another user can't take it; not finished / expired / too large / wrong type / bad origin / bad id refused before anything is opened; without a bucket the app upload is used, and on Cloud Run a file over 32 MB gets a clear message; the `/api/wizard/uploads` endpoint |
| `test_v99_no_url_watch` | **9.9 no address typed**: a failed page-reading AI call reports its real reason (quota) instead of "address bar not visible"; the addresses read from a recording are a learned skill - the same recording starts without AI and without an address, another recording does not match; without AI and without that skill the start is refused at once; typed addresses without AI make no page call; checking for the skill doesn't count as a use |
| `test_durable_store` | local store and Cloud Firestore (fake client): a restarted backend restores a session (interrupted job reported, secrets never stored and asked for again, download gate re-synced); admin policy, learned skills, telemetry and admin logins shared between instances; signed tokens survive an instance change, password change and logout propagate, a new revision falls back to the default password, tampered tokens rejected |

The AI paths are tested with a deterministic fake model, and the tests use a throwaway admin-policy file and store.

### 8.1 Benchmark

`python scripts/benchmark.py [--runs N] [--scenarios ...] [--framework ...] [--tool-version ...]` runs the real
product - HTTP API, Watch, Govern, Build, a real Chromium run against the demo ERP, Export - several times per scenario,
checks the ERP ledger itself (a run passes only when its record is there, exactly once) and writes a JSON and a
Markdown report to `generated/benchmark/`.

Latest run ([docs/BENCHMARK.md](docs/BENCHMARK.md), raw data `docs/benchmark.json`) - offline mode, because the
benchmark machine has no Gemini access; with a key or Vertex AI the same command measures the AI path:

* Understudy 9.5.0 · framework `playwright-python` 1.56.0 · AI mode **LOCAL_HEURISTIC** · 3 runs per scenario · Python 3.13.16 · total 39s

| Scenario | Pass | First-run pass | Healed automatically | Passed thanks to a learned skill | Retries | Blocked by GOVERN | Duplicate records | Median end-to-end | Median execute+export |
|---|---|---|---|---|---|---|---|---|---|
| clean | 3/3 | 3/3 | 0 | 0 | 0 | 0 | 0 | 3.78s | 3.6s |
| modern_portal | 3/3 | 3/3 | 0 | 0 | 0 | 0 | 0 | 3.77s | 3.6s |
| selector_drift | 3/3 | 2/3 | 1 | 2 | 1 | 0 | 0 | 3.83s | 3.61s |
| policy_block | 3/3 | n/a | 0 | 0 | 0 | 3 | 0 | 0.22s | 0.0s |

**Model Armor**: blocked 10/10 attacks (100%), 0 false positives on 10 benign SOPs, 0.08 ms per input.

How to read it: a run passes when all five steps pass and its record is in the ERP ledger (checked by the benchmark, independently of Understudy). *policy_block* passes when GOVERN blocks the run and the ERP is unchanged (ledger changes = 0). *selector_drift* is expected to fail its first run and heal itself; the lesson is saved as a learned skill, so later runs of the same drift pass first time ("passed thanks to a learned skill"). Duplicate records counts extra ledger rows for the same run (idempotency).



### 8.2 Tool matrix - every configured tool, pinned version, same sample site
```bash
python scripts/tool_matrix.py            # build level: version pinning in manifest, sandbox plan and ZIP (seconds)
python scripts/tool_matrix.py --live     # + real installs, real runs with each tool, the ERP record, the ZIP
UNDERSTUDY_LIVE_TOOL_TESTS=1 python -m pytest tests/test_tool_matrix.py   # the same as a test
```
For each tool the admin pins one exact version (Playwright Python/JS `1.56.0`, Puppeteer `23.11.1`, Cypress
`13.17.0`, Selenium `4.25.0`, Robot Framework `7.1`, Requests `2.32.3`, REST Assured `5.5.0`), a user records Portal 1
and picks the tool without choosing a version, and the case checks that version end to end. Live, it must also be the
version that is actually installed, the run must use the tool's own runtime (strategy `framework`), the record must be
in the ERP exactly once and the downloaded ZIP must pin the version. What a machine cannot do is reported as SKIPPED
with the reason - the report is written to `docs/TOOL_MATRIX.md`.

Latest live run ([docs/TOOL_MATRIX.md](docs/TOOL_MATRIX.md), Understudy 9.5.0 on a CI machine where npm and PyPI are
reachable but Maven Central, the Cypress binary host and Chrome-for-Testing are blocked, and no Chrome is installed):

| Tool | Version | Install = pin | Run with the tool + ERP record + ZIP |
|---|---|---|---|
| Playwright (Python) | 1.56.0 | PASS | PASS |
| Playwright (JavaScript) | 1.56.0 | PASS (isolated env) | PASS |
| Puppeteer | 23.11.1 | PASS (isolated env) | PASS |
| Cypress | 13.17.0 | PASS (isolated env) | skipped - Cypress binary host unreachable |
| Selenium (Python) | 4.25.0 | PASS (isolated env) | skipped - no Chrome + chromedriver, Selenium Manager can't download |
| Robot Framework | 7.1 | PASS (isolated env) | skipped - same as Selenium |
| Python Requests (API) | 2.32.3 | PASS (isolated env) | PASS |
| REST Assured | 5.5.0 | manifest / plan / ZIP PASS | skipped - Maven Central unreachable |

On a normal developer machine or the Docker image (Chrome / Edge or network access, Java + Maven) the skipped runs
execute too.

Run `python scripts/generate_prompt_docs.py` to regenerate the prompt sections of `ARCHITECTURE.md` from the code.

---

## 9. Repository layout

| Path | Purpose |
|---|---|
| `server.py` | FastAPI backend: mounts the routers below, the demo ERP, the Gemini key / model endpoints (`/api/config/*`), the Model Armor alert feed (`/api/security/alerts`) and `/api/health` |
| `core/wizard/` | Understudy itself (everything except the pipeline contract). Steps: `watch` (Step 1, `WATCH_URLS` pages from the video), `inspection` + `page_probe` + `complexity` (9.0/9.4 live-page inspection and the complexity summary), `governance` (Step 2) + `governance_runtime` (the rules in every script), `build` + `build_agent` + `templates` (Step 3), `difficulty` (the Automation Difficulty Score and the traditional / agentic decision), agentic tools: `agentic_script` (the Gemini-written agentic script and its gate) + `agentic_templates` + `agent_runtime` (the agent, shipped as `understudy_agent.py`, Playwright / Selenium drivers) + `agent_stacks` (agentic tools the admin sets up) + `agent_relay` (agentic runs on the organisation's Gemini, per-run relay), `export_readme` (the ZIP's Gemini-written README), `executor` + `egress` (Step 4, restricted network), `verifier` (self-healing) + `runtime_repair`, `exporter` (Step 5), `session` (the state machine, adapter onto the orchestration layer). AI: `gemini` (google-genai on Vertex AI / AI Studio - the only SDK), `api_keys` (Gemini key and model), `llm` (JSON client, telemetry), `prompts` (every prompt), `recall` (learned AI answers), `skills` (learned skills). Safety: `guardrails` (Model Armor stages), `cloud_armor` (Google Cloud Model Armor), `security_alerts` (interception feed, in the durable store), `dom_discovery`, `payload_schema`. Tools: `frameworks`, `dependency_resolver`, `dependency_lock`, `tool_versions`, `tool_requirements`, `tool_stack` (install assistant), `tool_setup` + `setup_agent` + `setup_doctor` + `synth_labels` (AI setup, repair agent), `sandbox/` (the local process runner, smoke tests), `package_managers`, `runtimes` + `runtime_restore` + `runtime_advisor` + `jdk` + `environments` (runtime downloads - restored on every instance - and per-tool versions), `robot_syntax`. Admin and accounts: `admin_policy`, `admin_auth`, `admin_api` (`/api/admin`), `users`, `otp`, `mailer`, `projects`, `history`, `user_api` (`/api/auth`, `/api/projects`, `/api/history`), `escalations`, `notifications`, `events` (WebSockets), `activity_log` (telemetry), `code_health` + `env_health`, `gcp_check`, `lockout` (failed-login counters, shared on Firestore), `store` (local files / Firestore), `env_file`, `banner`; `api` (`/api/wizard`) |
| `core/pipeline_contract.py` | The pipeline contract every session is recorded in: the five pillars, `PillarResult`, the run report, the console telemetry line and the ZIP registry (protected from AI code fixes) |
| `erp/` | The demo ERP: Portal 1 (classic AP form), Portal 2 (redesigned single-page portal), Portal 3 (ids and field order change on every load), with self-hosted fonts |
| `understudy-ui/` | Next.js web UI: the Studio (`app/wizard/`, `lib/wizard.ts`, `lib/studio.ts`) and Mission Control (`app/page.tsx`, `app/*-panel.tsx`, `app/admin-*.tsx`, `app/synthesizer.tsx`, `app/install-assistant.tsx`, `lib/admin.ts`); `lib/live.ts` (live events), `lib/api.ts` (backend URL, key / model, alerts) - see `understudy-ui/README.md` |
| `assets/` | `demo_ap_recording.mp4` (the sample recording offered in Step 1) and `demo_ap_recording_globex.mp4` (a second recording of the same task, used in the demo video) |
| `skills/` | Learned skills written at runtime (`SKILL.md` + `lessons.json` per tool / target) |
| `generated/` · `uploads/` | Runtime state: sessions and store, tool environments, downloaded runtimes, telemetry, mail outbox, benchmark reports, code-health backups; uploaded recordings (both git- and docker-ignored) |
| `run_demo.*` · `setup.*` | Native start (Windows `.bat` / `.ps1`, macOS / Linux `.sh`): one-time setup, then backend + UI |
| `deploy_docker.*` · `docker-compose.yml` · `docker-compose.gcp.yml` · `Dockerfile.backend` · `Dockerfile.frontend` · `Dockerfile` · `entrypoint.sh` | Docker: Compose (backend + UI, Google Cloud credentials mounted read-only when needed) or the all-in-one image |
| `deploy_cloudrun.sh` · `deploy_cloudrun.bat` / `.ps1` · `cloudbuild.yaml` · `Dockerfile.cloudrun` · `entrypoint-cloudrun.sh` | Google Cloud Run (one container, one URL; Vertex AI, Firestore, Cloud Storage, Secret Manager) - bash and Windows deploy scripts |
| `scripts/gcp_setup.*` · `scripts/gcp_check.py` · `scripts/model_armor_setup.sh` · `scripts/check_deployment.py` | Connect an install to Google Cloud, check it, prepare Google Cloud Model Armor; check a deployed URL against the submission requirements |
| `scripts/benchmark.py` · `docs/BENCHMARK.md` · `docs/benchmark.json` | End-to-end benchmark and its latest report (section 8.1) |
| `scripts/tool_matrix.py` · `docs/TOOL_MATRIX.md` | One sample case per configured tool: pinned version → install → run with the tool → ERP record → ZIP (section 8.2) |
| `scripts/check_rpachallenge.py` | Shows the dynamic-page handling (probe, verified locators, plan, a real run) against rpachallenge.com or Portal 3 |
| `scripts/code_health.py` | List / show / revert the AI's own code changes from a terminal (section 2.7) |
| `scripts/mail_outbox.py` · `scripts/check_accounts_ui.py` | Read the local mail outbox (development); browser check of every account flow against a running install |
| `scripts/generate_prompt_docs.py` | Regenerates the prompt sections of ARCHITECTURE.md from `core/wizard/prompts.py` |
| `scripts/demo/` | Demo-video helpers: `serve.py` (start the backend normal / with a labelled stand-in model / with AI off) and `standin_model.py` |
| `tests/` | Every test suite (section 8); `wizard_helpers.py` (fake model, live server, isolation) and `conftest.py` |
| `CHANGELOG.md` | Release history |

---

## 10. Known limitations
* **Agentic runs (9.8, 9.9).** The Understudy Gemini Agent, the Gemini-written agentic script and the Gemini-written
  ZIP README are tested end to end with a local stand-in for the Gemini API, not against real Gemini output; the
  checks (script gate, README check) are what keep a poor answer out, and fall back to Understudy's own versions.
  Gemini chooses the difficulty parameters from the video, so two assessments of the same recording can choose slightly
  different parameters; tune them in Step 1.
  An agentic run is less deterministic and
  costs a Gemini call per step; the user's key lives in one server instance's memory (a restart, or on Cloud Run another
  instance, asks for it again).
* **No duplicate-record guard (since 9.0).** Understudy doesn't re-check the target after a run, so a run that saved a
  record and then failed (e.g. waiting for a confirmation) is healed and run again, and can save the record twice. Use a test
  environment, or turn on "ask" self-healing (the default) so that you approve each retry.
* **One record per run.** For data-driven pages like rpachallenge.com (10 rows, 10 submits), the generated code
  fills one record from `payload.json`; loop over your data in CI.
* Without Gemini (no key and no Vertex AI) the video is validated and stored but not interpreted. Learned skills can
  re-use what was learned about the same page earlier, but values seen only in a new recording can't be read.
* Learned answers are matched on fingerprints plus *soft* matching (stemming, a small synonym table, near-spellings) -
  not on learned embeddings. Rules are only re-used for the same SOP sentences (a hard safety gate), and a new
  recording of a page Understudy has learned is assumed to show the same process - the note in Step 1 says so.
* The restricted network is an application-level proxy: it does not stop raw sockets and Chromium never proxies
  loopback. Use a separate Cloud Run job / container with a VPC egress firewall for hard isolation.
* Wildcards (`*.example.com`) are supported on the blocked list only; the allowed list is exact-host.
* **Forgot password** says whether an email address has an account (as specified), so it can be used to test
  addresses; it is rate limited per address and per client. Switch the wording to a neutral answer if that matters
  for your organisation.
* Mail delivery depends on the SMTP provider (Gmail ~500/day, Brevo 300/day on the free plans; codes can land in
  spam until the sender domain has SPF/DKIM). Outbox mode is for development only.
* Tools outside the 8 offline templates need AI mode for code generation, and the AI-written smoke test (the 10
  catalog tools have offline smoke-test templates).
* There is no Docker sandbox (Cloud Run has no Docker daemon): tool setups, smoke tests and runs share the Cloud Run
  instance, separated by folders and limits, not by containers. A tool's environment is per instance and is
  re-installed on demand.
* The sandbox's memory limit is a watchdog (it samples the process tree), not a kernel cgroup; the Cloud Run instance's
  own memory limit is the hard ceiling.
* WebSockets don't pass the Next.js proxy, so the single-origin Cloud Run layout uses the polling fallback.
* Version management covers each tool's main package; supporting packages keep their vetted ranges in the sandbox,
  and the ZIP pins them to the versions that were installed.
* The Vertex AI, Firestore, Cloud Storage and Cloud Run paths are covered by tests with faithful fakes of the Google
  clients and by a local run of the single-origin container layout; this repository's CI has no Google Cloud project,
  so run `deploy_cloudrun.sh` in your own project to see them live.
* Cloud Run limits a request to 32 MiB: without a Cloud Storage bucket, recordings uploaded to a Cloud Run deployment
  must be smaller than that (Step 1 shows the real limit); with the bucket (the default deploy) they go straight to
  Cloud Storage, up to 250 MB.
* A session restored on another instance fetches its recording back from Cloud Storage (`UNDERSTUDY_GCS_BUCKET`);
  without a bucket only that instance has the file. Tool environments are per instance and are re-installed on demand.
* In Firestore mode the admin policy is re-read every few seconds; two admins editing at the same moment - last write
  wins.
* An escalation can be answered with **Ask the user**, but the Studio has no reply box: the user sees the question
  in the notification bell and by email and answers outside Understudy (or the admin rejects / confirms).
* Code-health fixes are verified by compiling, importing the backend and re-scanning. They don't run the full test
  suite, so review a fix before approving it (auto-apply is off by default; it is off on Cloud Run, where the code is
  read-only).
* The admin login is a single account; Studio users are local accounts managed in Mission Control (no SSO/OIDC yet).
  Put Cloud Run behind IAP or another identity layer before exposing it widely.
* Previous-session context is a value-free text digest (bounded to 20 sessions / ~14k characters); it is not semantic
  retrieval, so pick the sessions that matter.
* Discovery reads the page as an anonymous visitor: fields behind a login, inside cross-origin iframes or in closed
  shadow roots are not in the inventory (those steps show **later page** and rely on labels and self-healing).
* Extra packages the generated code needs are limited to the trusted list (by design); anything else falls back to the
  template - request the package as a tool in Mission Control if you need it.
* Runtime repair uses what the machine has. If no Chrome/Edge is installed and Selenium Manager can't download a
  driver (offline or proxy), the repairs run out and you decide what to do.
* The benchmark runs against the bundled demo ERP; real-world pass rates depend on your applications.
