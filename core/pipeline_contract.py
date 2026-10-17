"""The pipeline contract every Understudy session is recorded in.

Five pillars, strict order, explicit PASS / FAIL per pillar, and a ZIP only when all five pass:

    WATCH -> BUILD -> GOVERN -> BUILD-EXECUTE -> EXPORT

* ``PillarResult``      - one pillar's verdict (status, plain-English reason, evidence); stored per session.
* ``PipelineReport``    - the run-level summary (passed / failed pillar / ZIP availability), kept in
                          ``PIPELINE_RUN_REGISTRY`` and re-synced whenever a pillar changes.
* ``GENERATED_ZIP_CACHE`` - the exported ZIP per session (``GET /api/wizard/sessions/{id}/export``).
* ``emit_pipeline_telemetry`` - one console line per agent action, tagged ACTUAL_AI_API / LOCAL_HEURISTIC; the
                          detailed record goes to Mission Control's telemetry (``core/wizard/activity_log.py``).

9.5.0: extracted from the v4.4 orchestration module (``core/five_pillar_pipeline.py``), whose own run path and the
helper modules it loaded (and with them the deprecated ``google-generativeai`` SDK) were no longer used and were
removed. The field names and the gating semantics are unchanged.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


@dataclass
class PillarResult:
    pillar: str  # "WATCH", "BUILD", "GOVERN", "BUILD-EXECUTE", "EXPORT"
    status: Literal["PASS", "FAIL"]
    reason: str
    timestamp: str = field(default_factory=_now)
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PipelineReport:
    run_id: str
    engine_target: str
    target_portal_url: str
    overall_status: Literal["PASS", "FAIL"]
    passed: bool
    failed_pillar: Optional[str] = None
    failure_reason: Optional[str] = None
    pillars: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    zip_available: bool = False
    zip_filename: Optional[str] = None
    zip_download_url: Optional[str] = None
    retry_count: int = 0
    max_retries_allowed: int = 2
    governing_policy_version: str = "understudy"
    retry_history: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


PIPELINE_RUN_REGISTRY: Dict[str, PipelineReport] = {}
GENERATED_ZIP_CACHE: Dict[str, bytes] = {}


def emit_pipeline_telemetry(
    run_id: str,
    stage: str,
    agent_source: Literal["ACTUAL_AI_API", "LOCAL_HEURISTIC"],
    action_description: str,
    detail: str = "",
    extra: Optional[Dict[str, Any]] = None,
    agent_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Prints one tagged line per agent action to the backend console and returns it as a structured event."""
    if agent_source == "ACTUAL_AI_API":
        source_tag = f"ACTUAL AI AGENT API ({agent_name or 'Gemini'})"
    else:
        source_tag = "LOCAL HEURISTIC AGENT" + (f" ({agent_name})" if agent_name else "")
    line = f"[{stage}] {source_tag}: {action_description}" + (f" -- {detail}" if detail else "")
    print(line, flush=True)
    return {"run_id": run_id, "stage": stage, "agent_source": agent_source,
            "agent_name": agent_name or ("Gemini" if agent_source == "ACTUAL_AI_API" else "Deterministic rule engine"),
            "action_description": action_description, "detail": detail, "line": line, "timestamp": _now(),
            "extra": extra or {}}
