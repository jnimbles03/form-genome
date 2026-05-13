# app/services/jobs_store.py
"""
Postgres-backed store for long-running pipeline jobs.

Cloud Run scales horizontally and recycles instances aggressively, so an
in-memory dict keyed by job_id is useless the second your POST lands on
instance A and your GET poll lands on instance B (or A has scaled away).
This module persists job state so any instance can answer status polls
and the orchestrator can resume / observe regardless of where it ran.

Schema:
    crawl_jobs (
        job_id        TEXT PRIMARY KEY,
        root_url      TEXT,
        status        TEXT,
        message       TEXT,
        state         JSONB DEFAULT '{}'::jsonb,   -- crawl/triage/analyze/etc.
        html          TEXT,                        -- built dashboard
        branding      JSONB,
        stop_requested BOOLEAN DEFAULT false,
        created_at    TIMESTAMP DEFAULT NOW(),
        updated_at    TIMESTAMP DEFAULT NOW()
    )

Only the JSONB `state` blob holds the per-stage counters. The other columns
exist for fast filtering / cleanup.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Any, Dict, Optional

from app.services import storage

logger = logging.getLogger(__name__)

_INIT_LOCK = threading.Lock()
_INITIALIZED = False


def _ensure_table() -> None:
    """Create the crawl_jobs table on first use (idempotent)."""
    global _INITIALIZED
    if _INITIALIZED:
        return
    with _INIT_LOCK:
        if _INITIALIZED:
            return
        is_pg = storage._is_postgres_mode()
        conn = storage._get_conn()
        try:
            cur = conn.cursor()
            if is_pg:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS crawl_jobs (
                        job_id         TEXT PRIMARY KEY,
                        root_url       TEXT,
                        status         TEXT,
                        message        TEXT,
                        state          JSONB NOT NULL DEFAULT '{}'::jsonb,
                        html           TEXT,
                        branding       JSONB,
                        stop_requested BOOLEAN NOT NULL DEFAULT false,
                        created_at     TIMESTAMP DEFAULT NOW(),
                        updated_at     TIMESTAMP DEFAULT NOW()
                    );
                """)
            else:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS crawl_jobs (
                        job_id         TEXT PRIMARY KEY,
                        root_url       TEXT,
                        status         TEXT,
                        message        TEXT,
                        state          TEXT NOT NULL DEFAULT '{}',
                        html           TEXT,
                        branding       TEXT,
                        stop_requested INTEGER NOT NULL DEFAULT 0,
                        created_at     INTEGER DEFAULT (strftime('%s','now')),
                        updated_at     INTEGER DEFAULT (strftime('%s','now'))
                    );
                """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crawl_jobs_status ON crawl_jobs(status);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crawl_jobs_created ON crawl_jobs(created_at);")
            conn.commit()
            cur.close()
            _INITIALIZED = True
            logger.info("[JOBS_STORE] crawl_jobs table ready")
        finally:
            storage._put_conn(conn)


def create_job(root_url: str, branding: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Insert a new queued job. Returns the full job dict."""
    _ensure_table()
    job_id = uuid.uuid4().hex
    initial_state = {
        "crawl":     {"done": 0, "total": 0, "pdfs": 0, "url": ""},
        "triage":    {"done": 0, "total": 0, "kept": 0, "dropped": 0, "by_class": {}},
        "analyze":   {"done": 0, "total": 0, "errors": 0},
        "dashboard": {"ready": False, "html_url": ""},
        "summary":   {},
    }
    is_pg = storage._is_postgres_mode()
    conn = storage._get_conn()
    try:
        cur = conn.cursor()
        if is_pg:
            cur.execute(
                "INSERT INTO crawl_jobs (job_id, root_url, status, message, state, branding) "
                "VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb)",
                (job_id, root_url, "queued", "", json.dumps(initial_state),
                 json.dumps(branding or {})),
            )
        else:
            cur.execute(
                "INSERT INTO crawl_jobs (job_id, root_url, status, message, state, branding) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, root_url, "queued", "", json.dumps(initial_state),
                 json.dumps(branding or {})),
            )
        conn.commit()
        cur.close()
    finally:
        storage._put_conn(conn)
    return {
        "job_id": job_id,
        "root_url": root_url,
        "status": "queued",
        "message": "",
        **initial_state,
        "stop_requested": False,
        "branding": branding or {},
    }


def _row_to_job(row: tuple, include_html: bool = False) -> Dict[str, Any]:
    """Convert a SELECT row into the canonical job dict."""
    (job_id, root_url, status, message, state_raw, html, branding_raw,
     stop_requested, created_at, updated_at) = row
    state = json.loads(state_raw) if isinstance(state_raw, str) else (state_raw or {})
    branding = json.loads(branding_raw) if isinstance(branding_raw, str) else (branding_raw or {})
    out = {
        "job_id": job_id,
        "root_url": root_url,
        "status": status,
        "message": message or "",
        "stop_requested": bool(stop_requested),
        "branding": branding,
        "created_at": int(created_at.timestamp()) if hasattr(created_at, "timestamp") else (created_at or 0),
        "ended_at": int(updated_at.timestamp()) if hasattr(updated_at, "timestamp") and status in ("done", "error", "stopped") else None,
        **state,
    }
    if include_html:
        out["html"] = html
    else:
        out["dashboard_html_available"] = html is not None
    return out


_SELECT_COLS = "job_id, root_url, status, message, state, html, branding, stop_requested, created_at, updated_at"


def get_job(job_id: str, include_html: bool = False) -> Optional[Dict[str, Any]]:
    _ensure_table()
    is_pg = storage._is_postgres_mode()
    conn = storage._get_conn()
    try:
        cur = conn.cursor()
        sql = f"SELECT {_SELECT_COLS} FROM crawl_jobs WHERE job_id = " + ("%s" if is_pg else "?")
        cur.execute(sql, (job_id,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return None
        return _row_to_job(row, include_html=include_html)
    finally:
        storage._put_conn(conn)


def update_job(
    job_id: str,
    *,
    status: Optional[str] = None,
    message: Optional[str] = None,
    state_patch: Optional[Dict[str, Any]] = None,
    html: Optional[str] = None,
    stop_requested: Optional[bool] = None,
) -> None:
    """
    Patch one or more fields on a job. `state_patch` is shallow-merged on top
    of the existing state JSON. The whole row is written in a single UPDATE.
    """
    _ensure_table()
    is_pg = storage._is_postgres_mode()
    conn = storage._get_conn()
    try:
        cur = conn.cursor()
        # Lock-and-read current state so we don't lose concurrent updates
        if is_pg:
            cur.execute("SELECT state FROM crawl_jobs WHERE job_id = %s FOR UPDATE", (job_id,))
        else:
            cur.execute("SELECT state FROM crawl_jobs WHERE job_id = ?", (job_id,))
        row = cur.fetchone()
        if not row:
            cur.close()
            return
        state_raw = row[0]
        state = json.loads(state_raw) if isinstance(state_raw, str) else (state_raw or {})

        if state_patch:
            for k, v in state_patch.items():
                if isinstance(v, dict) and isinstance(state.get(k), dict):
                    state[k] = {**state[k], **v}
                else:
                    state[k] = v

        sets = []
        vals: list = []
        if status is not None:
            sets.append("status = " + ("%s" if is_pg else "?"))
            vals.append(status)
        if message is not None:
            sets.append("message = " + ("%s" if is_pg else "?"))
            vals.append(message)
        if html is not None:
            sets.append("html = " + ("%s" if is_pg else "?"))
            vals.append(html)
        if stop_requested is not None:
            sets.append("stop_requested = " + ("%s" if is_pg else "?"))
            vals.append(bool(stop_requested) if is_pg else int(bool(stop_requested)))

        # State always gets re-written so the merge is durable
        if is_pg:
            sets.append("state = %s::jsonb")
        else:
            sets.append("state = ?")
        vals.append(json.dumps(state))

        if is_pg:
            sets.append("updated_at = NOW()")
        else:
            sets.append("updated_at = strftime('%s','now')")

        sql = "UPDATE crawl_jobs SET " + ", ".join(sets) + " WHERE job_id = " + ("%s" if is_pg else "?")
        vals.append(job_id)
        cur.execute(sql, tuple(vals))
        conn.commit()
        cur.close()
    finally:
        storage._put_conn(conn)


def is_stop_requested(job_id: str) -> bool:
    _ensure_table()
    is_pg = storage._is_postgres_mode()
    conn = storage._get_conn()
    try:
        cur = conn.cursor()
        sql = "SELECT stop_requested FROM crawl_jobs WHERE job_id = " + ("%s" if is_pg else "?")
        cur.execute(sql, (job_id,))
        row = cur.fetchone()
        cur.close()
        return bool(row[0]) if row else False
    finally:
        storage._put_conn(conn)
