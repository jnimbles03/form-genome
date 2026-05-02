"""
LLM-powered form title normalization service.

Cleans up messy form titles like:
- "Pfdesktopinternethttpjnet.ao.dcnimgassets4931prob0008s.wpdOB" → "Form 4931"
- "Gift Pledge Form — Gift — Pledge — Form_jan_2016_0 1.pdfdf" → "Gift Pledge Form"
- "2729.09.09.e1.pmdmd" → "Form 2729-09-09-E1"
"""
from __future__ import annotations
import re
from typing import Optional
from app.services import llm_router


def basic_normalize(title: str) -> str:
    """
    Basic normalization without LLM (fast, deterministic).
    Removes file extensions, URLs, and common artifacts.
    """
    if not title:
        return "Untitled Form"

    # Start with basic cleanup
    s = str(title).strip()
    s = re.sub(r'[\u200B-\u200D\uFEFF]', '', s)  # Zero-width chars
    s = re.sub(r'[–—]', '-', s)  # Normalize dashes

    # Remove URL fragments and paths
    s = re.sub(r'^(https?://|file://|[a-z]:\\)', '', s, flags=re.I)
    s = re.sub(r'[/\\]([^/\\]+)$', r'\1', s)  # Keep only filename

    # Remove file extensions (single and double)
    s = re.sub(r'\.(pdf|pmd|wpd|doc|docx|xls|xlsx|txt|rtf|odt|ods)(pdf|pmd|wpd|doc|docx|OB|df|md)?$', '', s, flags=re.I)

    # Clean up common artifacts
    s = re.sub(r'_+', ' ', s)  # Underscores to spaces
    s = re.sub(r'\s*-\s*', ' - ', s)  # Normalize dashes
    s = re.sub(r'\s{2,}', ' ', s)  # Collapse spaces

    return s.strip() or "Untitled Form"


def llm_normalize(title: str, use_cache: bool = True) -> str:
    """
    Use LLM to intelligently normalize a form title.

    Examples:
        "Pfdesktopinternethttpjnet.ao.dcnimgassets4931prob0008s" → "Problem Form 0008"
        "2729.09.09.e1" → "Form 2729-09-09-E1"
        "Gift Pledge Form — Gift — Pledge" → "Gift Pledge Form"
        "W-2" → "W-2 Wage and Tax Statement"

    Args:
        title: Raw form title to normalize
        use_cache: Use cached results (not implemented yet)

    Returns:
        Cleaned, professional form title
    """
    if not title or len(title.strip()) == 0:
        return "Untitled Form"

    # First apply basic normalization
    basic = basic_normalize(title)

    # Skip LLM for very short/simple titles
    if len(basic) < 5:
        return basic

    # Skip LLM if title already looks clean
    if _looks_clean(basic):
        return basic

    # Use LLM to intelligently clean the title
    try:
        prompt = f"""You are a form title cleanup expert. Clean up this messy form title into a professional, readable name.

Rules:
1. Remove file extensions, paths, URLs, and technical artifacts
2. Convert filename-like strings to proper form names
3. Identify form numbers and preserve them (e.g., W-2, Form 1040, SF-813)
4. Remove duplicate/redundant words
5. Use proper title case
6. Keep it concise (under 80 characters)
7. If the title is mostly garbage/corrupted, infer what the form likely is based on any readable parts
8. Common form patterns:
   - Tax forms: W-2, 1040, 1099, etc.
   - Federal forms: SF-XXX (Standard Form)
   - Medical: HIPAA, consent forms
   - Financial: account applications, credit agreements
   - Employment: I-9, applications

Input title: "{basic}"

Output ONLY the cleaned title, nothing else. No explanation, no quotes, just the title.

Examples:
Input: "Pfdesktopinternethttpjnet.ao.dcnimgassets4931prob0008s"
Output: Problem Form 0008

Input: "Gift Pledge Form — Gift — Pledge"
Output: Gift Pledge Form

Input: "2729.09.09.e1"
Output: Form 2729-09-09-E1

Input: "w-2_form_2023"
Output: W-2 Wage and Tax Statement

Input: "SF 813 — Verification of Military Retireess Service"
Output: SF-813 Verification of Military Retiree Service

Cleaned title:"""

        result = llm_router.chat_complete(
            provider="openai",  # Fast and cheap for simple tasks
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=100,
            temperature=0.0,  # Deterministic
            timeout=10.0,
            retries=1,
            fallback=True
        )

        # Clean up the LLM response
        cleaned = result.strip().strip('"').strip("'").strip()

        # Sanity checks
        if not cleaned or len(cleaned) < 2:
            print(f"[TITLE_NORM] LLM returned invalid result for '{title}': '{cleaned}'")
            return basic

        if len(cleaned) > 120:
            print(f"[TITLE_NORM] LLM returned too long result for '{title}': '{cleaned[:50]}...'")
            return basic

        print(f"[TITLE_NORM] '{title}' → '{cleaned}'")
        return cleaned

    except Exception as e:
        print(f"[TITLE_NORM] LLM error for '{title}': {e}")
        return basic


def _looks_clean(title: str) -> bool:
    """
    Check if a title already looks clean and doesn't need LLM processing.

    A title looks clean if:
    - No file extensions remaining
    - No excessive numbers or dots
    - No URL/path fragments
    - Mostly alphabetic characters
    """
    # Has file extension
    if re.search(r'\.(pdf|pmd|wpd|doc|txt)$', title, re.I):
        return False

    # Too many dots/numbers (technical artifact)
    if title.count('.') > 2 or (sum(c.isdigit() for c in title) / len(title) > 0.6):
        return False

    # URL fragments
    if 'http' in title.lower() or '\\' in title or title.count('/') > 2:
        return False

    # Mostly special characters
    alpha_ratio = sum(c.isalpha() for c in title) / len(title) if len(title) > 0 else 0
    if alpha_ratio < 0.4:
        return False

    return True


def batch_normalize(titles: list[str], use_llm: bool = True) -> dict[str, str]:
    """
    Normalize a batch of titles.

    Args:
        titles: List of raw titles to normalize
        use_llm: Use LLM for intelligent cleanup (slower but better)

    Returns:
        Dict mapping original title → normalized title
    """
    results = {}

    for title in titles:
        if use_llm:
            results[title] = llm_normalize(title)
        else:
            results[title] = basic_normalize(title)

    return results
