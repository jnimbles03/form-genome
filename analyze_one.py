#!/usr/bin/env python3
"""
Run a single PDF through the analyzer locally — no Flask server, no API call.

Usage:
    python analyze_one.py <pdf_url>
    python analyze_one.py /path/to/local.pdf

Output: pretty summary on stdout + full record JSON written to /tmp/analyze_one.json
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Make `app.*` imports work when run from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Load .env if present so GEMINI_API_KEY etc. are picked up.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from app.services import analyzer  # noqa: E402


def _pretty(record: dict) -> None:
    s = record.get("summary", {})
    diff = (s.get("difficulty") or {}) if isinstance(s, dict) else {}
    conv = (s.get("conversion") or record.get("conversion") or {}) if isinstance(s, dict) else {}
    q = record.get("quality", {}) if isinstance(record.get("quality"), dict) else {}

    print("\n" + "=" * 72)
    print(f"  {record.get('form_name') or record.get('pretty_title') or 'Untitled'}")
    print("=" * 72)
    print(f"  Entity:           {record.get('entity_name') or record.get('entity') or '—'}")
    print(f"  Pages:            {record.get('pages') or '—'}")
    print(f"  Field count:      {record.get('total_field_count') or record.get('field_count') or 0}")
    print(f"  Action type:      {record.get('action_type') or '—'}")
    print(f"  Complexity:       {record.get('complexity_score') or '—'}")
    print(f"  NIGO score:       {record.get('nigo_score') or '—'}")
    print(f"  Confidence:       {record.get('confidence_tier') or q.get('confidence_tier') or '—'}")
    if conv:
        print(f"  Conversion:       ${conv.get('cost_usd', 0):,} "
              f"({conv.get('effort_hours', 0)}h, tier={conv.get('tier', '—')})")
    if record.get("vision_analyzed"):
        print(f"  Vision used:      yes ({record.get('_vision_data', {}).get('document_type', '?')})")
    if record.get("parse_error"):
        print(f"  Parse error:      {record['parse_error']}")
    print()


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2

    target = sys.argv[1]
    opts = {
        "timeout": 30,
        "force_minimal": False,    # full analysis (vision + LLM)
        "skip_vision": False,
        "skip_llm_title": False,
    }

    if target.startswith(("http://", "https://")):
        record = analyzer.analyze_pdf(pdf_url=target, **opts)
    else:
        path = Path(target).expanduser().resolve()
        if not path.is_file():
            print(f"File not found: {path}", file=sys.stderr)
            return 1
        record = analyzer.analyze_pdf(pdf_bytes=path.read_bytes(), filename=path.name, **opts)

    if not record:
        print("Analyzer returned no record.", file=sys.stderr)
        return 1

    # Best-effort: derive the same summary/conversion the API endpoint adds.
    try:
        record["conversion"] = analyzer.estimate_conversion_cost(record)
    except Exception:
        pass

    _pretty(record)

    # Dump full record for inspection.
    out_path = Path("/tmp/analyze_one.json")
    out_path.write_text(json.dumps(record, indent=2, default=str, ensure_ascii=False))
    print(f"Full record → {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
