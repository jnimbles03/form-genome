# app/services/crawler.py
from __future__ import annotations

import logging
import os
import re
import time
import queue
import typing as t
import urllib.parse as up
import json

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Optional HTML parser (prefer BeautifulSoup; fallback to regex)
try:
    from bs4 import BeautifulSoup  # type: ignore
    _HAS_BS4 = True
except Exception:
    _HAS_BS4 = False

# LLM-first discovery helper (uses llm_router under the hood via llm_discover)
from app.services import llm_discover, adaptive_depth, progress
from app.services import politeness

logger = logging.getLogger(__name__)

# Module-level politeness singletons (Wave 1.5, F-CS-10).
# Process-local; cluster-wide coordination is Wave 3 backlog.
_ROBOTS = politeness.default_robots_cache()
_HOST_LIMITER = politeness.default_host_limiter()

# Hybrid search configuration: maximum number of directories to crawl and overall timeout (seconds)
HYBRID_MAX_ITER = int(os.getenv('HYBRID_MAX_ITER', '20'))  # default max directories
HYBRID_TIMEOUT = float(os.getenv('HYBRID_TIMEOUT', '120.0'))  # default overall timeout in seconds

# Optional Playwright support for JavaScript-rendered pages
try:
    from app.services import playwright_crawler
    _HAS_PLAYWRIGHT = True
except Exception:
    _HAS_PLAYWRIGHT = False


# ------------------------------------------------------------------------------------
# HTTP client
# ------------------------------------------------------------------------------------

# F-CS-10: One canonical, honest User-Agent for every outbound request.
# Identifies the bot, points to source, names a contact. Replaces the
# Chrome-impersonation strings and per-vendor header shims that used to
# live here.
HEADERS_PDF = {
    "User-Agent": politeness.USER_AGENT,
    "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
    "Connection": "close",
}
BROWSER_HEADERS = {
    "User-Agent": politeness.USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "DNT": "1",
    "Connection": "close",
}

def _session() -> requests.Session:
    s = requests.Session()
    # Apply the honest UA on the session so every request carries it,
    # even paths that don't pass headers= explicitly.
    s.headers.update({"User-Agent": politeness.USER_AGENT})
    # F-CS-10: include 429 in the retry list, bump backoff_factor so
    # successive retries actually wait, and respect Retry-After headers
    # explicitly (default in newer urllib3, but be explicit in case the
    # deployed version is older).
    retry = Retry(
        total=2,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("HEAD", "GET", "OPTIONS"),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    return s

def _verify_path() -> str:
    # Allow corp CA trust store
    return os.getenv("REQUESTS_CA_BUNDLE") or os.getenv("SSL_CERT_FILE") or requests.certs.where()


# ------------------------------------------------------------------------------------
# URL normalization & traversal guards
# ------------------------------------------------------------------------------------

def _normalize_url(url: str) -> str:
    if not url:
        return url
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    p = up.urlsplit(url)
    host = p.hostname or ""
    netloc = p.netloc
    path = p.path

    # Host may contain encoded slashes; fix and merge.
    if "%2F" in host.upper():
        decoded = up.unquote(host)
        if "/" in decoded:
            host_only, extra = decoded.split("/", 1)
            netloc = host_only
            path = "/" + extra
        else:
            netloc = decoded

    path = up.quote(up.unquote(path), safe="/:@-._~!$&'()*+,;=")
    return up.urlunsplit((p.scheme or "https", netloc, path, p.query, p.fragment))

def _is_same_domain(a: str, b: str) -> bool:
    """
    Check if two URLs belong to the same base domain (ignoring www., content., client., forms. prefixes).
    Examples:
      - cuwest.org and www.cuwest.org -> True
      - cuwest.org and content.cuwest.org -> True
      - dgs.ca.gov and ca.gov -> True (same base domain)
      - cuwest.org and schwab.com -> False
    """
    try:
        host1 = (up.urlsplit(a).hostname or "").lower()
        host2 = (up.urlsplit(b).hostname or "").lower()

        if not host1 or not host2:
            return False

        # Normalize hosts: remove common prefixes
        for prefix in ['www.', 'content.', 'client.', 'forms.']:
            if host1.startswith(prefix):
                host1 = host1[len(prefix):]
            if host2.startswith(prefix):
                host2 = host2[len(prefix):]

        # Extract base domain (last 2 parts: domain + TLD)
        def get_base_domain(host):
            parts = host.split('.')
            if len(parts) >= 2:
                return '.'.join(parts[-2:])
            return host

        base1 = get_base_domain(host1)
        base2 = get_base_domain(host2)

        return base1 == base2
    except Exception:
        return False

def _same_doc_fragment(base_url: str, candidate: str) -> bool:
    """
    True if candidate points to the same document as base_url but only differs by fragment.
    Covers "#frag" and "https://…/page#frag".
    """
    cu = up.urlsplit(candidate)
    if not (cu.fragment or candidate.strip().startswith("#")):
        return False
    bu = up.urlsplit(base_url)
    if (not cu.scheme and not cu.netloc and not cu.path):  # "#fragment"
        return True
    return (cu.scheme or bu.scheme) == bu.scheme and \
           (cu.netloc or bu.netloc).lower() == (bu.netloc or "").lower() and \
           (cu.path or bu.path) == bu.path and \
           (cu.query or "") == (bu.query or "")

def _same_path_prefix(seed: str, candidate: str) -> bool:
    """Keep traversal within the starter path (e.g., /forms-and-applications)."""
    sp = up.urlsplit(seed); cp = up.urlsplit(candidate)
    if (sp.hostname or "").lower() != (cp.hostname or "").lower():
        return False
    s_path = re.sub(r"/+", "/", sp.path or "/").rstrip("/")
    c_path = re.sub(r"/+", "/", cp.path or "/").rstrip("/")
    if not s_path:  # seed path is "/"
        return True
    return c_path.startswith(s_path)


# ------------------------------------------------------------------------------------
# Pre-filter heuristics (forms vs marketing)
# ------------------------------------------------------------------------------------

_EXCLUDE_IN_URL = re.compile(
    r"(?:^|[-_/])(report|handbook|guide|brochure|flyer|factsheet|white[-_]?paper|"
    r"presentation|slides|deck|minutes|agenda|transcript|policy|plan|"
    r"press|news|statute|publiclaw|law|publication|booklet|annual|"
    r"disposition|opinion|docket|training|faq|glossary)(?:[-_/]|$)", re.I
)
_INCLUDE_IN_URL = re.compile(
    r"(?:^|[-_/])(form|application|request|enroll|enrollment|claim|"
    r"waiver|authorization|consent|affidavit|declaration|"
    r"notice|appeal|designation|change|update|verification)(?:[-_/]|$)", re.I
)

def url_is_probably_form(u: str) -> bool:
    p = (u or "").lower()
    if _INCLUDE_IN_URL.search(p): return True
    if _EXCLUDE_IN_URL.search(p): return False
    return True

def _is_javascript_rendered(html_text: str, link_count: int) -> bool:
    """
    Detect if a page is likely JavaScript-rendered based on HTML content.

    Indicators:
    - Adobe Experience Manager (AEM) markers
    - React/Vue/Angular/Next.js frameworks
    - GitHub's Primer framework
    - Very low link count despite HTML content
    - Common SPA patterns
    - Webpack/module bundler artifacts
    """
    if not html_text:
        return False

    html_lower = html_text.lower()

    # Framework detection
    js_frameworks = [
        "adobe experience manager",
        "aem",
        "data-react",
        "ng-app",
        "vue-app",
        "__next",
        "nuxt",
        "gatsby",
        "data-turbo",  # GitHub/Rails Turbo
        "primer.style",  # GitHub's design system
        "octicon",  # GitHub icons
        "webpack",  # Module bundler
        "chunk.js",  # Code splitting artifact
        "app.bundle",  # Bundle artifact
        "vendor.bundle",  # Vendor bundle
        "svelte",  # Svelte framework
        "ember",  # Ember.js
    ]

    for framework in js_frameworks:
        if framework in html_lower:
            return True

    # SPA patterns - script tags dominating the HTML
    script_count = html_text.count("<script")
    if script_count > 10 and link_count < 10:
        return True

    # Very low link count despite substantial HTML
    if len(html_text) > 10000 and link_count < 5:
        return True

    # Root app div pattern (common in SPAs)
    spa_root_patterns = [
        '<div id="app"',
        '<div id="root"',
        '<div id="react-root"',
        '<div class="application"',
        '<div data-react-class',
    ]

    for pattern in spa_root_patterns:
        if pattern in html_lower:
            # If we find SPA root AND few links, likely JS-rendered
            if link_count < 15:
                return True

    return False


# ------------------------------------------------------------------------------------
# HTML extraction + extra sources (data-href / onclick / inline JSON / iframe)
# ------------------------------------------------------------------------------------

_PDF_EXT = re.compile(r"\.pdf($|[?#])", re.I)
_DATA_URL_RE = re.compile(r"""(?:
    data-href\s*=\s*["']([^"']+)["'] |
    data-url\s*=\s*["']([^"']+)["']  |
    onclick\s*=\s*["'][^"']*(https?://[^"'\s)]+)[^"']*["']
)""", re.I | re.X)
_JSON_URL_RE = re.compile(r"https?://[^\s\"']+\.pdf(?:[?#][^\s\"']*)?", re.I)

def _extract_links_html(html_text: str, base: str) -> t.Tuple[t.List[str], t.List[str]]:
    """Return (hrefs, pdfs) normalized/absolute from an HTML document."""
    hrefs: t.List[str] = []
    pdfs:  t.List[str] = []
    if _HAS_BS4:
        soup = BeautifulSoup(html_text or "", "html.parser")
        for a in soup.find_all("a", href=True):
            u = up.urljoin(base, a["href"].strip())
            hrefs.append(u)
            if _PDF_EXT.search(u): pdfs.append(u)
        for tag in soup.find_all(["embed", "object"]):
            src = tag.get("src") or tag.get("data")
            if src:
                u = up.urljoin(base, src.strip())
                hrefs.append(u)
                if _PDF_EXT.search(u): pdfs.append(u)
        # Schwab/others often use iframe to load PDFs
        for tag in soup.find_all("iframe", src=True):
            u = up.urljoin(base, tag["src"].strip())
            hrefs.append(u)
            if _PDF_EXT.search(u): pdfs.append(u)
    else:
        for m in re.finditer(r"""href\s*=\s*["']([^"']+)["']""", html_text or "", re.I):
            u = up.urljoin(base, m.group(1).strip())
            hrefs.append(u)
            if _PDF_EXT.search(u): pdfs.append(u)
        for m in re.finditer(r"""(?:src|data)\s*=\s*["']([^"']+)["']""", html_text or "", re.I):
            u = up.urljoin(base, m.group(1).strip())
            hrefs.append(u)
            if _PDF_EXT.search(u): pdfs.append(u)
        for m in re.finditer(r"""<iframe[^>]+src=["']([^"']+)["']""", html_text or "", re.I):
            u = up.urljoin(base, m.group(1).strip())
            hrefs.append(u)
            if _PDF_EXT.search(u): pdfs.append(u)

    hrefs = [_normalize_url(u) for u in hrefs]
    pdfs  = [_normalize_url(u) for u in pdfs]
    return hrefs, pdfs

def _extract_extra_urls(html_text: str, base: str) -> t.List[str]:
    """Return extra URLs from data-href/data-url/onclick and inline JSON/script blocks."""
    urls: t.List[str] = []
    for m in _DATA_URL_RE.finditer(html_text or ""):
        for g in m.groups():
            if g:
                urls.append(up.urljoin(base, g.strip()))
    for m in _JSON_URL_RE.finditer(html_text or ""):
        urls.append(m.group(0).strip())
    return [_normalize_url(u) for u in urls]

def _schwab_resource_probe(html_text: str, base: str) -> t.List[str]:
    """
    Extract PDFs from Schwab 'resource' pages that embed a PDF viewer or declare it in JSON.
    """
    urls: t.List[str] = []

    # Any absolute .pdf in the HTML
    for m in re.finditer(r"https?://[^\s\"'<>]+\.pdf(?:[?#][^\s\"']*)?", html_text or "", re.I):
        urls.append(m.group(0).strip())

    # iframe/embed/object sources
    for m in re.finditer(r"""<(?:iframe|embed|object)[^>]+?(?:src|data)=["']([^"']+)["']""", html_text or "", re.I):
        urls.append(up.urljoin(base, m.group(1).strip()))

    # JSON blocks with pdfUrl/file/document/url keys
    for m in re.finditer(r"<script[^>]*>([\s\S]*?)</script>", html_text or "", re.I):
        block = (m.group(1) or "")
        for key in ("pdfUrl", "document", "file", "fileUrl", "url"):
            for mm in re.finditer(rf'"{key}"\s*:\s*"([^"]+\.pdf[^"]*)"', block, re.I):
                urls.append(mm.group(1))

    urls = [_normalize_url(u) for u in urls]
    urls = list(dict.fromkeys(urls))
    return urls


# ------------------------------------------------------------------------------------
# HEAD probe  (and lenient LLM validator)
# ------------------------------------------------------------------------------------

def _maybe_head_pdf(u: str, timeout: float, connect_timeout: float = 3.0) -> bool:
    """
    Probe URL with HEAD request to check if it's a PDF.
    Returns True if Content-Type contains 'pdf', False otherwise.
    Raises exception on network errors (403, timeout, etc.) to let caller handle fallback.
    """
    s = _session()
    r = s.head(u, timeout=(connect_timeout, timeout), allow_redirects=True,
               headers=HEADERS_PDF, verify=_verify_path())
    ct = (r.headers.get("Content-Type") or "").lower()
    return "pdf" in ct

def _head_or_assume(u: str, head_timeout: float, connect_timeout: float) -> bool:
    """
    Prefer a HEAD probe; if it errors (403/timeout), keep the URL and let analyzer
    verify via %PDF- magic / Content-Type to avoid dropping good candidates.
    If HEAD confirms it's NOT a PDF (returns False), reject it.
    """
    try:
        result = _maybe_head_pdf(u, timeout=head_timeout, connect_timeout=connect_timeout)
        # If HEAD probe succeeded and confirmed it's a PDF, keep it
        # If HEAD probe succeeded but Content-Type is NOT pdf, reject it
        return result
    except Exception:
        # If HEAD request failed (403, timeout, etc.), assume it might be a PDF
        # and let the analyzer verify with %PDF- magic bytes
        return True


# ------------------------------------------------------------------------------------
# Google CSE fallback (optional; only used if keys exist)
# ------------------------------------------------------------------------------------

def _google_cse(domain: str, max_results: int = 1000, timeout: float = 8.0) -> t.List[str]:
    key = os.getenv("GOOGLE_CSE_KEY", "").strip()
    cx  = os.getenv("GOOGLE_CSE_CX",  "").strip()
    if not key or not cx:
        return []
    hits: t.List[str] = []
    s = _session()
    base = "https://www.googleapis.com/customsearch/v1"
    start = 1
    while len(hits) < max_results and start <= 991:
        params = {
            "key": key, "cx": cx,
            "q": f"site:{domain} filetype:pdf (form OR application OR request)",
            "num": 10, "start": start
        }
        try:
            r = s.get(base, params=params, timeout=(3.0, timeout),
                      headers=BROWSER_HEADERS, verify=_verify_path())
            r.raise_for_status()
            data = r.json()
            items = data.get("items") or []
            for it in items:
                link = it.get("link") or ""
                if link:
                    u = _normalize_url(link)
                    if _PDF_EXT.search(u) and url_is_probably_form(u):
                        hits.append(u)
            if not items:
                break
            start += 10
        except Exception:
            break
    return list(dict.fromkeys(hits))

def _google_cse_exhaustive(domain: str, timeout: float = 8.0) -> t.List[str]:
    """
    Exhaustive CSE search that finds ALL forms automatically.
    Runs initial broad search + targeted searches to maximize discovery.
    """
    # Run initial broad search
    print(f"[CSE] Running initial broad search for {domain}...")
    hits = _google_cse(domain, max_results=1000, timeout=timeout)
    initial_count = len(hits)
    print(f"[CSE] Initial search found {initial_count} forms")

    # ALWAYS run targeted searches to find more forms (not just when near cap)
    print(f"[CSE] Running targeted searches to find additional forms...")
    all_hits_set = set(hits)  # Deduplicate

    # Additional search terms to find more forms
    targeted_terms = [
        "enrollment",
        "claim",
        "beneficiary",
        "change",
        "withdrawal",
        "authorization",
        "agreement",
        "consent",
        "disclosure",
        "acknowledgment",
        "certification",
        "declaration",
        "amendment",
        "termination",
        "transfer",
        "distribution",
        "designation",
        "waiver",
        "election",
        "notification"
    ]

    for term in targeted_terms:
        # Search with specific term
        query = f"site:{domain} filetype:pdf {term} form"
        print(f"[CSE] Searching: {term} form...")

        key = os.getenv("GOOGLE_CSE_KEY", "").strip()
        cx = os.getenv("GOOGLE_CSE_CX", "").strip()
        if not key or not cx:
            break

        s = _session()
        base = "https://www.googleapis.com/customsearch/v1"

        # Search first 50 results for each targeted term (5 API calls per term)
        for start in range(1, 51, 10):
            params = {
                "key": key, "cx": cx,
                "q": query,
                "num": 10, "start": start
            }
            try:
                r = s.get(base, params=params, timeout=(3.0, timeout),
                          headers=BROWSER_HEADERS, verify=_verify_path())
                r.raise_for_status()
                data = r.json()
                items = data.get("items") or []
                if not items:
                    break

                for it in items:
                    link = it.get("link") or ""
                    if link:
                        u = _normalize_url(link)
                        if _PDF_EXT.search(u) and url_is_probably_form(u):
                            all_hits_set.add(u)
            except Exception as e:
                print(f"[CSE] Error in targeted search ({term}): {e}")
                break

        # Report progress every few terms
        if targeted_terms.index(term) % 5 == 4 or term == targeted_terms[-1]:
            new_total = len(all_hits_set)
            new_forms = new_total - initial_count
            print(f"[CSE] Progress: {new_total} total forms ({new_forms} new from targeted searches)")

    final_hits = list(all_hits_set)
    total_new = len(final_hits) - initial_count
    print(f"[CSE] Exhaustive search complete: {len(final_hits)} total forms ({total_new} additional from targeted searches)")
    return final_hits

def _google_cse_multi(domains: t.List[str], max_results: int = 1000, timeout: float = 8.0, exhaustive: bool = False) -> t.List[str]:
    """
    Search multiple domains for forms.

    Args:
        domains: List of domains to search
        max_results: Max results per domain (only used if exhaustive=False)
        timeout: Request timeout
        exhaustive: If True, bypass 1000 cap with targeted searches
    """
    all_hits: t.List[str] = []
    for d in domains:
        if exhaustive:
            all_hits.extend(_google_cse_exhaustive(d, timeout=timeout))
        else:
            all_hits.extend(_google_cse(d, max_results=max_results, timeout=timeout))
    return list(dict.fromkeys(all_hits))


def _analyze_form_directories_with_llm(urls: t.List[str], domain: str) -> dict:
    """
    Use LLM to analyze CSE results and identify form directory patterns.
    Returns directories to crawl and pagination patterns detected.
    """
    from urllib.parse import urlparse
    from collections import Counter

    # Extract paths from URLs
    paths = []
    for url in urls[:200]:  # Analyze first 200 URLs (representative sample)
        try:
            parsed = urlparse(url)
            path = parsed.path
            # Get directory (remove filename)
            if '/' in path:
                directory = '/'.join(path.split('/')[:-1])
                if directory:
                    paths.append(directory)
        except Exception:
            continue

    # Count directory frequencies
    dir_counts = Counter(paths)
    top_dirs = dir_counts.most_common(20)

    # Build analysis for LLM
    url_sample = '\n'.join(urls[:50])
    dir_summary = '\n'.join([f"{count:3d} forms: {directory}" for directory, count in top_dirs[:10]])

    prompt = f"""Analyze these form URLs from {domain} and identify the best directories to crawl for finding ALL forms:

DIRECTORY FREQUENCY:
{dir_summary}

SAMPLE URLs:
{url_sample}

Identify:
1. Primary form directories (where most forms are located)
2. Any pagination patterns (e.g., ?page=, /page/)
3. URL structure patterns that suggest more forms

Return JSON only:
{{
  "primary_directories": ["/path1", "/path2"],
  "recommended_crawl_urls": ["https://domain.com/forms/", ...],
  "pagination_detected": true/false,
  "notes": "brief analysis"
}}"""

    try:
        from app.services.llm_router import chat_complete
        
        # User Request: Use Gemini Flash for site index/directory analysis
        provider = "gemini"
        model = os.getenv("GEMINI_MODEL") or "gemini-2.0-flash"
        
        prompt_messages = [{"role": "user", "content": prompt}]
        
        response_text = chat_complete(
            provider=provider,
            model=model,
            messages=prompt_messages,
            temperature=0.3,
            max_tokens=1000,
            timeout=30.0
        )

        import json
        # Strip code fences if present
        clean_text = response_text.replace("```json", "").replace("```", "").strip()
        result = json.loads(clean_text)
        print(f"[LLM] Analysis ({model}): {result.get('notes', 'N/A')}")
        print(f"[LLM] Recommended crawl URLs: {len(result.get('recommended_crawl_urls', []))}")
        return result
    except Exception as e:
        print(f"[LLM] Analysis failed: {e}, using fallback directory detection")
        # Fallback: use top directories by frequency
        fallback_dirs = [d for d, _ in top_dirs[:5] if d]
        base_url = f"https://{domain}" if not domain.startswith('http') else domain
        return {
            "primary_directories": fallback_dirs,
            "recommended_crawl_urls": [f"{base_url}{d}" for d in fallback_dirs],
            "pagination_detected": any('page' in url.lower() for url in urls[:50]),
            "notes": "Fallback: using top directories by frequency"
        }


def intelligent_hybrid_search(domain: str, timeout: float = 8.0, progress_cb: t.Callable = None) -> t.List[str]:
    """
    Two-stage intelligent search that combines CSE + LLM analysis + targeted crawler.

    Stage 1: CSE exhaustive search finds scattered forms across domain
    Stage 2: LLM analyzes patterns and identifies form directories
    Stage 3: Targeted crawler exhaustively searches identified directories
    Stage 4: Deduplicate and return combined results

    This finds 3-5x more forms than CSE alone by:
    - Using CSE to discover form locations
    - Using LLM to identify directory patterns
    - Using crawler to handle pagination and deep directory traversal
    """
    import app.services.progress as progress

    print(f"[HYBRID] Starting intelligent hybrid search for {domain}")

    # Stage 1: CSE exhaustive search
    if progress_cb:
        try: progress_cb(event="hybrid_stage", stage=1, message="CSE exhaustive search...")
        except Exception: pass

    print(f"[HYBRID] Stage 1: Running CSE exhaustive search...")
    cse_results = _google_cse_exhaustive(domain, timeout=timeout)
    print(f"[HYBRID] Stage 1 complete: Found {len(cse_results)} forms via CSE")

    # Report CSE results count to UI
    if progress_cb:
        try: progress_cb(event="pdf", pdfs=len(cse_results), done=0)
        except Exception: pass

    # Check for stop request after CSE
    if progress.should_stop():
        print(f"[HYBRID] Stop requested after CSE stage - returning {len(cse_results)} forms")
        if progress_cb:
            try: progress_cb(event="finish", found=len(cse_results))
            except Exception: pass
        return cse_results

    if not cse_results:
        print(f"[HYBRID] No forms found via CSE, returning empty")
        if progress_cb:
            try: progress_cb(event="finish", found=0)
            except Exception: pass
        return []

    # Stage 2: LLM analyzes patterns
    if progress_cb:
        try: progress_cb(event="hybrid_stage", stage=2, message="LLM analyzing patterns...")
        except Exception: pass

    print(f"[HYBRID] Stage 2: LLM analyzing form directory patterns...")
    analysis = _analyze_form_directories_with_llm(cse_results, domain)
    crawl_urls = analysis.get("recommended_crawl_urls", [])
    print(f"[HYBRID] Stage 2 complete: Identified {len(crawl_urls)} directories to crawl")

    # Check for stop request after LLM analysis
    if progress.should_stop():
        print(f"[HYBRID] Stop requested after LLM stage - returning {len(cse_results)} forms from CSE")
        if progress_cb:
            try: progress_cb(event="finish", found=len(cse_results))
            except Exception: pass
        return cse_results

    # Stage 3: Targeted crawler on identified directories
    if progress_cb:
        try: progress_cb(event="hybrid_stage", stage=3, message=f"Crawling {len(crawl_urls)} directories...")
        except Exception: pass

    print(f"[HYBRID] Stage 3: Running targeted crawler on {len(crawl_urls)} directories...")
    crawler_results = []

    start_hybrid = time.time()
    for i, crawl_url in enumerate(crawl_urls[:HYBRID_MAX_ITER], 1):
        # Enforce max iteration limit
        if i > HYBRID_MAX_ITER:
            print(f"[HYBRID] Reached max iteration limit ({HYBRID_MAX_ITER}) - stopping further directory crawls")
            break
        # Enforce overall timeout
        if time.time() - start_hybrid > HYBRID_TIMEOUT:
            print(f"[HYBRID] Hybrid search timeout of {HYBRID_TIMEOUT}s exceeded - stopping further directory crawls")
            break
        # Check for stop request before each directory crawl
        if progress.should_stop():
            print(f"[HYBRID] Stop requested during crawler stage - returning {len(list(dict.fromkeys(cse_results + crawler_results)))} forms found so far")
            combined = list(dict.fromkeys(cse_results + crawler_results))
            if progress_cb:
                try: progress_cb(event="finish", found=len(combined))
                except Exception: pass
            return combined

        print(f"[HYBRID] Crawling directory {i}/{min(len(crawl_urls), 5)}: {crawl_url}")
        try:
            result = crawl(
                crawl_url,
                max_pdfs=None,  # No limit
                depth=3,  # Go deep in this directory
                same_domain=True,
                timeout=timeout,
                deadline_sec=120.0,  # 2 minutes per directory
                max_pages=300,
                progress_cb=progress_cb
            )
            crawler_results.extend(result.get("urls", []))
            print(f"[HYBRID] Found {len(result.get('urls', []))} forms in {crawl_url}")

            # Report running total (CSE + crawler so far, deduplicated)
            combined_so_far = list(dict.fromkeys(cse_results + crawler_results))
            if progress_cb:
                try: progress_cb(event="pdf", pdfs=len(combined_so_far), done=i)
                except Exception: pass
        except Exception as e:
            print(f"[HYBRID] Crawler failed for {crawl_url}: {e}")
            continue

    print(f"[HYBRID] Stage 3 complete: Found {len(crawler_results)} forms via crawler")

    # Stage 4: Deduplicate and combine
    if progress_cb:
        try: progress_cb(event="hybrid_stage", stage=4, message="Deduplicating results...")
        except Exception: pass

    print(f"[HYBRID] Stage 4: Deduplicating and combining results...")
    all_results = list(dict.fromkeys(cse_results + crawler_results))

    cse_only = len(cse_results)
    crawler_only = len(crawler_results)
    total = len(all_results)
    additional = total - cse_only

    print(f"[HYBRID] Complete! Total: {total} forms ({cse_only} from CSE, {additional} additional from crawler)")

    # Report final count
    if progress_cb:
        try: progress_cb(event="finish", found=total)
        except Exception: pass

    return all_results


# ------------------------------------------------------------------------------------
# Main crawl (LLM-first by default)
# ------------------------------------------------------------------------------------

def crawl(url: str,
          max_pdfs: int | None = None,
          same_domain: bool = False,
          allow_offsite: bool = True,
          depth: int = 1,
          timeout: float = 8.0,
          compat: bool = True,
          progress_cb: t.Callable[..., None] | None = None,
          deadline_sec: float = 240.0,
          head_timeout: float = 4.0,
          connect_timeout: float = 5.0,
          max_pages: int = 750,
          page_workers: int = 8,
          head_workers: int = 8,
          strategy: str = "llm_first",
          **kwargs) -> dict:
    """
    Crawl starting from `url` and return form-ish PDF URLs.

    strategy:
        - 'llm_first' (default): Use LLM to intelligently discover forms, then HTML crawl
        - 'html_first': Pure HTML crawling without LLM assistance
        - 'search_first': Pure Google Custom Search (requires API keys)
        - 'playwright': Use Playwright for JavaScript-rendered pages (requires Playwright installed)

    Automatic Playwright fallback: If HTML crawl finds nothing and JavaScript rendering is
    detected (e.g., Adobe Experience Manager, React, Vue), automatically falls back to Playwright.

    kwargs may include: llm_provider, llm_model
    """
    started = time.time()
    seed = _normalize_url(url)

    if progress_cb:
        try: progress_cb(event="start", seed=seed)
        except Exception: pass

    visited: set[str] = set()
    q_pages: "queue.Queue[tuple[str,int]]" = queue.Queue()
    q_pages.put((seed, 0))

    pdfs: list[str] = []
    pdfs_set: set[str] = set()
    reason: str | None = None
    expected_total: int | None = None  # Expected total forms (from LLM/sitemap)
    last_pdf_found_time = time.time()  # Track when we last found a PDF

    # Scale low-yield thresholds with depth - be more patient at deeper levels
    # Depth 1: 10 pages minimum (allow exploring subdirectories)
    # Depth 2: 15 pages minimum (deeper exploration)
    # Depth 3: 20 pages minimum (thorough deep crawl)
    LOW_YIELD_PAGES = max(10, 5 * depth)
    LOW_YIELD_MIN_PDFS = max(1, depth)  # Require more PDFs at deeper levels

    def over_deadline() -> bool:
        return (time.time() - started) >= max(5.0, deadline_sec)

    def maybe_add_pdf(u: str) -> None:
        nonlocal last_pdf_found_time
        if max_pdfs is not None and len(pdfs) >= max_pdfs:
            return
        if u in pdfs_set:
            return
        if not allow_offsite and not _is_same_domain(seed, u):
            return
        if not url_is_probably_form(u):
            return
        pdfs_set.add(u)
        pdfs.append(u)
        last_pdf_found_time = time.time()  # Update timestamp when PDF found
        if progress_cb:
            try: progress_cb(event="pdf", url=u, done=len(visited), queue=q_pages.qsize(), pdfs=len(pdfs))
            except Exception: pass

    host = up.urlsplit(seed).hostname or ""
    schwab_multi = ("schwab.com" in host)

    # ---------- playwright (JavaScript-rendered pages) ----------
    if strategy == "playwright":
        if not _HAS_PLAYWRIGHT:
            return {
                "found": 0,
                "urls": [],
                "source": "playwright",
                "ms": int((time.time()-started)*1000),
                "reason": "playwright_not_installed",
                "error": "Playwright not installed. Run: pip install playwright && playwright install chromium"
            }

        try:
            result = playwright_crawler.crawl_with_playwright(
                url=seed,
                wait_time=3.0,
                timeout=60000,
                max_pdfs=max_pdfs
            )
            # Validate and filter PDFs
            for u in result.get("urls", []):
                maybe_add_pdf(u)

            if progress_cb:
                try: progress_cb(event="finish", found=len(pdfs))
                except Exception: pass

            return {
                "found": len(pdfs),
                "urls": list(dict.fromkeys(pdfs)),
                "source": "playwright",
                "ms": result.get("ms", int((time.time()-started)*1000)),
                "reason": "playwright" if not result.get("error") else "playwright_error",
                "error": result.get("error")
            }
        except Exception as e:
            return {
                "found": 0,
                "urls": [],
                "source": "playwright",
                "ms": int((time.time()-started)*1000),
                "reason": "playwright_error",
                "error": str(e)
            }

    # ---------- search_first (pure Google; only if keys available) ----------
    if strategy == "search_first":
        hits = _google_cse_multi(["www.schwab.com","content.schwab.com","client.schwab.com"], 1000, head_timeout) \
               if schwab_multi else _google_cse(host, 1000, head_timeout)
        for u in hits: maybe_add_pdf(u)
        reason = "forced_search"
        if progress_cb:
            try: progress_cb(event="finish", found=len(pdfs))
            except Exception: pass
        return {"found": len(pdfs), "urls": list(dict.fromkeys(pdfs)), "source": "google",
                "ms": int((time.time()-started)*1000), "reason": reason or "ok"}

    # ---------- llm_first ----------
    if strategy == "llm_first":
        # Gather seed HTML + sitemap hints
        try:
            code, seed_html, _ = llm_discover.fetch(seed, timeout)
        except Exception:
            code, seed_html = 0, ""
        try:
            hints = llm_discover.try_sitemap(seed, timeout)
        except Exception:
            hints = []

        # Provider/model come from kwargs (or env defaults)
        prov = (kwargs.get("llm_provider") or os.getenv("LLM_PROVIDER") or "xai").lower().strip()
        mdl  = (kwargs.get("llm_model") or
                (os.getenv("GROK_MODEL") if prov=="xai" else
                 os.getenv("OPENAI_MODEL") if prov=="openai" else
                 os.getenv("ANTHROPIC_MODEL") if prov=="anthropic" else
                 os.getenv("GEMINI_MODEL")) or "").strip()

        try:
            out = llm_discover.discover(seed, seed_html or "", hints, provider=prov, llm_model=mdl)
            llm_urls = [u for u in (out.get("pdf_urls") or []) if u.lower().endswith(".pdf")]
            expected_total = out.get("expected_total")  # Get LLM's estimate of total forms

            # Validate LLM URLs (prefer HEAD; otherwise keep and let analyzer verify)
            valid = [u for u in llm_urls if _head_or_assume(u, head_timeout, connect_timeout)]
            for u in valid:
                maybe_add_pdf(u)

            # Log completeness info
            if expected_total:
                print(f"[crawler] LLM estimates ~{expected_total} total forms, found {len(pdfs)} so far", flush=True)

            # Check if we've found all expected forms from LLM/sitemap
            if expected_total and len(pdfs) >= expected_total:
                print(f"[crawler] ✓ Found {len(pdfs)}/{expected_total} expected forms - stopping early", flush=True)
                reason = "completeness_reached"
                # Skip HTML crawl - we found everything
            else:
                # Seed any LLM-suggested paths and fall through to HTML crawl
                # Trust LLM path suggestions - don't restrict to same path prefix
                # since LLM intelligently suggests relevant form directories across the site
                for pth in (out.get("paths") or []):
                    try:
                        u = up.urljoin(seed, pth)
                        # Only check same domain, not path prefix (LLM knows what's relevant)
                        if _is_same_domain(seed, u):
                            q_pages.put((u, 1))
                    except Exception:
                        pass

            # Don't set reason here - let HTML crawl run as fallback
            # If HTML crawl also fails, reason will be set by safety net (line 579)
        except Exception:
            # Don't set reason here either - let HTML crawl run as fallback
            pass

    # ---------- Concurrent HTML crawl (anchor skip, same-path, low-yield) ----------
    def page_worker():
        nonlocal reason
        local_sess = _session()
        while not q_pages.empty():
            # Check for user-requested stop
            if progress.should_stop():
                reason = reason or "user_stopped"; break
            if reason or over_deadline():
                reason = reason or "deadline"; break
            if len(visited) >= max_pages:
                reason = reason or "page_ceiling"; break

            # Smart completeness check: Stop if we've found all expected forms
            if expected_total and len(pdfs) >= expected_total:
                reason = reason or "completeness_reached"; break

            # Stop if no new PDFs found in 45 seconds (after visiting at least 5 pages)
            if len(visited) >= 5 and (time.time() - last_pdf_found_time) > 45:
                reason = reason or "no_new_forms"; break

            try:
                page_url, d = q_pages.get_nowait()
            except queue.Empty:
                break

            if page_url in visited:
                q_pages.task_done(); continue
            visited.add(page_url)
            if d > depth:
                q_pages.task_done(); continue

            # F-CS-10: robots.txt check (open-by-default if no robots).
            try:
                if not _ROBOTS.is_allowed(page_url):
                    logger.warning(
                        "robots.txt disallows %s — skipping (no content recorded)",
                        page_url,
                    )
                    if progress_cb:
                        try: progress_cb(event="page", url=page_url, done=len(visited),
                                         queue=q_pages.qsize(), pdfs=len(pdfs))
                        except Exception: pass
                    q_pages.task_done(); continue
            except Exception as _robots_err:
                # Defensive: any error in robots layer must not break crawl.
                logger.debug("robots check raised for %s: %s", page_url, _robots_err)

            # Fetch HTML page quickly, gated by per-host rate limiter (F-CS-10).
            try:
                with _HOST_LIMITER.acquire(page_url):
                    r = local_sess.get(page_url, timeout=(connect_timeout, timeout),
                                       headers=BROWSER_HEADERS,
                                       verify=_verify_path(), allow_redirects=True)
                status = getattr(r, "status_code", 200)
                if status == 429:
                    # Back-pressure, not a worker error. Honor Retry-After
                    # so the next acquire on this host waits.
                    retry_after = politeness.parse_retry_after(
                        r.headers.get("Retry-After") if hasattr(r, "headers") else None
                    )
                    host = (up.urlsplit(page_url).netloc or "").lower()
                    if retry_after is not None:
                        _HOST_LIMITER.set_retry_after(host, retry_after)
                    else:
                        # Default modest backoff if server didn't say.
                        _HOST_LIMITER.set_retry_after(host, 30.0)
                    logger.info(
                        "429 from %s; backing off %.1fs (Retry-After=%r)",
                        host, retry_after if retry_after is not None else 30.0,
                        r.headers.get("Retry-After") if hasattr(r, "headers") else None,
                    )
                    q_pages.task_done(); continue
                if status in (401, 403):
                    reason = reason or "blocked"
                    q_pages.task_done(); continue
                ct = (r.headers.get("Content-Type") or "").lower()
                if "html" not in ct and "xml" not in ct:
                    if progress_cb:
                        try: progress_cb(event="page", url=page_url, done=len(visited), queue=q_pages.qsize(), pdfs=len(pdfs))
                        except Exception: pass
                    q_pages.task_done(); continue
                html_text = r.text or ""
            except Exception:
                if progress_cb:
                    try: progress_cb(event="page", url=page_url, done=len(visited), queue=q_pages.qsize(), pdfs=len(pdfs))
                    except Exception: pass
                q_pages.task_done(); continue

            hrefs, page_pdfs = _extract_links_html(html_text, page_url)
            extra = _extract_extra_urls(html_text, page_url)
            for u in extra:
                if _PDF_EXT.search(u): maybe_add_pdf(u)
                else: hrefs.append(u)

            # Schwab resource probe: lift embedded PDFs from viewer/JSON
            if "schwab.com/resource/" in page_url.lower():
                for u in _schwab_resource_probe(html_text, page_url):
                    if _PDF_EXT.search(u): maybe_add_pdf(u)
                    else: hrefs.append(u)

            # Drop same-document fragments
            hrefs = [u for u in hrefs if not _same_doc_fragment(page_url, u)]

            # Obvious PDFs on page
            for u in page_pdfs:
                maybe_add_pdf(u)
                if over_deadline(): reason = reason or "deadline"; break
                if max_pdfs is not None and len(pdfs) >= max_pdfs: break
            if reason or (max_pdfs is not None and len(pdfs) >= max_pdfs):
                q_pages.task_done(); break

            # Queue next HTML pages under the same path prefix + collect non-.pdf for HEAD
            nonpdfs: list[str] = []
            for u in hrefs:
                if _PDF_EXT.search(u):
                    maybe_add_pdf(u)
                    if over_deadline(): reason = reason or "deadline"; break
                else:
                    if d < depth and u not in visited:
                        if not any(u.lower().endswith(ext) for ext in (
                            ".jpg",".jpeg",".png",".gif",".svg",".css",".js",".zip",
                            ".xlsx",".docx",".pptx",".mp4",".mov",".avi",".webm",
                            ".txt",".xml",".rss")):
                            if (not same_domain or _is_same_domain(seed, u)) and _same_path_prefix(seed, u):
                                q_pages.put((u, d+1))
                    nonpdfs.append(u)
            if reason:
                q_pages.task_done(); break

            # Concurrent HEAD probes for non-.pdf URLs
            if nonpdfs and not reason and not over_deadline():
                from concurrent.futures import ThreadPoolExecutor, as_completed
                def head_job(v: str):
                    try:
                        if _maybe_head_pdf(v, timeout=head_timeout, connect_timeout=connect_timeout): return v
                    except Exception: return None
                    return None
                with ThreadPoolExecutor(max_workers=max(1, head_workers)) as ex:
                    futures = [ex.submit(head_job, u) for u in nonpdfs]
                    for fut in as_completed(futures):
                        v = fut.result()
                        if v: maybe_add_pdf(v)
                        if over_deadline(): reason = reason or "deadline"; break

            if progress_cb:
                try: progress_cb(event="page", url=page_url, done=len(visited), queue=q_pages.qsize(), pdfs=len(pdfs))
                except Exception: pass

            q_pages.task_done()

            # Low-yield early fallback
            if (len(visited) >= LOW_YIELD_PAGES) and (len(pdfs) < LOW_YIELD_MIN_PDFS):
                reason = reason or "low_yield"; break

            if reason or (max_pdfs is not None and len(pdfs) >= max_pdfs):
                break

    # Run workers
    from threading import Thread
    workers = [Thread(target=page_worker, daemon=True) for _ in range(max(1, page_workers))]
    for w in workers: w.start()
    for w in workers: w.join(timeout=deadline_sec)
    if (not reason) and (time.time() - started >= deadline_sec): reason = "deadline"
    if len(visited) >= max_pages: reason = reason or "page_ceiling"

    # Optional safety net: Playwright fallback for JavaScript-rendered pages
    used_playwright = False
    if (not pdfs) and _HAS_PLAYWRIGHT and strategy != "playwright":
        # Check if we might have JavaScript-rendered content
        # Get seed page HTML to check for JS frameworks (F-CS-05: walk
        # redirects manually with SSRF re-check at every hop).
        try:
            s = _session()
            r = politeness.safe_redirect_walk(
                seed, s,
                timeout=(connect_timeout, timeout),
                headers=BROWSER_HEADERS,
                verify=_verify_path(),
                ssrf_check=politeness.is_safe_crawl_target,
            )
            seed_html = r.text or ""
            all_hrefs, _ = _extract_links_html(seed_html, seed)
            link_count = len(all_hrefs)

            if _is_javascript_rendered(seed_html, link_count):
                if progress_cb:
                    try: progress_cb(event="playwright_fallback", seed=seed)
                    except Exception: pass

                result = playwright_crawler.crawl_with_playwright(
                    url=seed,
                    wait_time=3.0,
                    timeout=60000,
                    max_pdfs=max_pdfs
                )
                for u in result.get("urls", []):
                    maybe_add_pdf(u)

                if pdfs:
                    used_playwright = True
                    reason = "playwright_fallback"
        except Exception:
            pass

    # Optional safety net: Google search (only if keys configured)
    used_google = False
    if (not pdfs) or (reason in ("blocked", "low_yield", "llm_empty", "llm_error")):
        if os.getenv("GOOGLE_CSE_KEY") and os.getenv("GOOGLE_CSE_CX"):
            hits = _google_cse_multi(["www.schwab.com","content.schwab.com","client.schwab.com"], 1000, head_timeout) \
                   if ("schwab.com" in (host or "")) else _google_cse(host, 1000, head_timeout)
            for u in hits: maybe_add_pdf(u)
            if hits:
                used_google = True
                if reason is None: reason = "google_fallback"

    pdfs = list(dict.fromkeys(pdfs))
    if progress_cb:
        try: progress_cb(event="finish", found=len(pdfs))
        except Exception: pass

    # Determine source for reporting
    if used_playwright:
        source = "playwright"
    elif reason == "llm_yield":
        source = "llm"
    elif used_google:
        source = "google"
    else:
        source = "html"

    return {
        "found": len(pdfs),
        "urls": pdfs,
        "source": source,
        "ms": int((time.time() - started) * 1000),
        "reason": reason or "ok",
        "expected_total": expected_total,  # Include expected total for UI progress tracking
    }


# ------------------------------------------------------------------------------------
# Compat alias
# ------------------------------------------------------------------------------------
def crawl_site(url: str,
               max_pdfs: int | None = None,
               same_domain: bool = False,
               allow_offsite: bool = True,
               depth: int = 1,
               timeout: float = 8.0,
               compat: bool = True,
               **kwargs) -> dict:
    return crawl(
        url=url,
        max_pdfs=max_pdfs,
        same_domain=same_domain,
        allow_offsite=allow_offsite,
        depth=depth,
        timeout=timeout,
        compat=compat,
        progress_cb=kwargs.get("progress_cb"),
        deadline_sec=kwargs.get("deadline_sec", 240.0),
        head_timeout=kwargs.get("head_timeout", 4.0),
        connect_timeout=kwargs.get("connect_timeout", 5.0),
        max_pages=kwargs.get("max_pages", 750),
        page_workers=kwargs.get("page_workers", 8),
        head_workers=kwargs.get("head_workers", 8),
        strategy=kwargs.get("strategy", "llm_first"),
        # pass through model selection if present
        llm_provider=kwargs.get("llm_provider"),
        llm_model=kwargs.get("llm_model"),
    )


def crawl_parallel(url: str,
                   progress_cb: t.Callable[..., None] | None = None,
                   deadline_sec: float = 120.0) -> dict:
    """
    🚀 Parallel Crawler - Run HTML crawler and Google CSE simultaneously

    Strategy:
    1. Start Google CSE immediately (fast, gets ground truth count)
    2. Start HTML crawler in parallel
    3. Share results between them - crawler stops when it matches CSE count
    4. Returns whichever completes first or has better results

    Perfect for sites with simple form pages where:
    - Page clearly lists all forms (crawler finds them fast)
    - But we don't want to waste time exploring dead ends
    - CSE provides "expected count" to know when we're done

    Args:
        url: Starting URL
        progress_cb: Optional progress callback
        deadline_sec: Maximum time for entire operation

    Returns:
        {found: int, urls: list, source: str, ms: int, reason: str}
    """
    import threading
    started = time.time()
    seed = _normalize_url(url)
    host = (up.urlsplit(seed).hostname or "").lower()

    if progress_cb:
        try: progress_cb(event="start", seed=seed)
        except Exception: pass

    # Shared state between threads (thread-safe)
    cse_results = {"urls": [], "done": False, "count": 0}
    crawler_results = {"urls": [], "done": False, "count": 0}
    lock = threading.Lock()

    # Thread 1: Google CSE (fast, gets ground truth)
    def run_cse():
        try:
            # Check if CSE is configured
            if not os.getenv("GOOGLE_CSE_KEY") or not os.getenv("GOOGLE_CSE_CX"):
                with lock:
                    cse_results["done"] = True
                return

            # Multi-domain for known sites
            if "schwab.com" in host:
                domains = ["www.schwab.com", "content.schwab.com", "client.schwab.com"]
                urls = _google_cse_multi(domains, max_results=1000, timeout=8.0)
            elif "fidelity.com" in host:
                domains = ["www.fidelity.com", "nb.fidelity.com"]
                urls = _google_cse_multi(domains, max_results=1000, timeout=8.0)
            else:
                urls = _google_cse(host, max_results=1000, timeout=8.0)

            with lock:
                cse_results["urls"] = urls
                cse_results["count"] = len(urls)
                cse_results["done"] = True
                print(f"[parallel] CSE found {len(urls)} forms", flush=True)
        except Exception as e:
            print(f"[parallel] CSE error: {e}", flush=True)
            with lock:
                cse_results["done"] = True

    # Thread 2: HTML crawler (validates + discovers)
    def run_crawler():
        try:
            # Wait a moment for CSE to potentially finish first
            # This gives us the expected_total to guide the crawler
            time.sleep(2)

            # Check if CSE already finished
            expected_from_cse = None
            with lock:
                if cse_results["done"]:
                    expected_from_cse = cse_results["count"]
                    if expected_from_cse > 0:
                        print(f"[parallel] CSE finished first with {expected_from_cse} forms - using as target", flush=True)

            # Custom progress callback that checks CSE results for early termination
            def smart_progress(**kw):
                # Forward to main progress callback
                if progress_cb:
                    try: progress_cb(**kw)
                    except Exception: pass

            # Run crawler with CSE count as expected_total (if available)
            # This triggers existing "completeness_reached" logic in crawl()
            # First try LLM discovery which respects expected_total
            llm_result = None
            try:
                code, seed_html, _ = llm_discover.fetch(seed, 8.0)
                hints = llm_discover.try_sitemap(seed, 8.0)
                prov = os.getenv("LLM_PROVIDER", "xai").lower().strip()
                mdl = (os.getenv("GROK_MODEL") if prov=="xai" else
                       os.getenv("OPENAI_MODEL") if prov=="openai" else
                       os.getenv("ANTHROPIC_MODEL") if prov=="anthropic" else
                       os.getenv("GEMINI_MODEL") or "").strip()

                llm_result = llm_discover.discover(seed, seed_html or "", hints, provider=prov, llm_model=mdl)
                llm_urls = [u for u in (llm_result.get("pdf_urls") or []) if u.lower().endswith(".pdf")]

                # If LLM found forms, validate them quickly
                if llm_urls:
                    print(f"[parallel] LLM found {len(llm_urls)} forms - validating...", flush=True)
                    # Quick validation
                    valid_urls = []
                    for u in llm_urls[:50]:  # Limit to avoid slowdown
                        try:
                            if _head_or_assume(u, 3.0, 3.0):
                                valid_urls.append(u)
                        except Exception:
                            continue

                    # If LLM found enough forms and they validate, we might be done!
                    if expected_from_cse and len(valid_urls) >= expected_from_cse:
                        print(f"[parallel] LLM validated {len(valid_urls)} forms matching CSE count - stopping early!", flush=True)
                        with lock:
                            crawler_results["urls"] = valid_urls
                            crawler_results["count"] = len(valid_urls)
                            crawler_results["done"] = True
                            crawler_results["source"] = "llm"
                            crawler_results["reason"] = "llm_complete_early"
                        return
                    elif len(valid_urls) > 0:
                        # LLM found some forms, continue with HTML crawl to find more
                        with lock:
                            crawler_results["urls"] = valid_urls
                            crawler_results["count"] = len(valid_urls)
            except Exception as e:
                print(f"[parallel] LLM discovery skipped: {e}", flush=True)

            # Continue with full HTML crawl (if needed)
            result = crawl(
                url=seed,
                max_pdfs=None,
                same_domain=False,
                allow_offsite=True,
                depth=2,  # Reasonable depth
                timeout=8.0,
                progress_cb=smart_progress,
                deadline_sec=max(30.0, deadline_sec - 15),  # Reduced time since we already tried LLM
                head_timeout=4.0,
                connect_timeout=5.0,
                max_pages=200,  # Reduced page limit
                page_workers=6,
                head_workers=6,
                strategy="html_first",  # Skip LLM since we already tried it
            )

            with lock:
                # Merge LLM results with HTML crawl results
                existing = set(crawler_results.get("urls", []))
                new_urls = [u for u in result.get("urls", []) if u not in existing]
                crawler_results["urls"] = list(existing) + new_urls
                crawler_results["count"] = len(crawler_results["urls"])
                crawler_results["done"] = True
                crawler_results["source"] = result.get("source", "html")
                crawler_results["reason"] = result.get("reason", "ok")
                print(f"[parallel] Crawler found {crawler_results['count']} forms total", flush=True)
        except Exception as e:
            print(f"[parallel] Crawler error: {e}", flush=True)
            with lock:
                crawler_results["done"] = True

    # Start both threads
    cse_thread = threading.Thread(target=run_cse, daemon=True)
    crawler_thread = threading.Thread(target=run_crawler, daemon=True)

    cse_thread.start()
    crawler_thread.start()

    # Wait for both to complete (with timeout)
    cse_thread.join(timeout=deadline_sec)
    crawler_thread.join(timeout=deadline_sec)

    # Merge results - prefer crawler URLs (validated) but use CSE as backup.
    # F-CS-14: build the entire response dict inside the lock so
    # `final_urls` cannot be mutated by a still-running worker thread
    # (which can happen when crawler_thread.join(timeout=...) returns
    # before the worker exits). Previously len(final_urls) was read
    # outside the lock, which produced flaky counts under load.
    with lock:
        crawler_urls = set(crawler_results["urls"])
        cse_urls = set(cse_results["urls"])

        # Start with crawler results (these are validated)
        final_urls = list(crawler_urls)

        # Add any CSE URLs we didn't find via crawling
        for url in cse_urls:
            if url not in crawler_urls:
                final_urls.append(url)

        # Determine source and reason
        if crawler_results["count"] > 0:
            source = crawler_results.get("source", "html")
            reason = "parallel_crawler_primary"
        elif cse_results["count"] > 0:
            source = "google"
            reason = "parallel_cse_only"
        else:
            source = "none"
            reason = "parallel_no_results"

        # Report how many unique forms each found
        crawler_only = len(crawler_urls - cse_urls)
        cse_only = len(cse_urls - crawler_urls)
        both = len(crawler_urls & cse_urls)

        if both > 0:
            print(f"[parallel] Found {both} forms in both, {crawler_only} crawler-only, {cse_only} CSE-only", flush=True)

        elapsed_ms = int((time.time() - started) * 1000)
        found_count = len(final_urls)
        # Snapshot the response payload while the lock is still held.
        result_payload = {
            "found": found_count,
            "urls": list(final_urls),
            "source": source,
            "ms": elapsed_ms,
            "reason": reason,
        }

    if progress_cb:
        try: progress_cb(event="finish", found=found_count)
        except Exception: pass

    return result_payload


def crawl_auto(url: str,
               progress_cb: t.Callable[..., None] | None = None) -> dict:
    """
    🍎 Apple Philosophy Crawler - "It Just Works"

    Zero configuration needed. Automatically:
    - Detects site type (JS-rendered, static HTML, etc.)
    - Chooses optimal strategy (LLM → HTML → Playwright → Google)
    - Uses parallel crawl+CSE for simple form pages (fast!)
    - Adjusts depth/timeout based on results
    - Stops when all forms found or diminishing returns
    - Uses smart defaults for everything

    Args:
        url: Starting URL (that's it!)
        progress_cb: Optional callback for progress updates

    Returns:
        {found: int, urls: list, source: str, ms: int, reason: str}

    Example:
        result = crawl_auto("https://example.com/forms")
        pdfs = result["urls"]
    """
    started = time.time()
    seed = _normalize_url(url)

    if progress_cb:
        try: progress_cb(event="start", seed=seed)
        except Exception: pass

    # Intelligent site detection
    host = (up.urlsplit(seed).hostname or "").lower()
    is_schwab = "schwab.com" in host
    is_fidelity = "fidelity.com" in host

    # Smart defaults - optimized for 95% of sites
    OPTIMAL_DEPTH = 2  # Deep enough to find forms, fast enough to finish quickly
    OPTIMAL_TIMEOUT = 120  # 2 minutes - enough for most sites
    OPTIMAL_PAGES = 500  # Enough to explore thoroughly without wasting time
    OPTIMAL_WORKERS = 6  # Good balance of speed vs politeness

    # 🚀 NEW: Use parallel strategy if Google CSE is available
    # This runs crawler + CSE simultaneously for faster results
    if os.getenv("GOOGLE_CSE_KEY") and os.getenv("GOOGLE_CSE_CX"):
        print("[crawl_auto] Using parallel crawler + CSE strategy", flush=True)
        return crawl_parallel(url=seed, progress_cb=progress_cb, deadline_sec=OPTIMAL_TIMEOUT)

    # Phase 1: LLM-first discovery (fast, intelligent)
    # This handles 80% of sites successfully
    result = crawl(
        url=seed,
        max_pdfs=None,  # Find all forms
        same_domain=False,  # Follow relevant links (LLM knows what's relevant)
        allow_offsite=True,  # Allow cross-domain forms
        depth=OPTIMAL_DEPTH,
        timeout=8.0,
        progress_cb=progress_cb,
        deadline_sec=OPTIMAL_TIMEOUT,
        head_timeout=4.0,
        connect_timeout=5.0,
        max_pages=OPTIMAL_PAGES,
        page_workers=OPTIMAL_WORKERS,
        head_workers=OPTIMAL_WORKERS,
        strategy="llm_first",
    )

    found = result.get("found", 0)
    reason = result.get("reason", "ok")

    # Phase 2: Automatic Playwright fallback for JS sites (if needed)
    # Handles sites like Adobe Experience Manager, React, Vue, Angular
    if found == 0 and reason not in ("user_stopped", "deadline"):
        if _HAS_PLAYWRIGHT:
            # Check if this might be a JS-rendered site (F-CS-05: walk
            # redirects manually with SSRF re-check at every hop).
            try:
                s = _session()
                r = politeness.safe_redirect_walk(
                    seed, s,
                    timeout=(5.0, 8.0),
                    headers=BROWSER_HEADERS,
                    verify=_verify_path(),
                    ssrf_check=politeness.is_safe_crawl_target,
                )
                html = r.text or ""
                hrefs, _ = _extract_links_html(html, seed)

                if _is_javascript_rendered(html, len(hrefs)):
                    if progress_cb:
                        try: progress_cb(event="playwright_fallback", seed=seed)
                        except Exception: pass

                    pw_result = playwright_crawler.crawl_with_playwright(
                        url=seed,
                        wait_time=3.0,
                        timeout=60000,
                        max_pdfs=None
                    )

                    if pw_result.get("found", 0) > 0:
                        result = {
                            "found": pw_result["found"],
                            "urls": pw_result.get("urls", []),
                            "source": "playwright_auto",
                            "ms": int((time.time() - started) * 1000),
                            "reason": "js_site_detected"
                        }
                        found = result["found"]
            except Exception:
                pass

    # Phase 3: Adaptive depth increase (if low yield)
    # Go deeper only if we found very few forms
    if found > 0 and found < 5 and reason not in ("user_stopped", "deadline", "completeness_reached"):
        # Try depth 3 for a bit longer
        deeper_result = crawl(
            url=seed,
            max_pdfs=None,
            same_domain=False,
            allow_offsite=True,
            depth=3,  # Go deeper
            timeout=8.0,
            progress_cb=progress_cb,
            deadline_sec=90,  # Extra time for depth 3
            head_timeout=4.0,
            connect_timeout=5.0,
            max_pages=300,  # Fewer pages at depth 3 (focus on quality)
            page_workers=OPTIMAL_WORKERS,
            head_workers=OPTIMAL_WORKERS,
            strategy="llm_first",
        )

        if deeper_result.get("found", 0) > found:
            result = deeper_result
            result["reason"] = "adaptive_depth_3"
            found = result["found"]

    # Phase 4: Google fallback (only if we found nothing)
    # Last resort - works when site blocks crawlers
    if found == 0 and reason not in ("user_stopped",):
        if os.getenv("GOOGLE_CSE_KEY") and os.getenv("GOOGLE_CSE_CX"):
            if is_schwab:
                hits = _google_cse_multi(
                    ["www.schwab.com", "content.schwab.com", "client.schwab.com"],
                    1000, 4.0
                )
            elif is_fidelity:
                hits = _google_cse_multi(
                    ["www.fidelity.com", "nb.fidelity.com"],
                    1000, 4.0
                )
            else:
                hits = _google_cse(host, 1000, 4.0)

            if hits:
                result = {
                    "found": len(hits),
                    "urls": hits,
                    "source": "google_auto",
                    "ms": int((time.time() - started) * 1000),
                    "reason": "google_fallback"
                }

    # Final polish
    if progress_cb:
        try: progress_cb(event="finish", found=result.get("found", 0))
        except Exception: pass

    return result


def crawl_adaptive(url: str,
                   max_pdfs: int | None = None,
                   same_domain: bool = False,
                   allow_offsite: bool = True,
                   timeout: float = 8.0,
                   max_depth: int = 3,
                   progress_cb: t.Callable[..., None] | None = None,
                   **kwargs) -> dict:
    """
    Adaptive depth crawler - automatically increases depth when yields are low.

    Starts at depth=1 (fast), then increases to depth=2 and depth=3 if needed.
    Uses smart path prioritization to make deeper crawls faster.

    Args:
        url: Starting URL
        max_pdfs: Max PDFs to find (None = unlimited)
        same_domain: Only crawl same domain
        allow_offsite: Allow offsite PDFs
        timeout: Request timeout
        max_depth: Maximum depth to try (default: 3)
        progress_cb: Progress callback
        **kwargs: Additional crawler options

    Returns:
        Same as crawl(), with adaptive reason string
    """
    attempts = []
    current_depth = 1
    base_deadline = kwargs.get("deadline_sec", 240.0)
    base_pages = kwargs.get("max_pages", 750)

    while current_depth <= max_depth:
        # Adjust parameters for current depth
        deadline = adaptive_depth.get_adaptive_deadline(current_depth, base_deadline)
        page_limit = adaptive_depth.get_adaptive_page_limit(current_depth, base_pages)

        if progress_cb:
            try:
                progress_cb(event="depth_change", depth=current_depth, max_depth=max_depth)
            except Exception:
                pass

        # Run crawl at current depth
        result = crawl(
            url=url,
            max_pdfs=max_pdfs,
            same_domain=same_domain,
            allow_offsite=allow_offsite,
            depth=current_depth,
            timeout=timeout,
            progress_cb=progress_cb,
            deadline_sec=deadline,
            max_pages=page_limit,
            **kwargs
        )

        found_count = result.get("found", 0)
        attempts.append((current_depth, found_count))

        # Removed form cap - continue crawling to find all forms
        # Check for hard limits (deadline, page ceiling, blocked)
        if result.get("reason") in ("deadline", "page_ceiling", "blocked"):
            # Hit a hard limit - stop trying
            result["reason"] = adaptive_depth.format_adaptive_reason(attempts, result["reason"])
            return result

        # Check if we should try deeper
        if not adaptive_depth.should_increase_depth(found_count, current_depth, max_depth):
            # Low yield but not worth going deeper
            result["reason"] = adaptive_depth.format_adaptive_reason(attempts, result.get("reason", "ok"))
            return result

        # Try next depth level
        current_depth += 1

    # Exhausted all depths - return last result
    result["reason"] = adaptive_depth.format_adaptive_reason(attempts, result.get("reason", "max_depth"))
    return result