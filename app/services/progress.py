# app/services/progress.py
from __future__ import annotations
import time
import threading
from typing import Dict, Any

_LOCK = threading.Lock()
_STATE: Dict[str, Any] = {
    "crawl":   {"done": 0, "total": 0, "queue": 0, "pdfs": 0, "url": "", "ts": 0},
    "triage":  {"done": 0, "total": 0, "kept": 0, "dropped": 0, "ts": 0},
    "analyze": {"done": 0, "total": 0, "ts": 0},
    "dashboard": {"ready": False, "url": "", "ts": 0},
    "job_id": "",
    "since": 0,
    "status": "idle",
    "message": "",
    "stop_requested": False
}

def _now() -> int:
    return int(time.time())

def reset() -> None:
    with _LOCK:
        _STATE["crawl"]   = {"done": 0, "total": 0, "queue": 0, "pdfs": 0, "url": "", "ts": _now()}
        _STATE["triage"]  = {"done": 0, "total": 0, "kept": 0, "dropped": 0, "ts": _now()}
        _STATE["analyze"] = {"done": 0, "total": 0, "ts": _now()}
        _STATE["dashboard"] = {"ready": False, "url": "", "ts": _now()}
        _STATE["job_id"]  = ""
        _STATE["since"]   = _now()
        _STATE["status"]  = "idle"
        _STATE["message"] = ""
        _STATE["stop_requested"] = False


def set_job_id(job_id: str) -> None:
    with _LOCK:
        _STATE["job_id"] = str(job_id)


def update_triage(*, done: int | None = None,
                  total: int | None = None,
                  kept: int | None = None,
                  dropped: int | None = None) -> None:
    with _LOCK:
        t = _STATE["triage"]
        if done    is not None: t["done"]    = int(done)
        if total   is not None: t["total"]   = int(total)
        if kept    is not None: t["kept"]    = int(kept)
        if dropped is not None: t["dropped"] = int(dropped)
        t["ts"] = _now()
        _STATE["status"]  = "triaging"
        _STATE["message"] = f"Triaging {t['done']}/{t['total']} (kept {t['kept']}, dropped {t['dropped']})"


def finish_triage() -> None:
    with _LOCK:
        _STATE["status"] = "triage_done"
        _STATE["triage"]["ts"] = _now()


def set_dashboard_ready(url: str) -> None:
    with _LOCK:
        _STATE["dashboard"] = {"ready": True, "url": str(url), "ts": _now()}
        _STATE["status"]  = "done"
        _STATE["message"] = "Dashboard ready"

def start_crawl(seed_url: str) -> None:
    with _LOCK:
        _STATE["crawl"] = {"done": 0, "total": 0, "queue": 0, "pdfs": 0, "url": seed_url, "ts": _now()}
        _STATE["status"]  = "crawling"
        _STATE["message"] = f"Crawling: {seed_url}"
        _STATE["since"]   = _now()

def update_crawl(*, done: int | None = None,
                 total: int | None = None,
                 queue: int | None = None,
                 pdfs: int  | None = None,
                 url:  str  | None = None) -> None:
    with _LOCK:
        c = _STATE["crawl"]
        if done  is not None: c["done"]  = int(done)
        if total is not None: c["total"] = int(total)
        if queue is not None: c["queue"] = int(queue)
        if pdfs  is not None: c["pdfs"]  = int(pdfs)
        if url   is not None: c["url"]   = url
        c["ts"] = _now()

def finish_crawl(found: int) -> None:
    with _LOCK:
        _STATE["status"]  = "crawl_done"
        _STATE["message"] = f"Crawl complete — found {int(found)} PDF(s)"
        _STATE["crawl"]["ts"] = _now()

def set_analyze_progress(done: int, total: int) -> None:
    with _LOCK:
        a = _STATE["analyze"]
        a["done"]  = int(done)
        a["total"] = int(total)
        a["ts"]    = _now()
        _STATE["status"] = "analyzing"

def finish_analyze() -> None:
    with _LOCK:
        _STATE["status"]  = "analyze_done"
        _STATE["message"] = "Analyze complete"
        _STATE["analyze"]["ts"] = _now()

def get() -> Dict[str, Any]:
    with _LOCK:
        import copy
        return copy.deepcopy(_STATE)

def request_stop() -> None:
    """Request the crawl to stop at the next opportunity"""
    with _LOCK:
        _STATE["stop_requested"] = True

def should_stop() -> bool:
    """Check if a stop has been requested"""
    with _LOCK:
        return bool(_STATE.get("stop_requested", False))