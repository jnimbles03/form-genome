# app/api/crawl.py
from __future__ import annotations

import json
import logging
import os
from urllib.parse import urlparse

from flask import Blueprint, request, jsonify
from app.services import crawler, politeness
import app.services.progress as prog

logger = logging.getLogger(__name__)

bp = Blueprint("crawl", __name__)


# ---------------------------------------------------------------------------
# SSRF defense (F-CS-05) lives in app/services/politeness.py so it can be
# reused by the redirect walker. We re-export here for back-compat with any
# callers that imported `_is_safe_crawl_target` directly from this module.
# ---------------------------------------------------------------------------

def _is_safe_crawl_target(url: str) -> tuple[bool, str]:
    """Back-compat shim. Prefer politeness.is_safe_crawl_target."""
    return politeness.is_safe_crawl_target(url)

# --------- helpers ---------
def _load_ui_prefs() -> dict:
    """
    Load persisted UI prefs (e.g., llm_provider, llm_model, column widths).
    We store this in project_root/data/ui_prefs.json (same place admin writes).
    """
    # app/api/.. -> app -> project root
    base = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".."))
    data_dir = os.path.join(base, "data")
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "ui_prefs.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _parse_int(x, default=None):
    try:
        return int(x)
    except Exception:
        return default

def _parse_float(x, default=None):
    try:
        return float(x)
    except Exception:
        return default

def _determine_timeout(domain: str) -> float:
    """
    Intelligent timeout scaling based on domain characteristics.

    Returns timeout in seconds for the request.

    Categories:
    - Known slow sites (Adobe, Medicare, Canada): 600s (10 minutes)
    - Large government sites: 480s (8 minutes)
    - Standard sites: 300s (5 minutes)
    """
    domain_lower = domain.lower()

    # Known problematic sites that timeout frequently - give them extra time
    known_slow_sites = [
        'adobe.com',
        'medicare.gov',
        'canada.ca',
        'redcross.org',  # Took 136s in testing
    ]

    # Large government/enterprise sites that may need more time
    large_sites = [
        '.gov.uk',
        'va.gov',
        'irs.gov',
        'ssa.gov',
        'studentaid.gov',
        'healthcare.gov',
    ]

    # Check for known slow sites - 10 minutes
    for slow_site in known_slow_sites:
        if slow_site in domain_lower:
            print(f"[TIMEOUT] Known slow site detected: {domain} → 600s timeout")
            return 600.0

    # Check for large government sites - 8 minutes
    for large_site in large_sites:
        if large_site in domain_lower:
            print(f"[TIMEOUT] Large site detected: {domain} → 480s timeout")
            return 480.0

    # Default - 5 minutes
    print(f"[TIMEOUT] Standard site: {domain} → 300s timeout")
    return 300.0

# --------- route ---------
@bp.post("/crawl")
def crawl_route():
    """
    🍎 Apple Philosophy Crawler - "It Just Works"

    Body (JSON):
    {
      "url": "https://site/page"  // That's it!
    }

    Returns: { ok, found, urls, source, ms, reason }

    All configuration is automatic:
    - Detects site type (JS-rendered, static HTML, etc.)
    - Chooses optimal strategy (LLM → HTML → Playwright → Google)
    - Adjusts depth/timeout based on results
    - Stops when all forms found or diminishing returns
    """
    j = request.get_json(force=True) or {}
    seed = (j.get("url") or "").strip()
    if not seed:
        return jsonify({"ok": False, "error": "Missing 'url'"}), 400

    # SSRF guard (F-CS-05): reject internal / metadata / RFC1918 targets
    # before any outbound request is made. Wave 1.5 also closes the
    # redirect-hop gap by replacing requests' `allow_redirects=True`
    # with `politeness.safe_redirect_walk` for seed fetches inside the
    # crawler — every hop is re-validated.
    safe, reason = _is_safe_crawl_target(seed)
    if not safe:
        logger.warning("Rejected /crawl request: %s (url=%s)", reason, seed)
        return jsonify({
            "ok": False,
            "error": "URL not allowed",
            "reason": reason,
        }), 400

    # Start crawl progress
    prog.start_crawl(seed)

    def _cb(**kw):
        ev = kw.pop("event", "")
        if ev == "page":
            prog.update_crawl(done=kw.get("done"),
                              queue=kw.get("queue"),
                              pdfs=kw.get("pdfs"),
                              url=kw.get("url"))
        elif ev == "pdf":
            # Update both PDFs found AND pages visited
            prog.update_crawl(done=kw.get("done"),
                              pdfs=kw.get("pdfs"))
        elif ev == "finish":
            prog.finish_crawl(found=int(kw.get("found") or 0))
        # 'start' handled above

    # Detect if this is a domain URL (for intelligent hybrid search) vs specific page/form URL
    parsed = urlparse(seed)
    path = parsed.path.strip('/') if parsed.path else ''

    # If it's just a domain or /forms-type page, use intelligent hybrid search
    # Otherwise, use traditional crawler for specific pages
    is_domain_search = not path or path in ['forms', 'applications', 'documents', 'resources']

    if is_domain_search and os.getenv("GOOGLE_CSE_KEY") and os.getenv("GOOGLE_CSE_CX"):
        # Use intelligent hybrid search: CSE + LLM analysis + targeted crawler
        print(f"[CRAWL] Domain detected - using intelligent hybrid search for {seed}")

        import time
        started = time.time()

        domain = parsed.hostname or parsed.netloc or seed

        # Determine intelligent timeout based on domain
        intelligent_timeout = _determine_timeout(domain)

        # Enhanced progress callback for hybrid stages
        def hybrid_cb(**kw):
            ev = kw.pop("event", "")
            if ev == "hybrid_stage":
                stage = kw.get("stage")
                message = kw.get("message", "")
                print(f"[HYBRID] Stage {stage}: {message}")
                # Update progress state so UI can show what's happening
                prog.start_crawl(seed)  # Ensure status is "crawling"
                import app.services.progress as progress
                with progress._LOCK:
                    progress._STATE["message"] = message
            else:
                _cb(**kw)  # Pass through to original callback

        # Retry logic: Try once, if timeout/error, retry with 2x timeout
        urls = []
        retry_count = 0
        max_retries = 1
        current_timeout = 8.0  # CSE timeout (internal operations)

        while retry_count <= max_retries:
            try:
                print(f"[CRAWL] Attempt {retry_count + 1}/{max_retries + 1} - Timeout: {intelligent_timeout}s")
                urls = crawler.intelligent_hybrid_search(domain, timeout=current_timeout, progress_cb=hybrid_cb)
                break  # Success - exit retry loop
            except Exception as e:
                error_msg = str(e)
                elapsed = time.time() - started
                print(f"[CRAWL] Attempt {retry_count + 1} failed after {elapsed:.1f}s: {error_msg}")

                # Check if we should retry
                if retry_count < max_retries and elapsed < intelligent_timeout * 0.8:
                    retry_count += 1
                    current_timeout *= 2  # Double the internal timeout
                    print(f"[CRAWL] Retrying with increased timeout: {current_timeout}s")
                    time.sleep(2)  # Brief pause before retry
                else:
                    # No more retries or already at max time
                    print(f"[CRAWL] Max retries reached or timeout exceeded - returning partial results")
                    break

        elapsed_ms = int((time.time() - started) * 1000)

        out = {
            "ok": True,
            "found": len(urls),
            "urls": urls,
            "source": "hybrid",
            "ms": elapsed_ms,
            "reason": "intelligent_hybrid_search",
            "retries": retry_count,
            "timeout_used": intelligent_timeout
        }
    else:
        # 🍎 Call the Apple Philosophy crawler - zero configuration!
        out = crawler.crawl_auto(
            url=seed,
            progress_cb=_cb
        )

    # Log analysis activity
    try:
        from flask import session
        from app.services import storage

        user_data = session.get("user", {})
        email = user_data.get("email", "anonymous")
        name = user_data.get("name", "")
        ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)
        user_agent = request.headers.get('User-Agent', 'Unknown')

        storage.log_analysis_activity(
            email=email,
            activity_type="crawl",
            source_url=seed,
            forms_found=int(out.get("found") or 0),
            forms_analyzed=0,
            success=True,
            name=name,
            ip_address=ip_address,
            user_agent=user_agent
        )
    except Exception as log_err:
        # Don't fail the request if logging fails
        print(f"Failed to log crawl activity: {log_err}")

    # Shape response
    return jsonify({
        "ok": True,
        "found": int(out.get("found") or 0),
        "urls": out.get("urls") or [],
        "source": out.get("source") or "html",
        "ms": int(out.get("ms") or 0),
        "reason": out.get("reason") or "ok"
    })


@bp.post("/crawl/stop")
def stop_crawl():
    """
    Stop the currently running crawl.
    Allows users to manually terminate crawling when they have enough forms.
    Returns: { ok: true, message: str }
    """
    prog.request_stop()
    return jsonify({"ok": True, "message": "Stop requested - crawl will terminate shortly"})


@bp.post("/crawl/google")
def crawl_google():
    """
    🔍 Google Search Mode - Find scattered PDFs with no central directory

    Uses Google Custom Search Engine to find form PDFs across a domain,
    even when there's no browsable forms page or linking structure.

    Perfect for companies where you can Google "company.com forms filetype:pdf"
    and find individual forms, but there's no single URL with all forms listed.

    Body (JSON):
    {
      "url": "https://example.com"  // Domain or URL to search
    }

    Returns: { ok, found, urls, source, ms }
    """
    import time

    j = request.get_json(force=True) or {}
    seed = (j.get("url") or "").strip()
    if not seed:
        return jsonify({"ok": False, "error": "Missing 'url'"}), 400

    # SSRF guard (F-CS-05): block internal / metadata / RFC1918 targets.
    safe, reason = _is_safe_crawl_target(seed)
    if not safe:
        logger.warning("Rejected /crawl/google request: %s (url=%s)", reason, seed)
        return jsonify({
            "ok": False,
            "error": "URL not allowed",
            "reason": reason,
        }), 400

    # Extract domain from URL
    try:
        parsed = urlparse(seed)
        domain = parsed.hostname or ""
        if not domain:
            return jsonify({"ok": False, "error": "Invalid URL - could not extract domain"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"Invalid URL: {str(e)}"}), 400

    # Check if Google CSE is configured
    if not os.getenv("GOOGLE_CSE_KEY") or not os.getenv("GOOGLE_CSE_CX"):
        return jsonify({
            "ok": False,
            "error": "Google Custom Search Engine not configured. Please set GOOGLE_CSE_KEY and GOOGLE_CSE_CX environment variables."
        }), 500

    # Start progress tracking
    prog.start_crawl(seed)

    started = time.time()

    # Check for multi-domain companies (Schwab, Fidelity)
    if "schwab.com" in domain.lower():
        domains = ["www.schwab.com", "content.schwab.com", "client.schwab.com"]
        urls = crawler._google_cse_multi(domains, timeout=8.0, exhaustive=True)
    elif "fidelity.com" in domain.lower():
        domains = ["www.fidelity.com", "nb.fidelity.com"]
        urls = crawler._google_cse_multi(domains, timeout=8.0, exhaustive=True)
    else:
        # Single domain search with exhaustive mode (bypasses 1000 cap)
        urls = crawler._google_cse_exhaustive(domain, timeout=8.0)

    elapsed_ms = int((time.time() - started) * 1000)
    found = len(urls)

    # Finish progress
    prog.finish_crawl(found=found)

    # Log analysis activity
    try:
        from flask import session
        from app.services import storage

        user_data = session.get("user", {})
        email = user_data.get("email", "anonymous")
        name = user_data.get("name", "")
        ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)
        user_agent = request.headers.get('User-Agent', 'Unknown')

        storage.log_analysis_activity(
            email=email,
            activity_type="crawl_google",
            source_url=seed,
            forms_found=found,
            forms_analyzed=0,
            success=True,
            name=name,
            ip_address=ip_address,
            user_agent=user_agent
        )
    except Exception as log_err:
        print(f"Failed to log Google crawl activity: {log_err}")

    return jsonify({
        "ok": True,
        "found": found,
        "urls": urls,
        "source": "google",
        "ms": elapsed_ms,
        "reason": "google_search_mode"
    })