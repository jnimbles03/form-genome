# app/api/crawl_and_analyze.py
"""
End-to-end pipeline endpoint: crawl a site, triage discovered PDFs to keep
only the real fillable forms, run the deep analyzer on the keepers, then
optionally build a FECA dashboard from the resulting records.

   POST /api/crawl_and_analyze
       body: {
         "root_url": "https://example.com",
         "max_pages": 200,           # crawl page budget (default 200)
         "depth": 99,                # effectively unlimited
         "same_domain": true,
         "build_dashboard": true,    # auto-build at end (default true)
         "institution_name": "Acme", # optional, defaults derived from domain
         "palette": "blue"           # optional
       }
       → 202 { "ok": true, "job_id": "<uuid>" }

   GET  /api/crawl_and_analyze/<job_id>
       → { "status": "...", "crawl": {...}, "triage": {...},
           "analyze": {...}, "dashboard": {...}, "summary": {...} }

   GET  /api/crawl_and_analyze/<job_id>/dashboard
       → HTML (when dashboard.ready is true)

   POST /api/crawl_and_analyze/<job_id>/stop
       → { "ok": true }
"""
from __future__ import annotations

import io
import logging
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from flask import Blueprint, Response, jsonify, request

from app.services import analyzer, crawler, dashboard_builder, progress, triage
from app.services import storage

logger = logging.getLogger(__name__)

bp = Blueprint("crawl_and_analyze", __name__)

# In-memory job table — keyed by job_id. Cloud Run instances don't share
# memory, so polling requests must land on the same instance that started
# the job. With max-instances=10 and current load this is fine; if it
# becomes a problem we move to Postgres.
_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def _new_job(root_url: str) -> Dict[str, Any]:
    job_id = uuid.uuid4().hex
    job = {
        "job_id": job_id,
        "root_url": root_url,
        "status": "queued",
        "message": "",
        "created_at": int(time.time()),
        "ended_at": None,
        "crawl":    {"done": 0, "total": 0, "pdfs": 0, "url": ""},
        "triage":   {"done": 0, "total": 0, "kept": 0, "dropped": 0, "by_class": {}},
        "analyze":  {"done": 0, "total": 0, "errors": 0},
        "dashboard": {"ready": False, "html_url": ""},
        "summary":  {},
        "stop_requested": False,
        "html": None,  # held in memory until fetched
        "branding": {},
    }
    with _jobs_lock:
        _jobs[job_id] = job
    return job


def _get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with _jobs_lock:
        return _jobs.get(job_id)


def _public_view(job: Dict[str, Any]) -> Dict[str, Any]:
    """Strip in-memory HTML blob from polling responses."""
    return {k: v for k, v in job.items() if k != "html"}


# ── Pipeline stages ────────────────────────────────────────────────────────

def _do_crawl(job: Dict[str, Any], max_pages: int, depth: int, same_domain: bool) -> List[str]:
    job["status"] = "crawling"
    job["message"] = f"Crawling {job['root_url']} (budget {max_pages} pages)"
    progress.reset()
    progress.set_job_id(job["job_id"])
    progress.start_crawl(job["root_url"])

    def cb(*, done=0, total=0, queue=0, pdfs=0, url=""):
        job["crawl"].update({"done": done, "total": total, "pdfs": pdfs, "url": url})
        progress.update_crawl(done=done, total=total, queue=queue, pdfs=pdfs, url=url)

    result = crawler.crawl_site(
        url=job["root_url"],
        max_pdfs=None,
        same_domain=same_domain,
        allow_offsite=False,
        depth=depth,
        max_pages=max_pages,
        progress_cb=cb,
        deadline_sec=600.0,
    )
    urls = [u.strip() for u in (result.get("pdfs") or result.get("urls") or []) if u]
    urls = list(dict.fromkeys(urls))  # de-dup, preserve order

    job["crawl"]["pdfs"] = len(urls)
    progress.finish_crawl(len(urls))
    logger.info("[CRAWL_AND_ANALYZE] crawl found %d unique PDF URLs", len(urls))
    return urls


def _do_triage(job: Dict[str, Any], urls: List[str]) -> List[str]:
    job["status"] = "triaging"
    job["triage"]["total"] = len(urls)
    keepers: List[str] = []
    by_class: Dict[str, int] = {}

    for i, url in enumerate(urls, 1):
        if job["stop_requested"] or progress.should_stop():
            break

        # Cheap probe: pull just enough to feed the triage prompt — first
        # ~256KB max. The full PDF goes to the analyzer later if it survives.
        title = ""
        text_head = ""
        pages = 0
        has_widgets = False
        filename = url.rsplit("/", 1)[-1].split("?")[0]
        try:
            probe = analyzer.analyze_pdf(
                pdf_url=url,
                timeout=20,
                max_pdf_mb=40,
                force_minimal=True,   # parse only, no LLM vision
                skip_vision=True,
                skip_llm_title=True,
            )
            if isinstance(probe, dict):
                title = probe.get("form_name") or probe.get("pretty_title") or ""
                pages = int(probe.get("pages") or 0)
                fc = int(probe.get("field_count") or 0)
                has_widgets = fc > 0
                # Use full_text head if available
                text_head = (probe.get("full_text") or "")[:2000]
        except Exception as e:
            logger.warning("[TRIAGE] probe failed for %s: %s", url, e)

        verdict = triage.classify(
            url=url,
            filename=filename,
            title=title,
            pages=pages,
            has_widgets=has_widgets,
            text=text_head,
        )

        cls = verdict["classification"]
        by_class[cls] = by_class.get(cls, 0) + 1

        if verdict["should_analyze"]:
            keepers.append(url)
            job["triage"]["kept"] += 1
        else:
            job["triage"]["dropped"] += 1

        # Persist triage decision onto the record so the dashboard /
        # records list can later surface "what we filtered out".
        try:
            existing = storage.get_by_source_url(url)
        except Exception:
            existing = None
        if existing:
            rec = dict(existing)
            rec["triage_classification"] = cls
            rec["triage_confidence"] = verdict["confidence"]
            rec["triage_reasoning"] = verdict["reasoning"]
            try:
                storage.save(rec)
            except Exception as e:
                logger.warning("[TRIAGE] save annotation failed for %s: %s", url, e)

        job["triage"]["done"] = i
        job["triage"]["by_class"] = by_class
        progress.update_triage(
            done=i, total=len(urls),
            kept=job["triage"]["kept"], dropped=job["triage"]["dropped"]
        )

    progress.finish_triage()
    logger.info("[CRAWL_AND_ANALYZE] triage kept %d/%d (by_class=%s)",
                len(keepers), len(urls), by_class)
    return keepers


def _do_analyze(job: Dict[str, Any], urls: List[str]) -> List[Dict[str, Any]]:
    job["status"] = "analyzing"
    job["analyze"]["total"] = len(urls)
    records: List[Dict[str, Any]] = []
    for i, url in enumerate(urls, 1):
        if job["stop_requested"] or progress.should_stop():
            break
        try:
            rec = analyzer.analyze_pdf(pdf_url=url, timeout=60, force_minimal=False)
            if isinstance(rec, dict):
                # Re-attach triage label (analyzer doesn't carry it through)
                try:
                    existing = storage.get_by_source_url(url)
                    if existing:
                        rec.setdefault("triage_classification",
                                       existing.get("triage_classification"))
                except Exception:
                    pass
                try:
                    rec["conversion"] = analyzer.estimate_conversion_cost(rec)
                except Exception:
                    pass
                try:
                    storage.save(rec)
                except Exception as e:
                    logger.warning("[ANALYZE] save failed for %s: %s", url, e)
                records.append(rec)
        except Exception as e:
            job["analyze"]["errors"] += 1
            logger.warning("[ANALYZE] failed for %s: %s", url, e)
        job["analyze"]["done"] = i
        progress.set_analyze_progress(i, len(urls))

    progress.finish_analyze()
    logger.info("[CRAWL_AND_ANALYZE] analyzed %d records (%d errors)",
                len(records), job["analyze"]["errors"])
    return records


def _do_dashboard(job: Dict[str, Any], records: List[Dict[str, Any]]) -> None:
    if not records:
        job["dashboard"] = {"ready": False, "html_url": "", "reason": "no_records"}
        return
    b = job.get("branding") or {}
    try:
        html = dashboard_builder.build_dashboard_html(
            records=records,
            institution_name=b.get("institution_name") or _institution_from_url(job["root_url"]),
            palette=b.get("palette", "blue"),
        )
        job["html"] = html
        job["dashboard"] = {
            "ready": True,
            "html_url": f"/api/crawl_and_analyze/{job['job_id']}/dashboard",
        }
        progress.set_dashboard_ready(job["dashboard"]["html_url"])
        logger.info("[CRAWL_AND_ANALYZE] dashboard built (%d KB)", len(html) // 1024)
    except Exception as e:
        logger.exception("[CRAWL_AND_ANALYZE] dashboard build failed: %s", e)
        job["dashboard"] = {"ready": False, "html_url": "", "error": str(e)}


def _institution_from_url(url: str) -> str:
    try:
        from urllib.parse import urlsplit
        host = urlsplit(url).hostname or ""
        host = host.replace("www.", "").split(".")[0]
        return host.title() if host else "Organization"
    except Exception:
        return "Organization"


def _pipeline(job_id: str, max_pages: int, depth: int, same_domain: bool, build_dash: bool) -> None:
    job = _get_job(job_id)
    if not job:
        return
    try:
        urls = _do_crawl(job, max_pages=max_pages, depth=depth, same_domain=same_domain)
        if job["stop_requested"]:
            job["status"] = "stopped"
            return
        keepers = _do_triage(job, urls)
        if job["stop_requested"]:
            job["status"] = "stopped"
            return
        records = _do_analyze(job, keepers)
        if build_dash:
            _do_dashboard(job, records)
        job["status"] = "done"
        job["message"] = "Pipeline complete"
        job["summary"] = {
            "discovered": len(urls),
            "kept_after_triage": len(keepers),
            "analyzed": len(records),
            "analyze_errors": job["analyze"]["errors"],
            "triage_classes": job["triage"]["by_class"],
        }
    except Exception as e:
        logger.exception("[CRAWL_AND_ANALYZE] pipeline crashed: %s", e)
        job["status"] = "error"
        job["message"] = str(e)
    finally:
        job["ended_at"] = int(time.time())


# ── Routes ─────────────────────────────────────────────────────────────────

@bp.post("/crawl_and_analyze")
def kickoff():
    j = request.get_json(force=True, silent=True) or {}
    root_url = (j.get("root_url") or "").strip()
    if not root_url.startswith(("http://", "https://")):
        return jsonify({"ok": False, "error": "root_url must start with http(s)://"}), 400

    max_pages = int(j.get("max_pages", 200))
    depth = int(j.get("depth", 99))
    same_domain = bool(j.get("same_domain", True))
    build_dash = bool(j.get("build_dashboard", True))

    job = _new_job(root_url)
    job["branding"] = {
        "institution_name": j.get("institution_name") or _institution_from_url(root_url),
        "palette": j.get("palette", "blue"),
    }

    t = threading.Thread(
        target=_pipeline,
        args=(job["job_id"], max_pages, depth, same_domain, build_dash),
        daemon=True,
    )
    t.start()

    return jsonify({"ok": True, "job_id": job["job_id"], "status": "queued"}), 202


@bp.get("/crawl_and_analyze/<job_id>")
def status(job_id: str):
    job = _get_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "unknown job_id"}), 404
    return jsonify({"ok": True, **_public_view(job)})


@bp.get("/crawl_and_analyze/<job_id>/dashboard")
def fetch_dashboard(job_id: str):
    job = _get_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "unknown job_id"}), 404
    html = job.get("html")
    if not html:
        return jsonify({"ok": False, "error": "dashboard not ready yet"}), 409
    return Response(html, mimetype="text/html")


@bp.post("/crawl_and_analyze/<job_id>/stop")
def stop(job_id: str):
    job = _get_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "unknown job_id"}), 404
    job["stop_requested"] = True
    progress.request_stop()
    return jsonify({"ok": True, "stop_requested": True})
