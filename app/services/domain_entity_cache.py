"""
Persistent domain-to-entity mapping cache.

This module provides a growing cache that ensures all forms from the same domain
get the same normalized "pretty" entity name via LLM.

Cache Structure:
{
  "domain.com": {
    "entity_name": "Pretty Entity Name",
    "generated_at": "2025-11-05T10:30:00Z",
    "source": "llm" | "default_mappings"
  }
}
"""
import os
import json
import threading
from typing import Optional, Dict, Any
from datetime import datetime
from pathlib import Path

from app.services.llm_router import chat_complete, LLMError

# Cache file location
CACHE_DIR = Path(__file__).parent.parent.parent / "data"
CACHE_FILE = CACHE_DIR / "domain_entity_cache.json"

# Thread-safe cache access. RLock (reentrant) because cache_entity()
# acquires the lock then calls load_cache() which also acquires the same
# lock — non-reentrant Lock() deadlocks the analyzer on every cache miss.
_cache_lock = threading.RLock()
_cache: Dict[str, Dict[str, Any]] = {}
_cache_loaded = False


def _ensure_cache_dir():
    """Ensure cache directory exists"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def load_cache() -> Dict[str, Dict[str, Any]]:
    """
    Load domain-entity cache from file.

    Returns:
        Dict mapping domain -> entity metadata
    """
    global _cache, _cache_loaded

    with _cache_lock:
        if _cache_loaded:
            return _cache.copy()

        _ensure_cache_dir()

        if CACHE_FILE.exists():
            try:
                with open(CACHE_FILE, 'r') as f:
                    _cache = json.load(f)
                print(f"[CACHE] Loaded {len(_cache)} domains from {CACHE_FILE}")
            except Exception as e:
                print(f"[CACHE] Error loading cache: {e}")
                _cache = {}
        else:
            _cache = {}
            print(f"[CACHE] No cache file found, starting empty")

        _cache_loaded = True
        return _cache.copy()


def save_cache():
    """
    Save domain-entity cache to file (thread-safe).
    """
    with _cache_lock:
        _ensure_cache_dir()
        try:
            with open(CACHE_FILE, 'w') as f:
                json.dump(_cache, f, indent=2, sort_keys=True)
            print(f"[CACHE] Saved {len(_cache)} domains to {CACHE_FILE}")
        except Exception as e:
            print(f"[CACHE] Error saving cache: {e}")


def get_cached_entity(domain: str) -> Optional[str]:
    """
    Get entity name from cache for a given domain.

    Args:
        domain: Root domain (e.g., "gsa.gov", "schwab.com")

    Returns:
        Entity name or None if not cached
    """
    cache = load_cache()
    entry = cache.get(domain.lower())
    if entry:
        return entry.get("entity_name")
    return None


def generate_entity_name_via_llm(domain: str, sample_url: Optional[str] = None) -> Optional[str]:
    """
    Generate a normalized "pretty" entity name for a domain using LLM.

    Args:
        domain: Root domain (e.g., "gsa.gov")
        sample_url: Optional sample URL from this domain for context

    Returns:
        Pretty entity name or None on error
    """
    prompt = f"""Given the domain "{domain}", extract the official organization name.

Rules:
1. Return the FULL official name (e.g., "U.S. General Services Administration", not "GSA")
2. Use proper capitalization and punctuation
3. For government agencies, include country prefix (e.g., "U.S." for US agencies)
4. For companies, use official corporate name
5. Be consistent with common conventions

Examples:
- gsa.gov → U.S. General Services Administration
- irs.gov → Internal Revenue Service
- schwab.com → Charles Schwab Corporation
- fidelity.com → Fidelity Investments
- becu.org → Boeing Employees Credit Union

Domain: {domain}
"""
    if sample_url:
        prompt += f"\nSample URL for context: {sample_url}"

    prompt += "\n\nReturn ONLY the entity name, nothing else."

    try:
        messages = [
            {"role": "system", "content": "You are an expert at identifying official organization names from domains."},
            {"role": "user", "content": prompt}
        ]

        # Don't pass an explicit model: chat_complete picks the right default
        # for whatever provider LLM_PROVIDER resolves to (and per-provider
        # defaults on fallback). Hard-coding OPENAI_MODEL here was breaking
        # Gemini calls by sending 'gpt-4o-mini' to the Gemini endpoint.
        response = chat_complete(
            provider=os.getenv("LLM_PROVIDER", "openai"),
            model="",
            messages=messages,
            max_tokens=100,
            temperature=0.3  # Low temperature for consistent naming
        )

        if response and isinstance(response, str):
            entity_name = response.strip()
            if entity_name and len(entity_name) > 2:
                print(f"[CACHE] LLM generated entity for {domain}: {entity_name}")
                return entity_name

        print(f"[CACHE] LLM returned invalid response for {domain}")
        return None

    except LLMError as e:
        print(f"[CACHE] LLM error for {domain}: {e}")
        return None
    except Exception as e:
        print(f"[CACHE] Unexpected error for {domain}: {e}")
        return None


def cache_entity(domain: str, entity_name: str, source: str = "llm"):
    """
    Cache an entity name for a domain.

    Args:
        domain: Root domain
        entity_name: Pretty entity name
        source: Where this entity came from ("llm", "default_mappings", "manual")
    """
    with _cache_lock:
        load_cache()  # Ensure cache is loaded
        _cache[domain.lower()] = {
            "entity_name": entity_name,
            "generated_at": datetime.now().isoformat(),
            "source": source
        }
        save_cache()
        print(f"[CACHE] Cached {domain} → {entity_name} (source: {source})")


def get_or_generate_entity(domain: str, sample_url: Optional[str] = None) -> Optional[str]:
    """
    Get entity name from cache, or generate via LLM if not cached.

    This is the main function to use for domain-first entity naming.

    Args:
        domain: Root domain
        sample_url: Optional sample URL for LLM context

    Returns:
        Entity name or None
    """
    # Check cache first
    cached = get_cached_entity(domain)
    if cached:
        print(f"[CACHE] Hit: {domain} → {cached}")
        return cached

    # Not cached - generate via LLM
    print(f"[CACHE] Miss: {domain}, generating via LLM...")
    entity_name = generate_entity_name_via_llm(domain, sample_url)

    if entity_name:
        # Cache it for future use
        cache_entity(domain, entity_name, source="llm")
        return entity_name

    print(f"[CACHE] Failed to generate entity for {domain}")
    return None


def seed_cache_from_default_mappings():
    """
    Seed cache with entries from domain_mappings.DEFAULT_MAPPINGS.

    This should be run once to populate the cache with known domains.
    """
    from app.services.domain_mappings import DEFAULT_MAPPINGS

    with _cache_lock:
        load_cache()
        added = 0

        for domain, metadata in DEFAULT_MAPPINGS.items():
            entity_name = metadata.get("entity_name")
            if entity_name and domain.lower() not in _cache:
                _cache[domain.lower()] = {
                    "entity_name": entity_name,
                    "generated_at": datetime.now().isoformat(),
                    "source": "default_mappings"
                }
                added += 1

        if added > 0:
            save_cache()
            print(f"[CACHE] Seeded {added} domains from DEFAULT_MAPPINGS")


def get_cache_stats() -> Dict[str, Any]:
    """
    Get statistics about the cache.

    Returns:
        Dict with cache stats (size, sources breakdown, etc.)
    """
    cache = load_cache()
    sources = {}
    for entry in cache.values():
        source = entry.get("source", "unknown")
        sources[source] = sources.get(source, 0) + 1

    return {
        "total_domains": len(cache),
        "sources": sources,
        "cache_file": str(CACHE_FILE),
        "cache_exists": CACHE_FILE.exists()
    }


# Note: Cache seeding must be done manually via seed_cache_from_default_mappings()
# to avoid circular imports at module load time.
