# app/services/dashboard_builder.py
"""
In-process FECA dashboard builder.

Thin orchestrator around the existing csv_to_fmr.py + build_dashboard.py
scripts in dashboard_assets/. Lets the Cloud Run service produce a
self-contained HTML dashboard from a list of DB records (or any iterable of
form-record dicts) without any external tooling.

The dashboard template is the patched version with our improvements:
  • "Payer" label (not "Service Vertical") in form-detail modal
  • Live recalc on floor/ceiling input (no need to blur)
  • Embedded Docusign Web Forms demo video on the Calculations tab
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).parent / "dashboard_assets"
CSV_TO_FMR = ASSETS_DIR / "csv_to_fmr.py"
BUILD_DASHBOARD = ASSETS_DIR / "build_dashboard.py"
TEMPLATE_PATH = ASSETS_DIR / "dashboard_template.jsx"

# Subset of the AZ-style palette presets — caller can override with custom hex.
PALETTES: Dict[str, Dict[str, str]] = {
    "blue":   {"primary": "#00A0DF", "accent": "#0072B1", "nav": "#004C97", "dark": "#1B365D"},
    "green":  {"primary": "#7DA24B", "accent": "#5A8A3C", "nav": "#3D6B2A", "dark": "#577B34"},
    "red":    {"primary": "#C41230", "accent": "#9B1B30", "nav": "#7A1428", "dark": "#B8293E"},
    "purple": {"primary": "#830051", "accent": "#A91B6A", "nav": "#5C0038", "dark": "#3E0026"},
}

CSV_HEADERS = [
    "Form Name", "Entity Name", "PDF URL", "Language Count", "Pages",
    "Total Fields", "Complexity Score", "NIGO Score", "Confidence Tier",
    "Action Type", "Signature Required", "Signature Count",
    "Notarization Required", "Attachments Required", "Attachment Count",
    "Payment Required", "Payment Amount", "Identification Required",
    "Conditional Logic", "Third Party Involved", "Witnesses Required",
    "Deadlines Present", "Form Purpose", "Industry Vertical",
    "Industry Subvertical", "Estimated Signer Time", "Estimated Processing Time",
    "Conversion Effort (hrs)", "Conversion Cost (USD)", "Conversion Tier",
]


# ── records → CSV ──────────────────────────────────────────────────────────

def _yn(v: Any) -> str:
    return "Yes" if v else "No"


def _derive_form_name(rec: Dict[str, Any]) -> str:
    fn = (rec.get("form_name") or rec.get("pretty_title") or "").strip()
    url = rec.get("source_url") or ""
    if fn and "azmee application" not in fn.lower() and "application free" not in fn.lower():
        return fn
    m = re.search(r"PAP-App-and-Rx-([A-Z0-9_]+)\.pdf", url)
    if m:
        brand = m.group(1).replace("_", " ").title()
        return f"AZ&ME PAP — {brand}"
    return fn or url.rsplit("/", 1)[-1].split("?")[0] or "Untitled"


def records_to_csv_string(records: Iterable[Dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    w.writerow(CSV_HEADERS)
    count = 0
    for m in records:
        if not isinstance(m, dict):
            continue
        sa = m.get("signature_analysis") or {}
        conv = m.get("conversion") or {}
        w.writerow([
            _derive_form_name(m),
            m.get("entity_name") or "",
            m.get("source_url") or "",
            m.get("language_count") or 1,
            m.get("pages") or 0,
            m.get("field_count") or 0,
            m.get("complexity_score") or 0,
            m.get("nigo_score") or 0,
            m.get("confidence_tier") or "",
            m.get("action_type") or "",
            _yn(sa.get("signature_count", 0) > 0 or m.get("signature_required")),
            sa.get("signature_count") or 0,
            _yn(m.get("notarization_required")),
            _yn(m.get("attachments_required")),
            m.get("attachment_count") or 0,
            _yn(m.get("payment_required")),
            m.get("payment_amount") or "",
            _yn(m.get("identification_required")),
            _yn(m.get("conditional_logic")),
            _yn(m.get("third_party_involved")),
            _yn(m.get("witnesses_required")),
            _yn(m.get("deadlines_present")),
            m.get("form_purpose") or "",
            m.get("industry_vertical") or "",
            m.get("industry_subvertical") or "",
            m.get("estimated_signer_time") or "",
            m.get("estimated_processing_time") or "",
            conv.get("effort_hours") or "",
            conv.get("cost_usd") or "",
            conv.get("tier") or "",
        ])
        count += 1
    logger.info("[DASHBOARD] records_to_csv_string wrote %d rows", count)
    return buf.getvalue()


# ── Build pipeline ─────────────────────────────────────────────────────────

def build_dashboard_html(
    *,
    records: Iterable[Dict[str, Any]],
    institution_name: str,
    component_name: Optional[str] = None,
    palette: str = "blue",
    colors_override: Optional[Dict[str, str]] = None,
    logo_path: Optional[str] = None,
    logo_height: int = 50,
    logo_margin_top: int = 0,
    logo_container_h: int = 50,
) -> str:
    """Run the existing csv_to_fmr + build_dashboard scripts in a tempdir."""
    if not CSV_TO_FMR.exists() or not BUILD_DASHBOARD.exists() or not TEMPLATE_PATH.exists():
        raise RuntimeError(
            f"Dashboard assets missing in {ASSETS_DIR}; "
            f"csv_to_fmr={CSV_TO_FMR.exists()}, "
            f"build={BUILD_DASHBOARD.exists()}, "
            f"template={TEMPLATE_PATH.exists()}"
        )

    palette_colors = dict(PALETTES.get(palette, PALETTES["blue"]))
    if colors_override:
        palette_colors.update(colors_override)

    comp_name = component_name or re.sub(r"[^A-Za-z0-9]", "", institution_name) or "Institution"

    csv_text = records_to_csv_string(records)

    with tempfile.TemporaryDirectory(prefix="feca_") as tmpd:
        tmp = Path(tmpd)
        csv_path = tmp / "data.csv"
        fmr_path = tmp / "fmr.json"
        cfg_path = tmp / "branding.json"
        out_path = tmp / "dashboard.html"

        csv_path.write_text(csv_text, encoding="utf-8")

        cfg = {
            "institution_name": institution_name,
            "component_name": comp_name,
            "logo_height": logo_height,
            "logo_margin_top": logo_margin_top,
            "logo_container_h": logo_container_h,
            "colors": palette_colors,
            "palette": palette,
            "readonly_check": (
                "['Disclosure (No Signature)','Disclosure (No Signature, No Info Collection)',"
                "'Information Only','Reference Document'].includes(d.at) || (!d.sig && !d.f)"
            ),
        }
        if logo_path and os.path.isfile(logo_path):
            cfg["logo_path"] = logo_path
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

        # 1) CSV → FMR JSON
        subprocess.run(
            [sys.executable, str(CSV_TO_FMR), str(csv_path), str(fmr_path)],
            check=True, capture_output=True, text=True,
        )

        # 2) FMR + config + template → HTML
        subprocess.run(
            [sys.executable, str(BUILD_DASHBOARD),
             "--data", str(fmr_path),
             "--config", str(cfg_path),
             "--template", str(TEMPLATE_PATH),
             "--output", str(out_path)],
            check=True, capture_output=True, text=True,
        )

        return out_path.read_text(encoding="utf-8")
