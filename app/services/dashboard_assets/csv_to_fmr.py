#!/usr/bin/env python3
"""
Convert a Form Genome Chrome Extension CSV export into FMR compact JSON.

Usage:
    python csv_to_fmr.py input.csv [output.json]

The CSV is expected to have columns exported by the Form Genome Chrome Extension:
  Form Name, Entity Name, PDF URL, Language Count, Pages, Total Fields,
  Complexity Score, NIGO Score, Confidence Tier, Action Type, Signature Required,
  Signature Count, Notarization Required, Attachments Required, Attachment Count,
  Payment Required, Payment Amount, Identification Required, Conditional Logic,
  Third Party Involved, Witnesses Required, Deadlines Present, Form Purpose,
  Industry Vertical, Industry Subvertical, Estimated Signer Time,
  Estimated Processing Time

Output is a JSON array of objects with compact keys:
  n, e, p, f, c, ni, at, sig, sc, not, att, ac, pay, id, con, tp, wit, dl,
  pur, sv, st, pt
"""
import csv
import json
import sys
import os


# Map CSV column headers → FMR compact keys
COLUMN_MAP = {
    "Form Name":                "n",
    "Entity Name":              "e",
    "Pages":                    "p",
    "Total Fields":             "f",
    "Complexity Score":         "c",
    "NIGO Score":               "ni",
    "Action Type":              "at",
    "Signature Required":       "sig",
    "Signature Count":          "sc",
    "Notarization Required":    "not",
    "Attachments Required":     "att",
    "Attachment Count":         "ac",
    "Payment Required":         "pay",
    "Identification Required":  "id",
    "Conditional Logic":        "con",
    "Third Party Involved":     "tp",
    "Witnesses Required":       "wit",
    "Deadlines Present":        "dl",
    "Form Purpose":             "pur",
    "Industry Vertical":        "sv",
    "Estimated Signer Time":    "st",
    "Estimated Processing Time":"pt",
}

# Fields that should be parsed as numbers (int or float)
NUMERIC_FIELDS = {"p", "f", "c", "ni", "sc", "ac", "st", "pt"}


def parse_numeric(val):
    """Try to parse a value as int, then float, else return 0."""
    if val is None or val.strip() == "":
        return 0
    val = val.strip().replace(",", "")
    try:
        f = float(val)
        return int(f) if f == int(f) else round(f, 1)
    except ValueError:
        return 0


def _normalize_entity(name):
    """Normalize an entity name: strip, collapse whitespace, fix unicode spaces."""
    import re
    if not name:
        return "Unknown"
    name = re.sub(r'[\u00a0\u2000-\u200b\u202f\u205f\u3000]+', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name or "Unknown"


def convert_row(row):
    """Convert one CSV row dict into an FMR compact record."""
    record = {}
    for csv_col, fmr_key in COLUMN_MAP.items():
        raw = row.get(csv_col, "").strip()
        if fmr_key in NUMERIC_FIELDS:
            record[fmr_key] = parse_numeric(raw)
        elif fmr_key == "e":
            record[fmr_key] = _normalize_entity(raw)
        else:
            record[fmr_key] = raw
    return record


def convert_csv(input_path, output_path=None):
    """Read a CSV file and write FMR compact JSON."""
    records = []
    with open(input_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rec = convert_row(row)
            # Skip rows with no form name and no entity
            if rec["n"] or rec["e"]:
                records.append(rec)

    if output_path is None:
        base = os.path.splitext(input_path)[0]
        output_path = base + "_fmr.json"

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, separators=(",", ":"))

    return output_path, len(records)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python csv_to_fmr.py input.csv [output.json]")
        sys.exit(1)
    inp = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else None
    path, count = convert_csv(inp, out)
    print(f"Converted {count} records → {path}")
