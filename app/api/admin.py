# app/api/admin.py
from __future__ import annotations

import os
from flask import Blueprint, request, jsonify, current_app

# Storage contract expected:
#   storage.delete_all() -> int
#   storage.delete_ids(list[str]) -> int
#   storage.count_empty() -> int
#   storage.delete_empty() -> int
#   (ui/records endpoints are provided by other blueprints)
from app.services import storage

bp = Blueprint("admin", __name__)

# In-memory UI prefs as a simple fallback; replace with persistent store if you want.
_UI_PREFS: dict = {}

def _admin_pin_ok(pin: str) -> bool:
    expect = (current_app.config.get("ADMIN_PIN")
              or os.environ.get("ADMIN_PIN")
              or "")
    return bool(expect) and str(pin) == str(expect)

# ---------- DELETE RECORDS ----------
@bp.post("/delete_records")
def delete_records():
    """
    Body (JSON):
      { "pin": "1126" }              -> delete ALL
      { "pin": "1126", "ids":[...]}  -> delete only those ids
    """
    j = request.get_json(silent=True) or {}
    pin = str(j.get("pin") or "")
    if not _admin_pin_ok(pin):
        return jsonify({"ok": False, "msg": "Invalid PIN"}), 403
    try:
        ids = j.get("ids")
        if ids:
            deleted = storage.delete_ids(ids)
        else:
            deleted = storage.delete_all()
        return jsonify({"ok": True, "deleted": int(deleted)})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500

# ---------- MAINTENANCE ----------
@bp.get("/maintenance/count_empty")
def maintenance_count_empty():
    try:
        n = storage.count_empty()
        return jsonify({"empty": int(n)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@bp.post("/maintenance/cleanup_empty")
def maintenance_cleanup_empty():
    j = request.get_json(silent=True) or {}
    pin = str(j.get("pin") or "")
    if not _admin_pin_ok(pin):
        return jsonify({"ok": False, "msg": "Invalid PIN"}), 403
    try:
        n = storage.delete_empty()
        return jsonify({"ok": True, "deleted": int(n)})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500

@bp.post("/maintenance/clear_uncommitted")
def maintenance_clear_uncommitted():
    """
    Delete all uncommitted records. No PIN required since these are working drafts.
    Called automatically on dashboard load to provide a clean slate for each session.
    """
    try:
        n = storage.delete_uncommitted()
        print(f"[MAINTENANCE] Cleared {n} uncommitted records")
        return jsonify({"ok": True, "deleted": int(n)})
    except Exception as e:
        print(f"[MAINTENANCE] Error clearing uncommitted: {e}")
        return jsonify({"ok": False, "msg": str(e)}), 500

@bp.get("/maintenance/db_stats")
def maintenance_db_stats():
    """
    Get database statistics for debugging. No PIN required for read-only stats.
    """
    try:
        all_records = storage.list_all()
        committed_count = sum(1 for r in all_records if r.get("committed") is True)
        uncommitted_count = len(all_records) - committed_count

        print(f"[DB_STATS] Total: {len(all_records)}, Committed: {committed_count}, Uncommitted: {uncommitted_count}")

        return jsonify({
            "ok": True,
            "total": len(all_records),
            "committed": committed_count,
            "uncommitted": uncommitted_count
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500

# ---------- UPDATE RECORDS ----------
# NOTE: Single-record `/update_record` was previously also defined here as a
# PIN-gated handler that accepted arbitrary `updates` dicts. The records.py
# blueprint registers a safer field-allowlisted version of the same path,
# and registration order made the records.py version the live one. The
# admin version has been removed to eliminate the silent shadow and stop
# accepting arbitrary field writes. Authentication for the surviving route
# is enforced by the /api/* `before_request` hook in app/__init__.py.

@bp.post("/update_records")
def update_records():
    """
    Batch update multiple records.
    Body (JSON):
      { "pin": "1126", "ids": ["abc", "def"], "updates": {"committed": true, ...} }

    Automatically tracks user information when committing forms.
    """
    j = request.get_json(silent=True) or {}
    pin = str(j.get("pin") or "")
    if not _admin_pin_ok(pin):
        return jsonify({"ok": False, "msg": "Invalid PIN"}), 403

    ids = j.get("ids") or []
    updates = j.get("updates") or {}

    if not ids:
        return jsonify({"ok": False, "msg": "Missing ids"}), 400

    try:
        # If committing forms, add user tracking metadata
        if updates.get("committed") is True:
            from flask import session
            from datetime import datetime

            user_data = session.get("user", {})
            email = user_data.get("email", "anonymous")
            name = user_data.get("name", "")

            # Add user tracking fields
            updates["committed_by"] = email
            updates["committed_by_name"] = name
            updates["committed_at"] = datetime.utcnow().isoformat() + "Z"

            print(f"[COMMIT] User {email} ({name}) committing {len(ids)} forms")

        count = storage.update_many(ids, updates)
        return jsonify({"ok": True, "updated": count})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500

# ---------- UI PREFS (widths, provider/model) ----------
@bp.get("/admin/ui_prefs")
def ui_prefs_get():
    return jsonify(_UI_PREFS or {})

@bp.post("/admin/ui_prefs")
def ui_prefs_set():
    """
    Body:
      {
        "pin": "1126",
        "prefs": {
          "col_widths": { "form_name": "14%", ... },
          "llm_provider": "xai",
          "llm_model": "grok-4-latest"
        }
      }
    """
    j = request.get_json(silent=True) or {}
    pin = str(j.get("pin") or "")
    if not _admin_pin_ok(pin):
        return jsonify({"ok": False, "msg": "Invalid PIN"}), 403
    prefs = j.get("prefs") or {}
    try:
        _UI_PREFS.update(prefs)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ---------- SMS ALERTS ----------
@bp.post("/admin/test_sms")
def test_sms():
    """
    Test SMS alert configuration
    Body (JSON):
      { "pin": "1126" }
    """
    j = request.get_json(silent=True) or {}
    pin = str(j.get("pin") or "")
    if not _admin_pin_ok(pin):
        return jsonify({"ok": False, "msg": "Invalid PIN"}), 403

    try:
        from app.services.sms_alerts import send_test_alert
        send_test_alert()
        return jsonify({"ok": True, "msg": "Test SMS sent (check your phone)"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"SMS test failed: {str(e)}"}), 500

# ---------- NORMALIZE EXISTING RECORDS ----------
@bp.post("/admin/normalize_records")
def normalize_records():
    """
    Re-normalize entity names and vertical/subvertical for all existing records
    Body (JSON):
      { "pin": "1126" }
    """
    j = request.get_json(silent=True) or {}
    pin = str(j.get("pin") or "")
    if not _admin_pin_ok(pin):
        return jsonify({"ok": False, "msg": "Invalid PIN"}), 403

    try:
        from app.services import storage
        from app.services.analyzer import classify_industry
        from app.services.title_llm import normalize_org_name

        records = storage.list_all()
        updated_count = 0

        for record in records:
            updates = {}

            # Normalize entity name
            entity_name = record.get("entity_name")
            if entity_name:
                normalized = normalize_org_name(entity_name)
                if normalized != entity_name:
                    updates["entity_name"] = normalized

            # Re-classify industry
            source_url = record.get("source_url")
            if source_url:
                vertical, subvertical = classify_industry(entity_name, source_url)
                if vertical != record.get("industry_vertical") or subvertical != record.get("industry_subvertical"):
                    updates["industry_vertical"] = vertical
                    updates["industry_subvertical"] = subvertical

            # Update record if changes were made
            if updates:
                rec_id = record.get("id")
                if rec_id and storage.update_record(rec_id, updates):
                    updated_count += 1

        # Force sync to Cloud Storage
        from app.services import db_sync
        db_sync.upload_to_cloud(force=True)

        return jsonify({
            "ok": True,
            "msg": f"Normalized {updated_count} of {len(records)} records",
            "updated": updated_count,
            "total": len(records)
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Normalization failed: {str(e)}"}), 500

# ---------- LOGIN LOGS ----------
@bp.get("/admin/login_logs")
def get_login_logs():
    """
    Get recent login logs (requires admin PIN)
    Query params:
      ?pin=1126
      ?limit=100 (optional, default 100)
    """
    pin = request.args.get("pin", "")
    if not _admin_pin_ok(pin):
        return jsonify({"ok": False, "msg": "Invalid PIN"}), 403

    try:
        limit = int(request.args.get("limit", 100))
        logs = storage.get_login_logs(limit=limit)
        return jsonify({"ok": True, "logs": logs, "count": len(logs)})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500

@bp.get("/admin/login_stats")
def get_login_stats():
    """
    Get login statistics (requires admin PIN)
    Query params:
      ?pin=1126
    """
    pin = request.args.get("pin", "")
    if not _admin_pin_ok(pin):
        return jsonify({"ok": False, "msg": "Invalid PIN"}), 403

    try:
        stats = storage.get_login_stats()
        return jsonify({"ok": True, "stats": stats})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500

@bp.get("/admin/sheets_status")
def get_sheets_status():
    """
    Get Google Sheets logging status (requires admin PIN)
    Query params:
      ?pin=1126
    """
    pin = request.args.get("pin", "")
    if not _admin_pin_ok(pin):
        return jsonify({"ok": False, "msg": "Invalid PIN"}), 403

    try:
        from app.services import sheets_logger
        status = sheets_logger.get_status()
        return jsonify({"ok": True, "status": status})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ---------- DOMAIN MAPPINGS ----------
@bp.get("/domain_mappings")
def get_domain_mappings():
    """
    Get all domain mappings (no auth required for viewing).
    Returns: { ok: true, mappings: { "pa.gov": {...}, ... } }
    """
    try:
        from app.services import domain_mappings
        mappings = domain_mappings.get_all_mappings()
        return jsonify({"ok": True, "mappings": mappings})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@bp.post("/domain_mappings")
def add_domain_mapping():
    """
    Add or update a domain mapping (requires admin PIN).
    Body (JSON):
      {
        "pin": "1126",
        "domain": "example.com",
        "entity_name": "Example Corporation",
        "industry_vertical": "Technology",
        "industry_subvertical": "Software"
      }
    """
    j = request.get_json(silent=True) or {}
    pin = str(j.get("pin") or "")
    if not _admin_pin_ok(pin):
        return jsonify({"ok": False, "msg": "Invalid PIN"}), 403

    domain = j.get("domain", "").strip()
    entity_name = j.get("entity_name", "").strip()
    vertical = j.get("industry_vertical", "").strip()
    subvertical = j.get("industry_subvertical", "").strip()

    if not domain or not entity_name:
        return jsonify({"ok": False, "msg": "Missing domain or entity_name"}), 400

    try:
        from app.services import domain_mappings
        success = domain_mappings.add_domain_mapping(domain, entity_name, vertical, subvertical)
        if success:
            return jsonify({"ok": True, "msg": f"Domain mapping added for {domain}"})
        else:
            return jsonify({"ok": False, "msg": "Failed to add mapping"}), 500
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@bp.delete("/domain_mappings")
def delete_domain_mapping():
    """
    Delete a domain mapping (requires admin PIN).
    Query params:
      ?pin=1126&domain=example.com
    """
    pin = request.args.get("pin", "")
    if not _admin_pin_ok(pin):
        return jsonify({"ok": False, "msg": "Invalid PIN"}), 403

    domain = request.args.get("domain", "").strip()
    if not domain:
        return jsonify({"ok": False, "msg": "Missing domain"}), 400

    try:
        from app.services import domain_mappings
        success = domain_mappings.remove_domain_mapping(domain)
        if success:
            return jsonify({"ok": True, "msg": f"Domain mapping removed for {domain}"})
        else:
            return jsonify({"ok": False, "msg": "Domain not found"}), 404
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@bp.get("/domain_stats")
def get_domain_stats():
    """
    Get statistics on domains in current records (no auth required).
    Returns: { ok: true, domains: { "pa.gov": 82, "opm.gov": 5, ... } }
    """
    try:
        from app.services import domain_mappings
        all_records = storage.list_all()
        domains = domain_mappings.get_domains_for_records(all_records)
        return jsonify({"ok": True, "domains": domains})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ---------- ANALYSIS ACTIVITY LOGS ----------
@bp.get("/admin/analysis_logs")
def get_analysis_activity_logs():
    """
    Get recent analysis activity logs (requires admin PIN)
    Query params:
      ?pin=1126
      ?limit=100 (optional, default 100)
    """
    pin = request.args.get("pin", "")
    if not _admin_pin_ok(pin):
        return jsonify({"ok": False, "msg": "Invalid PIN"}), 403

    try:
        limit = int(request.args.get("limit", 100))
        logs = storage.get_analysis_logs(limit=limit)
        return jsonify({"ok": True, "logs": logs, "count": len(logs)})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500

@bp.get("/admin/analysis_stats")
def get_analysis_activity_stats():
    """
    Get analysis activity statistics (requires admin PIN)
    Query params:
      ?pin=1126
    """
    pin = request.args.get("pin", "")
    if not _admin_pin_ok(pin):
        return jsonify({"ok": False, "msg": "Invalid PIN"}), 403

    try:
        stats = storage.get_analysis_stats()
        return jsonify({"ok": True, "stats": stats})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500

@bp.get("/admin/forms_for_review")
def get_forms_for_review():
    """
    Get forms that need review (non-actionable forms)
    Query params:
      ?pin=1126
    Returns forms where is_actionable=False for manual review/deletion
    """
    pin = request.args.get("pin", "")
    if not _admin_pin_ok(pin):
        return jsonify({"ok": False, "msg": "Invalid PIN"}), 403

    try:
        all_records = storage.list_all()
        # Filter for non-actionable forms
        review_forms = [r for r in all_records if not r.get("is_actionable", True)]

        # Also include low-complexity forms (< 10) as potential candidates
        low_complexity = [r for r in all_records
                         if r.get("is_actionable", True)
                         and (r.get("complexity_score", 0) < 10)
                         and (r.get("pages", 0) < 2)]

        return jsonify({
            "ok": True,
            "non_actionable": review_forms,
            "low_complexity": low_complexity,
            "total_review_count": len(review_forms) + len(low_complexity)
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500

# ---------- DATABASE SYNC ----------
@bp.post("/admin/sync-db")
def sync_database():
    """
    Force immediate database sync to Cloud Storage (for Cloud Scheduler).
    Query params:
      ?pin=1126
    Returns: { ok: true, synced: true, record_count: 123 }
    """
    pin = request.args.get("pin", "")
    if not _admin_pin_ok(pin):
        return jsonify({"ok": False, "msg": "Invalid PIN"}), 403

    try:
        from app.services import db_sync

        # Force immediate sync
        db_sync.upload_to_cloud(force=True)

        # Get record count for verification
        all_records = storage.list_all()
        record_count = len(all_records)

        return jsonify({
            "ok": True,
            "synced": True,
            "record_count": record_count,
            "msg": f"Database synced successfully ({record_count} records)"
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Sync failed: {str(e)}"}), 500


# ---------- RESTORE COMMITTED STATUS ----------
@bp.post("/admin/restore_committed")
def restore_committed_status():
    """
    Restore committed=True status on forms that were incorrectly uncommitted
    by the batch re-analysis bug.

    Finds forms that:
    - Were successfully re-analyzed (have field_count > 0)
    - Are currently uncommitted
    - Should be committed (were originally committed before re-analysis)

    Body (JSON):
    {
      "pin": "1126",  // Required for authentication
      "dry_run": false  // Optional: if true, only reports what would be restored
    }

    Returns: { ok: true, stats: {...} }
    """
    j = request.get_json(silent=True) or {}
    pin = str(j.get("pin") or "")
    if not _admin_pin_ok(pin):
        return jsonify({"ok": False, "msg": "Invalid PIN"}), 403

    dry_run = j.get("dry_run", False)

    try:
        import os
        import json

        checkpoint_file = "reanalysis_checkpoint.json"

        # Check if checkpoint file exists in environment
        # On Cloud Run, we won't have it, so we'll restore all uncommitted forms with data
        processed_ids = set()

        # Try to get checkpoint from request body (user can upload it)
        checkpoint_data = j.get("checkpoint")
        if checkpoint_data:
            processed_ids = set(checkpoint_data.get("processed_ids", []))
            print(f"[RESTORE] Using checkpoint data from request: {len(processed_ids)} processed IDs")

        # Get all records
        all_records = storage.list_all()
        print(f"[RESTORE] Loaded {len(all_records)} total records")

        # Find records that need restoration
        to_restore = []

        for record in all_records:
            record_id = str(record.get("id"))

            # If we have checkpoint data, only restore forms that were processed
            if processed_ids and record_id not in processed_ids:
                continue

            # Check if currently uncommitted
            committed = record.get("committed")
            is_committed = (committed is True or str(committed).lower() in ("1", "true", "yes"))
            if is_committed:
                continue  # Already committed, skip

            # Check if has data (successful analysis)
            field_count = record.get("field_count", 0)
            pages = record.get("pages", 0)

            # Only restore if it has meaningful data
            if field_count > 0 or pages > 0:
                to_restore.append({
                    "id": record_id,
                    "form_name": record.get("form_name", "Unknown")[:60],
                    "field_count": field_count,
                    "pages": pages
                })

        print(f"[RESTORE] Found {len(to_restore)} forms to restore")

        if not to_restore:
            return jsonify({
                "ok": True,
                "msg": "No forms need restoration",
                "stats": {
                    "total_records": len(all_records),
                    "to_restore": 0,
                    "restored": 0
                }
            })

        # If dry_run, just return what would be restored
        if dry_run:
            return jsonify({
                "ok": True,
                "msg": f"DRY RUN: Would restore {len(to_restore)} forms",
                "stats": {
                    "total_records": len(all_records),
                    "to_restore": len(to_restore),
                    "restored": 0
                },
                "sample_forms": to_restore[:10]
            })

        # Restore committed status
        restored_count = 0
        failed_count = 0

        for form in to_restore:
            try:
                updated = storage.update_one(form["id"], {"committed": True})
                if updated > 0:
                    restored_count += 1
                else:
                    failed_count += 1
            except Exception as e:
                print(f"[RESTORE] Error restoring {form['id']}: {e}")
                failed_count += 1

        return jsonify({
            "ok": True,
            "msg": f"Restored committed=True on {restored_count} forms",
            "stats": {
                "total_records": len(all_records),
                "to_restore": len(to_restore),
                "restored": restored_count,
                "failed": failed_count
            },
            "sample_forms": to_restore[:10]
        })

    except Exception as e:
        print(f"[RESTORE] Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "msg": str(e)}), 500


# ---------- USER ACTIVITY REPORTS ----------
@bp.get("/admin/user_activity_report")
def user_activity_report():
    """
    Get detailed user activity report showing:
    - Total forms committed by each user
    - Forms analyzed by each user (from activity logs)
    - Recent activity
    - Top contributors

    Query params:
      ?pin=1126
      ?limit=50 (optional, default 50 top users)

    Returns:
      {
        "ok": true,
        "users": [
          {
            "email": "user@example.com",
            "name": "User Name",
            "forms_committed": 123,
            "forms_analyzed": 456,
            "last_activity": "2025-01-15T10:30:00Z",
            "last_committed": "2025-01-14T15:00:00Z"
          },
          ...
        ],
        "totals": {
          "total_committed": 1234,
          "total_analyzed": 5678,
          "unique_users": 25
        }
      }
    """
    pin = request.args.get("pin", "")
    if not _admin_pin_ok(pin):
        return jsonify({"ok": False, "msg": "Invalid PIN"}), 403

    try:
        limit = int(request.args.get("limit", 50))

        # Get all committed forms with user tracking
        all_records = storage.list_all()

        # Count forms committed by each user
        user_commits = {}
        for record in all_records:
            if record.get("committed") is True:
                email = record.get("committed_by", "unknown")
                name = record.get("committed_by_name", "")
                committed_at = record.get("committed_at")

                if email not in user_commits:
                    user_commits[email] = {
                        "email": email,
                        "name": name,
                        "forms_committed": 0,
                        "last_committed": None
                    }

                user_commits[email]["forms_committed"] += 1

                # Track most recent commit
                if committed_at:
                    if (not user_commits[email]["last_committed"] or
                        committed_at > user_commits[email]["last_committed"]):
                        user_commits[email]["last_committed"] = committed_at

        # Get analysis activity stats
        analysis_stats = storage.get_analysis_stats()
        top_analyzers = analysis_stats.get("top_users", [])

        # Create analyzer lookup
        analyzer_map = {}
        for analyzer in top_analyzers:
            analyzer_map[analyzer["email"]] = analyzer["count"]

        # Merge commit and analysis data
        user_activity = []
        for email, data in user_commits.items():
            user_activity.append({
                "email": email,
                "name": data["name"],
                "forms_committed": data["forms_committed"],
                "forms_analyzed": analyzer_map.get(email, 0),
                "last_committed": data["last_committed"]
            })

        # Add users who only analyzed but never committed
        for analyzer in top_analyzers:
            if analyzer["email"] not in user_commits:
                user_activity.append({
                    "email": analyzer["email"],
                    "name": "",
                    "forms_committed": 0,
                    "forms_analyzed": analyzer["count"],
                    "last_committed": None
                })

        # Sort by total contribution (commits + analyses)
        user_activity.sort(
            key=lambda x: x["forms_committed"] + x["forms_analyzed"],
            reverse=True
        )

        # Limit results
        user_activity = user_activity[:limit]

        # Calculate totals
        total_committed = sum(u["forms_committed"] for u in user_activity)
        total_analyzed = sum(u["forms_analyzed"] for u in user_activity)

        return jsonify({
            "ok": True,
            "users": user_activity,
            "totals": {
                "total_committed": total_committed,
                "total_analyzed": total_analyzed,
                "unique_users": len(user_activity),
                "total_records": len(all_records),
                "committed_records": sum(1 for r in all_records if r.get("committed") is True)
            }
        })

    except Exception as e:
        print(f"[USER_ACTIVITY] Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "msg": str(e)}), 500