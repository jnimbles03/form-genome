"""
Configurable scoring weights for complexity and NIGO calculations.

Allows admin to adjust all weight values and save them to database.

Weights are loaded lazily on first use (see _ensure_loaded) to avoid
SQLite-lock contention during multi-worker boot. The previous behavior of
opening the SQLite DB at module import caused every Gunicorn worker to race
on the same file at startup; lazy init defers that work until the first call
to get_complexity_weights() / get_nigo_weights().
"""
import json
import logging
import sqlite3
import threading
from typing import Dict, Any
import os

logger = logging.getLogger(__name__)

# Default weights for complexity scoring
DEFAULT_COMPLEXITY_WEIGHTS = {
    # === TIER 1: Human Coordination (Hardest) ===
    "notarization": 25.0,
    "third_party": 18.0,
    "witness_per_signature": 15.0,
    "witness_max_points": 15.0,

    # === TIER 2: Process Complexity ===
    "conditional_logic": 10.0,
    "attachments_base": 8.0,
    "attachments_per_count": 2.0,
    "attachments_max_points": 6.0,
    "payment_required": 6.0,
    "identification": 7.0,
    "data_validation_per_field": 1.5,
    "data_validation_max_points": 9.0,
    "dependencies": 6.0,
    "deadlines": 4.0,

    # === TIER 3: Data Entry Options ===
    "radio_per_group": 1.5,
    "radio_max_points": 5.0,

    # === TIER 4: Volume Indicators ===
    "pages_per_page": 1.2,
    "pages_max_points": 6.0,
    "fields_per_field": 0.15,
    "fields_max_points": 8.0,

    # === Other Factors ===
    "unique_roles_per_role": 4.0,
    "unique_roles_max_points": 8.0,
    "signature_base": 2.0,
    "pii_per_hit": 0.8,
    "pii_max_points": 6.0,
    "drawings_high_threshold": 30.0,
    "drawings_high_points": 3.0,
    "drawings_medium_threshold": 15.0,
    "drawings_medium_points": 2.0,
    "drawings_low_threshold": 5.0,
    "drawings_low_points": 1.0,
    "images_high_threshold": 2.0,
    "images_high_points": 2.0,
    "images_low_threshold": 1.0,
    "images_low_points": 1.0,
    "xfa_points": 3.0,
}

# Default weights for NIGO risk scoring
DEFAULT_NIGO_WEIGHTS = {
    # === HIGH RISK ===
    "data_validation_per_field": 3.0,
    "data_validation_max_points": 18.0,
    "attachments_base": 5.0,
    "attachments_per_count": 2.5,
    "attachments_max_points": 10.0,
    "notarization": 4.0,
    "payment_required": 3.5,
    "identification": 3.0,
    "deadlines": 3.0,

    # === MEDIUM RISK ===
    "conditional_logic": 2.5,
    "dependencies": 2.0,
    "witness_per_signature": 2.0,
    "witness_max_points": 4.0,
    "third_party": 2.0,

    # === LOW RISK ===
    "unique_roles_per_role": 0.5,
    "unique_roles_max_points": 2.0,
    "radio_per_group": 0.25,
    "radio_max_points": 1.5,
    "pages_per_page": 0.1,
    "pages_max_points": 3.0,
    "fields_per_field": 0.05,
    "fields_max_points": 3.0,
}

# In-memory cache of weights
_WEIGHTS_CACHE = {
    "complexity": DEFAULT_COMPLEXITY_WEIGHTS.copy(),
    "nigo": DEFAULT_NIGO_WEIGHTS.copy(),
}

def get_db_path() -> str:
    """Get database path from environment or use default."""
    return os.getenv("DB_PATH", "/workspace/data/formgenome.db")

def init_weights_table():
    """Initialize the scoring_weights table in the database if it doesn't exist."""
    db_path = get_db_path()

    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()

            # Create table if it doesn't exist
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS scoring_weights (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    score_type TEXT NOT NULL,  -- 'complexity' or 'nigo'
                    weights_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
            """)

            # Check if weights exist, if not insert defaults
            cursor.execute("SELECT COUNT(*) FROM scoring_weights")
            count = cursor.fetchone()[0]

            if count == 0:
                import time
                timestamp = int(time.time())

                # Insert default complexity weights
                cursor.execute("""
                    INSERT INTO scoring_weights (score_type, weights_json, updated_at)
                    VALUES (?, ?, ?)
                """, ("complexity", json.dumps(DEFAULT_COMPLEXITY_WEIGHTS), timestamp))

                # Insert default NIGO weights
                cursor.execute("""
                    INSERT INTO scoring_weights (score_type, weights_json, updated_at)
                    VALUES (?, ?, ?)
                """, ("nigo", json.dumps(DEFAULT_NIGO_WEIGHTS), timestamp))

                conn.commit()
                print("[WEIGHTS] Initialized default scoring weights in database")

    except Exception as e:
        print(f"[WEIGHTS] Error initializing weights table: {e}")
        # If DB fails, just use defaults from memory

def load_weights_from_db():
    """Load weights from database into memory cache."""
    db_path = get_db_path()

    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()

            # Load complexity weights
            cursor.execute("""
                SELECT weights_json FROM scoring_weights
                WHERE score_type = 'complexity'
                ORDER BY updated_at DESC
                LIMIT 1
            """)
            row = cursor.fetchone()
            if row:
                _WEIGHTS_CACHE["complexity"] = json.loads(row[0])

            # Load NIGO weights
            cursor.execute("""
                SELECT weights_json FROM scoring_weights
                WHERE score_type = 'nigo'
                ORDER BY updated_at DESC
                LIMIT 1
            """)
            row = cursor.fetchone()
            if row:
                _WEIGHTS_CACHE["nigo"] = json.loads(row[0])

    except Exception as e:
        print(f"[WEIGHTS] Error loading weights from database: {e}, using defaults")
        # If DB fails, use defaults

def save_weights_to_db(score_type: str, weights: Dict[str, float]):
    """Save weights to database."""
    db_path = get_db_path()

    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            import time
            timestamp = int(time.time())

            # Insert new weights record
            cursor.execute("""
                INSERT INTO scoring_weights (score_type, weights_json, updated_at)
                VALUES (?, ?, ?)
            """, (score_type, json.dumps(weights), timestamp))

            conn.commit()

            # Update cache
            _WEIGHTS_CACHE[score_type] = weights.copy()
            print(f"[WEIGHTS] Saved {score_type} weights to database")

    except Exception as e:
        print(f"[WEIGHTS] Error saving weights to database: {e}")
        raise

# Lazy-load state. Sentinel values:
#   False  -> not yet attempted
#   True   -> successfully loaded
#   "error" -> attempted and failed; do not retry on every call
_weights_loaded: Any = False
_weights_lock = threading.Lock()


def _ensure_loaded() -> None:
    """Initialize the weights table and load weights from DB on first use.

    Subsequent calls are no-ops. Errors are logged once and the in-memory
    defaults are used; the sentinel prevents repeated DB hits on a hot path.
    """
    global _weights_loaded
    if _weights_loaded:  # True or "error" both short-circuit
        return
    with _weights_lock:
        if _weights_loaded:
            return
        try:
            init_weights_table()
            load_weights_from_db()
            _weights_loaded = True
        except Exception as e:
            logger.error(
                "[WEIGHTS] Lazy initialization failed; falling back to defaults: %s",
                e,
            )
            _weights_loaded = "error"


def get_complexity_weights() -> Dict[str, float]:
    """Get current complexity scoring weights."""
    _ensure_loaded()
    return _WEIGHTS_CACHE["complexity"].copy()

def get_nigo_weights() -> Dict[str, float]:
    """Get current NIGO scoring weights."""
    _ensure_loaded()
    return _WEIGHTS_CACHE["nigo"].copy()

def set_complexity_weights(weights: Dict[str, float]):
    """Set complexity weights (saves to database)."""
    save_weights_to_db("complexity", weights)

def set_nigo_weights(weights: Dict[str, float]):
    """Set NIGO weights (saves to database)."""
    save_weights_to_db("nigo", weights)

def reset_to_defaults():
    """Reset all weights to default values."""
    save_weights_to_db("complexity", DEFAULT_COMPLEXITY_WEIGHTS)
    save_weights_to_db("nigo", DEFAULT_NIGO_WEIGHTS)
    print("[WEIGHTS] Reset all weights to defaults")

# NOTE: We intentionally do NOT call init_weights_table() / load_weights_from_db()
# at module import. Weights load lazily via _ensure_loaded() on first
# get_complexity_weights() / get_nigo_weights() call to avoid SQLite-lock
# contention during multi-worker boot.
