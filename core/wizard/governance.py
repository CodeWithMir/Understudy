"""STEP 3 - GOVERN: SOP rule engine, compliance evaluation and generated-script safety scan.

* ``parse_sop_rules``      - deterministic parser for common SOP constraint phrasings
                             (allowed/denied lists, spend limits, minimums, date windows, role access,
                             required fields, starts-with/contains formats).
* ``extract_rules``        - AI extraction (GOVERN_EXTRACT_RULES) merged with the parser output; the
                             user reviews and CONFIRMS the table before anything is enforced.
* ``evaluate_compliance``  - authoritative, deterministic evaluation of the payload against the
                             confirmed rules -> explicit violations with plain-English fix hints.
* ``run_compliance_check`` - evaluation + script safety scan + optional AI advisory review.
* ``scan_suite``           - static scan of generated code: dangerous calls, hardcoded secrets,
                             hardcoded parameter values (parameterisation proof), unexpected hosts.
"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

RULE_TYPES = ("allowed_values", "denied_values", "max", "min", "date_range", "regex", "required", "role_access")

SYNONYMS: Dict[str, List[str]] = {
    "vendor": ["vendor", "vendors", "supplier", "suppliers", "company", "companies", "payee", "merchant", "seller"],
    "amount": ["amount", "total", "price", "cost", "spend", "spending", "payable", "sum", "invoice value", "value", "budget"],
    "date": ["date", "dates", "day", "period", "posting", "dated"],
    "customer": ["customer", "customers", "client", "clients", "buyer", "buyers", "account name"],
    "role": ["role", "roles", "user role", "user_role"],
    "currency": ["currency", "currencies"],
    "notes": ["notes", "note", "remarks", "memo", "comment", "comments"],
    "department": ["department", "departments", "cost center", "cost centre", "cost centers"],
    "country": ["country", "countries", "region"],
    "category": ["category", "categories", "product", "products"],
    "email": ["email", "e-mail"],
    "quantity": ["quantity", "qty", "units"],
}
_LIST_NOUNS = r"(vendors?|suppliers?|customers?|clients?|companies|payees?|merchants?|cost cent(?:er|re)s?|currencies|currency|countries|departments?|products?|categories|roles?|users?)"


_FILLER = (r"(?:\s+(?:per|each|for|an?|the|single|one|invoice|invoices|transaction|transactions|order|orders|purchase|"
           r"payment|payments|request|item|amount|total|value|spend)){0,4}")


def _new_id() -> str:
    return "R-" + uuid.uuid4().hex[:6].upper()


def _concept_of(text: str) -> Optional[str]:
    t = text.lower()
    best, pos = None, 10 ** 9
    for concept, words in SYNONYMS.items():
        for w in words:
            m = re.search(r"\b" + re.escape(w) + r"\b", t)
            if m and m.start() < pos:
                best, pos = concept, m.start()
    return best


def _param_tokens(p: Dict[str, Any]) -> str:
    return (p.get("key", "").replace("_", " ") + " " + str(p.get("label", ""))).lower()


def bind_field(concept: Optional[str], params: List[Dict[str, Any]], want_type: Optional[str] = None) -> Tuple[str, bool]:
    """Returns (field_key, bound)."""
    candidates = params
    if want_type:
        typed = [p for p in params if p.get("type") == want_type]
        candidates = typed or params
    if concept:
        def hits(p: Dict[str, Any], words: List[str]) -> bool:
            toks = _param_tokens(p)
            return any(re.search(r"\b" + re.escape(w) + r"\b", toks) for w in words)

        def other_concepts(p: Dict[str, Any]) -> int:     # 9.9: "Role in Company" is a role, not the company
            return sum(1 for c, ws in SYNONYMS.items() if c != concept and hits(p, ws))
        words = SYNONYMS.get(concept, [concept])
        for pool in (candidates, params):
            found = [p for p in pool if hits(p, words)]
            if found:
                return min(found, key=other_concepts)["key"], True
        # a field named in plain words ("The Last Name is required") -> the field with that label / key
        plain = re.sub(r"^(?:the|a|an|your|our)[ _]+", "", str(concept).lower().replace("_", " ")).strip()
        for p in params:
            if plain and plain in (str(p.get("label", "")).strip().lower(), p["key"].replace("_", " ")):
                return p["key"], True
    if want_type in ("number", "date"):
        typed = [p for p in params if p.get("type") == want_type]
        if len(typed) == 1:
            return typed[0]["key"], True
    fallback = {"role": "user_role", "vendor": "vendor_name", "amount": "total_amount", "date": "date",
                "customer": "customer_name"}.get(concept or "", concept or "field")
    exists = any(p["key"] == fallback for p in params)
    return fallback, exists


# ---------------------------------------------------------------------------
# Value parsing helpers
# ---------------------------------------------------------------------------
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|march|april|june|july|august|september|october|november|december"
DATE_RX = (r"(\d{4}-\d{2}-\d{2}|\d{1,2}[/.]\d{1,2}[/.]\d{4}|\d{1,2}\s+(?:" + _MONTHS + r")[a-z]*\.?,?\s+\d{4}|(?:" + _MONTHS +
           r")[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})")


def parse_date(s: Any) -> Optional[date]:
    if isinstance(s, date):
        return s
    t = re.sub(r"(\d)(st|nd|rd|th)", r"\1", str(s or "").strip().rstrip(".").replace(",", ""))
    t = re.sub(r"\bsept\b", "sep", t, flags=re.I)
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y", "%Y/%m/%d",
                "%d-%m-%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(t[:19], fmt).date()
        except ValueError:
            continue
    return None


def parse_number(s: Any) -> Optional[float]:
    if isinstance(s, bool):
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"-?[0-9][0-9,]*(?:\.[0-9]+)?", str(s or ""))
    if not m:
        return None
    try:
        val = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    suffix = str(s)[m.end():m.end() + 2].strip().lower()
    if suffix.startswith("k") and not suffix.startswith("kg"):
        val *= 1000
    elif suffix.startswith("m") and not suffix.startswith("mo"):
        val *= 1_000_000
    return val


_ABBR_DOT = "\u2024"                   # a stand-in for an abbreviation's full stop while splitting
# "Rs. 200", "No. 7", "approx. 5k", "e.g. acme" - followed by a number / lowercase word / currency / comma
_ABBR_RX = re.compile(r"\b(Rs|No|Nos|approx|e\.g|i\.e)\.(?=\s*[0-9a-z₹$€£]|[,;)])")
# "Globex Pvt. Ltd.", "Acme Inc., Globex" - a company suffix followed by another suffix or a comma
_SUFFIX_RX = re.compile(r"\b(Inc|Ltd|Co|Corp|Pvt|LLC|LLP|Bros)\.(?=\s*(?:Ltd|Limited|Inc|LLC|LLP|Co)\b|[,;)])")


def _guard_abbr(text: str) -> str:
    """9.9: an abbreviation's full stop ends neither a sentence nor a list - but "... Globex Corp. The limit ..." still
    ends the sentence after "Corp."."""
    text = _ABBR_RX.sub(lambda m: m.group(1) + _ABBR_DOT, text)
    return _SUFFIX_RX.sub(lambda m: m.group(1) + _ABBR_DOT, text)


def _split_list(text: str) -> List[str]:
    text = re.split(r"(?<![A-Za-z]\.[A-Za-z])\.(\s|$)|\n", _guard_abbr(text))[0].replace(_ABBR_DOT, ".")
    parts = re.split(r",|;|\s+and\s+|\s+or\s+|\s*/\s*|\s*\|\s*", text)
    out = []
    for p in parts:
        p = p.strip().strip("\"'“”‘’()[]").strip()
        p = re.sub(r"^(only|the|and|or)\s+", "", p, flags=re.I)
        p = re.sub(r"\s+(only|are allowed|is allowed|are permitted)$", "", p, flags=re.I)
        if p and len(p) <= 120 and not re.fullmatch(r"(etc|others?)", p, re.I):
            out.append(p)
    return out[:50]


def _fmt_money(v: float) -> str:
    return f"{v:,.2f}".rstrip("0").rstrip(".") if v != int(v) else f"{int(v):,}"


def plain_english(rule: Dict[str, Any]) -> str:
    f, t = rule.get("field"), rule.get("rule_type")
    if t in ("allowed_values", "role_access"):
        return f"'{f}' must be one of: {', '.join(rule.get('values') or [])}."
    if t == "denied_values":
        return f"'{f}' must NOT be any of: {', '.join(rule.get('values') or [])}."
    if t == "max":
        return f"'{f}' must be {'below' if rule.get('exclusive') else 'at most'} {_fmt_money(float(rule.get('limit') or 0))}."
    if t == "min":
        return f"'{f}' must be {'above' if rule.get('exclusive') else 'at least'} {_fmt_money(float(rule.get('limit') or 0))}."
    if t == "date_range":
        s, e = rule.get("start_date"), rule.get("end_date")
        if s and e:
            return f"'{f}' must be between {s} and {e}."
        return f"'{f}' must be on or after {s}." if s else f"'{f}' must be on or before {e}."
    if t == "regex":
        return f"'{f}' must match the format {rule.get('pattern')}."
    if t == "required":
        return f"'{f}' must be filled in."
    return f"Rule on '{f}'."


def _mk(field: str, bound: bool, rule_type: str, source_text: str, **kw) -> Dict[str, Any]:
    r = {"rule_id": _new_id(), "field": field, "bound": bound, "rule_type": rule_type, "values": kw.get("values", []),
         "limit": kw.get("limit"), "exclusive": kw.get("exclusive", False), "start_date": kw.get("start_date"),
         "end_date": kw.get("end_date"), "pattern": kw.get("pattern"), "source_text": source_text.strip()[:300],
         "source": kw.get("source", "parser"), "enabled": True}
    r["plain_english"] = plain_english(r)
    return r


# ---------------------------------------------------------------------------
# SOP in any language (7.2)
# With AI, GOVERN_EXTRACT_RULES reads an SOP in any language. Offline, the deterministic parser below works on English
# phrases, so the common rule phrases of Spanish, French, German, Portuguese, Italian and Hindi are first mapped onto
# their English equivalents (numerals in any script become ASCII digits, "31.12.2026" dates are understood). Names and
# values are never translated - "Globex Corp" stays "Globex Corp". An SOP in another language without AI yields a
# warning instead of silently passing (see extract_rules).
# ---------------------------------------------------------------------------
_D = r"(\d{4}-\d{2}-\d{2}|\d{1,2}[./]\d{1,2}[./]\d{4})"
# (pattern, replacement) - regexes, applied in order, case-insensitive
_PHRASES: List[Tuple[str, str]] = [
    # --- Hindi (word order: "<date> से/और <date> के बीच", "<amount> से अधिक नहीं") ---------------------------------
    (_D + r"\s*(?:से|और)\s*" + _D + r"\s*(?:के\s+बीच|तक)", r"between \1 and \2"),
    (r"([0-9][0-9,.]*)\s*(?:रुपये|रुपए|₹|रु\.?)?\s*से\s+(?:अधिक|ज़्यादा|ज्यादा)\s+नहीं", r"must not exceed \1"),
    (r"(?:स्वीकृत|अनुमोदित|मान्य)\s+विक्रेता(?:ओं)?", "approved vendors"),
    (r"(?:प्रतिबंधित|निषिद्ध|ब्लॉक)\s+विक्रेता(?:ओं)?", "blocked vendors"),
    (r"प्रति\s+चालान\s+(?:खर्च\s+)?सीमा", "spend limit per invoice"),
    (r"(?:खर्च\s+सीमा|अधिकतम\s+सीमा|अधिकतम)", "maximum"), (r"न्यूनतम", "minimum"),
    (r"विक्रेता\s+का\s+नाम", "vendor name"), (r"\s+है\b", " is"), ("।", ". "), (r"\s+और\s+", " and "),
    # --- Spanish -------------------------------------------------------------------------------------------------
    (r"entre\s+(?:el\s+(?:d[ií]a\s+)?)?" + _D + r"\s+y\s+(?:el\s+(?:d[ií]a\s+)?)?" + _D, r"between \1 and \2"),
    (r"proveedores\s+(?:aprobados|autorizados|permitidos)", "approved vendors"),
    (r"proveedores\s+(?:bloqueados|prohibidos)", "blocked vendors"),
    (r"nunca\s+(?:pagar|pague)\s+a\s+los\s+siguientes\s+proveedores", "never pay the following vendors"),
    (r"l[ií]mite\s+de\s+gasto\b\s*por\s+factura\s+es\s+(?:de\s+)?", "spend limit per invoice is "),
    (r"l[ií]mite\s+de\s+gasto\b", "spend limit"),
    (r"no\s+(?:debe|puede)\s+(?:superar|exceder)", "must not exceed"),
    (r"m[aá]ximo", "maximum"), (r"m[ií]nimo", "minimum"), (r"las\s+facturas\s+deben\s+tener\s+fecha", "invoices must be dated"),
    (r"nombre\s+del\s+proveedor", "vendor name"), (r"importe\s+total", "total amount"),
    # --- French --------------------------------------------------------------------------------------------------
    (r"entre\s+(?:le\s+)?" + _D + r"\s+et\s+(?:le\s+)?" + _D, r"between \1 and \2"),
    (r"fournisseurs\s+(?:approuv[ée]s|autoris[ée]s|agr[ée]{2}s)", "approved vendors"),
    (r"fournisseurs\s+(?:interdits|bloqu[ée]s)", "blocked vendors"),
    (r"ne\s+jamais\s+payer\s+les\s+fournisseurs\s+suivants", "never pay the following vendors"),
    (r"(?:le\s+)?plafond\s+de\s+d[ée]penses\s+par\s+facture\s+est\s+de\s+", "spend limit per invoice is "),
    (r"plafond\s+de\s+d[ée]penses|limite\s+de\s+d[ée]penses", "spend limit"),
    (r"ne\s+doit\s+pas\s+d[ée]passer", "must not exceed"),
    (r"les\s+factures\s+doivent\s+[êe]tre\s+dat[ée]es", "invoices must be dated"),
    (r"nom\s+du\s+fournisseur", "vendor name"), (r"montant\s+total", "total amount"),
    # --- German (verb at the end: "darf 5.000 EUR nicht überschreiten") -------------------------------------------
    (r"zwischen\s+(?:dem\s+)?" + _D + r"\s+und\s+(?:dem\s+)?" + _D, r"between \1 and \2"),
    (r"darf\s+([^\n]{1,40}?)\s+nicht\s+[üu]berschreiten", r"must not exceed \1"),
    (r"(?:zugelassene|genehmigte|freigegebene)\s+lieferanten", "approved vendors"),
    (r"(?:gesperrte|verbotene)\s+lieferanten", "blocked vendors"),
    (r"niemals\s+an\s+(?:die\s+)?folgenden?\s+lieferanten\s+zahlen", "never pay the following vendors"),
    (r"(?:das\s+)?ausgabenlimit\s+pro\s+rechnung\s+betr[äa]gt\s+", "spend limit per invoice is "),
    (r"ausgabenlimit|h[öo]chstbetrag", "spend limit"), (r"h[öo]chstens", "at most"), (r"mindestens", "at least"),
    (r"rechnungen\s+m[üu]ssen\s+datiert\s+sein", "invoices must be dated"),
    (r"lieferantenname", "vendor name"), (r"gesamtbetrag", "total amount"),
    # --- Portuguese / Italian ------------------------------------------------------------------------------------
    (r"(?:tra|fra)\s+(?:il\s+)?" + _D + r"\s+e\s+(?:il\s+)?" + _D, r"between \1 and \2"),
    (r"fornecedores\s+aprovados|fornitori\s+(?:approvati|autorizzati)", "approved vendors"),
    (r"fornecedores\s+bloqueados|fornitori\s+(?:bloccati|vietati)", "blocked vendors"),
    (r"limite\s+de\s+gastos?\s+por\s+fatura\s+[ée]\s+(?:de\s+)?", "spend limit per invoice is "),
    (r"limite\s+di\s+spesa\s+per\s+fattura\s+[èe]\s+(?:di\s+)?", "spend limit per invoice is "),
    (r"n[ãa]o\s+deve\s+exceder|non\s+deve\s+superare", "must not exceed"),
]
_CONJ = [(r"\s+(?:y|et|und|e|o|ou|oder)\s+", " and ")]      # only inside value lists (after a ':' / 'are')
_LANG_HINTS = {"es": r"\b(proveedores|factura|entre|debe|límite|limite)\b", "fr": r"\b(fournisseurs|facture|doit|plafond)\b",
               "de": r"\b(lieferanten|rechnung|darf|zwischen|betrag)\b", "pt": r"\b(fornecedores|fatura|deve)\b",
               "it": r"\b(fornitori|fattura|deve)\b", "hi": r"[\u0900-\u097F]"}
_LANG_NAMES = {"es": "Spanish", "fr": "French", "de": "German", "pt": "Portuguese", "it": "Italian", "hi": "Hindi",
               "other": "a non-English language"}


def _ascii_digits(text: str) -> str:
    import unicodedata
    out = []
    for ch in text:
        if ch.isdigit() and not ch.isascii():
            try:
                out.append(str(unicodedata.digit(ch)))
                continue
            except (TypeError, ValueError):
                pass
        out.append(ch)
    return "".join(out)


def sop_language(text: str) -> str:
    """'en' or a best guess of the SOP's language (es/fr/de/pt/it/hi/other) - used for notes, not for rules."""
    t = _ascii_digits(text or "")
    letters = [c for c in t if c.isalpha()]
    if not letters:
        return "en"
    for code, rx in _LANG_HINTS.items():
        if len(re.findall(rx, t, re.I)) >= (1 if code == "hi" else 2):
            return code
    non_latin = len([c for c in letters if ord(c) > 0x24F])
    return "other" if non_latin > len(letters) * 0.3 else "en"


_EU_NUMBER = re.compile(r"(?<![\d.,])(\d{1,3}(?:\.\d{3})+)(,\d{1,2})?(?![.\d]\d)")


def to_english_phrases(text: str) -> Tuple[str, int]:
    """Maps the common SOP rule phrases of other languages onto English ones (values untouched). Returns
    (text, number of phrases mapped)."""
    out, n = _ascii_digits(text or ""), 0
    if sop_language(out) in ("de", "es", "fr", "pt", "it"):     # 5.000,50 EUR -> 5000.50 EUR (dates keep their dots)
        out = _EU_NUMBER.sub(lambda m: m.group(1).replace(".", "") + (m.group(2) or "").replace(",", "."), out)
    for rx, repl in _PHRASES:
        out, k = re.subn(rx, repl, out, flags=re.I)
        n += k
    if n:
        # list conjunctions ("Acme y Globex", "Acme und Globex") - only in sentences that now carry an English rule
        lines = []
        for line in out.split("\n"):
            if re.search(r"approved vendors|blocked vendors|following vendors", line, re.I):
                for rx, repl in _CONJ:
                    line = re.sub(rx, repl, line)
            lines.append(line)
        out = "\n".join(lines)
    return out, n


# ---------------------------------------------------------------------------
# Deterministic SOP parser
# ---------------------------------------------------------------------------


def parse_sop_rules(sop_text: str, params: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rules: List[Dict[str, Any]] = []
    if not sop_text or not sop_text.strip():
        return rules
    sop_text, _ = to_english_phrases(sop_text)
    guarded = _guard_abbr(sop_text)
    sentences = [s.strip().replace(_ABBR_DOT, ".")
                 for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])|\n+", guarded) if s.strip()]
    for s in sentences:
        low = s.lower()
        concept = _concept_of(s)

        # --- role access ------------------------------------------------------------------
        m = re.search(r"(?:allowed|permitted|authori[sz]ed|approved)\s+roles?\s*(?:are|is|include|:|-)?\s*(.+)", s, re.I) or \
            re.search(r"\bonly\s+(?:users?\s+with\s+(?:the\s+)?(?:role\s+)?)?(.+?)\s+(?:role\s+|users?\s+|staff\s+|team\s+)?(?:may|can|are allowed to|is allowed to)\b", s, re.I) or \
            re.search(r"must\s+be\s+(?:submitted|performed|executed|run|done)\s+by\s+(?:a|an|the)?\s*(.+?)(?:\s+role)?(?:\.|$)", s, re.I)
        if m and ("role" in low or re.search(r"\bonly\b.+\b(may|can)\b", low) or "must be submitted by" in low
                  or "must be performed by" in low):
            vals = [v for v in _split_list(m.group(1)) if len(v.split()) <= 5]
            vals = [re.sub(r"\s+(role|roles|users?|staff)$", "", v, flags=re.I) for v in vals]
            if vals and not re.search(r"\b(vendor|supplier|customer|client)s?\b", m.group(1), re.I):
                field, bound = bind_field("role", params)
                rules.append(_mk(field, bound, "role_access", s, values=vals))
                continue

        # --- allowed values ---------------------------------------------------------------
        m = re.search(r"(?:approved|allowed|authori[sz]ed|permitted|whitelisted|valid|accepted|eligible)\s+" + _LIST_NOUNS +
                      r"\s*(?:list\s*)?(?:are|is|include|includes|:|-|=)?\s*(.+)", s, re.I) or \
            re.search(r"\bonly\s+(?:use|pay|accept|process|bill|invoice)?\s*(?:the\s+following\s+)?" + _LIST_NOUNS +
                      r"\s*(?:are allowed|may be used|:|-)?\s*[:\-]?\s*(.+)", s, re.I) or \
            re.search(r"\b([a-z][a-z _]{1,30}?)\s+must\s+be\s+one\s+of\s*:?\s*(.+)", s, re.I)
        if m and not re.search(r"\b(not|never|no)\b\s+(?:be\s+)?(?:approved|allowed)", low):
            noun, rest = m.group(1), m.group(2)
            vals = _split_list(rest)
            if vals:
                c = _concept_of(noun) or concept
                field, bound = bind_field(c, params)
                rules.append(_mk(field, bound, "allowed_values", s, values=vals))
                continue

        # --- denied values ----------------------------------------------------------------
        m = re.search(r"(?:blocked|banned|prohibited|blacklisted|forbidden|disallowed|sanctioned)\s+" + _LIST_NOUNS +
                      r"\s*(?:are|is|include|:|-)?\s*(.+)", s, re.I) or \
            re.search(r"(?:never|do\s+not|don't|must\s+not)\s+(?:pay|use|process|accept|bill)\s+(?:the\s+following\s+)?" +
                      _LIST_NOUNS + r"?\s*:?\s*(.+)", s, re.I)
        if m:
            noun = m.group(1) or ""
            vals = _split_list(m.group(2))
            if vals:
                c = _concept_of(noun) or concept
                field, bound = bind_field(c, params)
                rules.append(_mk(field, bound, "denied_values", s, values=vals))
                continue

        # --- date window -----------------------------------------------------------------
        m = re.search(r"(?:between|from)\s+" + DATE_RX + r"\s+(?:and|to|until|through|thru|-)\s+" + DATE_RX, s, re.I)
        if m and parse_date(m.group(1)) and parse_date(m.group(2)):
            field, bound = bind_field(concept if concept == "date" else "date", params, want_type="date")
            rules.append(_mk(field, bound, "date_range", s, start_date=parse_date(m.group(1)).isoformat(),
                             end_date=parse_date(m.group(2)).isoformat()))
            continue
        m_after = re.search(r"(?:on\s+or\s+after|no\s+earlier\s+than|not\s+before|after|starting|from)\s+" + DATE_RX, s, re.I)
        m_before = re.search(r"(?:on\s+or\s+before|no\s+later\s+than|not\s+after|before|by|until)\s+" + DATE_RX, s, re.I)
        if (m_after and parse_date(m_after.group(1))) or (m_before and parse_date(m_before.group(1))):
            field, bound = bind_field("date", params, want_type="date")
            rules.append(_mk(field, bound, "date_range", s,
                             start_date=parse_date(m_after.group(1)).isoformat() if m_after and parse_date(m_after.group(1)) else None,
                             end_date=parse_date(m_before.group(1)).isoformat() if m_before and parse_date(m_before.group(1)) else None))
            continue

        # --- numeric limits --------------------------------------------------------------
        money = r"(?:[$€£₹¥]|usd|eur|gbp|inr|rs\.?)?\s*([0-9][0-9,]*(?:\.[0-9]+)?\s*[kKmM]?)\b"
        negated_more = re.search(r"\b(not|never|no)\b[^.]{0,25}\b(more|greater|higher|above|over|exceed)", low)
        m_max = re.search(r"(?:spend(?:ing)?\s+limit|limit|maximum|max\.?|ceiling|cap|must\s+not\s+exceed|cannot\s+exceed|"
                          r"can't\s+exceed|may\s+not\s+exceed|not\s+exceed|no\s+more\s+than|up\s+to|at\s+most|"
                          r"not\s+be\s+(?:more|greater|higher)\s+than|less\s+than|under|below)" + _FILLER + r"\s*(?:of|is|:|at|=)?\s*" + money, s, re.I)
        m_min = re.search(r"(?:minimum|min\.?|at\s+least|no\s+less\s+than|more\s+than|greater\s+than|above|over)" + _FILLER + r"\s*(?:of|is|:|=)?\s*" + money, s, re.I)
        if m_max and (negated_more or not m_min or m_max.start() <= m_min.start()):
            val = parse_number(m_max.group(1))
            if val is not None:
                field, bound = bind_field(concept if concept not in (None, "vendor", "customer", "role") else "amount",
                                          params, want_type="number")
                excl = bool(re.search(r"(less\s+than|under|below)\s*(?:of|is|:)?\s*" + money, s, re.I))
                rules.append(_mk(field, bound, "max", s, limit=val, exclusive=excl))
                continue
        if m_min:
            val = parse_number(m_min.group(1))
            if val is not None:
                field, bound = bind_field(concept if concept not in (None, "vendor", "customer", "role") else "amount",
                                          params, want_type="number")
                # "Payments above $10k are not allowed / require approval" is a MAXIMUM, not a minimum.
                forbidden_tail = re.search(r"\b(not\s+(?:be\s+)?(?:allowed|permitted|approved|accepted)|prohibited|forbidden|"
                                           r"disallowed|blocked|requires?\s+(?:\w+\s+)?approval|must\s+be\s+escalated)\b",
                                           low[m_min.end():])
                if forbidden_tail and not re.search(r"at\s+least|minimum|min\.?|no\s+less", m_min.group(0), re.I):
                    rules.append(_mk(field, bound, "max", s, limit=val, exclusive=False))
                    continue
                excl = bool(re.search(r"(more|greater)\s+than|above|over", m_min.group(0), re.I))
                rules.append(_mk(field, bound, "min", s, limit=val, exclusive=excl))
                continue

        # --- formats ---------------------------------------------------------------------
        m = re.search(r"\b([a-z][a-z _]{1,30}?)\s+must\s+(start|begin|end)\s+with\s+[\"'“]?([^\"'”\s]+)", s, re.I)
        if m:
            field, bound = bind_field(_concept_of(m.group(1)) or m.group(1).strip().lower().replace(" ", "_"), params)
            prefix = re.escape(m.group(3).rstrip("."))
            pattern = f"^{prefix}" if m.group(2).lower() in ("start", "begin") else f"{prefix}$"
            r = _mk(field, bound, "regex", s, pattern=pattern)
            r["plain_english"] = f"'{field}' must {m.group(2).lower()} with '{m.group(3).rstrip('.')}'."
            rules.append(r)
            continue
        m = re.search(r"\b([a-z][a-z _]{1,30}?)\s+(?:is|are)\s+(?:required|mandatory)|\b([a-z][a-z _]{1,30}?)\s+must\s+(?:always\s+)?be\s+(?:provided|filled|included|present)", s, re.I)
        if m:
            noun = (m.group(1) or m.group(2)).strip()
            c = _concept_of(noun)
            field, bound = bind_field(c or noun.lower().replace(" ", "_"), params)
            rules.append(_mk(field, bound, "required", s))
            continue
    return _dedupe(rules)


def _dedupe(rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen, out = set(), []
    for r in rules:
        key = (r["field"], r["rule_type"], tuple(sorted(v.lower() for v in r.get("values") or [])), r.get("limit"),
               r.get("start_date"), r.get("end_date"), r.get("pattern"))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def normalize_rule(raw: Dict[str, Any], params: List[Dict[str, Any]], source: str = "user") -> Dict[str, Any]:
    """Validates/cleans a rule coming from the AI or edited by the user."""
    rt = str(raw.get("rule_type", "")).strip()
    if rt not in RULE_TYPES:
        raise ValueError(f"Unknown rule type '{rt}'")
    field = re.sub(r"[^a-z0-9_]", "_", str(raw.get("field", "")).strip().lower())[:64] or "field"
    keys = {p["key"] for p in params}
    vals = [str(v).strip()[:200] for v in (raw.get("values") or []) if str(v).strip()][:50]
    limit = parse_number(raw.get("limit")) if raw.get("limit") not in (None, "") else None
    sd = parse_date(raw.get("start_date")) if raw.get("start_date") else None
    ed = parse_date(raw.get("end_date")) if raw.get("end_date") else None
    pattern = str(raw.get("pattern") or "")[:200] or None
    if rt in ("allowed_values", "denied_values", "role_access") and not vals:
        raise ValueError("This rule needs at least one value")
    if rt in ("max", "min") and limit is None:
        raise ValueError("This rule needs a numeric limit")
    if rt == "date_range" and not (sd or ed):
        raise ValueError("This rule needs a start and/or end date")
    if rt == "regex":
        try:
            re.compile(pattern or "")
        except re.error as e:
            raise ValueError(f"Invalid pattern: {e}")
    r = {"rule_id": str(raw.get("rule_id") or _new_id())[:20], "field": field, "bound": field in keys, "rule_type": rt,
         "values": vals, "limit": limit, "exclusive": bool(raw.get("exclusive", False)),
         "start_date": sd.isoformat() if sd else None, "end_date": ed.isoformat() if ed else None, "pattern": pattern,
         "source_text": str(raw.get("source_text") or "")[:300], "source": raw.get("source") or source,
         "enabled": bool(raw.get("enabled", True))}
    r["plain_english"] = plain_english(r)
    return r


def extract_rules(sop_text: str, params: List[Dict[str, Any]], session_id: Optional[str] = None) -> Dict[str, Any]:
    """AI + parser extraction. Returns {rules, agent_source, model, security_flags, notes}."""
    from core.wizard.llm import generate_json

    parser_rules = parse_sop_rules(sop_text, params)
    meta: Dict[str, Any] = {"agent_source": "LOCAL_HEURISTIC", "model": "Deterministic SOP parser", "security_flags": [],
                            "notes": []}
    res = generate_json("GOVERN_EXTRACT_RULES",
                        {"parameter_keys": [{"key": p["key"], "label": p.get("label"), "type": p.get("type")} for p in params],
                         "sop_text": sop_text or ""},
                        untrusted=["sop_text"], item_schema_wrapper="rules")
    ai_rules: List[Dict[str, Any]] = []
    if res.ok:
        meta.update({"agent_source": res.agent_source, "model": res.model, "recall": res.recall,
                     "security_flags": list((res.data or {}).get("security_flags") or [])[:10]})
        if res.validation_errors:
            meta["notes"].append(f"Dropped an AI rule that failed schema validation ({res.validation_errors[0]}).")
        for raw in res.data.get("rules", []):
            try:
                ai_rules.append(normalize_rule(raw, params, source="ai"))
            except ValueError as e:
                meta["notes"].append(f"Dropped an AI rule: {e}")
    else:
        meta["notes"].append(f"AI extraction unavailable ({res.error}); used the deterministic parser.")
        from core.wizard.llm import record_fallback
        record_fallback("GOVERN_EXTRACT_RULES", res.error or "AI unavailable", parser_rules,
                        "Deterministic SOP rule parser.")
    lang = sop_language(sop_text)
    meta["sop_language"] = lang
    if lang != "en" and meta["agent_source"] != "ACTUAL_AI_API":
        _, mapped = to_english_phrases(sop_text)
        name = _LANG_NAMES.get(lang, lang)
        if not (ai_rules or parser_rules):
            meta["notes"].append(f"Your SOP looks like {name}. Without AI, Understudy could not read any rule from it - "
                                 "add the rules below by hand (or set up Gemini, which reads SOPs in any language) before "
                                 "confirming. Nothing is checked until you do.")
            meta["language_warning"] = True
        else:
            meta["notes"].append(f"Your SOP looks like {name}. Without AI, Understudy read it through its built-in "
                                 f"{name} rule phrases ({mapped} matched) - please check every rule below carefully.")
    merged = _dedupe(ai_rules + parser_rules)
    for r in merged:
        r["agreement"] = "both" if (r in ai_rules and any(_same(r, p) for p in parser_rules)) else r["source"]
        if r["source"] == "ai" and meta["agent_source"] != "ACTUAL_AI_API":
            r["source"] = "learned"
            if r["agreement"] == "ai":
                r["agreement"] = "learned"
    return {"rules": merged, **meta}


def _same(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return a["field"] == b["field"] and a["rule_type"] == b["rule_type"]


# ---------------------------------------------------------------------------
# Compliance evaluation (authoritative)
# ---------------------------------------------------------------------------
def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def evaluate_compliance(rules: List[Dict[str, Any]], params: List[Dict[str, Any]]) -> Dict[str, Any]:
    values = {p["key"]: p.get("value") for p in params}
    violations: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    checked = 0
    for r in rules:
        if not r.get("enabled", True):
            continue
        f, t = r["field"], r["rule_type"]
        if f not in values:
            if t in ("role_access", "required"):
                violations.append({"rule_id": r["rule_id"], "field": f, "kind": "MISSING_PARAMETER", "value": None,
                                   "message": f"The SOP says: \"{r['source_text'] or r['plain_english']}\" but your payload has no "
                                              f"'{f}' parameter.",
                                   "fix_hint": f"Add a '{f}' parameter in the payload table (Step 2) or link this rule to "
                                               f"an existing parameter.", "rule": r["plain_english"]})
            else:
                warnings.append({"rule_id": r["rule_id"], "field": f,
                                 "message": f"Rule \"{r['plain_english']}\" is not linked to any parameter, so it can't be checked. "
                                            f"Link it to a parameter or delete it."})
            continue
        checked += 1
        v = values[f]
        sv = "" if v is None else str(v).strip()
        problem: Optional[str] = None
        hint = ""
        if t in ("allowed_values", "role_access"):
            allowed = [_norm(x) for x in r.get("values") or []]
            if _norm(sv) not in allowed:
                problem = f"'{sv or '(empty)'}' is not an approved value for {f}."
                hint = f"Change it to one of: {', '.join(r.get('values') or [])} - or update the SOP if '{sv}' should be allowed."
        elif t == "denied_values":
            for bad in r.get("values") or []:
                if _norm(bad) and (_norm(bad) == _norm(sv) or _norm(bad) in _norm(sv)):
                    problem = f"'{sv}' is on the SOP's blocked list for {f}."
                    hint = "Use a different value, or update the SOP if this block no longer applies."
                    break
        elif t in ("max", "min"):
            num = parse_number(v)
            lim = float(r.get("limit") or 0)
            if num is None:
                problem = f"{f} must be a number to check the {'limit' if t == 'max' else 'minimum'} ({sv or 'empty'})."
                hint = "Enter a plain number such as 1250.50."
            elif t == "max" and (num >= lim if r.get("exclusive") else num > lim):
                problem = f"{f} = {_fmt_money(num)} is over the SOP limit of {_fmt_money(lim)}."
                hint = f"Lower it to {'below' if r.get('exclusive') else 'at most'} {_fmt_money(lim)}, or update the SOP limit."
            elif t == "min" and (num <= lim if r.get("exclusive") else num < lim):
                problem = f"{f} = {_fmt_money(num)} is under the SOP minimum of {_fmt_money(lim)}."
                hint = f"Raise it to {'above' if r.get('exclusive') else 'at least'} {_fmt_money(lim)}, or update the SOP."
        elif t == "date_range":
            d = parse_date(v)
            s, e = parse_date(r.get("start_date")), parse_date(r.get("end_date"))
            if d is None:
                problem = f"{f} ('{sv}') is not a valid date."
                hint = "Use the YYYY-MM-DD format, e.g. 2026-09-23."
            elif (s and d < s) or (e and d > e):
                problem = f"{f} = {d.isoformat()} is outside the allowed window ({s or 'any'} to {e or 'any'})."
                hint = "Pick a date inside the window, or update the SOP's date range."
        elif t == "regex":
            try:
                if not re.search(r.get("pattern") or "", sv[:2000]):
                    problem = f"{f} ('{sv}') does not match the required format {r.get('pattern')}."
                    hint = "Change the value to match the format described in the SOP."
            except re.error:
                warnings.append({"rule_id": r["rule_id"], "field": f, "message": "Invalid pattern; rule skipped."})
        elif t == "required":
            if not sv:
                problem = f"{f} is required by the SOP but is empty."
                hint = "Fill in a value."
        if problem:
            violations.append({"rule_id": r["rule_id"], "field": f, "kind": t.upper(), "value": v, "message": problem,
                               "fix_hint": hint, "rule": r["plain_english"], "source_text": r.get("source_text", "")})
    return {"passed": not violations, "violations": violations, "warnings": warnings, "rules_checked": checked}


# ---------------------------------------------------------------------------
# Generated-script safety scan
# ---------------------------------------------------------------------------
DANGEROUS = [
    (r"\bos\.system\s*\(", "Runs shell commands"),
    (r"\bsubprocess\b", "Starts other programs"),
    (r"(?<![\w.])eval\s*\(", "Executes dynamic code (eval)"),
    (r"(?<![\w.])exec\s*\(", "Executes dynamic code (exec)"),
    (r"__import__\s*\(", "Dynamic import"),
    (r"child_process", "Starts other programs (Node child_process)"),
    (r"Runtime\.getRuntime\(\)\.exec|ProcessBuilder", "Starts other programs (Java)"),
    (r"\brm\s+-rf\b|shutil\.rmtree|fs\.rm(Sync)?\s*\(|\bunlink(Sync)?\s*\(|os\.remove\s*\(", "Deletes files"),
    (r"(?i)\bdrop\s+table\b|\btruncate\s+table\b", "Destructive database command"),
    (r"pickle\.loads|yaml\.load\((?!.*SafeLoader)", "Unsafe deserialisation"),
    (r"(?i)Run\s+Process|Start\s+Process|modules=\s*(os|subprocess|shutil)\b", "Robot keyword that runs programs"),
    (r"new\s+Function\s*\(", "Executes dynamic code (new Function)"),
]
SECRET_PATTERNS = [
    (r"AIza[0-9A-Za-z_-]{30,}", "Google API key"),
    (r"\bsk-[A-Za-z0-9]{20,}", "API secret key"),
    (r"\bghp_[A-Za-z0-9]{30,}", "GitHub token"),
    (r"\bAKIA[0-9A-Z]{16}\b", "AWS access key"),
    (r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----", "Private key"),
    (r"(?i)\b(password|passwd|pwd|api[_-]?key|secret|token|bearer)\b\s*[:=]\s*[\"'][^\"'\s{$]{6,}[\"']", "Hardcoded credential"),
    (r"(?i)authorization[\"']?\s*[:=,]\s*[\"'](bearer|basic)\s+[A-Za-z0-9._~+/-]{8,}", "Hardcoded authorization header"),
]
SAFE_DOC_HOSTS = {"www.w3.org", "maven.apache.org", "rubygems.org", "json-schema.org", "localhost", "127.0.0.1",
                  "example.com", "www.example.com", "schemas.microsoft.com"}


# 8.2: Cypress 16 removed Cypress.env(); its replacements don't exist before 15.10 - generated code must use neither.
CYPRESS_ENV_RX = re.compile(r"\bCypress\.(?:env|expose)\s*\(|\bcy\.env\s*\(")


def scan_suite(files: Dict[str, str], params: List[Dict[str, Any]], target_url: Any,
               steps: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """``target_url`` may be a list (9.4: an automation that visits several pages) - every page's host is allowed."""
    findings: List[Dict[str, Any]] = []
    urls = [target_url] if isinstance(target_url, str) else list(target_url or [])
    target_hosts = {(urlsplit(u).hostname or "").lower() for u in urls if u} - {""}
    target_host = ", ".join(sorted(target_hosts))
    step_literals = " ".join(" ".join([str(s.get("selector") or ""), " ".join(s.get("fallback_selectors") or []),
                                       str(s.get("label") or ""), str(s.get("description") or ""), str(s.get("value") or "")])
                             for s in (steps or []))
    if len(files) > 30:
        findings.append({"file": "*", "line": 0, "kind": "TOO_MANY_FILES", "severity": "blocking",
                         "message": "Generated suite has more than 30 files."})
    from core.wizard import governance_runtime
    for path, content in files.items():
        if governance_runtime.is_vetted(path, content):
            continue                              # 9.9: Understudy's own rule file / checker (unchanged)
        if not re.match(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,150}$", path) or ".." in path:
            findings.append({"file": path, "line": 0, "kind": "UNSAFE_PATH", "severity": "blocking",
                             "message": "File path must be a simple relative path."})
            continue
        if len(content) > 200_000:
            findings.append({"file": path, "line": 0, "kind": "TOO_LARGE", "severity": "blocking",
                             "message": "File is larger than 200 KB."})
        lines = content.splitlines()
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith(("#", "//", "*", "<!--")) and "http" not in stripped:
                continue
            for rx, why in DANGEROUS:
                if re.search(rx, line):
                    findings.append({"file": path, "line": i, "kind": "DANGEROUS_CALL", "severity": "blocking",
                                     "message": f"{why}: `{stripped[:120]}`"})
            for rx, why in SECRET_PATTERNS:
                if re.search(rx, line):
                    findings.append({"file": path, "line": i, "kind": "HARDCODED_SECRET", "severity": "blocking",
                                     "message": f"{why} found in code - use an environment variable instead."})
            if CYPRESS_ENV_RX.search(line):
                findings.append({"file": path, "line": i, "kind": "REMOVED_API", "severity": "blocking",
                                 "message": "Cypress.env() / Cypress.expose() / cy.env() - removed in Cypress 16 or "
                                            "missing before 15.10. Read environment variables in cypress.config.js and "
                                            "pass them to the spec with cy.task('understudyEnv')."})
            for m in re.finditer(r"https?://([A-Za-z0-9.-]+)", line):
                host = m.group(1).lower()
                if host not in target_hosts and host not in SAFE_DOC_HOSTS and not host.endswith(".apache.org"):
                    findings.append({"file": path, "line": i, "kind": "UNEXPECTED_HOST", "severity": "blocking",
                                     "message": f"Code talks to '{host}', which is not "
                                                + (f"the target host '{target_host}'." if len(target_hosts) < 2
                                                   else f"one of the target hosts ({target_host}).")})
        from core.wizard import agentic_templates
        if agentic_templates.is_vetted_runner(content):          # 9.8: fixed agent runner - holds no data
            content = ""
        full = content
        if path.endswith(".py"):                  # 9.9: a comment changes nothing the code does - values are checked in code
            content = "\n".join(ln for ln in content.splitlines() if not ln.strip().startswith("#"))
        for p in params:
            val = p.get("value")
            if val is None or isinstance(val, bool):
                continue
            sval = str(val).strip()
            if p.get("type") == "secret":
                if sval and len(sval) >= 4 and sval in full:
                    findings.append({"file": path, "line": 0, "kind": "HARDCODED_SECRET", "severity": "blocking",
                                     "message": f"The secret value of '{p['key']}' is written into the code."})
                continue
            if len(sval) < 4 or re.fullmatch(r"[0-9.]{1,6}", sval) or sval.lower() in ("true", "false", "none", "null"):
                continue
            if sval in step_literals:
                continue
            if re.search(r"(?<![\w-])" + re.escape(sval) + r"(?![\w-])", content):
                findings.append({"file": path, "line": 0, "kind": "HARDCODED_PARAMETER", "severity": "blocking",
                                 "message": f"The value of '{p['key']}' ('{sval[:40]}') is hardcoded - it must come from payload.json."})
    return findings


# ---------------------------------------------------------------------------
# Full GOVERN check
# ---------------------------------------------------------------------------
def run_compliance_check(rules: List[Dict[str, Any]], params: List[Dict[str, Any]], sop_text: str,
                         suite_files: Dict[str, str], target_url: Any, steps: List[Dict[str, Any]]) -> Dict[str, Any]:
    from core.wizard.llm import generate_json

    result = evaluate_compliance(rules, params)
    scan = scan_suite(suite_files or {}, params, target_url, steps)
    blocking_scan = [f for f in scan if f["severity"] == "blocking"]
    for f in blocking_scan:
        result["violations"].append({"rule_id": "SCRIPT-SAFETY", "field": f["file"], "kind": f["kind"], "value": None,
                                     "message": f"Generated code problem in {f['file']}"
                                                + (f" line {f['line']}" if f.get("line") else "") + f": {f['message']}",
                                     "fix_hint": "Go back to Step 3 (Build) and regenerate the code (the template generator never "
                                                 "produces this).", "rule": "Generated code must be safe and parameterised"})
    result["script_findings"] = scan
    # An SOP Understudy could not read (another language, no AI) must never pass silently with zero rules.
    enabled = [r for r in rules if r.get("enabled", True)]
    if (sop_text or "").strip() and not enabled and sop_language(sop_text) != "en":
        from core.wizard.llm import ai_available
        if not ai_available():
            name = _LANG_NAMES.get(sop_language(sop_text), "another language")
            result["violations"].append({
                "rule_id": "SOP-NOT-UNDERSTOOD", "field": "sop", "kind": "SOP_NOT_UNDERSTOOD", "value": None,
                "message": f"Your SOP is written in {name}, but no rule could be read from it without AI, so nothing "
                           "would be checked.",
                "fix_hint": "Add the rules by hand above, rewrite the SOP (Update SOP), or set up Gemini - it reads SOPs "
                            "in any language.", "rule": "An SOP with content must produce rules to check"})
    result["passed"] = not result["violations"]
    result["ai_review"] = {"agent_source": "LOCAL_HEURISTIC", "warnings": []}
    if rules:
        payload_view = {p["key"]: ("***" if p.get("type") == "secret" else p.get("value")) for p in params}
        res = generate_json("GOVERN_COMPLIANCE_REVIEW",
                            {"rules": [{k: r[k] for k in ("field", "rule_type", "values", "limit", "start_date", "end_date",
                                                           "pattern", "plain_english")} for r in rules if r.get("enabled", True)],
                             "payload": payload_view, "sop_text": sop_text or ""},
                            untrusted=["payload", "sop_text"])
        if res.ok:
            result["ai_review"] = {"agent_source": res.agent_source, "model": res.model,
                                   "warnings": list(res.data.get("warnings") or [])[:10],
                                   "security_flags": list(res.data.get("security_flags") or [])[:10]}
        else:
            result["ai_review"]["note"] = f"AI advisory review unavailable ({res.error})."
            from core.wizard.llm import record_fallback
            record_fallback("GOVERN_COMPLIANCE_REVIEW", res.error or "AI unavailable",
                            {"passed": result["passed"], "violations": result["violations"]},
                            "Deterministic rule engine result only (AI review is advisory).")
    return result
