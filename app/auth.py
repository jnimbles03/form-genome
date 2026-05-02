# app/auth.py
"""
Google OAuth authentication with domain restriction
"""
import os
import logging
from flask import Blueprint, redirect, url_for, session, request, jsonify
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from authlib.integrations.flask_client import OAuth

# Setup logging
logger = logging.getLogger(__name__)


def _split_csv_env(name: str) -> list[str]:
    """Read a comma-separated env var and return a lowercased, stripped list."""
    raw = os.getenv(name, "")
    return [v.strip().lower() for v in raw.split(",") if v.strip()]


# Allowed email domains. Driven entirely by the ALLOWED_EMAIL_DOMAINS env var
# in production. The previous default (`gmail.com`) silently allowed any
# Google account holder; that is now a deploy-time decision.
ALLOWED_DOMAINS = _split_csv_env("ALLOWED_EMAIL_DOMAINS") or ["docusign.com"]

# Optional per-email allowlist. If set, an email must EITHER match one of
# these addresses OR have a domain in ALLOWED_DOMAINS. Useful for scoping
# access to specific external collaborators without opening their entire
# domain.
ALLOWED_EMAILS = _split_csv_env("ALLOWED_EMAILS")

# User model
class User(UserMixin):
    def __init__(self, email, name, picture):
        self.id = email
        self.email = email
        self.name = name
        self.picture = picture

# Flask-Login setup
login_manager = LoginManager()

@login_manager.user_loader
def load_user(user_id):
    """Load user from session"""
    if "user" in session:
        user_data = session["user"]
        return User(user_data["email"], user_data["name"], user_data.get("picture"))
    return None

@login_manager.unauthorized_handler
def unauthorized():
    """Redirect to login page if not authenticated"""
    return redirect(url_for("auth.login", next=request.url))

# OAuth setup
oauth = OAuth()

def init_oauth(app):
    """Initialize OAuth with Flask app"""
    oauth.init_app(app)

    # Register Google OAuth
    oauth.register(
        name="google",
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"}
    )

# Auth blueprint
bp = Blueprint("auth", __name__)

@bp.route("/login")
def login():
    """Redirect to Google OAuth"""
    # Save the original destination URL (if provided via 'next' parameter)
    next_url = request.args.get('next') or request.referrer or "/"
    session['next_url'] = next_url

    # Check if OAuth credentials are configured
    if not os.getenv("GOOGLE_CLIENT_ID") or not os.getenv("GOOGLE_CLIENT_SECRET"):
        logger.error("❌ OAuth not configured! Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET environment variables.")
        return "Authentication is not configured. Please contact the administrator.", 500

    # Use Google OAuth
    redirect_uri = url_for("auth.callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)

@bp.route("/callback")
def callback():
    """Handle OAuth callback"""
    try:
        # Attempt to authorize and get access token
        # This will raise an error if state doesn't match
        token = oauth.google.authorize_access_token()
        user_info = token.get("userinfo")

        if not user_info:
            return "Failed to get user info", 400

        email = user_info.get("email")

        # Check if email domain is allowed
        if not email:
            return "No email provided", 400

        email_lower = email.lower()
        domain = email_lower.split("@")[-1]
        if email_lower not in ALLOWED_EMAILS and domain not in ALLOWED_DOMAINS:
            logger.warning("Auth denied for email outside allowlist: %s", email_lower)
            return "Access denied. This account is not authorized.", 403

        # Create user and log in
        user = User(
            email=email,
            name=user_info.get("name", email),
            picture=user_info.get("picture")
        )

        # Store user in session
        session["user"] = {
            "email": user.email,
            "name": user.name,
            "picture": user.picture
        }

        login_user(user)

        # Log successful login
        try:
            from app.services import storage
            from app.services import sheets_logger
            ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)
            user_agent = request.headers.get('User-Agent', 'Unknown')

            # Log to database
            storage.log_login(
                email=user.email,
                name=user.name,
                ip_address=ip_address,
                user_agent=user_agent
            )

            # Log to Google Sheets (non-blocking)
            sheets_logger.log_to_sheet(
                email=user.email,
                name=user.name,
                ip_address=ip_address,
                user_agent=user_agent
            )

            logger.info(f"✓ Login: {user.email} from {ip_address}")
        except Exception as log_err:
            # Don't fail login if logging fails
            logger.error(f"Failed to log login: {log_err}")

        # Redirect to saved destination or homepage
        next_url = session.pop('next_url', '/')
        return redirect(next_url)

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Authentication failed: {error_msg}")

        # Provide helpful error messages for common issues
        if "mismatching_state" in error_msg or "CSRF" in error_msg:
            return """
            <h2>OAuth Session Error</h2>
            <p>The authentication session expired or cookies were blocked.</p>
            <h3>Solutions:</h3>
            <ul>
                <li>Try logging in again (cookies may have been blocked)</li>
                <li>Ensure cookies are enabled in your browser</li>
                <li>If using incognito/private mode, try regular mode</li>
                <li>Clear your browser cache and try again</li>
            </ul>
            <a href="/login">← Try Login Again</a>
            <hr>
            <small>Technical details: """ + error_msg + """</small>
            """, 400

        return f"Authentication failed: {error_msg}", 400

@bp.route("/logout")
@login_required
def logout():
    """Log out user"""
    logout_user()
    session.pop("user", None)
    return redirect("/login")

@bp.route("/api/auth/user")
def current_user_info():
    """Get current user info (for UI)"""
    if current_user.is_authenticated:
        return jsonify({
            "authenticated": True,
            "email": current_user.email,
            "name": current_user.name,
            "picture": current_user.picture
        })
    return jsonify({"authenticated": False})
