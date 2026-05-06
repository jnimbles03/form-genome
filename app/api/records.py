# app/api/records.py
from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from flask import Blueprint, request, jsonify

from app.services import storage

# Unique name to avoid collisions in app.__init__
bp = Blueprint("records_api_v1", __name__)

# ---- Small storage helpers --------------------------------------------------

def _list_all() -> List[Dict[str, Any]]:
    """Read all rows from storage, compatible with older/newer storage modules."""
    try:
        rows = storage.list_all()  # ✅ FIXED: Changed from list() to list_all()
    except AttributeError:
        # Fallback: try list() if list_all() doesn't exist
        try:
            rows = storage.list()  # type: ignore[attr-defined]
        except AttributeError:
            # Last resort fallback
            rows = storage.list_records()  # type: ignore[attr-defined]
    return rows or []

def _save_record(row: Dict[str, Any]) -> None:
    """
    Persist one record in a storage-agnostic way.

    OPTIMIZED: When the record already has an id, route through
    storage.update_record() which performs a targeted SQL UPDATE against the
    primary-key index. This avoids the previous behavior where storage.save()
    was used to "upsert" an existing record, which (combined with the old
    cloud-sync-on-every-save) was the hot path for migration endpoints
    handling 500-row batches.

    Falls back to storage.save() when there is no id (genuinely new record),
    so true inserts still work.
    """
    rid = str(row.get("id") or row.get("_id") or "").strip()
    if rid:
        try:
            updated = storage.update_record(rid, row)
            if updated:
                return
        except Exception as e:
            # Fall through to save() on any error to preserve previous behavior.
            print(f"[RECORDS] update_record({rid}) failed, falling back to save(): {e}")
    storage.save(row)

def _by_ids(rows: List[Dict[str, Any]], idset: set[str]) -> List[Dict[str, Any]]:
    """Select rows whose id/sha1/_id matches any in idset."""
    out: List[Dict[str, Any]] = []
    for r in rows:
        rid = str(r.get("id") or r.get("sha1") or r.get("_id") or "")
        if rid and rid in idset:
            out.append(r)
    return out

def _is_committed(val: Any) -> bool:
    """Check if a value represents a committed state (True, 1, "1", "true", "yes")."""
    if val is True or val == 1:
        return True
    if isinstance(val, str):
        return val.lower() in ("1", "true", "yes")
    return False

def _has_data_quality_issue(record: Dict[str, Any]) -> bool:
    """
    Detect records with data quality issues that should be filtered out.

    Returns True if record has ANY of these issues:
    - Zero fields (unanalyzed, failed analysis, or not a form)

    Note: We can't check analysis_status or full_text because those fields
    are filtered out by UI_FIELDS optimization. We only check field counts.

    Rationale: Properly analyzed forms should ALWAYS have fields detected, even
    flat/scanned PDFs (Vision API detects fields). field_count==0 means:
    - Never analyzed (analysis_status=MISSING)
    - Analysis failed
    - Not actually a form
    All three cases should be filtered out.
    """
    # Check for zero fields - these are always data quality issues
    field_count = record.get("field_count", 0)

    # ANY form with 0 fields is suspicious and should be filtered
    if field_count == 0:
        return True

    return False

# ---- Routes -----------------------------------------------------------------

@bp.get("/records")
def list_records():
    """
    GET /api/records[?committed=1|?all=1&page=1&limit=100]
      - Returns uncommitted rows by default (for dashboard)
      - If committed=1, returns only committed rows (for genetics page)
      - If all=1, returns all rows
      - Pagination: page (default 1), limit (default 100, max 500)

    OPTIMIZED: Uses storage.list_filtered() with SQL-level filtering and pagination
    for dramatic performance improvement with large datasets.
    """
    committed_param = str(request.args.get("committed") or "").strip().lower()
    all_param = str(request.args.get("all") or "").strip().lower()

    # Pagination parameters
    try:
        page = max(1, int(request.args.get("page") or 1))
    except ValueError:
        page = 1

    try:
        requested_limit = int(request.args.get("limit") or 100)
        # Allow higher limits for chart generation (genetics page needs all records)
        # But cap at 10,000 to prevent memory issues
        limit = min(max(1, requested_limit), 10000)
    except ValueError:
        limit = 100

    # Calculate offset for SQL pagination
    offset = (page - 1) * limit

    # Determine committed filter
    if all_param in ("1", "true", "yes"):
        committed_filter = None  # All records
    elif committed_param in ("1", "true", "yes"):
        committed_filter = True  # Only committed
    else:
        committed_filter = False  # Only uncommitted (default)

    print(f"[RECORDS] committed_param={committed_param}, all_param={all_param}, page={page}, limit={limit}, offset={offset}")
    print(f"[RECORDS] Using SQL-level filtering: committed={committed_filter}")

    # Get total count (without pagination) for pagination metadata
    total = storage.count_filtered(committed=committed_filter)
    print(f"[RECORDS] Total matching records: {total}")

    # Get paginated records with SQL-level filtering
    rows = storage.list_filtered(
        committed=committed_filter,
        limit=limit,
        offset=offset
    )

    # Filter out records with data quality issues (unanalyzed/invalid records)
    # BUT ONLY for uncommitted records (dashboard). Committed records should always show.
    # Rationale: Committed records were explicitly approved by users, so we trust them.
    original_count = len(rows)
    if committed_filter == False:  # Only filter uncommitted records
        rows = [
            r for r in rows
            if not _has_data_quality_issue(r)
        ]
        filtered_count = original_count - len(rows)
        if filtered_count > 0:
            print(f"[RECORDS] Filtered out {filtered_count} uncommitted records with data quality issues")
    else:
        print(f"[RECORDS] Skipping data quality filter for committed records (user-approved)")

    total_pages = (total + limit - 1) // limit if total > 0 else 1
    print(f"[RECORDS] Returning page {page}/{total_pages}, showing {len(rows)} records (total: {total})")

    # Optimize payload: Return only fields needed for UI display (not full metadata)
    # This reduces payload size from ~3MB to ~300KB for 361 records
    UI_FIELDS = {
        "id", "form_name", "source_url", "pages", "complexity_score", "nigo_score",
        "signature_count", "field_count", "notarization_required", "attachments_required",
        "conditional_logic", "deadlines_present", "third_party_involved", "committed",
        "industry_vertical", "industry_subvertical", "entity_name", "action_type",
        "estimated_signer_time", "estimated_processing_time", "language_count",
        "text_fields", "checkboxes", "dropdowns", "data_validation_fields",
        "key_drivers", "special_requirements", "witnesses_required", "identification_required",
        "instructions_included", "click_to_agree", "form_dependencies", "payment_required",
        "payment_amount", "value_type", "form_value",
        "root_domain", "region", "same_domain", "llm_suggested_entity", "metadata_source",
        "hosting_entity", "form_owner_entity", "is_intermediary_model", "intermediary_type",
        "business_impact", "timestamp", "ts",
        # Vision-derived counts and flags. Without these, the dashboard
        # reads `null` for sig_count / att_count even though the records
        # are fully analyzed in the DB. signature_analysis is the dict
        # that holds the per-widget breakdown including signature_count.
        "signature_analysis", "vision_analyzed", "attachment_count",
        "attachment_list", "form_purpose", "confidence_tier",
        "conversion",
    }

    optimized_rows = [
        {k: v for k, v in r.items() if k in UI_FIELDS}
        for r in rows
    ]

    return jsonify({
        "records": optimized_rows,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages
        }
    })

@bp.get("/records/<string:record_id>")
def get_record(record_id: str):
    """
    GET /api/records/<id>

    Returns the full stored record for a single form, including all metadata
    fields and a computed conversion cost estimate.  Unlike GET /api/records
    (which strips to UI_FIELDS for performance), this endpoint returns
    everything stored in the JSON blob.
    """
    from app.services.analyzer import estimate_conversion_cost

    matches = storage.list_by_ids([record_id])
    if not matches:
        return jsonify({"ok": False, "error": "Record not found"}), 404

    record = matches[0]

    # Conversion cost estimate
    conversion = estimate_conversion_cost(record)

    # Action / difficulty summary (mirrors analyze endpoint)
    action_type = record.get("action_type", "")
    requires_action = action_type not in ("Disclosure (No Signature, No Info Collection)",)
    complexity_score = float(record.get("complexity_score") or 0)
    if complexity_score >= 70:
        complexity_tier = "very_complex"
    elif complexity_score >= 45:
        complexity_tier = "complex"
    elif complexity_score >= 20:
        complexity_tier = "moderate"
    else:
        complexity_tier = "simple"

    # Strip internal debug fields before returning
    _SKIP = {"full_text", "_vision_data", "_flags", "title_debug",
             "quality_signals"}
    metadata = {k: v for k, v in record.items() if k not in _SKIP}

    return jsonify({
        "ok": True,
        "id": record_id,

        # ── 3-question summary ───────────────────────────────────────
        "summary": {
            "requires_action": requires_action,
            "action_type": action_type,
            "difficulty": {
                "complexity_score": complexity_score,
                "complexity_tier": complexity_tier,
                "nigo_score": float(record.get("nigo_score") or 0),
                "key_drivers": record.get("key_drivers", []),
                "estimated_signer_time_min": record.get("estimated_signer_time"),
                "estimated_processing_time_min": record.get("estimated_processing_time"),
            },
            "conversion": conversion,
        },

        # ── Full metadata table ──────────────────────────────────────
        "metadata": metadata,
    })


@bp.get("/records/sample")
def list_records_sample():
    """Tiny helper for sanity checks."""
    rows = _list_all()
    return jsonify(rows[:5])

@bp.post("/commit_records")
def commit_records():
    """
    POST /api/commit_records
    Body: { "ids": ["sha1-or-id", ...] }

    Marks only the provided rows as committed=true.
    Skips duplicates (forms with the same source_url already committed).
    Returns {ok, committed_count, committed_ids, skipped_count, skipped_ids, skipped_reasons}

    OPTIMIZED: Uses storage.list_filtered() to only load necessary records.
    """
    try:
        data = request.get_json(force=True) or {}
    except Exception as e:
        print(f"[COMMIT] Failed to parse JSON: {e}")
        return jsonify({"ok": False, "error": f"Invalid JSON: {str(e)}"}), 400

    ids = data.get("ids") or []
    if not isinstance(ids, list):
        return jsonify({"ok": False, "error": "ids must be a list"}), 400

    idset = set(map(str, ids))
    if not idset:
        return jsonify({"ok": True, "committed_count": 0, "committed_ids": [],
                       "skipped_count": 0, "skipped_ids": [], "skipped_reasons": {}})

    # DEBUG: Log what we received
    print(f"[COMMIT] Received {len(ids)} IDs to commit: {ids[:5]}..." if len(ids) > 5 else f"[COMMIT] Received IDs: {ids}")

    try:
        # OPTIMIZATION: Only load committed records for duplicate detection
        committed_rows = storage.list_filtered(committed=True)
        print(f"[COMMIT] Loaded {len(committed_rows)} committed records for duplicate detection")
    except Exception as e:
        print(f"[COMMIT] Error loading committed records: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"Database error: {str(e)}"}), 500

    # Build map of committed source URLs for duplicate detection
    committed_urls = {}
    for r in committed_rows:
        url = (r.get("source_url") or "").strip()
        if url:
            rid = str(r.get("id") or r.get("sha1") or r.get("_id") or "")
            form_name = (r.get("form_name") or "unknown")[:50]
            # Store first committed record for this URL
            if url not in committed_urls:
                committed_urls[url] = {"id": rid, "form_name": form_name}

    print(f"[COMMIT] Found {len(committed_urls)} unique committed URLs in /genetics")

    try:
        # OPTIMIZATION: Only load the specific records we need to commit
        targets = storage.list_by_ids(list(idset))
        print(f"[COMMIT] Found {len(targets)} matching rows to commit")
    except Exception as e:
        print(f"[COMMIT] Error loading target records: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"Database error loading records: {str(e)}"}), 500

    committed_ids: List[str] = []
    skipped_ids: List[str] = []
    skipped_reasons: Dict[str, str] = {}

    for r in targets:
        rid = str(r.get("id") or r.get("sha1") or r.get("_id") or "")
        url = (r.get("source_url") or "").strip()
        form_name = (r.get("form_name") or "unknown")[:50]

        # Check if this URL is already committed
        if url and url in committed_urls:
            existing = committed_urls[url]
            reason = f"Duplicate of {existing['form_name']} (ID: {existing['id'][:8]}...)"
            skipped_ids.append(rid)
            skipped_reasons[rid] = reason
            print(f"[COMMIT] ⊘ Skipped duplicate: {form_name} - {reason}")
            continue

        # Mark committed & persist
        r = dict(r)
        r["committed"] = True
        _save_record(r)
        committed_ids.append(rid)
        print(f"[COMMIT] ✓ Committed record: {form_name} ({rid[:8]}...)")

        # Add to committed URLs map to catch duplicates within this batch
        if url:
            committed_urls[url] = {"id": rid, "form_name": form_name}

    print(f"[COMMIT] Successfully committed {len(committed_ids)} records, skipped {len(skipped_ids)} duplicates")

    # CRITICAL: Force immediate sync to Cloud Storage (only for SQLite mode)
    # PostgreSQL data is already persisted in Cloud SQL - no sync needed
    if not os.environ.get("CLOUD_SQL_CONNECTION_NAME"):
        try:
            from app.services import db_sync
            print("[COMMIT] Forcing immediate sync to Cloud Storage...")
            db_sync.upload_to_cloud(force=True)
            print("[COMMIT] ✓ Synced to Cloud Storage")
        except Exception as e:
            print(f"[COMMIT] ⚠️  Cloud sync failed: {e}")
    else:
        print("[COMMIT] Using PostgreSQL - data already persisted in Cloud SQL")

    return jsonify({
        "ok": True,
        "committed_count": len(committed_ids),
        "committed_ids": committed_ids,
        "skipped_count": len(skipped_ids),
        "skipped_ids": skipped_ids,
        "skipped_reasons": skipped_reasons
    })

@bp.post("/uncommit_all")
def uncommit_all():
    """
    POST /api/uncommit_all
    Body: { "pin": "..." }

    Clears the committed flag from ALL records.
    Useful for resetting the genetics view.

    OPTIMIZED: Only loads committed records instead of all records.
    """
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"ok": False, "msg": "Invalid JSON"}), 400

    # Simple PIN check (should match ADMIN_PIN)
    from flask import current_app
    import os
    pin = str(data.get("pin") or "")
    expected_pin = current_app.config.get("ADMIN_PIN") or os.environ.get("ADMIN_PIN") or ""

    if not expected_pin or pin != expected_pin:
        return jsonify({"ok": False, "msg": "Invalid or missing PIN"}), 403

    # OPTIMIZATION: Only load committed records
    rows = storage.list_filtered(committed=True)
    print(f"[UNCOMMIT] Found {len(rows)} committed records to uncommit")
    uncommitted_count = 0

    for r in rows:
        r = dict(r)
        r["committed"] = False
        _save_record(r)
        uncommitted_count += 1

    print(f"[UNCOMMIT] Cleared committed flag from {uncommitted_count} records")
    return jsonify({"ok": True, "uncommitted_count": uncommitted_count})

@bp.post("/migrate_business_impact")
def migrate_business_impact():
    """
    POST /api/migrate_business_impact
    Body: { "pin": "..." }

    Applies business_impact classification to all existing records.
    Uses batched updates for much faster performance.
    """
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"ok": False, "msg": "Invalid JSON"}), 400

    # Simple PIN check (should match ADMIN_PIN)
    from flask import current_app
    import os
    pin = str(data.get("pin") or "")
    expected_pin = current_app.config.get("ADMIN_PIN") or os.environ.get("ADMIN_PIN") or ""

    if not expected_pin or pin != expected_pin:
        return jsonify({"ok": False, "msg": "Invalid or missing PIN"}), 403

    # Import classification function
    from app.services.analyzer import classify_business_impact

    # Load all records
    print(f"[MIGRATE] Loading all records from database...")
    rows = _list_all()
    total_records = len(rows)
    print(f"[MIGRATE] Found {total_records} records to process")

    # Statistics
    stats = {
        "total": total_records,
        "updated": 0,
        "already_has_impact": 0,
        "revenue": 0,
        "expenses": 0,
        "regulatory": 0,
        "errors": 0
    }

    # Process and batch records
    BATCH_SIZE = 500  # Save 500 records at a time
    batch = []

    for i, record in enumerate(rows, 1):
        try:
            # Check if already has business_impact
            existing_impact = record.get("business_impact")

            # Classify business impact
            impact = classify_business_impact(record)

            # Track statistics
            if impact == "Revenue":
                stats["revenue"] += 1
            elif impact == "Expenses":
                stats["expenses"] += 1
            elif impact == "Regulatory":
                stats["regulatory"] += 1

            # Update record if needed
            if existing_impact == impact:
                stats["already_has_impact"] += 1
            else:
                stats["updated"] += 1
                record = dict(record)
                record["business_impact"] = impact
                batch.append(record)

            # Save batch when full or at end
            if len(batch) >= BATCH_SIZE or i == total_records:
                if batch:
                    storage.batch_save(batch)
                    print(f"[MIGRATE] Progress: {i}/{total_records} ({i*100//total_records}%) - Saved batch of {len(batch)} records")
                    batch = []

        except Exception as e:
            print(f"[MIGRATE] Error processing record {i}: {e}")
            stats["errors"] += 1
            continue

    print(f"[MIGRATE] Migration complete:")
    print(f"[MIGRATE]   Total: {stats['total']}")
    print(f"[MIGRATE]   Updated: {stats['updated']}")
    print(f"[MIGRATE]   Already classified: {stats['already_has_impact']}")
    print(f"[MIGRATE]   Revenue: {stats['revenue']} ({stats['revenue']*100//total_records if total_records > 0 else 0}%)")
    print(f"[MIGRATE]   Expenses: {stats['expenses']} ({stats['expenses']*100//total_records if total_records > 0 else 0}%)")
    print(f"[MIGRATE]   Regulatory: {stats['regulatory']} ({stats['regulatory']*100//total_records if total_records > 0 else 0}%)")
    print(f"[MIGRATE]   Errors: {stats['errors']}")

    return jsonify({
        "ok": True,
        "stats": stats
    })

@bp.post("/migrate_instructions")
def migrate_instructions():
    """
    POST /api/migrate_instructions
    Body: { "pin": "..." }

    Applies instructions_included detection to all existing records.
    Uses batched updates for fast performance.
    """
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"ok": False, "msg": "Invalid JSON"}), 400

    # Simple PIN check
    from flask import current_app
    import os
    pin = str(data.get("pin") or "")
    expected_pin = current_app.config.get("ADMIN_PIN") or os.environ.get("ADMIN_PIN") or ""

    if not expected_pin or pin != expected_pin:
        return jsonify({"ok": False, "msg": "Invalid or missing PIN"}), 403

    # Import detection function and PDF text extraction
    import re
    import io
    import requests
    from pypdf import PdfReader

    _INSTRUCTIONS_PAT = re.compile(
        r"\b(instructions?|how to (complete|fill out)|directions|guide|filing instructions|"
        r"completion guide|filling out this form|completing this form|form instructions|"
        r"general instructions|specific instructions|detailed instructions)\b",
        re.I
    )

    def _extract_pdf_text(pdf_url: str) -> str:
        """Download PDF and extract text for migration (fallback for records without full_text)"""
        try:
            from app.services import politeness as _politeness
            resp = requests.get(pdf_url, timeout=30, headers={"User-Agent": _politeness.USER_AGENT})
            if resp.status_code != 200:
                return ""
            reader = PdfReader(io.BytesIO(resp.content))
            text = ""
            for page in reader.pages:
                text += (page.extract_text() or "")
            return text
        except Exception as e:
            print(f"[MIGRATE_INSTRUCTIONS] Failed to extract text from {pdf_url}: {e}")
            return ""

    # Load all records
    print(f"[MIGRATE_INSTRUCTIONS] Loading all records from database...")
    rows = _list_all()
    total_records = len(rows)
    print(f"[MIGRATE_INSTRUCTIONS] Found {total_records} records to process")

    # Statistics
    stats = {
        "total": total_records,
        "updated": 0,
        "already_has_instructions": 0,
        "with_instructions": 0,
        "without_instructions": 0,
        "errors": 0,
        "pdf_downloads": 0,  # Track how many PDFs we had to download
        "used_cached_text": 0  # Track how many used existing full_text
    }

    # Use ThreadPoolExecutor for parallel PDF downloads
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    # Thread-safe counter for progress
    progress_lock = threading.Lock()
    progress_counter = [0]  # Use list for mutability in nested scope

    def process_record_download(record_tuple):
        """Download PDF and extract text for a single record"""
        idx, record = record_tuple
        source_url = record.get("source_url", "")
        if not source_url:
            return idx, record, ""

        full_text = _extract_pdf_text(source_url)

        # Update progress counter thread-safely
        with progress_lock:
            progress_counter[0] += 1
            current_progress = progress_counter[0]
            if current_progress % 50 == 0:  # Log every 50 downloads
                print(f"[MIGRATE_INSTRUCTIONS] Downloaded {current_progress} PDFs...")

        return idx, record, full_text

    # First pass: identify records that need PDF downloads
    records_to_download = []
    for i, record in enumerate(rows):
        if not record.get("full_text", ""):
            records_to_download.append((i, record))
        else:
            stats["used_cached_text"] += 1

    # Download PDFs in parallel (10 at a time)
    print(f"[MIGRATE_INSTRUCTIONS] Downloading {len(records_to_download)} PDFs in parallel (10 concurrent)...")
    downloaded_texts = {}

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(process_record_download, rec_tuple): rec_tuple for rec_tuple in records_to_download}

        for future in as_completed(futures):
            try:
                idx, record, full_text = future.result()
                if full_text:
                    downloaded_texts[idx] = full_text
                    stats["pdf_downloads"] += 1
            except Exception as e:
                print(f"[MIGRATE_INSTRUCTIONS] Error downloading PDF: {e}")
                stats["errors"] += 1

    print(f"[MIGRATE_INSTRUCTIONS] Downloaded {stats['pdf_downloads']} PDFs successfully")

    # Second pass: process all records with text (cached or downloaded)
    BATCH_SIZE = 500  # Save 500 records at a time
    batch = []

    for i, record in enumerate(rows, 1):
        try:
            # Get full text (either cached or downloaded)
            full_text = record.get("full_text", "") or downloaded_texts.get(i-1, "")

            # If we downloaded text, save it to record
            if not record.get("full_text", "") and full_text:
                record = dict(record)
                record["full_text"] = full_text

            # Check if record already has instructions_included
            existing_value = record.get("instructions_included")

            # Detect instructions in text
            has_instructions = bool(_INSTRUCTIONS_PAT.search(full_text))

            # Track statistics
            if has_instructions:
                stats["with_instructions"] += 1
            else:
                stats["without_instructions"] += 1

            # Update record if needed
            if existing_value == has_instructions:
                stats["already_has_instructions"] += 1
            else:
                stats["updated"] += 1
                record = dict(record)
                record["instructions_included"] = has_instructions
                batch.append(record)

            # Save batch when full or at end
            if len(batch) >= BATCH_SIZE or i == total_records:
                if batch:
                    storage.batch_save(batch)
                    print(f"[MIGRATE_INSTRUCTIONS] Progress: {i}/{total_records} ({i*100//total_records}%) - Saved batch of {len(batch)} records")
                    batch = []

        except Exception as e:
            print(f"[MIGRATE_INSTRUCTIONS] Error processing record {i}: {e}")
            stats["errors"] += 1
            continue

    print(f"[MIGRATE_INSTRUCTIONS] Migration complete:")
    print(f"[MIGRATE_INSTRUCTIONS]   Total: {stats['total']}")
    print(f"[MIGRATE_INSTRUCTIONS]   Updated: {stats['updated']}")
    print(f"[MIGRATE_INSTRUCTIONS]   Already correct: {stats['already_has_instructions']}")
    print(f"[MIGRATE_INSTRUCTIONS]   With instructions: {stats['with_instructions']} ({stats['with_instructions']*100//total_records if total_records > 0 else 0}%)")
    print(f"[MIGRATE_INSTRUCTIONS]   Without instructions: {stats['without_instructions']} ({stats['without_instructions']*100//total_records if total_records > 0 else 0}%)")
    print(f"[MIGRATE_INSTRUCTIONS]   Used cached text: {stats['used_cached_text']}")
    print(f"[MIGRATE_INSTRUCTIONS]   Downloaded PDFs: {stats['pdf_downloads']}")
    print(f"[MIGRATE_INSTRUCTIONS]   Errors: {stats['errors']}")

    return jsonify({
        "ok": True,
        "stats": stats
    })

@bp.post("/migrate_entity_names")
def migrate_entity_names():
    """
    POST /api/migrate_entity_names
    Body: { "pin": "..." }

    Re-derives entity names from crawl_root_url/source_url using domain_mappings.
    Fixes bad entity names from CDN-hosted PDFs (like HubSpot).
    Uses batched updates for much faster performance.
    """
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"ok": False, "msg": "Invalid JSON"}), 400

    # Simple PIN check (should match ADMIN_PIN)
    from flask import current_app
    import os
    pin = str(data.get("pin") or "")
    expected_pin = current_app.config.get("ADMIN_PIN") or os.environ.get("ADMIN_PIN") or ""

    if not expected_pin or pin != expected_pin:
        return jsonify({"ok": False, "msg": "Invalid or missing PIN"}), 403

    # Import domain_mappings
    from app.services import domain_mappings

    # Load all records
    print(f"[MIGRATE_ENTITY] Loading all records from database...")
    rows = _list_all()
    total_records = len(rows)
    print(f"[MIGRATE_ENTITY] Found {total_records} records to process")

    # Statistics
    stats = {
        "total": total_records,
        "updated": 0,
        "already_correct": 0,
        "errors": 0
    }

    # Process and batch records
    BATCH_SIZE = 500  # Save 500 records at a time
    batch = []

    for i, record in enumerate(rows, 1):
        try:
            # Get existing entity name
            old_entity = record.get("entity_name")

            # Derive new entity name from crawl_root_url or source_url
            url = record.get("crawl_root_url") or record.get("source_url")
            if not url:
                stats["errors"] += 1
                continue

            # Apply domain metadata (this will set correct entity_name)
            updated_record = dict(record)

            # For CDN-hosted forms, restore the original LLM suggestion if available
            # This ensures apply_domain_metadata() can use it as fallback
            if record.get("llm_suggested_entity"):
                updated_record["entity_name"] = record["llm_suggested_entity"]

            updated_record = domain_mappings.apply_domain_metadata(updated_record, url)
            new_entity = updated_record.get("entity_name")

            # Check if changed
            if old_entity == new_entity:
                stats["already_correct"] += 1
            else:
                stats["updated"] += 1
                batch.append(updated_record)
                print(f"[MIGRATE_ENTITY] {i}/{total_records}: '{old_entity}' → '{new_entity}'")

            # Save batch when full or at end
            if len(batch) >= BATCH_SIZE or i == total_records:
                if batch:
                    storage.batch_save(batch)
                    print(f"[MIGRATE_ENTITY] Progress: {i}/{total_records} ({i*100//total_records}%) - Saved batch of {len(batch)} records")
                    batch = []

        except Exception as e:
            print(f"[MIGRATE_ENTITY] Error processing record {i}: {e}")
            stats["errors"] += 1
            continue

    print(f"[MIGRATE_ENTITY] Migration complete:")
    print(f"[MIGRATE_ENTITY]   Total: {stats['total']}")
    print(f"[MIGRATE_ENTITY]   Updated: {stats['updated']}")
    print(f"[MIGRATE_ENTITY]   Already correct: {stats['already_correct']}")
    print(f"[MIGRATE_ENTITY]   Errors: {stats['errors']}")

    return jsonify({
        "ok": True,
        "stats": stats
    })


@bp.post("/migrate_region")
def migrate_region():
    """
    POST /api/migrate_region
    Body: { "pin": "..." }

    Applies region classification to all existing records based on domain TLD.
    Uses batched updates for fast performance.
    """
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"ok": False, "msg": "Invalid JSON"}), 400

    # Simple PIN check (should match ADMIN_PIN)
    from flask import current_app
    import os
    pin = str(data.get("pin") or "")
    expected_pin = current_app.config.get("ADMIN_PIN") or os.environ.get("ADMIN_PIN") or ""

    if not expected_pin or pin != expected_pin:
        return jsonify({"ok": False, "msg": "Invalid or missing PIN"}), 403

    # Import region detection function
    from app.services.domain_mappings import get_region_from_domain

    # Load all records
    print(f"[MIGRATE_REGION] Loading all records from database...")
    rows = _list_all()
    total_records = len(rows)
    print(f"[MIGRATE_REGION] Found {total_records} records to process")

    # Statistics
    stats = {
        "total": total_records,
        "updated": 0,
        "already_has_region": 0,
        "na": 0,
        "latam": 0,
        "emea": 0,
        "apj": 0,
        "errors": 0
    }

    # Process and batch records
    BATCH_SIZE = 500  # Save 500 records at a time
    batch = []

    for i, record in enumerate(rows, 1):
        try:
            # Get existing region
            old_region = record.get("region")

            # Get root_domain
            root_domain = record.get("root_domain")
            if not root_domain:
                stats["errors"] += 1
                continue

            # Calculate region from domain
            region = get_region_from_domain(root_domain)

            # Track statistics
            if region == "NA":
                stats["na"] += 1
            elif region == "LATAM":
                stats["latam"] += 1
            elif region == "EMEA":
                stats["emea"] += 1
            elif region == "APJ":
                stats["apj"] += 1

            # Update record if needed
            if old_region == region:
                stats["already_has_region"] += 1
            else:
                stats["updated"] += 1
                updated_record = dict(record)
                updated_record["region"] = region
                batch.append(updated_record)

                # Log updates for visibility
                if i <= 20 or i % 500 == 0:
                    entity = record.get("entity_name", "unknown")[:40]
                    print(f"[MIGRATE_REGION] {i}/{total_records}: {entity} ({root_domain}) → {region}")

            # Save batch when full or at end
            if len(batch) >= BATCH_SIZE or i == total_records:
                if batch:
                    storage.batch_save(batch)
                    print(f"[MIGRATE_REGION] Progress: {i}/{total_records} ({i*100//total_records}%) - Saved batch of {len(batch)} records")
                    batch = []

        except Exception as e:
            print(f"[MIGRATE_REGION] Error processing record {i}: {e}")
            stats["errors"] += 1
            continue

    print(f"[MIGRATE_REGION] Migration complete:")
    print(f"[MIGRATE_REGION]   Total: {stats['total']}")
    print(f"[MIGRATE_REGION]   Updated: {stats['updated']}")
    print(f"[MIGRATE_REGION]   Already had region: {stats['already_has_region']}")
    print(f"[MIGRATE_REGION]   NA: {stats['na']} ({stats['na']*100//total_records if total_records > 0 else 0}%)")
    print(f"[MIGRATE_REGION]   LATAM: {stats['latam']} ({stats['latam']*100//total_records if total_records > 0 else 0}%)")
    print(f"[MIGRATE_REGION]   EMEA: {stats['emea']} ({stats['emea']*100//total_records if total_records > 0 else 0}%)")
    print(f"[MIGRATE_REGION]   APJ: {stats['apj']} ({stats['apj']*100//total_records if total_records > 0 else 0}%)")
    print(f"[MIGRATE_REGION]   Errors: {stats['errors']}")

    return jsonify({
        "ok": True,
        "stats": stats
    })


@bp.post("/migrate_form_value")
def migrate_form_value():
    """
    POST /api/migrate_form_value
    Body: { "pin": "...", "force": true }

    Calculate and backfill value_type and form_value for all existing records.
    Uses smart calculation:
    - Direct fees <$500 → submission_fee
    - Account minimums → account_value (with revenue multipliers)
    - Reports/studies → none ($0)

    Set force=true to recalculate ALL records (even those with existing values).
    """
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"ok": False, "msg": "Invalid JSON"}), 400

    # Simple PIN check
    from flask import current_app
    import os
    pin = str(data.get("pin") or "")
    force_recalc = data.get("force", False)
    expected_pin = current_app.config.get("ADMIN_PIN") or os.environ.get("ADMIN_PIN") or ""

    if not expected_pin or pin != expected_pin:
        return jsonify({"ok": False, "msg": "Invalid or missing PIN"}), 403

    # Import analyzer for calculate_form_value function
    from app.services import analyzer

    # Load all records
    print(f"[MIGRATE_FORM_VALUE] Loading all records from database...")
    rows = _list_all()
    total_records = len(rows)
    print(f"[MIGRATE_FORM_VALUE] Found {total_records} records to process")

    # Statistics
    stats = {
        "total": total_records,
        "updated": 0,
        "already_has_value": 0,
        "no_payment": 0,
        "reports_cleared": 0,
        "errors": 0,
        "value_types": {
            "submission_fee": 0,
            "account_value": 0,
            "none": 0
        }
    }

    # Process and batch records
    BATCH_SIZE = 500
    batch = []

    for i, record in enumerate(rows, 1):
        try:
            # Skip if already has form_value (unless force=true)
            if not force_recalc and record.get("form_value") is not None:
                stats["already_has_value"] += 1
                continue

            payment_amount = record.get("payment_amount")

            # Calculate form value
            value_type, form_value = analyzer.calculate_form_value(
                payment_amount=payment_amount,
                form_name=record.get("form_name", ""),
                action_type=record.get("action_type", ""),
                industry_vertical=record.get("industry_vertical", ""),
                industry_subvertical=record.get("industry_subvertical", "")
            )

            # Update record
            updated_record = dict(record)
            updated_record["value_type"] = value_type
            updated_record["form_value"] = form_value

            # Track statistics
            stats["value_types"][value_type] += 1

            if value_type == "none" and payment_amount and payment_amount > 500:
                stats["reports_cleared"] += 1
                print(f"[MIGRATE_FORM_VALUE] {i}/{total_records}: Cleared ${payment_amount:,.0f} from '{record.get('form_name', '')[:50]}'")

            stats["updated"] += 1
            batch.append(updated_record)

            # Save batch when full or at end
            if len(batch) >= BATCH_SIZE or i == total_records:
                if batch:
                    storage.batch_save(batch)
                    print(f"[MIGRATE_FORM_VALUE] Progress: {i}/{total_records} ({i*100//total_records}%) - Saved batch of {len(batch)} records")
                    batch = []

        except Exception as e:
            print(f"[MIGRATE_FORM_VALUE] Error processing record {i}: {e}")
            stats["errors"] += 1
            continue

    print(f"[MIGRATE_FORM_VALUE] Migration complete:")
    print(f"[MIGRATE_FORM_VALUE]   Total: {stats['total']}")
    print(f"[MIGRATE_FORM_VALUE]   Updated: {stats['updated']}")
    print(f"[MIGRATE_FORM_VALUE]   Already had value: {stats['already_has_value']}")
    print(f"[MIGRATE_FORM_VALUE]   Reports cleared: {stats['reports_cleared']}")
    print(f"[MIGRATE_FORM_VALUE]   Errors: {stats['errors']}")
    print(f"[MIGRATE_FORM_VALUE]   Value Types:")
    print(f"[MIGRATE_FORM_VALUE]     submission_fee: {stats['value_types']['submission_fee']}")
    print(f"[MIGRATE_FORM_VALUE]     account_value: {stats['value_types']['account_value']}")
    print(f"[MIGRATE_FORM_VALUE]     none: {stats['value_types']['none']}")

    return jsonify({
        "ok": True,
        "stats": stats
    })


@bp.post("/update_record")
def update_record():
    """
    POST /api/update_record
    Body: { "id": "...", "updates": { ... } }

    Allows inline edits of individual columns from the UI before committing.
    We restrict which fields can be updated.

    OPTIMIZED: Uses storage.list_by_ids() to only load the specific record.
    """
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        data = {}

    rec_id = str(data.get("id") or "").strip()
    updates = data.get("updates") or {}

    if not rec_id or not updates:
        return jsonify({"ok": False, "error": "Missing id or updates"}), 400

    # Allow only safe fields to be updated
    ALLOW = {
        "form_name", "pages", "complexity_score", "nigo_score",
        "signature_count", "attachments_required", "notarization_required",
        "conditional_logic", "deadlines_present", "third_party_involved",
        "field_count", "text_fields", "checkboxes", "dropdowns",
        "data_validation_count", "language_count",
        "estimated_signer_time", "estimated_processing_time",
        "entity_name", "industry_vertical", "industry_subvertical",
        "form_dependencies", "identification_required",
        "click_to_agree", "witnesses_required", "key_drivers",
        "special_requirements"
    }
    safe_updates = {k: v for k, v in updates.items() if k in ALLOW}
    if not safe_updates:
        return jsonify({"ok": False, "error": "no editable fields"}), 400

    # OPTIMIZATION: Only load the specific record we need
    rows = storage.list_by_ids([rec_id])
    if not rows:
        return jsonify({"ok": False, "error": "record not found"}), 404

    found = dict(rows[0])

    # DEBUG: Log before update
    print(f"[UPDATE] Before update - committed={found.get('committed')} (type={type(found.get('committed'))})")

    # Apply updates
    found.update(safe_updates)

    # DEBUG: Log after update
    print(f"[UPDATE] After update - committed={found.get('committed')} (type={type(found.get('committed'))})")
    print(f"[UPDATE] Saving record {rec_id} with updates: {safe_updates}")

    _save_record(found)

    # DEBUG: Verify save
    saved_records = storage.list_by_ids([rec_id])
    if saved_records:
        saved_record = saved_records[0]
        print(f"[UPDATE] After save - committed={saved_record.get('committed')} (type={type(saved_record.get('committed'))})")
    else:
        print(f"[UPDATE] WARNING: Record not found after save!")

    return jsonify({"ok": True, "updated": 1})


@bp.post("/reanalyze")
def reanalyze_forms():
    """
    POST /api/reanalyze
    Body: { "pin": "...", "ids": [...], "committed": true/false, "limit": 100 }

    Re-analyze existing forms with latest analyzer improvements:
    - Stricter payment extraction (removes report/study amounts)
    - Updated form value calculation
    - Improved entity name extraction
    - All latest analyzer enhancements

    Options:
    - ids: Array of specific IDs to reanalyze
    - committed: Filter by committed status (true/false)
    - limit: Max number of forms to reanalyze in one call (default 10, max 100)

    This is expensive (downloads PDFs, runs LLM) - use carefully!
    """
    # PIN check
    from flask import current_app
    import os
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        data = {}

    pin = str(data.get("pin") or "")
    expected_pin = current_app.config.get("ADMIN_PIN") or os.environ.get("ADMIN_PIN") or ""

    if not expected_pin or pin != expected_pin:
        return jsonify({"ok": False, "msg": "Invalid or missing PIN"}), 403

    # Import analyzer
    from app.services import analyzer

    # Get filter parameters
    ids_filter = data.get("ids") or []
    committed_filter = data.get("committed")  # Can be True, False, or None (all)
    limit = min(int(data.get("limit") or 10), 100)  # Default 10, max 100 per call

    print(f"[REANALYZE] ids_filter={len(ids_filter) if isinstance(ids_filter, list) else 0}, committed={committed_filter}, limit={limit}")

    # Load records to reanalyze
    if ids_filter:
        # Specific IDs
        rows = storage.list_by_ids(ids_filter)
    elif committed_filter is not None:
        # Filter by committed status
        rows = storage.list_filtered(committed=committed_filter, limit=limit)
    else:
        # All records (dangerous!)
        rows = _list_all()
        rows = rows[:limit]  # Apply limit

    print(f"[REANALYZE] Found {len(rows)} records to reanalyze")

    # Statistics
    stats = {
        "total": len(rows),
        "success": 0,
        "failed": 0,
        "skipped_no_url": 0,
        "errors": []
    }

    # Reanalyze each form
    for i, record in enumerate(rows, 1):
        source_url = record.get("source_url")
        record_id = record.get("id")

        if not source_url:
            stats["skipped_no_url"] += 1
            print(f"[REANALYZE] {i}/{len(rows)}: Skipped (no source_url) - id={record_id}")
            continue

        try:
            print(f"[REANALYZE] {i}/{len(rows)}: Reanalyzing {source_url[:80]}...")

            # Re-analyze the PDF with latest logic
            new_analysis = analyzer.analyze_pdf(
                pdf_url=source_url,
                timeout=20,
                force_minimal=True,
                disable_size_guard=False,
                max_pdf_mb=120
            )

            if not new_analysis or not isinstance(new_analysis, dict):
                stats["failed"] += 1
                stats["errors"].append(f"ID {record_id}: Analyzer returned no data")
                print(f"[REANALYZE] Failed: No data returned")
                continue

            # Preserve important metadata from original record
            new_analysis["id"] = record_id  # Keep same ID
            new_analysis["committed"] = record.get("committed")  # Preserve commit status
            new_analysis["timestamp"] = record.get("timestamp")  # Preserve original timestamp

            # Save updated record (uses indexed update path when id exists)
            _save_record(new_analysis)

            stats["success"] += 1
            print(f"[REANALYZE] Success: Updated record {record_id}")

        except Exception as e:
            stats["failed"] += 1
            error_msg = f"ID {record_id}: {str(e)[:100]}"
            stats["errors"].append(error_msg)
            print(f"[REANALYZE] Error: {error_msg}")

    print(f"[REANALYZE] Complete: {stats['success']} success, {stats['failed']} failed, {stats['skipped_no_url']} skipped")

    return jsonify({
        "ok": True,
        "stats": stats
    })


@bp.post("/delete_broken_nsw")
def delete_broken_nsw():
    """
    Delete broken NSW Transport records from the database.

    These are records that were crawled but never analyzed (field_count=0),
    causing them to show all zeros in the UI. They are already filtered from
    API results, but this permanently removes them from the database to clean up.

    Requires PIN authentication (ADMIN_PIN environment variable).

    Body (JSON):
    {
      "pin": "1126"  // Required for authentication
    }

    Returns: { ok: true, stats: {...} }
    """
    import json

    # Check PIN
    j = request.get_json(force=True) or {}
    pin = str(j.get("pin") or "").strip()
    admin_pin = os.getenv("ADMIN_PIN", "").strip()

    if not admin_pin or pin != admin_pin:
        return jsonify({"ok": False, "error": "Invalid or missing PIN"}), 403

    print("[DELETE_NSW] Starting NSW Transport broken records cleanup...")

    # Find broken records by scanning all records
    conn = storage._get_conn()
    cur = conn.cursor()

    try:
        cur.execute("SELECT id, data FROM records;")

        broken_ids = []
        stats = {
            "total_scanned": 0,
            "nsw_records": 0,
            "broken_found": 0,
            "committed_broken": 0,
            "uncommitted_broken": 0,
            "deleted": 0
        }

        for row in cur.fetchall():
            record_id = row[0]
            stats["total_scanned"] += 1

            try:
                data = json.loads(row[1])
            except Exception as e:
                print(f"[DELETE_NSW] Failed to parse record {record_id}: {e}")
                continue

            # Check if this is a NSW Transport record
            source_url = data.get("source_url", "")
            field_count = data.get("field_count", 0)
            committed = data.get("committed", False)

            # NSW Transport URLs contain these patterns
            is_nsw = any(pattern in source_url.lower() for pattern in [
                "tfnswforms.transport.nsw.gov.au",
                "transport.nsw.gov.au"
            ])

            if is_nsw:
                stats["nsw_records"] += 1

            # Broken = NSW + field_count=0
            if is_nsw and field_count == 0:
                stats["broken_found"] += 1
                if committed:
                    stats["committed_broken"] += 1
                else:
                    stats["uncommitted_broken"] += 1

                broken_ids.append(record_id)

                form_name = data.get("form_name", "unknown")[:60]
                print(f"[DELETE_NSW] Found broken: {form_name} (fields={field_count})")

        # Close cursor before deletion
        cur.close()
        storage._put_conn(conn)

        # Delete broken records
        if broken_ids:
            print(f"[DELETE_NSW] Deleting {len(broken_ids)} broken NSW Transport records...")
            deleted_count = storage.delete_ids(broken_ids)
            stats["deleted"] = deleted_count
            print(f"[DELETE_NSW] ✓ Deleted {deleted_count} records")
        else:
            print("[DELETE_NSW] No broken NSW Transport records found")

        print(f"[DELETE_NSW] Cleanup complete:")
        print(f"[DELETE_NSW]   Total scanned: {stats['total_scanned']}")
        print(f"[DELETE_NSW]   NSW records: {stats['nsw_records']}")
        print(f"[DELETE_NSW]   Broken found: {stats['broken_found']}")
        print(f"[DELETE_NSW]   - Committed: {stats['committed_broken']}")
        print(f"[DELETE_NSW]   - Uncommitted: {stats['uncommitted_broken']}")
        print(f"[DELETE_NSW]   Deleted: {stats['deleted']}")

        return jsonify({
            "ok": True,
            "stats": stats
        })

    except Exception as e:
        print(f"[DELETE_NSW] Error during cleanup: {e}")
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500
    finally:
        # Ensure cursor and connection are released
        try:
            cur.close()
            storage._put_conn(conn)
        except:
            pass