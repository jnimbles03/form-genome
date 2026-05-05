# app/services/llm_discover.py
# LLM-based PDF discovery with enhanced debugging
from __future__ import annotations

import os
import re
import json
import urllib.parse as up
import requests
from typing import List, Dict, Any, Tuple

from app.services.llm_router import chat_complete, LLMError
from app.services import politeness

# -----------------------------------------------------------------------------
# HTTP helpers
# -----------------------------------------------------------------------------
# F-CS-10: honest UA across the codebase, no Chrome impersonation.
BROWSER_HEADERS = {
    "User-Agent": politeness.USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Connection": "close",
}

PDF_RE    = re.compile(r"https?://[^\s\"'<>]+\.pdf(?:[?#][^\s\"'<>\]]*)?", re.I)

def _norm(u: str) -> str:
    if not u:
        return u
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    p = up.urlsplit(u)
    path = up.quote(up.unquote(p.path), safe="/:@-._~!$&'()*+,;=")
    return up.urlunsplit((p.scheme or "https", p.netloc, path, p.query, p.fragment))

def fetch(url: str, timeout: float = 10.0) -> Tuple[int, str, Dict[str, str]]:
    """Fetch a single URL (HTML)."""
    r = requests.get(url, headers=BROWSER_HEADERS, timeout=timeout, allow_redirects=True)
    return r.status_code, (r.text or ""), {k.lower(): v for k, v in r.headers.items()}

def try_sitemap(seed: str, timeout: float = 8.0) -> List[str]:
    """Return a (possibly empty) list of absolute URLs found in sitemap files."""
    out: List[str] = []
    try:
        p = up.urlsplit(seed)
        host = f"{p.scheme}://{p.netloc}"
        for path in ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml"):
            s = host + path
            try:
                code, text, _ = fetch(s, timeout=timeout)
                if code // 100 == 2 and ("</urlset>" in text or "</sitemapindex>" in text):
                    out.extend(re.findall(r"https?://[^\s\"'<>]+", text))
            except Exception:
                pass
    except Exception:
        pass
    # normalize + de-dupe
    return list(dict.fromkeys(_norm(u) for u in out))

# -----------------------------------------------------------------------------
# Asset hint extraction (iframe/src, absolute .pdf, JSON blocks)
# -----------------------------------------------------------------------------
def extract_pdf_hints(seed_html: str, base: str) -> list[str]:
    """Pull reasonable .pdf candidates directly from HTML before LLM."""
    hints = set()
    html = seed_html or ""

    # iframe/object/embed sources
    for m in re.finditer(r"""<(?:iframe|embed|object)[^>]+?(?:src|data)=["']([^"']+)["']""", html, re.I):
        hints.add(_norm(up.urljoin(base, m.group(1).strip())))

    # absolute .pdf anywhere in HTML (including content.*)
    for m in re.finditer(r"https?://[^\s\"'<>]+\.pdf(?:[?#][^\s\"']*)?", html, re.I):
        hints.add(_norm(m.group(0)))

    # JSON/Ld & app-state keys with URLs
    for m in re.finditer(r"<script[^>]*>([\s\S]*?)</script>", html, re.I):
        block = (m.group(1) or "")
        for key in ("pdfUrl", "document", "file", "fileUrl", "url"):
            for mm in re.finditer(rf'"{key}"\s*:\s*"([^"]+\.pdf[^"]*)"', block, re.I):
                hints.add(_norm(mm.group(1)))

    # Heuristic: prefer same org or obvious pdfs
    out = []
    for u in hints:
        host = (up.urlsplit(u).hostname or "").lower()
        if u.lower().endswith(".pdf") or host.endswith("schwab.com") or "content.schwab.com" in host:
            out.append(u)
    return list(dict.fromkeys(out))[:80]  # cap to keep prompt small


# -----------------------------------------------------------------------------
# Multi-provider LLM call (via llm_router) - No fallback for clearer error messages
# -----------------------------------------------------------------------------
def _llm_call(provider: str, model: str, messages: List[Dict[str, str]],
              *, max_tokens: int, timeout: float, retries: int) -> str:
    """Call LLM without fallback chain to see actual provider errors"""
    return chat_complete(
        provider=provider, model=model, messages=messages,
        max_tokens=max_tokens, temperature=0.0,
        timeout=timeout, retries=retries, fallback=False  # No fallback - show real errors
    )

# -----------------------------------------------------------------------------
# Prompts (nudged for resource/viewer behavior)
# -----------------------------------------------------------------------------
PROMPT_SMALL = """You are a forms discovery agent extracting PDF form links.

EXAMPLES to INCLUDE:
✓ https://site.com/forms/application-benefits.pdf
✓ https://content.schwab.com/forms/SF-2826.pdf
✓ Forms with codes: SF-*, OPM-*, IRS Form *, Form W-*, etc.

EXAMPLES to EXCLUDE:
✗ annual-report.pdf, whitepaper.pdf, handbook.pdf

Return STRICT JSON:
{"pdf_urls":[ "https://...pdf", ... ], "paths":[ "/forms/", "/applications/", ... ], "expected_total": 50}

Rules:
- ONLY actionable forms: application, request, enrollment, authorization, consent, affidavit, declaration, appeal, designation, verification, waiver, election, claim
- EXCLUDE: report, handbook, guide, brochure, whitepaper, presentation, transcript, policy, publication, booklet
- If viewer page, extract embedded PDF URL
- "pdf_urls": absolute HTTPS ending .pdf
- "paths": promising directories to crawl
- "expected_total": YOUR BEST ESTIMATE of total forms available on this site (based on sitemap, page structure, etc.)
- Return 50-200 candidates ranked by form probability
- JSON ONLY, no markdown, no commentary
"""

PROMPT_LARGE = """You are a forms discovery agent.
Return a strict JSON object only:
{"pdf_urls":[ "https://...pdf", ... ], "paths":[ "/forms/...", "/applications", ... ], "expected_total": 50}
Context samples:
- Seed page HTML excerpts (top/tail)
- A short list of likely form pages from sitemap
- Asset hints discovered in HTML (iframe/src or JSON with pdfUrl/file/document/url keys)

Rules:
- ONLY actionable PDFs: applications, requests, enrollment, authorizations, consents, affidavits, declarations, appeals, designations, verifications.
- Exclude marketing PDFs (reports, handbooks, brochures, whitepapers).
- Prefer content hosted on content.* or client.* subdomains when they serve the actual PDF (even with query strings).
- Treat viewer endpoints that load PDFs in <iframe>/<embed>/<object> as the actual PDF links.
- "pdf_urls": absolute https URLs ending .pdf on the same organization
- "paths": short same-site paths worth probing next
- "expected_total": YOUR BEST ESTIMATE of total forms available (based on sitemap size, page mentions, directory structure, etc.)
- No commentary—JSON only. Target 50–200 best candidates.
"""

# -----------------------------------------------------------------------------
# Main entry
# -----------------------------------------------------------------------------
def discover(seed: str, seed_html: str, sitemap_urls: List[str],
             provider: str | None = None, llm_model: str | None = None) -> Dict[str, Any]:
    """
    LLM-first discovery with fast sitemap hints + asset hints from HTML.
    Returns dict: { pdf_urls: [...], paths:[...], note?: str, error?: str }
    """
    seed = _norm(seed)
    host = up.urlsplit(seed).netloc or ""

    # 0) Quick harvest (non-blocking, immediate)
    # Prefer sitemap PDFs (often plenty on doc-heavy sites)
    def harvest_sitemap_quick(sitemap_urls: List[str], host: str) -> Dict[str, List[str]]:
        pdfs: List[str] = []
        pages: List[str] = []
        if "://" in host:
            host_root = (up.urlsplit(host).netloc or "").lower()
        else:
            host_root = (up.urlsplit("https://" + host).netloc or "").lower()
        for u in sitemap_urls or []:
            nu = _norm(u)
            h = (up.urlsplit(nu).netloc or "").lower()
            # allow org siblings (content/client.*)
            if host_root not in h:
                if not (host_root.split(".")[-2:] == h.split(".")[-2:]):
                    continue
            if PDF_RE.search(nu):
                pdfs.append(nu)
            else:
                path = up.urlsplit(nu).path or ""
                if re.search(r"(form|forms|application|request|enroll|enrollment|authorization|consent|affidavit|declaration|appeal|designation|verification)", path, re.I):
                    pages.append(nu)
        pdfs  = list(dict.fromkeys(pdfs))[:500]
        pages = list(dict.fromkeys(pages))[:200]
        return {"pdfs": pdfs, "form_pages": pages}

    quick = harvest_sitemap_quick(sitemap_urls or [], host)
    pdfs_quick  = quick["pdfs"]
    pages_quick = quick["form_pages"]

    ENOUGH = 50
    if len(pdfs_quick) >= ENOUGH:
        return {"pdf_urls": pdfs_quick[:1000], "paths": [], "expected_total": len(pdfs_quick), "note": "sitemap_quick"}

    # Resolve provider/model (allow env defaults)
    # User Request default: Gemini ("Gemini 2.5 Flash" -> mapped to latest Flash)
    prov = (provider or os.getenv("LLM_PROVIDER") or "gemini").lower().strip()
    mdl  = (llm_model or
            (os.getenv("GROK_MODEL") if prov == "xai" else
             os.getenv("OPENAI_MODEL") if prov == "openai" else
             os.getenv("ANTHROPIC_MODEL") if prov == "anthropic" else
             os.getenv("GEMINI_MODEL") if prov == "gemini" else 
             "gemini-2.0-flash")).strip()
    
    # Ensure Gemini Flash is used if provider is gemini and no model specified
    if prov == "gemini" and not mdl:
        mdl = "gemini-2.0-flash"

    # Asset hints from seed HTML (pre-LLM)
    asset_hints = extract_pdf_hints(seed_html, seed)

    # 1) Small "scout" call
    messages_small = [
        {"role": "system", "content": "You extract actionable PDF form links for the given site."},
        {"role": "user",   "content": (
            PROMPT_SMALL
            + "\nSeed URL:\n" + seed
            + (("\n\nDiscovered asset hints (from HTML):\n" + "\n".join(asset_hints)) if asset_hints else "")
        )}
    ]
    # Skip LLM if we already have enough PDFs from sitemap
    if len(pdfs_quick) >= ENOUGH:
        print(f"[llm_discover] Sitemap found {len(pdfs_quick)} PDFs, skipping LLM")
        return {"pdf_urls": pdfs_quick[:1000], "paths": pages_quick[:50], "expected_total": len(pdfs_quick), "note": "sitemap_sufficient"}

    try:
        print(f"[llm_discover] Calling {prov.upper()} ({mdl}) for PDF discovery...")
        raw_small = _llm_call(prov, mdl, messages_small, max_tokens=2000, timeout=30.0, retries=1).strip()
        print("[llm_discover] ✓ Small LLM completed, parsing results...")
        print("[llm_discover] raw(SMALL):", raw_small[:400])

        # Strip markdown code fences if present
        cleaned = re.sub(r'^```(?:json)?\s*\n?', '', raw_small, flags=re.MULTILINE)
        cleaned = re.sub(r'\n?```\s*$', '', cleaned, flags=re.MULTILINE)

        m = re.search(r"\{[\s\S]*\}", cleaned)
        obj = json.loads(m.group(0) if m else cleaned)
        pdfs = [ _norm(u) for u in (obj.get("pdf_urls") or []) if isinstance(u, str) ]
        paths= [ p.strip() for p in (obj.get("paths") or []) if isinstance(p, str) ]
        expected_total = obj.get("expected_total") or len(pdfs)  # Use LLM estimate or default to found count
        pdfs = [u for u in pdfs if u.lower().startswith("https://") and u.lower().endswith(".pdf")]
        print(f"[llm_discover] Found {len(pdfs)} PDFs from LLM, {len(paths)} paths to explore, expected total: ~{expected_total}")
        if len(pdfs) >= ENOUGH:
            print(f"[llm_discover] ✓ Sufficient PDFs found ({len(pdfs)}), returning early")
            return {"pdf_urls": list(dict.fromkeys(pdfs))[:1000], "paths": paths[:200], "expected_total": expected_total, "note": "llm_small"}
        pdfs = list(dict.fromkeys(pdfs_quick + pdfs))
        print(f"[llm_discover] Combined total: {len(pdfs)} PDFs (sitemap + LLM)")
    except Exception as e:
        err_str = str(e)
        print(f"[llm_discover] small LLM error: {err_str}")

        # Check for quota/auth errors
        if "quota" in err_str.lower() or "insufficient_quota" in err_str.lower():
            print("[llm_discover] ⚠️  OpenAI quota exceeded - add credits or use different provider")
        elif "unauthorized" in err_str.lower() or "invalid" in err_str.lower():
            print("[llm_discover] ⚠️  Invalid API key - check your configuration")

        print(f"[llm_discover] Falling back to sitemap harvest ({len(pdfs_quick)} PDFs) + HTML crawl")
        pdfs = pdfs_quick[:]  # keep quick harvest
        paths = pages_quick[:100]  # Use sitemap paths for HTML crawl
        expected_total = len(pdfs_quick) if pdfs_quick else 50  # Estimate based on sitemap or default

    # 2) Trimmed "large" pass (top + tail excerpts) if still not enough
    top_excerpt  = (seed_html or "")[:20000]
    tail_excerpt = (seed_html or "")[-20000:]
    site_sample  = pages_quick[:80]

    messages_large = [
        {"role": "system", "content": "You extract actionable PDF form links for the given site."},
        {"role": "user",   "content": (
            PROMPT_LARGE
            + "\n\nSeed URL:\n" + seed
            + "\n\nHTML excerpt (top):\n"  + top_excerpt
            + "\n\nHTML excerpt (tail):\n" + tail_excerpt
            + (("\n\nLikely form pages (sample):\n" + "\n".join(site_sample)) if site_sample else "")
            + (("\n\nDiscovered asset hints (from HTML):\n" + "\n".join(asset_hints)) if asset_hints else "")
        )}
    ]
    # Skip large LLM if we already have enough
    if len(pdfs) >= ENOUGH:
        print(f"[llm_discover] Already have {len(pdfs)} PDFs, skipping large LLM call")
        est_total = expected_total if 'expected_total' in locals() else len(pdfs)
        return {"pdf_urls": pdfs[:1000], "paths": paths[:200], "expected_total": est_total, "note": "small_sufficient"}

    try:
        print(f"[llm_discover] Calling {prov.upper()} for deeper analysis...")
        raw_large = _llm_call(prov, mdl, messages_large, max_tokens=2500, timeout=60.0, retries=0).strip()
        print("[llm_discover] ✓ Large LLM completed, parsing results...")
        print("[llm_discover] raw(LARGE):", raw_large[:400])

        # Strip markdown code fences if present
        cleaned2 = re.sub(r'^```(?:json)?\s*\n?', '', raw_large, flags=re.MULTILINE)
        cleaned2 = re.sub(r'\n?```\s*$', '', cleaned2, flags=re.MULTILINE)

        m2 = re.search(r"\{[\s\S]*\}", cleaned2)
        obj2 = json.loads(m2.group(0) if m2 else cleaned2)
        pdfs2 = [ _norm(u) for u in (obj2.get("pdf_urls") or []) if isinstance(u, str) ]
        paths2= [ p.strip() for p in (obj2.get("paths") or []) if isinstance(p, str) ]
        expected_total2 = obj2.get("expected_total")  # Large LLM might have better estimate
        pdfs2 = [u for u in pdfs2 if u.lower().startswith("https://") and u.lower().endswith(".pdf")]
        print(f"[llm_discover] Large LLM found {len(pdfs2)} additional PDFs, {len(paths2)} paths")
        pdfs  = list(dict.fromkeys(pdfs + pdfs2))
        paths_combined = list(dict.fromkeys((paths if 'paths' in locals() else []) + paths2))
        # Use large LLM's estimate if available, otherwise use small LLM's estimate
        final_expected = expected_total2 if expected_total2 else (expected_total if 'expected_total' in locals() else len(pdfs))
        print(f"[llm_discover] ✓ Total discovered: {len(pdfs)} PDFs, {len(paths_combined)} paths, expected total: ~{final_expected}")
        return {"pdf_urls": pdfs[:1000], "paths": paths_combined[:200], "expected_total": final_expected, "note": "llm_large"}
    except Exception as e:
        err_str = str(e)
        print(f"[llm_discover] large LLM error: {err_str}")

        # Don't log quota errors twice
        if "quota" not in err_str.lower():
            print(f"[llm_discover] → Continuing with HTML crawl mode")

        print(f"[llm_discover] Returning {len(pdfs)} PDFs, {len(pages_quick)} paths for HTML crawl")
        # Return what we have and a small set of candidate paths to keep the HTML crawl moving
        est_total = expected_total if 'expected_total' in locals() else max(len(pdfs), len(pdfs_quick))
        return {"pdf_urls": pdfs[:1000], "paths": pages_quick[:100], "expected_total": est_total, "note": "html_crawl_mode"}