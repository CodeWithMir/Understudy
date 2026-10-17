# Changelog

Newest first. Part 1 is what changed for users and admins in each release; part 2 holds the technical change notes
(modules, prompts, tests). The current state of the product is described in [README.md](README.md) and
[ARCHITECTURE.md](ARCHITECTURE.md).

## 9.9.1 - Cloud Run only sandbox, honest run results, Java packages that stick

* **One sandbox: local (Cloud Run has no Docker).** Understudy runs on Google Cloud Run, which has no Docker daemon, so
  the Docker sandbox is gone: `core/wizard/sandbox/docker.py`, the Docker / Auto modes, the Docker image in tool
  manifests, the smoke-test network option, the "Run the setup in Docker" answers and the CPU / process limits.
  Mission Control > Access & tools > Sandbox now sets only memory, run time-out and install time-out. A Docker / Auto
  mode saved by an older version is read as local. Local development and docker-compose for running the app itself
  are unchanged.
* **A passing pytest suite is no longer reported as failed.** pytest hid the script's `GOVERN OK` and `STEP` lines for
  passing tests, so a good run was marked "did not check the SOP rules" and then "repaired" with a reinstall. Runs now
  pass `-s` (also in the generated `pytest.ini`), and a missing `GOVERN OK` is never treated as a runtime problem.
* **STEP lines tell the truth.** Generated code must print `STEP <n> OK` only after the step worked and
  `STEP <n> FAILED` with the step's number; Understudy's code check rejects code that doesn't.
* **Java packages reach the run.** An admin-added package (e.g. Jackson from a runtime-help escalation) is written into
  the run's `pom.xml` before every run; Java imports are detected (Jackson, WebDriverManager, Playwright, TestNG,
  JUnit, REST Assured); well-known artifacts get a fixed version instead of Maven's `LATEST`.
* **Every run from the exported ZIP writes a report.** `run.sh` / `run.bat` (now starting `run.ps1`) keep
  `results/<run id>/understudy-report.html` + `.json` (pass/fail, SOP check, every step with its time, what failed, the
  data used, the full log), `run.log` with secrets masked, and `junit.xml` for pytest; `results/latest.html` opens the
  newest. Works for every tool with nothing extra to install; the tool's own reports are linked.
* **Robot Framework suites run.** A Library / Resource path the AI wrote relative to the wrong folder
  (`../../run_automation.py` from `tests/`) is repaired to the file that exists, and imports that point nowhere are
  sent back to the AI. A Python keyword library may not call `sys.exit()` (it stopped the whole Robot run) and must
  print STEP lines with `logger.console()`; STEP lines a suite printed into Robot's log are read back from
  `output.xml`, so such a run is no longer mistaken for a broken runtime.
* **A Selenium timeout is a page problem, not a broken runtime.** Selenium's error dump (`chromedriverVersion`) no
  longer turns a failed step into an ENVIRONMENT "reinstall".

## 9.9.0 - Govern before Build with the rules in every script, agentic scripts, agent-chosen difficulty

* **Govern comes before Build, and the SOP rules are part of every script.** The steps are now Watch → Govern → Build
  → Execute → Export. Step 2 (Govern) holds the payload table and the SOP rules; Step 3 (Build) generates the code with
  the confirmed rules in it: every suite - traditional or agentic - ships `governance.json` and a fixed checker
  (`understudy_governance.py` / `.js` / `UnderstudyGovernance.java`) that checks `payload.json` before step 1 and stops
  with `GOVERN FAILED: <why>` - in Understudy and wherever the ZIP runs. A rule violation in a run is not self-healed.
* **`payload.json` in the suite view.** Step 3 shows the payload file next to the code and `governance.json` - the
  Step 2 data exactly as the script reads it (secrets stay environment variables).
* **The agent decides the difficulty parameters.** The ten difficulty parameters are no longer a fixed list. Gemini
  watches the recording like an enterprise automation CoE and chooses the ten that matter most for this process - from
  a library of 21 candidates (each with what was measured) or its own process-specific ones - in five weighted
  categories, each with why it matters here and the moment in the video. Without Gemini, the ten most relevant
  candidates are chosen by rule. A parameter can be marked **not applicable**; its weight is spread over the rest.
* **One decision: traditional or agentic automation.** The enterprise toolkit and slides are only the reference for
  choosing the parameters; the assessment outputs no use-case classes or feasibility scale - just the recommended kind,
  and a warning when the picked tool is of the other kind.
* **Agentic script.** Gemini writes `run_agent.py`, a readable script of agent instructions (`agent.step`, `agent.sees`
  branches) that Step 3 shows like any generated script. At every run the agent decides each action on the live page.
  The script is checked (agent API only, no addresses or values, every recorded step present) and falls back to a
  script written from the steps. There is no final screenshot check: the run passes when every step passed.
* **Agentic tools are Understudy's own agent.** Browser Use and Stagehand are gone. **Understudy Gemini Agent**
  (Playwright + Chromium) is built in: always available to every user, nothing for the admin to approve or set up,
  can't be removed or blocked. **The admin can set up more agentic tools on the same agent** (Mission Control > Tools >
  *Agentic tool on Understudy's agent*), choosing the stack like a traditional tool: Playwright (Chromium, Chrome, Edge,
  Firefox, WebKit) or Selenium (Chrome, Edge, Firefox), versions, extra packages, a default model. Understudy installs
  it and runs its agent smoke test (no Gemini key needed); it is active for users when the test passes.
* **README in the ZIP, written by Gemini.** How to install and run this package (traditional or agentic), every
  environment variable with its meaning, the SOP check, the data and the output. Checked for secrets, API keys and
  foreign links; missing variables and the run command are added; Understudy's own README is the fallback.
* **Fixes found by running a real recording (the RPA Challenge form):** numeric-looking values (`1234567890`, `01234`)
  stay text instead of becoming `1234567890.0`; the Selenium and Robot Framework scripts find a button written as
  `<input type="submit" value="Submit">` and match button text in any case; offline rule parsing binds "companies" to
  Company Name (not Role in Company) and a field named in plain words ("The Last Name is required") to its field.
* **One repair round for AI answers.** When Gemini answers in the wrong format (broken JSON, a missing or wrongly
  typed field), Understudy no longer falls back straight away: it sends Gemini its own answer (quoted as data) and the
  exact errors once, and uses the corrected answer if it passes. Only a second bad answer falls back - to a learned
  skill, then the local rules. Network, quota and key errors are not retried. Mission Control shows each repair round
  as **AI repair round** with the errors that were sent back.
* **Starting without a page address, explained and working offline.** When the AI call that reads the addresses
  from the recording fails (key, quota, network), Step 1 now says so instead of "the address bar may not be visible".
  The addresses read from a recording are kept as a learned skill, so the same recording can be analysed again without
  typing an address even when AI is not available. Without AI and without that skill, Understudy asks for the start
  address right away and says why.
* **"No user" access for a tool.** Next to *All users* and *Selected users*, an approved tool can be set to **No
  user**: it stays installed and approved, but nobody can pick or run it until the admin opens it again. Users see it
  locked as "turned off by your admin".
* **The built-in agentic tool is no longer listed in Mission Control** (Access & tools, Environments, the tool
  registry): it is always available to every user and has nothing to approve, set up, version or restrict.
* **Recordings over 32 MB upload on Cloud Run.** Cloud Run refuses request bodies over 32 MB, so a longer recording
  failed in Step 1 with a bare "Request failed (413)". The browser now uploads the recording straight to Cloud Storage
  (the deployment's bucket) with a progress indicator, and Step 1 only names the upload - any size up to the 250 MB
  limit. Without a bucket on Cloud Run, the message says why and what to do.
* **Download any runtime version, not a fixed list.** Mission Control > Environments now offers every release line the
  official sources publish - Java (Temurin 8, 11, 17, 21, 25 ...), Node.js, Python, .NET, Gradle, Maven 3 / 4 - read
  live and marked LTS / end of life. End-of-life lines (e.g. Node.js 18, Python 3.9) are offered under *Older
  versions* and ask the admin to confirm. Same checksum-verified downloads; the setup chat understands any line
  ("download JDK 11"); the AI setup agent may propose any supported line. A run's self-healing (which can act without
  the admin) only downloads a line the official source lists as supported - never an end-of-life one.
* **Mirrors for runtime downloads.** Mission Control > Environments > *Download sources*: point Adoptium, nodejs.org,
  the Python builds, Maven Central, Gradle, .NET or Go at an internal mirror (Artifactory, Nexus ...). https only, no
  credentials in the address, a Test button, every change audited; the checksum is still verified, so a mirror that
  serves a different file is refused.
* **Cloud Run, setup from scratch:** a tool that pins a different Playwright version could not install its browser
  (`/ms-playwright` belonged to root, Understudy runs as its own user) - fixed in the image. A runtime download is
  refused up front when the instance lacks the memory for it (Cloud Run's disk is in memory), with the fix.
* **Fix: "No module named 'understudy_governance'" in pytest suites.** When the generated tests sit in a sub-folder
  (`tests/test_selenium.py`), pytest only puts that folder on the import path, so the SOP-rules check couldn't be
  loaded and the run failed before step 1 - in Understudy and in the ZIP. The vetted checker now also sits next to
  every test file that uses it (it finds `governance.json` in the suite folder above), and a suite run by pytest gets
  its own `pytest.ini`, so pytest no longer reads the settings of the folder Understudy runs in. Both are checked
  again before every run, in case self-healing moved a test. **Java (Selenium Java, TestNG, JUnit):** the checker
  class is in the entry test's package; a test class in another package that calls `UnderstudyGovernance.check`
  without importing it now gets its own vetted copy in its package, and `governance.json` is found from the project
  folder or a sub-folder (an IDE or `target/`). **JavaScript / TypeScript (Playwright JS/TS, Puppeteer, WebdriverIO,
  Cypress ...):** a test loads the checker with a path relative to itself (`require("./understudy_governance")` from
  `tests/` points at `tests/`), so the vetted checker now goes wherever such a path points inside the suite; in an
  ES-module project (`"type": "module"`) the `.js` entry is a small ESM wrapper around the `.cjs` checker, so `import`
  works too; `checkFile("payload.json")` finds the payload from the working folder. Other languages (C#, Ruby, Go,
  Kotlin) implement the check from `governance.json` themselves and are told to look for it up to two folders above.
* **The built-in agent gives way to the admin's.** Once the admin has set up an agentic tool on Understudy's agent,
  users see that tool (and any others the admin built) - the built-in Understudy Gemini Agent is no longer listed next
  to it. It stays the fallback when the admin has none (or none open to that user), and a session already using it
  keeps it. The agentic downside now reads "your organisation's Gemini" instead of "your API key".
* **The full model list on Vertex AI.** On Google Cloud (Vertex AI, no key) Mission Control's model list showed only
  the five built-in suggestions - the live list was only read with an API key. It now comes from Vertex AI itself:
  every Gemini model Google offers your project, recommended first, with **Refresh list** and a line saying where the
  list came from (or why it couldn't be loaded). The API key field is hidden on Vertex AI, where a key isn't used.
* **No Gemini key for users.** Agentic tools inside Understudy use the Gemini your admin set up - Vertex AI with the
  service account on Google Cloud, or the Mission Control key - and the model chosen there. The *Gemini API key for the
  agent* card is gone; Step 4 shows which Gemini the agent uses (or that Gemini isn't set up yet). The run itself
  holds no Google key: it asks Gemini through a relay Understudy starts for that run only. The exported ZIP still runs
  with the user's own key outside Understudy.
* **The upload limit shown is the real one.** Step 1 shows the limit of this deployment - 250 MB, or 31 MB on Cloud
  Run without a Cloud Storage bucket - and stops a larger file before uploading it, with how to make it smaller.
  (With the bucket the deploy script creates, recordings go straight to Cloud Storage, up to 250 MB.)
* **Tool setups keep working on Cloud Run.** What a setup downloaded (a JDK, Maven ...) was only on the instance that
  downloaded it: after a restart, a new revision or on a second instance the tool showed "needs java" although its
  smoke test had passed. Understudy now re-downloads such runtimes on every instance that lacks them (at start-up and
  before any setup, install or run; checksum-verified, no AI), keeps the paths working, and the Cloud Run image
  includes Java + Maven by default. "needs java" also counts the tool's own run settings.
* **Tool setup finds the browser on Windows and macOS.** A setup plan that needs Chrome checked for a program called
  `google-chrome` on PATH - a Linux name. On Windows Chrome is `chrome.exe` under Program Files and never on PATH, so
  the setup failed with "'google-chrome' is not installed" on a machine that has Chrome, and the setup agent kept
  asking the admin to install it. Browser checks now look where browsers are installed on each OS (and use a path
  the admin gave). With Selenium 4.11+ a missing Chrome or Firefox is no longer a blocker either: Selenium Manager
  downloads a matching one at the first run. The setup agent is told which browsers the server has.
* **Two accounts for one email can no longer slip through** when two sign-ups are confirmed at the same moment on two
  instances (a claim that was still being written was taken for a stale one; Understudy now waits up to a second for it).
* **Clean-up.** Removed what nothing uses any more: the agent task-file format (`agent_task.json`) and its scan branch,
  the final screenshot check, `agent_runtime.resolve_url`, `agent_stacks.smoke_env`, `users.set_password`, the fake
  Gemini's result-check answer, dead locals and imports. All documentation follows the Watch → Govern → Build order.

### Technical notes (9.9.0)
* `core/wizard/difficulty.py` rewritten: `LIBRARY` (21 candidates, categories, blockers), `candidates()`,
  `_rule_choice`, `_ai_params` (validated, clamped, ±3 of a measurement), largest-remainder weights summing to 100,
  `applicable` in `compute` / `recalculate`, `recommend` (approach only), `solution_fit` (tool kind vs recommendation), `refresh_fit`.
* Prompts: `AUTOMATION_DIFFICULTY` rewritten (uses the media, exactly ten parameters), `AUTOMATION_ROUTING` decide only
  traditional or agentic, new `AGENTIC_SCRIPT` (BUILD) and `EXPORT_README` (EXPORT) - 28 prompts.
* `core/wizard/agent_runtime.py` (replaces `agent_engine.py`) is shipped as `understudy_agent.py`: `Agent`
  (`step`, `sees`, `note`, `wait`, `open`, `govern`) and `run`; secrets from `UNDERSTUDY_SECRET_*`, masked everywhere.
* `core/wizard/agentic_script.py`: `generate` (two AI rounds with feedback), AST gate `check_script`, `render` fallback.
* `core/wizard/export_readme.py`: `facts`, `check`, `write` (cached per fact set); `exporter.env_example_lines`.
* Governance scan ignores comment lines for hard-coded values and blanks the vetted runtime.
* API: `DifficultyParam.weight` up to 100, `applicable`. UI: parameters grouped by category with **Applies**, video
  time and process-specific badges, the recommended kind and the tool-kind check; agentic-script badge; README preview.
* New `core/wizard/governance_runtime.py` (rules document, the three checkers, files_for / uses_checker / ai_instructions);
  `PILLARS` reordered; `build._governance_files` on every suite; templates call the check; `BUILD_GENERATE_SUITE` gets a
  GOVERNANCE input and a GOVERNANCE_MISSING gate; `Agent.govern()`; executor `GOVERN OK/FAILED` parsing; UI: payload
  table in Govern, *SOP rules in the script* in Build. Tests: `tests/test_v99_governed_scripts.py`; flows updated.
* `agent_runtime`: `Driver` / `PlaywrightDriver` / `SeleniumDriver`, `UNDERSTUDY_AGENT_DRIVER` / `_BROWSER`,
  `RuleBrain` + `UNDERSTUDY_AGENT_SELFTEST` (smoke test only); `verify` / `VERIFY_SYSTEM` / `VerifyFailed` removed (the
  gate no longer allows `agent.verify`). New `core/wizard/agent_stacks.py` (options, compose, smoke files, create, run
  env); `FrameworkSpec.agent`; `tool_setup._setup_job` runs the agent smoke test for agentic tools; admin endpoints
  `/api/admin/agentic-tools(/options)`; Mission Control builder card. Tests: `tests/test_v99_agentic_builder.py`.
* `admin_policy.BUILTIN_TOOLS` / `is_builtin` / `_ensure_builtin`: the built-in tool in every policy; remove / block
  refused; `tool_block_for` skips it. `browser-use`, `stagehand` removed from the catalog, `agentic_templates` and the
  tool matrix; `difficulty.tool_lists` and the session's options keep only built-in agentic tools; options carry
  `builtin`. `llm.sanitize_against_schema` keeps a string when the schema allows strings. Selenium `locate` /
  Robot `Find First Locator`: `BUTTON_XPATH` (button, role=button, input submit/button by value, case-insensitive).
* `llm.generate_json`: one repair round on a format / schema failure (`repair_prompt`: the original request, the
  rejected answer fenced as data and truncated to `REPAIR_MAX_ANSWER`, the exact errors, the whole expected schema -
  for item lists the `{"<wrapper>": [items]}` shape). `LLMResult.repaired`; telemetry kind `AI_REPAIR` (warn) and
  `stats().ai_repairs`; the call itself (network, quota, key) is never retried; partly invalid item lists still drop
  bad items without a repair. Tests: `tests/test_v99_ai_repair.py`.
* Watch without an address: `_watch_job` reports `pages_meta.error` (`evidence.ai_error`); recall policy `WATCH_URLS`
  (`_f_pages` / `_s_pages`, gate = recording hash); `recall.lookup(touch=False)`; `watch._pages_vars`,
  `watch.learned_pages`; `start_session` checks it after the media is saved; `_pages_from_video` runs without AI
  only when no address was typed; UI: the start form no longer requires an address without AI, hint updated;
  Mission Control names the memory "Page addresses from a recording". Tests: `tests/test_v99_no_url_watch.py`.
* `admin_policy.ACCESS_MODES` (`all` / `selected` / `none`), `access_denied_message`; `user_may_use` refuses
  everyone for `none` (also without accounts); `set_tool_access` refuses the built-in tool. `annotate_tools` adds
  `access_mode`. The admin policy view, `registry()` and `environments.tool_views()` leave out built-in tools. UI:
  third radio *No user*, red "No user" access chip, Step 1 lock text. Tests in `test_v90_agentic.py` /
  `test_tool_setup.py`.
* New `core/wizard/uploads.py`: `plan`, `begin` (checks name / type / size / origin, opens a resumable upload session
  for `understudy/uploads/<id>/<name>` bound to the page origin, records the slot in the state store), `take` (owner,
  6 h expiry, size re-check, download, delete; one use). `POST /api/wizard/uploads`; Step 1 (`/sessions`,
  `/sessions/{id}/start`) accepts `upload_id`. UI: `uploadRecording` (XHR PUT with progress), friendly 413 text.
  Tests: `tests/test_v99_direct_upload.py`.
* New `core/wizard/runtime_mirrors.py` (`SOURCES` with official prefixes and test files, `mirrors`, `mirrored`,
  `official`, `check_url`, `set_mirror` (audited, clears the catalogue cache), `view`, `test`); `runtimes._get` /
  `runtime_catalog._fetch` fetch through it (`_get_raw`), download-location checks use `_official`; admin API
  `GET /runtime-mirrors`, `PUT /runtime-mirrors/{source}`, `POST /runtime-mirrors/{source}/test`; UI *Download
  sources* card. Tests: `tests/test_v99_runtime_mirrors.py`.
* New `core/wizard/runtime_catalog.py` (`parse`, `label`, `nominal`, `catalog` / `entries` / `find` / `is_eol`;
  sources Adoptium, nodejs/Release schedule, CPython cadence, .NET releases-index, Gradle current, Maven Central;
  6 h cache, 10 min after a failure, `FALLBACK`, `UNDERSTUDY_RUNTIME_CATALOG=offline`). `runtimes.is_supported` /
  `kind_of` / `provision` take any parsed line; Maven by major; Python searches up to `PYTHON_BUILDS_PAGES` release
  pages; `memory_headroom_mb` + `NEEDS_MB` on Cloud Run. `package_managers.download(confirm_eol=)` (409 with
  `details.eol`), `label()` for any line; `environments.downloads()` / `downloadable()` from the catalogue (apply_fix
  confirms, the fix label says end of life); `confirm_eol` on `/environments/download` and `/toolchains/download`;
  prompts list release lines; the setup chat parses a named line. UI: LTS marks, *Older versions* select,
  `withEolConfirm`. `Dockerfile.cloudrun`: `/ms-playwright` owned by the app user. Tests: `tests/test_v99_runtime_catalog.py`.
* `governance_runtime.companions` (checker copies beside importing Python / Robot files, `pytest.ini` when the tool
  uses pytest and the suite has no pytest config; vetted), called by `files_for` and before each run in
  `session._do_execute`; the Python and JS checkers find `governance.json` up to two folders above. Tests:
  `test_v99_governed_scripts.py` (`TestCheckerFromATestSubfolder`: a real pytest run under a foreign `pytest.ini`;
  `TestJavaCheckerInAnyPackage`: javac + java with tests in two packages). `_java_companions`; Java `findRules()`;
  `_js_companions`, `JS_ESM_WRAPPER`, JS `checkFile` lookup; `TestJsCheckerFromAnyFolder` (node: CommonJS and ES modules).
* `admin_policy.builtin_agent_hidden(user)`; applied in `session._assess_difficulty`, `annotate_tools(current=)`,
  `public_view` and `difficulty.tool_lists`. Tests: `test_v99_agentic_builder.py` (`TestBuiltinGivesWayToTheAdminsAgent`).
* `api_keys.fetch_vertex_models` (publisher models, `_VERTEX_SKIP` filter), `_CACHE_SOURCE` per platform, `models_source` /
  `models_error` in the key status, `source` / `error` from `/api/config/models`; `_describe_model` strips Vertex names.
  UI: list source + **Refresh list**, no key field on Vertex AI. Tests: `tests/test_model_discovery.py`.
* New `core/wizard/agent_relay.py` (`Relay`: per-run loopback relay, token, size limits, `MAX_CALLS`, error mapping;
  `available`, `info`, `model`, `redact`); `agent_runtime.RelayBrain` (`UNDERSTUDY_AGENT_RELAY`,
  `UNDERSTUDY_SECRET_AGENT_RELAY_TOKEN`, no proxy), `NOT_PAYLOAD`; `gemini.generate(image=)`; executor starts / stops the
  relay and reports `agent_ai`; the Gemini host left the run's egress allow-list. Removed: `agent_keys.py`, the
  `/sessions/{id}/agent-key` endpoints, `session.set_agent_key` / `set_agent_model` / `clear_agent_key`, the Step 4 key
  card, `UNDERSTUDY_AGENT_KEY_TTL_S`, the fake Gemini HTTP server (tests use `fake_gemini.decide` as the `AGENT_STEP`
  answer). `public_view.agent_ai`; execute refuses with `needs_ai` when the organisation has no Gemini.
* `uploads.max_bytes` / `limit_info` -> `/api/wizard/info` `max_media_mb` + `max_media_why`; Step 1 shows it and
  pre-checks the file.
* New `core/wizard/runtime_restore.py` (`wanted`, `missing`, `restore`, `start`, `ensure`); `runtimes.key_of_path` /
  `resolve_path`; applied in `admin_policy.tool_runtime` / `toolchain_bins` / `toolchain_homes`; `ensure` in
  `tool_setup._setup_job` and `session._ensure_dependencies`; `start` in the server lifespan;
  `dependency_resolver.check_toolchain(ecosystem, tool_id)`; `Dockerfile.cloudrun` `INSTALL_JAVA=true`.
  Tests: `tests/test_v99_cloud_run_fixes.py`, `test_v98_difficulty_agentic.py` (agentic run on the organisation's Gemini).
* `runtime_repair`: `BROWSER_NAMES`, `browser_kind`, `find_browser` (CHROME_BIN / CHROME_BINARY / EDGE_BINARY /
  FIREFOX_BINARY, then `detect_browsers`, which now also finds Firefox), `selenium_manager_fetches_browsers`.
  `LocalProcessSandboxRunner.check_dependency("binary", <browser>)` uses them; `tool_setup._server_facts` reports
  `browsers_installed` and counts chrome.exe as `google-chrome`. Tests: `tests/test_v99_browser_dependency.py`.
* `users._claim_email`: a claim read without its `at` timestamp is still being written by another instance - re-read
  for up to 1 s before the stale-claim rules apply (fixes a flaky double sign-up under a race).
* Tests: `test_v98_difficulty_agentic.py` rewritten (34 tests: offline / AI parameter choice, tuning with N/A, solution
  fit, script gate, Gemini script rounds, README check, end-to-end agentic run with an `agent.sees` branch).

## 9.8.0 - Automation Difficulty Score, agentic automation and routing

Understudy no longer only generates selector scripts. Step 1 measures how hard a workflow is, recommends a traditional
or an agentic tool, and agentic tools run with an AI agent at run time - with the user's own Gemini key.

* **Automation Difficulty Score (Watch).** A score out of 10 from ten parameters - DOM stability, asynchronous
  loading, iframes & shadow DOM, CAPTCHA & bot protection, authentication & 2FA, interaction complexity, navigation &
  multi-site flow, data variability, visual validation need, process length & branching. Each shows its evidence, the
  AI's rationale, value, weight and contribution. Measured values come from the live-page inspection, the steps and the
  SOP; Gemini refines them within a band. A weighted cubic mean keeps one hard parameter from being hidden; CAPTCHA /
  2FA of 7+ set a floor.
* **Parameter tuning.** Values (sliders) and weights can be changed with a live preview; **Recalculate** recomputes the
  score on the server and redoes the recommendation; **Back to assessed values** undoes it. Changes are logged and the
  build agent receives the tuned values.
* **Traditional vs agentic routing.** A recommendation with confidence, the deciding reasons (naming parameters and
  values) and what speaks for the other approach, plus the tools of each kind with a fit rating to **Use** or
  **Request**.
* **Agentic tools.** *Understudy Gemini Agent* (built in: Gemini decides each action on the live page, never sees
  secret values, retries with a screenshot, checks the final page visually), *Browser Use* (Python) and *Stagehand*
  (JavaScript). Build writes the task (steps, labels, payload keys) instead of a script.
* **Secure key hand-off (Execute).** The user sets their Gemini API key for the session: checked with Google, kept in
  memory only (8 h), shown masked, passed to the agent's run as a secret variable, scrubbed from the log, never stored
  or exported; the run's network may reach the Gemini API. Running an agentic tool without a key is refused.
* **Agent guard rails.** A step allows only matching actions, values come only from that step's own payload key, the
  element must be on the page, and secret values never reach the model (masked in snapshots, page text and
  screenshots). Python suites print UTF-8 (no crash on Windows with non-ASCII labels).
* The ZIP of an agentic tool documents the key variable in `.env.example` and README.

## 9.7.2 - Node.js tools install on Windows again

* **Every Node.js was rejected on Windows.** Understudy checks that a Node.js has a working npm next to it. A Windows
  Node.js folder holds both `npm` (a shell script only Git Bash can run) and `npm.cmd`; the check ran `npm`, which
  Windows can't start, so the system Node.js *and* the ones Understudy downloaded were all reported as "does not
  start". It now runs `npm.cmd` (and `gem.cmd` for Ruby). Tools that need Node.js (WebdriverIO, Cypress, Playwright JS,
  Puppeteer) install with the right runtime again.
* **Retries start clean.** An npm install that failed half-way left `node_modules` / `package-lock.json` in the tool's
  folder, and the retry reused them - npm then crashes with "Cannot read properties of null (reading 'edgesOut')".
  An unfinished npm folder is now cleared before the next attempt (a finished install is marked and never touched) -
  on Windows, Linux and macOS, in the local sandbox and in the Docker sandbox.

## 9.7.1 - One admin-password rule everywhere

Local starts now behave like Cloud Run: no more published default password that comes back after every restart.

* **First start:** `run_demo.bat` / `.ps1` / `.sh`, `deploy_docker.sh` / `.bat`, the Docker all-in-one entrypoint and
  `python server.py` generate a unique Mission Control password, save it (`.env`, `UNDERSTUDY_ADMIN_PASSWORD`) and show
  it **once** - the same shape as the Cloud Run one in Secret Manager.
* **Every later start** reuses it; restarting never changes it.
* **Show it again:** `python scripts/admin_password.py show` (Docker: `grep UNDERSTUDY_ADMIN_PASSWORD .env`; Cloud Run:
  `gcloud secrets versions access latest --secret=understudy-admin-password`). New one: `admin_password.py reset`.
* A password changed in Mission Control still lasts until the next restart (Cloud Run: the next revision).
* `docker-compose.yml` no longer injects the published default; the value comes from `.env`. The Cloud Run entrypoint
  warns when no password is mounted. Login screen, banner, `.env.example`, README section 2 and ARCHITECTURE §7c
  describe the rule.

## 9.7.0 - Google Cloud Run first

The submission must use Google AI models and be deployed on Google Cloud (Cloud Run). Understudy already ran Gemini on
Vertex AI and shipped a Cloud Run deployment; this release makes that the primary path and closes the gaps a public,
scaled-out Cloud Run service exposes.

* **Windows deploy:** `deploy_cloudrun.bat` / `deploy_cloudrun.ps1` (PowerShell 5.1+, the Google Cloud CLI) do the same
  steps as `deploy_cloudrun.sh`: APIs, Firestore, Artifact Registry, recordings bucket, least-privilege service account,
  Cloud Build, Cloud Run with Gemini on Vertex AI.
* **No published password on a public URL:** the deploy scripts generate the Mission Control password on the first
  deployment, keep it in Secret Manager (`understudy-admin-password`, readable only by the service account), print it
  once and mount it on every revision (`UNDERSTUDY_ADMIN_PASSWORD_SECRET=<name>` for your own secret, `none` to opt
  out). `cloudbuild.yaml` mounts the same secret.
* **Shared state for scale-out:** failed-login counters for Studio users and the admin (`core/wizard/lockout.py`) and the
  Model Armor interception feed (`core/wizard/security_alerts.py`) move to the durable store - Firestore on Cloud Run -
  so a brute-force attempt can't escape the lock-out on another instance, and the feed is the same everywhere and
  survives new revisions. On a single local instance the counters stay in memory, as before.
* **Submission check:** `/api/health` reports where Understudy is hosted (`cloud_run` + service and revision) and which
  Google AI it uses (Vertex AI / Gemini API, model) - nothing secret. `python scripts/check_deployment.py <url>` checks
  a live deployment against the requirements (HTTPS, Studio, Mission Control, demo portal, Cloud Run, Gemini; WARN for
  Vertex AI and Firestore); the deploy scripts run it at the end.
* The backend banner shows **Hosting** (Cloud Run service / revision, or how to deploy); the local and Docker start
  scripts print the Cloud Run deploy command.
* Docs: README opens with a requirements-to-evidence table and the Cloud Run quick start (Linux / Cloud Shell and
  Windows); path E, the admin password and ARCHITECTURE §12 (several instances, the submission check, run isolation on
  Cloud Run) are updated.
* Tests: `tests/test_cloudrun_ready.py` (a second simulated instance sees the lock-out, the feed survives a restart and
  is pruned, the health fields, the deployment check passing and failing).

## 9.6.0 - Two-step sign-in, and dead code removed

* New: **two-step sign-in** for Studio users. Profile > Two-step sign-in > *Turn on* mails a 6-digit code first (a
  mailbox that doesn't work can't lock anyone out); once it is on, the right password no longer logs in by itself -
  the login answers `two_factor_required` and the code mailed to the account finishes it (`POST /api/auth/login/verify`;
  10 minutes, single use, 5 tries, wrong codes also count towards the account lock-out). *Turn off* needs the
  password; the admin can turn it off for a locked-out user (Mission Control > Users > **2-step off**,
  `POST /api/admin/users/{u}/two-factor/disable`). Notice mails when it is turned on or off. Sign-in codes can't be
  redeemed as password-reset tickets. Tests: `tests/test_two_factor.py`.
* The start scripts (`run_demo.sh/.bat/.ps1`, the Docker entrypoint) say where email codes go without SMTP
  (`python scripts/mail_outbox.py`); the backend banner counts accounts with two-step sign-in.
* Expired one-time codes are now cleaned up when new ones are created (`otp.cleanup` existed but was never called).
* Removed code nothing called: the Studio's `PUT /api/auth/password` (Studio users change passwords with an emailed
  code) and `users.change_own_password`; `users.generate_password`, `api_keys.get_available_model_ids`,
  `FrameworkSpec.npm_spec`, `package_managers.ecosystem_of`, `recall._values_by_key`, `sandbox.local.python_ok`,
  `sandbox.smoke.template_for` (and its test line); a duplicated `frameworks.ensure_browser_download` (defined and
  called twice); unused UI client calls (`updateProject`, `changePassword`, `registry`, `allowTool`, `approve`,
  `setHistory`, `extractRules`); unused imports.

## 9.5.3 - Find a tool by typing

* Step 1 > Choose how to automate it: typing in the tool box now filters the cards live (name, language, ecosystem or
  package; a version such as "13.6" is ignored for matching), shows every match instead of the first three, and says
  how many match. Enter picks the tool when exactly one matches (and no version is typed); otherwise it requests what
  was typed, as "Use this tool" always did. Before, the box only requested a tool and the user had to open
  "Show N more tools" and scroll.

## 9.5.2 - Demo videos, and fixes found while recording them

* New: judge and promo videos (`docs/DEMO_VIDEO.md`, rewritten for the current 5-step flow) and the tooling that
  records and edits them from the running product (`scripts/demo/video/`: screencast recorder, narration, captions,
  original synthesized music, 16:9 and 9:16 promo).
* Fixed: the build agent's locator check named a button after a heading next to it ("FIN-SOP-402 ..." instead of
  "Submit Invoice"), so correct locators were reported as unconfirmed. Buttons and links are now named by their own
  text (`core/wizard/page_probe.py`).
* Fixed: a Model Armor interception named the selected Gemini model as its inspector even when the built-in pattern
  rules had blocked the input without any AI call. The alert now names the layer that blocked it
  (`core/wizard/guardrails.py`).
* Fixed: a password set with `UNDERSTUDY_ADMIN_PASSWORD` was reported as "the default admin password".
* Fixed: FastAPI's deprecated `on_event("startup")` hook is replaced by a lifespan handler, so a fresh start has no
  code-health warning.
* Stand-in model for offline demo recording: answers the page-address prompt honestly (address bar not visible),
  names the session and lists the locators its code uses.
* `tests/test_gemini_key_propagation.py` plants its own warning for the code-health AI call (the tree has none now).

## 9.5.1 - Mission Control lists the key's real Gemini models again

* Fixed: Mission Control > AI, security & account showed only the 5 built-in model names. The google-genai client
  used for `models.list` was created inline and garbage-collected before the first page was fetched ("Cannot send a
  request, as the client has been closed"), so discovery fell back to the suggestions. The client is now held for the
  whole listing (`core/wizard/api_keys.py`).
* New test `tests/test_model_discovery.py` (a local stand-in for the Gemini API) checks that the live list is used
  and that models that cannot generate content (embeddings) are left out.

## 9.5.0 - One SDK, no v4 engine: the orchestration module reduced to its contract

Behaviour for users and admins is unchanged (same 5 steps, gating, telemetry, ZIP); the code base is about 6,500 lines
and one deprecated dependency smaller.

* **`core/pipeline_contract.py`** replaces `core/five_pillar_pipeline.py` (139 KB). The wizard only ever used the
  module's contract - `PillarResult`, the run report and its registry, the ZIP registry and the telemetry emitter -
  and that is what was kept, with the same field names and gating. The module's own v4 run path is gone, and with it
  the eight v4 modules only it imported: `watch_ingest_v3`, `package_exporter`, `polyglot_compiler`, the Mode A/B/C
  factories, `supervisor_sop`, `currency_converter`, `sop_validation_script`, `contracts`. The rule "do not change the
  orchestration layer" (from 5.0) is retired: its purpose - building the wizard without breaking the running system -
  was met, and the remaining code never ran.
* **`google-generativeai` removed.** `google-genai` is the only Gemini SDK: the key check and model list
  (`core/wizard/api_keys.py`, formerly `core/api_key_manager.py`) were ported to it, the transport's `legacy` mode
  (`UNDERSTUDY_GEMINI_SDK=legacy`) is gone, and `server.py` no longer silences the old SDK's start-up FutureWarning -
  there is none. Mission Control > Code health starts with an empty list.
* `core/model_armor.py` → its regex signatures are stage 1a of `core/wizard/guardrails.py` (unchanged patterns);
  `core/datastore.py` → `core/wizard/security_alerts.py` (the Model Armor interception feed). `core/` now holds only
  `pipeline_contract.py` and `wizard/`.
* `emit_pipeline_telemetry` prints the tagged console line only; the in-memory v4 "agent_runs" collections it also
  filled (read by nothing since 9.4.1) are gone.
* Code health protects `core/pipeline_contract.py`; its tests use a fixture module written only into their throw-away
  copy of the code (Understudy has no start-up warning left to fix).

## 9.4.1 - Clean-up: legacy code, tests and scripts removed; docs and start scripts brought up to date

Nothing changes in the Studio's five steps or in Mission Control. Removed because nothing in the wizard or the admin
console used them any more:

* **Backend (`server.py`)**: the pre-wizard v4 endpoints - `/ws`, `/api/watch/*`, `/api/run/execute`,
  `/api/pipeline/*`, `/api/runs/*`, `/api/package/download`, `/api/governance/*`, `/api/govern/inspect-sop`,
  `/api/security/screen-note`, `/api/contract/latest`, `/api/demo/scenario/*`, `/api/erp/ledger` (clear; the ERP's own
  `DELETE /api/erp/ledger` stays) - and the seeded "supervisor inbox" task. `server.py` now only mounts the wizard,
  admin, account, project, history and live-event routers, the demo ERP, the Gemini key / model endpoints and the Model
  Armor alert feed.
* **Wizard**: the pre-9.0 page verification (`verifier.verify` and its snapshot helpers, the `VERIFY_ASSESS` prompt,
  the `POST /api/wizard/sessions/{id}/verify` alias, `session.verify`), the "verification escalation" a user could
  raise before 9.0 (`escalations.create / reply / withdraw`, the user-side escalation routes, `session.escalation_eligible`,
  `admin_confirm_verify`, `VERIFIED_BY_ADMIN.md`), the idempotency-guard stub, `legacy_download_url`, and the 410
  tombstones `PUT /api/admin/users/{u}/password` and `/active`. Runtime-help escalations (the admin fixes a package,
  version, run variable, PATH folder or network setting) are unchanged. An older verification escalation still stored
  can be rejected in Mission Control; confirming it answers 409.
* **Modules**: `core/agent_runtime.py`, `core/govern_checkpoint.py` and `runner/` (the v4 Mode A/B/C and legacy RPA
  runners). The protected orchestration layer `core/five_pillar_pipeline.py` and the v4 modules it imports are kept
  unchanged - the wizard still records its steps through it.
* **Tests**: `tests/legacy/` (v4 integration suites), `tests/test_five_pillar_pipeline.py` (exercised only the removed
  v4 run path), the tests of the removed verification; suites that touched removed endpoints now test the wizard API.
* **Scripts and assets**: `scripts/test_e2e_workflow.py`, `scripts/test_live_workflow.py`,
  `scripts/record_ap_scenario.py`, `assets/raw_video/`, `assets/screenshots/`, `assets/sample_invoices/`,
  `assets/FIN-SOP-402.txt`, `assets/demo_portal2_recording.mp4`, `schema.json`, `portal2_check.txt`, `uploads/videos/`.
* **Web UI**: `app/wizard/escalation.tsx`, `lib/firebase.ts`, the v4 calls in `lib/api.ts`, the unused `VerifyStep`
  alias and user escalation calls, the create-next-app images and the unused `public/demo_ap_recording.mp4`, and the
  unused `clsx` dependency.
* **Dependencies**: `pypdf` and `pillow` (only the removed code used them); `requirements-dev.txt` now includes the
  Google Cloud clients and `pytest`, so the whole suite runs from one install.

Fixed / updated:

* **Start scripts** (`run_demo.*`, `setup.*`, `deploy_docker.*`, `entrypoint.sh`, `deploy_cloudrun.sh`) read the
  version from `core/wizard/__init__.py` (they were stuck at 8.3.0, so upgrades didn't re-install or rebuild), show the
  5 steps as *Watch -> Build -> Govern -> Execute -> Export*, list Portal 3, wait for `/api/health` before starting the
  UI, and check the Node.js version (20.9+). `setup.*` re-installs the UI's packages once after an upgrade
  (`npm ci`).
* **Docker**: `docker-compose.yml` passes the whole `.env` to the backend (before, only a fixed list reached it), uses
  `/api/health` as health check and tags images with the version; images use `npm ci` and have a `HEALTHCHECK`; the
  all-in-one fallback also mounts `skills/`.
* **Cloud Run**: `/portal3` is proxied in the single-origin image (it returned the UI's 404 before);
  `cloudbuild.yaml` sets `UNDERSTUDY_SIGNUP=off` like `deploy_cloudrun.sh`.
* **Benchmark** (`scripts/benchmark.py`) works with 9.x again: single-user mode and the catalog tools, Step 5 = Export,
  a run passes only when its record is in the ERP ledger, and `--tool-version` pins the admin's version. New results
  in `docs/BENCHMARK.md`. `scripts/tool_matrix.py` uses Export as well.
* `.gitignore` / `.dockerignore` cover `.venv`, tool caches, credential files and runtime folders; `.env.example`
  documents the operator settings (`UNDERSTUDY_SANDBOX`, `UNDERSTUDY_DEFAULT_TOOLS`, `UNDERSTUDY_SETUP_AGENT`, ...).
* README, ARCHITECTURE, BENCHMARK and the other docs describe the current state; the release history moved here.

---

# Part 1 - Release highlights

**Fixed in 9.4:** a C# (.NET) tool setup failing with `dotnet restore ... exited with code 2147516561` ("No .NET SDKs
were found") **although .NET SDK 8 was downloaded** in Mission Control > Environments. The `dotnet` on the server's
PATH was the .NET *host* in Program Files, which has no SDK, and Understudy used it instead of the SDK it had downloaded.
Now:
* A tool setup (and every run in the local sandbox) checks that the runtime it needs **really works** (for .NET:
  `dotnet --list-sdks` lists an SDK). If not, it uses the working install Understudy has, preferring one it
  downloaded. It puts it first on PATH and sets `DOTNET_ROOT` and `DOTNET_MULTILEVEL_LOOKUP=0`. The setup chat says so:
  *Using .NET SDK 8.0.425 (downloaded by Understudy) - the .NET SDK on PATH can't build - it has no SDK*.
* If such a failure still happens, the question offers **Use .NET SDK 8.0.425 (downloaded by Understudy)** in one
  click. The setup agent sees the installed runtimes and can propose `use_runtime` itself, instead of asking you to
  install the SDK.
* **A path you type is understood.** A folder or the program itself (`...\runtimes\dotnet-8\dotnet.exe`) works,
  whether in an answer or in a hint for the agent. A known install becomes `use_runtime`, a .NET folder gets
  `DOTNET_ROOT`, and the path of a program becomes its folder instead of being rejected with "is not a folder".

**Tool setups check every runtime first, and ask before downloading (9.4).** Before installing anything, a setup
checks that each runtime the tool needs works **for what the build really uses**:

| Runtime | Must have |
|---|---|
| Java | a JDK with `javac` (a JRE, e.g. Oracle's `javapath` shim, is not enough for Maven / Gradle) |
| `JAVA_HOME` | verified, not trusted |
| Node.js | `npm` |
| Ruby | `gem` |
| .NET | an SDK |
| Maven / Gradle / Go | starts |

* A runtime that doesn't work is replaced by a working install Understudy already has.
* If there is none, the setup **stops before installing** and asks: *"to set up X I need Java (JDK) - may I download
  Eclipse Temurin JDK 21? I'll use it for this tool and continue the setup by myself."* Other runtimes it also needs
  (e.g. Maven) are downloaded with it.
* Click the download (*recommended*) or another version, give a location, or just write what to do: "yes", "use 17",
  a path. The setup downloads (checksum verified) and continues.
* What you chose or approved in a setup (a `JAVA_HOME` you typed, a download you approved) is never replaced
  automatically.

**New in 9.4:**
* **The AI reads the page addresses from your recording.** You no longer have to type them. Before it analyses the
  process, a page-address agent watches the recording and lists every page you visit, in order. It reads them from the
  address bar, typed URLs, links and the narration, and notes where in the video it saw each one. At the top of Step 1,
  **Pages the automation visits** shows the list:
  * each page with *seen in the recording at 00:41*, what you do there, and what the AI saw;
  * edit an address, add a page it missed, remove one, or move pages up and down;
  * **Save pages** keeps the AI's understanding of the steps and redoes only the page inspection and the complexity;
  * **Watch again with these pages** makes the AI re-watch the recording with your list and an optional note (e.g.
    "after Save the app opens the approvals page"). A page you removed is not added back;
  * **These pages are right** - **Build waits until the pages are checked**;
  * addresses the AI saw but can't use (blocked by your admin, or only partly readable) are listed separately.

  Typing addresses in Step 1 is optional with AI. Without AI the recording can't be read, so the start page is
  required. If the AI can't read any address (no address bar visible), Step 1 asks you to add the start page and
  watch again.
* **More than one page address.** A process can visit several pages (up to 10). Page 1 is where it starts.
  * The AI sees every page's fields, and a step that goes to another page uses that page's address.
  * Each address passes the same URL checks (Model Armor, admin allowed / blocked lists); the run's network allows
    every listed site.
  * The generated code never contains the addresses. It reads page *n* from `TARGET_URL_<n>` (`TARGET_URL` is page 1,
    `TARGET_URLS` has all of them). The ZIP's `.env.example` and README list them, so you can point the suite at
    another environment.
* **The inspection looks at the automation first, then checks only what it needs.** Before 9.4 Understudy always loaded
  the page twice and only compared locators. Now:
  * An inspection agent reads the steps, the pages and the SOP and **picks the checks this automation needs**, each with
    its reason, e.g. "sign-in: step 3 types a password", "file upload: step 6 attaches a file", "dialogs: 'Delete draft'
    may pop up a confirm", "repeated rows: the SOP repeats the form for each row". Other checks: elements the steps use,
    the page opens directly, CAPTCHA, iframes, shadow DOM, late rendering, new tabs. Offline, rules make the same choices.
  * **The agent decides whether, and how many times, to reload.** Every page is opened once first. A reload agent
    then looks at what that load showed (generated-looking ids on the fields the steps use, recorded fields that are
    gone, a single-page app) next to what the automation does. It decides **per page** how many more fresh loads it
    needs to compare ids, names and field order (0-3), with its reason. It also decides how many fresh loads the build
    agent uses to confirm the script's locators (1-3). Without AI, rules decide. The card shows *AI decided* or
    *Rules decided* for every page. Nothing is ever clicked or submitted.
  * Only findings that matter for **this** automation raise the score. An iframe that no step touches adds nothing.
  * New factors: several pages, more than one site, a page that needs a sign-in first, elements that appear only after
    an earlier step, a link that opens a new tab, pop-up dialogs.
  * **"How complex is this automation?"** now shows *What Understudy checked*: each check, why this automation needed it,
    and what it found.
* **The build agent gets the complexity.** The score, the challenges and how to handle each one go to the code
  generator, and to self-healing. Code for a simple page stays simple. For a complex one it adds what the challenges
  call for: label-only locators, finding elements again after every submit, frame switching, new-tab handling. Step 2
  shows which level the code was written for.
* **Locators are checked on as many fresh loads per page as the reload agent decided** (one for a stable page). Ids and
  positions the inspection already saw change are rejected without loading the page again.

**New in 9.3:** **Escalations: apply a fix to everyone, or only to the user who asked.** When you confirm a runtime-help
request, choose **Everyone using <tool>** (as before) or **Only <user>**. With *Only <user>*, these fixes reach only
that user's runs; other users keep the tool unchanged:
* a package;
* an allowed version;
* a run variable;
* a PATH folder.

Server settings always apply to everyone, because the server has only one of each:
* the proxy bypass;
* the run network;
* switching or downloading a runtime.

The new **Fixes for single users** list in Escalations shows every per-user fix, with **Remove**.

**Changed in 9.2 (Studio layout):**
* **Every box in the Studio starts open.** Mission Control keeps its boxes collapsed. *Collapse all / Expand all* and
  your own folding still work and are remembered.
* **New project** and **New session** have moved from a card on the right to the top left:
  * a button next to the page title;
  * two labelled buttons at the top of the explorer sidebar.
* An empty project shows a **Start your first session** panel in the middle of the page.

**New in 9.1:** **sessions name themselves**, like a chat:
* **New session** asks for nothing. The session is called "New session" until Step 1 has understood your recording and
  SOP; then the AI names it, e.g. "Supplier Invoice Entry - AP Portal" or "RPA Challenge Form Fill".
* The name is short and specific, unique within the project, and never contains your values.
* **Rename** it any time: click the name in the breadcrumb (or the pencil), or use **Rename...** in the explorer's
  menu. A name you choose is never changed by the AI.
* Offline, the WATCH process name is used.

**New in 9.0:**
* **Step 5 is Export only.** When the run in Step 4 passes, the ZIP is created right away. Understudy no longer opens the
  page again to compare before and after, and there is no "ask your admin to verify" escalation. Runtime-help requests
  (the admin installs a package or allows a version) are unchanged.
* **The build is agentic.** Gemini writes the suite for the tool you picked, and Understudy then checks it. Every
  locator the code uses is tested on **two fresh loads of the live page**. A locator must find exactly one visible
  element of the right kind with the expected label. A locator that breaks, an unsafe pattern or a syntax error goes
  back to Gemini together with its previous code (up to 3 rounds), instead of falling back to a template.
* **Self-healing is agentic too.** A failed run sends the error, the failing step, the log tail and the code to the same
  loop, along with a fresh probe of the page, so the fix rewrites the code against what the page is now.
* **Pages that change on every load are handled,** for example rpachallenge.com, where ids, names and field order change
  each time. Before you pick a tool, Understudy loads the page twice and learns:
  * which attributes stay the same;
  * which ones change;
  * a label-anchored locator for each field, verified on both loads.

  It never uses ids, positions or nth-child when they move. Offline (no AI), the plan and payload keys are rebuilt from
  the fields' visible labels. **Portal 3** (`/portal3`) is a local replica of this kind of form for demos and tests.
* **"How complex is this automation?"** appears in Step 1, above the tool cards. It shows:
  * a 1-10 score and a level (Simple, Moderate, Complex, Very complex);
  * what makes the automation harder (changing ids, moving fields, iframes, shadow DOM, a single-page-app framework,
    CAPTCHA, login, file upload, repeated data rows);
  * how Understudy handles each of those;
  * how well each tool fits.

  The score comes from the evidence; Gemini explains it and can move it by at most 2.
* **Per-tool access.** For every tool the admin approved, Mission Control > Access & tools has an **Access** setting:
  * **All users**, or **Selected users** (a checklist). Other users see the tool as locked.
  * **Delete** removes the tool for everyone and frees its isolated environments on the server.

**Fixed in 8.5.3:** a Robot Framework run failing with "No module named 'JSONLibrary'": Understudy now reads the
`Library` lines of generated .robot files, so libraries like JSONLibrary are installed with the suite
(`robotframework-jsonlibrary` and `robotframework-seleniumlibrary` joined the trusted extras) · when self-healing asks
the admin to add a package, Escalations offers it as **one click** ("Add robotframework-jsonlibrary & notify the
user") and *Fix it yourself* has an **Add a package to the tool** field - for any tool (pip, npm incl. scoped packages, Maven /
Gradle coordinates, gems, Go modules, NuGet); the approved tool and the user's session get it, and the next run installs
it.

**Fixed in 8.5.2:** Robot Framework smoke tests still failing with "Keyword 'Browser.Fill Text' expected 2 to 3
arguments, got 0" after every AI rewrite: the test wrote the selector as `#name`, and in a .robot file a cell starting
with `#` is a **comment** - the arguments silently disappeared although the line looked right (and Understudy's own
instructions told the AI to "type into #name"). Understudy now escapes such selectors (`\#name`) before the test runs,
its check explains the rule when it can't, and the AI is told to write `id=name` in Robot Framework.

**Changed in 8.5.1:** the setup agent **never switches the AI model by itself** - when the same error comes back it
offers *Let <stronger model> look* as a button and only uses that model when the admin clicks it · **every box starts
collapsed** in Mission Control and the Studio (title and status badge only; open what you need, or *Expand all*) · Mission Control's sections (Telemetry, Users, Access & tools, Environments, Escalations ...) are a **menu on the
left** (with an *Icons only* mode; a scrollable row on phones).

**New in 8.5:** a **setup repair agent** - when a tool setup fails, Gemini no longer gives one guess and stops: it **stays on
the problem until the setup passes**, with the admin approving every change. It investigates with **read-only checks in
the tool's sandbox** (a dry run of the test - `robot --dryrun`, `py_compile`, `node --check` -, the packages really
installed, the real keyword / API signatures of the **installed** library version, runtime versions), **checks its own
fix before you see it** (a corrected test must pass Understudy's safety check and its dry run, otherwise the agent gets
the result and corrects itself), and shows the fix with the **complete test** and what it checked. **Approve** - Understudy
applies it, re-runs the setup and, if something else fails, the agent continues **with the full history** of what was
tried; **Not right - look again** with a hint; or **Stop**. When an error comes back after a fix, a **stronger model** is
offered (setting, default `gemini-pro-latest`; used only when the admin clicks it), and every fix that made a setup pass is **remembered as a
lesson** for future setups. Understudy's own certain fixes still come first; in *automatic* mode the agent applies
package-only fixes itself, a new test or another tool version always waits for the admin (Mission Control > Access &
tools > Self-healing > Setup agent).

**Fixed in 8.4.1:** Robot Framework tool setups stuck in a smoke-test loop ("Keyword 'Browser.Fill Text' expected 2 to 3
arguments, got 0", even after the AI's retries): the AI kept putting a keyword alone on a line with its arguments below
it, and every rewrite repeated that. Understudy now repairs that layout itself (and single-spaced or non-breaking-space
separators) before the test runs - in setup smoke tests and in generated suites - shows the admin what it changed,
rejects what it can't repair before running with the exact rule in the rewrite request, and the AI's prompt states the
Robot layout rules.

**New in 8.4:** a Mission Control **Environments** tab - see **every runtime version on the server** before deciding what a
tool needs: each Java (JDK), Python, Node.js, Ruby, Go, .NET SDK, Maven and Gradle install Understudy finds (PATH,
`/usr/lib/jvm`, pyenv, nvm, sdkman, Program Files ...), its exact version, path and source (installed on the server,
downloaded by Understudy, registered), which one is the default and which tools use it, plus the browsers · **several
versions side by side**: *Add* downloads another one (checksum verified) without changing what any tool uses, and a
**dropdown per tool** switches it to any installed version - no paths to type; the tool's smoke test re-runs right away ·
**Python downloads** (official standalone CPython 3.10-3.13 builds, SHA-256 verified), so a tool can run on Python 3.12
while Understudy stays on its own Python - the tool's isolated environment is rebuilt with the chosen interpreter ·
**version requirements per tool** ("Java 17+", "Python 3.9-3.12", "Node.js 18+") from Understudy's rules, the **AI stack
assistant** (recorded when a tool becomes ACTIVE, with the versions its smoke test passed with) or the admin; they're
**checked before every run** - a run that doesn't meet them is held with a one-click fix (switch to an installed version
that fits, or download one) that the user sends to the admin and the admin approves in Escalations, or the user can *Run
anyway*.

**New in 8.3:** **Understudy installs missing runtimes itself, guided by the AI** - "Install ruby and gem on the server
(Understudy can't download it)" is gone: **Let the AI set it up** picks the official, checksum-verified build for this OS
(Ruby + RubyGems + Bundler, Go, the .NET 8 SDK, Java, Node.js, Maven, Gradle, or an allow-listed conda-forge package),
downloads and registers it, starts every program once and explains any failure · **the smoke-test loops are fixed** -
WebdriverIO gets its runner packages (`@wdio/cli`, `@wdio/local-runner`, the Mocha adapter, the spec reporter) in the
same version, pytest / Robot Framework entry files are placed correctly, the Robot Framework Browser library is
initialised (`rfbrowser init`, or the pre-built *robotframework-browser-batteries* when npm fails there), the AI knows whether the sandbox really has no network, and a known failure is **fixed
automatically** (missing package, missing init step, wrong test file) before the admin is asked; options in the setup
chat now carry the real change, so "Add the missing packages" actually adds them · **escalate a run the AI couldn't
verify**: when Execute passed but Verify failed, the user explains and attaches evidence (screenshots, PDF, logs,
video); the admin confirms (Verify passes as *confirmed by the admin*, the ZIP unlocks with `VERIFIED_BY_ADMIN.md`),
rejects or asks a question - new **Escalations** tab in Mission Control · a **notification bell in the Studio** (plus
email) · **every box folds** in both portals, with **Collapse all / Expand all** · fix: **"type the username to confirm"**
when deleting a user works again (the field lost focus on every 4-second refresh) · **the AI proposes a fix for any failure, a human approves it** - in Mission Control the AI's fix for a failed setup is one click ("Apply the AI's fix"; admin setting *AI fixes for tool setups*, default: show first), and in the Studio self-healing can switch to another allowed version, fix the run command, remove stray project files or **send a request to the admin** (e.g. "allow Playwright 1.63.0" - one click in Mission Control) · fixes: a requested version was silently replaced by the backend's own (`packaging` missing), C# runs failing with MSB1011, Robot Framework smoke tests rejected for using subprocess · **the admin can fix runtime problems directly** - a *Fix it yourself* panel on runtime-help requests (allow a version, a run variable, a PATH folder, a host that skips the run proxy, the network mode, and re-run the user's session), *Run settings* on every approved tool and *Reached without the run proxy* hosts · fix: Selenium runs failing with "RemoteDisconnected" because Understudy's run proxy also caught localhost (ChromeDriver) · fix: identical tool cards in Step 1 that were all selected at once · an **eye button on every password
field** in the Studio (login, sign up, reset) and Mission Control to show / hide what you typed.

**New in 8.2:** **nothing is pre-built** - a fresh install has no active tool, not even Playwright · an **AI install
assistant** in Mission Control: type a tool (e.g. *Selenium*) and the AI lists the companion frameworks it needs
(WebDriver, TestNG, JUnit, Cucumber ...), the languages it comes in (Java, Python, JavaScript ...) and the package
managers per language (Maven, pip, npm/npx ...); you pick the language, tick the package managers your server has, choose
the components and a version for each from the **real registry lists** (Maven Central, PyPI, npm) · the **smoke test is
always written by the AI** for exactly that stack and **you review the code before it runs** (approve, or tell the AI
what to change) · **self-healing fixes Java "invalid target release: 17"** (Maven used an older JDK than the pom asked
for): the pom now targets the JDK Maven really uses, and a failed run points `JAVA_HOME` at a newer JDK on the machine,
compiles for the JDK that is there, or downloads a verified Temurin JDK · an **AI-advised runtime repair**
(`RUNTIME_REPAIR`) when the rules don't recognise a failure, and a **"tell the AI what's different on this machine"** box
on the retry card (e.g. *"JDK 17 is in C:\Program Files\Java\jdk-17"*) · **Cypress 15.10+/16** support -
`Cypress.env()` (removed in Cypress 16) is no longer used or accepted anywhere · **self-signup can be limited to company
email domains** (e.g. only `@companyname.com`, configurable in Mission Control > Users) · the "works offline" badge is gone
from the Studio's tool cards · **Gradle support** and a **Package managers & build tools** card: the admin switches each
package manager on or off for the assistant, sees what the server has, and downloads Gradle 8/9, Maven 3, Node.js or a
Temurin JDK (checksum verified) or registers an existing install - used for every install, smoke test and run.

**New in 8.1:** **email accounts** - every account has an email address (one email = one account) · **self-signup**
verified with a 6-digit code sent to that email · a **Profile** tab with an OTP **password reset** · **Forgot password**
on the login screen (code valid for 10 minutes) · the admin **never sees or sets a password** any more, and **Block** /
**Delete** need a reason that is **emailed to the user** (Unblock is emailed too) · mail through **any SMTP server -
free with a Gmail App Password or Brevo** (section 2.0.2), or a local outbox for development.

**New in 8.0:** a **zero-trust tool registry** - a fresh install trusts only Playwright, and an SOP that requires
any other tool or version **pauses the session** with an *Unsupported Framework* warning and an **Enablement Request**
that reaches Mission Control live (WebSocket) · a **human-in-the-loop AI tool synthesizer**: the AI proposes a setup
manifest (OS dependencies, package commands, environment variables, runner wrapper), the admin steers it in a chat
("use our Nexus", "use the local python3.10"), and when a step fails the setup **halts, shows the captured output and
asks the admin** (e.g. *download OpenJDK or give me JAVA_HOME?*) before it resumes · a **hybrid sandbox** - isolated
local processes by default, or ephemeral, network-isolated **Docker** containers · a **mandatory smoke test** (dummy page,
click, assert) before any tool becomes ACTIVE; a pass resumes every waiting session · an **IDE-style workspace explorer**
in the Studio (projects and sessions as a tree with status badges, quick add, Duplicate / Delete / View Execution Logs).

**New in 7.2:** a **Dark / Light theme switch** at the top of every page (Studio, Mission Control and both login
screens; remembered per browser) · when the admin allows **several versions** of a tool, the user picks one and exactly
that version is installed in the sandbox before the run (tested live, including after switching versions) · the SOP can
be **written in any language** - the rules and the two remedies for a payload that breaks them (**Fix payload** /
**Update SOP instead**) are tested in seven languages, and an SOP Understudy can't read never passes silently.

**New in 7.0:** users log in with accounts the admin creates in Mission Control · work is organised as
**User → Projects → Sessions** · Step 1 can add the **context of previous sessions** (search by name, or a whole
project) · every log line and telemetry event shows **`[User] | [Project Name] | [Session Name]`** · the tool
**version** the admin set / the user picked is now exactly what the sandbox installs and the ZIP ships (bug fix) ·
page discovery, grounding and the payload schema work for **any** site, not just the demo portals (section 3.0, 5.2).

---

# Part 2 - Technical change notes

**9.8.0** - Difficulty score, routing, agentic tools, key hand-off
* New `core/wizard/difficulty.py` (`PARAMETERS`, `evidence_values`, `compute`, `recommend`, `tool_lists`, `assess`,
  `recalculate`, `reset`, `apply_to_complexity`), prompts `AUTOMATION_DIFFICULTY` and `AUTOMATION_ROUTING`,
  `session._assess_difficulty` / `recalculate_difficulty`, `POST /sessions/{id}/difficulty/recalculate`.
* Catalog: `gemini-agent`, `browser-use`, `stagehand` (`category: agentic`). `core/wizard/agentic_templates.py`,
  `core/wizard/agent_engine.py`; `templates.render(context=)`, `build._agentic_suite`, `task_context` through
  `build_agent.generate`.
* `core/wizard/agent_keys.py`; `PUT/DELETE /sessions/{id}/agent-key`, `PUT .../agent-key/model`; `session.is_agentic`,
  `set_agent_key`, `clear_agent_key`; `executor.execute/run_framework(agent_env=)` with log redaction and the Gemini
  host on the egress allow-list; `public_view` adds `agent_key` (masked) and `agentic`.
* `security_alerts` ids are strictly increasing (two alerts in one millisecond sorted wrongly).
* UI: `app/wizard/difficulty.tsx` (DifficultyCard, RoutingCard, AgentKeyCard).
* Tests: `tests/test_v98_difficulty_agentic.py` (incl. a real agentic run on the demo ERP against
  `tests/fake_gemini.py`), tool-matrix cases for the three agentic tools; `scripts/generate_prompt_docs.py` documents
  the engine's prompts.

**9.7.2** - Windows runtime check and npm retries
* `environments.runtime_works`: `companion()` prefers `.cmd` / `.bat` / `.exe` on Windows (was the extension-less
  POSIX script, which `subprocess` can't start - every Windows Node.js / Ruby failed the check).
* `sandbox/local.py`: `_clear_unfinished_npm_tree()` removes `node_modules` and `package-lock.json` unless the
  `.understudy-npm-installed` marker (written after a complete install) exists; runs before the manifests are written.
* `sandbox/docker.py`: the npm install step removes `/env/isolated_env/node_modules` and `package-lock.json` first
  (it only runs when `/env` holds no completed install).
* Tests: `test_windows_node_folder_uses_npm_cmd`, `TestUnfinishedNpmInstall`.

**9.7.1** - Admin password generated once per installation
* New `core/wizard/admin_password.py`: `generate()` (`Us-` + 18 from an unambiguous alphabet + `-9`), `current()`
  (environment, then `.env`; empty = unset), `ensure()` (writes `.env` via `env_file.update`, sets the environment,
  remembers `GENERATED_THIS_START` for the banner), `reset()`, `startup()` (called in `server.py` after `.env` is loaded;
  no-op with `K_SERVICE`, `UNDERSTUDY_SKIP_DOTENV=1` or `UNDERSTUDY_ADMIN_PASSWORD_GENERATE=off`; ignores `OSError`).
* New `scripts/admin_password.py ensure|show|reset` and `scripts/ensure_admin_password.ps1` (for `deploy_docker.bat`,
  no Python needed on the host). `tests/conftest.py` turns generation off. `banner.py` shows the generated password
  once, or where it is saved. New `tests/test_admin_password.py`.

**9.4.0** - Page addresses read from the recording; several pages; automation-aware inspection; complexity for the build agent
* New prompt `WATCH_URLS` (`watch.pages_from_video`, run first in `session._watch_job`): the page addresses seen in the
  recording, in order, each with `video_time`, `evidence` and `purpose`. Addresses are completed (`complete_url`: no
  scheme gives https, or http for local hosts; a bare path is resolved against a known page) and screened like typed
  ones. Unusable ones go to `pages_rejected` (`kind`: blocked | partial | removed).
* The session keeps `pages` (`url`, `source: user | video`, `seen_in_video`, `video_time` ...), `pages_confirmed`,
  `pages_removed` and `pages_meta`; `target_url(s)` follow `pages`. `_merge_video_pages` keeps the recording's
  order, the typed start page first, and typed pages the AI did not see.
* `PUT /api/wizard/sessions/{id}/pages {urls, rewatch, note}` (`set_pages`) and
  `POST /api/wizard/sessions/{id}/pages/confirm`. Without `rewatch`, navigate steps follow the edit and the
  inspection and complexity are redone (`_pages_job`). With `rewatch`, WATCH runs again with the list and the note
  (`page_note` also goes to `WATCH_ANALYZE`). `build_suite` returns 409 (`needs_page_check`) while pages the AI found
  are unchecked. With AI, `start_session` accepts no URL.
* Step 1 takes a list of page addresses (`target_urls`, form field or JSON; `target_url` alone still works). Each is
  screened. A bad one is refused with `details.index`. The session keeps `target_urls`; `target_url` is page 1.
* WATCH reads every page (`watch.discover_pages`). The prompt gets numbered `TARGET URLS` and one inventory section per
  page. Navigate steps to any listed host survive normalisation. Offline, the other pages' forms are appended
  (`_merge_other_pages`).
* Runs get `TARGET_URL_<n>`, `TARGET_URLS` and `CYPRESS_TARGET_URL_<n>` (`executor.page_url_env`). The egress allow
  list and the pre-check cover every page. `scan_suite` allows every listed host. Templates turn a navigate step to page
  *n* into `${TARGET_URL_<n>}` (`templates.page_tokens`). The export lists the pages in README and `.env.example`.
* New `core/wizard/inspection.py` and prompt `INSPECTION_PLAN`: plan the checks from the steps, then inspect each page
  once, then the new prompt `INSPECTION_RELOAD` decides from that first-load evidence how many reloads each page gets
  (0-3) and `verify_loads` for the locator check (1-3); `rule_reloads` is the offline fallback. Findings are marked
  `relevant`. Self-healing re-inspects with the same plan and tells the agent what was learned (`prior`).
* `page_probe.profile_from_snaps` (one or more loads), `generated_looking`, `static_problems`, `is_positional`.
  `verify_locators(urls, locators, loads=<int | {page: n}>, profile=...)` uses the agent's `verify_loads` per page (a
  locator may carry `page`).
* `complexity.baseline` scores pages, sites, sign-in-first, missing elements, new tabs and dialogs.
  `complexity.for_prompt` goes to `BUILD_GENERATE_SUITE` (new inputs `complexity` and `pages`), for build and heal.
* UI: the page-address list in Step 1; *What Understudy checked* in the complexity card; the build trace shows the
  level the code was written for.
* New `tests/test_v94_multi_url_inspection.py`.

**9.3.0** - Escalation fixes for everyone or for one user
* `admin_policy.user_overrides` stores packages, versions, run variables and PATH folders per user and tool. It has
  `add_user_override` (validated like the tool-wide change), `remove_user_override` and `all_user_overrides`.
* `allowed_spec`, `tool_version_policy` and `tool_runtime` take an optional `user`. The session passes its owner, so
  that user's tool spec, allowed versions and run environment include their per-user fixes
  (`session._user_runtime_env`).
* `POST /api/admin/escalations/{id}/decide` accepts `scope: everyone | user`; the escalation records `scope`. New
  endpoints: `GET /api/admin/user-overrides` and `POST /api/admin/user-overrides/remove`.
* UI: an "Apply this fix to" choice in the escalation; the **Fixes for single users** card.
* New `tests/test_v93_escalation_scope.py`.

**9.2.0** - Studio layout
* `ui.tsx`: a box without an explicit `defaultOpen` follows the portal: open in the Studio (`/wizard`, storage key
  `understudy.fold3.`), collapsed in Mission Control (`understudy.fold2.`).
* Projects home and project page: the main action sits next to the title, projects and sessions show as a card grid,
  and an empty project gets a centred "Start your first session" panel. The explorer has labelled **New session** /
  **New project** buttons.
* Step 1's step list truncates long selectors (shown in full on hover).

**9.1.0** - Sessions are named by the AI
* `POST /api/projects/{id}/sessions` takes no name; the session is "New session" (made unique) with `name_auto: true`.
* After WATCH, `session._auto_name` asks the new prompt `SESSION_TITLE` for a 2-6 word title, with the process,
  summary, steps, site, page title, SOP excerpt and the project's other names. Without AI it falls back to the
  process name. The result is screened, made unique in the project, logged in telemetry and indexed.
* `PUT /api/wizard/sessions/{id}/name` renames a session and sets `name_auto: false`, so the AI never renames it
  again. With user login on, `/api/wizard/sessions` no longer needs a session name.
* UI: the New session button replaces the name form and dialog; the breadcrumb title is editable in place (with
  "naming..." while Step 1 runs); the explorer has **Rename...**; AI-named sessions get a sparkle in the project list.
* New `tests/test_v91_session_names.py`.

**9.0.0** - Export-only Step 5, agentic build and self-healing, dynamic pages, complexity summary, per-tool access
* **Step 5 = EXPORT** (`session.export`, `POST /sessions/{id}/export`; `/verify` is kept as an alias). The pillars are
  WATCH, BUILD, GOVERN, BUILD-EXECUTE, EXPORT. A passing run ends with `_finish_export`.
  * Removed: the before/after snapshots, `verification.json`, the screenshots and the verify escalation.
  * Removed with the snapshots: the duplicate-record guard. A run that saved a record and then failed is healed and run
    again, which can save the record a second time.
* **Page probe** (`core/wizard/page_probe.py`): two headless Chromium loads; per element the visible label, which
  attributes are stable, ranked locator candidates and a label-anchored XPath verified in the page; page signals
  (dynamic ids or names, shuffled fields, iframes, shadow DOM, framework, CAPTCHA, ...). `verify_locators` checks
  locators on fresh loads. `ground_steps` re-anchors WATCH selectors by label. `plan_from_profile` rebuilds the offline
  plan.
* **Build agent** (`core/wizard/build_agent.py`): generate, then the gates, then locator verification, then feedback,
  for up to `UNDERSTUDY_BUILD_AGENT_ROUNDS` (3) rounds. The prompt (`BUILD_GENERATE_SUITE`) gets PAGE PROFILE and AGENT
  FEEDBACK and returns the `locators` it used. Healing uses the same loop with `failure_feedback`.
  `UNDERSTUDY_PAGE_PROBE=off` disables probing.
* **Complexity** (`core/wizard/complexity.py`, new prompt `AUTOMATION_COMPLEXITY`): a deterministic score plus the AI's
  explanation and tool fit, shown in Step 1.
* **Tool access** (`admin_policy.set_tool_access` / `user_may_use` / `delete_tool`): checked in `/frameworks`,
  `annotate_tools`, `select_framework` and dependency set-up.
* **Selenium template** accepts XPath selectors and finds fields by a label placed next to them.
* **Portal 3** (`/portal3`): an rpachallenge-style form whose field order, ids and names change on every load.
* New `tests/test_v90_agentic.py`; the verify-based tests were updated.

**8.1.1** - Email uniqueness across instances, signup off by default on Cloud Run
* **Atomic email claims** (`users._claim_email`, `store.create`): one claim document per address, created only if
  absent (O_EXCL file locally, Firestore `DocumentReference.create` on Cloud Run); new usernames are created the same
  way. Stale claims (an instance died before creating the account) are reclaimed after 2 minutes; delete releases the
  address; `backfill_email_claims()` runs at start-up for older accounts; `find_by_email` is now a direct lookup.
* **`deploy_cloudrun.sh` sets `UNDERSTUDY_SIGNUP=off`** unless you opt in with `UNDERSTUDY_SIGNUP=on`; the Mission
  Control switch still wins.
* New `tests/test_email_uniqueness.py`: 8 simulated instances racing for one address (local store and Firestore),
  two signups verified at the same moment, duplicate usernames, stale-claim recovery, backfill, the deploy default -
  and the race test fails when the claims are switched off.

**8.1.0** - Email accounts, self-signup, OTP password resets, admin Block / Delete with mailed reasons
* **Email on every account** (`users.py`): required when the admin creates a user, unique (case-insensitive); login
  with username or email. Accounts created before 8.1 get their email once from Mission Control (**Add email**).
* **Self-signup** (`POST /api/auth/signup` → code → `/signup/verify`): the account is created only after the emailed
  code; **Allow self-signup** switch in Mission Control > Users (`UNDERSTUDY_SIGNUP=on|off` is the default).
* **OTP service** (`core/wizard/otp.py`): 6-digit codes, HMAC-stored, 10 minutes, single use, 5 tries, one-time
  tickets for the password step, per-purpose cooldown + hourly caps, durable (Firestore on Cloud Run).
* **Profile tab** (username, email, account, last password change) with **Reset password** by code, and **Forgot
  password** on the login screen (email checked → code → new + confirm password). Both end every session and mail a
  "password changed" notice.
* **Admin changes:** no password is ever shown or set by the admin (generated passwords are gone; an account can be
  created without one and the user sets their own); **Ask to reset** mails a notice; **Block** (reason) replaces
  Disable, **Unblock**, and **Delete** now needs a reason - all three mail the user (block / delete include the admin
  contact). `PUT /users/{u}/password` and `/active` answer 410.
* **Mailer** (`core/wizard/mailer.py`, standard library only): any SMTP server (free: Gmail App Password or Brevo),
  outbox mode for development (`scripts/mail_outbox.py`), Mission Control status + test mail, start-up
  banner line, Docker and Cloud Run (Secret Manager: `UNDERSTUDY_SMTP_PASSWORD_SECRET`) wiring.
* New `tests/test_email_accounts.py` (incl. a real SMTP round trip) and `scripts/check_accounts_ui.py` (browser check
  of every flow); `tests/test_users_projects_history.py` updated for the new rules.

**8.0.0** - Zero-trust tool registry, HITL AI tool synthesizer, hybrid sandbox, workspace explorer
* **Zero-trust tool registry.** A fresh install trusts only Playwright (Python), pinned to the installed version
  (`UNDERSTUDY_DEFAULT_TOOLS`; existing policies keep their tools). Statuses ACTIVE / REQUESTED / PROPOSED / INSTALLING /
  SMOKE_TESTING / NEEDS_ADMIN / FAILED / REJECTED (`GET /api/admin/tool-registry`).
* **The SOP decides the tool.** WATCH reports `sop_tool_requirement`; `tool_requirements.detect` backs it with the SOP
  text. An inactive tool or version shows *Unsupported Framework* in Steps 1 and 2; an **Enablement Request** sets the
  session to `pending_admin_approval` (Build / Execute 409) and notifies Mission Control. Approval resumes every waiting
  session automatically; rejection releases them. The SOP's version is carried into the request and resolved against
  the full registry (`13.6` → newest `13.6.x`).
* **HITL AI tool synthesizer** (`core/wizard/tool_setup.py`). Phase A: manifest (OS dependencies, package commands,
  environment variables, interpreter, Docker image, generated runner wrapper). Phase B: admin instructions in a chat
  (package index / Nexus, local folder, local interpreter via `sources.interpreter`). Phase C: install → smoke test;
  any failure halts with the captured output and a question (`TOOL_SETUP_CLARIFY`), the answer is applied
  (`TOOL_SETUP_RESOLVE`, validated) and the setup resumes; up to 8 rounds. Checksum-verified Temurin JDK and Node.js
  downloads (`core/wizard/runtimes.py`). "Add without a verified install" was removed (409).
* **Hybrid sandbox** (`core/wizard/sandbox/`): `BaseSandboxRunner` (`setup_environment`, `install_dependencies`,
  `execute_script`, `teardown`), `LocalProcessSandboxRunner` (isolated env folders, allow-listed environment,
  time-outs, process-tree memory watchdog) and `DockerSandboxRunner` (Engine API, ephemeral containers, capabilities
  dropped, network disconnected before the script runs, volumes), chosen by a factory from the admin setting (local by
  default). Automation runs (`executor.run_framework`) use the same runner.
* **Mandatory smoke test** (`TOOL_SMOKE_TEST`, templates for the 10 catalog tools, file checks): exit 0 + marker makes
  the tool ACTIVE; **Re-run smoke test** in the registry.
* **Live events**: WebSockets `/api/admin/ws` and `/api/wizard/ws` with polling fallbacks (`/api/admin/inbox`,
  `/api/wizard/events`); Mission Control inbox bell and toasts; the Studio updates badges, banners and sessions live.
* **Workspace explorer** in the Studio: projects and sessions as an accessible tree with badges (DRAFT, ANALYSED,
  BUILT, GOVERNED, RUNNING, PENDING, FAILED, VERIFIED), quick add, context menu with Duplicate
  (`POST …/sessions/{id}/duplicate`), Delete and View Execution Logs (`GET …/sessions/{id}/execution-logs`).
* **Includes the tool-setup request of 7.2b** (empty-by-default tool list, requests for tools that are neither allowed
  nor blocked, an AI setup plan the admin can steer - "download from this directory" - and the AI creating the tool
  setup with configurable versions), now completed with the registry, sandbox, clarification loop and smoke test.
* **Fixes found while testing 8.0:** explorer badges were DRAFT for sessions indexed before 8.0 (derived now), and the
  open session's badge didn't follow its progress (it does now: ANALYSED → BUILT → GOVERNED → VERIFIED); the
  clarification question quoted npm's "complete log" line instead of the real error; a repository answering 401/403 now
  gets its own question (another repository, a proxy, or credentials set on the server - never typed in the chat); a
  version typed with a tool name was ignored; "Continue to Build" stayed clickable while the session waited for the
  admin; after the admin decided, Step 1 showed no reason - it now shows the decision and the admin's note, and the
  requested tool stays among the first cards; an SOP quote ending in a full stop was shown with two.
* 438 tests (+ the legacy suites); the orchestration layer `core/five_pillar_pipeline.py` is unchanged.

**7.2.0** - Theme switch, version choice and SOPs in any language
* **Dark / Light theme switch** at the top of every page and on both login screens (`app/theme-toggle.tsx`); the light
  palette mirrors each Tailwind colour scale, so every component keeps its contrast; stored per browser, applied
  before first paint.
* **Several allowed versions → the user's pick is installed.** New `tests/test_version_choice.py` (incl. a live
  install of two `requests` versions). **Fix:** a slow install of a previously picked version could finish last and
  overwrite the new version's status with FAILED; every install now carries an id and a superseded result is dropped.
* **SOP in any language.** The prompt for `GOVERN_EXTRACT_RULES` says so; the offline parser reads the common rule
  phrases of 6 more languages (es, fr, de, pt, it, hi; Spanish date ranges also with "entre el ... y el ..."),
  any-script digits, European amounts and `dd.mm.yyyy` dates, and reports the detected language; an unreadable SOP is
  a blocking `SOP-NOT-UNDERSTOOD` problem. New
  `tests/test_sop_violation_remedies.py` covers **Fix payload** and **Update SOP instead** in seven languages. **Fix:**
  "Update SOP instead" opened the SOP editor collapsed.
* **Fixes found while recording the demo video:** the before/after screenshots in Step 5 did not load with login on
  (the cache-buster was appended after the access token); the Step 4 banner said "Fixed and verified - self-healing
  made it work" even when no healing was needed; run logs were low-contrast in the light theme; Mission Control said
  "No AI key" while a labelled demo stand-in model was answering.
* **Google Cloud Model Armor from Mission Control** (section 2.5.1): on/off, project / location / template, fail-open
  or fail-closed, connection test with the exact fix, template creation, activity counters, part of "Check Google
  Cloud set-up"; stored in the admin policy and overriding `UNDERSTUDY_MODEL_ARMOR_TEMPLATE`. `--model-armor` for
  `scripts/gcp_setup.sh` / `.ps1` (`-ModelArmor`) and `deploy_cloudrun.sh`, and `scripts/model_armor_setup.sh`.
  New `tests/test_model_armor_admin.py`.
* 394 tests (+ the legacy suites); the orchestration layer `core/five_pillar_pipeline.py` is unchanged.

**7.1.0** - Tool matrix and an auditable AI code-health
* **Sample test case for every configured tool** (`scripts/tool_matrix.py`, `tests/test_tool_matrix.py`): admin-pinned
  version → user's tool → manifest, sandbox plan and ZIP all carry exactly that version; `--live` /
  `UNDERSTUDY_LIVE_TOOL_TESTS=1` installs for real, checks the installed version, runs the suite with the tool's own
  runtime against Portal 1, verifies the ERP record and the ZIP. Results in `docs/TOOL_MATRIX.md`.
* **AI code changes are visible and revertible**: Mission Control says whether the AI changed code (and whether it did
  it alone via auto-apply), shows the lines before/after with line numbers and the diff, why (diagnosis), the
  verification, the backup location (`CHANGE.md` beside the original), live status (in place / reverted / edited
  since), restart-needed, **Revert** and **Revert anyway** (keeps the edited file); `scripts/code_health.py` does the
  same from a terminal when the backend won't start.
**7.0.0** - Users, projects, history context, one dependency truth, any website
* **Studio users** - Mission Control > Users creates accounts (username + password, or a generated one shown once);
  reset, disable, delete; the Studio and all its APIs require a login (`UNDERSTUDY_USER_LOGIN=required`). Signed
  12-hour tokens, salted PBKDF2 hashes, lockout, revocation shared across instances.
* **User → Projects → Sessions** - projects (name + description) and named sessions (one full pipeline run each),
  owner-scoped, with status, rename, delete; breadcrumb navigation and refresh-safe routing in the Studio.
* **Context of previous sessions in WATCH** - search by name, add sessions or whole projects; value-free, Model-Armor
  screened digests go to `WATCH_ANALYZE`/`WATCH_REFINE` (new `history_context` variable and rules, new output
  `history_applied`); offline re-use of proven selectors and parameter names.
* **Run context on every log** - `[User] | [Project Name] | [Session Name]` on execution/dependency log lines,
  telemetry events (with user/project filters) and the legacy telemetry stream; stripped before AI prompts.
* **Bug fix: tool versions and dependencies** - the AI no longer writes manifests; the pinned version is what the
  sandbox installs, the run declares and the ZIP ships; trusted extra packages the code imports are added; the sandbox
  is verified/repaired before every run; a no-longer-allowed version stops the run; gem installs pinned; ZIP pins the
  installed versions and adds `config/dependency_lock.json`.
* **Generalised for any public URL** - richer discovery (links, ARIA widgets, radios, editors, forms, constraints,
  same-origin fetch/axios/jQuery calls, automatic rendering of JS-built pages, ranked bounded inventory), domain-free
  WATCH prompts with grounding/multi-page/payload-schema rules, deterministic grounding of every step on the live page,
  and a payload schema derived from the page (Step 2 validation, JSON Schema in the ZIP).
**6.3.0** - Self-healing with the user in the loop
* After a failure the AI **proposes** a fix instead of re-running at once: Step 4/5 show a **Review the fix** card with
  *what went wrong* (failing step + error), the AI's diagnosis, *what it changed* (step-by-step selector/wait changes,
  instructions for the code generator, or the runtime repair it plans) and the **diff of the regenerated script**.
* **Approve & retry** runs exactly the reviewed code (it is not regenerated), after the SOP rules are re-checked.
* **Ask the AI to revise**: plain-English corrections (Model Armor screened, fenced as untrusted data in
  `VERIFY_SELF_HEAL`) make the AI re-diagnose with every correction so far; the AI can ask **one clarifying question**
  when it is unsure (`question_for_user`) and reports how it applied the correction (`user_feedback_applied`). Offline,
  "step N should use <selector>" and "wait N seconds after submit" are applied by the built-in healer.
* Admin switch **Ask the user before every retry** (default) / **Retry automatically**; API
  `PUT /api/admin/self-heal-approval`, `POST /api/wizard/sessions/{id}/heal/approve|revise`.
**6.2.0** - Admin URL policy, restricted network, UX and smarter recall
* **Blocked URL list** in Mission Control, mirroring the allowed list (wildcards `*.example.com`, reasons, audit
  events). **A link can never be on both lists** - enforced in the API (HTTP 400) and the UI (conflict message,
  disabled button), including wildcard overlaps. Blocked beats allowed and every other exception.
* **Restricted network for automation runs** (closes the sandbox gap): each run goes through a per-run egress proxy; in
  *restricted* mode (default) it may reach only its target and the allowed list; blocked hosts, cloud metadata and
  private ranges are refused in both modes. Admin switch *Network for automation runs*, live-log lines, a Step 4
  "Network policy" summary and `execution.egress` in the result. A target blocked after Step 1 stops the run
  (no pointless self-healing retries).
* **Recall understands paraphrases**: soft matching (stemming, a vetted business-synonym table, near-spellings) for
  form fields, parameter names and SOP wording, so `supplier_name` finds the skill learned for `vendor_name`. The
  safety gates are unchanged. Step 1 explains **"Why did this match?"**.
* **Optional Google Cloud Model Armor** stage (`UNDERSTUDY_MODEL_ARMOR_TEMPLATE`) in front of the built-in stages.
* **Accessibility and density**: contrast raised (no more slate-500/600 text), step navigation exposes `aria-current`
  and labels, live regions for alerts and the run log (`role="log"`), labelled form controls, collapsible "What happens
  in this step", dependency details and "Update the SOP", and a clear "N values need your input" banner with
  highlighted fields when a new recording was answered offline.
**6.1.0** - Learned skills work offline
* **Offline recall of learned AI answers** (section 5.1): validated Gemini answers are remembered and re-used for the
  same or similar problems when AI is unavailable, labelled `LEARNED_SKILL`, adapted to the new inputs and re-checked
  by every gate; per-step matching rules; Firestore-shared on Cloud Run.
* Mission Control: **Learned AI answers** panel (list, inspect, delete, forget all), `RECALL` telemetry events, and the
  Understudy header shows how many learned answers are available.
* The demo ERP portals no longer need the internet: Tailwind CSS is compiled into `erp/static/portal.css` and the
  fonts (SIL OFL) are bundled, served at `/erp-static/` (also proxied by the UI for single-URL deployments).
* Fix: a value such as `450.0` no longer leaves `{total_amount}0` in step evidence when the video shows `450.00`.
* Telemetry and the UI never show a Gemini model name for answers that came from a test/stand-in backend.
* Demo video kit: `docs/DEMO_VIDEO.md` (narration, honesty note, how to re-record with real Gemini), `scripts/demo/` (a
  clearly labelled stand-in model for recording on machines without internet) and `assets/demo_ap_recording_globex.mp4`.

**6.0.1** - Google Cloud set-up made easy
* **Step-by-step Google Cloud guide** (this README): API key, Vertex AI, Firestore, Cloud Storage, Docker, Cloud Run,
  troubleshooting.
* **`scripts/gcp_setup.sh` / `.ps1` / `.bat`**: one command connects a native or Docker install to a Google Cloud
  project (APIs, credentials, Firestore, bucket, roles, `.env`) and checks it for real.
* **Google Cloud check** everywhere: `scripts/gcp_check.py` (`--quick` at every start in `run_demo.*`), Docker
  (`docker compose exec understudy-backend python scripts/gcp_check.py`) and a **Check Google Cloud set-up** button
  in Mission Control (`POST /api/admin/gcp/check`). Each failure names the command that fixes it.
* **Fix: settings in `.env` were ignored** except the Gemini key. The backend now reads the whole `.env` at start
  (shell/Docker values still win).
* **Docker + Google Cloud**: the images include the Firestore/Cloud Storage clients; `deploy_docker.*` passes the
  Google Cloud settings and mounts your credentials read-only (`docker-compose.gcp.yml`) when they are switched on.
* **Firestore unavailable no longer breaks Mission Control**: if Firestore is switched on but can't be used (no
  credentials, ...), Understudy falls back to local files, says why (banner, Mission Control, check) and retries
  every minute.
* **Vertex AI model fallback**: if the selected model isn't offered by Vertex AI in the region, `gemini-2.5-flash`
  (configurable) is used instead of failing; `UNDERSTUDY_VERTEX_MODEL` / `UNDERSTUDY_VERTEX_LOCATION` added.
* Setup scripts install `requirements-gcp.txt` and create `.env` from `.env.example`; Node.js 20.9+ is required.

**6.0.0** - Google Cloud native, durable, measured
* **Gemini through Google's current Gen AI SDK (`google-genai`)**, on **Vertex AI** (service account / ADC, no API
  key) or AI Studio. The deprecated `google-generativeai` SDK is only used by the unchanged legacy modules (its
  FutureWarning is silenced at start-up) or when forced with `UNDERSTUDY_GEMINI_SDK=legacy`.
* **Recordings are uploaded once** and reused by WATCH, every correction and every self-heal (Files API handle on AI
  Studio; `gs://` object or inline bytes on Vertex AI). Mission Control shows uploads vs. reuses.
* **Model Armor's semantic stage uses the prompt registry** (`MODEL_ARMOR_CLASSIFIER`), so its prompt, response and
  fallbacks are in telemetry like every other agent.
* **Durable, shareable state** (`core/wizard/store.py`): sessions are saved (without secret values) and restored after
  a restart or on another instance - a job that was running is reported as interrupted, secrets are asked for again.
  With `UNDERSTUDY_STORE=firestore` the admin policy, learned skills, telemetry and admin logins are shared by every
  Cloud Run instance.
* **Stateless signed admin tokens** (HMAC), shared per Cloud Run revision; a new deployment still falls back to the
  default password.
* **Cloud Run deployment**: `deploy_cloudrun.sh`, `cloudbuild.yaml`, `Dockerfile.cloudrun` - one container and one
  public URL (Next.js proxies `/api` and the portals; uploads up to the backend limit), health probes (`/healthz`),
  CPU always allocated for background automations, session affinity, least-privilege service account.
* **No duplicate transactions** from automatic retries: the idempotency guard re-checks the target before any retry
  and never re-submits once a new record has appeared.
* **Benchmark** (`scripts/benchmark.py`) with measured pass rates, healing, duplicates and Model Armor rates - and the
  fixes it led to (Portal 2 recordings, not-displayed values, offline Model Armor patterns).
* **Moment-by-moment WATCH**: each step can carry the time in the recording and what was seen there; the UI plays the
  recording from that moment.
* **Less to read, easier to use**: three tool cards first (selected, recommended, allowed) with "show more", skip
  link, visible keyboard focus everywhere, tab semantics in Mission Control, higher contrast small text, reduced motion.
* **One test command**: `python -m pytest` runs every Understudy suite and the orchestration spec; the original v4.x
  suites moved to `tests/legacy/` with a server fixture and documented expected failures.
* Dockerfiles use Node.js 24 LTS (Node 20 reached end-of-life in April 2026).

**5.4.0**
* **Installed versions are visible.** Every install, including cached and already-satisfied ones, logs
  `Version in use: <package> <version> (requested …; isolated/shared environment)`. The set-up card shows the version
  badges, and the ZIP records them.
* **Fix: a pinned Playwright version downloads its own browser build.** The browser download now runs with the tool's
  isolated Python instead of the backend's, so the browser matches the chosen version.
* **Show/hide password** (eye button) on the Mission Control login and account forms.
* **Code health covers runtimes and dependencies.** It checks Python and Node.js end-of-life, npm `EBADENGINE` and
  `deprecated` warnings from the lockfile, and `pip check`, and gives upgrade advice (AI with a key, built-in steps
  without). New prompt `ENV_UPGRADE_ADVICE`. The start-up banner shows the runtime versions and their end-of-life
  dates.

**5.3.0**
* **Automatic self-healing.** Understudy no longer asks "Retry with self-healing?". It fixes itself and re-runs until
  the automation works or the admin's limit is reached (default 5, 1–50; "unlimited" is gone because the loop is now
  unattended). There is a **Stop** button. It asks only when the tool's runtime still can't start, and then the user
  chooses between retrying with their tool and using the built-in driver.
* **Correct the AI's understanding.** After Watch, users explain what the AI missed. The explanation passes Model Armor,
  and a new **WATCH_REFINE** prompt re-watches the recording with every correction so far. Users can repeat this until
  they confirm, and it works offline for `Field: value` corrections.
* **Learned skills.** Every error becomes a lesson in a `SKILL.md` file on the machine running Understudy. When
  self-healing fixes it, the fix is saved and applied automatically before the next run with the same tool or target,
  and the skills ship in the ZIP. There's a new Mission Control tab for them.
* **Any tool version when unrestricted.** If the admin set no version restriction, users can pick any stable version
  from the package registry. Versions that don't exist are refused.
* **Password-protected Mission Control.** The default login is `admin` / `Understudy@2026`, printed by every start
  script. Admins can change the password; the change lasts until restart and then falls back to the default. The
  Gemini key, model and security alerts are admin-only.
* **Code health.** The AI self-heals start-up warnings such as the `google.generativeai` FutureWarning. Fixes are
  verified on a copy of the code, then applied with approval or automatically, with a backup and rollback. The
  orchestration layer is protected. This adds a new **CODE_HEALTH_FIX** prompt.

**5.2.0**
* **Detailed Mission Control telemetry.** It records every user action, the exact prompt sent to Gemini (system and
  user prompt, model, masked key, attached recording), the raw response and what was accepted, AI errors with a
  plain-English reason, and every fallback to local code with what that code produced. There are filters, a session
  picker, search, expandable events, a telemetry API (`/api/admin/telemetry`) and a JSONL file. Keys are never
  recorded.
* **Telemetry no longer jumps to the bottom.** It follows new events only while you're at the bottom, with a
  "N new events" button and Pause.
* **Selenium heals itself instead of switching to Playwright.** On a runtime failure, Retry repairs the chosen tool
  (installed Chrome/Edge/Chromium, stale driver hidden, browser download, clean reinstall). The built-in driver runs
  only when you choose it. Selenium's generated code now supports `SELENIUM_BROWSER`, `CHROME_BINARY` and
  `EDGE_BINARY`. A tool that can't be installed fails with the reason instead of being silently replaced.
* **Fix for the Windows Selenium error** (`The browser path is not a valid file` / `Unable to obtain driver for
  chrome`). The automation was started with a minimal environment that lacked `PROGRAMFILES` and `LOCALAPPDATA`, so
  Selenium Manager couldn't find the installed Chrome. Child processes now get the full environment minus secrets.
* **Fix: tools reported "toolchain missing" on Windows.** `npx`, `npm` and `mvn` are `.cmd` launchers that can't be
  started by name without a shell (`[WinError 2]`). They're now resolved to their full path. Genuinely missing
  toolchains name the binary and what to install, and Mission Control shows it per tool.
* **Fix: "No Gemini API key is configured" when a key was set.** Watch now shows the real reason the AI result wasn't
  used, such as an invalid key, quota, model not available, or an unusable answer. AI output validation is tolerant:
  invalid parts are dropped instead of discarding the whole answer. The default model lookup no longer runs on every
  call.
* **Tool versions.** Admins choose the allowed versions and a default for each tool from the live package registry.
  Users can pick among the allowed versions.
* **Mission Control redesign.** Header with AI status, stat tiles, and Telemetry / Access & tools / AI & security tabs.
* **Current start scripts.** `run_demo.*`, `setup.*`, `deploy_docker.*`, `entrypoint.sh` and the backend banner show
  v5.2 and the Understudy and Mission Control URLs, with no Studio/Supervisor. Setup re-installs dependencies and
  rebuilds the UI once after an upgrade. The launchers check toolchains. The Docker backend includes Node.js, with
  optional Java.
* The obsolete `UNDERSTUDY_APP_ARCHITECTURE_AI_PROMPTS_EXPLAINED.txt` is removed. `ARCHITECTURE.md` is the single
  reference, including every prompt.

**5.1.1**
* Fix: when a tool's runtime can't start (for example Selenium with no usable Chrome or driver: `NoSuchDriverException`
  or "browser path is not a valid file"), a retry now switches to the built-in Playwright driver. Before, in AI mode the
  self-healer diagnosed ENVIRONMENT but re-ran the same broken runtime. Crashes before step 1 are now also classified as
  environment problems.
* Self-healing retries are unlimited by default, with an optional limit in Mission Control.
* New `test_gemini_key_propagation` integration test covering the Mission Control key and model.

**5.1.0**
* The User Studio (`/user`) and Supervisor (`/supervisor`) pages are removed. Mission Control is now the admin console
  and the Supervisor HITL queue panel is gone. Exhausted self-healing now stops the run without a ZIP instead of
  escalating.
* The product is renamed from "Automation Wizard" to **Understudy**.
* New admin **target-URL exception list**, with Portal 1 and Portal 2 by default. Users see approved targets as
  one-click buttons.
* New admin **tool whitelist and access requests**. Admin approval now covers a tool's packages; the old per-user
  package approval is removed.
* New `/api/admin` API with optional `UNDERSTUDY_ADMIN_TOKEN`, and a new `test_admin_policy` suite.

**5.0.0**: the 5-step flow on top of the unchanged orchestration layer, Model Armor on every input, dynamic
dependency resolution, self-healing and ZIP export.
