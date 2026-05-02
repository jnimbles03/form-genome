# app/api/batch_reanalyze.py
from flask import Blueprint, current_app, request, jsonify
from app.services import storage, analyzer
import time
import threading

bp = Blueprint("batch_reanalyze", __name__)

# Global state for tracking batch re-analysis progress
_batch_state = {
    "running": False,
    "progress": {
        "total": 0,
        "processed": 0,
        "successful": 0,
        "failed": 0,
        "current_url": None,
        "errors": []
    }
}
_batch_lock = threading.Lock()


def _reanalyze_form(record: dict) -> dict:
    """
    Re-analyze a single form and update the database.

    Args:
        record: Form record with source_url and id

    Returns:
        dict with keys: ok (bool), error (str, optional), record_id, fields, pages
    """
    source_url = record.get("source_url")
    record_id = record.get("id")

    if not source_url:
        return {"ok": False, "error": "No source_url", "record_id": record_id}

    try:
        # Analyze the PDF with full analysis (not minimal)
        opts = {
            "timeout": 30,
            "disable_size_guard": False,
            "max_pdf_mb": 120,
            "force_minimal": False,  # Full analysis to get field data
        }

        analyzed_record = analyzer.analyze_pdf(pdf_url=source_url, **opts)

        if not analyzed_record or not isinstance(analyzed_record, dict):
            return {"ok": False, "error": "Analyzer returned no record", "record_id": record_id}

        # Preserve the original committed status and other metadata
        analyzed_record["committed"] = record.get("committed", False)
        analyzed_record["id"] = record_id

        # Save the updated record
        storage.save(analyzed_record)

        return {
            "ok": True,
            "record_id": record_id,
            "fields": analyzed_record.get("field_count", 0),
            "pages": analyzed_record.get("pages", 0),
            "complexity": analyzed_record.get("complexity_score", 0.0)
        }

    except Exception as e:
        error_msg = str(e)
        return {"ok": False, "error": error_msg, "record_id": record_id}


def _run_batch_reanalysis(limit: int = None, batch_size: int = 10, delay: float = 0.5):
    """
    Background worker to re-analyze all committed forms with field_count=0.

    Args:
        limit: Max number of forms to process (None = all)
        batch_size: Number of forms to process before pausing
        delay: Delay between batches in seconds
    """
    global _batch_state

    try:
        with _batch_lock:
            _batch_state["running"] = True
            _batch_state["progress"] = {
                "total": 0,
                "processed": 0,
                "successful": 0,
                "failed": 0,
                "current_url": None,
                "errors": []
            }

        # Fetch all committed forms with field_count=0 or pages=0 (incomplete analysis)
        print("[BATCH] Fetching committed forms needing re-analysis (paginated)...")

        # Get all committed record IDs using direct database query to avoid memory issues
        unanalyzed = []

        try:
            # Use storage.list_all() but load in batches manually
            all_records = storage.list_all()

            # Filter for committed records with incomplete analysis
            for record in all_records:
                # Check if committed
                committed = record.get("committed")
                is_committed = (committed is True or str(committed).lower() in ("1", "true", "yes"))

                if not is_committed:
                    continue

                # Check if needs reanalysis (field_count=0 or pages=0)
                field_count = record.get("field_count", 0)
                pages = record.get("pages", 0)

                if field_count == 0 or pages == 0:
                    unanalyzed.append(record)

            print(f"[BATCH] Found {len(unanalyzed)} forms needing re-analysis")

        except Exception as e:
            print(f"[BATCH] Fatal error loading records: {e}")
            with _batch_lock:
                _batch_state["running"] = False
            return jsonify({"ok": False, "error": str(e)})

        if limit:
            unanalyzed = unanalyzed[:limit]

        total = len(unanalyzed)
        print(f"[BATCH] Found {total} forms to re-analyze")

        with _batch_lock:
            _batch_state["progress"]["total"] = total

        if total == 0:
            print("[BATCH] No forms need re-analysis")
            with _batch_lock:
                _batch_state["running"] = False
            return

        # Process in batches
        for i in range(0, total, batch_size):
            batch = unanalyzed[i:i + batch_size]

            for record in batch:
                source_url = record.get("source_url", "unknown")

                # Update current URL
                with _batch_lock:
                    _batch_state["progress"]["current_url"] = source_url

                # Re-analyze the form
                result = _reanalyze_form(record)

                # Update progress
                with _batch_lock:
                    _batch_state["progress"]["processed"] += 1

                    if result.get("ok"):
                        _batch_state["progress"]["successful"] += 1
                        print(f"[BATCH] {_batch_state['progress']['processed']}/{total} - SUCCESS: {source_url[:80]} - {result.get('fields')} fields")
                    else:
                        _batch_state["progress"]["failed"] += 1
                        error = result.get("error", "unknown error")
                        print(f"[BATCH] {_batch_state['progress']['processed']}/{total} - FAILED: {source_url[:80]} - {error}")

                        # Keep only last 50 errors
                        _batch_state["progress"]["errors"].append({
                            "url": source_url,
                            "error": error
                        })
                        if len(_batch_state["progress"]["errors"]) > 50:
                            _batch_state["progress"]["errors"] = _batch_state["progress"]["errors"][-50:]

            # Delay between batches
            if i + batch_size < total:
                time.sleep(delay)

        print(f"[BATCH] Complete! Processed: {total}, Successful: {_batch_state['progress']['successful']}, Failed: {_batch_state['progress']['failed']}")

    except Exception as e:
        print(f"[BATCH] Fatal error: {e}")
        import traceback
        traceback.print_exc()

        with _batch_lock:
            _batch_state["progress"]["errors"].append({
                "url": "FATAL",
                "error": str(e)
            })

    finally:
        with _batch_lock:
            _batch_state["running"] = False


@bp.post("/batch_reanalyze")
def batch_reanalyze():
    """
    Start batch re-analysis of committed forms with missing field data.

    Body (JSON):
    {
      "pin": "<ADMIN_PIN>",  // Admin PIN required (from env)
      "limit": 100,  // Optional: limit number of forms (for testing)
      "batch_size": 10,  // Optional: forms per batch (default: 10)
      "delay": 0.5  // Optional: delay between batches in seconds (default: 0.5)
    }

    Returns: { ok, message, total_forms }
    """
    j = request.get_json(force=True) or {}

    # Require admin PIN from env-driven config (matches the rest of /api).
    pin = j.get("pin", "")
    expected = current_app.config.get("ADMIN_PIN")
    if not expected:
        return jsonify({"ok": False, "error": "Server not configured (ADMIN_PIN)"}), 500
    if pin != expected:
        return jsonify({"ok": False, "error": "Invalid PIN"}), 403

    # Check if already running
    with _batch_lock:
        if _batch_state["running"]:
            return jsonify({
                "ok": False,
                "error": "Batch re-analysis already running",
                "progress": _batch_state["progress"]
            }), 409

    # Parse options
    limit = j.get("limit")
    if limit:
        limit = int(limit)

    batch_size = int(j.get("batch_size", 10))
    delay = float(j.get("delay", 0.5))

    # Start background thread
    thread = threading.Thread(
        target=_run_batch_reanalysis,
        args=(limit, batch_size, delay),
        daemon=True
    )
    thread.start()

    # Wait briefly for initial count
    time.sleep(1.0)

    with _batch_lock:
        total = _batch_state["progress"]["total"]

    return jsonify({
        "ok": True,
        "message": "Batch re-analysis started",
        "total_forms": total,
        "batch_size": batch_size,
        "delay": delay,
        "limit": limit
    })


@bp.get("/batch_reanalyze/status")
def batch_reanalyze_status():
    """
    Get status of batch re-analysis.

    Returns: { ok, running, progress }
    """
    with _batch_lock:
        return jsonify({
            "ok": True,
            "running": _batch_state["running"],
            "progress": _batch_state["progress"]
        })


@bp.post("/batch_reanalyze/stop")
def batch_reanalyze_stop():
    """
    Stop batch re-analysis (not implemented - process will complete current batch).

    Returns: { ok, message }
    """
    # Note: Actual stopping would require checking a flag in the loop
    # For now, just return a message
    return jsonify({
        "ok": True,
        "message": "Stop not implemented - batch will complete current processing"
    })
