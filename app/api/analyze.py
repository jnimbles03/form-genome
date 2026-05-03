# app/api/analyze.py
import logging
from flask import Blueprint, request, jsonify, current_app
from app.services import analyzer, storage, language_dedup
import base64, binascii

logger = logging.getLogger(__name__)

bp = Blueprint("analyze", __name__)

@bp.post("/analyze_pdf")
def analyze_pdf_route():
    """Wrapper with rate limiting.

    Calls analyze_pdf() exactly once. When a Flask-Limiter extension is
    available, the call is wrapped through limiter.limit(...) so the rate
    limit decorator records the request and short-circuits with 429 if the
    bucket is exhausted; otherwise it falls through to a direct call.
    The previous implementation invoked analyze_pdf() twice (once via the
    limiter wrapper, once unconditionally on the next line) which both
    duplicated work and effectively bypassed the rate limiter.
    """
    limiter = current_app.extensions.get('limiter')
    if limiter:
        # Apply rate limit to expensive PDF analysis - allows batch processing.
        # The wrapper returned by limiter.limit(...)(analyze_pdf) returns the
        # underlying handler's response (or raises 429), so we return that
        # directly instead of calling analyze_pdf() a second time.
        return limiter.limit("100 per minute")(analyze_pdf)()
    return analyze_pdf()

def analyze_pdf():
    """
    Accepts either:
      { "pdf_url": "https://..." }
    or
      { "pdf_bytes_b64": "<base64>", "filename": "file.pdf" }
    Optional: timeout, disable_size_guard, max_pdf_mb, force_minimal

    Returns a small debug echo so the UI/you can see why a row failed:
      {"ok":true,"id": "... or null ...","status":"ok|failed","pages":N,
       "complexity":X,"parse_error":"...", "source_url":"..."}
    """
    j = request.get_json(force=True) or {}
    pdf_url = j.get("pdf_url")
    pdf_b64 = j.get("pdf_bytes_b64")
    filename = j.get("filename")

    # --- Input Validation ---
    if pdf_url:
        # Validate URL
        if not isinstance(pdf_url, str) or len(pdf_url) > 2048:
            return jsonify({"ok": False, "error": "Invalid URL length"}), 400
        if not pdf_url.startswith(('http://', 'https://')):
            return jsonify({"ok": False, "error": "Invalid URL protocol"}), 400

    if pdf_b64:
        # Validate base64 data
        if not isinstance(pdf_b64, str) or len(pdf_b64) > 20 * 1024 * 1024:  # ~15MB base64
            return jsonify({"ok": False, "error": "PDF data too large"}), 400

    if not pdf_url and not pdf_b64:
        return jsonify({"ok": False, "error": "Either pdf_url or pdf_bytes_b64 required"}), 400

    opts = {
        "timeout": int(j.get("timeout") or 20),
        "disable_size_guard": bool(j.get("disable_size_guard") or False),
        "max_pdf_mb": int(j.get("max_pdf_mb") or 120),
        "force_minimal": bool(j.get("force_minimal") if "force_minimal" in j else True),
        "filename": filename,
        "skip_vision": bool(j.get("skip_vision") or False),  # Fast re-analysis mode
        "skip_llm_title": bool(j.get("skip_llm_title") or False),  # Fast re-analysis mode
    }

    try:
        if not pdf_url and not pdf_b64:
            return jsonify({"ok": False, "error": "Provide pdf_url or pdf_bytes_b64"}), 400

        if pdf_url:
            record = analyzer.analyze_pdf(pdf_url=pdf_url, **opts)
        else:
            try:
                raw = base64.b64decode(pdf_b64, validate=True)
            except (binascii.Error, ValueError):
                return jsonify({"ok": False, "error": "Invalid base64 content"}), 400
            record = analyzer.analyze_pdf(pdf_bytes=raw, **opts)

        if not record or not isinstance(record, dict):
            return jsonify({"ok": False, "error": "Analyzer returned no record"}), 500

        # Calculate quality score (3-tier system: high/medium/low confidence)
        confidence_tier, confidence_score, quality_signals = analyzer.calculate_quality_score(record)

        # Check if quality filtering is enabled (env var or request param)
        import os
        enable_filter = os.getenv("ENABLE_QUALITY_FILTER", "false").lower() == "true"
        bypass_filter = bool(j.get("bypass_quality_filter", False))  # Admin override

        # PHASE 1: Add quality metadata but don't reject yet (unless explicitly enabled)
        # PHASE 2: Enable rejection for low confidence docs
        should_save = True
        rejection_reason = None

        if enable_filter and not bypass_filter:
            if confidence_tier == "low":
                # Reject low-confidence documents
                should_save = False
                rejection_reason = f"Low quality score ({confidence_score:.2f}): {'; '.join(quality_signals[:3])}"
                print(f"[QUALITY FILTER] Rejecting: {record.get('form_name', 'Unknown')[:50]} - {rejection_reason}")

                return jsonify({
                    "ok": True,
                    "saved": False,
                    "rejected": True,
                    "reason": rejection_reason,
                    "quality": {
                        "confidence_tier": confidence_tier,
                        "confidence_score": confidence_score,
                        "signals": quality_signals
                    }
                })

        # Legacy check (keep for backward compatibility)
        has_url = bool((record.get("source_url") or "").strip())
        pages_val = record.get("pages")
        complexity_val = record.get("complexity_score")
        has_signal = (int(pages_val if pages_val is not None else 0) > 0) or (float(complexity_val if complexity_val is not None else 0) > 0.0)
        should_save = should_save and (has_url or has_signal)

        # Determine auto-commit based on confidence tier
        # HIGH confidence: auto-commit (if not existing record)
        # MEDIUM/LOW confidence: require manual review
        force_commit = bool(j.get("force_commit", False))  # Admin override
        auto_commit = (confidence_tier == "high") or force_commit

        rid = None
        merged = False
        skip_dedup = j.get("skip_dedup", False)
        existing_record = None  # referenced in the response payload below

        if should_save:
            # Targeted lookups instead of storage.list_all() on the hot path.
            # Previously this block called list_all() (full table scan + JSON
            # parse of every row) for *both* the existing-record check and the
            # language-dedup candidate list. At 100k records that was an
            # O(N) per-analyze hot path. We now:
            #   1. Look up any existing record by source_url via an indexed
            #      query (storage.get_by_source_url).
            #   2. Build a small candidate list for language-dedup using the
            #      base form pattern when available (storage.find_by_base_form_pattern).
            existing_record = None
            candidate_records: list = []
            source_url = record.get("source_url")

            if source_url:
                try:
                    existing_record = storage.get_by_source_url(source_url)
                except Exception as e:
                    logger.warning("get_by_source_url failed: %s", e)

            if not skip_dedup and source_url:
                try:
                    pattern = language_dedup.get_base_form_pattern(source_url)
                except Exception as e:
                    logger.warning("get_base_form_pattern failed: %s", e)
                    pattern = None

                if pattern:
                    try:
                        candidate_records = storage.find_by_base_form_pattern(pattern) or []
                    except Exception as e:
                        # Helper may not exist on older storage backends; degrade
                        # gracefully to no candidates rather than failing analyze.
                        logger.warning("find_by_base_form_pattern failed: %s", e)
                        candidate_records = []

            # Preserve committed status from existing record, OR auto-commit if high confidence
            if existing_record:
                record["committed"] = existing_record.get("committed", False)
                print(f"[ANALYZE] Re-analyzing existing record - preserving committed={record['committed']}")
            else:
                # NEW records: auto-commit if high confidence
                record["committed"] = auto_commit
                if auto_commit:
                    print(f"[ANALYZE] Auto-committing HIGH confidence form: {record.get('form_name', 'Unknown')[:50]} (score: {confidence_score:.2f})")
                else:
                    print(f"[ANALYZE] Saving for review ({confidence_tier} confidence): {record.get('form_name', 'Unknown')[:50]} (score: {confidence_score:.2f})")

            # Store quality metadata
            record["confidence_tier"] = confidence_tier
            record["confidence_score"] = confidence_score
            record["quality_signals"] = quality_signals

        # Check if this is a language variant of an existing form.
        # Skip this expensive check in fast re-analysis mode.
        # candidate_records is a pre-filtered list (typically O(10)) keyed
        # by base form pattern, so the linear scan inside
        # language_dedup.find_matching_form is bounded.
        if should_save:
            if not skip_dedup and candidate_records:
                matching_form = language_dedup.should_merge_as_language_variant(
                    record,
                    candidate_records=candidate_records,
                )

                if matching_form:
                    # Merge into existing record
                    updated_record = language_dedup.merge_language_variant(matching_form, record['source_url'])
                    rid = storage.save(updated_record)
                    merged = True
                else:
                    # Save as new record
                    rid = storage.save(record)
            else:
                # Fast re-analysis (or no candidates): just save/update directly.
                rid = storage.save(record)

        # Log analysis activity
        try:
            from flask import session
            user_data = session.get("user", {})
            email = user_data.get("email", "anonymous")
            name = user_data.get("name", "")
            ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)
            user_agent = request.headers.get('User-Agent', 'Unknown')

            storage.log_analysis_activity(
                email=email,
                activity_type="analyze",
                source_url=pdf_url or "base64_upload",
                forms_found=0,
                forms_analyzed=1,
                success=True,
                name=name,
                ip_address=ip_address,
                user_agent=user_agent
            )
        except Exception as log_err:
            # Don't fail the request if logging fails
            print(f"Failed to log analysis activity: {log_err}")

        # Build conversion cost estimate
        conversion = analyzer.estimate_conversion_cost(record)

        # Determine requires_action flag
        action_type = record.get("action_type", "")
        requires_action = action_type not in (
            "Disclosure (No Signature, No Info Collection)",
        )

        # Complexity tier label
        complexity_score = float(record.get("complexity_score") or 0)
        if complexity_score >= 70:
            complexity_tier = "very_complex"
        elif complexity_score >= 45:
            complexity_tier = "complex"
        elif complexity_score >= 20:
            complexity_tier = "moderate"
        else:
            complexity_tier = "simple"

        # Full metadata table — every stored field except internal/debug
        _SKIP = {"full_text", "_vision_data", "_flags", "title_debug",
                 "quality_signals", "confidence_tier", "confidence_score"}
        metadata = {k: v for k, v in record.items() if k not in _SKIP}

        return jsonify({
            "ok": True,
            "id": rid,
            "saved": should_save,
            "committed": record.get("committed", False) if should_save else None,
            "status": record.get("status"),
            "parse_error": record.get("parse_error"),
            "merged_as_language_variant": merged,

            # ── 3-question summary ───────────────────────────────────────────
            "summary": {
                # Q1: Does this form require action?
                "requires_action": requires_action,
                "action_type": action_type,

                # Q2: How hard is that action?
                "difficulty": {
                    "complexity_score": complexity_score,
                    "complexity_tier": complexity_tier,
                    "nigo_score": float(record.get("nigo_score") or 0),
                    "key_drivers": record.get("key_drivers", []),
                    "estimated_signer_time_min": record.get("estimated_signer_time"),
                    "estimated_processing_time_min": record.get("estimated_processing_time"),
                },

                # Q3: What does it cost to convert to a guided wizard?
                "conversion": conversion,
            },

            # ── Quality / confidence ─────────────────────────────────────────
            "quality": {
                "confidence_tier": confidence_tier,
                "confidence_score": confidence_score,
                "signals": quality_signals,
                "auto_committed": auto_commit and should_save and not existing_record,
            },

            # ── Full metadata table ──────────────────────────────────────────
            "metadata": metadata,
        })

    except Exception as e:
        # Log failed analysis activity
        try:
            from flask import session
            user_data = session.get("user", {})
            email = user_data.get("email", "anonymous")
            name = user_data.get("name", "")
            ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)
            user_agent = request.headers.get('User-Agent', 'Unknown')

            storage.log_analysis_activity(
                email=email,
                activity_type="analyze",
                source_url=pdf_url or "base64_upload",
                forms_found=0,
                forms_analyzed=0,
                success=False,
                error_message=str(e),
                name=name,
                ip_address=ip_address,
                user_agent=user_agent
            )
        except Exception as log_err:
            print(f"Failed to log failed analysis activity: {log_err}")

        return jsonify({"ok": False, "error": str(e)}), 500

@bp.post("/batch_summary")
def batch_summary():
    """
    Log a batch analysis summary after completing a batch of PDF analyses.
    Body (JSON):
    {
      "source_url": "https://example.com",
      "forms_found": 198,
      "forms_analyzed": 150,
      "forms_failed": 48
    }
    """
    j = request.get_json(silent=True) or {}

    try:
        from flask import session
        user_data = session.get("user", {})
        email = user_data.get("email", "anonymous")
        name = user_data.get("name", "")
        ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)
        user_agent = request.headers.get('User-Agent', 'Unknown')

        source_url = j.get("source_url", "")
        forms_found = int(j.get("forms_found", 0))
        forms_analyzed = int(j.get("forms_analyzed", 0))
        forms_failed = int(j.get("forms_failed", 0))

        storage.log_analysis_activity(
            email=email,
            activity_type="batch_analyze",
            source_url=source_url,
            forms_found=forms_found,
            forms_analyzed=forms_analyzed,
            success=True,
            name=name,
            ip_address=ip_address,
            user_agent=user_agent
        )

        return jsonify({
            "ok": True,
            "logged": {
                "forms_found": forms_found,
                "forms_analyzed": forms_analyzed,
                "forms_failed": forms_failed
            }
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# REMOVED: Duplicate /records endpoint (now handled by app/api/records.py with optimization)
