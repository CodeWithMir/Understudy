"""Model Armor extension: prompt-injection protection + input sanitisation for EVERY wizard input.

Channels covered
    MEDIA       - uploaded video/audio: extension allow-list, magic-byte sniffing, size cap,
                  filename sanitisation + screening, SHA-256 fingerprint. After the WATCH agent
                  "watches" the media, its transcribed on-screen/audio text is screened again
                  (MEDIA_TRANSCRIPT) to stop *indirect* prompt injection hidden in a recording.
    SOP         - freeform SOP / constraint text.
    URL         - user-provided target URLs and any URL the AI proposes (verification/API URLs):
                  scheme allow-list, no embedded credentials, DNS resolution, SSRF blocking of
                  link-local / cloud-metadata / multicast / reserved ranges, private ranges only when
                  explicitly allow-listed, and injection screening of the URL text itself.
    PAYLOAD     - parameter keys and values edited by the user in BUILD/GOVERN.
    TOOL_NAME   - free-text framework names typed by the user.
    PAGE_CONTENT- text scraped from the target page before it is shown to the AI.

Layering:
    0. Sanitise   - NFKC normalise, strip zero-width chars, reject bidi overrides (Trojan-Source),
                    strip control chars, enforce length caps.
    1. Regex      - ``CORE_PATTERNS`` (the original Model Armor signatures) + ``WIZARD_PATTERNS``
                    (role/delimiter spoofing, system-prompt exfiltration, persona hijacks ...).
    1b. Optional  - Google Cloud Model Armor (core/wizard/cloud_armor.py) when an admin switched it on.
    2. Semantic   - the ``MODEL_ARMOR_CLASSIFIER`` prompt (zero-temperature Gemini classifier) when AI is
                    available; otherwise a context-aware local intent heuristic that only fires
                    when an override verb is aimed at the AI/instructions (fewer false positives on
                    legitimate SOP sentences such as "managers may override the limit in writing").
Every block is written to the Mission Control security-alert feed (core/wizard/security_alerts.py).
"""
from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import socket
import unicodedata
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote_plus, urlsplit, urlunsplit

MAX_TEXT_CHARS = {"SOP": 20000, "PAYLOAD": 2000, "TOOL_NAME": 200, "URL": 2048, "MEDIA_TRANSCRIPT": 20000,
                  "PAGE_CONTENT": 50000, "FILENAME": 255, "DEFAULT": 20000}

_ZERO_WIDTH = re.compile("[​‌‍⁠﻿­]")
_BIDI = re.compile("[‪-‮⁦-⁩]")
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Stage 1a - the original Model Armor signatures (moved here from core/model_armor.py in 9.4.1)
CORE_PATTERNS: List[str] = [
    r"(?i)\bignore\s+(all\s+)?previous\s+instructions\b",
    r"(?i)\bsystem\s+prompt\s+override\b",
    r"(?i)\bbypass\s+(governance|validation|rules|compliance|checks)\b",
    r"(?i)\bdrop\s+table\b",
    r"(?i)\badmin\s+mode\s+enable\b",
    r"(?i)\bdisregard\s+(all\s+)?prior\s+(rules|prompts|guidelines|governance)\b",
    r"(?i)\bwaive\s+(validation|compliance|checks|limits)\b",
    r"(?i)\bdisable\s+(security|guardrails|safety|governance|validation)\b",
    r"(?i)\bapprove\s+all\s+(invoices|transactions|without\s+checks|unconditionally)\b(?!\s+(under|below|up\s+to|less\s+than|smaller\s+than)\b)",
    r"(?i)\bdo\s+not\s+(validate|verify|check|escalate)\b",
    r"(?i)\bjailbreak\b",
    r"(?i)\bprompt\s+injection\b",
]


def _core_regex(text: str) -> Tuple[bool, str, Optional[str]]:
    """(is_safe, message, matched text) for the original signatures plus the unspaced-payload entropy check."""
    for pattern in CORE_PATTERNS:
        m = re.search(pattern, text)
        if m:
            return False, f"[LOCAL HEURISTIC (Regex Pre-Filter)]: Adversarial signature detected: '{pattern}' (matched: '{m.group(0)}')", m.group(0)
    if len(text) > 5000 and " " not in text[:500]:
        return False, "[LOCAL HEURISTIC (Entropy Check)]: Payload failed entropy integrity validation.", "High Entropy / Unspaced Payload"
    return True, "", None


WIZARD_PATTERNS: List[Tuple[str, str]] = [
    (r"(?i)</?\s*untrusted_input", "DELIMITER SPOOFING"),
    (r"(?i)<\|?\s*(im_start|im_end|system|endoftext)\s*\|?>", "CHAT-TEMPLATE TOKEN INJECTION"),
    (r"(?im)^\s*#{2,}\s*(system|assistant|developer)\b", "ROLE HEADER SPOOFING"),
    (r"(?im)^\s*(system|developer)\s*:\s*(you|ignore|from now|new instructions)", "ROLE HEADER SPOOFING"),
    (r"(?i)\b(ignore|disregard|forget)\s+(all\s+|any\s+|the\s+)?(above|prior|previous|earlier|preceding)\s+(instructions|rules|prompts|context)", "IGNORE PREVIOUS INSTRUCTIONS"),
    (r"(?i)\b(reveal|print|show|repeat|leak)\s+(your|the)\s+(system\s+prompt|hidden\s+instructions|instructions)", "SYSTEM PROMPT EXFILTRATION"),
    (r"(?i)\byou\s+are\s+now\s+(an?\s+)?(unrestricted|jailbroken|dan|developer\s+mode|admin)", "PERSONA HIJACK"),
    (r"(?i)\bnew\s+instructions\s*:", "INSTRUCTION INJECTION"),
    (r"(?i)\b(auto[- ]?approve|mark\s+as\s+compliant|skip\s+(the\s+)?(governance|compliance|verification))\b", "GOVERNANCE BYPASS"),
    (r"(?i)\b(curl|wget)\s+https?://", "EXFILTRATION COMMAND"),
    (r"(?i)\b(rm\s+-rf|os\.system|subprocess\.|child_process|Runtime\.getRuntime)", "COMMAND INJECTION"),
    # v6.0 - added after the benchmark (scripts/benchmark.py) showed these slipping through the offline stage
    (r"(?i)<\s*script\b|\bjavascript\s*:|\bon(error|load)\s*=", "SCRIPT INJECTION"),
    (r"(?i)\$\{\s*jndi\s*:|\{\{\s*[\w.]+\s*[*+]\s*[\w.]+\s*\}\}|<%=|\$\{\s*\d+\s*[*+]\s*\d+\s*\}", "TEMPLATE / JNDI INJECTION"),
    (r"(?i)169\.254\.169\.254|metadata\.google\.internal|/latest/meta-data|/computeMetadata/", "CLOUD METADATA EXFILTRATION"),
    (r"(?i)\bexfiltrat\w*", "EXFILTRATION COMMAND"),
    (r"(?i)(?<!not )(?<!never )(?<!n't )\b(ignore|disregard|forget)\s+(the\s+|all\s+|any\s+|this\s+)?(sop|policy|policies|rules?|governance|compliance)\b", "GOVERNANCE BYPASS"),
    (r"(?i)\b(approve|accept|allow|whitelist)\s+(every|all|any)\s+(vendors?|amounts?|invoices?|tools?|requests?)\b(?!\s+(under|below|up\s+to|less\s+than|from\s+the|on\s+the|listed|approved))", "GOVERNANCE BYPASS"),
    (r"(?i)\b(act|pose|pretend|behave)\s+(as|like|to\s+be)\s+(the\s+|an?\s+)?(admin|administrator|root|superuser|supervisor)\b", "PRIVILEGE ESCALATION"),
    (r"(?i)\b(print|reveal|show|leak|send|dump)\b[^.\n]{0,40}\b(api[_ ]?key|password|secret|token|credentials?|environment\s+variables?|GEMINI_API_KEY)\b(?!\s+(field|input|box|textbox|column|label|prompt|reset))", "SECRET EXFILTRATION"),
]

_AI_TARGET = re.compile(
    r"(?i)\b(instructions?|prompts?|assistant|ai|llm|language\s+model|agent|guardrails?|chatbot|model\s+armor)\b"
    r"|\byour\s+(rules|guidelines|policies|programming|safety)\b")
_OVERRIDE_VERBS = re.compile(r"(?i)\b(ignore|disregard|override|bypass|forget|disable|jailbreak|circumvent)\b")


@dataclass
class GuardrailVerdict:
    is_safe: bool
    channel: str
    sanitized: str = ""
    stage: str = "CLEARED"
    pattern: Optional[str] = None
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    agent_source: str = "LOCAL_HEURISTIC"

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["sanitized"] = d["sanitized"][:300]
        return d

    @property
    def friendly_message(self) -> str:
        if self.is_safe:
            return "Looks safe."
        what = self.pattern or (self.reasons[0] if self.reasons else "unsafe content")
        return (f"Understudy's security guard (Model Armor) blocked this {self.channel.lower().replace('_', ' ')} "
                f"because it contains something that looks like an attempt to control the AI ({what}). "
                f"Please remove that part and try again.")


class GuardrailViolation(Exception):
    def __init__(self, verdict: GuardrailVerdict):
        super().__init__(verdict.friendly_message)
        self.verdict = verdict


def sanitize_text(text: Any, channel: str = "DEFAULT") -> Tuple[str, List[str], List[str]]:
    """Returns (clean_text, blocking_reasons, warnings)."""
    reasons: List[str] = []
    warnings: List[str] = []
    s = "" if text is None else str(text)
    if _BIDI.search(s):
        reasons.append("Bidirectional override characters (Trojan-Source style hidden text)")
        s = _BIDI.sub("", s)
    if _ZERO_WIDTH.search(s):
        warnings.append("Invisible zero-width characters were removed")
        s = _ZERO_WIDTH.sub("", s)
    s = unicodedata.normalize("NFKC", s)
    if _CONTROL.search(s):
        warnings.append("Control characters were removed")
        s = _CONTROL.sub("", s)
    limit = MAX_TEXT_CHARS.get(channel, MAX_TEXT_CHARS["DEFAULT"])
    if len(s) > limit:
        reasons.append(f"Input is too long ({len(s)} characters; limit {limit})")
    return s, reasons, warnings


def _local_semantic(text: str) -> Tuple[bool, Optional[str]]:
    """Context-aware offline intent check: override verb aimed at the AI within one sentence."""
    for sentence in re.split(r"(?<=[.!?\n])\s+", text):
        v = _OVERRIDE_VERBS.search(sentence)
        if v and _AI_TARGET.search(sentence):
            return False, f"override verb '{v.group(0)}' aimed at the AI"
    return True, None


def screen_text(text: Any, channel: str, use_semantic: bool = True) -> GuardrailVerdict:
    clean, reasons, warnings = sanitize_text(text, channel)
    verdict = GuardrailVerdict(is_safe=True, channel=channel, sanitized=clean, warnings=warnings)
    if reasons:
        verdict.is_safe, verdict.stage, verdict.reasons = False, "STAGE_0_SANITIZE", reasons
        verdict.pattern = reasons[0]
        return verdict
    if not clean.strip():
        return verdict
    ok, msg, pattern = _core_regex(clean)
    if not ok:
        verdict.is_safe, verdict.stage, verdict.pattern, verdict.reasons = False, "STAGE_1_REGEX", pattern, [msg]
        return verdict
    for rx, label in WIZARD_PATTERNS:
        m = re.search(rx, clean)
        if m:
            verdict.is_safe, verdict.stage, verdict.pattern = False, "STAGE_1_REGEX", label
            verdict.reasons = [f"Matched '{m.group(0)[:80]}'"]
            return verdict
    if use_semantic:
        from core.wizard import cloud_armor
        if cloud_armor.enabled():
            blocked, why = cloud_armor.screen(clean)
            if blocked:
                verdict.is_safe, verdict.stage, verdict.pattern = False, "STAGE_1B_CLOUD_MODEL_ARMOR", "Google Cloud Model Armor"
                verdict.reasons = [why]
                return verdict
            if blocked is None:
                verdict.warnings.append(why + "; the built-in Model Armor stages were used")
                if cloud_armor.fail_closed():
                    verdict.is_safe, verdict.stage, verdict.pattern = False, "STAGE_1B_CLOUD_MODEL_ARMOR", "Model Armor unavailable"
                    verdict.reasons = [why + " and fail-closed mode is on"]
                    return verdict
    if use_semantic and len(clean) >= 12:
        from core.wizard.llm import ai_available, _TEST_BACKEND
        if ai_available() and _TEST_BACKEND is None:
            # Stage 2 with the Mission Control model/key (or Vertex AI) through the prompt registry - the exact prompt,
            # response, errors and fallback are all recorded in Mission Control telemetry by llm.generate_json.
            from core.wizard.llm import generate_json
            res = generate_json("MODEL_ARMOR_CLASSIFIER", {"channel": channel, "payload": clean[:4000]},
                                untrusted=["payload"])
            if res.ok:
                verdict.agent_source = res.agent_source
                if res.data.get("is_adversarial"):
                    reason = str(res.data.get("reason") or "Adversarial intent detected")[:300]
                    verdict.is_safe, verdict.stage = False, "STAGE_2_SEMANTIC"
                    verdict.pattern = str(res.data.get("detected_trigger") or "AI OVERRIDE ATTEMPT")[:120]
                    verdict.reasons = [(f"Gemini semantic classifier ({res.model})" if res.agent_source == "ACTUAL_AI_API"
                                        else f"Learned Model Armor verdict ({res.model})") + f": {reason}"]
                return verdict
            verdict.warnings.append(f"Semantic classifier unavailable ({res.error}); used local intent heuristic")
        ok, why = _local_semantic(clean)
        if ok and not ai_available():
            # Offline: the exact same text was judged adversarial by Gemini before -> keep blocking it (fail closed).
            from core.wizard import recall
            hit = recall.lookup("MODEL_ARMOR_CLASSIFIER", {"channel": channel, "payload": clean[:4000]})
            if hit and (hit["data"] or {}).get("is_adversarial"):
                verdict.agent_source = recall.SOURCE
                verdict.is_safe, verdict.stage = False, "STAGE_2_SEMANTIC"
                verdict.pattern = str(hit["data"].get("detected_trigger") or "AI OVERRIDE ATTEMPT")[:120]
                verdict.reasons = [f"Blocked by a learned Model Armor verdict ({hit['learned_with']}, "
                                   f"{str(hit['learned_at'])[:10]}): {str(hit['data'].get('reason') or '')[:200]}"]
                return verdict
        if ai_available() and _TEST_BACKEND is None:
            from core.wizard.llm import record_fallback
            record_fallback("MODEL_ARMOR_CLASSIFIER", "Semantic classifier call failed", {"safe": ok, "why": why},
                            "Local intent heuristic (keyword/phrase matcher).")
        if not ok:
            verdict.is_safe, verdict.stage, verdict.pattern, verdict.reasons = False, "STAGE_2_SEMANTIC", "AI OVERRIDE ATTEMPT", [why]
    return verdict


# ---------------------------------------------------------------------------
# URL screening (SSRF + injection)
# ---------------------------------------------------------------------------
# Private hosts are normally allowed through the admin URL exception list (Mission Control); this env var is an
# additional operator-level allow-list (comma separated), empty by default.
_DEFAULT_ALLOWED_PRIVATE = ""
_METADATA_HOSTS = {"metadata.google.internal", "metadata", "169.254.169.254", "fd00:ec2::254", "100.100.100.200"}


def _allowed_private_hosts() -> set:
    raw = os.environ.get("UNDERSTUDY_ALLOWED_PRIVATE_HOSTS", _DEFAULT_ALLOWED_PRIVATE)
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


@dataclass
class UrlVerdict:
    is_safe: bool
    url: str
    normalized_url: str = ""
    host: str = ""
    resolved_ips: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    admin_exception: bool = False
    admin_blocked: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def friendly_message(self) -> str:
        if self.is_safe:
            return "URL looks safe."
        return "This URL can't be used: " + "; ".join(self.reasons)


def screen_url(url: Any, resolve: bool = True, same_host_as: Any = None) -> UrlVerdict:
    raw = "" if url is None else str(url).strip()
    v = UrlVerdict(is_safe=False, url=raw[:300])
    clean, reasons, _ = sanitize_text(raw, "URL")
    if reasons:
        v.reasons = reasons
        return v
    if not clean:
        v.reasons = ["URL is empty"]
        return v
    if re.search(r"\s", clean):
        v.reasons = ["URL contains spaces"]
        return v
    if "://" not in clean:
        clean = "http://" + clean
    try:
        parts = urlsplit(clean)
        port = parts.port
    except ValueError as e:
        v.reasons = [f"URL is malformed ({e})"]
        return v
    if parts.scheme.lower() not in ("http", "https"):
        v.reasons = [f"Only http:// and https:// URLs are allowed (got '{parts.scheme}')"]
        return v
    if parts.username or parts.password or "@" in parts.netloc:
        v.reasons = ["URLs must not contain usernames or passwords - use the Secrets fields instead"]
        return v
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        v.reasons = ["URL has no host name"]
        return v
    v.host = host
    text_verdict = screen_text(unquote_plus(clean), "URL", use_semantic=False)
    if not text_verdict.is_safe:
        v.reasons = [f"URL text contains a prompt-injection pattern ({text_verdict.pattern})"]
        return v
    if same_host_as:
        # 9.4: a session may have several target URLs - any of their hosts is "the target host"
        bases = [same_host_as] if isinstance(same_host_as, str) else [b for b in same_host_as if b]
        base_hosts = [(urlsplit(b if "://" in b else "http://" + b).hostname or "").lower() for b in bases]
        base_hosts = [b for b in base_hosts if b]
        if base_hosts and host not in base_hosts:
            v.reasons = [f"URL host '{host}' differs from the target host"
                         + (f" '{base_hosts[0]}'" if len(base_hosts) == 1 else "s " + ", ".join(f"'{b}'" for b in base_hosts))]
            return v
    if host in _METADATA_HOSTS:
        v.reasons = ["Cloud metadata endpoints are never allowed"]
        return v
    from core.wizard.admin_policy import blocking_entry, is_host_excepted
    blk = blocking_entry(host)
    if blk:                                   # the admin's blocked list beats every exception and allow-list
        v.admin_blocked = True
        v.reasons = [f"'{host}' is on the blocked list set by your admin in Mission Control ({blk['host']})."
                     + (f" Reason: {blk['note']}" if blk.get("note") else "")]
        return v
    excepted = is_host_excepted(host)
    v.admin_exception = excepted
    ask_admin = " Ask your admin to add it to the URL exception list in Mission Control if you need to test it."
    ips: List[str] = []
    try:
        ips = [str(ipaddress.ip_address(host))]
    except ValueError:
        if resolve:
            try:
                infos = socket.getaddrinfo(host, port or (443 if parts.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
                ips = sorted({i[4][0] for i in infos})
            except socket.gaierror:
                if not excepted:
                    v.reasons = [f"Host '{host}' could not be found (DNS lookup failed)." + ask_admin]
                    return v
    v.resolved_ips = ips
    allow_all_private = os.environ.get("UNDERSTUDY_ALLOW_PRIVATE_NETWORKS", "").lower() in ("1", "true", "yes")
    allowed_hosts = _allowed_private_hosts()
    for ip_s in ips:
        ip = ipaddress.ip_address(ip_s.split("%")[0])
        if ip.is_link_local or str(ip) in _METADATA_HOSTS:
            v.reasons = [f"{ip} is a link-local / cloud-metadata address (SSRF protection)"]
            return v
        if excepted:
            continue  # admin exception: private / loopback / unusual ranges are allowed for this host
        if ip.is_multicast or ip.is_unspecified or ip.is_reserved:
            v.reasons = [f"{ip} is not a routable address." + ask_admin]
            return v
        if (ip.is_private or ip.is_loopback) and not allow_all_private and host not in allowed_hosts and ip_s not in allowed_hosts:
            v.reasons = [f"{host} resolves to the private/internal address {ip}, which is blocked by default "
                         f"(SSRF protection)." + ask_admin]
            return v
    netloc = host if not port else f"{host}:{port}"
    if ":" in host and not host.startswith("["):
        netloc = f"[{host}]" + (f":{port}" if port else "")
    v.normalized_url = urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))
    v.is_safe = True
    return v


# ---------------------------------------------------------------------------
# Media screening
# ---------------------------------------------------------------------------
MEDIA_SIGNATURES = {
    ".mp4": "iso", ".m4v": "iso", ".mov": "iso", ".m4a": "iso", ".3gp": "iso",
    ".webm": "ebml", ".mkv": "ebml",
    ".avi": "riff_avi", ".wav": "riff_wav",
    ".mp3": "mp3", ".ogg": "ogg", ".flac": "flac",
}
AUDIO_EXT = {".m4a", ".wav", ".mp3", ".ogg", ".flac"}
MAX_MEDIA_BYTES = int(os.environ.get("UNDERSTUDY_MAX_MEDIA_MB", "250")) * 1024 * 1024


def _sniff(head: bytes) -> Optional[str]:
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "iso"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "ebml"
    if head.startswith(b"RIFF") and head[8:12] == b"AVI ":
        return "riff_avi"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "riff_wav"
    if head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "mp3"
    if head.startswith(b"OggS"):
        return "ogg"
    if head.startswith(b"fLaC"):
        return "flac"
    return None


def safe_filename(name: str) -> str:
    base = os.path.basename(str(name or "upload").replace("\\", "/"))
    base, _, _ = sanitize_text(base, "FILENAME")
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "upload"
    return base[:120]


@dataclass
class MediaVerdict:
    is_safe: bool
    filename: str
    kind: str = "unknown"
    size_bytes: int = 0
    sha256: str = ""
    detected_format: Optional[str] = None
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def friendly_message(self) -> str:
        return "Media accepted." if self.is_safe else "This file can't be used: " + "; ".join(self.reasons)


def screen_media(path: str, original_filename: str) -> MediaVerdict:
    fname = safe_filename(original_filename)
    v = MediaVerdict(is_safe=False, filename=fname)
    ext = os.path.splitext(fname)[1].lower()
    if ext not in MEDIA_SIGNATURES:
        v.reasons.append(f"'{ext or 'no extension'}' is not a supported video/audio type "
                         f"({', '.join(sorted(MEDIA_SIGNATURES))})")
        return v
    name_verdict = screen_text(os.path.splitext(original_filename or "")[0], "FILENAME", use_semantic=False)
    if not name_verdict.is_safe:
        v.reasons.append(f"The file name contains a prompt-injection pattern ({name_verdict.pattern})")
        return v
    if not os.path.isfile(path):
        v.reasons.append("Uploaded file is missing on the server")
        return v
    size = os.path.getsize(path)
    v.size_bytes = size
    if size == 0:
        v.reasons.append("File is empty")
        return v
    if size > MAX_MEDIA_BYTES:
        v.reasons.append(f"File is larger than {MAX_MEDIA_BYTES // (1024 * 1024)} MB")
        return v
    h = hashlib.sha256()
    with open(path, "rb") as f:
        head = f.read(64)
        h.update(head)
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    v.sha256 = h.hexdigest()
    detected = _sniff(head)
    v.detected_format = detected
    if detected != MEDIA_SIGNATURES[ext]:
        v.reasons.append(f"File content does not match its '{ext}' extension (possible disguised file)")
        return v
    v.kind = "audio" if ext in AUDIO_EXT else "video"
    v.is_safe = True
    return v


# ---------------------------------------------------------------------------
# Payload screening
# ---------------------------------------------------------------------------
PARAM_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def normalize_param_key(raw: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", str(raw or "")).strip("_").lower()
    if not s or not s[0].isalpha():
        s = "p_" + s
    return s[:64]


def screen_parameters(parameters: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Returns (clean_parameters, problems). problems: [{key, message, pattern}]"""
    clean: List[Dict[str, Any]] = []
    problems: List[Dict[str, Any]] = []
    seen = set()
    for p in parameters or []:
        key = str(p.get("key", ""))
        if not PARAM_KEY_RE.match(key):
            problems.append({"key": key, "message": "Parameter names must be lower_snake_case (letters, digits, _)",
                             "pattern": "INVALID_KEY"})
            continue
        if key in seen:
            problems.append({"key": key, "message": "Duplicate parameter name", "pattern": "DUPLICATE_KEY"})
            continue
        seen.add(key)
        item = dict(p)
        for text_field in ("label", "description"):
            if item.get(text_field):
                tv = screen_text(item[text_field], "PAYLOAD", use_semantic=False)
                if not tv.is_safe:
                    problems.append({"key": key, "message": tv.friendly_message, "pattern": tv.pattern})
                item[text_field] = tv.sanitized[:300]
        value = item.get("value")
        if isinstance(value, (dict, list)):
            problems.append({"key": key, "message": "Values must be plain text, numbers or true/false", "pattern": "INVALID_TYPE"})
            continue
        if isinstance(value, str) and item.get("type") != "secret":
            tv = screen_text(value, "PAYLOAD")
            if not tv.is_safe:
                problems.append({"key": key, "message": tv.friendly_message, "pattern": tv.pattern})
                continue
            item["value"] = tv.sanitized
        clean.append(item)
    return clean, problems


def _inspector(pattern: Optional[str], detail: str) -> str:
    """Which layer actually blocked the input (9.5.2: the alert used to name the selected Gemini model even when the
    built-in pattern rules had blocked it without any AI call)."""
    m = re.search(r"Gemini semantic classifier \(([^)]+)\)", detail or "")
    if m:
        return m.group(1)
    if "learned Model Armor verdict" in (detail or "") or "Learned Model Armor verdict" in (detail or ""):
        return "learned Model Armor verdict"
    if (pattern or "").startswith("Google Cloud Model Armor") or "Model Armor unavailable" == pattern:
        return "Google Cloud Model Armor"
    return "Understudy pattern rules (no AI call)"


def record_block(session_id: str, channel: str, pattern: Optional[str], raw_text: str, portal_url: str = "",
                 detail: str = "") -> Dict[str, Any]:
    """Writes the interception to the Mission Control security-alert feed."""
    try:
        from core.wizard import security_alerts
        return security_alerts.record(
            run_id=session_id,
            prompt_injection=pattern or "Adversarial pattern",
            channel=f"Understudy - {channel}",
            portal=(urlsplit(portal_url).hostname if portal_url and "://" in portal_url else portal_url) or "n/a",
            portal_url=portal_url or "n/a",
            raw_text=str(raw_text)[:400],
            model_name=_inspector(pattern, detail),
        )
    except Exception as e:  # pragma: no cover - alert feed is best effort
        return {"error": str(e)}


MAX_TARGET_URLS = 10


def parse_url_list(value: Any) -> List[str]:
    """9.4: the target URLs a user gave - a list, a JSON list, or one URL per line / comma. Order kept, duplicates and
    blanks dropped. Nothing is screened here (``screen_url`` does that per URL)."""
    import json as _json
    if value is None:
        return []
    items: List[Any]
    if isinstance(value, (list, tuple)):
        items = list(value)
    else:
        text = str(value).strip()
        if text.startswith("["):
            try:
                items = _json.loads(text)
            except ValueError:
                items = re.split(r"[\s,]+", text.strip("[]"))
            if not isinstance(items, list):
                items = [text]
        else:
            items = re.split(r"[\r\n]+|,(?=\s*https?://)", text)
    out: List[str] = []
    for it in items:
        u = str(it or "").strip()
        if u and u not in out:
            out.append(u)
    return out
