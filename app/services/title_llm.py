# app/services/title_llm.py
# LLM-based title generator for forms.
# Uses llm_router to support multiple providers (OpenAI, Anthropic, XAI, Gemini)

import os
import re
import traceback
from app.services.llm_router import chat_complete, LLMError
from app.services.sms_alerts import send_llm_failure_alert

# Organization name normalization map (case-insensitive matching)
ORG_NORMALIZATIONS = {
    # U.S. Government Agencies
    r'(opm|office of personnel management|u\.?s\.? office of personnel management|united states office of personnel management)': 'U.S. Office of Personnel Management',
    r'(irs|internal revenue service|u\.?s\.? internal revenue service)': 'Internal Revenue Service',
    r'(gsa|general services administration|u\.?s\.? general services administration)': 'U.S. General Services Administration',
    r'(fda|food and drug administration|u\.?s\.? food and drug administration)': 'U.S. Food and Drug Administration',
    r'(dod|department of defense|u\.?s\.? department of defense)': 'U.S. Department of Defense',
    r'(va|department of veterans affairs|u\.?s\.? department of veterans affairs|veterans affairs)': 'U.S. Department of Veterans Affairs',
    r'(ssa|social security administration|u\.?s\.? social security administration)': 'U.S. Social Security Administration',
    r'(epa|environmental protection agency|u\.?s\.? environmental protection agency)': 'U.S. Environmental Protection Agency',

    # Financial Institutions - Banks & Brokerages
    r'(charles schwab|schwab|the charles schwab corporation|charles schwab corporation|charles schwab & co\.?)': 'Charles Schwab Corporation',
    r'(fidelity|fidelity investments|fidelity brokerage)': 'Fidelity Investments',

    # Credit Unions
    r'(aagcu|alaska air group credit union|alaska air group cu)': 'Alaska Air Group Credit Union',
    r'(becu|boeing employees credit union|boeing employees\' credit union)': 'Boeing Employees Credit Union',
    r'(gtfcu|greater texas federal credit union|greater texas fcu)': 'Greater Texas Federal Credit Union',
    r'(texasbaycu|texas bay area credit union|texas bay cu|texas bay area cu)': 'Texas Bay Area Credit Union',
}

def normalize_org_name(org_name: str) -> str:
    """
    Normalize organization names to canonical forms.
    Handles common variations like "OPM" -> "U.S. Office of Personnel Management"
    """
    if not org_name or not org_name.strip():
        return org_name

    cleaned = org_name.strip()
    cleaned_lower = cleaned.lower()

    # Check each normalization pattern
    for pattern, canonical in ORG_NORMALIZATIONS.items():
        if re.match(f'^{pattern}$', cleaned_lower, re.IGNORECASE):
            return canonical

    # No match found, return original
    return cleaned

PROMPT = """You are an expert at creating beautiful, concise titles for form documents in a professional forms catalog.

Given:
- url: {url}
- first_page_text (truncated): {fp}

CRITICAL: Output EXACTLY TWO LINES with NO additional text, explanation, or preamble:
Line 1: Form title (following rules below)
Line 2: Entity/Organization name (full name, not acronym)

DO NOT include any explanations, analysis, or additional text. ONLY output the two lines above.

TITLE RULES:
1) Extract the ACTUAL FORM TITLE from the document, not form codes, field labels, or addresses
2) DO NOT extract text that contains colons (":") - these are field labels, not titles
3) DO NOT extract addresses (e.g., "Po Box", "123 Main Street") - these are NOT titles
4) DO NOT include form codes/numbers from filenames (e.g., "P-1250", "FM-AO", "SCU-4098") in the title
5) DO NOT include filename fragments (e.g., "Packet", "Interactive", "v2") in the title
6) Create a clear, professional title in Title Case, ≤ 75 characters
7) Make descriptions concise and action-oriented (prefer "Application" over "Application Form")
8) Remove all file extensions (.pdf, .docx), version numbers (v1, v2), and dates
9) Use proper capitalization for acronyms (IRS, TSP, OPM, not Irs, Tsp, Opm)
10) If the PDF has a large, prominent title or heading near the top, use that as the form title
11) For government forms with official codes (e.g., "SF 2826", "IRS 1040"), preserve the code ONLY if it appears in the actual PDF title

COMMON MISTAKES TO AVOID:
❌ "Member Name:" - This is a field label (has colon), NOT a title
❌ "Po Box" - This is an address fragment, NOT a title
❌ "P - 1250 Becu Successor in Interest Packet" - Includes form code and filename word "Packet"
❌ "Fm - Ao - Account Options" - Includes form code "FM-AO" from filename
❌ "Untitled Form" - Read the actual PDF text to find the real title

CORRECT EXAMPLES:
✓ "Successor in Interest Information Sheet" - Clean title without form code
✓ "Member Change of Information" - Extracted from PDF content, not field label
✓ "Account Options Form" - Clean title without form code "FM-AO"
✓ "Overdrafts and Overdraft Fees" - Extracted actual title from PDF text
✓ "Your Billing Rights - Open-Ended Lines of Credit" - Full descriptive title from PDF

ENTITY NAME RULES:
1) Extract the full organization name from the PDF text (usually appears at top or bottom of first page)
2) If PDF text only shows partial name (e.g., "Credit Union"), use the URL domain as a clue to identify the full organization
3) Use the full, official name - NO acronyms (e.g., "Alaska Air Group Credit Union" not "AAGCU")
4) Common URL patterns: aagcu.org → Alaska Air Group Credit Union, becu.org → Boeing Employees Credit Union
5) If no organization name found, return "Unknown"

CORRECT OUTPUT FORMAT (two lines only, no explanations):
Life Insurance Election
Charles Schwab Corporation

CORRECT OUTPUT FORMAT (two lines only, no explanations):
Christmas Club Application
Alaska Air Group Credit Union

INCORRECT OUTPUT (includes explanation):
Based on the provided text and rules:
Life Insurance Election
Charles Schwab Corporation
Explanation: ...
"""

def make_title(url: str, first_page_text: str) -> tuple[str, str]:
    """
    Ask LLM (via llm_router) to produce a high-quality, compact title and entity name.
    Returns (title, entity_name) tuple. Returns ("", "") if LLM is not configured or any error occurs.
    """
    # Get provider and model from environment
    # Default to 'gemini' per user request (was 'openai')
    provider = (os.getenv("LLM_PROVIDER") or "gemini").lower().strip()

    # Get model with provider-specific defaults
    if provider == "openai":
        model = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
    elif provider == "anthropic":
        model = (os.getenv("ANTHROPIC_MODEL") or "claude-3-5-sonnet-20241022").strip()
    elif provider == "xai":
        model = (os.getenv("GROK_MODEL") or "grok-beta").strip()
    elif provider == "gemini":
        model = (os.getenv("GEMINI_MODEL") or "gemini-1.5-flash").strip()
    else:
        model = "gpt-4o-mini"  # Default fallback

    txt = (first_page_text or "").strip()
    if len(txt) > 1200:
        txt = txt[:1200]

    messages = [
        {"role": "system", "content": "You are a professional document titling expert. Create beautiful, concise, properly formatted titles for forms catalogs and extract organization names."},
        {"role": "user", "content": PROMPT.format(url=url or "", fp=txt or "")}
    ]

    try:
        response = chat_complete(
            provider=provider,
            model=model,
            messages=messages,
            max_tokens=120,
            temperature=0.1,
            timeout=20.0,
            retries=1
        )

        # Handle None response from LLM failure
        if response is None or not response:
            return "", ""

        response = response.strip()

        # Split response into title and entity name (two lines)
        # Filter out empty lines and lines that look like explanations
        lines = [line.strip() for line in response.split('\n') if line.strip()]

        # Remove lines that start with common explanation patterns
        filtered_lines = []
        skip_patterns = [
            'based on',
            'explanation:',
            'analysis:',
            'note:',
            'reasoning:',
            'here is',
            'here are',
            'i will',
            'i have',
            'the title',
            'the entity'
        ]

        for line in lines:
            line_lower = line.lower()
            # Skip explanation lines
            if any(line_lower.startswith(pattern) for pattern in skip_patterns):
                continue
            # Skip lines that look like markdown headers or separators
            if line.startswith('#') or line.startswith('-') * 3:
                continue
            filtered_lines.append(line)
            # Stop after we have 2 good lines
            if len(filtered_lines) >= 2:
                break

        title = filtered_lines[0] if len(filtered_lines) > 0 else ""
        entity_name = filtered_lines[1] if len(filtered_lines) > 1 else ""

        # Clean up title
        title = title.strip('"\'`*_')
        title = title.replace(".pdf", "").replace(".PDF", "").replace(".docx", "").replace(".DOCX", "")
        title = title.strip(" .-–—")
        title = ' '.join(title.split())
        title = title.replace(' — ', ' — ').replace('—', ' — ').replace('  —  ', ' — ')
        if len(title) > 80:
            title = title[:77].rsplit(' ', 1)[0] + '...'

        # Clean up entity name
        entity_name = entity_name.strip('"\'`*_')
        entity_name = ' '.join(entity_name.split())
        if entity_name.lower() == "unknown" or not entity_name:
            entity_name = None
        else:
            # Normalize organization name to canonical form
            entity_name = normalize_org_name(entity_name)

        return title, entity_name
    except Exception as e:
        # Log the error
        error_type = type(e).__name__
        error_msg = str(e)
        provider = (os.getenv("LLM_PROVIDER") or "openai").lower().strip()

        print(f"[LLM ERROR] {error_type}: {error_msg}")

        # Send SMS alert (throttled to prevent spam)
        alert_message = f"LLM title generation failed\n\nProvider: {provider}\nError: {error_type}\nDetails: {error_msg[:100]}"
        send_llm_failure_alert(alert_message)

        return "", ""
