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
   GET  /api/crawl_and_analyze/<job_id>/dashboard
   POST /api/crawl_and_analyze/<job_id>/stop

Jobs are persisted in the `crawl_jobs` Postgres table so any Cloud Run
instance can answer polls and the orchestrator survives instance recycling.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from flask import Blueprint, Response, jsonify, request

from app.services import analyzer, crawler, dashboard_builder, jobs_store, progress, triage
from app.services import storage

logger = logging.getLogger(__name__)

bp = Blueprint("crawl_and_analyze", __name__)


def _institution_from_url(url: str) -> str:
    try:
        from urllib.parse import urlsplit
        host = urlsplit(url).hostname or ""
        host = host.replace("www.", "").split(".")[0]
        return host.title() if host else "Organization"
    except Exception:
        return "Organization"


def _stop_check(job_id: str) -> bool:
    """Composite stop signal: per-job DB flag OR global progress stop."""
    if progress.should_stop():
        return True
    try:
        return jobs_store.is_stop_requested(job_id)
    except Exception:
        return False


# ── Pipeline stages ────────────────────────────────────────────────────────

def _do_crawl(job_id: str, root_url: str, max_pages: int, depth: int,
              same_domain: bool) -> List[str]:
    jobs_store.update_job(job_id, status="crawling",
                          message=f"Crawling {root_url} (budget {max_pages} pages)")
    progress.reset()
    progress.set_job_id(job_id)
    progress.start_crawl(root_url)

    last_db_write = [0.0]

    def cb(*, done=0, total=0, queue=0, pdfs=0, url=""):
        # Local progress service updates every callback (cheap)
        progress.update_crawl(done=done, total=total, queue=queue, pdfs=pdfs, url=url)
        # Debounce DB writes so we don't hammer Postgres mid-crawl
        now = time.time()
        if now - last_db_write[0] < 1.5 and pdfs % 5 != 0:
            return
        last_db_write[0] = now
        jobs_store.update_job(
            job_id,
            state_patch={"crawl": {"done": done, "total": total,
                                   "pdfs": pdfs, "url": url[:300]}},
        )

    # crawl_auto() is the "It Just Works" entry point — it picks the
    # right strategy (HTML / Playwright / Google CSE / LLM-assisted) per
    # site, including JS-rendered pages. crawl_site() falls flat on
    # JavaScript-driven listings like bank/insurer form portals.
    result = crawler.crawl_auto(url=root_url, progress_cb=cb)
    urls = [u.strip() for u in (result.get("urls") or result.get("pdfs") or []) if u]
    urls = list(dict.fromkeys(urls))

    # Respect the max_pages-style cap as a cap on results (the crawler's
    # smart defaults already throttle pages internally).
    if max_pages and len(urls) > max_pages * 10:
        logger.info("[CRAWL_AND_ANALYZE] %s: trimming %d→%d URLs to fit budget",
                    job_id, len(urls), max_pages * 10)
        urls = urls[: max_pages * 10]

    jobs_store.update_job(
        job_id,
        state_patch={"crawl": {"pdfs": len(urls), "total": len(urls), "done": len(urls)}},
    )
    progress.finish_crawl(len(urls))
    logger.info("[CRAWL_AND_ANALYZE] %s: crawl found %d PDFs", job_id, len(urls))
    return urls


def _do_triage(job_id: str, urls: List[str]) -> List[str]:
    jobs_store.update_job(
        job_id,
        status="triaging",
        state_patch={"triage": {"total": len(urls), "done": 0, "kept": 0,
                                "dropped": 0, "by_class": {}}},
    )
    keepers: List[str] = []
    by_class: Dict[str, int] = {}
    kept = dropped = 0

    for i, url in enumerate(urls, 1):
        if _stop_check(job_id):
            break

        # Cheap probe: parse the PDF only — no LLM vision yet.
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
                force_minimal=True,
                skip_vision=True,
                skip_llm_title=True,
            )
            if isinstance(probe, dict):
                title = probe.get("form_name") or probe.get("pretty_title") or ""
                pages = int(probe.get("pages") or 0)
                fc = int(probe.get("field_count") or 0)
                has_widgets = fc > 0
                text_head = (probe.get("full_text") or "")[:2000]
        except Exception as e:
            logger.warning("[TRIAGE] probe failed for %s: %s", url, e)

        verdict = triage.classify(
            url=url, filename=filename, title=title,
            pages=pages, has_widgets=has_widgets, text=text_head,
        )

        cls = verdict["classification"]
        by_class[cls] = by_class.get(cls, 0) + 1
        if verdict["should_analyze"]:
            keepers.append(url)
            kept += 1
        else:
            dropped += 1

        # Annotate the stored record with the triage decision
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

        # Update DB on every 3rd item OR final item to keep poll latency low
        if i % 3 == 0 or i == len(urls):
            jobs_store.update_job(
                job_id,
                state_patch={"triage": {"done": i, "total": len(urls),
                                        "kept": kept, "dropped": dropped,
                                        "by_class": by_class}},
            )
        progress.update_triage(done=i, total=len(urls), kept=kept, dropped=dropped)

    progress.finish_triage()
    logger.info("[CRAWL_AND_ANALYZE] %s: triage kept %d/%d (by_class=%s)",
                job_id, len(keepers), len(urls), by_class)
    return keepers


def _do_analyze(job_id: str, urls: List[str]) -> List[Dict[str, Any]]:
    jobs_store.update_job(
        job_id,
        status="analyzing",
        state_patch={"analyze": {"done": 0, "total": len(urls), "errors": 0}},
    )
    records: List[Dict[str, Any]] = []
    errors = 0

    for i, url in enumerate(urls, 1):
        if _stop_check(job_id):
            break
        try:
            rec = analyzer.analyze_pdf(pdf_url=url, timeout=60, force_minimal=False)
            if isinstance(rec, dict):
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
            errors += 1
            logger.warning("[ANALYZE] failed for %s: %s", url, e)

        if i % 2 == 0 or i == len(urls):
            jobs_store.update_job(
                job_id,
                state_patch={"analyze": {"done": i, "total": len(urls), "errors": errors}},
            )
        progress.set_analyze_progress(i, len(urls))

    progress.finish_analyze()
    logger.info("[CRAWL_AND_ANALYZE] %s: analyzed %d records (%d errors)",
                job_id, len(records), errors)
    return records


def _do_dashboard(job_id: str, records: List[Dict[str, Any]],
                  branding: Dict[str, Any], root_url: str) -> None:
    if not records:
        jobs_store.update_job(
            job_id,
            state_patch={"dashboard": {"ready": False, "html_url": "",
                                        "reason": "no_records"}},
        )
        return
    try:
        html = dashboard_builder.build_dashboard_html(
            records=records,
            institution_name=branding.get("institution_name") or _institution_from_url(root_url),
            palette=branding.get("palette", "blue"),
        )
        jobs_store.update_job(
            job_id,
            html=html,
            state_patch={"dashboard": {
                "ready": True,
                "html_url": f"/api/crawl_and_analyze/{job_id}/dashboard",
            }},
        )
        progress.set_dashboard_ready(f"/api/crawl_and_analyze/{job_id}/dashboard")
        logger.info("[CRAWL_AND_ANALYZE] %s: dashboard built (%d KB)",
                    job_id, len(html) // 1024)
    except Exception as e:
        logger.exception("[CRAWL_AND_ANALYZE] %s: dashboard build failed: %s", job_id, e)
        jobs_store.update_job(
            job_id,
            state_patch={"dashboard": {"ready": False, "html_url": "", "error": str(e)}},
        )


def _pipeline(job_id: str, root_url: str, branding: Dict[str, Any],
              max_pages: int, depth: int, same_domain: bool, build_dash: bool) -> None:
    try:
        urls = _do_crawl(job_id, root_url, max_pages, depth, same_domain)
        if _stop_check(job_id):
            jobs_store.update_job(job_id, status="stopped",
                                  message="Stopped by user during crawl")
            return

        keepers = _do_triage(job_id, urls)
        if _stop_check(job_id):
            jobs_store.update_job(job_id, status="stopped",
                                  message="Stopped by user during triage")
            return

        records = _do_analyze(job_id, keepers)
        if build_dash:
            _do_dashboard(job_id, records, branding, root_url)

        # Final summary
        triage_state = jobs_store.get_job(job_id) or {}
        summary = {
            "discovered": len(urls),
            "kept_after_triage": len(keepers),
            "analyzed": len(records),
            "analyze_errors": (triage_state.get("analyze") or {}).get("errors", 0),
            "triage_classes": (triage_state.get("triage") or {}).get("by_class", {}),
        }
        jobs_store.update_job(
            job_id,
            status="done",
            message="Pipeline complete",
            state_patch={"summary": summary},
        )
    except Exception as e:
        logger.exception("[CRAWL_AND_ANALYZE] %s: pipeline crashed: %s", job_id, e)
        jobs_store.update_job(job_id, status="error", message=str(e))


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

    branding = {
        "institution_name": j.get("institution_name") or _institution_from_url(root_url),
        "palette": j.get("palette", "blue"),
    }

    job = jobs_store.create_job(root_url=root_url, branding=branding)

    t = threading.Thread(
        target=_pipeline,
        args=(job["job_id"], root_url, branding, max_pages, depth, same_domain, build_dash),
        daemon=True,
    )
    t.start()

    return jsonify({"ok": True, "job_id": job["job_id"], "status": "queued"}), 202


@bp.get("/crawl_and_analyze/<job_id>")
def status(job_id: str):
    job = jobs_store.get_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "unknown job_id"}), 404
    return jsonify({"ok": True, **job})


@bp.get("/crawl_and_analyze/<job_id>/dashboard")
def fetch_dashboard(job_id: str):
    job = jobs_store.get_job(job_id, include_html=True)
    if not job:
        return jsonify({"ok": False, "error": "unknown job_id"}), 404
    html = job.get("html")
    if not html:
        return jsonify({"ok": False, "error": "dashboard not ready yet"}), 409
    return Response(html, mimetype="text/html")


@bp.post("/crawl_and_analyze/<job_id>/stop")
def stop(job_id: str):
    job = jobs_store.get_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "unknown job_id"}), 404
    jobs_store.update_job(job_id, stop_requested=True)
    progress.request_stop()
    return jsonify({"ok": True, "stop_requested": True})
