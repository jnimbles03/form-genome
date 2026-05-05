#!/usr/bin/env python3
"""
Direct test of the Gemini Flash vision analyzer on a single PDF.

Usage:
    python test_vision_one.py <pdf_url_or_path>

Skips the heuristic analyzer entirely; just feeds PDF bytes into
`analyze_flat_pdf_with_vision()` so we can verify the Gemini multimodal
path works (PDF -> images -> base64 -> Gemini -> JSON parse).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests
from app.services.pdf_vision_analyzer import analyze_flat_pdf_with_vision


def fetch(target: str) -> bytes:
    if target.startswith(("http://", "https://")):
        print(f"Downloading {target}...", flush=True)
        r = requests.get(target, timeout=30)
        r.raise_for_status()
        return r.content
    p = Path(target).expanduser().resolve()
    return p.read_bytes()


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2

    pdf_bytes = fetch(sys.argv[1])
    print(f"PDF size: {len(pdf_bytes):,} bytes", flush=True)

    print(f"\nCalling analyze_flat_pdf_with_vision (provider=gemini, "
          f"model=gemini-2.0-flash)...\n", flush=True)
    t0 = time.time()
    result = analyze_flat_pdf_with_vision(
        pdf_bytes=pdf_bytes,
        source_url=sys.argv[1],
        provider="gemini",  # explicit
        model=None,         # let it pick gemini-2.0-flash from env/default
    )
    elapsed = time.time() - t0

    print(f"\n--- Vision analysis complete in {elapsed:.1f}s ---")
    print(f"  vision_analyzed:     {result.get('vision_analyzed')}")
    print(f"  is_actionable:       {result.get('is_actionable')}")
    print(f"  document_type:       {result.get('document_type', '—')}")
    print(f"  reason:              {result.get('actionability_reason') or result.get('reason') or '—'}")
    if 'fields' in result:
        flds = result.get('fields', [])
        print(f"  fields_detected:     {len(flds)}")
        for i, f in enumerate(flds[:10]):
            print(f"    [{i+1}] {f}")
    if 'signature_count' in result:
        print(f"  signature_count:     {result.get('signature_count')}")
    if result.get('error'):
        print(f"  ERROR:               {result['error']}")

    out = Path("/tmp/vision_one.json")
    out.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\nFull JSON → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
