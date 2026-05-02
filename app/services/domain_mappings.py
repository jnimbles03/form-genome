"""
Domain-based metadata mappings.

Automatically populate entity_name, vertical, and subvertical based on domain.
All forms from the same root domain inherit the same metadata.
"""
import re
from typing import Optional, Dict, Any
from urllib.parse import urlparse
import json


def extract_root_domain(url: str) -> Optional[str]:
    """
    Extract root domain from URL.

    Examples:
      https://www.pa.gov/forms/page.pdf -> pa.gov
      https://subdomain.schwab.com/docs/form.pdf -> schwab.com
      https://opm.gov/forms/SF-86.pdf -> opm.gov

    Returns:
      Root domain or None
    """
    if not url:
        return None

    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""

        # Remove www. prefix
        hostname = re.sub(r'^www\.', '', hostname, flags=re.IGNORECASE)

        # Extract root domain (last 2 parts for .com/.gov/.org, last 3 for .co.uk)
        parts = hostname.split('.')

        if len(parts) < 2:
            return None

        # Handle country-code TLDs (e.g., .co.uk, .gov.au)
        if len(parts) >= 3 and parts[-2] in ('co', 'gov', 'com', 'org', 'ac'):
            root = '.'.join(parts[-3:])
        else:
            root = '.'.join(parts[-2:])

        return root.lower()

    except Exception:
        return None


def get_region_from_domain(domain: str) -> str:
    """
    Determine geographic region from domain TLD.

    Regions:
      - NA (North America)
      - LATAM (Latin America)
      - EMEA (Europe, Middle East, Africa)
      - APJ (Asia Pacific Japan)

    Args:
        domain: Root domain (e.g., "schwab.com", "transport.nsw.gov.au")

    Returns:
        Region code: "NA", "LATAM", "EMEA", or "APJ"
    """
    if not domain:
        return "NA"  # Default to NA if no domain

    domain = domain.lower()

    # Extract TLD (last part after final dot)
    parts = domain.split('.')
    if len(parts) < 2:
        return "NA"

    # Handle country-code TLDs (e.g., .gov.au, .co.uk)
    # Use last 2 parts for country code TLDs
    tld = parts[-1]
    country_code = parts[-2] if len(parts) >= 2 else None

    # LATAM (Latin America & Caribbean)
    latam_tlds = {
        'ar', 'bo', 'br', 'cl', 'co', 'cr', 'cu', 'do', 'ec', 'sv', 'gt',
        'hn', 'mx', 'ni', 'pa', 'py', 'pe', 'pr', 'uy', 've', 'bz', 'gy',
        'sr', 'jm', 'tt', 'bb', 'ht'
    }

    # EMEA (Europe, Middle East, Africa)
    emea_tlds = {
        # Europe
        'uk', 'de', 'fr', 'it', 'es', 'nl', 'be', 'ch', 'at', 'se', 'no',
        'dk', 'fi', 'pl', 'cz', 'hu', 'ro', 'pt', 'gr', 'ie', 'bg', 'sk',
        'hr', 'lt', 'lv', 'ee', 'si', 'cy', 'mt', 'lu', 'is', 'li', 'mc',
        'ad', 'sm', 'va', 'rs', 'me', 'mk', 'al', 'ba', 'md', 'ua', 'by',
        'ru',  # Russia spans both EMEA and APJ, but typically grouped in EMEA
        # Middle East
        'ae', 'sa', 'il', 'tr', 'qa', 'kw', 'om', 'bh', 'jo', 'lb', 'iq',
        'ye', 'sy', 'ps', 'ir',
        # Africa
        'za', 'eg', 'ng', 'ke', 'ma', 'tn', 'gh', 'ci', 'sn', 'ug', 'et',
        'tz', 'zw', 'zm', 'mw', 'bw', 'na', 'mu', 'rw', 'ao', 'mz', 'cm',
        'mg', 'zr', 'sd', 'dz', 'ly'
    }

    # APJ (Asia Pacific Japan)
    apj_tlds = {
        # Asia
        'au', 'nz', 'jp', 'cn', 'in', 'sg', 'kr', 'hk', 'tw', 'my', 'th',
        'id', 'ph', 'vn', 'pk', 'bd', 'lk', 'np', 'mm', 'kh', 'la', 'bn',
        'mn', 'kz', 'uz', 'tm', 'tj', 'kg', 'af',
        # Pacific
        'fj', 'pg', 'sb', 'vu', 'nc', 'pf', 'ws', 'to', 'ki', 'fm', 'mh',
        'pw', 'nr', 'tv'
    }

    # Check TLD against region sets
    if tld in latam_tlds:
        return "LATAM"
    elif tld in emea_tlds:
        return "EMEA"
    elif tld in apj_tlds:
        return "APJ"

    # Default to NA for .com, .org, .gov, .edu, .us, .ca, and unknown TLDs
    # Most .com domains are US-based
    return "NA"


# Default domain mappings (can be extended via database)
#
# STRICT RULES - Sub-verticals MUST match analyzer.py classify_industry() function:
#
# Financial Services:
#   - Banking (banks, credit unions)
#   - Wealth Management (brokerages, investment firms, asset managers)
#   - P&C Insurance (property & casualty insurance)
#
# Healthcare:
#   - Payer (health insurance)
#   - Provider (hospitals, clinics)
#   - Life Sciences (pharma, biotech, medtech)
#
# Public Sector:
#   - Federal (federal government agencies)
#   - State & Local (state and local government)
#   - Education (schools, universities)
#   - Not-for-Profit (nonprofits, charities, foundations)
#
DEFAULT_MAPPINGS = {
    # ===== PUBLIC SECTOR - FEDERAL =====
    "opm.gov": {
        "entity_name": "U.S. Office of Personnel Management",
        "industry_vertical": "Public Sector",
        "industry_subvertical": "Federal"  # FIXED: Was "Federal Government"
    },
    "irs.gov": {
        "entity_name": "Internal Revenue Service",
        "industry_vertical": "Public Sector",
        "industry_subvertical": "Federal"  # FIXED: Was "Federal Government"
    },
    "uscis.gov": {
        "entity_name": "U.S. Citizenship and Immigration Services",
        "industry_vertical": "Public Sector",
        "industry_subvertical": "Federal"  # FIXED: Was "Federal Government"
    },
    "sba.gov": {
        "entity_name": "U.S. Small Business Administration",
        "industry_vertical": "Public Sector",
        "industry_subvertical": "Federal"  # FIXED: Was "Federal Government"
    },
    "gsa.gov": {
        "entity_name": "U.S. General Services Administration",
        "industry_vertical": "Public Sector",
        "industry_subvertical": "Federal"  # FIXED: Was "Federal Government"
    },
    "mycreditunion.gov": {
        "entity_name": "National Credit Union Administration",
        "industry_vertical": "Public Sector",
        "industry_subvertical": "Federal"
    },
    "egov.usda.gov": {
        "entity_name": "U.S. Department of Agriculture",
        "industry_vertical": "Public Sector",
        "industry_subvertical": "Federal"
    },

    # ===== PUBLIC SECTOR - STATE & LOCAL =====
    "pa.gov": {
        "entity_name": "Commonwealth of Pennsylvania",
        "industry_vertical": "Public Sector",
        "industry_subvertical": "State & Local"  # FIXED: Was "State Government"
    },
    "ny.gov": {
        "entity_name": "State of New York",
        "industry_vertical": "Public Sector",
        "industry_subvertical": "State & Local"
    },
    "ca.gov": {
        "entity_name": "State of California",
        "industry_vertical": "Public Sector",
        "industry_subvertical": "State & Local"
    },
    "wv.gov": {
        "entity_name": "State of West Virginia",
        "industry_vertical": "Public Sector",
        "industry_subvertical": "State & Local"
    },
    "jacksonville.gov": {
        "entity_name": "City of Jacksonville",
        "industry_vertical": "Public Sector",
        "industry_subvertical": "State & Local"
    },
    "coj.net": {
        "entity_name": "City of Jacksonville",
        "industry_vertical": "Public Sector",
        "industry_subvertical": "State & Local"
    },
    "in.gov": {
        "entity_name": "State of Indiana",
        "industry_vertical": "Public Sector",
        "industry_subvertical": "State & Local"
    },
    "mt.gov": {
        "entity_name": "State of Montana",
        "industry_vertical": "Public Sector",
        "industry_subvertical": "State & Local"
    },
    "fairfaxcounty.gov": {
        "entity_name": "Fairfax County",
        "industry_vertical": "Public Sector",
        "industry_subvertical": "State & Local"
    },
    "clevelandohio.gov": {
        "entity_name": "City of Cleveland",
        "industry_vertical": "Public Sector",
        "industry_subvertical": "State & Local"
    },
    "nsw.gov.au": {
        "entity_name": "New South Wales Government",
        "industry_vertical": "Public Sector",
        "industry_subvertical": "State & Local"
    },

    # ===== PUBLIC SECTOR - EDUCATION =====
    "esu.edu": {
        "entity_name": "East Stroudsburg University",
        "industry_vertical": "Public Sector",
        "industry_subvertical": "Education"
    },
    "sru.edu": {
        "entity_name": "Slippery Rock University",
        "industry_vertical": "Public Sector",
        "industry_subvertical": "Education"
    },
    "kutztown.edu": {
        "entity_name": "Kutztown University",
        "industry_vertical": "Public Sector",
        "industry_subvertical": "Education"
    },
    "ship.edu": {
        "entity_name": "Shippensburg University",
        "industry_vertical": "Public Sector",
        "industry_subvertical": "Education"
    },
    "wcupa.edu": {
        "entity_name": "West Chester University",
        "industry_vertical": "Public Sector",
        "industry_subvertical": "Education"
    },
    "iup.edu": {
        "entity_name": "Indiana University of Pennsylvania",
        "industry_vertical": "Public Sector",
        "industry_subvertical": "Education"
    },

    # ===== FINANCIAL SERVICES - WEALTH MANAGEMENT =====
    "schwab.com": {
        "entity_name": "Charles Schwab Corporation",
        "industry_vertical": "Financial Services",
        "industry_subvertical": "Wealth Management"  # FIXED: Was "Brokerage"
    },
    "fidelity.com": {
        "entity_name": "Fidelity Investments",
        "industry_vertical": "Financial Services",
        "industry_subvertical": "Wealth Management"  # FIXED: Was "Asset Management"
    },
    "nationwidefinancial.com": {
        "entity_name": "Nationwide Financial",
        "industry_vertical": "Financial Services",
        "industry_subvertical": "Wealth Management"
    },

    # ===== FINANCIAL SERVICES - BANKING =====
    # Credit Unions
    "aagcu.org": {
        "entity_name": "Alaska Air Group Credit Union",
        "industry_vertical": "Financial Services",
        "industry_subvertical": "Banking"  # CORRECT
    },
    "becu.org": {
        "entity_name": "Boeing Employees Credit Union",
        "industry_vertical": "Financial Services",
        "industry_subvertical": "Banking"  # CORRECT
    },
    "schoolsfirstfcu.org": {
        "entity_name": "SchoolsFirst Federal Credit Union",
        "industry_vertical": "Financial Services",
        "industry_subvertical": "Banking"
    },
    "gtfcu.org": {
        "entity_name": "Greater Texas Federal Credit Union",
        "industry_vertical": "Financial Services",
        "industry_subvertical": "Banking"  # CORRECT
    },
    "texasbaycu.org": {
        "entity_name": "Texas Bay Area Credit Union",
        "industry_vertical": "Financial Services",
        "industry_subvertical": "Banking"  # CORRECT
    },
    "cccu.com": {
        "entity_name": "Central California Credit Union",
        "industry_vertical": "Financial Services",
        "industry_subvertical": "Banking"  # CORRECT
    },
    "sdccu.com": {
        "entity_name": "San Diego County Credit Union",
        "industry_vertical": "Financial Services",
        "industry_subvertical": "Banking"
    },

    # ===== HEALTHCARE - PAYER =====
    "uhc.com": {
        "entity_name": "UnitedHealthcare",
        "industry_vertical": "Healthcare",
        "industry_subvertical": "Payer"
    },

    # ===== TRANSPORTATION =====
    "illinoistollway.com": {
        "entity_name": "Illinois Tollway",
        "industry_vertical": "Public Sector",
        "industry_subvertical": "State & Local"
    },
}


def get_domain_metadata(url: str) -> Optional[Dict[str, str]]:
    """
    Get metadata for a domain from URL.

    Args:
        url: Source URL of form

    Returns:
        Dict with entity_name, industry_vertical, industry_subvertical or None
    """
    domain = extract_root_domain(url)
    if not domain:
        return None

    # Check default mappings
    return DEFAULT_MAPPINGS.get(domain)


def apply_domain_metadata(record: Dict[str, Any], url: str) -> Dict[str, Any]:
    """
    Apply domain-based metadata to a record.

    Domain mappings take PRECEDENCE over LLM-generated metadata to ensure
    batch consistency - all forms from the same domain get the same entity_name.

    For CDN domains (HubSpot, CloudFront, etc.), use LLM-suggested entity name
    as fallback since the CDN domain doesn't represent the actual organization.

    Args:
        record: Form record to update
        url: Source URL

    Returns:
        Updated record
    """
    # Extract root domain for consistency
    root_domain = extract_root_domain(url)
    if not root_domain:
        return record

    # Store the root domain
    record["root_domain"] = root_domain

    # Capture LLM-suggested entity name before overriding
    llm_entity = record.get("entity_name")

    # Check if domain has explicit mapping
    metadata = get_domain_metadata(url)

    if metadata:
        # Domain mapping exists in DEFAULT_MAPPINGS - use mapped values
        domain_entity = metadata.get("entity_name")
        record["entity_name"] = domain_entity
        record["industry_vertical"] = metadata.get("industry_vertical")
        record["industry_subvertical"] = metadata.get("industry_subvertical")
        record["metadata_source"] = "domain_mapped"
    else:
        # No DEFAULT_MAPPINGS entry - check persistent cache
        from app.services.domain_entity_cache import get_or_generate_entity

        # Check if this is a CDN domain
        cdn_domains = [
            'hubspotusercontent', 'cloudfront', 'amazonaws', 'azureedge',
            'googleusercontent', 'cloudflare-ipfs', 'jsdelivr', 'unpkg'
        ]
        is_cdn = any(cdn in root_domain.lower() for cdn in cdn_domains)

        if is_cdn:
            # CDN domain - use LLM-suggested entity (form owner, not CDN)
            if llm_entity:
                domain_entity = llm_entity
                record["entity_name"] = domain_entity
                record["metadata_source"] = "llm_fallback"
                record["is_cdn_hosted"] = True
            else:
                # No LLM entity - this shouldn't happen but fallback to auto-generate
                domain_entity = auto_generate_entity_name(root_domain)
                record["entity_name"] = domain_entity
                record["metadata_source"] = "domain_auto"
        else:
            # Regular domain - check cache or generate via LLM
            cached_entity = get_or_generate_entity(root_domain, url)

            if cached_entity:
                # Use cached/generated entity (ensures consistency across domain)
                domain_entity = cached_entity
                record["entity_name"] = cached_entity
                record["metadata_source"] = "domain_cached"
            elif llm_entity:
                # Cache lookup failed but we have per-form LLM entity - use it
                domain_entity = llm_entity
                record["entity_name"] = llm_entity
                record["metadata_source"] = "llm_suggested"
            else:
                # Last resort - auto-generate from domain
                domain_entity = auto_generate_entity_name(root_domain)
                record["entity_name"] = domain_entity
                record["metadata_source"] = "domain_auto"

    # INTERMEDIARY BUSINESS MODEL DETECTION
    # Detect when website owner (hosting entity) differs from form owner
    # Example: IRS form hosted on Charles Schwab website

    # Store both entities for transparency
    record["hosting_entity"] = domain_entity  # Who owns the website
    record["form_owner_entity"] = llm_entity if llm_entity else domain_entity  # Who owns the form

    # Known form owners (government agencies, etc.)
    KNOWN_FORM_OWNERS = {
        'internal revenue service', 'irs',
        'social security administration', 'ssa',
        'u.s. office of personnel management', 'opm',
        'u.s. general services administration', 'gsa',
        'u.s. citizenship and immigration services', 'uscis',
        'u.s. small business administration', 'sba',
        'u.s. department of defense', 'dod',
        'u.s. department of veterans affairs', 'va',
        'u.s. food and drug administration', 'fda',
        'u.s. environmental protection agency', 'epa',
        'centers for medicare & medicaid services', 'cms',
        'federal deposit insurance corporation', 'fdic',
        'securities and exchange commission', 'sec',
    }

    # Check if LLM detected a different entity (intermediary model)
    if llm_entity and domain_entity:
        # Normalize for comparison
        llm_norm = llm_entity.lower().strip()
        domain_norm = domain_entity.lower().strip()

        # Check if LLM entity is a known form owner (like IRS)
        is_known_form_owner = any(owner in llm_norm for owner in KNOWN_FORM_OWNERS)

        # Different entities = intermediary business model
        # Be strict: entities must match closely to consider same_domain=True
        entities_match = (
            llm_norm == domain_norm or
            llm_norm in domain_norm or
            domain_norm in llm_norm
        )

        if not entities_match:
            # Keep same_domain as URL-based (set by analyzer._domains_match)
            # Don't overwrite it based on entity matching
            record["llm_suggested_entity"] = llm_entity  # Preserve for reference
            record["is_intermediary_model"] = True  # Flag intermediary business model

            # Add extra context if it's a known form owner
            if is_known_form_owner:
                record["intermediary_type"] = "government_form_on_private_site"
            else:
                record["intermediary_type"] = "third_party_form"
        else:
            # Entities match - not an intermediary model
            record["is_intermediary_model"] = False
    else:
        # No LLM entity detected - can't determine intermediary status
        record["is_intermediary_model"] = False

    # REGION DETECTION
    # Determine geographic region based on domain TLD
    record["region"] = get_region_from_domain(root_domain)

    return record


def auto_generate_entity_name(domain: str) -> str:
    """
    Auto-generate a clean entity name from a root domain.

    Examples:
        schwab.com -> Charles Schwab
        becu.org -> BECU
        pa.gov -> Pennsylvania
        irs.gov -> IRS

    Args:
        domain: Root domain (e.g., "example.com")

    Returns:
        Generated entity name
    """
    if not domain:
        return "Unknown"

    # Extract the part before TLD
    parts = domain.split('.')
    if len(parts) < 2:
        return domain.title()

    name_part = parts[0]

    # Special cases for common abbreviations (keep uppercase)
    abbrev_upper = {
        'irs': 'IRS',
        'gsa': 'GSA',
        'dod': 'DOD',
        'hhs': 'HHS',
        'cms': 'CMS',
        'fda': 'FDA',
        'epa': 'EPA',
        'sec': 'SEC',
        'fdic': 'FDIC',
        'opm': 'OPM',
        'sba': 'SBA',
        'uscis': 'USCIS',
        'becu': 'BECU',
        'nasa': 'NASA',
        'noaa': 'NOAA',
        'usda': 'USDA',
        'va': 'VA',
        'hud': 'HUD',
    }

    # Check if it's a known abbreviation
    if name_part in abbrev_upper:
        return abbrev_upper[name_part]

    # Check for state abbreviations in .gov domains
    if domain.endswith('.gov'):
        state_names = {
            'pa': 'Pennsylvania',
            'ca': 'California',
            'ny': 'New York',
            'tx': 'Texas',
            'fl': 'Florida',
            'il': 'Illinois',
            'az': 'Arizona',
            'wa': 'Washington',
            'ma': 'Massachusetts',
            'ga': 'Georgia',
        }
        if name_part in state_names:
            return state_names[name_part]

    # Default: title case, replace hyphens/underscores with spaces
    clean_name = name_part.replace('-', ' ').replace('_', ' ').title()

    return clean_name


def add_domain_mapping(domain: str, entity_name: str, vertical: str, subvertical: str) -> bool:
    """
    Add a new domain mapping (runtime only - not persisted yet).

    Args:
        domain: Root domain (e.g., "example.com")
        entity_name: Entity/organization name
        vertical: Industry vertical
        subvertical: Industry subvertical

    Returns:
        True if added successfully
    """
    if not domain or not entity_name:
        return False

    # Normalize domain
    domain = domain.lower().strip()
    domain = re.sub(r'^www\.', '', domain)

    DEFAULT_MAPPINGS[domain] = {
        "entity_name": entity_name,
        "industry_vertical": vertical,
        "industry_subvertical": subvertical
    }

    return True


def get_all_mappings() -> Dict[str, Dict[str, str]]:
    """Get all domain mappings."""
    return DEFAULT_MAPPINGS.copy()


def remove_domain_mapping(domain: str) -> bool:
    """
    Remove a domain mapping.

    Args:
        domain: Root domain to remove

    Returns:
        True if removed, False if not found
    """
    domain = domain.lower().strip()
    domain = re.sub(r'^www\.', '', domain)

    if domain in DEFAULT_MAPPINGS:
        del DEFAULT_MAPPINGS[domain]
        return True

    return False


def get_domains_for_records(records: list) -> Dict[str, int]:
    """
    Get count of records per domain.

    Args:
        records: List of form records

    Returns:
        Dict mapping domain -> count
    """
    from collections import Counter

    domains = []
    for record in records:
        url = record.get("source_url", "")
        domain = extract_root_domain(url)
        if domain:
            domains.append(domain)

    return dict(Counter(domains))
