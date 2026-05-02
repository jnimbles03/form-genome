"""
Database sync with Cloud Storage for persistence across Cloud Run restarts
"""
import os
import time
import sqlite3
import shutil
from google.cloud import storage

BUCKET_NAME = "form-genome-db"
DB_FILENAME = "formgenome.db"
LOCAL_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", DB_FILENAME)

_last_sync_time = 0
_sync_interval = 180  # 3 minutes in seconds (reduced from 10 for better reliability)

# High-water mark: highest record count we have ever successfully observed
# (either downloaded from cloud at startup or uploaded). Used as a cheap
# proxy for "what's currently in the cloud" so we don't have to download the
# cloud DB on every upload to check for catastrophic shrinkage. This is
# per-process state; it is conservative across instances (each instance
# starts at 0 until it observes a real value).
_last_cloud_count = 0

def _get_bucket():
    """Get Cloud Storage bucket"""
    try:
        client = storage.Client()
        return client.bucket(BUCKET_NAME)
    except Exception as e:
        print(f"[DB_SYNC] Failed to connect to Cloud Storage: {e}")
        return None

def _verify_database(db_path):
    """
    Verify database integrity.
    Returns True if database is valid, False otherwise.
    """
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("PRAGMA integrity_check;")
        result = cursor.fetchone()
        conn.close()

        if result and result[0] == "ok":
            print(f"[DB_SYNC] ✓ Database integrity check passed")
            return True
        else:
            print(f"[DB_SYNC] ✗ Database integrity check failed: {result}")
            return False
    except Exception as e:
        print(f"[DB_SYNC] ✗ Database verification error: {e}")
        return False

def download_from_cloud():
    """
    Download database from Cloud Storage on startup.
    If no database exists in Cloud Storage, use local empty database.
    Verifies database integrity and discards corrupted databases.
    """
    print("[DB_SYNC] ========================================")
    print("[DB_SYNC] STARTING DATABASE DOWNLOAD FROM CLOUD STORAGE")
    print(f"[DB_SYNC] Bucket name: {BUCKET_NAME}")
    print(f"[DB_SYNC] DB filename: {DB_FILENAME}")
    print(f"[DB_SYNC] Local DB path: {LOCAL_DB_PATH}")
    print(f"[DB_SYNC] Working directory: {os.getcwd()}")
    print("[DB_SYNC] ========================================")

    print("[DB_SYNC] Step 1: Connecting to Cloud Storage bucket...")
    bucket = _get_bucket()
    if not bucket:
        print("[DB_SYNC] ✗ FAILED: Cloud Storage not available - skipping download")
        print("[DB_SYNC] This means the app will use whatever database exists locally (if any)")
        return
    print(f"[DB_SYNC] ✓ Successfully connected to bucket: {BUCKET_NAME}")

    try:
        print(f"[DB_SYNC] Step 2: Checking if '{DB_FILENAME}' exists in Cloud Storage...")
        blob = bucket.blob(DB_FILENAME)

        print(f"[DB_SYNC] Step 3: Calling blob.exists() to verify file...")
        blob_exists = blob.exists()
        print(f"[DB_SYNC] blob.exists() returned: {blob_exists}")

        if blob_exists:
            # Reload blob metadata to get size
            print("[DB_SYNC] Step 4: Reloading blob metadata to get file size...")
            blob.reload()
            print(f"[DB_SYNC] ✓ Found database in Cloud Storage:")
            print(f"[DB_SYNC]   - Size: {blob.size} bytes ({blob.size / 1024:.2f} KB)")
            print(f"[DB_SYNC]   - Updated: {blob.updated}")

            # Ensure local db directory exists
            db_dir = os.path.dirname(LOCAL_DB_PATH)
            print(f"[DB_SYNC] Step 5: Ensuring local database directory exists: {db_dir}")
            os.makedirs(db_dir, exist_ok=True)
            print(f"[DB_SYNC] ✓ Directory ready")

            # Download to temporary location first
            temp_path = LOCAL_DB_PATH + ".tmp"
            print(f"[DB_SYNC] Step 6: Downloading database to temporary location: {temp_path}")
            blob.download_to_filename(temp_path)
            downloaded_size = os.path.getsize(temp_path)
            print(f"[DB_SYNC] ✓ Downloaded database from Cloud Storage:")
            print(f"[DB_SYNC]   - Downloaded size: {downloaded_size} bytes ({downloaded_size / 1024:.2f} KB)")
            print(f"[DB_SYNC]   - Expected size: {blob.size} bytes")

            if downloaded_size != blob.size:
                print(f"[DB_SYNC] ⚠️  WARNING: Size mismatch! Downloaded {downloaded_size} but expected {blob.size}")

            # Verify integrity before using
            print(f"[DB_SYNC] Step 7: Verifying database integrity...")
            if _verify_database(temp_path):
                # Move to final location
                print(f"[DB_SYNC] Step 8: Moving verified database to final location: {LOCAL_DB_PATH}")
                os.replace(temp_path, LOCAL_DB_PATH)
                final_size = os.path.getsize(LOCAL_DB_PATH)
                print(f"[DB_SYNC] ✓✓✓ DATABASE RESTORED SUCCESSFULLY ✓✓✓")
                print(f"[DB_SYNC]   - Final path: {LOCAL_DB_PATH}")
                print(f"[DB_SYNC]   - Final size: {final_size} bytes ({final_size / 1024:.2f} KB)")

                # Quick record count check - also seeds the local high-water mark
                # used by upload_to_cloud() to avoid downloading the cloud DB
                # on every upload.
                try:
                    global _last_cloud_count
                    conn = sqlite3.connect(LOCAL_DB_PATH)
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM records;")
                    count = cursor.fetchone()[0]
                    conn.close()
                    _last_cloud_count = int(count or 0)
                    print(f"[DB_SYNC]   - Record count: {count} records")
                    print(f"[DB_SYNC]   - Seeded cloud row-count high-water mark: {_last_cloud_count}")
                except Exception as count_err:
                    print(f"[DB_SYNC]   - Could not count records: {count_err}")
            else:
                # Corrupted - delete and start fresh
                print(f"[DB_SYNC] ✗ Database integrity check FAILED")
                print(f"[DB_SYNC] Step 8: Removing corrupted temporary file...")
                os.remove(temp_path)
                print(f"[DB_SYNC] ✓ Corrupted database discarded - starting fresh")
                # Delete corrupted database from cloud
                print(f"[DB_SYNC] Step 9: Deleting corrupted database from Cloud Storage...")
                blob.delete()
                print(f"[DB_SYNC] ✓ Removed corrupted database from Cloud Storage")
        else:
            print("[DB_SYNC] ℹ️  No database found in Cloud Storage - starting fresh")
            print("[DB_SYNC] The app will create a new empty database")
    except Exception as e:
        print(f"[DB_SYNC] ✗✗✗ ERROR DOWNLOADING DATABASE ✗✗✗")
        print(f"[DB_SYNC] Error type: {type(e).__name__}")
        print(f"[DB_SYNC] Error message: {e}")
        import traceback
        print(f"[DB_SYNC] Full traceback:")
        traceback.print_exc()

    print("[DB_SYNC] ========================================")

def upload_to_cloud(force=False):
    """
    Upload database to Cloud Storage with proper locking and integrity checks.

    Args:
        force: If True, upload immediately and bypass the local row-count
               high-water mark safety check. If False, only upload if the
               debounce interval has elapsed and the local row count is not
               more than 50% lower than the high-water mark.

    OPTIMIZED: Previously this function downloaded the cloud database on
    every call to compare row counts. We now compare against an in-process
    high-water mark (_last_cloud_count) seeded at startup by
    download_from_cloud() and updated after each successful upload. This
    avoids one download per save in the worst case.
    """
    global _last_sync_time, _last_cloud_count

    current_time = time.time()

    # Check if enough time has elapsed since last sync
    if not force and (current_time - _last_sync_time) < _sync_interval:
        return

    bucket = _get_bucket()
    if not bucket:
        print("[DB_SYNC] Skipping upload - Cloud Storage not available")
        return

    # Check if local database exists
    if not os.path.exists(LOCAL_DB_PATH):
        print("[DB_SYNC] No local database to upload")
        return

    backup_path = None
    try:
        # Import storage to access database lock (only for SQLite mode)
        from app.services import storage

        # Create backup while holding database lock to prevent concurrent writes
        backup_path = LOCAL_DB_PATH + ".backup"

        # Use the appropriate lock for SQLite mode
        if hasattr(storage, '_SQLITE_LOCK'):
            lock = storage._SQLITE_LOCK
        else:
            # Fallback for compatibility
            import threading
            lock = threading.Lock()

        with lock:
            print("[DB_SYNC] Acquired database lock for backup")

            # Checkpoint WAL to ensure all writes are flushed to main database file
            try:
                conn = sqlite3.connect(LOCAL_DB_PATH)
                conn.execute("PRAGMA wal_checkpoint(FULL);")
                conn.close()
                print("[DB_SYNC] ✓ WAL checkpoint completed")
            except Exception as e:
                print(f"[DB_SYNC] Warning: WAL checkpoint failed: {e}")

            # Create backup copy (safe file copy while locked)
            shutil.copy2(LOCAL_DB_PATH, backup_path)
            print("[DB_SYNC] ✓ Created backup copy")

        # Verify backup integrity before uploading
        if not _verify_database(backup_path):
            raise Exception("Backup failed integrity check - aborting upload")

        # SAFETY CHECKS: row count + high-water mark comparison.
        # We no longer download the cloud DB here — instead we compare
        # against _last_cloud_count, the highest count we've observed in
        # this process (seeded at startup, updated after each successful
        # upload).
        try:
            conn_check = sqlite3.connect(backup_path)
            cursor = conn_check.cursor()
            cursor.execute("SELECT COUNT(*) FROM records;")
            local_count = cursor.fetchone()[0]
            conn_check.close()

            # SAFETY 1: Block empty database uploads
            if local_count == 0:
                print(f"[DB_SYNC] ⚠️  SAFETY: Refusing to upload empty database (0 records)")
                return

            print(f"[DB_SYNC] ✓ Local database has {local_count} records")
            print(f"[DB_SYNC] Cloud row-count high-water mark: {_last_cloud_count}")

            # SAFETY 2: Block uploads that would shrink the row count by
            # more than 50% relative to the in-process high-water mark.
            # `force=True` callers (e.g. shutdown sync, /commit_records)
            # bypass this guard.
            if (
                not force
                and _last_cloud_count > 0
                and local_count < (_last_cloud_count * 0.5)
            ):
                drop_percent = ((_last_cloud_count - local_count) / _last_cloud_count) * 100
                print(f"[DB_SYNC] 🚨 SAFETY: Refusing upload - {drop_percent:.1f}% record loss vs high-water mark!")
                print(f"[DB_SYNC]   High-water: {_last_cloud_count} records -> Local: {local_count} records")
                print(f"[DB_SYNC]   Pass force=True to override (e.g. intentional bulk delete).")
                return

            # Log significant changes (>10% difference) vs high-water mark.
            if _last_cloud_count > 0:
                diff_percent = abs(local_count - _last_cloud_count) / _last_cloud_count * 100
                if diff_percent > 10:
                    print(f"[DB_SYNC] ℹ️  Note: {diff_percent:.1f}% row-count change vs high-water ({_last_cloud_count} -> {local_count})")

            record_count = local_count  # For compatibility with rest of function

        except Exception as e:
            print(f"[DB_SYNC] Error in safety checks: {e}")
            return

        # Upload the verified backup (main copy)
        blob = bucket.blob(DB_FILENAME)
        blob.upload_from_filename(backup_path)

        file_size = os.path.getsize(backup_path)
        print(f"[DB_SYNC] ✓ Uploaded database to Cloud Storage ({file_size} bytes, {record_count} records)")

        # Also upload a timestamped version for backup history
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        versioned_name = f"backups/formgenome-{timestamp}.db"
        versioned_blob = bucket.blob(versioned_name)
        versioned_blob.upload_from_filename(backup_path)
        print(f"[DB_SYNC] ✓ Created versioned backup: {versioned_name}")

        _last_sync_time = current_time
        # Bump high-water mark monotonically.
        if record_count > _last_cloud_count:
            _last_cloud_count = int(record_count)
        elif force:
            # Forced upload (e.g. intentional bulk delete) replaces the mark.
            _last_cloud_count = int(record_count)

    except Exception as e:
        print(f"[DB_SYNC] Error uploading database: {e}")
    finally:
        # Clean up backup file
        if backup_path and os.path.exists(backup_path):
            try:
                os.remove(backup_path)
                print("[DB_SYNC] ✓ Cleaned up backup file")
            except Exception as e:
                print(f"[DB_SYNC] Warning: Failed to cleanup backup: {e}")

def sync_on_shutdown():
    """Force upload database before shutdown"""
    print("[DB_SYNC] Syncing database before shutdown...")
    upload_to_cloud(force=True)
