"""
API endpoint for batch title normalization using LLM.
POST /api/normalize_titles - Normalize all form titles in the database
"""
from flask import Blueprint, request, jsonify
import os
from app.services import storage
from app.services.title_normalizer import llm_normalize, basic_normalize

bp = Blueprint("normalize_titles_api", __name__)


@bp.post("/normalize_titles")
def normalize_titles():
    """
    POST /api/normalize_titles
    Body: {
        "pin": "...",
        "use_llm": true,  // Use LLM (slow, smart) or basic normalization (fast, simple)
        "limit": 100,     // Max records to normalize per request (default 100)
        "dry_run": false  // If true, preview changes without saving
    }

    Normalizes all form titles in the database using either:
    - Basic normalization: Fast, regex-based cleanup
    - LLM normalization: Intelligent cleanup using GPT/Claude (requires API key)

    Returns: {
        ok: true,
        stats: {
            total: number of records processed,
            updated: number of titles changed,
            unchanged: number of titles that stayed the same,
            errors: number of errors
        },
        examples: [...] // First 10 changes for preview
    }
    """
    # PIN check
    from flask import current_app
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        data = {}

    pin = str(data.get("pin") or "")
    expected_pin = current_app.config.get("ADMIN_PIN") or os.environ.get("ADMIN_PIN") or ""

    if not expected_pin or pin != expected_pin:
        return jsonify({"ok": False, "msg": "Invalid or missing PIN"}), 403

    # Parameters
    use_llm = data.get("use_llm", True)
    limit = min(int(data.get("limit") or 100), 1000)  # Max 1000 per request
    dry_run = data.get("dry_run", False)

    print(f"[NORMALIZE_TITLES] Starting normalization (use_llm={use_llm}, limit={limit}, dry_run={dry_run})")

    # Load records
    all_records = storage.list_all()
    total_records = len(all_records)
    print(f"[NORMALIZE_TITLES] Found {total_records} records")

    # Limit records to process
    records_to_process = all_records[:limit]

    # Statistics
    stats = {
        "total": len(records_to_process),
        "updated": 0,
        "unchanged": 0,
        "errors": 0,
        "llm_used": use_llm
    }

    examples = []
    batch = []

    for i, record in enumerate(records_to_process, 1):
        try:
            original_title = record.get("form_name", "")
            if not original_title:
                stats["unchanged"] += 1
                continue

            # Normalize title
            if use_llm:
                normalized_title = llm_normalize(original_title)
            else:
                normalized_title = basic_normalize(original_title)

            # Check if changed
            if original_title == normalized_title:
                stats["unchanged"] += 1
            else:
                stats["updated"] += 1

                # Save example (first 10)
                if len(examples) < 10:
                    examples.append({
                        "before": original_title,
                        "after": normalized_title,
                        "saved_chars": len(original_title) - len(normalized_title)
                    })

                # Update record
                if not dry_run:
                    record = dict(record)
                    record["form_name"] = normalized_title
                    batch.append(record)

                    # Batch save every 50 records
                    if len(batch) >= 50:
                        storage.batch_save(batch)
                        print(f"[NORMALIZE_TITLES] Saved batch of {len(batch)} records ({i}/{len(records_to_process)})")
                        batch = []

            # Progress logging
            if i % 10 == 0:
                print(f"[NORMALIZE_TITLES] Progress: {i}/{len(records_to_process)} ({i*100//len(records_to_process)}%)")

        except Exception as e:
            print(f"[NORMALIZE_TITLES] Error processing record {i}: {e}")
            stats["errors"] += 1
            continue

    # Save remaining batch
    if not dry_run and batch:
        storage.batch_save(batch)
        print(f"[NORMALIZE_TITLES] Saved final batch of {len(batch)} records")

    print(f"[NORMALIZE_TITLES] Complete: {stats['updated']} updated, {stats['unchanged']} unchanged, {stats['errors']} errors")

    return jsonify({
        "ok": True,
        "stats": stats,
        "examples": examples,
        "dry_run": dry_run,
        "message": f"{'Preview' if dry_run else 'Normalized'} {stats['total']} titles ({stats['updated']} changed)"
    })
