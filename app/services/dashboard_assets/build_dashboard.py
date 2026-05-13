#!/usr/bin/env python3
"""
Build a self-contained HTML dashboard from FMR JSON data + branding config.

Usage:
    python build_dashboard.py \\
        --data fmr_data.json \\
        --config branding.json \\
        --template dashboard_template.jsx \\
        --output dashboard.html

The branding config JSON should contain:
{
    "institution_name": "Acme Bank",
    "component_name": "AcmeBank",
    "logo_url": "https://...",
    "logo_path": "/path/to/local/logo.png",
    "logo_height": 120,
    "logo_margin_top": -25,
    "logo_container_h": 70,
    "colors": {
        "primary": "#00A0DF",
        "accent": "#0072B1",
        "nav": "#004C97",
        "dark": "#1B365D"
    },
    "readonly_check": "['Disclosure (No Signature)','Information Only','Reference Document'].includes(d.at)"
}

Any fields not provided use sensible defaults.
Entity names, colors, and calibrated constants are auto-computed from the data
unless explicitly overridden in the config.
"""
import json
import sys
import os
import argparse
import base64
import mimetypes
from urllib.request import urlopen, Request
from urllib.error import URLError

# Add parent dir to path so we can import calibrate
sys.path.insert(0, os.path.dirname(__file__))
from calibrate import calibrate


DEFAULTS = {
    "institution_name": "Institution",
    "component_name": "Institution",
    "logo_url": "",
    "logo_height": 80,
    "logo_margin_top": -20,
    "logo_container_h": 50,
    "colors": {
        "primary": "#00A0DF",
        "accent": "#0072B1",
        "nav": "#004C97",
        "dark": "#1B365D",
    },
    "palette": "blue",
    "readonly_check": "['Disclosure (No Signature)','Information Only','Reference Document'].includes(d.at)",
}


def deep_merge(base, override):
    """Merge override into base, recursing into dicts."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = v
    return result


# Common extension → MIME mapping for logos
_MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
}


def embed_logo(logo_source):
    """Convert a logo URL or local file path into a base64 data URI.

    Accepts:
      - An HTTP/HTTPS URL  → downloads and encodes
      - A local file path  → reads and encodes
      - An empty string    → returns empty string (no logo)
      - Already a data URI → returns as-is

    Returns the data URI string, or the original URL as a fallback if
    download fails (with a warning printed to stderr).
    """
    if not logo_source:
        return ""
    if logo_source.startswith("data:"):
        return logo_source  # already embedded

    raw_bytes = None
    mime_type = None

    # --- Local file ---
    if os.path.isfile(logo_source):
        ext = os.path.splitext(logo_source)[1].lower()
        mime_type = _MIME_MAP.get(ext, mimetypes.guess_type(logo_source)[0] or "image/png")
        with open(logo_source, "rb") as f:
            raw_bytes = f.read()

    # --- Remote URL ---
    elif logo_source.startswith(("http://", "https://")):
        try:
            req = Request(logo_source, headers={
                "User-Agent": "FormGenomeDashboardBuilder/1.0",
                "Accept": "image/*,*/*",
            })
            with urlopen(req, timeout=15) as resp:
                content_type = resp.headers.get("Content-Type", "")
                raw_bytes = resp.read()
                # Derive MIME from Content-Type header, fall back to extension
                if "/" in content_type:
                    mime_type = content_type.split(";")[0].strip()
                else:
                    ext = os.path.splitext(logo_source.split("?")[0])[1].lower()
                    mime_type = _MIME_MAP.get(ext, "image/png")
        except (URLError, OSError, TimeoutError) as exc:
            print(f"  ⚠ Logo download failed ({exc}); using raw URL as fallback", file=sys.stderr)
            return logo_source

    else:
        # Unknown format — pass through as-is
        return logo_source

    if raw_bytes is None:
        return logo_source

    b64 = base64.b64encode(raw_bytes).decode("ascii")
    data_uri = f"data:{mime_type};base64,{b64}"
    size_kb = len(raw_bytes) / 1024
    print(f"  Logo embedded: {size_kb:.1f} KB ({mime_type})")
    return data_uri


def build(data_path, config_path, template_path, output_path):
    # Load data
    with open(data_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Load config
    config = dict(DEFAULTS)
    if config_path and os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            user_config = json.load(f)
        config = deep_merge(config, user_config)

    # Auto-calibrate from data
    cal = calibrate(data, config.get("palette", "blue"))

    # Use calibrated values unless overridden
    entities = config.get("entities", cal["entities"])
    entity_short = config.get("entity_short", cal["entity_short"])
    entity_colors = config.get("entity_colors", cal["entity_colors"])
    maxC = config.get("maxC", cal["maxC"])
    maxN = config.get("maxN", cal["maxN"])
    medC = config.get("medC", cal["medC"])
    medN = config.get("medN", cal["medN"])
    nigo_high = config.get("nigo_high", cal["nigo_high"])
    nigo_med = config.get("nigo_med", cal["nigo_med"])

    # Load template
    with open(template_path, "r", encoding="utf-8") as f:
        jsx = f.read()

    # Embed logo as base64 data URI for self-contained output
    # Accepts logo_url (remote) or logo_path (local file) in config
    logo_source = config.get("logo_path", config["logo_url"])
    logo_data_uri = embed_logo(logo_source)

    # Perform all substitutions
    replacements = {
        "__RAW_DATA_JSON__":        json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        "__ENTITIES_JSON__":        json.dumps(entities, ensure_ascii=False),
        "__ENTITY_SHORT_JSON__":    json.dumps(entity_short, ensure_ascii=False),
        "__ENTITY_COLORS_JSON__":   json.dumps(entity_colors, ensure_ascii=False),
        "__MAX_C__":                str(maxC),
        "__MAX_N__":                str(maxN),
        "__MED_C__":                str(medC),
        "__MED_N__":                str(medN),
        "__NIGO_HIGH__":            str(nigo_high),
        "__NIGO_MED__":             str(nigo_med),
        "__COLOR_PRIMARY__":        config["colors"]["primary"],
        "__COLOR_ACCENT__":         config["colors"]["accent"],
        "__COLOR_NAV__":            config["colors"]["nav"],
        "__COLOR_DARK__":           config["colors"].get("dark", config["colors"]["nav"]),
        "__LOGO_URL__":             logo_data_uri,
        "__LOGO_HEIGHT__":          str(config["logo_height"]),
        "__LOGO_MARGIN_TOP__":      str(config["logo_margin_top"]),
        "__LOGO_CONTAINER_H__":     str(config["logo_container_h"]),
        "__INST_NAME__":            config["institution_name"],
        "__COMP_NAME__":            config["component_name"],
        "__READONLY_CHECK__":       config["readonly_check"],
    }

    for placeholder, value in replacements.items():
        jsx = jsx.replace(placeholder, value)

    # Wrap in self-contained HTML with React from CDN
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="theme-color" content="{config['colors']['primary']}"/>
<title>{config['institution_name']} — Form Experience Conversion Analysis</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/react/18.3.1/umd/react.production.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/react-dom/18.3.1/umd/react-dom.production.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/babel-standalone/7.26.5/babel.min.js"></script>
</head>
<body>
<div id="root"></div>
<script type="text/babel">
{jsx}

const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(React.createElement({config['component_name']}Dashboard));
</script>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Dashboard built: {output_path}")
    print(f"  Forms: {len(data)}")
    print(f"  Entities: {len(entities)}")
    print(f"  maxC={maxC}, maxN={maxN}, medC={medC}, medN={medN}")
    return output_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build Form Genome dashboard HTML")
    parser.add_argument("--data", required=True, help="Path to FMR compact JSON")
    parser.add_argument("--config", default=None, help="Path to branding config JSON")
    parser.add_argument("--template", required=True, help="Path to dashboard_template.jsx")
    parser.add_argument("--output", required=True, help="Output HTML path")
    args = parser.parse_args()
    build(args.data, args.config, args.template, args.output)
