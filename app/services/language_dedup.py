"""
Language version deduplication logic.

Detects when multiple PDFs are language versions of the same form and merges them
into a single record with language_count tracking.
"""
import logging
import re
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse
import os

logger = logging.getLogger(__name__)


def extract_language_code(url: str) -> Optional[str]:
    """
    Extract language code from URL if present.

    Examples:
      pub_223s.pdf -> 's' (Spanish)
      pub_223v.pdf -> 'v' (Vietnamese)
      pub_223.pdf -> None (base/English)
      pub%20223.pdf -> None

    Returns:
      Language code (1-3 chars) or None if no language variant detected
    """
    if not url:
        return None

    path = urlparse(url).path
    filename = os.path.basename(path)

    # Pattern: filename ending with 1-3 letter code before .pdf
    # e.g., pub_223s.pdf, form-45k.pdf, SF-86a.pdf
    # But NOT numbers like pub_223.pdf or version like form-v2.pdf
    match = re.search(r'([a-z]{1,3})\.pdf$', filename, re.IGNORECASE)

    if match:
        lang_code = match.group(1).lower()

        # Exclude common false positives (version numbers, etc.)
        false_positives = ['v1', 'v2', 'v3', 'v4', 'v5', 'v6', 'v7', 'v8', 'v9', 'pdf']
        if lang_code in false_positives:
            return None

        # Must be preceded by a digit or separator (not another letter)
        # This prevents matching "manual.pdf" but allows "pub_223s.pdf"
        pos = match.start()
        if pos > 0:
            prev_char = filename[pos - 1]
            # Previous char should be digit, _, -, or space
            if prev_char.isalpha():
                return None

        # Single-letter codes or known multi-letter language codes
        known_multi = ['be', 'ur', 'pu', 'gu', 'po', 'th', 'uk', 'vi']
        if len(lang_code) == 1 or lang_code in known_multi:
            return lang_code

    return None


def get_base_form_pattern(url: str) -> Optional[str]:
    """
    Extract base form pattern from URL (without language code).

    Examples:
      .../pub_223s.pdf -> pub_223
      .../pub_223.pdf -> pub_223
      .../SF-86a.pdf -> sf-86

    Returns:
      Base form identifier or None
    """
    if not url:
        return None

    path = urlparse(url).path
    filename = os.path.basename(path)

    # Remove .pdf extension
    name_no_ext = re.sub(r'\.pdf$', '', filename, flags=re.IGNORECASE)

    # Remove language code suffix if present
    # Detect language code (1-3 letters at end after digit or separator)
    lang_code = extract_language_code(url)
    if lang_code:
        # Remove the language code from the end
        # e.g., "pub_223s" -> "pub_223", "SF-86a" -> "SF-86"
        base = name_no_ext[:-len(lang_code)]
    else:
        base = name_no_ext

    # Normalize: lowercase, strip whitespace
    base = base.lower().strip()

    if len(base) >= 3:  # Minimum reasonable form name
        return base

    return None


def find_matching_form(
    new_url: str,
    new_pages: int,
    existing_records: Optional[List[Dict[str, Any]]] = None,
    candidate_records: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Find existing record that matches this form (ignoring language variant).

    Args:
      new_url: URL of form being analyzed
      new_pages: Page count of new form
      existing_records: Legacy list of all existing records. Retained for
        backwards compatibility with callers that already perform a
        full-table load. Prefer candidate_records.
      candidate_records: Pre-filtered list of records sharing the same
        base_form_pattern. When supplied, the linear scan happens over
        this small set (typically O(10)) instead of the full table.

    Returns:
      Matching record or None
    """
    if not new_url or new_pages <= 0:
        return None

    new_pattern = get_base_form_pattern(new_url)
    if not new_pattern:
        return None

    # Only check if new URL has a language code
    new_lang = extract_language_code(new_url)
    if not new_lang:
        # Base form (no language code) - don't merge
        return None

    # Prefer pre-filtered candidates when supplied; fall back to the legacy
    # full-list parameter so existing call sites keep working unchanged.
    pool = candidate_records if candidate_records is not None else existing_records
    if pool is None:
        # Nothing to scan
        return None

    for record in pool:
        existing_url = record.get('source_url', '')
        existing_pages = record.get('pages', 0)

        # Must have same page count
        if existing_pages != new_pages:
            continue

        # Must have same base pattern
        existing_pattern = get_base_form_pattern(existing_url)
        if existing_pattern != new_pattern:
            continue

        # Must be from same domain (safety check)
        new_domain = urlparse(new_url).netloc
        existing_domain = urlparse(existing_url).netloc
        if new_domain != existing_domain:
            continue

        # Found a match!
        return record

    return None


def merge_language_variant(
    existing_record: Dict[str, Any],
    new_url: str
) -> Dict[str, Any]:
    """
    Merge a new language variant into an existing record.

    Args:
      existing_record: The base form record to update
      new_url: URL of the new language variant

    Returns:
      Updated record with incremented language_count and stored variant URL
    """
    # Get current language count
    lang_count = existing_record.get('language_count', 1)

    # Get current language variants list
    variants = existing_record.get('language_variants', [])
    if not isinstance(variants, list):
        variants = []

    # Add new variant if not already present
    if new_url not in variants:
        variants.append(new_url)
        lang_count += 1

    # Update record
    updated = existing_record.copy()
    updated['language_count'] = lang_count
    updated['language_variants'] = variants

    # Preserve committed status from existing record
    # If the base form was committed, keep it committed after merging
    # (Only set to False if it wasn't already committed)

    # Update form title to reflect multiple languages
    form_name = updated.get('form_name', '')
    if lang_count > 1 and 'languages' not in form_name.lower():
        # Add language count to title
        updated['form_title'] = f"{form_name} ({lang_count} languages)"

    return updated


def should_merge_as_language_variant(
    new_record: Dict[str, Any],
    existing_records: Optional[List[Dict[str, Any]]] = None,
    candidate_records: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Check if new record should be merged as a language variant.

    Args:
      new_record: Record being saved
      existing_records: Legacy full record list (kept for backwards
        compatibility with older callers).
      candidate_records: Pre-filtered candidate list (preferred). When
        provided, dedup matching scans this small set instead of the full
        table.

    Returns:
      Existing record to merge into, or None if should save as new record
    """
    new_url = new_record.get('source_url', '')
    new_pages = new_record.get('pages', 0)

    # Skip if no URL or pages
    if not new_url or new_pages <= 0:
        return None

    # Skip if already failed
    if new_record.get('status') == 'failed':
        return None

    # Find matching form
    return find_matching_form(
        new_url,
        new_pages,
        existing_records=existing_records,
        candidate_records=candidate_records,
    )
