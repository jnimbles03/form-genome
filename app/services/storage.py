# app/services/storage.py
# Dual-mode record store: SQLite (local) or PostgreSQL Cloud SQL (production)
import os, json, logging, threading, time, hashlib, sqlite3
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

# -----------------------------
# Database Mode Detection
# -----------------------------
def _is_postgres_mode() -> bool:
    """Check if we should use PostgreSQL Cloud SQL (vs SQLite)"""
    conn_name = os.environ.get("CLOUD_SQL_CONNECTION_NAME", "").strip()
    return bool(conn_name)

# -----------------------------
# PostgreSQL Cloud SQL Setup
# -----------------------------
_PG_POOL = None
_PG_CONNECTOR = None
_PG_LOCK = threading.Lock()

def _init_postgres():
    """Initialize PostgreSQL Cloud SQL connection"""
    global _PG_POOL, _PG_CONNECTOR
    from google.cloud.sql.connector import Connector

    with _PG_LOCK:
        if _PG_CONNECTOR is not None:
            return

        # Get Cloud SQL connection details from environment.
        # No plaintext fallbacks: any of these missing in PG mode is a configuration
        # bug that should fail loudly at startup, not silently authenticate against
        # production with a stale literal.
        connection_name = os.environ.get("CLOUD_SQL_CONNECTION_NAME")
        db_name = os.environ.get("DB_NAME", "postgres")
        db_user = os.environ.get("DB_USER", "postgres")
        db_password = os.environ.get("DB_PASSWORD")

        if not connection_name:
            raise RuntimeError("CLOUD_SQL_CONNECTION_NAME required for PostgreSQL mode")
        if not db_password:
            raise RuntimeError(
                "DB_PASSWORD required for PostgreSQL mode. "
                "Provide via Secret Manager (--set-secrets DB_PASSWORD=...:latest)."
            )

        logger.info("Initializing PostgreSQL Cloud SQL connection")

        # Initialize Cloud SQL Python Connector
        _PG_CONNECTOR = Connector()

        # Store connection factory function
        def getconn():
            conn = _PG_CONNECTOR.connect(
                connection_name,
                "pg8000",
                user=db_user,
                password=db_password,
                db=db_name,
            )
            return conn

        _PG_POOL = getconn
        print("[STORAGE] PostgreSQL connector initialized")

def _get_pg_conn():
    """Get a PostgreSQL connection"""
    if _PG_POOL is None:
        _init_postgres()
    return _PG_POOL()

def _put_pg_conn(conn):
    """Close PostgreSQL connection"""
    if conn:
        try:
            conn.close()
        except Exception:
            pass

# -----------------------------
# SQLite Setup
# -----------------------------
_SQLITE_PATH = None
_SQLITE_LOCK = threading.Lock()

# -----------------------------
# Background Cloud Storage Sync (SQLite mode only)
# -----------------------------
# These globals coordinate a single daemon thread that periodically uploads
# the local SQLite DB to Cloud Storage instead of doing it inline on every
# save(). See _schedule_cloud_sync() and _cloud_sync_daemon().
_SYNC_DAEMON_LOCK = threading.Lock()
_SYNC_DAEMON_THREAD = None
_SYNC_DIRTY_SINCE: Optional[float] = None  # epoch seconds of first dirty save
_SYNC_LAST_UPLOAD: float = 0.0
_SYNC_INTERVAL_SECS: int = 60  # how often the daemon wakes up

def _schedule_cloud_sync() -> None:
    """
    Mark the local DB as dirty and (lazily) start the background sync daemon.

    Called from save() / batch_save() in SQLite mode. Replaces the previous
    inline call to db_sync.upload_to_cloud(), which held _SQLITE_LOCK and
    re-downloaded the cloud copy on every save.

    Cheap and non-blocking: just records a timestamp and ensures the daemon
    is running.
    """
    global _SYNC_DAEMON_THREAD, _SYNC_DIRTY_SINCE
    now = time.time()
    if _SYNC_DIRTY_SINCE is None:
        _SYNC_DIRTY_SINCE = now

    if _SYNC_DAEMON_THREAD is not None and _SYNC_DAEMON_THREAD.is_alive():
        return

    with _SYNC_DAEMON_LOCK:
        if _SYNC_DAEMON_THREAD is not None and _SYNC_DAEMON_THREAD.is_alive():
            return
        t = threading.Thread(
            target=_cloud_sync_daemon,
            name="storage-cloud-sync",
            daemon=True,
        )
        _SYNC_DAEMON_THREAD = t
        t.start()
        logger.info("Started background Cloud Storage sync daemon")

def _cloud_sync_daemon() -> None:
    """
    Background loop: every _SYNC_INTERVAL_SECS, upload to Cloud Storage if
    there have been saves since the last successful upload.

    Daemon thread (daemon=True) — does not block process exit.
    """
    global _SYNC_DIRTY_SINCE, _SYNC_LAST_UPLOAD
    while True:
        try:
            time.sleep(_SYNC_INTERVAL_SECS)
            dirty_since = _SYNC_DIRTY_SINCE
            if dirty_since is None:
                continue
            if dirty_since <= _SYNC_LAST_UPLOAD:
                # Nothing new since last successful upload.
                continue
            try:
                from app.services import db_sync
                # Snapshot dirty marker before upload so concurrent saves
                # arriving during the upload still trigger a future cycle.
                snapshot = time.time()
                db_sync.upload_to_cloud()
                _SYNC_LAST_UPLOAD = snapshot
                # If no new saves happened during upload, clear the dirty flag.
                if _SYNC_DIRTY_SINCE is not None and _SYNC_DIRTY_SINCE <= snapshot:
                    _SYNC_DIRTY_SINCE = None
            except Exception as e:
                logger.warning("Background cloud sync failed: %s", e)
        except Exception as e:
            # Never let the daemon die.
            logger.exception("Cloud sync daemon loop error: %s", e)

def _init_sqlite(db_path: Optional[str] = None):
    """Initialize SQLite database"""
    global _SQLITE_PATH

    with _SQLITE_LOCK:
        if _SQLITE_PATH is not None:
            return

        # Default path for Cloud Run compatibility
        if db_path:
            _SQLITE_PATH = db_path
        else:
            data_dir = os.path.join(os.path.dirname(__file__), "..", "..", "data")
            os.makedirs(data_dir, exist_ok=True)
            _SQLITE_PATH = os.path.join(data_dir, "formgenome.db")

        print(f"[STORAGE] Initializing SQLite at: {_SQLITE_PATH}")

        # Create tables
        conn = sqlite3.connect(_SQLITE_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        try:
            # Records table
            cur.execute("""
            CREATE TABLE IF NOT EXISTS records(
                id TEXT PRIMARY KEY,
                ts INTEGER NOT NULL,
                source_url TEXT,
                form_name TEXT,
                data TEXT NOT NULL
            );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_records_ts ON records(ts);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_records_source ON records(source_url);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_records_form ON records(form_name);")

            # Login tracking table
            cur.execute("""
            CREATE TABLE IF NOT EXISTS login_logs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                email TEXT NOT NULL,
                name TEXT,
                ip_address TEXT,
                user_agent TEXT
            );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_login_logs_ts ON login_logs(ts);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_login_logs_email ON login_logs(email);")

            # Analysis activity tracking table
            cur.execute("""
            CREATE TABLE IF NOT EXISTS analysis_logs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                email TEXT NOT NULL,
                name TEXT,
                activity_type TEXT NOT NULL,
                source_url TEXT,
                forms_found INTEGER DEFAULT 0,
                forms_analyzed INTEGER DEFAULT 0,
                success INTEGER DEFAULT 1,
                error_message TEXT,
                ip_address TEXT,
                user_agent TEXT
            );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_analysis_logs_ts ON analysis_logs(ts);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_analysis_logs_email ON analysis_logs(email);")

            conn.commit()
        finally:
            cur.close()
            conn.close()

        print("[STORAGE] SQLite database initialized")

def _get_sqlite_conn():
    """Get a SQLite connection"""
    if _SQLITE_PATH is None:
        _init_sqlite()
    conn = sqlite3.connect(_SQLITE_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def _put_sqlite_conn(conn):
    """Close SQLite connection"""
    if conn:
        try:
            conn.close()
        except Exception:
            pass

# -----------------------------
# Unified Connection Interface
# -----------------------------
def init_db(db_path: Optional[str] = None) -> None:
    """
    Initialize database connection (PostgreSQL or SQLite based on environment).
    Safe to call multiple times.

    Environment variables (PostgreSQL mode):
    - CLOUD_SQL_CONNECTION_NAME: formgenome:us-central1:formgenome-db
    - DB_NAME: postgres (default)
    - DB_USER: postgres (default)
    - DB_PASSWORD: (required)
    """
    if _is_postgres_mode():
        _init_postgres()
    else:
        _init_sqlite(db_path)

def _get_conn():
    """Get a database connection (PostgreSQL or SQLite)"""
    if _is_postgres_mode():
        return _get_pg_conn()
    else:
        return _get_sqlite_conn()

def _put_conn(conn):
    """Close database connection"""
    if _is_postgres_mode():
        _put_pg_conn(conn)
    else:
        _put_sqlite_conn(conn)

# -----------------------------
# Helper Functions
# -----------------------------
def _now() -> int:
    return int(time.time())

def _normalize_url(url: str) -> str:
    """
    Normalize a URL for use as a deterministic identity key.

    Lowercases scheme + host, strips default ports (80/443), trims trailing
    slash on the path, and discards URL fragments. Query string is preserved
    because two URLs differing only in query params almost always represent
    different forms (e.g. ?formId=abc vs ?formId=def).
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
        scheme = (parts.scheme or "").lower()
        host = (parts.hostname or "").lower()
        port = parts.port
        # Strip default ports
        if port is not None and not (
            (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
        ):
            netloc = f"{host}:{port}"
        else:
            netloc = host
        path = parts.path or ""
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")
        # Drop fragment, preserve query
        return urlunsplit((scheme, netloc, path, parts.query, ""))
    except Exception:
        return url.strip()

def _make_id(record: Dict[str, Any]) -> str:
    """
    Build a deterministic record id.

    Deterministic IDs are required so re-crawls upsert the existing row
    instead of inserting a duplicate. Callers MUST pass `source_url`
    (or a pre-computed `id`/`_id`).

    Resolution order:
      1. Existing `id` or `_id` field (used as-is).
      2. SHA1 of `_normalize_url(source_url)`, truncated to 16 hex chars.
         16 hex chars = 64 bits of entropy, sufficient for collision
         resistance at our scale (~100k rows).
      3. Fallback: SHA1 of `src|name|now|urandom` for legacy records that
         lack source_url. This is non-deterministic and a logger.warning is
         emitted so the caller is visible. New code should never hit this.
    """
    rid = str(record.get("id") or record.get("_id") or "").strip()
    if rid:
        return rid
    src = str(record.get("source_url") or record.get("url") or "").strip()
    if src:
        normalized = _normalize_url(src)
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]
    # Legacy fallback: should not happen in new code paths.
    name = str(record.get("form_name") or record.get("form_title") or "")
    logger.warning(
        "_make_id called with no source_url and no id; falling back to "
        "non-deterministic id (form_name=%s). Re-crawls will produce duplicates.",
        name[:80],
    )
    blob = f"{src}|{name}|{_now()}|{os.urandom(4).hex()}"
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()

# -----------------------------
# Record Operations
# -----------------------------
def save(record: Dict[str, Any]) -> str:
    """
    Upsert a record; returns its id.
    """
    rid = _make_id(record)
    ts = int(record.get("timestamp") or _now())
    source_url = record.get("source_url") or record.get("url") or None
    form_name = record.get("form_name") or record.get("form_title") or None
    data_json = json.dumps(record, ensure_ascii=False)

    conn = _get_conn()
    cur = conn.cursor()
    try:
        if _is_postgres_mode():
            # PostgreSQL upsert
            cur.execute("""
            INSERT INTO records(id, ts, source_url, form_name, data)
            VALUES(%s, %s, %s, %s, %s)
            ON CONFLICT(id) DO UPDATE SET
                ts=EXCLUDED.ts,
                source_url=EXCLUDED.source_url,
                form_name=EXCLUDED.form_name,
                data=EXCLUDED.data
            """, (rid, ts, source_url, form_name, data_json))
        else:
            # SQLite upsert
            cur.execute("""
            INSERT INTO records(id, ts, source_url, form_name, data)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                ts=excluded.ts,
                source_url=excluded.source_url,
                form_name=excluded.form_name,
                data=excluded.data
            """, (rid, ts, source_url, form_name, data_json))
        conn.commit()
    finally:
        cur.close()
        _put_conn(conn)

    # Sync to Cloud Storage (only for SQLite mode, not PostgreSQL).
    # Schedule a background sync rather than blocking the request.
    if not _is_postgres_mode():
        try:
            _schedule_cloud_sync()
        except Exception as e:
            logger.warning("Failed to schedule cloud sync: %s", e)

    return rid

def batch_save(records: List[Dict[str, Any]]) -> int:
    """
    Batch upsert multiple records in a single transaction.
    Much faster than calling save() individually.

    Args:
        records: List of record dictionaries to save

    Returns:
        Number of records saved
    """
    if not records:
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    count = 0

    try:
        # Prepare all records
        batch_data = []
        for record in records:
            rid = _make_id(record)
            ts = int(record.get("timestamp") or _now())
            source_url = record.get("source_url") or record.get("url") or None
            form_name = record.get("form_name") or record.get("form_title") or None
            data_json = json.dumps(record, ensure_ascii=False)
            batch_data.append((rid, ts, source_url, form_name, data_json))

        if _is_postgres_mode():
            # PostgreSQL batch upsert
            for data in batch_data:
                cur.execute("""
                INSERT INTO records(id, ts, source_url, form_name, data)
                VALUES(%s, %s, %s, %s, %s)
                ON CONFLICT(id) DO UPDATE SET
                    ts=EXCLUDED.ts,
                    source_url=EXCLUDED.source_url,
                    form_name=EXCLUDED.form_name,
                    data=EXCLUDED.data
                """, data)
                count += 1
        else:
            # SQLite batch upsert
            for data in batch_data:
                cur.execute("""
                INSERT INTO records(id, ts, source_url, form_name, data)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    ts=excluded.ts,
                    source_url=excluded.source_url,
                    form_name=excluded.form_name,
                    data=excluded.data
                """, data)
                count += 1

        # Single commit for entire batch
        conn.commit()
    finally:
        cur.close()
        _put_conn(conn)

    # Schedule a background cloud sync (SQLite mode only).
    if not _is_postgres_mode() and count > 0:
        try:
            _schedule_cloud_sync()
        except Exception as e:
            logger.warning("Failed to schedule cloud sync after batch_save: %s", e)

    return count

def list_all() -> List[Dict[str, Any]]:
    conn = _get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, ts, data FROM records ORDER BY ts DESC;")
        results = []
        for row in cur.fetchall():
            db_id = row[0]
            db_ts = row[1]
            record = json.loads(row[2])
            record["id"] = db_id
            record["ts"] = db_ts
            results.append(record)
        return results
    finally:
        cur.close()
        _put_conn(conn)

def list_unique() -> List[Dict[str, Any]]:
    """
    Newest per (source_url, form_name).
    """
    conn = _get_conn()
    cur = conn.cursor()
    try:
        if _is_postgres_mode():
            # PostgreSQL version
            cur.execute("""
            SELECT r.id, r.ts, r.data
            FROM records r
            JOIN (
                SELECT COALESCE(source_url,'') su,
                       COALESCE(form_name,'') fn,
                       MAX(ts) mx
                FROM records
                GROUP BY su, fn
            ) t ON COALESCE(r.source_url,'')=t.su
               AND COALESCE(r.form_name,'')=t.fn
               AND r.ts=t.mx
            ORDER BY r.ts DESC;
            """)
        else:
            # SQLite version (same query, compatible)
            cur.execute("""
            SELECT r.id, r.ts, r.data
            FROM records r
            JOIN (
                SELECT COALESCE(source_url,'') su,
                       COALESCE(form_name,'') fn,
                       MAX(ts) mx
                FROM records
                GROUP BY su, fn
            ) t ON COALESCE(r.source_url,'')=t.su
               AND COALESCE(r.form_name,'')=t.fn
               AND r.ts=t.mx
            ORDER BY r.ts DESC;
            """)
        results = []
        for row in cur.fetchall():
            db_id = row[0]
            db_ts = row[1]
            record = json.loads(row[2])
            record["id"] = db_id
            record["ts"] = db_ts
            results.append(record)
        return results
    finally:
        cur.close()
        _put_conn(conn)

def list_committed() -> List[Dict[str, Any]]:
    """
    Return only committed records (committed=true in JSON data).
    Filters at SQL level for performance (PostgreSQL only - SQLite does in-memory).
    """
    conn = _get_conn()
    cur = conn.cursor()
    use_python_filtering = False

    try:
        if _is_postgres_mode():
            # Try PostgreSQL JSONB query first
            try:
                cur.execute("""
                SELECT id, ts, data
                FROM records
                WHERE (data::jsonb->>'committed')::text IN ('true', '1', 'yes', 'True', 'YES', 'Yes')
                   OR (data::jsonb->'committed')::boolean = true
                ORDER BY ts DESC;
                """)
            except Exception as e:
                # Fall back to Python filtering if JSONB query fails (e.g., null bytes in data)
                print(f"[STORAGE] PostgreSQL JSONB query failed in list_committed, falling back to Python filtering: {e}", flush=True)
                # Reload cursor after error
                cur.close()
                _put_conn(conn)
                conn = _get_conn()
                cur = conn.cursor()
                use_python_filtering = True

        # SQLite or PostgreSQL fallback: Load all and filter in Python
        if not _is_postgres_mode() or use_python_filtering:
            cur.execute("SELECT id, ts, data FROM records ORDER BY ts DESC;")

        results = []
        for row in cur.fetchall():
            db_id = row[0]
            db_ts = row[1]
            record = json.loads(row[2])

            # SQLite or fallback: Filter in Python
            if not _is_postgres_mode() or use_python_filtering:
                committed = record.get("committed")
                if not (committed is True or str(committed).lower() in ("1", "true", "yes")):
                    continue

            record["id"] = db_id
            record["ts"] = db_ts
            results.append(record)
        return results
    finally:
        cur.close()
        _put_conn(conn)

def list_by_ids(ids: List[str]) -> List[Dict[str, Any]]:
    """
    Return records matching the provided IDs.
    Filters at SQL level for performance.
    """
    if not ids:
        return []

    id_list = [str(x).strip() for x in ids if x]
    if not id_list:
        return []

    conn = _get_conn()
    cur = conn.cursor()
    try:
        if _is_postgres_mode():
            # PostgreSQL: Use ANY
            cur.execute("""
            SELECT id, ts, data
            FROM records
            WHERE id = ANY(%s)
            ORDER BY ts DESC;
            """, (id_list,))
        else:
            # SQLite: Use IN with placeholders
            placeholders = ",".join("?" * len(id_list))
            cur.execute(f"""
            SELECT id, ts, data
            FROM records
            WHERE id IN ({placeholders})
            ORDER BY ts DESC;
            """, id_list)

        results = []
        for row in cur.fetchall():
            db_id = row[0]
            db_ts = row[1]
            record = json.loads(row[2])
            record["id"] = db_id
            record["ts"] = db_ts
            results.append(record)
        return results
    finally:
        cur.close()
        _put_conn(conn)

def count_filtered(
    committed: Optional[bool] = None,
    ids: Optional[List[str]] = None,
    industry_vertical: Optional[str] = None
) -> int:
    """
    Count records matching the provided filters (without pagination).
    Used for pagination total counts.
    """
    conn = _get_conn()
    cur = conn.cursor()

    try:
        if _is_postgres_mode():
            # Try PostgreSQL JSONB queries, but fall back to Python filtering if data has null bytes
            try:
                # PostgreSQL: Build dynamic WHERE clause
                where_clauses = []
                params = []

                if committed is True:
                    where_clauses.append("""
                        ((data::jsonb->>'committed')::text IN ('true', '1', 'yes', 'True', 'YES', 'Yes')
                         OR (data::jsonb->'committed')::boolean = true)
                    """)
                elif committed is False:
                    where_clauses.append("""
                        ((data::jsonb->>'committed') IS NULL
                         OR (data::jsonb->>'committed')::text NOT IN ('true', '1', 'yes', 'True', 'YES', 'Yes'))
                    """)

                if ids:
                    id_list = [str(x).strip() for x in ids if x]
                    if id_list:
                        where_clauses.append("id = ANY(%s)")
                        params.append(id_list)

                if industry_vertical:
                    where_clauses.append("(data::jsonb->>'industry_vertical')::text ILIKE %s")
                    params.append(f"%{industry_vertical}%")

                query = "SELECT COUNT(*) FROM records"
                if where_clauses:
                    query += " WHERE " + " AND ".join(where_clauses)
                query += ";"

                cur.execute(query, tuple(params))
                return cur.fetchone()[0]
            except Exception as e:
                # Fall back to Python filtering if JSONB query fails (e.g., null bytes in data)
                print(f"[STORAGE] PostgreSQL JSONB query failed, falling back to Python filtering: {e}")
                # Reload cursor after error
                cur.close()
                _put_conn(conn)
                conn = _get_conn()
                cur = conn.cursor()
                # Fall through to SQLite-style Python filtering

        # SQLite or fallback: Build query with ID filter, then filter in Python
        if ids:
            id_list = [str(x).strip() for x in ids if x]
            if id_list:
                # Use correct placeholder syntax for PostgreSQL (%s) vs SQLite (?)
                if _is_postgres_mode():
                    cur.execute("SELECT data FROM records WHERE id = ANY(%s)", (id_list,))
                else:
                    placeholders = ",".join("?" * len(id_list))
                    cur.execute(f"SELECT data FROM records WHERE id IN ({placeholders})", id_list)
            else:
                cur.execute("SELECT data FROM records")
        else:
            cur.execute("SELECT data FROM records")

        # Filter in Python for SQLite
        count = 0
        for row in cur.fetchall():
            record = json.loads(row[0])

            if committed is True:
                rec_committed = record.get("committed")
                if not (rec_committed is True or str(rec_committed).lower() in ("1", "true", "yes")):
                    continue
            elif committed is False:
                rec_committed = record.get("committed")
                if rec_committed is True or str(rec_committed).lower() in ("1", "true", "yes"):
                    continue

            if industry_vertical:
                rec_vertical = str(record.get("industry_vertical", "")).lower()
                if industry_vertical.lower() not in rec_vertical:
                    continue

            count += 1
        return count
    finally:
        cur.close()
        _put_conn(conn)

def list_filtered(
    committed: Optional[bool] = None,
    ids: Optional[List[str]] = None,
    industry_vertical: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None
) -> List[Dict[str, Any]]:
    """
    Return records matching the provided filters.
    Filters at SQL level for PostgreSQL (performance), in Python for SQLite.

    Args:
        committed: Filter by committed status (True/False/None for all)
        ids: Filter by specific record IDs
        industry_vertical: Filter by industry vertical (case-insensitive partial match)
        limit: Maximum number of records to return (pagination)
        offset: Number of records to skip (pagination)
    """
    conn = _get_conn()
    cur = conn.cursor()
    use_python_filtering = False

    try:
        if _is_postgres_mode():
            # Try PostgreSQL JSONB queries, but fall back to Python filtering if data has null bytes
            try:
                # PostgreSQL: Build dynamic WHERE clause with JSONB queries
                where_clauses = []
                params = []

                if committed is True:
                    where_clauses.append("""
                        ((data::jsonb->>'committed')::text IN ('true', '1', 'yes', 'True', 'YES', 'Yes')
                         OR (data::jsonb->'committed')::boolean = true)
                    """)
                elif committed is False:
                    where_clauses.append("""
                        ((data::jsonb->>'committed') IS NULL
                         OR (data::jsonb->>'committed')::text NOT IN ('true', '1', 'yes', 'True', 'YES', 'Yes'))
                    """)

                if ids:
                    id_list = [str(x).strip() for x in ids if x]
                    if id_list:
                        where_clauses.append("id = ANY(%s)")
                        params.append(id_list)

                if industry_vertical:
                    where_clauses.append("(data::jsonb->>'industry_vertical')::text ILIKE %s")
                    params.append(f"%{industry_vertical}%")

                query = "SELECT id, ts, data FROM records"
                if where_clauses:
                    query += " WHERE " + " AND ".join(where_clauses)
                query += " ORDER BY ts DESC"

                # Add pagination at SQL level for performance
                if limit is not None:
                    query += " LIMIT %s"
                    params.append(limit)
                if offset is not None:
                    query += " OFFSET %s"
                    params.append(offset)

                query += ";"

                print(f"[STORAGE] PostgreSQL filtered query: {query[:200]}... with {len(params)} params", flush=True)
                cur.execute(query, tuple(params))
            except Exception as e:
                # Fall back to Python filtering if JSONB query fails (e.g., null bytes in data)
                print(f"[STORAGE] PostgreSQL JSONB query failed, falling back to Python filtering: {e}", flush=True)
                # Reload cursor after error
                cur.close()
                _put_conn(conn)
                conn = _get_conn()
                cur = conn.cursor()
                # Mark that we need Python filtering
                use_python_filtering = True

        # SQLite or PostgreSQL fallback: Load data and filter in Python
        if not _is_postgres_mode() or use_python_filtering:
            query = "SELECT id, ts, data FROM records"
            params = []

            if ids:
                id_list = [str(x).strip() for x in ids if x]
                if id_list:
                    if _is_postgres_mode():
                        query += " WHERE id = ANY(%s)"
                        params.append(id_list)
                    else:
                        placeholders = ",".join("?" * len(id_list))
                        query += f" WHERE id IN ({placeholders})"
                        params.extend(id_list)

            query += " ORDER BY ts DESC;"

            print(f"[STORAGE] Using Python filtering fallback, loading records...", flush=True)
            cur.execute(query, tuple(params) if params else ())

        results = []
        row_count = 0
        for row in cur.fetchall():
            db_id = row[0]
            db_ts = row[1]
            record = json.loads(row[2])

            # Apply Python filters when needed (SQLite or PostgreSQL fallback)
            if not _is_postgres_mode() or use_python_filtering:
                if committed is True:
                    rec_committed = record.get("committed")
                    if not (rec_committed is True or str(rec_committed).lower() in ("1", "true", "yes")):
                        continue
                elif committed is False:
                    rec_committed = record.get("committed")
                    if rec_committed is True or str(rec_committed).lower() in ("1", "true", "yes"):
                        continue

                if industry_vertical:
                    rec_vertical = str(record.get("industry_vertical", "")).lower()
                    if industry_vertical.lower() not in rec_vertical:
                        continue

            # Apply pagination in Python when using fallback
            if use_python_filtering:
                if offset is not None and row_count < offset:
                    row_count += 1
                    continue
                if limit is not None and len(results) >= limit:
                    break

            record["id"] = db_id
            record["ts"] = db_ts
            results.append(record)
            row_count += 1

        print(f"[STORAGE] Filtered query returned {len(results)} records", flush=True)
        return results
    finally:
        cur.close()
        _put_conn(conn)

def delete_all() -> int:
    conn = _get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM records;")
        n = cur.fetchone()[0]
        cur.execute("DELETE FROM records;")
        conn.commit()
        return int(n)
    finally:
        cur.close()
        _put_conn(conn)

def delete_ids(ids: Iterable[str]) -> int:
    ids = [str(x).strip() for x in ids if x]
    if not ids:
        return 0
    conn = _get_conn()
    cur = conn.cursor()
    try:
        if _is_postgres_mode():
            cur.execute("DELETE FROM records WHERE id = ANY(%s);", (ids,))
        else:
            placeholders = ",".join("?" * len(ids))
            cur.execute(f"DELETE FROM records WHERE id IN ({placeholders});", ids)
        rowcount = cur.rowcount
        conn.commit()
        return int(rowcount)
    finally:
        cur.close()
        _put_conn(conn)

# Chunk size for SQLite IN (...) clauses to stay under the default
# SQLITE_MAX_VARIABLE_NUMBER expression limit (~999).
_SQLITE_DELETE_CHUNK = 500


def _delete_ids_chunked_sqlite(cur, ids: List[str]) -> int:
    """Issue chunked DELETE WHERE id IN (...) statements for SQLite."""
    total = 0
    for start in range(0, len(ids), _SQLITE_DELETE_CHUNK):
        chunk = ids[start:start + _SQLITE_DELETE_CHUNK]
        if not chunk:
            continue
        placeholders = ",".join("?" * len(chunk))
        cur.execute(f"DELETE FROM records WHERE id IN ({placeholders});", chunk)
        total += cur.rowcount or 0
    return total


def delete_uncommitted() -> int:
    """
    Delete all records where committed != true.

    Postgres: pushes the predicate into SQL via JSONB; no id list materialized.
    SQLite: streams ids that fail the JSON committed-check, then chunks the
    DELETE in batches of _SQLITE_DELETE_CHUNK to stay under SQLite's
    expression-tree limit.
    """
    conn = _get_conn()
    cur = conn.cursor()
    try:
        if _is_postgres_mode():
            # Single SQL DELETE — no Python row iteration, no id list.
            try:
                cur.execute("""
                DELETE FROM records
                WHERE (data::jsonb->>'committed') IS NULL
                   OR lower((data::jsonb->>'committed')::text) NOT IN ('true', '1', 'yes')
                """)
                rowcount = cur.rowcount or 0
                conn.commit()
                return int(rowcount)
            except Exception as e:
                # Fall back to row-iterating delete if JSONB query fails
                # (e.g. null bytes in data). Reload cursor after error.
                logger.warning(
                    "Postgres JSONB delete_uncommitted failed, falling back: %s", e
                )
                cur.close()
                _put_conn(conn)
                conn = _get_conn()
                cur = conn.cursor()
                # Drop into the SQLite-style path using ANY()
                cur.execute("SELECT id, data FROM records;")
                ids: List[str] = []
                for row in cur:
                    rid = row[0]
                    try:
                        d = json.loads(row[1])
                    except Exception:
                        ids.append(rid)
                        continue
                    if not (d.get("committed") is True or str(d.get("committed") or "").lower() in ("1", "true", "yes")):
                        ids.append(rid)
                if not ids:
                    return 0
                cur.execute("DELETE FROM records WHERE id = ANY(%s);", (ids,))
                rowcount = cur.rowcount or 0
                conn.commit()
                return int(rowcount)

        # SQLite path: stream rows, accumulate ids, chunk the DELETE.
        cur.execute("SELECT id, data FROM records;")
        ids: List[str] = []
        for row in cur:
            rid = row[0]
            try:
                d = json.loads(row[1])
            except Exception:
                ids.append(rid)
                continue
            if not (d.get("committed") is True or str(d.get("committed") or "").lower() in ("1", "true", "yes")):
                ids.append(rid)
        if not ids:
            return 0
        rowcount = _delete_ids_chunked_sqlite(cur, ids)
        conn.commit()
        return int(rowcount)
    finally:
        cur.close()
        _put_conn(conn)


def delete_empty() -> int:
    """
    Delete records whose data is empty or missing source_url AND pages==0 AND complexity==0.

    Postgres: SQL predicate via JSONB.
    SQLite: streams rows, chunks the DELETE.
    """
    conn = _get_conn()
    cur = conn.cursor()
    try:
        if _is_postgres_mode():
            try:
                cur.execute("""
                DELETE FROM records
                WHERE data IS NULL
                   OR data = ''
                   OR (
                        COALESCE(NULLIF(TRIM(data::jsonb->>'source_url'), ''), '') = ''
                        AND COALESCE((data::jsonb->>'pages')::int, 0) = 0
                        AND COALESCE((data::jsonb->>'complexity_score')::float, 0.0) = 0.0
                   )
                """)
                rowcount = cur.rowcount or 0
                conn.commit()
                return int(rowcount)
            except Exception as e:
                logger.warning(
                    "Postgres JSONB delete_empty failed, falling back: %s", e
                )
                cur.close()
                _put_conn(conn)
                conn = _get_conn()
                cur = conn.cursor()
                cur.execute("SELECT id, data FROM records;")
                ids: List[str] = []
                for row in cur:
                    rid = row[0]
                    try:
                        d = json.loads(row[1])
                    except Exception:
                        ids.append(rid)
                        continue
                    src = (d.get("source_url") or "").strip()
                    pages = int(d.get("pages") or 0)
                    cx = float(d.get("complexity_score") or 0.0)
                    if (not d) or (not src and pages == 0 and cx == 0.0):
                        ids.append(rid)
                if not ids:
                    return 0
                cur.execute("DELETE FROM records WHERE id = ANY(%s);", (ids,))
                rowcount = cur.rowcount or 0
                conn.commit()
                return int(rowcount)

        # SQLite path: stream rows, accumulate ids, chunk DELETE.
        cur.execute("SELECT id, data FROM records;")
        ids: List[str] = []
        for row in cur:
            rid = row[0]
            try:
                d = json.loads(row[1])
            except Exception:
                ids.append(rid)
                continue
            src = (d.get("source_url") or "").strip()
            pages = int(d.get("pages") or 0)
            cx = float(d.get("complexity_score") or 0.0)
            if (not d) or (not src and pages == 0 and cx == 0.0):
                ids.append(rid)
        if not ids:
            return 0
        rowcount = _delete_ids_chunked_sqlite(cur, ids)
        conn.commit()
        return int(rowcount)
    finally:
        cur.close()
        _put_conn(conn)


def count_empty() -> int:
    """
    Count records that delete_empty() would remove. Pushes predicate into SQL
    where possible.
    """
    conn = _get_conn()
    cur = conn.cursor()
    try:
        if _is_postgres_mode():
            try:
                cur.execute("""
                SELECT COUNT(*) FROM records
                WHERE data IS NULL
                   OR data = ''
                   OR (
                        COALESCE(NULLIF(TRIM(data::jsonb->>'source_url'), ''), '') = ''
                        AND COALESCE((data::jsonb->>'pages')::int, 0) = 0
                        AND COALESCE((data::jsonb->>'complexity_score')::float, 0.0) = 0.0
                   )
                """)
                return int(cur.fetchone()[0])
            except Exception as e:
                logger.warning("Postgres JSONB count_empty failed, falling back: %s", e)
                cur.close()
                _put_conn(conn)
                conn = _get_conn()
                cur = conn.cursor()

        # SQLite path: still streams rows but does not load all into memory.
        cur.execute("SELECT data FROM records;")
        n = 0
        for row in cur:
            try:
                d = json.loads(row[0])
            except Exception:
                n += 1
                continue
            src = (d.get("source_url") or "").strip()
            pages = int(d.get("pages") or 0)
            cx = float(d.get("complexity_score") or 0.0)
            if (not d) or (not src and pages == 0 and cx == 0.0):
                n += 1
        return n
    finally:
        cur.close()
        _put_conn(conn)

def update_record(rec_id: str, patch: Dict[str, Any]) -> bool:
    conn = _get_conn()
    cur = conn.cursor()
    try:
        if _is_postgres_mode():
            cur.execute("SELECT data FROM records WHERE id = %s", (rec_id,))
        else:
            cur.execute("SELECT data FROM records WHERE id = ?", (rec_id,))
        row = cur.fetchone()
        if not row:
            return False
        obj = json.loads(row[0])
        obj.update(patch or {})

        if _is_postgres_mode():
            cur.execute("UPDATE records SET data = %s WHERE id = %s",
                       (json.dumps(obj, ensure_ascii=False), rec_id))
        else:
            cur.execute("UPDATE records SET data = ? WHERE id = ?",
                       (json.dumps(obj, ensure_ascii=False), rec_id))
        conn.commit()
        return True
    finally:
        cur.close()
        _put_conn(conn)

# -----------------------------
# In-memory progress (crawl/analyze)
# -----------------------------
_PROGRESS_LOCK = threading.Lock()
_PROGRESS: Dict[str, Dict[str, Optional[int]]] = {
    "crawl":   {"done": 0, "total": 0, "ts": 0},
    "analyze": {"done": 0, "total": 0, "ts": 0},
}

def set_progress(kind: str, done: Optional[int] = None, total: Optional[int] = None) -> None:
    with _PROGRESS_LOCK:
        obj = _PROGRESS.setdefault(kind, {"done": 0, "total": 0, "ts": 0})
        if done is not None:  obj["done"]  = max(0, int(done))
        if total is not None: obj["total"] = max(0, int(total))
        obj["ts"] = _now()

def bump_progress(kind: str, inc: int = 1) -> None:
    with _PROGRESS_LOCK:
        obj = _PROGRESS.setdefault(kind, {"done": 0, "total": 0, "ts": 0})
        obj["done"] = int(obj.get("done") or 0) + int(inc)
        obj["ts"] = _now()

def reset_progress(kind: Optional[str] = None) -> None:
    with _PROGRESS_LOCK:
        if kind:
            _PROGRESS[kind] = {"done": 0, "total": 0, "ts": _now()}
        else:
            for k in _PROGRESS:
                _PROGRESS[k] = {"done": 0, "total": 0, "ts": _now()}

def get_progress() -> Dict[str, Any]:
    with _PROGRESS_LOCK:
        return {
            "crawl": dict(_PROGRESS["crawl"]),
            "analyze": dict(_PROGRESS["analyze"]),
        }

# -----------------------------
# Login Tracking
# -----------------------------
def log_login(email: str, name: str = None, ip_address: str = None, user_agent: str = None) -> int:
    """Log a successful login. Returns the new login log ID."""
    conn = _get_conn()
    cur = conn.cursor()
    try:
        if _is_postgres_mode():
            cur.execute("""
            INSERT INTO login_logs(ts, email, name, ip_address, user_agent)
            VALUES(%s, %s, %s, %s, %s)
            RETURNING id
            """, (_now(), email, name, ip_address, user_agent))
            log_id = cur.fetchone()[0]
        else:
            cur.execute("""
            INSERT INTO login_logs(ts, email, name, ip_address, user_agent)
            VALUES(?, ?, ?, ?, ?)
            """, (_now(), email, name, ip_address, user_agent))
            log_id = cur.lastrowid
        conn.commit()
        return log_id
    finally:
        cur.close()
        _put_conn(conn)

def get_login_logs(limit: int = 100) -> List[Dict[str, Any]]:
    """Get recent login logs, newest first."""
    conn = _get_conn()
    cur = conn.cursor()
    try:
        if _is_postgres_mode():
            cur.execute("""
            SELECT id, ts, email, name, ip_address, user_agent
            FROM login_logs
            ORDER BY ts DESC
            LIMIT %s
            """, (limit,))
        else:
            cur.execute("""
            SELECT id, ts, email, name, ip_address, user_agent
            FROM login_logs
            ORDER BY ts DESC
            LIMIT ?
            """, (limit,))
        return [
            {
                "id": row[0],
                "timestamp": row[1],
                "email": row[2],
                "name": row[3],
                "ip_address": row[4],
                "user_agent": row[5]
            }
            for row in cur.fetchall()
        ]
    finally:
        cur.close()
        _put_conn(conn)

def get_login_stats() -> Dict[str, Any]:
    """Get login statistics."""
    conn = _get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM login_logs")
        total = cur.fetchone()[0]

        cur.execute("SELECT COUNT(DISTINCT email) FROM login_logs")
        unique_users = cur.fetchone()[0]

        day_ago = _now() - 86400
        if _is_postgres_mode():
            cur.execute("SELECT COUNT(*) FROM login_logs WHERE ts > %s", (day_ago,))
        else:
            cur.execute("SELECT COUNT(*) FROM login_logs WHERE ts > ?", (day_ago,))
        last_24h = cur.fetchone()[0]

        cur.execute("SELECT email, ts FROM login_logs ORDER BY ts DESC LIMIT 1")
        recent = cur.fetchone()

        return {
            "total_logins": total,
            "unique_users": unique_users,
            "last_24h": last_24h,
            "most_recent": {"email": recent[0], "timestamp": recent[1]} if recent else None
        }
    finally:
        cur.close()
        _put_conn(conn)

# -----------------------------
# Analysis Activity Tracking
# -----------------------------
def log_analysis_activity(
    email: str,
    activity_type: str,
    source_url: str = None,
    forms_found: int = 0,
    forms_analyzed: int = 0,
    success: bool = True,
    error_message: str = None,
    name: str = None,
    ip_address: str = None,
    user_agent: str = None
) -> int:
    """Log analysis activity (crawl or analyze)."""
    conn = _get_conn()
    cur = conn.cursor()
    try:
        if _is_postgres_mode():
            cur.execute("""
            INSERT INTO analysis_logs(
                ts, email, name, activity_type, source_url,
                forms_found, forms_analyzed, success, error_message,
                ip_address, user_agent
            )
            VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """, (
                _now(), email, name, activity_type, source_url,
                forms_found, forms_analyzed, 1 if success else 0, error_message,
                ip_address, user_agent
            ))
            log_id = cur.fetchone()[0]
        else:
            cur.execute("""
            INSERT INTO analysis_logs(
                ts, email, name, activity_type, source_url,
                forms_found, forms_analyzed, success, error_message,
                ip_address, user_agent
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                _now(), email, name, activity_type, source_url,
                forms_found, forms_analyzed, 1 if success else 0, error_message,
                ip_address, user_agent
            ))
            log_id = cur.lastrowid
        conn.commit()
        return log_id
    finally:
        cur.close()
        _put_conn(conn)

def get_analysis_logs(limit: int = 100) -> List[Dict[str, Any]]:
    """Get recent analysis activity logs, newest first."""
    conn = _get_conn()
    cur = conn.cursor()
    try:
        if _is_postgres_mode():
            cur.execute("""
            SELECT id, ts, email, name, activity_type, source_url,
                   forms_found, forms_analyzed, success, error_message,
                   ip_address, user_agent
            FROM analysis_logs
            ORDER BY ts DESC
            LIMIT %s
            """, (limit,))
        else:
            cur.execute("""
            SELECT id, ts, email, name, activity_type, source_url,
                   forms_found, forms_analyzed, success, error_message,
                   ip_address, user_agent
            FROM analysis_logs
            ORDER BY ts DESC
            LIMIT ?
            """, (limit,))
        return [
            {
                "id": row[0],
                "timestamp": row[1],
                "email": row[2],
                "name": row[3],
                "activity_type": row[4],
                "source_url": row[5],
                "forms_found": row[6],
                "forms_analyzed": row[7],
                "success": bool(row[8]),
                "error_message": row[9],
                "ip_address": row[10],
                "user_agent": row[11]
            }
            for row in cur.fetchall()
        ]
    finally:
        cur.close()
        _put_conn(conn)

def get_analysis_stats() -> Dict[str, Any]:
    """Get analysis activity statistics."""
    conn = _get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM analysis_logs")
        total = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM analysis_logs WHERE activity_type = 'crawl'")
        crawls = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM analysis_logs WHERE activity_type = 'analyze'")
        analyses = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM analysis_logs WHERE success = 1")
        successful = cur.fetchone()[0]
        success_rate = (successful / total * 100) if total > 0 else 0

        cur.execute("SELECT SUM(forms_found) FROM analysis_logs")
        total_found = cur.fetchone()[0] or 0
        cur.execute("SELECT SUM(forms_analyzed) FROM analysis_logs")
        total_analyzed = cur.fetchone()[0] or 0

        day_ago = _now() - 86400
        if _is_postgres_mode():
            cur.execute("SELECT COUNT(*) FROM analysis_logs WHERE ts > %s", (day_ago,))
        else:
            cur.execute("SELECT COUNT(*) FROM analysis_logs WHERE ts > ?", (day_ago,))
        last_24h = cur.fetchone()[0]

        cur.execute("""
        SELECT email, COUNT(*) as count
        FROM analysis_logs
        GROUP BY email
        ORDER BY count DESC
        LIMIT 5
        """)
        top_users = cur.fetchall()

        return {
            "total_activities": total,
            "crawls": crawls,
            "analyses": analyses,
            "success_rate": round(success_rate, 1),
            "total_forms_found": total_found,
            "total_forms_analyzed": total_analyzed,
            "last_24h": last_24h,
            "top_users": [{"email": u[0], "count": u[1]} for u in top_users]
        }
    finally:
        cur.close()
        _put_conn(conn)

# -----------------------------
# Helper functions for update_one/update_many
# -----------------------------
def _load_one_indexed(rec_id: str) -> Dict[str, Any] | None:
    """
    Load a single record by ID using a targeted SELECT (uses primary key
    index — no full table scan, no JSON-parsing of every row).
    """
    if not rec_id:
        return None
    conn = _get_conn()
    cur = conn.cursor()
    try:
        if _is_postgres_mode():
            cur.execute("SELECT id, ts, data FROM records WHERE id = %s", (str(rec_id),))
        else:
            cur.execute("SELECT id, ts, data FROM records WHERE id = ?", (str(rec_id),))
        row = cur.fetchone()
        if not row:
            return None
        try:
            record = json.loads(row[2])
        except Exception:
            return None
        record["id"] = row[0]
        record["ts"] = row[1]
        return record
    finally:
        cur.close()
        _put_conn(conn)

# Backwards-compatible alias. Prefer _load_one_indexed in new code.
def _load_one(rec_id: str) -> Dict[str, Any] | None:
    """Load a single record by ID (indexed; constant time per call)."""
    return _load_one_indexed(rec_id)

def _save_one(record: Dict[str, Any]) -> int:
    """Save a single record"""
    try:
        save(record)
        return 1
    except Exception:
        pass
    return 0

def update_one(rec_id: str, updates: Dict[str, Any]) -> int:
    """
    Merge `updates` into the existing record with id=rec_id and persist.
    Returns number of rows updated (0 or 1).

    Routes through update_record() so the SQL UPDATE happens against the
    primary-key index — no list_all() scan, no JSON parse of unrelated rows,
    and no per-row Cloud Storage upload.
    """
    if not rec_id:
        return 0
    try:
        ok = update_record(str(rec_id), updates or {})
    except Exception as e:
        logger.warning("update_one(%s) failed: %s", rec_id, e)
        return 0
    if not ok:
        return 0
    # update_record() bypasses save() and therefore the cloud-sync schedule;
    # reschedule a sync here so SQLite-mode persistence still happens.
    if not _is_postgres_mode():
        try:
            _schedule_cloud_sync()
        except Exception as e:
            logger.warning("Failed to schedule cloud sync after update_one: %s", e)
    return 1

def update_many(ids: Iterable[str], updates: Dict[str, Any]) -> int:
    """
    Apply the same `updates` to each id in `ids`. Returns total updated count.
    """
    count = 0
    for rid in ids:
        try:
            count += update_one(rid, updates)
        except Exception:
            continue
    return count


# ---------------------------------------------------------------------------
# Indexed helpers (added to replace list_all() scans on the analyze hot path).
# Both helpers use the indexed `source_url` column on the records table.
# Added by F-AP-04 / F-AP-10 fix; see app/api/analyze.py for usage.
# ---------------------------------------------------------------------------
def get_by_source_url(source_url: str) -> Optional[Dict[str, Any]]:
    """
    Return the most recent record whose source_url matches exactly, or None.

    Uses the existing idx_records_source index — no JSON scan required.
    Replaces the previous pattern of calling list_all() then Python-side
    filtering, which was O(N) per analyze.
    """
    if not source_url:
        return None
    conn = _get_conn()
    cur = conn.cursor()
    try:
        if _is_postgres_mode():
            cur.execute(
                "SELECT id, ts, data FROM records WHERE source_url = %s "
                "ORDER BY ts DESC LIMIT 1;",
                (source_url,),
            )
        else:
            cur.execute(
                "SELECT id, ts, data FROM records WHERE source_url = ? "
                "ORDER BY ts DESC LIMIT 1;",
                (source_url,),
            )
        row = cur.fetchone()
        if not row:
            return None
        try:
            record = json.loads(row[2])
        except Exception as e:
            logger.warning("get_by_source_url: failed to parse record JSON: %s", e)
            return None
        record["id"] = row[0]
        record["ts"] = row[1]
        return record
    finally:
        cur.close()
        _put_conn(conn)


def find_by_base_form_pattern(pattern: str, limit: int = 50) -> List[Dict[str, Any]]:
    """
    Return a small candidate list of records whose source_url *might* share
    the supplied base form pattern (a normalized lowercased form name like
    `pub_223` or `sf-86`).

    There is no dedicated index for base_form_pattern yet, so this uses the
    indexed `source_url` column with a LIKE/ILIKE filter on `%pattern%`. The
    result is intentionally bounded by `limit` to keep the candidate list
    small enough that downstream linear scans (language_dedup) stay cheap.

    NOTE: This is a best-effort prefilter, not a precise match. The caller
    is expected to apply the strict comparison (same base pattern, same
    page count, same domain) on the returned list.

    TODO: A proper fix is to persist `base_form_pattern` as its own
    indexed column on records and query it directly. Tracked separately.
    """
    if not pattern:
        return []
    needle = f"%{pattern}%"
    conn = _get_conn()
    cur = conn.cursor()
    try:
        if _is_postgres_mode():
            cur.execute(
                "SELECT id, ts, data FROM records "
                "WHERE source_url ILIKE %s "
                "ORDER BY ts DESC LIMIT %s;",
                (needle, int(limit)),
            )
        else:
            cur.execute(
                "SELECT id, ts, data FROM records "
                "WHERE LOWER(source_url) LIKE LOWER(?) "
                "ORDER BY ts DESC LIMIT ?;",
                (needle, int(limit)),
            )
        out: List[Dict[str, Any]] = []
        for row in cur.fetchall():
            try:
                record = json.loads(row[2])
            except Exception as e:
                logger.warning(
                    "find_by_base_form_pattern: failed to parse JSON for id=%s: %s",
                    row[0], e,
                )
                continue
            record["id"] = row[0]
            record["ts"] = row[1]
            out.append(record)
        return out
    finally:
        cur.close()
        _put_conn(conn)
