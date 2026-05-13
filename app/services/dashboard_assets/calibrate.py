#!/usr/bin/env python3
"""
Auto-calibrate dashboard constants from FMR compact JSON data.

Usage:
    python calibrate.py fmr_data.json [--branding config.json]

Outputs a JSON config with:
  - entities: list of entity names sorted by frequency
  - entity_short: short-name mapping
  - entity_colors: color assignments
  - maxC, maxN, medC, medN: complexity/NIGO bounds for quadrant chart
  - nigo_high, nigo_med: NIGO threshold percentiles
  - form_count, billable_count, readonly_count
  - readonly_actions: which action types are "readonly" (no signature/collection)
"""
import json
import sys
import statistics


# Default color palettes by institution type
PALETTES = {
    "blue":  ["#00A0DF", "#0072B1", "#004C97", "#1B365D", "#2E8BC0", "#145DA0"],
    "green": ["#7DA24B", "#5A8A3C", "#3D6B2A", "#577B34", "#4A7C2F", "#6B9B3A"],
    "red":   ["#C41230", "#9B1B30", "#7A1428", "#B8293E", "#D94452", "#A01020"],
    "purple":["#6B2D8B", "#5A2480", "#4A1B6D", "#7B3D9B", "#8A4DAB", "#3D1560"],
}

READONLY_ACTIONS = {"Disclosure (No Signature)", "Information Only", "Reference Document", ""}


def _normalize_entity(name):
    """Normalize an entity name: strip, collapse whitespace, fix unicode spaces."""
    import re
    if not name:
        return "Unknown"
    # Replace unicode whitespace variants (non-breaking space, em space, etc.)
    name = re.sub(r'[\u00a0\u2000-\u200b\u202f\u205f\u3000]+', ' ', name)
    # Collapse multiple spaces to one and strip
    name = re.sub(r'\s+', ' ', name).strip()
    return name or "Unknown"


def calibrate(data, palette_name="blue"):
    """Compute all dashboard constants from FMR data."""
    palette = PALETTES.get(palette_name, PALETTES["blue"])

    # Normalize entity names in-place so the dashboard groups them correctly
    for r in data:
        r["e"] = _normalize_entity(r.get("e", ""))

    # Entity detection
    entity_counts = {}
    for r in data:
        e = r.get("e", "Unknown")
        entity_counts[e] = entity_counts.get(e, 0) + 1
    entities = sorted(entity_counts.keys(), key=lambda x: -entity_counts[x])

    # Short names: progressively add words until unique across all entities
    # Two-pass: first assign naive short names, then resolve collisions
    entity_short = {}
    
    # Pass 1: assign first-word short names
    for e in entities:
        words = e.split()
        entity_short[e] = words[0]
    
    # Pass 2: resolve collisions by progressively expanding colliding names
    changed = True
    while changed:
        changed = False
        # Find groups of entities sharing the same short name
        short_to_entities = {}
        for e, s in entity_short.items():
            short_to_entities.setdefault(s, []).append(e)
        for short, group in short_to_entities.items():
            if len(group) <= 1:
                continue
            # Expand each colliding entity by one more word
            for e in group:
                words = e.split()
                current_word_count = len(entity_short[e].split())
                if current_word_count < len(words):
                    entity_short[e] = " ".join(words[:current_word_count + 1])
                    changed = True
                # else: already using full name — leave as-is (natural disambiguation)

    # Assign colors round-robin
    entity_colors = {}
    for i, e in enumerate(entities):
        entity_colors[e] = palette[i % len(palette)]

    # Complexity and NIGO stats
    complexities = [r.get("c", 0) for r in data if r.get("c", 0) > 0]
    nigos = [r.get("ni", 0) for r in data if r.get("ni", 0) > 0]

    maxC = max(complexities) if complexities else 100
    maxN = max(nigos) if nigos else 100
    medC = statistics.median(complexities) if complexities else maxC / 2
    medN = statistics.median(nigos) if nigos else maxN / 2

    # Round for cleaner display
    medC = round(medC)
    medN = round(medN)

    # NIGO thresholds (percentile-based)
    if nigos:
        sorted_n = sorted(nigos)
        p75 = sorted_n[int(len(sorted_n) * 0.75)]
        p50 = sorted_n[int(len(sorted_n) * 0.50)]
        nigo_high = round(p75)
        nigo_med = round(p50)
    else:
        nigo_high = 25
        nigo_med = 15

    # Billable vs readonly
    readonly_count = sum(1 for r in data if r.get("at", "") in READONLY_ACTIONS)
    billable_count = len(data) - readonly_count

    return {
        "form_count": len(data),
        "billable_count": billable_count,
        "readonly_count": readonly_count,
        "entities": entities,
        "entity_counts": {e: entity_counts[e] for e in entities},
        "entity_short": entity_short,
        "entity_colors": entity_colors,
        "maxC": maxC,
        "maxN": maxN,
        "medC": medC,
        "medN": medN,
        "nigo_high": nigo_high,
        "nigo_med": nigo_med,
        "readonly_actions": list(READONLY_ACTIONS - {""}),
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python calibrate.py fmr_data.json [--palette blue|green|red|purple]")
        sys.exit(1)

    with open(sys.argv[1], "r") as f:
        data = json.load(f)

    palette = "blue"
    if "--palette" in sys.argv:
        idx = sys.argv.index("--palette")
        palette = sys.argv[idx + 1]

    config = calibrate(data, palette)
    print(json.dumps(config, indent=2, ensure_ascii=False))
