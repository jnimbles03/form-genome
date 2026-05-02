# app/api/report.py
from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from flask import Blueprint, jsonify, current_app, request

# We only read; storage abstracts your DB/file layer.
from app.services import storage

bp = Blueprint("report", __name__)

# Location of UI assets (report template)
UI_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "ui"))
REPORT_TEMPLATE_FILE = os.path.join(UI_DIR, "report-template.html")


# ---------------------------
# Helpers
# ---------------------------

def _action_type_from_row(r: Dict[str, Any]) -> str:
    """
    Infer Action Type according to your spec:
      - Signature Required
      - Information Collection (No Signature)
      - Disclosure (No Signature, No Info Collection)
    """
    sig = 0
    try:
        sig = int(
            r.get("signature_count")
            or (r.get("signature_analysis") or {}).get("signature_count")
            or 0
        )
    except Exception:
        sig = 0

    notary = bool(
        r.get("notarization_required")
        or r.get("witnesses_required")
        or r.get("click_to_agree")
    )
    info = (
        (int(r.get("field_count") or 0) > 0)
        or bool(r.get("attachments_required"))
        or bool(r.get("identification_required"))
    )

    if sig > 0 or notary:
        return "Signature Required"
    if info:
        return "Information Collection (No Signature)"
    return "Disclosure (No Signature, No Info Collection)"


def _backfill_action_type(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add/repair action_type for each row in the list."""
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        if not r.get("action_type"):
            rr = dict(r)
            rr["action_type"] = _action_type_from_row(rr)
            out.append(rr)
        else:
            out.append(r)
    return out


def _load_rows_filtered(
    committed: bool = None,
    ids: List[str] = None,
    public_sector: bool = False
) -> List[Dict[str, Any]]:
    """
    Load rows with filters applied at SQL level for performance.

    Args:
        committed: If True, only committed records; if False, only uncommitted; if None, all
        ids: Optional list of IDs to filter by
        public_sector: If True, filter to Public Sector records only

    Returns:
        Filtered list of records
    """
    # Use new SQL-level filtering
    try:
        if public_sector:
            # For public sector, we need to filter by industry_vertical in SQL
            rows = storage.list_filtered(
                committed=committed,
                ids=ids,
                industry_vertical="Public Sector"
            )
        else:
            # Regular filtering (committed and/or IDs)
            rows = storage.list_filtered(
                committed=committed,
                ids=ids
            )
        return rows or []
    except AttributeError:
        # Fallback to old method if list_filtered doesn't exist yet
        print("[REPORT] WARNING: storage.list_filtered() not available, using fallback", flush=True)
        try:
            rows = storage.list_all()
        except AttributeError:
            try:
                rows = storage.list()
            except AttributeError:
                rows = storage.list_records()
        return rows or []


def _choose_template_html() -> str:
    """
    Try to load /ui/report-template.html. If missing, use a minimal fallback.
    """
    try:
        with open(REPORT_TEMPLATE_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        # Minimal but branded fallback
        return """<!doctype html>
<meta charset="utf-8">
<title>Form Genome Project — Report</title>
<style>
  body{font-family: ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; margin:32px; color:#101828; background:#0b1020;}
  h1,h2,h3{color:#e6ecff; margin:0 0 12px}
  .wrap{max-width:1200px; margin:0 auto; background:#0f1533; border:1px solid #223; border-radius:14px; padding:24px}
  .grid{display:grid; grid-template-columns: 1fr 1fr; gap:16px}
  .card{background:#121a3a; border:1px solid #203; border-radius:12px; padding:16px}
  .mono{font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace; color:#b7c1ff}
  table{width:100%; border-collapse: collapse; margin-top:12px; min-width:900px}
  th,td{border-bottom:1px solid #202a55; padding:8px 10px; color:#d6dcff; font-size:13px}
  th{color:#9fb2ff; text-align:left; position:sticky; top:0; background:#0f1533;}
  .bar{display:inline-block;height:6px;width:80px;background:#334;border-radius:3px;margin-right:6px}
  .bar>span{display:block;height:100%;background:#0B6E4F;border-radius:3px}
  a{color:#7dd3fc; text-decoration:none}
</style>
<div class="wrap">
  <h1>Global Genetics Report</h1>
  <p class="mono">Subset generated {{NOW}}</p>
  <div id="summary" class="grid"></div>
  <div class="card">
    <h2>Forms</h2>
    <table id="tbl">
      <thead>
        <tr>
          <th>Form Name</th><th>Complexity Score</th><th>NIGO Score</th>
          <th>Notarization</th><th>Attachments</th><th>Data Validation</th>
          <th>Dependencies</th><th>Key Requirements</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
  </div>
</div>
<script>
(function(){
  const rows = (window.__REPORT_ROWS__||[]);
  const tb = document.querySelector('#tbl tbody');
  function yn(b){return b?'Yes':'No'}
  function bar(score){
    const s=Math.round(score||0);
    return score!=null?`<span class="bar"><span style="width:${Math.min(100,Math.max(0,s))}%"></span></span>${s}`:'—';
  }
  function hasVal(r){
    return !!(r.field_analysis?.validation_rules || r.has_calculations || r.javascript_present);
  }
  function reqs(r){
    const a=[];
    const sig=(r.signature_analysis?.signature_count||r.signature_count||0);
    if(sig>0)a.push(sig+' signature'+(sig>1?'s':''));
    if(r.notarization_required)a.push('Notarization');
    if(r.witnesses_required)a.push('Witnesses');
    if(r.attachments_required)a.push('Attachments');
    if(r.identification_required)a.push('ID verification');
    if(r.conditional_logic)a.push('Conditional logic');
    return a.join(', ')||'—';
  }
  rows.forEach(r=>{
    const tr=document.createElement('tr');
    tr.innerHTML=`
      <td>${(r.form_name||'').replace(/</g,'&lt;')}</td>
      <td>${bar(r.complexity_score)}</td>
      <td>${bar(r.nigo_score)}</td>
      <td>${yn(r.notarization_required||r.witnesses_required)}</td>
      <td>${yn(r.attachments_required)}</td>
      <td>${yn(hasVal(r))}</td>
      <td>${yn(r.conditional_logic)}</td>
      <td>${reqs(r)}</td>`;
    tb.appendChild(tr);
  });
}());
</script>
"""


def _embed_rows_into_template(rows: List[Dict[str, Any]]) -> str:
    """
    Return final HTML by embedding the row data into the template.
    We keep the template’s content intact and only add a script tag
    with window.__REPORT_ROWS__.
    """
    tpl = _choose_template_html()
    # If template has a placeholder, use it; else append script at end.
    data_script = f'<script>window.__REPORT_ROWS__={json.dumps(rows, ensure_ascii=False)};</script>'
    if "<!--__DATA__-->" in tpl:
        html = tpl.replace("<!--__DATA__-->", data_script)
    else:
        # Also inject a timestamp for the fallback template
        html = tpl.replace("{{NOW}}", json.dumps(__import__("datetime").datetime.now().isoformat()))
        html = html + "\n" + data_script
    return html


# ---------------------------
# Route
# ---------------------------

@bp.post("/report/generate")
def generate_report():
    """
    Request JSON:
      {
        "ids": ["sha1-or-id", ...],      # optional; subset by IDs
        "pubsec": false,                 # optional; public-sector filter
        "committed": true                # optional; ONLY committed
      }

    Response JSON:
      { "ok": true, "count": N, "html": "<!doctype html>..." }
    """
    # Use Flask's built-in JSON parsing (handles request body correctly)
    data = request.get_json(silent=True) or {}

    ids_filter = data.get("ids") or []
    public_only = bool(data.get("pubsec"))
    committed_only = bool(data.get("committed"))

    print(f"[REPORT] Request data keys: {list(data.keys())}", flush=True)
    print(f"[REPORT] ids_filter: {len(ids_filter) if isinstance(ids_filter, list) else 'N/A'} IDs", flush=True)
    print(f"[REPORT] committed_only: {committed_only}, public_only: {public_only}", flush=True)

    # 1) Load with SQL-level filtering (OPTIMIZATION: no longer loads all records!)
    committed_filter = committed_only if committed_only else None
    rows = _load_rows_filtered(
        committed=committed_filter,
        ids=ids_filter if ids_filter else None,
        public_sector=public_only
    )

    print(f"[REPORT] SQL-filtered query returned {len(rows)} records", flush=True)

    # 2) Backfill action_type (never crash if missing)
    rows = _backfill_action_type(rows)

    # Check if we have any data to report
    if not rows:
        print(f"[REPORT] ⚠️  WARNING: No records match the filters. ids_filter={len(ids_filter) if isinstance(ids_filter, list) else 0}, committed_only={committed_only}, public_only={public_only}", flush=True)
        error_html = """<!doctype html>
<meta charset="utf-8">
<title>Form Genome Project — No Data</title>
<style>
  body{font-family: system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif; margin:0; display:flex; align-items:center; justify-content:center; min-height:100vh; background:#FAFAFA; color:#222}
  .box{max-width:500px; padding:32px; background:#fff; border:1px solid #EAEAEA; border-radius:18px; text-align:center; box-shadow:0 6px 18px rgba(0,0,0,.06)}
  h1{color:#C43D00; font-size:28px; margin:0 0 12px}
  p{color:#666; line-height:1.6}
  .note{font-size:13px; color:#999; margin-top:20px; padding-top:20px; border-top:1px solid #EAEAEA}
</style>
<div class="box">
  <h1>⚠️ No Data to Report</h1>
  <p>The report could not be generated because no records matched the requested filters.</p>
  <p><strong>Possible causes:</strong></p>
  <ul style="text-align:left; color:#666">
    <li>The forms in your dashboard haven't been saved to the database yet</li>
    <li>The requested form IDs don't exist in the database</li>
    <li>The filters excluded all records (e.g., committed-only filter)</li>
  </ul>
  <p class="note">Tip: Make sure to analyze forms and save them before generating a report.</p>
</div>
"""
        return jsonify({"ok": False, "count": 0, "html": error_html, "error": "No records matched filters"})

    # 4) Build HTML (template or fallback)
    try:
        html = _embed_rows_into_template(rows)
    except Exception as e:
        # As a last resort, return a tiny HTML with just a JSON dump
        html = "<!doctype html><meta charset='utf-8'><title>Report (Fallback)</title>" \
               "<pre style='white-space:pre-wrap;color:#fff;background:#111;padding:16px;border-radius:8px'>" \
               + json.dumps(rows, indent=2, ensure_ascii=False) + "</pre>"

    return jsonify({"ok": True, "count": len(rows), "html": html})