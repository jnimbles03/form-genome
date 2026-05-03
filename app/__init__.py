# app/__init__.py
from __future__ import annotations

import os
import atexit
import logging
from functools import wraps
from flask import Flask, send_from_directory, request, Response, jsonify
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import current_user
from dotenv import load_dotenv
from werkzeug.middleware.proxy_fix import ProxyFix

# Load environment variables from .env file
load_dotenv()

logger = logging.getLogger(__name__)


def _split_csv_env(name: str) -> list[str]:
    """Read a comma-separated env var and return a lowercased, stripped list."""
    raw = os.getenv(name, "")
    return [v.strip().lower() for v in raw.split(",") if v.strip()]


def create_app() -> Flask:
    app = Flask(__name__)

    # Detect if running on Cloud Run (K_SERVICE env var is set by Cloud Run)
    is_cloud_run = os.environ.get("K_SERVICE") is not None
    is_production = os.environ.get("FLASK_ENV") == "production"

    # Trust X-Forwarded-Proto from Cloud Run proxy (needed for OAuth HTTPS redirect)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

    # --- Config / secrets ---
    secret_key = os.environ.get("SECRET_KEY")
    if not secret_key:
        if is_cloud_run or is_production:
            raise RuntimeError(
                "SECRET_KEY environment variable is required in production. "
                "Provide via Secret Manager (--set-secrets SECRET_KEY=flask-secret-key:latest)."
            )
        # Local dev only: stable per-process random so OAuth state survives reloads.
        secret_key = os.urandom(24).hex()
        logger.warning("SECRET_KEY not set; generated an ephemeral key for local dev only.")
    app.config["SECRET_KEY"] = secret_key
    app.config["ADMIN_PIN"] = os.environ.get("ADMIN_PIN", "")
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB max request size

    # Session configuration for reliable OAuth
    # IMPORTANT: SameSite=None with Secure=True required for OAuth cross-domain redirects
    app.config.update({
        "SESSION_COOKIE_NAME": "form_genome_session",  # Explicit session cookie name
        "SESSION_COOKIE_HTTPONLY": True,  # Prevent JavaScript access
        "SESSION_COOKIE_SAMESITE": "None" if (is_cloud_run or is_production) else "Lax",  # None required for OAuth
        "SESSION_COOKIE_SECURE": True if (is_cloud_run or is_production) else False,  # Required with SameSite=None
        "PERMANENT_SESSION_LIFETIME": 3600,  # 1 hour session lifetime
        "SESSION_REFRESH_EACH_REQUEST": True,  # Refresh session on each request
    })

    # --- Google OAuth Authentication ---
    from app.auth import login_manager, init_oauth
    login_manager.init_app(app)
    init_oauth(app)

    # --- Database Sync: local-dev only (skip Cloud Storage & Cloud SQL) ---
    using_postgres = bool(os.getenv("CLOUD_SQL_CONNECTION_NAME"))

    if using_postgres:
        # Cloud SQL mode – initialise later via storage.init_db()
        print("[STARTUP] Using PostgreSQL Cloud SQL – skipping SQLite sync")
    else:
        # Local development – no DB sync needed
        print("[STARTUP] No DB sync required – skipping SQLite download from Cloud Storage")

    # --- Security: Rate Limiting ---
    limiter = Limiter(
        app=app,
        key_func=get_remote_address,
        default_limits=["200 per day", "50 per hour"],
        storage_uri="memory://",
        strategy="fixed-window"
    )
    # Make limiter available to blueprints
    app.extensions['limiter'] = limiter

    # --- Security: CORS ---
    cors_origins = os.getenv("CORS_ORIGINS", "").strip()
    if cors_origins:
        # Production: restrict to specific domains
        origins = [o.strip() for o in cors_origins.split(",") if o.strip()]
        CORS(app, resources={
            r"/api/*": {
                "origins": origins,
                "methods": ["GET", "POST", "DELETE"],
                "max_age": 3600
            }
        })
    else:
        # Development: allow all origins
        CORS(app)

    # --- Security: HTTP Basic Auth for /admin ---
    def check_auth(username: str, password: str) -> bool:
        """Validate HTTP Basic Auth credentials"""
        admin_user = os.getenv("ADMIN_USER", "admin")
        admin_pass = os.getenv("ADMIN_PASSWORD", "")
        return username == admin_user and password == admin_pass and bool(admin_pass)

    def authenticate() -> Response:
        """Send 401 response that enables basic auth"""
        return Response(
            'Authentication required. Please enter your admin credentials.',
            401,
            {'WWW-Authenticate': 'Basic realm="Admin Area"'}
        )

    def requires_auth(f):
        """Decorator for routes that require HTTP Basic Auth"""
        @wraps(f)
        def decorated(*args, **kwargs):
            auth = request.authorization
            if not auth or not check_auth(auth.username, auth.password):
                return authenticate()
            return f(*args, **kwargs)
        return decorated

    # --- Storage init (Cloud SQL) ---
    from app.services import storage
    if using_postgres: # Only initialize DB if using PostgreSQL
        storage.init_db()  # Uses Cloud SQL environment variables
        # --- API auth gate ---
    # Routes that are allowed to be hit without authentication. Add new
    # ones explicitly; the default for /api/* is "must be logged in".
    API_PUBLIC_PATHS = {
        "/api/auth/user",   # auth.py exposes this so the UI can detect login state
        "/api/healthz",     # leave room for a future health-check endpoint
        "/api/progress",    # progress polling is read-only and tolerable to leave open;
                            # tighten in Wave 2 once long jobs return job ids.
    }

    def _api_auth_gate():
        """Block unauthenticated requests to /api/* except for an allowlist.

        Two ways a request can be authenticated for /api/* routes:

        1. Flask-Login session cookie — set after a successful OAuth flow
           through `/login` (the browser case).
        2. Cloud Run IAM identity token in `Authorization: Bearer ...` —
           when Cloud Run is deployed with `--no-allow-unauthenticated`
           the platform itself validates the token and the granted
           `roles/run.invoker` BEFORE forwarding the request, so by the
           time we see the Authorization header here, GCP has already
           confirmed the caller is on the invoker allowlist. We don't
           re-validate; we trust that gate.

        Once IAP lands, the third path becomes
        `X-Goog-Authenticated-User-Email` (set by IAP) which is even
        cleaner — the Flask gate can fall back to that.
        """
        path = request.path or ""
        if not path.startswith("/api/"):
            return None
        if request.method == "OPTIONS":  # CORS preflight
            return None
        # Match either exact path or path-prefix entries (so /api/progress/... is covered).
        for public in API_PUBLIC_PATHS:
            if path == public or path.startswith(public.rstrip("/") + "/"):
                return None
        # (1) Browser session.
        if getattr(current_user, "is_authenticated", False):
            return None
        # (2) Cloud Run IAM identity token. Presence is sufficient: an
        # invalid token never reaches us when the service is deployed
        # with --no-allow-unauthenticated.
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer ") and len(auth_header) > 32:
            return None
        return jsonify({"ok": False, "error": "authentication required"}), 401

    app.before_request(_api_auth_gate)

    # --- Helper to avoid duplicate blueprint registration ---
    def safe_register(bp, url_prefix="/api"):
        name = getattr(bp, "name", None)
        if name and name in app.blueprints:
            logger.warning("Skipping duplicate blueprint '%s'", name)
            return
        app.register_blueprint(bp, url_prefix=url_prefix)
        logger.info("Registered blueprint '%s'", name)

    # --- Auth Blueprint ---
    from app.auth import bp as auth_bp
    safe_register(auth_bp, url_prefix="")

    # --- API Blueprints ---
    from .api.progress import bp as progress_bp
    safe_register(progress_bp, url_prefix="/api")

    from .api.crawl import bp as crawl_bp
    safe_register(crawl_bp, url_prefix="/api")

    from .api.analyze import bp as analyze_bp
    safe_register(analyze_bp, url_prefix="/api")

    from .api.report import bp as report_bp
    safe_register(report_bp, url_prefix="/api")

    from .api.admin import bp as admin_bp
    safe_register(admin_bp, url_prefix="/api")

    # Records API — must use unique name in app/api/records.py
    from .api.records import bp as records_bp
    safe_register(records_bp, url_prefix="/api")

    # Title Normalization API
    from .api.normalize_titles import bp as normalize_titles_bp
    safe_register(normalize_titles_bp, url_prefix="/api")

    # Entity Name Update API
    from .api.update_entity_names import bp as update_entity_names_bp
    safe_register(update_entity_names_bp, url_prefix="/api")

    # Batch Re-analysis API
    from .api.batch_reanalyze import bp as batch_reanalyze_bp
    safe_register(batch_reanalyze_bp, url_prefix="/api")

    # Debug printout of registered blueprints
    print(f"\n[DEBUG] Registered blueprints: {list(app.blueprints.keys())}\n")
        # --- UI pages ---
    from flask_login import login_required

    UI_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "ui"))

    @app.route("/")
    def index():
        return send_from_directory(UI_DIR, "index.html")

    @app.route("/genetics")
    def genetics():
        return send_from_directory(UI_DIR, "genetics.html")

    @app.route("/infographic")
    def infographic():
        return send_from_directory(UI_DIR, "infographic.html")

    @app.route("/gemini-guide")
    def gemini_guide():
        return send_from_directory(UI_DIR, "gemini-gem-guide.html")

    # Admin page allowlist: env-driven (ADMIN_EMAILS, comma-separated). If
    # unset in production, the admin page is unreachable — fail closed.
    admin_emails = _split_csv_env("ADMIN_EMAILS")

    @app.route("/admin")
    @login_required
    def admin_page():
        from flask_login import current_user
        if not admin_emails:
            return Response("Admin page is not configured (ADMIN_EMAILS env var).", 503)
        if (current_user.email or "").lower() not in admin_emails:
            return Response("Access denied. This account is not authorized.", 403)
        return send_from_directory(UI_DIR, "admin.html")

    @app.route("/ui/<path:path>")
    def ui_assets(path: str):
        return send_from_directory(UI_DIR, path)

    # --- Route map (debug helper) ---
    try:
        print("\n== Registered routes ==")
        for r in sorted(app.url_map.iter_rules(), key=lambda x: (x.rule, list(x.methods))):
            methods = ",".join(sorted(r.methods - {"HEAD", "OPTIONS"}))
            print(f"{methods:6} {r.rule}")
        print("== End routes ==\n")
    except Exception as e:
        print(f"[WARN] Route debug failed: {e}")

    return app