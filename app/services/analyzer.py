# app/services/analyzer.py
# PDF analyzer: fetch bytes, verify PDF early, extract structure & signals,
# compute Complexity + NIGO, build LLM-first tidy title (+ title_debug),
# determine action_type, and return a normalized record.

from __future__ import annotations
import io
import os
import re
import math
import time
import hashlib
import requests
from typing import Dict, Any, Optional, Tuple, List
from urllib.parse import urlparse

from pypdf import PdfReader
from pypdf.generic import DictionaryObject

# --- Optional LLM title helper (safe no-op if missing) ---
try:
    from app.services.title_llm import make_title  # uses configured LLM provider
except Exception:  # pragma: no cover
    def make_title(url: str, first_page_text: str) -> tuple[str, str]:  # type: ignore
        return "", ""

# --- Vision analysis for flat/scanned PDFs (safe no-op if missing) ---
try:
    from app.services.pdf_vision_analyzer import (
        analyze_flat_pdf_with_vision,
        merge_vision_results_into_record
    )
except ImportError:
    def analyze_flat_pdf_with_vision(*args, **kwargs):  # type: ignore
        return {"is_actionable": True, "vision_analyzed": False, "reason": "not_available"}
    def merge_vision_results_into_record(record, vision_data):  # type: ignore
        return record

# --- Domain-based metadata mappings ---
from app.services import domain_mappings

# -------------------------------------------------------------------
# Utilities
# -------------------------------------------------------------------
def _now() -> int:
    return int(time.time())

def _filename_from_url(u: str) -> str:
    try:
        path = urlparse(u).path
        name = os.path.basename(path) or "document.pdf"
        return name
    except Exception:
        return "document.pdf"

# -------------------------------------------------------------------
# Robust HTTP fetch (normalize URL, retries, stream retry, CA bundle)
# -------------------------------------------------------------------
import urllib.parse as up
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import certifi

# F-CS-10: honest UA, single source of truth in app.services.politeness.
from app.services import politeness

HEADERS = {
    "User-Agent": politeness.USER_AGENT,
    "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
    "Connection": "close",
}

def _normalize_url(url: str) -> str:
    if not url:
        return url
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    p = up.urlsplit(url)
    host = p.hostname or ""
    netloc = p.netloc
    path = p.path
    if "%2F" in host.upper():
        decoded = up.unquote(host)
        if "/" in decoded:
            host_only, extra_path = decoded.split("/", 1)
            netloc = host_only
            path = "/" + extra_path
        else:
            netloc = decoded
    path = up.quote(up.unquote(path), safe="/:@-._~!$&'()*+,;=")
    return up.urlunsplit((p.scheme or "https", netloc, path, p.query, p.fragment))

def _requests_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.6,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=("HEAD", "GET", "OPTIONS"),
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    return s

def _looks_like_pdf(ct: str, data_head: bytes) -> bool:
    """
    True if the response is a PDF by either content-type or file magic.
    """
    if "pdf" in (ct or "").lower():
        return True
    return data_head[:5] == b"%PDF-"

def _safe_get(url: str, timeout: int = 25, max_mb: int = 120) -> bytes:
    """
    GET with size guard, redirects, and robust retries.
    Verifies PDF early: content-type OR %PDF- magic. Skips HTML/blocked pages cleanly.
    """
    if not url:
        raise ValueError("No URL provided")
    url = _normalize_url(url)
    max_bytes = max_mb * 1024 * 1024

    s = _requests_session()
    verify = os.getenv("REQUESTS_CA_BUNDLE") or os.getenv("SSL_CERT_FILE") or certifi.where()

    # F-CS-10: removed per-vendor Referer shims (Schwab, TSP). Honest
    # UA + politeness layer should pass the same WAFs; if a specific
    # site blocks us, escalate case-by-case rather than re-introducing
    # impersonation defaults.
    headers = dict(HEADERS)

    # 1) HEAD (best effort) to detect 4xx and huge files fast
    try:
        hr = s.head(url, headers=headers, allow_redirects=True, timeout=timeout, verify=verify)
        if 400 <= hr.status_code < 500:
            raise requests.HTTPError(f"{hr.status_code} Client Error: {hr.reason} for url: {url}")
        clen = hr.headers.get("Content-Length")
        if clen and int(clen) > max_bytes:
            raise ValueError(f"PDF exceeds size guard ({max_mb} MB)")
    except requests.HTTPError:
        raise
    except Exception:
        pass

    # 2) GET streamed, with early PDF detection using CT or %PDF- magic
    try:
        with s.get(url, headers=headers, stream=True, timeout=timeout, allow_redirects=True, verify=verify) as r:
            r.raise_for_status()
            ct = (r.headers.get("Content-Type") or "")
            total = 0
            chunks = []
            head_checked = False
            for chunk in r.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"PDF exceeds size guard ({max_mb} MB)")
                if not head_checked and total >= 8:
                    head_checked = True
                    if not _looks_like_pdf(ct, chunks[0][:8]):
                        # Check if it's an HTML error page
                        first_bytes = chunks[0][:200]
                        if first_bytes[:15].lower().startswith(b'<!doctype html') or first_bytes[:6].lower() == b'<html>':
                            snippet = first_bytes.decode('utf-8', errors='ignore')
                            raise ValueError(f"Server returned HTML error page instead of PDF: {snippet}")
                        raise ValueError(f"Not a PDF (content-type={ct or 'unknown'})")
            if total == 0:
                raise IOError("Empty stream")
            data = b"".join(chunks)
            if not _looks_like_pdf(ct, data[:8]):
                # Check if it's an HTML error page
                if data[:15].lower().startswith(b'<!doctype html') or data[:6].lower() == b'<html>':
                    snippet = data[:200].decode('utf-8', errors='ignore')
                    raise ValueError(f"Server returned HTML error page instead of PDF: {snippet}")
                raise ValueError(f"Not a PDF (content-type={ct or 'unknown'})")
            return data
    except Exception as e:
        with s.get(url, headers=headers, stream=False, timeout=timeout, allow_redirects=True, verify=verify) as r:
            r.raise_for_status()
            data = r.content or b""
            ct = (r.headers.get("Content-Type") or "")
            if len(data) == 0:
                raise IOError(f"Stream retry failed: {e}")
            if len(data) > max_bytes:
                raise ValueError(f"PDF exceeds size guard ({max_mb} MB)")
            if not _looks_like_pdf(ct, data[:8]):
                # Check if it's an HTML error page
                if data[:15].lower().startswith(b'<!doctype html') or data[:6].lower() == b'<html>':
                    snippet = data[:200].decode('utf-8', errors='ignore')
                    raise ValueError(f"Server returned HTML error page instead of PDF: {snippet}")
                raise ValueError(f"Not a PDF (content-type={ct or 'unknown'})")
            return data

# -------------------------------------------------------------------
# PDF open/decrypt
# -------------------------------------------------------------------
def _open_reader(pdf_bytes: bytes) -> PdfReader:
    return PdfReader(io.BytesIO(pdf_bytes))

def _ensure_decrypted(reader: PdfReader) -> None:
    """Try to open encrypted PDFs with an empty password. Raise clean error if locked."""
    if getattr(reader, "is_encrypted", False):
        for pw in ("", b"", None):
            try:
                res = reader.decrypt(pw)
                if res:
                    return
            except Exception:
                pass
        raise ValueError("Encrypted PDF cannot be analyzed without a password")

# -------------------------------------------------------------------
# Extraction helpers
# -------------------------------------------------------------------
def extract_pages_text_images_xfa(reader: PdfReader) -> Tuple[int, str, int, bool, int]:
    pages = len(reader.pages) if reader.pages else 0
    text_parts: List[str] = []
    image_count = 0
    radio_groups = 0
    xfa_hint = False

    try:
        af = reader.trailer["/Root"].get("/AcroForm")
        if isinstance(af, DictionaryObject):
            if af.get("/XFA") is not None:
                xfa_hint = True
    except Exception:
        pass

    for i in range(pages):
        try:
            p = reader.pages[i]
            txt = ""
            try:
                txt = p.extract_text() or ""
            except Exception:
                txt = ""
            if txt:
                text_parts.append(txt)
            try:
                res = p.get("/Resources") or {}
                xobj = res.get("/XObject") or {}
                if hasattr(xobj, "items"):
                    for _, o in xobj.items():
                        try:
                            if o.get("/Subtype") == "/Image":
                                image_count += 1
                        except Exception:
                            pass
            except Exception:
                pass
        except Exception:
            continue

    all_text = "\n".join(text_parts)
    radio_groups = len(re.findall(r"\b(Yes|No)\b", all_text, flags=re.I)) // 2

    return pages, all_text, image_count, xfa_hint, radio_groups

def extract_form_widgets(reader: PdfReader) -> Dict[str, int]:
    """
    Enhanced signature detection with nested field support, witness/co-signer patterns,
    and digital signature detection. Includes deduplication to prevent double-counting
    signatures that appear in both AcroForm fields and annotations.
    """
    counts = {
        "field_count": 0,
        "text_fields": 0,
        "checkboxes": 0,
        "dropdowns": 0,
        "signature_count": 0,
        "witness_signature_count": 0,
        "conditional_signature_count": 0,
        "digital_signature_count": 0,
    }

    # Track seen signature fields to prevent double-counting
    seen_signatures = set()

    def _process_field(field, parent_name: str = "") -> None:
        """Recursively process PDF fields including nested structures"""
        try:
            # Get field name (may be inherited from parent)
            fname_obj = field.get("/T")
            fname = (fname_obj or "").lower() if fname_obj else ""
            full_name = f"{parent_name}.{fname}" if parent_name else fname

            # Get field type
            ft = field.get("/FT")

            # Count field types
            if ft == "/Tx":
                counts["text_fields"] += 1
                counts["field_count"] += 1
            elif ft == "/Btn":
                counts["checkboxes"] += 1
                counts["field_count"] += 1
            elif ft == "/Ch":
                counts["dropdowns"] += 1
                counts["field_count"] += 1
            elif ft == "/Sig":
                # Create unique identifier for this signature field (using field name)
                sig_id = full_name or f"sig_{id(field)}"

                # Only count if not already seen (prevents double-counting)
                if sig_id not in seen_signatures:
                    seen_signatures.add(sig_id)
                    counts["signature_count"] += 1
                    counts["field_count"] += 1

                    # Enhanced witness/co-signer detection with comprehensive patterns
                    witness_patterns = [
                        "witness", "cosigner", "co-signer", "co signer",
                        "joint", "secondary", "additional signer",
                        "spouse", "guarantor", "co-applicant", "co-maker",
                        "witness signature", "witness name", "witness date"
                    ]
                    if any(pattern in full_name for pattern in witness_patterns):
                        counts["witness_signature_count"] += 1

                    # Check for digital signature (has /V value or /Lock)
                    if field.get("/V") or field.get("/Lock"):
                        counts["digital_signature_count"] += 1

            # Process nested fields (Kids array)
            kids = field.get("/Kids")
            if kids:
                for kid in kids:
                    if isinstance(kid, DictionaryObject):
                        _process_field(kid, full_name)

        except Exception:
            # Skip malformed fields
            pass

    try:
        root = reader.trailer["/Root"]
        af = root.get("/AcroForm")
        if not af:
            # No AcroForm, check for signature annotations
            _check_signature_annotations(reader, counts, seen_signatures)
            return counts

        fields = af.get("/Fields") or []
        for f in fields:
            _process_field(f)

        # Also check for signature annotations (non-AcroForm signatures)
        _check_signature_annotations(reader, counts, seen_signatures)

    except Exception:
        pass

    return counts


def _check_signature_annotations(reader: PdfReader, counts: Dict[str, int], seen_signatures: set) -> None:
    """
    Check for signature annotations (non-AcroForm signature fields).
    Some PDFs use annotations instead of AcroForm fields for signatures.
    Includes deduplication to avoid double-counting signatures already found in AcroForm.
    """
    try:
        for page in reader.pages:
            annots = page.get("/Annots")
            if not annots:
                continue

            for annot in annots:
                try:
                    if not isinstance(annot, DictionaryObject):
                        continue

                    # Check if annotation is a signature widget
                    subtype = annot.get("/Subtype")
                    ft = annot.get("/FT")

                    if subtype == "/Widget" and ft == "/Sig":
                        # Get annotation name for unique identifier
                        annot_name = (annot.get("/T") or "").lower()
                        sig_id = annot_name or f"annot_sig_{id(annot)}"

                        # Only count if not already seen (prevents double-counting)
                        if sig_id not in seen_signatures:
                            seen_signatures.add(sig_id)
                            counts["signature_count"] += 1

                            # Check for witness patterns in annotation name
                            witness_patterns = [
                                "witness", "cosigner", "co-signer", "co signer",
                                "joint", "secondary", "additional signer",
                                "spouse", "guarantor", "co-applicant", "co-maker"
                            ]
                            if any(pattern in annot_name for pattern in witness_patterns):
                                counts["witness_signature_count"] += 1

                except Exception:
                    continue
    except Exception:
        pass

# -------------------------------------------------------------------
# Text flags & scoring
# -------------------------------------------------------------------
_PII_PAT        = re.compile(r"\b(SSN|Social Security|TIN|EIN|DOB|Date of Birth|Routing Number|Account Number|Driver'?s License)\b", re.I)
_ATTACH_PAT     = re.compile(r"\b(attach(ed|ment)?|include (a|an)|supporting document|enclos(ed|ure))\b", re.I)
_NOTARY_PAT     = re.compile(r"\b(notary|notariz(e|ed)|seal|commission expires)\b", re.I)
# Enhanced conditional logic - catch natural language patterns in forms
_LOGIC_PAT      = re.compile(
    r"\b(if yes|if no|if applicable|if checked|check one|select one|"
    r"skip to (section|question|line)|go to (section|question|line)|"
    r"complete (this|only if|if)|answer (only )?if|"
    r"do not complete if|only if|unless|depends on|"
    r"JavaScript|if\s*\(|conditional|branch|interactive|dynamic)\b", re.I
)
_DEADLINE_PAT   = re.compile(r"\b(within\s+\d+\s+(days?|business days?)|no later than|deadline)\b", re.I)
_DEPEND_PAT     = re.compile(r"\b(form\s+\w{2,}[-]?\d+|see (form|document)|submit (with|along))\b", re.I)
# Enhanced third-party - include beneficiaries, trustees, and other common third parties
_THIRDPARTY_PAT = re.compile(
    r"\b(physician|doctor|attorney|advisor|guardian|witness|"
    r"beneficiary|beneficiaries|trustee|executor|administrator|"
    r"custodian|fiduciary|representative|agent|power of attorney)\b", re.I
)
# Enhanced ID detection - captures various forms of identification requirements
# Excludes notarization (captured separately by _NOTARY_PAT)
_ID_PAT = re.compile(
    r"\b(photo ID|government[- ]?issued ID|identification required|"
    r"driver'?s? license|driver license|state ID|state[- ]issued ID|"
    r"passport|valid ID|proof of identity|identity verification|"
    r"ID verification|verify (your )?identity|provide identification|"
    r"submit (a |an )?ID|attach (a |an )?ID|copy of ID|"
    r"birth certificate|social security card|"
    r"military ID|employee ID card|student ID|national ID|"
    r"identification document|identity document|valid photo identification)\b",
    re.I
)
_CLICK2AGREE_PAT= re.compile(r"\b(click (to )?(agree|accept|confirm|acknowledge))\b", re.I)
_WITNESS_TEXT   = re.compile(r"\bwitness signature\b", re.I)
# Only match fees required for form submission, not payment schedules/notifications/authorizations
_PAYMENT_PAT    = re.compile(
    r"\b(payment required|application fee|processing fee|filing fee|submission fee|"
    r"service fee|administrative fee|registration fee|license fee|permit fee|"
    r"fee\s+(is\s+)?required|fee\s+must\s+be|remit payment|submit payment|"
    r"include payment|enclose payment|attach payment|payment\s+of\s+\$\d+)\b",
    re.I
)
# Instructions detection - indicates guided experience and form complexity
_INSTRUCTIONS_PAT = re.compile(
    r"\b(instructions?|how to (complete|fill out)|directions|guide|filing instructions|"
    r"completion guide|filling out this form|completing this form|form instructions|"
    r"general instructions|specific instructions|detailed instructions)\b",
    re.I
)

def extract_payment_amount(text: str) -> Optional[float]:
    """
    Extract dollar amount for DIRECT fees to process/submit this form, or regulatory fines.

    STRICT INCLUSION CRITERIA (realistic and defendable):
    1. Form processing fees ONLY:
       - "application fee $50"
       - "filing fee $35"
       - "processing fee $10"
       - "submission fee $25"
       - "service charge $15"
       - "payment required: $20"

    2. Account minimums (ONLY for new accounts/loans mentioned in THIS form):
       - "minimum to open $100"
       - "minimum opening deposit $50"
       - "minimum initial deposit $25"
       - "minimum balance to maintain $10"

    3. Regulatory fines (ONLY if no fees found):
       - "penalty $500"
       - "fine $250"
       - "late fee $100"

    STRICT EXCLUSIONS (amounts NOT directly facilitated by this form):
    - Refunds, contributions, withdrawals, investments
    - Loan amounts, mortgage amounts, credit amounts
    - Benefit payouts, distributions, disbursements
    - Transaction amounts, transfer amounts
    - Maximum limits, caps, thresholds
    - Account balance examples
    - Savings goals, target amounts

    Returns:
        Float amount if found, None otherwise
    """
    if not text:
        return None

    # Pattern to match dollar amounts with context: $XX or $XX.XX or $X,XXX or $X,XXX.XX
    # Capture up to 100 chars before and after for better context analysis
    dollar_pattern = re.compile(
        r'(.{0,100})\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)(.{0,100})',
        re.I
    )
    matches = dollar_pattern.findall(text)

    if not matches:
        return None

    # CRITICAL: Strong exclusion patterns - amounts that are NEVER form fees
    # These are transaction amounts, not processing fees
    strong_exclusions = re.compile(
        r'\b(refund|reimbursement|contribution|contribute|'
        r'invest(?:ment)?|withdrawal?|withdraw|transfer|'
        r'loan amount|loan balance|credit amount|mortgage amount|'
        r'benefit|payout|distribution|disbursement|rollover|'
        r'retirement|annuity|pension|IRA|401\(k\)|403\(b\)|'
        r'principal|interest rate|APR|APY|'
        r'up to|maximum|max(?:imum)?|limit|cap|ceiling|threshold|'
        r'example|sample|illustration|hypothetical|'
        r'balance|total|value|worth|net|gross|'
        r'income|salary|wage|earning|compensation|'
        r'asset|liabilit|equity|'
        r'purchase|buy|sell|sale|transaction|'
        r'claim amount|damage|settlement|award|judgment|'
        r'grant|scholarship|financial aid|subsid|'
        r'deduction|credit|rebate|discount|'
        r'gift|donation|charitable)\b',
        re.I
    )

    # STRICT: Direct form processing fees ONLY
    # These are fees required to submit/process THIS specific form
    direct_fee_patterns = re.compile(
        r'\b(application fee|filing fee|processing fee|submission fee|'
        r'service fee|service charge|administrative fee|admin fee|'
        r'registration fee|enrollment fee|setup fee|activation fee|'
        r'license fee|permit fee|certification fee|exam fee|'
        r'notary fee|notarization fee|document fee|'
        r'payment required|remit payment|submit payment|enclose payment|'
        r'fee (?:is )?required|fee must be|fee payable|'
        r'cost to (?:file|submit|process|apply)|'
        r'fee of|charge of|cost of \$\d+)\b',
        re.I
    )

    # Account opening minimums (ONLY for new accounts/loans in THIS form)
    # Must be specific to account opening, not existing account balances
    account_minimum_patterns = re.compile(
        r'\b(minimum (?:to open|opening deposit|initial deposit|deposit to open|required to open)|'
        r'minimum balance to (?:open|establish)|'
        r'opening balance requirement|initial balance requirement)\b',
        re.I
    )

    # Regulatory fines/penalties (fallback if no direct fees found)
    regulatory_fine_patterns = re.compile(
        r'\b(penalty|fine|late fee|late charge|late payment|'
        r'non-compliance fee|violation fee|penalty fee)\b',
        re.I
    )

    # Try to find direct fees first (highest priority)
    for before, amount_str, after in matches:
        context = (before + after).lower()

        # CRITICAL: Skip if this is clearly a transaction amount
        if strong_exclusions.search(context):
            continue

        # Check for direct form processing fees
        if direct_fee_patterns.search(context):
            try:
                amount = float(amount_str.replace(',', ''))
                # Sanity check: form fees are typically under $500
                # If > $500, likely not a form processing fee
                if amount > 500:
                    continue
                return amount
            except ValueError:
                continue

        # Check for account opening minimums (only if clearly for NEW accounts)
        if account_minimum_patterns.search(context):
            try:
                amount = float(amount_str.replace(',', ''))
                # Sanity check: account minimums typically under $10,000
                # Higher amounts likely refer to something else
                if amount > 10000:
                    continue
                return amount
            except ValueError:
                continue

    # If no direct fees found, look for regulatory fines (last resort)
    for before, amount_str, after in matches:
        context = (before + after).lower()

        # Skip exclusions
        if strong_exclusions.search(context):
            continue

        # Check for regulatory fines
        if regulatory_fine_patterns.search(context):
            try:
                amount = float(amount_str.replace(',', ''))
                # Sanity check: fines typically under $5,000
                if amount > 5000:
                    continue
                return amount
            except ValueError:
                continue

    # No valid fee amount found
    return None


def calculate_form_value(
    payment_amount: Optional[float],
    form_name: str,
    action_type: str,
    industry_vertical: str,
    industry_subvertical: str
) -> tuple[str, float]:
    """
    Calculate defendable form value based on type and context.

    Three-tier value system:
    1. Direct submission fees (actual money to submit form) → use fee amount
    2. Account value (account minimums × revenue multiplier) → calculate revenue
    3. No value (reports, studies, disclosures) → $0

    Returns:
        Tuple of (value_type, form_value)
        value_type: "submission_fee" | "account_value" | "none"
        form_value: Float dollar amount
    """
    # No payment amount extracted
    if not payment_amount or payment_amount <= 0:
        return ("none", 0.0)

    form_name_lower = form_name.lower() if form_name else ""

    # Check if this is a report/study/disclosure (no value)
    # These are NOT actionable forms users submit
    report_indicators = [
        'report', 'study', 'analysis', 'decision', 'ruling', 'opinion',
        'biennial', 'annual', 'quarterly', 'actuarial', 'legislation',
        'sponsored', 'industry report', 'white paper', 'research',
        'findings', 'results', 'statistics', 'data', 'survey'
    ]

    if any(indicator in form_name_lower for indicator in report_indicators):
        # This is a report/study, not a form - no value
        return ("none", 0.0)

    # Check if this is a disclosure document (no submission value)
    if action_type == "Disclosure (No Signature, No Info Collection)":
        return ("none", 0.0)

    # If amount is under $500, it's likely a direct submission fee
    if payment_amount <= 500:
        return ("submission_fee", payment_amount)

    # Amounts over $500 are likely account minimums, not fees
    # Calculate account value using conservative revenue multipliers

    # Check if this is a financial account opening form
    account_indicators = [
        'account', 'ira', 'roth', '401', '403b', 'hsa', 'esa',
        'brokerage', 'investment', 'savings', 'checking', 'deposit',
        'credit card', 'card application', 'loan', 'mortgage'
    ]

    is_account_form = any(indicator in form_name_lower for indicator in account_indicators)
    is_financial = industry_vertical == "Financial Services"

    if is_account_form and is_financial:
        # Calculate account value based on subvertical and product type

        # Wealth Management - IRA, brokerage accounts
        if industry_subvertical == "Wealth Management":
            # Conservative: 0.75% AUM annually × 10 year lifetime
            # $10,000 minimum → $75/year → $750 lifetime value
            annual_revenue = payment_amount * 0.0075
            lifetime_value = annual_revenue * 10
            return ("account_value", round(lifetime_value, 2))

        # Banking - deposit accounts
        elif industry_subvertical == "Banking":
            if any(x in form_name_lower for x in ['credit card', 'card application']):
                # Credit cards: $150/year average × 10 years
                return ("account_value", 1500.0)
            else:
                # Checking/savings: $200/year average × 10 years
                return ("account_value", 2000.0)

        # P&C Insurance - policies
        elif industry_subvertical == "P&C Insurance":
            # 10% of annual premium × 10 years = 100% of premium amount
            return ("account_value", payment_amount)

        # Default financial account value: conservative 5% of minimum
        else:
            return ("account_value", round(payment_amount * 0.05, 2))

    # If it's a large amount but NOT an account form, likely bad data
    # (e.g., amounts from reports/studies that slipped through)
    return ("none", 0.0)


def estimate_conversion_cost(record: dict) -> dict:
    """
    Estimate the effort and cost to convert a paper/PDF form into a guided
    wizard experience (e.g. DocuSign, web form, CLM workflow).

    Returns:
        {
            "effort_hours": int,          # estimated engineering hours
            "cost_usd": int,              # at $150/hr blended rate
            "tier": str,                  # "simple" | "moderate" | "complex" | "very_complex"
            "cost_drivers": list[str],    # human-readable list of what's driving cost up
        }
    """
    HOURLY_RATE = 150  # blended design + engineering rate

    hours = 8.0  # base: every form has discovery, QA, deploy
    drivers: list[str] = []

    field_count      = int(record.get("field_count") or 0)
    sig_count        = int((record.get("signature_analysis") or {}).get("signature_count")
                           or record.get("signature_count") or 0)
    pages            = int(record.get("pages") or 0)
    conditional      = bool(record.get("conditional_logic"))
    notarization     = bool(record.get("notarization_required"))
    attachments      = bool(record.get("attachments_required"))
    identification   = bool(record.get("identification_required"))
    third_party      = bool(record.get("third_party_involved"))
    payment          = bool(record.get("payment_required"))
    data_validation  = int(record.get("data_validation_fields") or 0)
    witnesses        = bool(record.get("witnesses_required"))

    # Field mapping: 1 hr per 10 fields
    if field_count > 0:
        field_hours = round(field_count / 10, 1)
        hours += field_hours
        if field_count >= 10:
            drivers.append(f"{field_count} fields to map ({field_hours:.0f} hrs)")

    # Signature routing: 2 hrs per signer
    if sig_count > 0:
        sig_hours = sig_count * 2
        hours += sig_hours
        drivers.append(f"{sig_count} signature route{'s' if sig_count > 1 else ''} ({sig_hours} hrs)")

    # Conditional logic: branching wizard paths
    if conditional:
        hours += 8
        drivers.append("Conditional logic — branching wizard paths (+8 hrs)")

    # Notarization: requires specialized integration or out-of-band step
    if notarization:
        hours += 10
        drivers.append("Notarization required — specialized integration (+10 hrs)")

    # Witnesses: additional signer coordination
    if witnesses:
        hours += 6
        drivers.append("Witness signatures — additional routing (+6 hrs)")

    # Attachments: upload/validation UX
    if attachments:
        hours += 4
        drivers.append("Attachment collection — upload + validation UX (+4 hrs)")

    # ID verification: identity check integration
    if identification:
        hours += 5
        drivers.append("ID verification — identity check integration (+5 hrs)")

    # Third-party involvement: external systems or signers
    if third_party:
        hours += 5
        drivers.append("Third-party involvement — external coordination (+5 hrs)")

    # Payment processing: payment gateway integration
    if payment:
        hours += 6
        drivers.append("Payment processing — gateway integration (+6 hrs)")

    # Data validation fields: lookup/verify steps (SSN, account #, etc.)
    if data_validation > 0:
        dv_hours = min(8, data_validation * 1.5)
        hours += dv_hours
        drivers.append(f"{data_validation} validation field(s) — lookup/verify steps (+{dv_hours:.0f} hrs)")

    # Extra pages: more content review
    if pages > 4:
        extra = (pages - 4) * 1
        hours += extra
        drivers.append(f"{pages} pages — extended content review (+{extra} hrs)")

    hours = round(hours)
    cost  = hours * HOURLY_RATE

    if hours <= 20:
        tier = "simple"
    elif hours <= 40:
        tier = "moderate"
    elif hours <= 65:
        tier = "complex"
    else:
        tier = "very_complex"

    return {
        "effort_hours": hours,
        "cost_usd":     cost,
        "tier":         tier,
        "cost_drivers": drivers,
    }


# Data validation - fields requiring lookup/verification
_DATA_VALIDATION_PAT = re.compile(
    r"\b(SSN|Social Security Number|TIN|Tax ID|Taxpayer ID|EIN|Employer ID|"
    r"Account Number|Policy Number|Member Number|Certificate Number|ID Number|"
    r"Driver'?s License|License Number|Passport Number|"
    r"ZIP Code|Postal Code|"
    r"Routing Number|ABA Number|SWIFT|IBAN|"
    r"VIN|Vehicle Identification|"
    r"Case Number|Claim Number|Reference Number|Confirmation Number|"
    r"Employee ID|Student ID|Patient ID|Customer ID|Client ID)\b", re.I
)

# Enhanced signature detection patterns for text-based analysis
_SIGNATURE_WORDS = re.compile(
    r"\b(signature|sign here|sign and date|"
    r"applicant signature|authorized signature|employee signature|employer signature|"
    r"signature of|signed by|signer|signatory|"
    r"witness signature|co-signer signature|joint signature|"
    r"signature line|signature block|signature field|signature required|"
    r"sign below|sign above|printed name and signature|"
    r"date signed|signature date|executed by|"
    r"under penalty of perjury|sworn statement|affirmation|attestation|"
    r"notary signature|notarization|notarize|"
    r"electronic signature|digital signature|e-signature|esignature|"
    r"I certify|I declare|I affirm|I acknowledge|"
    r"by signing|upon signing|signature acknowledges|"
    r"signed on|signed this|signature and date)\b", re.I
)
_COMPLETE_WORDS = re.compile(
    r"\b(please complete|fill (in|out)|submit this form|return (the )?form|"
    r"complete and sign|complete and return|fill out and sign)\b", re.I
)

# Max characters of extracted PDF text to persist on each record.
# Kept intentionally small to bound storage growth at scale (5GB+ at 100k rows
# of unbounded text). Downstream length-based heuristics that previously
# operated on the full text should re-extract from the source PDF if they
# need more than this preview.
_FULL_TEXT_MAX_CHARS = 2000


def _redact_and_truncate_text(text: str, max_chars: int = _FULL_TEXT_MAX_CHARS) -> str:
    """Truncate and redact PII/sensitive identifiers from extracted PDF text
    before persisting it on a record.

    SECURITY NOTE: full_text is intentionally truncated and redacted; do not
    extend the cap or remove redaction without security review. This field is
    persisted to disk and replicated downstream, so it must never carry raw
    PII at rest.
    """
    if not text:
        return ""
    snippet = text[:max_chars]
    try:
        snippet = _PII_PAT.sub("[REDACTED_PII]", snippet)
        snippet = _DATA_VALIDATION_PAT.sub("[REDACTED_ID]", snippet)
    except Exception:
        # Defensive: never let redaction failure leak unredacted text downstream.
        return ""
    return snippet


def estimate_signature_count_from_text(text: str) -> int:
    """
    Estimate signature count from text patterns for flat/scanned PDFs.
    Returns conservative estimate of how many signatures are required.

    Used when PDF has no extractable fields but text indicates signature requirements.
    """
    if not text:
        return 0

    # Patterns that indicate individual signature fields
    signature_field_patterns = [
        r"\bapplicant\s+signature\b",
        r"\bemployee\s+signature\b",
        r"\bemployer\s+signature\b",
        r"\bauthorized\s+signature\b",
        r"\bsupervisor\s+signature\b",
        r"\bmanager\s+signature\b",
        r"\bwitness\s+signature\b",
        r"\bco-?signer\s+signature\b",
        r"\bspouse\s+signature\b",
        r"\bguarantor\s+signature\b",
        r"\btrustee\s+signature\b",
        r"\bbeneficiary\s+signature\b",
        r"\bsignature\s+of\s+(applicant|employee|employer|witness|spouse)",
        r"\bsign\s+here\b",
        r"\bsign\s+and\s+date\b",
        r"\bsignature\s+line\b",
        r"\bsignature\s+block\b",
        # Notarization-specific signature patterns
        r"\bnotary\s+signature\b",
        r"\bnotary\s+public\s+signature\b",
        r"\bsignature\s+of\s+notary\b",
        r"\bnotarial\s+signature\b",
        r"\bsigned\s+before\s+me\b",
        r"\baffiant\s+signature\b",
        r"\bdeponent\s+signature\b",
    ]

    sig_count = 0
    text_lower = text.lower()

    # Count unique signature field mentions
    found_patterns = set()
    for pattern in signature_field_patterns:
        matches = re.findall(pattern, text_lower)
        if matches:
            # Only count unique pattern types (not every instance)
            found_patterns.add(pattern)
            sig_count += len(matches)

    # Cap at reasonable maximum (most forms have 1-4 signatures)
    # If we found > 6 matches, likely just repetitive instructions, so cap it
    if sig_count > 6:
        sig_count = min(sig_count, 3)  # Conservative estimate for heavily repetitive text

    # If we found any signature words but no specific patterns, estimate 1
    if sig_count == 0 and _SIGNATURE_WORDS.search(text):
        sig_count = 1

    return sig_count

# -------------------------------------------------------------------
# Special requirements patterns - unique requirements not captured elsewhere
# -------------------------------------------------------------------
_AGE_REQ_PAT = re.compile(
    r"\b(minimum age|must be \d+|at least \d+ years|age \d+ or (older|above)|"
    r"under (the )?age of|over (the )?age of|age requirement|age eligibility|"
    r"must be 18|18 years or older|21 years or older)\b", re.I
)
_RESIDENCY_PAT = re.compile(
    r"\b(U\.?S\.? citizen|united states citizen|citizenship required|proof of citizenship|"
    r"legal resident|permanent resident|state resident|resident of (the state of )?|residency requirement|"
    r"reside in|domiciled in|legal domicile|primary residence)\b", re.I
)
_ELIGIBILITY_PAT = re.compile(
    r"\b(income requirement|employment status|currently employed|proof of (income|employment)|"
    r"eligibility criteria|eligible if|must meet|qualification|qualified applicant|"
    r"insurance coverage required|must be insured|active coverage)\b", re.I
)
_ENROLLMENT_PERIOD_PAT = re.compile(
    r"\b(enrollment period|open enrollment|filing period|filing window|"
    r"registration period|application period|during the period|"
    r"annual enrollment|special enrollment|qualifying event)\b", re.I
)
_ORIGINAL_DOCS_PAT = re.compile(
    r"\b(original documents?( only)?|certified cop(y|ies)|"
    r"notarized cop(y|ies)|official cop(y|ies)|authenticated documents?|"
    r"no photocopies|photocopies not accepted|original only|"
    r"submit original)\b", re.I
)
_IN_PERSON_PAT = re.compile(
    r"\b(appear in person|in-person (appointment|visit)|office visit required|"
    r"must appear|personal appearance|cannot be mailed|"
    r"no mail submissions|submit(ted)? in person|in person only)\b", re.I
)
_PRE_APPROVAL_PAT = re.compile(
    r"\b(pre-approval|preapproval|prior approval|advance approval|"
    r"approval required|pre-authorization|preauthorization|"
    r"must be approved|authorization required before)\b", re.I
)
_BACKGROUND_CHECK_PAT = re.compile(
    r"\b(background check|security clearance|clearance required|"
    r"criminal (history|record) check|fingerprint|fingerprinting required|"
    r"security screening)\b", re.I
)
_MEDICAL_REQ_PAT = re.compile(
    r"\b(medical (examination|exam)|physical (examination|exam)|"
    r"health screening|medical clearance|doctor's (note|clearance|certification)|"
    r"physician (statement|certification)|health certificate)\b", re.I
)
_LICENSE_VERIFY_PAT = re.compile(
    r"\b(professional license|license verification|licensed (professional|practitioner)|"
    r"certification required|board certified|credential verification|"
    r"active license|current license|valid license)\b", re.I
)
_SPOUSAL_REQ_PAT = re.compile(
    r"\b(spouse (signature|consent|approval)|spousal consent|"
    r"joint (application|election)|both spouses|husband and wife|"
    r"married (couples|applicants)|joint (applicant|owner)s?)\b", re.I
)
_SUBMISSION_METHOD_PAT = re.compile(
    r"\b(mail only|cannot be (emailed|faxed)|do not (email|fax)|"
    r"online submission only|electronic submission (is )?required|"
    r"must be mailed|postal mail required|cannot submit online)\b", re.I
)
_BUSINESS_DAYS_PAT = re.compile(
    r"\b(business days|working days|exclud(e|ing) (weekends|holidays)|"
    r"calendar days)\b", re.I
)

def detect_special_requirements(text: str) -> List[str]:
    """
    Detect unique form requirements not already captured by other indicators.

    Returns list of special requirements found, avoiding duplication with:
    - notarization_required, attachments_required, conditional_logic
    - witness signatures, payment_required, identification_required
    - deadlines_present, form_dependencies, third_party_involved
    - data_validation_fields
    """
    if not text:
        return []

    requirements = []

    # Age requirements
    if _AGE_REQ_PAT.search(text):
        requirements.append("Age requirement")

    # Residency/citizenship requirements
    if _RESIDENCY_PAT.search(text):
        requirements.append("Residency/citizenship requirement")

    # Eligibility criteria
    if _ELIGIBILITY_PAT.search(text):
        requirements.append("Eligibility criteria")

    # Enrollment/filing periods
    if _ENROLLMENT_PERIOD_PAT.search(text):
        requirements.append("Time-specific filing period")

    # Original/certified document requirements (excluding notarization itself)
    if _ORIGINAL_DOCS_PAT.search(text):
        requirements.append("Original/certified documents required")

    # In-person requirements
    if _IN_PERSON_PAT.search(text):
        requirements.append("In-person submission/appearance required")

    # Pre-approval requirements
    if _PRE_APPROVAL_PAT.search(text):
        requirements.append("Pre-approval/authorization required")

    # Background checks
    if _BACKGROUND_CHECK_PAT.search(text):
        requirements.append("Background check/security clearance")

    # Medical requirements
    if _MEDICAL_REQ_PAT.search(text):
        requirements.append("Medical examination required")

    # Professional licensing
    if _LICENSE_VERIFY_PAT.search(text):
        requirements.append("Professional license verification")

    # Spousal/joint requirements
    if _SPOUSAL_REQ_PAT.search(text):
        requirements.append("Spousal/joint participation required")

    # Submission method restrictions
    if _SUBMISSION_METHOD_PAT.search(text):
        requirements.append("Specific submission method required")

    # Business days vs calendar days (timing precision)
    if _BUSINESS_DAYS_PAT.search(text):
        requirements.append("Business days timing")

    return requirements

def detect_text_flags(text: str) -> Dict[str, Any]:
    payment_required = bool(_PAYMENT_PAT.search(text))
    payment_amount = extract_payment_amount(text) if payment_required else None

    return {
        "attachments_required":    bool(_ATTACH_PAT.search(text)),
        "notarization_required":   bool(_NOTARY_PAT.search(text)),
        "conditional_logic":       bool(_LOGIC_PAT.search(text)),
        "deadlines_present":       bool(_DEADLINE_PAT.search(text)),
        "form_dependencies":       bool(_DEPEND_PAT.search(text)),
        "third_party_involved":    bool(_THIRDPARTY_PAT.search(text)),
        "identification_required": bool(_ID_PAT.search(text)),
        "click_to_agree":          bool(_CLICK2AGREE_PAT.search(text)),
        "payment_required":        payment_required,
        "payment_amount":          payment_amount,
        "pii_hits":                len(_PII_PAT.findall(text)),
        "witness_text":            bool(_WITNESS_TEXT.search(text)),
        "data_validation_fields":  len(_DATA_VALIDATION_PAT.findall(text)),
        "instructions_included":   bool(_INSTRUCTIONS_PAT.search(text)),

        # NEW signals for scanned forms
        "signature_words":         bool(_SIGNATURE_WORDS.search(text or "")),
        "complete_words":          bool(_COMPLETE_WORDS.search(text or "")),
    }

def compute_complexity(
    *, pages:int, field_count:int, signature_count:int, witness_signature_count:int,
    attachments_required:bool, attachment_count:int, conditional_logic:bool,
    unique_roles:int, third_party_involved:bool, pii_hits:int,
    radio_groups:int, images_per_page:float, drawings_per_page:float,
    xfa_hint:bool, deadlines_present:bool, dependencies_present:bool,
    notarization_required:bool=False, identification_required:bool=False,
    data_validation_fields:int=0, payment_required:bool=False
) -> float:
    """
    Complexity scoring prioritized by operational difficulty:

    TIER 1 (Hardest - Human Coordination): Notary > 3rd Party > Witness
    TIER 2 (Process Complexity): Conditional Logic, Attachments, ID Requirements, Data Validation, Dependencies
    TIER 3 (Data Entry Options): Radio buttons, Dropdowns (picklists)
    TIER 4 (Volume indicators): Pages, Fields (correlation factors, not direct complexity)
    """
    score = 0.0

    # === TIER 1: Human Coordination (Hardest) ===
    # Notarization - requires scheduling, in-person meeting, credential verification
    if notarization_required:
        score += 25

    # 3rd Party Involvement - coordination, delays, external dependencies
    if third_party_involved:
        score += 18

    # Witness Signatures - finding witnesses, coordinating signatures
    score += min(15, witness_signature_count * 15)  # 15 points per witness, capped at 1

    # === TIER 2: Process Complexity ===
    # Conditional Logic - branching paths, rules engine required
    if conditional_logic:
        score += 10

    # Attachments - gathering, uploading, validating documents
    if attachments_required:
        score += 8
    score += min(6, max(0, attachment_count * 2))  # Additional points for multiple attachments

    # Payment Required - handling fees, processing payments, verification
    if payment_required:
        score += 6

    # ID Requirements - verification process, fraud prevention
    if identification_required:
        score += 7

    # Data Validation - fields requiring lookup/verification (SSN, TIN, Account #, etc.)
    score += min(9, data_validation_fields * 1.5)  # 1.5 points per validation field, capped at 6 fields

    # Form Dependencies - requires other forms, sequential processing
    if dependencies_present:
        score += 6

    # Deadlines - time-sensitive processing
    if deadlines_present:
        score += 4

    # === TIER 3: Data Entry Options (Medium) ===
    # Radio buttons and dropdowns - options require validation but not coordination
    score += min(5, radio_groups * 1.5)  # 1.5 points per radio group, capped at ~3 groups

    # === TIER 4: Volume Indicators (Lower weight) ===
    # Pages and fields are correlation factors - more fields = higher chance of complex fields
    # But alone they don't drive complexity
    score += min(6, pages * 1.2)  # Up to 6 points for pages (5 pages max meaningful impact)
    score += min(8, field_count * 0.15)  # Up to 8 points for fields (53 fields max impact)

    # === Other Factors ===
    # Multiple unique roles (signing order coordination)
    score += min(8, max(0, (unique_roles - 1) * 4))  # 4 points per role beyond first

    # Basic signature (baseline if any signature exists)
    if signature_count > 0:
        score += 2

    # PII sensitivity (data handling complexity)
    score += min(6, pii_hits * 0.8)

    # Visual complexity (complex layouts)
    if drawings_per_page >= 30:
        score += 3
    elif drawings_per_page >= 15:
        score += 2
    elif drawings_per_page >= 5:
        score += 1

    if images_per_page >= 2.0:
        score += 2
    elif images_per_page >= 1.0:
        score += 1

    # XFA forms (dynamic/complex structure)
    if xfa_hint:
        score += 3

    return float(round(max(0, score)))

def compute_nigo_risk(
    *, data_validation_fields:int=0, attachments_required:bool=False, attachment_count:int=0,
    notarization_required:bool=False, identification_required:bool=False, deadlines_present:bool=False,
    conditional_logic:bool=False, dependencies_present:bool=False, witness_signature_count:int=0,
    third_party_involved:bool=False, unique_roles:int=0, radio_groups:int=0,
    pages:int=0, field_count:int=0, payment_required:bool=False
) -> float:
    """
    NIGO (Not In Good Order) Risk Score - probability form will be rejected.
    Different from complexity - focuses on common rejection causes, not operational difficulty.

    HIGH RISK (frequent rejection causes):
      - Data Validation Fields: Fields requiring lookup/verification (SSN, TIN, ZIP, Account #)
      - Attachments: Most commonly forgotten item
      - Notarization: Often incomplete or improper
      - ID Requirements: Missing/expired documents
      - Deadlines: Missed time windows = automatic rejection

    MEDIUM RISK:
      - Conditional Logic: Wrong paths taken, sections missed
      - Dependencies: Prerequisite forms/data missing
      - Witness Signatures: Incomplete or improper execution
      - 3rd Party: Missing third-party data/signatures

    LOW RISK:
      - Multiple Signers: Organizational delay, rarely causes rejection
      - Radio/Picklists: Usually validated by form design
      - Page/Field Count: Correlation to errors, not direct cause
    """
    score = 0.0

    # === HIGH RISK (Common rejection causes) ===
    # Data Validation Fields - wrong SSN/TIN/ZIP/Account numbers are top rejection reason
    score += min(18, data_validation_fields * 3.0)  # 3 points per field, capped at 6 fields

    # Required Attachments - most commonly forgotten item
    if attachments_required:
        score += 5
    score += min(10, attachment_count * 2.5)  # Additional risk per attachment

    # Notarization - often incomplete, improper, or missing
    if notarization_required:
        score += 4

    # Payment Required - incorrect amounts, wrong payment method, payment processing errors
    if payment_required:
        score += 3.5

    # ID Requirements - missing or expired documents
    if identification_required:
        score += 3

    # Deadlines - missed time windows = automatic rejection
    if deadlines_present:
        score += 3

    # === MEDIUM RISK ===
    # Conditional Logic - users miss required sections or take wrong paths
    if conditional_logic:
        score += 2.5

    # Dependencies - prerequisite forms or data often missing
    if dependencies_present:
        score += 2

    # Witness Signatures - often incomplete or improperly executed
    score += min(4, witness_signature_count * 2.0)  # 2 points per witness

    # 3rd Party - missing third-party data or signatures
    if third_party_involved:
        score += 2

    # === LOW RISK (Correlations, not direct causes) ===
    # Multiple Signers - causes delays but rarely rejection
    score += min(2, max(0, (unique_roles - 1) * 0.5))

    # Radio/Picklists - usually validated by form design, low rejection risk
    score += min(1.5, radio_groups * 0.25)

    # Page/Field Count - correlation to errors, not direct cause
    score += min(3, pages * 0.1)  # Very small impact
    score += min(3, field_count * 0.05)  # Very small impact

    return float(round(max(0, score)))

def estimate_times(pages:int, field_count:int, signature_count:int, attachment_count:int) -> Tuple[int,int]:
    signer = max(1, math.ceil(pages*1.2 + field_count*0.15 + signature_count*0.8 + attachment_count*1.0))
    ops    = max(2, math.ceil(pages*1.5 + field_count*0.2  + attachment_count*2.0))
    return signer, ops

def _domains_match(url1: Optional[str], url2: Optional[str]) -> bool:
    """
    Check if two URLs belong to the same domain (ignoring www., content., client. prefixes).
    Examples:
      - cuwest.org and www.cuwest.org -> True
      - cuwest.org and content.cuwest.org -> True
      - cuwest.org and schwab.com -> False
    """
    if not url1 or not url2:
        return True  # If either is missing, assume same domain (don't flag as different)

    try:
        host1 = urlparse(url1).netloc.lower()
        host2 = urlparse(url2).netloc.lower()

        # Normalize hosts: remove common prefixes
        for host in [host1, host2]:
            for prefix in ['www.', 'content.', 'client.', 'forms.']:
                if host.startswith(prefix):
                    host = host[len(prefix):]
                    break

        # Re-normalize after prefix removal
        host1 = host1.replace('www.', '').replace('content.', '').replace('client.', '').replace('forms.', '')
        host2 = host2.replace('www.', '').replace('content.', '').replace('client.', '').replace('forms.', '')

        # Extract base domain (ignore subdomains beyond the main domain)
        # e.g., "foo.bar.cuwest.org" -> "cuwest.org"
        def get_base_domain(host):
            parts = host.split('.')
            if len(parts) >= 2:
                # Take last 2 parts (domain + TLD)
                return '.'.join(parts[-2:])
            return host

        base1 = get_base_domain(host1)
        base2 = get_base_domain(host2)

        return base1 == base2
    except Exception:
        return True  # On error, assume same domain (conservative)

def extract_entity_name(url: Optional[str]) -> Optional[str]:
    """
    Extract a readable entity name from URL domain.
    Examples:
      - aagcu.org -> AAGCU
      - schwab.com -> Schwab
      - fidelity.com -> Fidelity
      - irs.gov -> IRS
    """
    if not url:
        return None

    try:
        host = urlparse(url).netloc.lower()
        # Remove www. prefix
        host = host.replace('www.', '')
        # Get domain without TLD
        domain = host.split('.')[0]

        # Special cases for common abbreviations (keep uppercase)
        abbrev_upper = ['irs', 'gsa', 'dod', 'hhs', 'cms', 'fda', 'epa', 'sec', 'fdic']
        if domain in abbrev_upper:
            return domain.upper()

        # Title case for regular names
        return domain.replace('-', ' ').replace('_', ' ').title()
    except Exception:
        return None

def classify_industry(entity_name: Optional[str], url: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    Classify forms into Industry Vertical and Sub-Vertical based on URL domain patterns.

    Verticals:
    - Financial Services: Banking, P&C Insurance, Wealth Management
    - Healthcare: Payer, Provider, Life Sciences
    - Public Sector: Federal, State & Local, Education, Not-for-Profit
    - Geography: Everything else
    """
    host = (urlparse(url).netloc if url else "").lower()

    # Normalize host: remove www. and content./client. prefixes for consistency
    # This ensures forms from schwab.com and content.schwab.com get same classification
    host = host.replace('www.', '').replace('content.', '').replace('client.', '')

    # ===== HEALTHCARE (check first - many use .org/.edu domains) =====
    # Payer (Health Insurance)
    if any(x in host for x in ["unitedhealthcare", "anthem", "aetna", "cigna", "humana",
                                "bluecross", "blueshield", "bcbs", "centene", "molina",
                                "wellcare", "healthnet", "kaiser", "healthpartners",
                                "highmark", "carefirst", "healthplan", "medicaid", "medicare"]):
        return ("Healthcare", "Payer")

    # Provider (Hospitals, Clinics) - check before .org
    if any(x in host for x in ["hospital", "clinic", "medical", "health", "mayo", "cleveland",
                                "johns-hopkins", "massgeneral", "ucla", "stanford-health",
                                "newyork-presbyterian", "cedars-sinai", "partners-healthcare",
                                "hca-healthcare", "ascension", "commonspirit", "providence",
                                "advent", "trinity", "tenet", "dignityhealth", "sutter",
                                "northwell", "beaumont", "spectrum-health", "healthcare"]):
        return ("Healthcare", "Provider")

    # Life Sciences (Pharma, Biotech, MedTech)
    if any(x in host for x in ["pharma", "biotech", "medtech", "pfizer", "moderna", "johnson",
                                "merck", "abbvie", "amgen", "gilead", "regeneron", "biogen",
                                "genentech", "novartis", "roche", "sanofi", "glaxo", "astra",
                                "lilly", "bms", "takeda", "bayer", "boehringer", "novo",
                                "medtronic", "abbott", "boston-scientific", "stryker",
                                "baxter", "becton", "zimmer", "danaher", "thermo", "illumina",
                                "agilent", "perkin", "waters", "bio-rad", "qiagen"]):
        return ("Healthcare", "Life Sciences")

    # ===== PUBLIC SECTOR =====
    # Federal
    if any(x in host for x in [".gov", "opm.gov", "tsp.gov", "rrb.gov", "gsa.gov", "irs.gov",
                                "treasury.gov", "nasa.gov", "usda.gov", "dhs.gov", "va.gov",
                                "doi.gov", "congress.gov", "gpo.gov", "govinfo.gov", "epa.gov",
                                "fda.gov", "sec.gov", "ftc.gov", "ssa.gov", "cms.gov", "hhs.gov",
                                "ed.gov", "dol.gov", "dot.gov", "army.mil", "navy.mil", "usmc.mil",
                                "af.mil", "uscg.mil", "defense.gov"]):
        return ("Public Sector", "Federal")

    # State & Local
    if any(x in host for x in ["state.", ".state.", "county.", "city.", "municipal"]):
        return ("Public Sector", "State & Local")

    # Education
    if any(x in host for x in [".edu", "university", "college", "school", "academic"]):
        return ("Public Sector", "Education")

    # ===== FINANCIAL SERVICES (check before .org to catch credit unions) =====
    # Banking (including credit unions - must check before .org NFP classification)
    if any(x in host for x in ["bank", "creditunion", "cu.org", "fcu", "federalcreditunion",
                                "chase", "wellsfargo", "bofa", "bankofamerica", "citi",
                                "jpmorgan", "usbank", "pnc", "truist", "capitalone", "ally",
                                "discover", "synchrony", "regions", "fifththird", "keybank",
                                "mtb.com", "huntington", "comerica", "zions", "bokf",
                                "synovus", "umpqua", "websterbank", "santander", "td.com",
                                "bmo.com", "rbcbank", "hsbc", "barclays", "credit-suisse",
                                "deutschebank", "bnpparibas", "socgen", "ing.com"]):
        return ("Financial Services", "Banking")

    # Wealth Management
    if any(x in host for x in ["fidelity", "schwab", "vanguard", "etrade", "tdameritrade",
                                "merrill", "morganstanley", "ubs.com", "wellsfargoadvisors",
                                "raymondjames", "edwardjones", "ameriprise", "lpl",
                                "wealthmanagement", "advisor", "investment", "asset-management",
                                "blackrock", "statestreet", "invesco", "franklin",
                                "prudential-financial", "principal.com", "tiaa"]):
        return ("Financial Services", "Wealth Management")

    # P&C Insurance
    if any(x in host for x in ["insurance", "allstate", "statefarm", "geico", "progressive",
                                "liberty", "nationwide", "farmers", "travelers", "hartford",
                                "chubb", "zurich", "aig.com", "metlife", "prudential",
                                "newyorklife", "massmutual", "guardian", "northwestern",
                                "aflac", "usaa", "erie", "auto-owners", "hanover",
                                "amfam", "safeco", "kemper", "selective"]):
        return ("Financial Services", "P&C Insurance")

    # Not-for-Profit (after healthcare and financial services checks)
    if any(x in host for x in [".org", "nonprofit", "foundation", "charity", "redcross",
                                "unitedway", "salvation"]):
        return ("Public Sector", "Not-for-Profit")

    # ===== GEOGRAPHY / COMMERCIAL (NAICS-based categorization) =====
    # Retail & Consumer Goods (NAICS 44-45, 31-33)
    if any(x in host for x in ["amazon", "walmart", "target", "costco", "homedepot",
                                "lowes", "bestbuy", "macys", "nordstrom", "kohls",
                                "retail", "shop", "store", "mall", "ecommerce"]):
        return ("Geography", "Retail & Consumer Goods")

    # Technology & Software (NAICS 51, 54)
    if any(x in host for x in ["tech", "software", "cloud", "saas", "microsoft",
                                "apple", "google", "amazon", "oracle", "salesforce",
                                "adobe", "intuit", "servicenow", "workday"]):
        return ("Geography", "Technology & Software")

    # Manufacturing (NAICS 31-33)
    if any(x in host for x in ["manufacturing", "industrial", "factory", "automotive",
                                "ford", "gm", "toyota", "boeing", "ge", "honeywell",
                                "caterpillar", "deere", "3m"]):
        return ("Geography", "Manufacturing")

    # Real Estate & Construction (NAICS 23, 53)
    if any(x in host for x in ["realestate", "realtor", "zillow", "redfin", "construction",
                                "builder", "property", "lease", "rental", "apartment"]):
        return ("Geography", "Real Estate & Construction")

    # Professional Services (NAICS 54)
    if any(x in host for x in ["consulting", "legal", "law", "accounting", "audit",
                                "deloitte", "pwc", "ey", "kpmg", "accenture", "mckinsey"]):
        return ("Geography", "Professional Services")

    # Transportation & Logistics (NAICS 48-49)
    if any(x in host for x in ["shipping", "logistics", "transport", "freight", "fedex",
                                "ups", "usps", "dhl", "airline", "railway", "trucking"]):
        return ("Geography", "Transportation & Logistics")

    # Hospitality & Food Service (NAICS 72)
    if any(x in host for x in ["hotel", "restaurant", "hospitality", "marriott", "hilton",
                                "hyatt", "mcdonalds", "starbucks", "dining", "catering"]):
        return ("Geography", "Hospitality & Food Service")

    # Energy & Utilities (NAICS 22)
    if any(x in host for x in ["energy", "power", "electric", "utility", "gas", "oil",
                                "exxon", "chevron", "bp", "shell", "solar", "renewable"]):
        return ("Geography", "Energy & Utilities")

    # Telecommunications (NAICS 51)
    if any(x in host for x in ["telecom", "wireless", "verizon", "att", "tmobile",
                                "sprint", "comcast", "spectrum", "cable"]):
        return ("Geography", "Telecommunications")

    # Default: Geography with no specific NAICS category
    return ("Geography", "Other")

# -------------------------------------------------------------------
# Title helpers (fallbacks + tidy) and LLM-first builder with debug
# -------------------------------------------------------------------
TITLE_STOP = re.compile(r"(table of contents|contents|index)\b", re.I)

def _pdf_title_from_meta(reader: PdfReader) -> str:
    try:
        info = reader.metadata or {}
        t = (info.get("/Title") or info.get("Title") or "").strip()
        return t if len(t) >= 6 else ""
    except Exception:
        return ""

def _heading_from_text(first_page_text: str) -> str:
    if not first_page_text:
        return ""
    lines = [ln.strip() for ln in first_page_text.splitlines()[:50] if ln.strip()]
    take = []
    for ln in lines:
        if TITLE_STOP.search(ln):
            break
        take.append(ln)
        if len(take) >= 8:
            break
    cands = [ln for ln in take if 4 <= len(ln) <= 120 and ln.count(" ") <= 12]
    cands.sort(key=lambda s: (not s.isupper(), len(s)))
    return cands[0] if cands else ""

def _prettify_filename(url_or_name: str) -> str:
    if not url_or_name: return ""
    name = os.path.basename(urlparse(url_or_name).path) or url_or_name
    name = re.sub(r"\.pdf$","", name, flags=re.I)
    name = re.sub(r"[_\-]+"," ", name)
    name = re.sub(r"\b(sf)\s*[-_]*\s*(\d{3,4}[a-z]?)\b",
                  lambda m: f"{m.group(1).upper()} {m.group(2).upper()}",
                  name, flags=re.I)
    subs = {"opm":"OPM","irs":"IRS","gpo":"GPO","pub":"Publication"}
    tokens = [subs.get(tok.lower(), tok) for tok in name.split()]
    def tcase(tok): return tok if (tok.isupper() or re.search(r"\d", tok)) else tok.capitalize()
    cleaned = " ".join(tcase(t) for t in tokens)
    return re.sub(r"\s{2,}"," ", cleaned).strip(" -–—_.")

def _is_poor_quality_title(title: str) -> bool:
    """
    Check if an LLM-generated title is poor quality and should be replaced
    with filename-based fallback.

    Poor quality indicators:
    - Just a date (e.g., "December 2022", "May 2025")
    - Just a percentage (e.g., "7.62 %")
    - Just numbers (e.g., "686 964 312")
    - Too short (< 3 words and < 15 chars)
    - Generic/vague (e.g., "Form", "Document", "PDF")
    """
    if not title or len(title.strip()) < 3:
        return True

    title_lower = title.strip().lower()

    # Check for month names (date-only titles)
    months = ['january', 'february', 'march', 'april', 'may', 'june',
              'july', 'august', 'september', 'october', 'november', 'december']
    if any(month in title_lower for month in months):
        # If it's JUST a date (month + year), it's poor quality
        # e.g., "December 2022", "May 2025"
        words = title_lower.split()
        if len(words) <= 3 and any(w.isdigit() or w in months for w in words):
            return True

    # Check for percentage-only titles (e.g., "7.62 %")
    if '%' in title and len(title_lower.split()) <= 3:
        return True

    # Check for number-heavy titles (e.g., "686 964 312", "Arsn 686 964 312")
    words = title.split()
    if len(words) <= 4:
        number_words = sum(1 for w in words if re.search(r'\d', w))
        if number_words >= len(words) - 1:  # All or all-but-one words contain numbers
            return True

    # Check for too-short titles (< 3 words and < 15 chars)
    if len(words) < 3 and len(title) < 15:
        # Allow if it's an acronym or form code
        if not re.match(r'^[A-Z]{2,}-?\d+', title):
            return True

    # Check for generic/vague titles
    generic_titles = ['form', 'document', 'pdf', 'file', 'untitled', 'n/a']
    if title_lower in generic_titles:
        return True

    return False

_SMALL_WORDS = {
    "a","an","and","as","at","but","by","for","in","nor",
    "of","on","or","per","the","to","via","vs","vs."
}
_ALWAYS_UPPER = {"OPM","IRS","GPO","US","U.S.","SF","SCOTUS","IL","PDF"}

_MONTHS = ["","January","February","March","April","May","June","July","August","September","October","November","December"]

def _fix_typos(s: str) -> str:
    fixes = {r"\bApopeal\b": "Appeal", r"\bDispostion\b": "Disposition", r"\bDispostions\b": "Dispositions"}
    for pat, rep in fixes.items(): s = re.sub(pat, rep, s, flags=re.I)
    return s

def _normalize_dates(s: str) -> str:
    def repl(m):
        mth, day, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if yr < 100: yr += 2000
        if 1 <= mth <= 12:
            return f"{_MONTHS[mth]} {day}, {yr}"
        return m.group(0)
    return re.sub(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", repl, s)

def _normalize_dashes(s: str) -> str:
    s = re.sub(r"\s*[-–]\s*", " — ", s)
    return re.sub(r"\s{2,}", " ", s).strip()

_CANON = [
    (r"\bSupreme Court of Illinois\b", "Illinois Supreme Court"),
    (r"\bIL Supreme Court\b", "Illinois Supreme Court"),
    (r"\bDisposition[s]?\b", "Dispositions"),
    (r"\bDocket\b", "Docket"),
    (r"\bLeave to Appeal\b", "Leave to Appeal"),
    (r"\bReport\b", "Report"),
    (r"\bHandbook\b", "Handbook"),
]

def _canonicalize_phrases(s: str) -> str:
    for pat, rep in _CANON: s = re.sub(pat, rep, s, flags=re.I)
    return _normalize_dashes(s)

def _smart_titlecase(s: str) -> str:
    if not s: return s
    words = s.split(); out=[]
    for i, w in enumerate(words):
        bare = re.sub(r"[^\w.]", "", w)
        if bare.upper() in _ALWAYS_UPPER: out.append(bare.upper() + w[len(bare):]); continue
        if i not in (0, len(words)-1) and bare.lower() in _SMALL_WORDS: out.append(bare.lower() + w[len(bare):]); continue
        if bare.isupper() and len(bare) > 2: cap = bare.capitalize(); out.append(cap + w[len(bare):])
        else: cap = bare[:1].upper() + bare[1:].lower(); out.append(cap + w[len(bare):])
    return " ".join(out)

def _tidy_title(s: str) -> str:
    if not s: return s
    s = s.strip()
    s = _fix_typos(s)
    s = _canonicalize_phrases(s)
    s = _normalize_dates(s)
    s = _normalize_dashes(s).strip(" .–—-")
    if s.isupper(): s = s.title()
    s = _smart_titlecase(s)
    return _normalize_dashes(s)

def build_title_with_debug(*, url: str, text: str, reader, default_name: str, skip_llm_title: bool = False) -> tuple[str, str, dict]:
    """Returns (title, entity_name, debug)"""
    first_page = (text or "")[:1200]
    debug = {
        "strategy": "llm-first",
        "first_page_excerpt": first_page,
        "meta_title": "",
        "heading": "",
        "filename_pretty": "",
        "llm_title": "",
        "llm_entity": "",
        "llm_error": "",
    }
    try:
        meta = _pdf_title_from_meta(reader) or ""
        head = _heading_from_text(text or "") or ""
        fname = _prettify_filename(url or default_name) or ""
        debug.update({"meta_title": meta, "heading": head, "filename_pretty": fname})
    except Exception as e:
        debug["llm_error"] = f"pre-extract error: {e}"

    title = ""
    entity_name = ""
    if not skip_llm_title:
        try:
            lt, le = make_title(url or "", first_page)
            print(f"[TITLE] LLM returned: {repr(lt)}, Entity: {repr(le)}")  # DEBUG
            if lt:
                lt_stripped = lt.strip()
                # Check if LLM title is good quality
                if not _is_poor_quality_title(lt_stripped):
                    title = lt_stripped
                    debug["llm_title"] = title
                    print(f"[TITLE] Using LLM title: {title}")  # DEBUG
                else:
                    # LLM title is poor quality - will use fallback
                    debug["llm_title_rejected"] = lt_stripped
                    print(f"[TITLE] Rejecting poor-quality LLM title: {lt_stripped}")  # DEBUG
            if le:
                entity_name = le.strip()
                debug["llm_entity"] = entity_name
                print(f"[TITLE] Using LLM entity: {entity_name}")  # DEBUG
        except Exception as e:
            debug["llm_error"] = f"llm error: {e}"
            print(f"[TITLE] LLM error: {e}")  # DEBUG
    else:
        debug["llm_skipped"] = "fast_reanalysis"

    if not title:
        # Use fallback chain: metadata > heading > filename > default
        # Prioritize filename over heading for better quality
        title = (debug["meta_title"] or debug["filename_pretty"] or debug["heading"] or default_name)
        print(f"[TITLE] Fallback to: {title}")  # DEBUG

    # Fallback for entity name if LLM didn't provide one
    if not entity_name:
        entity_name = extract_entity_name(url)

    return _tidy_title(title), entity_name, debug

# -------------------------------------------------------------------
# Actionability gate & Action Type
# -------------------------------------------------------------------
def _determine_action_type(flags: dict, widgets: dict) -> str:
    """
    Return one of:
      - "Signature Required"
      - "Information Collection (No Signature)"
      - "Disclosure (No Signature, No Info Collection)"
    """
    sig_ct = int(widgets.get("signature_count") or 0)
    if sig_ct > 0 or flags.get("signature_words"):
        return "Signature Required"
    field_ct = int(widgets.get("field_count") or 0)
    if field_ct > 0 or (flags.get("pii_hits") or 0) > 0 or flags.get("complete_words"):
        return "Information Collection (No Signature)"
    return "Disclosure (No Signature, No Info Collection)"

def is_actionable(record: dict) -> bool:
    """
    Filter for actionable forms - documents that require user action or serve regulatory purposes.

    INCLUSION criteria:
    1. Forms that collect information (fillable fields, data collection signals)
    2. Forms that serve as regulatory disclosures
    3. Forms with at least one signature field

    EXCLUSION criteria:
    - Reports, handbooks, general instruction documents (not actionable)
    """
    # Get form characteristics first (needed for both vision and heuristic logic)
    title = (record.get("pretty_title") or record.get("form_name") or "").lower()
    url = (record.get("source_url") or "").lower()
    fields = int(record.get("field_count") or 0)
    sigs = int((record.get("signature_analysis") or {}).get("signature_count") or 0)
    flags = record.get("_flags") or {}

    # If vision analyzed, consider it BUT check hard signals first
    if record.get("vision_analyzed"):
        # OVERRIDE: If form has clear actionable signals, it's actionable regardless of vision
        # This prevents false negatives where vision model is too strict
        if sigs > 0 or flags.get("signature_words"):
            return True  # Has signatures - definitely actionable
        if fields > 0:
            return True  # Has fillable fields - definitely actionable

        # No hard signals - trust vision determination
        vision_data = record.get("_vision_data", {})
        is_act = vision_data.get("is_actionable", True)  # Default to actionable if uncertain
        return is_act

    # EXCLUSION: Non-actionable document types (reports, handbooks, etc.)
    # Must be strict - only exclude if clearly a non-form document
    EXCLUDE_KEYWORDS = [
        "handbook", "guidebook", "instruction manual", "user guide",
        "whitepaper", "white paper", "presentation", "slides",
        "transcript", "statute text", "law text", "regulation text",
        "proceedings", "newsletter", "press release",
        "executive summary", "briefing", "research paper",
        "annual report", "quarterly report", "findings"
    ]
    exclude_matches = sum(1 for keyword in EXCLUDE_KEYWORDS if keyword in title or keyword in url)
    if exclude_matches >= 2:  # Require 2+ exclusion matches to be conservative
        return False

    # SPECIAL EXCLUSION: Internal legal/governance documents (NOT regulatory disclosures)
    # These are often lengthy documents (>15 pages) with 0 fields and 0 signatures
    # EXCEPTION: Regulatory disclosure documents (privacy policies, prospectuses, etc.) ARE accepted
    pages = int(record.get("pages") or 0)
    if fields == 0 and sigs == 0 and pages > 15:
        # Internal legal documents (should be excluded)
        INTERNAL_LEGAL_KEYWORDS = [
            "constitution", "charter", "bylaws", "by-laws",
            "memorandum of association", "articles of incorporation",
            "articles of association", "operating agreement",
            "partnership agreement", "shareholder agreement",
            "corporate policy", "company policy", "internal policy",
            "governance document", "compliance manual",
            "trust deed", "deed of trust", "scheme constitution"
        ]

        # Regulatory disclosure documents (should be accepted - required by law)
        REGULATORY_DISCLOSURE_KEYWORDS = [
            "disclosure", "privacy policy", "privacy notice", "privacy statement",
            "terms and conditions", "terms of service", "terms of use",
            "prospectus", "offering memorandum", "offering circular",
            "truth in lending", "tila", "regulation z", "reg z",
            "schumer box", "privacy act", "fair lending",
            "product disclosure statement", "pds",
            "information memorandum", "client disclosure"
        ]

        # Check if it's an internal legal document
        internal_matches = sum(1 for keyword in INTERNAL_LEGAL_KEYWORDS if keyword in title or keyword in url)

        # Check if it's a regulatory disclosure
        disclosure_matches = sum(1 for keyword in REGULATORY_DISCLOSURE_KEYWORDS if keyword in title or keyword in url)

        # Exclude if it's internal legal AND NOT a regulatory disclosure
        if internal_matches >= 1 and disclosure_matches == 0:
            return False

    # INCLUSION 1: Has at least one signature field
    if sigs > 0 or flags.get("signature_words"):
        return True

    # INCLUSION 2: Has fillable fields (data collection)
    if fields > 0:
        return True

    # INCLUSION 3: Text signals suggest data collection
    if (flags.get("pii_hits") or 0) > 0 or flags.get("complete_words"):
        return True

    # INCLUSION 4: Title contains disclosure keywords (regulatory forms)
    # Include all regulatory disclosures and required supplemental documents
    DISCLOSURE_KEYWORDS = [
        "disclosure", "notice", "notification", "statement",
        "terms and conditions", "privacy policy", "consent form",
        "acknowledgment", "acknowledgement", "agreement",
        # Financial Services - Regulatory disclosure documents
        "tila", "truth in lending", "truth-in-lending",
        "privacy act", "privacy notice", "privacy statement",
        "reg z", "regulation z", "reg e", "regulation e",
        "reg d", "regulation d", "reg dd", "regulation dd",
        "schumer box", "annual percentage rate disclosure",
        "truth in savings", "truth-in-savings",
        # Financial Services - Securities and investment disclosures
        "prospectus", "offering memorandum", "offering circular",
        "private placement memorandum", "ppm",
        "subscription agreement", "risk disclosure",
        "investment policy statement", "suitability disclosure",
        "form adv", "form crs", "client relationship summary",
        "fee schedule", "fee disclosure",
        # Financial Services - Other regulatory disclosures
        "equal credit opportunity", "ecoa", "fair lending",
        "fair credit reporting", "fcra", "fcba",
        "right to financial privacy act",
        "gramm-leach-bliley", "glb act", "glba",
        "consumer financial protection", "cfpb",
        "fdic insurance", "ncua insurance",
        "arbitration agreement", "class action waiver",
        # Healthcare & Life Sciences - HIPAA and privacy
        "hipaa", "hitech", "health insurance portability",
        "protected health information", "phi", "ephi",
        "notice of privacy practices", "npp",
        "patient bill of rights", "patient rights",
        "patient privacy", "medical privacy",
        # Healthcare & Life Sciences - Clinical and regulatory
        "informed consent", "clinical trial consent",
        "research subject", "institutional review board", "irb",
        "fda disclosure", "drug labeling", "device labeling",
        "adverse event", "black box warning",
        "medication guide", "patient information leaflet",
        "vaccine information statement", "vis",
        # Healthcare & Life Sciences - Insurance and benefits
        "explanation of benefits", "eob", "summary of benefits",
        "medicare notice", "medicaid notice", "cms notice",
        "advance directive", "living will", "healthcare proxy",
        "authorization for release", "medical records release",
        # Public Sector - Federal transparency and FOIA
        "freedom of information", "foia", "foia request",
        "privacy act request", "public records request",
        "sunshine act", "open meetings", "federal register",
        "paperwork reduction act", "pra", "omb control",
        "omb number", "information collection",
        # Public Sector - Federal regulatory
        "environmental impact statement", "eis",
        "environmental assessment", "nepa",
        "federal acquisition regulation", "far",
        "national environmental policy", "clean air act",
        "clean water act", "endangered species act",
        # Public Sector - State and local transparency
        "state public records", "local public records",
        "sunshine law", "open records", "public disclosure",
        "zoning notice", "public hearing notice",
        "environmental disclosure", "impact assessment",
        "municipal code", "ordinance disclosure"
    ]
    if any(keyword in title for keyword in DISCLOSURE_KEYWORDS):
        return True

    # INCLUSION 5: Title contains actionable form keywords
    ACTIONABLE_KEYWORDS = [
        "application", "request", "enrollment", "authorization",
        "consent", "affidavit", "declaration", "waiver", "election",
        "claim", "appeal", "designation", "verification", "certification",
        "registration", "petition", "submission", "filing",
        "worksheet", "checklist", "questionnaire", "survey", "form"
    ]
    if any(keyword in title for keyword in ACTIONABLE_KEYWORDS):
        return True

    # DEFAULT: Exclude if we're unsure (conservative filtering)
    return False


def calculate_quality_score(record: dict) -> tuple[str, float, list[str]]:
    """
    Calculate document quality and actionability confidence.

    Returns 3-tier quality classification:
    - HIGH (0.8-1.0): Clear actionable signals - auto-commit
    - MEDIUM (0.5-0.79): Some signals but uncertain - manual review
    - LOW (0.0-0.49): Non-actionable - reject

    Returns:
        - confidence_tier: "high", "medium", "low"
        - confidence_score: 0.0-1.0
        - signals: List of quality signals (positive or negative)
    """
    signals = []
    score = 0.0

    # Extract key attributes
    field_count = int(record.get("field_count") or 0)
    signature_count = int((record.get("signature_analysis") or {}).get("signature_count") or 0)
    page_count = int(record.get("pages") or 0)
    action_type = (record.get("action_type") or "").lower()
    form_name = (record.get("pretty_title") or record.get("form_name") or "").lower()
    url = (record.get("source_url") or "").lower()
    payment_required = record.get("payment_required", False)
    flags = record.get("_flags") or {}

    # Check actionability filter
    actionable = is_actionable(record)

    # ========================================
    # HIGH CONFIDENCE INDICATORS (+0.3 each)
    # ========================================

    if signature_count >= 1:
        score += 0.35
        signals.append(f"✓ Has {signature_count} signature field(s)")

    if field_count >= 5:
        score += 0.35
        signals.append(f"✓ Has {field_count} fillable fields")

    if payment_required:
        score += 0.20
        signals.append("✓ Payment required")

    if actionable and (field_count > 0 or signature_count > 0):
        score += 0.15
        signals.append("✓ Passes actionability filter with fields/signatures")

    # ========================================
    # MEDIUM CONFIDENCE INDICATORS (+0.1-0.2)
    # ========================================

    if 1 <= field_count <= 4:
        score += 0.15
        signals.append(f"⚠ Low field count ({field_count} fields)")

    if actionable and field_count == 0 and signature_count == 0:
        score += 0.20
        signals.append("⚠ Regulatory disclosure (0 fields but actionable)")

    if flags.get("pii_hits", 0) > 0 and field_count == 0:
        score += 0.10
        signals.append("⚠ Has PII signals but no fields")

    if action_type == "information collection":
        score += 0.10
        signals.append("⚠ Information collection action type")

    # ========================================
    # NEGATIVE INDICATORS (-0.2 to -0.4)
    # ========================================

    # Check exclusion keywords (reports, guides, handbooks, legal documents)
    EXCLUSION_KEYWORDS = [
        "annual report", "quarterly report", "statistical report", "data report",
        "summary report", "findings report", "research report",
        "handbook", "guidebook", "user guide", "instruction manual",
        "constitution", "charter", "bylaws", "by-laws",
        "memorandum of association", "articles of incorporation", "operating agreement",
        "partnership agreement", "corporate policy", "governance document",
        "reference guide", "best practices guide", "implementation guide",
        "whitepaper", "white paper", "case study", "fact sheet",
        "presentation", "slides", "brochure", "catalog", "flyer",
        "tutorial", "training materials", "course materials",
        "newsletter", "press release", "executive summary", "briefing"
    ]

    exclusion_matches = [kw for kw in EXCLUSION_KEYWORDS if kw in form_name or kw in url]
    if exclusion_matches:
        penalty = -0.4 * len(exclusion_matches)
        score += penalty
        signals.append(f"✗ Matches exclusion keyword(s): {', '.join(exclusion_matches[:3])}")

    # Zero signals penalty
    if field_count == 0 and signature_count == 0 and not actionable:
        score -= 0.5
        signals.append("✗ Zero fields, zero signatures, not actionable")

    # Failed actionability filter
    if not actionable and field_count == 0 and signature_count == 0:
        score -= 0.3
        signals.append("✗ Failed actionability filter")

    # Extra penalty for lengthy INTERNAL legal documents with no interaction
    # EXCEPTION: Regulatory disclosure documents (required by law) are not penalized
    if field_count == 0 and signature_count == 0 and page_count > 15:
        # Check if it's a regulatory disclosure
        REGULATORY_DISCLOSURE_KEYWORDS = [
            "disclosure", "privacy policy", "privacy notice", "privacy statement",
            "terms and conditions", "terms of service", "terms of use",
            "prospectus", "offering memorandum", "offering circular",
            "truth in lending", "tila", "regulation z", "reg z",
            "schumer box", "privacy act", "fair lending",
            "product disclosure statement", "pds"
        ]
        is_regulatory_disclosure = any(keyword in form_name or keyword in url for keyword in REGULATORY_DISCLOSURE_KEYWORDS)

        if not is_regulatory_disclosure:
            # It's an internal legal document - penalize it
            score -= 0.4
            signals.append(f"✗ Internal legal document pattern ({page_count} pages, 0 fields, 0 signatures)")

    # ========================================
    # BONUS: Strong positive signals
    # ========================================

    if field_count >= 10 and signature_count >= 1:
        score += 0.10
        signals.append("✓✓ Strong form signals (10+ fields + signature)")

    # ========================================
    # Normalize score to 0.0-1.0
    # ========================================

    score = max(0.0, min(1.0, score))

    # Determine tier
    if score >= 0.80:
        tier = "high"
    elif score >= 0.50:
        tier = "medium"
    else:
        tier = "low"

    return (tier, score, signals)


def classify_business_impact(record: dict) -> str:
    """
    Classify form by business impact category.

    Categories:
    - Revenue: Collects fees, enables new revenue streams (account opening, service enrollment)
    - Expenses: Facilitates transactions that don't generate revenue (withdrawals, refunds, claims)
    - Regulatory: Compliance, disclosure, reports (true regulatory documents only)

    Classification priority:
    1. Revenue indicators checked FIRST (applications, licenses, permits, enrollments)
    2. Expenses indicators checked SECOND (withdrawals, claims, refunds)
    3. Regulatory indicators checked LAST (disclosures, compliance, tax forms)

    This ensures government application forms (license apps, permits) are correctly
    classified as Revenue based on business intent, not as Regulatory based on domain.

    Args:
        record: Form analysis record

    Returns:
        One of: "Revenue", "Expenses", "Regulatory"
    """
    form_name = (record.get("form_name") or "").lower()
    action_type = (record.get("action_type") or "").lower()
    source_url = (record.get("source_url") or "").lower()
    payment_required = record.get("payment_required", False)
    payment_amount = record.get("payment_amount", 0)

    # REVENUE: Account opening, service enrollment, fee collection
    # Check FIRST - business intent matters more than domain
    revenue_indicators = [
        # Forms that collect fees
        payment_required and payment_amount and payment_amount > 0,
        # Account opening keywords
        any(keyword in form_name for keyword in [
            "application", "new account", "account opening", "open account",
            "enrollment", "enroll", "sign up", "signup", "registration", "register",
            "subscription", "service agreement",
            "account agreement", "custodial agreement",
            "direct deposit", "automatic payment", "ach authorization",
            "beneficiary designation",  # revenue opportunity (keeps assets)
            # Government revenue forms (licenses, permits, registrations)
            "license", "permit", "certification", "authorization",
            "renewal", "reapplication"
        ]),
        # Revenue action types
        action_type in [
            "application", "account opening", "enrollment",
            "service activation", "authorization", "agreement"
        ]
    ]

    if any(revenue_indicators):
        return "Revenue"

    # EXPENSES: Outbound money movement, account closures, claims/payouts
    # Check SECOND - before regulatory fallback
    expense_indicators = [
        # Money outbound keywords
        any(keyword in form_name for keyword in [
            "withdrawal", "withdraw", "distribution", "disbursement",
            "refund", "redemption", "liquidation",
            "claim", "payout", "benefit payment",
            "rollover", "transfer out",
            "account closure", "close account", "termination",
            "rebate"  # government rebates are expenses
        ]),
        # Expense action types
        action_type in [
            "withdrawal", "distribution", "refund", "claim",
            "account closure", "transfer out", "redemption"
        ]
    ]

    if any(expense_indicators):
        return "Expenses"

    # REGULATORY: Compliance, disclosure, reports
    # Check LAST - only for true regulatory/compliance documents
    regulatory_indicators = [
        # Regulatory keywords in form name (NO blanket .gov check)
        any(keyword in form_name for keyword in [
            "disclosure", "compliance", "regulatory", "sec form", "finra",
            "tax form", "irs", "1099", "w-2", "w-4", "w-9", "1040",
            "annual report", "quarterly report", "filing",
            "privacy notice", "privacy policy", "terms and conditions",
            "truth in lending", "truth in savings",
            "equal credit opportunity", "fair lending",
            "reg ", "regulation ", "cfpb", "fdic", "occ",
            "notice", "advisory", "bulletin"
        ]),
        # Regulatory action types
        action_type in ["disclosure", "notice", "report", "filing", "compliance"]
    ]

    if any(regulatory_indicators):
        return "Regulatory"

    # DEFAULT: If unclear, categorize as Revenue (most forms enable business)
    # Forms like "change of address" or "beneficiary update" support revenue activities
    return "Revenue"

# -------------------------------------------------------------------
# Main entry
# -------------------------------------------------------------------
def analyze_pdf(
    *,
    pdf_url: Optional[str] = None,
    pdf_bytes: Optional[bytes] = None,
    timeout: int = 25,
    max_pdf_mb: int = 120,
    disable_size_guard: bool = False,
    force_minimal: bool = True,
    filename: Optional[str] = None,
    crawl_root_url: Optional[str] = None,  # URL of crawl root domain for entity/vertical consistency
    skip_vision: bool = False,  # Skip expensive vision analysis
    skip_llm_title: bool = False,  # Skip LLM title generation
    **_
) -> Dict[str, Any]:
    # SECURITY: full_text on the returned record is intentionally truncated
    # and PII-redacted via _redact_and_truncate_text. Do not extend without
    # security review -- this field is persisted at rest and downstream.
    # Vision is invoked at most once per call: the flat-PDF branch and the
    # safety-check branch are mutually exclusive (gated by vision_already_ran).
    src_url = pdf_url or None
    name = (filename or (pdf_url and _filename_from_url(pdf_url)) or "document.pdf")

    base_record: Dict[str, Any] = {
        "id": hashlib.sha1(((src_url or "") + "|" + name).encode("utf-8")).hexdigest(),
        "timestamp": _now(),
        "form_name": name,
        "form_title": name,
        "entity_name": None,
        "source_url": src_url,
        "industry_vertical": None,
        "industry_subvertical": None,
        "pages": 0,
        "attachment_count": 0,
        "field_count": 0,
        "text_fields": 0,
        "checkboxes": 0,
        "dropdowns": 0,
        "signature_analysis": {
            "signature_count": 0,
            "witness_signature_count": 0,
            "conditional_signature_count": 0,
        },
        "notarization_required": False,
        "attachments_required": False,
        "conditional_logic": False,
        "deadlines_present": False,
        "form_dependencies": False,
        "third_party_involved": False,
        "identification_required": False,
        "click_to_agree": False,
        "payment_required": False,
        "payment_amount": None,
        "same_domain": True,
        "witnesses_required": False,
        "language_count": 1,
        "estimated_signer_time": 0,
        "estimated_processing_time": 0,
        "complexity_score": 0.0,
        "nigo_score": 0.0,
        "key_drivers": [],
        "special_requirements": [],
        "status": "failed",
        "parse_error": None
    }

    if not pdf_url and not pdf_bytes:
        base_record["parse_error"] = "No input: provide pdf_url or pdf_bytes"
        return base_record

    try:
        if pdf_bytes:
            content = pdf_bytes
        else:
            guard_mb = (10**9) if disable_size_guard else max_pdf_mb
            content = _safe_get(pdf_url, timeout=timeout, max_mb=guard_mb)

        reader = _open_reader(content)
        _ensure_decrypted(reader)

        pages, text, image_count, xfa_hint, radio_groups = extract_pages_text_images_xfa(reader)
        widgets = extract_form_widgets(reader)
        flags = detect_text_flags(text or "")

        # Vision analysis for flat/scanned PDFs
        vision_data = None
        vision_already_ran = False  # Gate to prevent the safety-check branch from re-firing vision
        text_len = len(text or "")
        field_count = widgets.get("field_count", 0)
        widget_sig_count = int(widgets.get("signature_count", 0) or 0)
        threshold = int(os.getenv("VISION_TRIGGER_THRESHOLD", "100"))

        # Detect flat PDFs: minimal text AND no extractable fields
        is_flat_pdf = (text_len < threshold) and (field_count == 0)

        # Even non-flat PDFs benefit from vision when the heuristics know the
        # form REQUIRES action but can't COUNT the action items, e.g.:
        #   - "Signature Required" inferred from text but 0 AcroForm sig widgets
        #     (forms designed to be printed and wet-signed)
        #   - Attachments required by text pattern but no structured count
        # Vision is the only way to count what humans actually see on the page.
        # Gated by VISION_AMBIGUOUS_TRIGGER (default on); set to "false" to
        # disable the extra calls and revert to flat-only behaviour.
        ambiguous_trigger_on = os.getenv("VISION_AMBIGUOUS_TRIGGER", "true").lower() in ("true", "1", "yes")
        sig_required_text = bool(
            flags.get("signature_words")
            or flags.get("notarization_required")
            or flags.get("witness_text")
        )
        needs_vision_for_counts = (
            ambiguous_trigger_on
            and field_count > 0  # not flat (flat is handled above)
            and (
                # Form text says signing is required but AcroForm has no sig widget
                (sig_required_text and widget_sig_count == 0)
                # Or attachments are required and we can't count them yet
                or flags.get("attachments_required")
            )
        )

        should_run_vision = (is_flat_pdf or needs_vision_for_counts) and not skip_vision

        if should_run_vision:
            try:
                import logging
                trigger_reason = (
                    "flat_pdf" if is_flat_pdf
                    else ("ambiguous_signatures" if (sig_required_text and widget_sig_count == 0)
                          else "attachments_required")
                )
                logging.info(
                    f"[VISION] Triggered ({trigger_reason}): text_len={text_len}, "
                    f"fields={field_count}, widget_sigs={widget_sig_count}, url={src_url}"
                )
                vision_data = analyze_flat_pdf_with_vision(
                    pdf_bytes=content,
                    source_url=src_url or ""
                )
                vision_already_ran = True
                logging.info(f"[VISION] Analysis complete: is_actionable={vision_data.get('is_actionable')}, vision_analyzed={vision_data.get('vision_analyzed')}")
            except Exception as e:
                import logging
                logging.error(f"[VISION] Error during vision analysis: {e}", exc_info=True)
                vision_data = {"is_actionable": True, "vision_analyzed": False, "error": str(e)}
                vision_already_ran = True  # Even on error, do not re-fire below

        # For flat PDFs with no extractable signatures, estimate from text patterns
        if is_flat_pdf and widgets.get("signature_count", 0) == 0:
            estimated_sig_count = estimate_signature_count_from_text(text or "")
            if estimated_sig_count > 0:
                import logging
                logging.info(f"[FLAT PDF] Estimated {estimated_sig_count} signatures from text for {src_url}")
                widgets["signature_count"] = estimated_sig_count

        # SIGNATURE INFERENCE: When notarization is required, signatures are inherently needed
        # If notarization detected but no signature fields found, infer minimum signatures
        if flags.get("notarization_required") and widgets.get("signature_count", 0) == 0:
            import logging
            # Notarized documents typically need: 1) applicant signature, 2) notary signature
            # Check text for additional signature requirements (witnesses, co-signers, etc.)
            text_sig_count = estimate_signature_count_from_text(text or "")
            inferred_count = max(2, text_sig_count) if text_sig_count > 0 else 2
            logging.info(f"[SIG INFERENCE] Notarization required but no signatures detected. Inferring {inferred_count} signatures for {src_url}")
            widgets["signature_count"] = inferred_count

            # Also check for witness signatures in text
            if flags.get("witness_text") and widgets.get("witness_signature_count", 0) == 0:
                widgets["witness_signature_count"] = 1

        images_per_page = (image_count / pages) if pages > 0 else 0.0
        drawings_per_page = 0.0

        unique_roles = 1 if widgets.get("signature_count", 0) > 0 else 0
        if widgets.get("witness_signature_count", 0) > 0:
            unique_roles += 1

        complexity = compute_complexity(
            pages=pages,
            field_count=widgets.get("field_count", 0),
            signature_count=widgets.get("signature_count", 0),
            witness_signature_count=widgets.get("witness_signature_count", 0),
            attachments_required=flags["attachments_required"],
            attachment_count=0,
            conditional_logic=flags["conditional_logic"],
            unique_roles=unique_roles,
            third_party_involved=flags["third_party_involved"],
            pii_hits=flags["pii_hits"],
            radio_groups=radio_groups,
            images_per_page=images_per_page,
            drawings_per_page=drawings_per_page,
            xfa_hint=xfa_hint,
            deadlines_present=flags["deadlines_present"],
            dependencies_present=flags["form_dependencies"],
            notarization_required=flags["notarization_required"],
            identification_required=flags["identification_required"],
            data_validation_fields=flags["data_validation_fields"],
            payment_required=flags["payment_required"],
        )
        nigo = compute_nigo_risk(
            data_validation_fields=flags["data_validation_fields"],
            attachments_required=flags["attachments_required"],
            attachment_count=0,
            notarization_required=flags["notarization_required"],
            identification_required=flags["identification_required"],
            deadlines_present=flags["deadlines_present"],
            conditional_logic=flags["conditional_logic"],
            dependencies_present=flags["form_dependencies"],
            witness_signature_count=widgets.get("witness_signature_count", 0),
            third_party_involved=flags["third_party_involved"],
            unique_roles=unique_roles,
            radio_groups=radio_groups,
            pages=pages,
            field_count=widgets.get("field_count", 0),
            payment_required=flags["payment_required"],
        )

        est_signer, est_ops = estimate_times(
            pages=pages,
            field_count=widgets.get("field_count", 0),
            signature_count=widgets.get("signature_count", 0),
            attachment_count=0
        )

        # Title and entity name (LLM-first + debug)
        # Use crawl_root_url for better context if available
        llm_context_url = crawl_root_url if crawl_root_url else src_url
        title, entity_name, title_debug = build_title_with_debug(
            url=llm_context_url or "", text=text or "", reader=reader, default_name=name, skip_llm_title=skip_llm_title
        )

        # Use crawl_root_url for entity/vertical classification if provided (ensures consistency across crawl)
        # If crawl_root_url not provided, fall back to individual PDF URL
        classification_url = crawl_root_url if crawl_root_url else pdf_url
        industry_vertical, industry_subvertical = classify_industry(entity_name, classification_url)

        # Calculate same_domain: check if PDF URL matches crawl root domain
        # If crawl_root_url provided, compare domains; otherwise assume same domain
        same_domain = _domains_match(pdf_url, crawl_root_url) if crawl_root_url else True

        key_drivers = []
        if flags["attachments_required"]: key_drivers.append("Attachments required")
        if flags["notarization_required"]: key_drivers.append("Notary required")
        if flags["conditional_logic"]:     key_drivers.append("Conditional logic")
        if widgets.get("witness_signature_count", 0) > 0 or flags["witness_text"]: key_drivers.append("Witness signature")
        if widgets.get("signature_count", 0) > 1: key_drivers.append("Multiple signatures")
        if (flags.get("pii_hits") or 0) > 0:          key_drivers.append("Collects PII")
        if flags["deadlines_present"]:     key_drivers.append("Deadlines present")
        if flags["form_dependencies"]:     key_drivers.append("Dependencies on other forms")
        if flags["payment_required"]:      key_drivers.append("Payment required")
        if images_per_page >= 1.0:         key_drivers.append("High visual density")

        action_type = _determine_action_type(flags, widgets)

        # Detect special requirements not captured by other indicators
        special_requirements = detect_special_requirements(text or "")

        # SAFETY CHECK: Run vision analysis for problematic forms
        # Even if not detected as flat PDF, vision can help validate forms with:
        # 1. Zero fields (might be scanned/image-based)
        # 2. No signatures (might have visual signature boxes we missed)
        # 3. Low complexity scores (might have missed form elements)
        if not vision_already_ran and not vision_data and content:  # Only if vision not already run and we have PDF content
            needs_vision_check = False
            vision_reasons = []

            # Check 1: Zero fields (but not a completely empty/broken PDF)
            if widgets.get("field_count", 0) == 0 and pages > 0 and len(text or "") > 50:
                needs_vision_check = True
                vision_reasons.append("zero_fields")

            # Check 2: No signatures (but form might have visual signature boxes)
            if widgets.get("signature_count", 0) == 0 and pages > 0 and len(text or "") > 100:
                needs_vision_check = True
                vision_reasons.append("no_signatures")

            # Check 3: Very low complexity (might have missed form elements)
            # Complexity < 2.0 is suspiciously low for real forms
            if complexity < 2.0 and pages > 0 and len(text or "") > 100:
                needs_vision_check = True
                vision_reasons.append(f"low_complexity_{complexity:.1f}")

            # Check 4: Gemini Low Confidence / Failure (User Request: "If the confidence score of the gemini output is low, use Claude Sonnet")
            # We use "llm_title_rejected" (poor quality) or "llm_error" as proxies for low confidence
            if not needs_vision_check and not skip_llm_title:
                if title_debug.get("llm_title_rejected") or title_debug.get("llm_error"):
                    needs_vision_check = True
                    reason = "poor_quality_title" if title_debug.get("llm_title_rejected") else "llm_error"
                    vision_reasons.append(f"gemini_low_confidence_{reason}")
                    import logging
                    logging.info(f"[VISION FALLBACK] Gemini confidence low/failed ({reason}), falling back to Claude Sonnet for {src_url}")

            # Skip vision if requested (for fast re-analysis)
            if needs_vision_check and not skip_vision:
                try:
                    import logging
                    logging.info(f"[VISION SAFETY] Running vision analysis for problematic form: {src_url} (reasons: {', '.join(vision_reasons)})")

                    # Respect env-configured provider/model; default to Anthropic Sonnet for the safety pass
                    safety_provider = os.getenv("LLM_PROVIDER", "anthropic")
                    safety_model = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022")
                    vision_data = analyze_flat_pdf_with_vision(
                        pdf_bytes=content,
                        source_url=src_url or "",
                        provider=safety_provider,
                        model=safety_model,
                    )
                    vision_already_ran = True
                    logging.info(f"[VISION SAFETY] Analysis complete: is_actionable={vision_data.get('is_actionable')}, vision_analyzed={vision_data.get('vision_analyzed')}")
                except Exception as e:
                    import logging
                    logging.error(f"[VISION SAFETY] Error during vision analysis: {e}", exc_info=True)
                    vision_data = {"is_actionable": True, "vision_analyzed": False, "error": str(e)}
                    vision_already_ran = True

        # If vision analysis was performed, merge results and check actionability
        if vision_data and vision_data.get("vision_analyzed"):
            # Create temp record for merging
            temp_record = {
                "source_url": src_url,
                "field_count": widgets.get("field_count", 0),
                "signature_analysis": widgets,
                "attachments_required": flags["attachments_required"],
                "identification_required": flags["identification_required"],
                "conditional_logic": flags["conditional_logic"],
                "data_validation_fields": flags["data_validation_fields"],
            }
            temp_record = merge_vision_results_into_record(temp_record, vision_data)

            # Update widgets and flags with vision-enhanced data
            if "signature_analysis" in temp_record:
                widgets = temp_record["signature_analysis"]
                # Ensure widgets dict has all required keys with default 0
                if not isinstance(widgets, dict):
                    widgets = {}
                for key in ["field_count", "text_fields", "checkboxes", "dropdowns",
                           "signature_count", "witness_signature_count", "conditional_signature_count"]:
                    if key not in widgets or widgets[key] is None:
                        widgets[key] = 0
            if "field_count" in temp_record:
                field_count_value = temp_record["field_count"]
                widgets["field_count"] = field_count_value if field_count_value is not None else 0
            if "attachments_required" in temp_record:
                flags["attachments_required"] = temp_record["attachments_required"]
            if "identification_required" in temp_record:
                flags["identification_required"] = temp_record["identification_required"]
            if "conditional_logic" in temp_record:
                flags["conditional_logic"] = temp_record["conditional_logic"]

            # Recalculate complexity and NIGO with vision-enhanced data
            attachment_count = temp_record.get("attachment_count", 0)
            complexity = compute_complexity(
                pages=pages,
                field_count=widgets.get("field_count", 0),
                signature_count=widgets.get("signature_count", 0),
                witness_signature_count=widgets.get("witness_signature_count", 0),
                attachments_required=flags["attachments_required"],
                attachment_count=attachment_count,
                conditional_logic=flags["conditional_logic"],
                unique_roles=unique_roles,
                third_party_involved=flags["third_party_involved"],
                pii_hits=flags["pii_hits"],
                radio_groups=radio_groups,
                images_per_page=images_per_page,
                drawings_per_page=drawings_per_page,
                xfa_hint=xfa_hint,
                deadlines_present=flags["deadlines_present"],
                dependencies_present=flags["form_dependencies"],
                notarization_required=flags["notarization_required"],
                identification_required=flags["identification_required"],
                data_validation_fields=flags["data_validation_fields"],
            )
            nigo = compute_nigo_risk(
                data_validation_fields=flags["data_validation_fields"],
                attachments_required=flags["attachments_required"],
                attachment_count=attachment_count,
                notarization_required=flags["notarization_required"],
                identification_required=flags["identification_required"],
                deadlines_present=flags["deadlines_present"],
                conditional_logic=flags["conditional_logic"],
                dependencies_present=flags["form_dependencies"],
                witness_signature_count=widgets.get("witness_signature_count", 0),
                third_party_involved=flags["third_party_involved"],
                unique_roles=unique_roles,
                radio_groups=radio_groups,
                pages=pages,
                field_count=widgets.get("field_count", 0),
            )

        base_record.update({
            "_flags": flags,
            "entity_name": entity_name,
            "industry_vertical": industry_vertical,
            "industry_subvertical": industry_subvertical,

            "pages": pages,
            # full_text is intentionally truncated and redacted; do not extend
            # without security review (see _redact_and_truncate_text).
            "full_text": _redact_and_truncate_text(text or ""),
            "field_count": widgets.get("field_count", 0),
            "text_fields": widgets.get("text_fields", 0),
            "checkboxes": widgets.get("checkboxes", 0),
            "dropdowns": widgets.get("dropdowns", 0),
            "signature_analysis": widgets,
            # Promote nested counts to top-level so the dashboard's
            # generic column reads them without unwrapping signature_analysis.
            # Vision can later overwrite signature_analysis.signature_count
            # via merge_vision_results_into_record(); we re-sync at end.
            "signature_count": int(widgets.get("signature_count", 0) or 0),

            "notarization_required": flags["notarization_required"],
            "attachments_required": flags["attachments_required"],
            "conditional_logic": flags["conditional_logic"],
            "deadlines_present": flags["deadlines_present"],
            "form_dependencies": flags["form_dependencies"],
            "third_party_involved": flags["third_party_involved"],
            "identification_required": flags["identification_required"],
            "click_to_agree": flags["click_to_agree"],
            "payment_required": flags["payment_required"],
            "payment_amount": flags.get("payment_amount"),
            "same_domain": same_domain,  # Use calculated value instead of hardcoded True
            "witnesses_required": (widgets.get("witness_signature_count", 0) > 0 or flags["witness_text"]),
            "data_validation_fields": flags["data_validation_fields"],

            "estimated_signer_time": est_signer,
            "estimated_processing_time": est_ops,
            "complexity_score": complexity,
            "nigo_score": nigo,

            "key_drivers": key_drivers,
            "special_requirements": special_requirements,

            "form_name": title,
            "form_title": base_record.get("form_title", title),
            "pretty_title": title,
            "title_debug": title_debug,

            # NEW
            "action_type": action_type,

            "status": "ok",
            "parse_error": None
        })

        # Add vision analysis results if performed
        if vision_data:
            base_record["_vision_data"] = vision_data
            base_record["vision_analyzed"] = vision_data.get("vision_analyzed", False)
            base_record["is_actionable"] = vision_data.get("is_actionable", True)
            # Merge additional vision fields
            for key in ["attachment_list", "attachment_count", "id_verification_type", "conditional_logic_details"]:
                if key in vision_data:
                    base_record[key] = vision_data[key]

            # Vision may have detected sigs the AcroForm widgets missed
            # (printed-and-signed forms). Promote the higher count to
            # both signature_analysis and the top-level signature_count
            # so the dashboard renders a useful number.
            vision_sig_count = int(vision_data.get("signature_count", 0) or 0)
            if vision_sig_count > 0:
                widgets["signature_count"] = max(
                    int(widgets.get("signature_count", 0) or 0),
                    vision_sig_count,
                )
                base_record["signature_count"] = widgets["signature_count"]
                base_record["signature_analysis"] = widgets

        # Apply domain-based metadata using crawl root domain if available
        # This ensures PDFs hosted on CDNs (like HubSpot) get entity names from the org's actual domain
        domain_url = crawl_root_url if crawl_root_url else src_url
        if domain_url:
            base_record = domain_mappings.apply_domain_metadata(base_record, domain_url)

        # Classify business impact (Revenue, Expenses, Regulatory)
        base_record["business_impact"] = classify_business_impact(base_record)

        # Calculate defendable form value
        value_type, form_value = calculate_form_value(
            payment_amount=base_record.get("payment_amount"),
            form_name=base_record.get("form_name", ""),
            action_type=base_record.get("action_type", ""),
            industry_vertical=base_record.get("industry_vertical", ""),
            industry_subvertical=base_record.get("industry_subvertical", "")
        )
        base_record["value_type"] = value_type
        base_record["form_value"] = form_value

        return base_record

    except Exception as e:
        import traceback
        error_msg = str(e) or e.__class__.__name__
        error_trace = traceback.format_exc()

        # Store detailed error (first 500 chars of message + type)
        base_record["parse_error"] = f"{error_msg} [{e.__class__.__name__}]"[:500]

        # Log full error for debugging
        print(f"[ANALYZER ERROR] {src_url or 'unknown'}")
        print(f"  Error: {error_msg}")
        print(f"  Type: {e.__class__.__name__}")
        print(f"  Trace: {error_trace[:1000]}")

        return base_record