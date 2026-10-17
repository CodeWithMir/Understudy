"""Understudy - the 5-step Watch / Build / Govern / Execute / Export flow and its admin console (Mission Control).

Every step is recorded in the pipeline contract (``core/pipeline_contract.py``): the pillars WATCH, BUILD, GOVERN,
BUILD-EXECUTE, EXPORT with strict gating, ``PillarResult`` objects, the run report, the console telemetry line and
the ZIP registry. ARCHITECTURE.md describes every module; the main ones:

    session             - the wizard state machine (jobs, pillars, retries, export), recorded in the pipeline contract
    api / admin_api /   - FastAPI routers: /api/wizard, /api/admin, /api/auth + /api/projects + /api/history,
    user_api / events     live events (/api/admin/ws, /api/wizard/ws)
    watch               - Step 1: recording + pages + SOP analysis (WATCH_URLS, WATCH_ANALYZE, WATCH_REFINE)
    inspection, page_probe, complexity - automation-aware live-page inspection and the complexity summary (9.0, 9.4)
    build, build_agent, templates      - Step 2: payload table, agentic code generation, offline templates
    governance          - Step 3: SOP rules, compliance, generated-script safety scan
    executor, egress    - Step 4: payload injection, the run with the tool's own runtime, restricted network
    verifier, runtime_repair           - self-healing of a failed run (steps / code, and the tool's runtime)
    exporter            - Step 5: the ZIP
    prompts, llm, gemini, api_keys, recall, skills - the prompt registry, the Gemini JSON client and transport
                          (google-genai on Vertex AI / AI Studio), key and model selection, learned answers / skills
    guardrails, cloud_armor, security_alerts - Model Armor on every input (+ optional Google Cloud Model Armor) and
                          the interception feed
    frameworks, dependency_resolver, dependency_lock, tool_* , setup_agent, setup_doctor, sandbox/, package_managers,
    runtimes, runtime_advisor, jdk, environments - tools, versions, installs, smoke tests and runtimes
    admin_policy, admin_auth, users, otp, mailer, projects, history, escalations, notifications, activity_log,
    code_health, env_health, gcp_check, store - admin policy, accounts, telemetry and durable state
"""

WIZARD_VERSION = "9.9.1"
