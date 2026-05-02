"""
API endpoint to update entity names based on domain mappings.
"""
from flask import Blueprint, current_app, request, jsonify
from app.services import storage, domain_mappings

bp = Blueprint("update_entity_names", __name__)

# Domain mappings to apply
DOMAIN_UPDATES = {
    'mt.gov': 'State of Montana',
    'fairfaxcounty.gov': 'Fairfax County',
    'schoolsfirstfcu.org': 'SchoolsFirst Federal Credit Union',
    'iup.edu': 'Indiana University of Pennsylvania',
    'nsw.gov.au': 'New South Wales Government',
    'illinoistollway.com': 'Illinois Tollway',
    'clevelandohio.gov': 'City of Cleveland',
}

@bp.post("/update_entity_names")
def update_entity_names():
    """
    Update entity names for records with new domain mappings.

    POST /api/update_entity_names
    Body: { "pin": "..." }

    Returns: { "ok": true, "updated": 595, "by_domain": {...} }
    """
    # Check PIN against env-driven config (matches every other admin endpoint).
    data = request.get_json(silent=True) or {}
    pin = data.get('pin', '')
    expected = current_app.config.get("ADMIN_PIN")
    if not expected:
        return jsonify({"ok": False, "error": "Server not configured (ADMIN_PIN)"}), 500
    if pin != expected:
        return jsonify({"ok": False, "error": "Invalid PIN"}), 403

    try:
        # Load all committed records
        print("[UPDATE] Loading committed records...")
        all_records = storage.list_filtered(committed=True)
        print(f"[UPDATE] Found {len(all_records)} committed records")

        # Find records that need updating
        updates_by_domain = {}
        total_updated = 0

        for record in all_records:
            domain = record.get('root_domain', '')
            if domain in DOMAIN_UPDATES:
                current_entity = record.get('entity_name', '')
                new_entity = DOMAIN_UPDATES[domain]

                if current_entity != new_entity:
                    # Update entity name
                    record['entity_name'] = new_entity
                    record['metadata_source'] = 'domain_mapped'

                    # Save updated record
                    storage.save(record)

                    total_updated += 1
                    if domain not in updates_by_domain:
                        updates_by_domain[domain] = 0
                    updates_by_domain[domain] += 1

                    if total_updated % 50 == 0:
                        print(f"[UPDATE] Updated {total_updated} records...")

        print(f"[UPDATE] Complete! Updated {total_updated} records")

        return jsonify({
            "ok": True,
            "updated": total_updated,
            "by_domain": updates_by_domain,
            "mappings": DOMAIN_UPDATES
        })

    except Exception as e:
        print(f"[UPDATE] Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500
