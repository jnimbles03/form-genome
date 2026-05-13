# app/services/triage.py
"""
Cheap text-only triage step between crawl-discovery and full analysis.

Goal: from a list of discovered PDF URLs (often dozens to hundreds), keep only
the ones that are actually fillable forms — drop annual reports, marketing
collateral, brochures, fact sheets, etc. before they get pushed through the
expensive vision analyzer.

Uses Gemini Flash text-only — ~$0.0001 per call, ~1s latency. Classification
is one of:

    form           — fillable form: enrollment, claim, application, consent,
                     authorization, request, prescription, etc.
    disclosure     — required regulatory disclosure (privacy notice,
                     HIPAA-style notice, terms doc)
    report         — corporate report: annual report, sustainability report,
                     financial filing, study results
    marketing      — brochure, fact sheet, infographic, newsletter, sell-sheet
    instructions   — instructions/help docs that accompany a form but are
                     not themselves fillable

The pipeline keeps only `form` with confidence >= TRIAGE_KEEP_THRESHOLD
(default 0.75). The rest are saved to the DB with a `triage_classification`
field so the dashboard can show what was filtered.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, Optional

from app.services.llm_router import chat_complete, LLMError

logger = logging.getLogger(__name__)

# Tunable via env vars
TRIAGE_PROVIDER = os.getenv("TRIAGE_PROVIDER", "gemini").strip().lower()
TRIAGE_MODEL = (os.getenv("TRIAGE_MODEL") or "").strip() or None
TRIAGE_KEEP_THRESHOLD = float(os.getenv("TRIAGE_KEEP_THRESHOLD", "0.75"))
TRIAGE_TIMEOUT_SEC = float(os.getenv("TRIAGE_TIMEOUT_SEC", "15.0"))

CLASSES = ("form", "disclosure", "report", "marketing", "instructions")

# Classes the analyzer should actually deep-analyze.
KEEPER_CLASSES = ("form",)


_PROMPT = """You are classifying a PDF document so that an expensive form-analysis
pipeline only runs on real fillable forms. Pick the SINGLE best label from this
fixed list:

  form           — fillable form a user fills out and submits:
                   applications, registrations, claims, enrollment, consent,
                   authorization, prescription, order, request, attestation
  disclosure     — regulatory disclosure document (privacy notice, HIPAA,
                   terms, EFPIA disclosure)
  report         — corporate / financial / sustainability / annual / study
                   report; not actionable
  marketing      — brochure, fact sheet, infographic, newsletter, sell-sheet
  instructions   — help/instruction document that ACCOMPANIES a form but is
                   not itself fillable

INPUTS:
  URL: {url}
  Filename: {filename}
  PDF /Title: {title}
  Pages: {pages}
  Has AcroForm widgets: {has_widgets}
  First ~2000 chars of extracted text:
  ---
  {text}
  ---

Respond as MINIFIED JSON only (no prose, no markdown fences):
  {{"classification":"<one of: form|disclosure|report|marketing|instructions>",
    "confidence":<float 0..1>,
    "reasoning":"<one sentence under 25 words>"}}
"""


def _coerce_class(s: str) -> str:
    s = (s or "").strip().lower()
    for c in CLASSES:
        if s == c or s.startswith(c):
            return c
    # tolerate common variants
    if "form" in s:
        return "form"
    if "disclos" in s:
        return "disclosure"
    if "report" in s or "annual" in s:
        return "report"
    if "market" in s or "brochure" in s or "fact" in s:
        return "marketing"
    if "instruct" in s or "help" in s or "guide" in s:
        return "instructions"
    return "report"  # safe default — won't push through analyzer


def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    # Strip code fences if present
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    raw = m.group(1) if m else text
    # Find first balanced JSON object
    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    for i, ch in enumerate(raw[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def classify(
    *,
    url: str,
    filename: str = "",
    title: str = "",
    pages: int = 0,
    has_widgets: bool = False,
    text: str = "",
) -> Dict[str, Any]:
    """
    Classify a single PDF. Returns:
        {"classification": str, "confidence": float, "reasoning": str,
         "should_analyze": bool, "error": Optional[str]}
    """
    # Trim text to ~2000 chars (Gemini Flash is fast on small inputs)
    text_head = (text or "")[:2000]

    prompt = _PROMPT.format(
        url=url[:300],
        filename=filename[:200] or url.rsplit("/", 1)[-1][:200],
        title=(title or "(none)")[:200],
        pages=pages or 0,
        has_widgets="yes" if has_widgets else "no",
        text=text_head or "(no extractable text)",
    )

    try:
        raw = chat_complete(
            provider=TRIAGE_PROVIDER,
            model=TRIAGE_MODEL or "",
            messages=[
                {"role": "system", "content": "You are a precise document classifier. Reply with JSON only."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=200,
            temperature=0.0,
            timeout=TRIAGE_TIMEOUT_SEC,
            retries=1,
            fallback=True,
        )
    except LLMError as e:
        logger.warning("[TRIAGE] LLM error for %s: %s", url, e)
        # Fail open — let it through to analyzer, mark uncertain.
        return {
            "classification": "form",
            "confidence": 0.0,
            "reasoning": f"triage_llm_error: {e}",
            "should_analyze": True,
            "error": str(e),
        }

    parsed = _extract_json(raw)
    if not parsed:
        logger.warning("[TRIAGE] Could not parse JSON for %s: %r", url, raw[:200])
        return {
            "classification": "form",  # fail open
            "confidence": 0.0,
            "reasoning": "triage_parse_error",
            "should_analyze": True,
            "error": "parse_error",
            "raw": raw[:500],
        }

    cls = _coerce_class(parsed.get("classification"))
    try:
        conf = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    reasoning = str(parsed.get("reasoning", ""))[:300]

    should_analyze = (cls in KEEPER_CLASSES) and (conf >= TRIAGE_KEEP_THRESHOLD)

    return {
        "classification": cls,
        "confidence": conf,
        "reasoning": reasoning,
        "should_analyze": should_analyze,
        "error": None,
    }
