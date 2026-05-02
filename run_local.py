#!/usr/bin/env python3
"""
Simple local server runner.

Pulls environment variables from your shell (or a .env file you've
sourced). NEVER hardcode production credentials in this file again —
prior versions inlined the Postgres password and admin PIN, both of
which leaked through git when this file was committed.

Local-dev pattern:
    export CLOUD_SQL_CONNECTION_NAME=formgenome:us-central1:formgenome-db
    export DB_NAME=postgres DB_USER=postgres
    export DB_PASSWORD="$(gcloud secrets versions access latest --secret=formgenome-db-password --project=formgenome)"
    export ADMIN_PIN="$(gcloud secrets versions access latest --secret=admin-pin --project=formgenome)"
    export SECRET_KEY="$(gcloud secrets versions access latest --secret=flask-secret-key --project=formgenome)"
    export FLASK_ENV=development
    python3 run_local.py
"""
import os
import sys

# Provide non-sensitive defaults; refuse to start if any secret is missing.
os.environ.setdefault('CLOUD_SQL_CONNECTION_NAME', 'formgenome:us-central1:formgenome-db')
os.environ.setdefault('DB_NAME', 'postgres')
os.environ.setdefault('DB_USER', 'postgres')
os.environ.setdefault('FLASK_ENV', 'development')

_REQUIRED = ('DB_PASSWORD', 'ADMIN_PIN', 'SECRET_KEY')
_missing = [k for k in _REQUIRED if not os.environ.get(k)]
if _missing:
    print(
        "ERROR: missing required env var(s): "
        + ", ".join(_missing)
        + "\n\nPull from Secret Manager (or your password manager) before starting:\n"
          "  export DB_PASSWORD=\"$(gcloud secrets versions access latest "
          "--secret=formgenome-db-password --project=formgenome)\"\n"
          "  export ADMIN_PIN=\"$(gcloud secrets versions access latest "
          "--secret=admin-pin --project=formgenome)\"\n"
          "  export SECRET_KEY=\"$(gcloud secrets versions access latest "
          "--secret=flask-secret-key --project=formgenome)\"",
        file=sys.stderr,
    )
    sys.exit(1)

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

print("=" * 80)
print("  🧬 FORM GENOME LOCAL SERVER")
print("=" * 80)
print()
print("Environment:")
print(f"  Cloud SQL: {os.environ['CLOUD_SQL_CONNECTION_NAME']}")
print(f"  Database: {os.environ['DB_NAME']}")
print()
print("Starting server on http://localhost:8080")
print()
print("Test pages:")
print("  • Dashboard: http://localhost:8080/")
print("  • Genetics:  http://localhost:8080/genetics")
print("  • API Test:  http://localhost:8080/api/records?committed=1&limit=10")
print()
print("=" * 80)
print()

# Import and run the app
from app import create_app

app = create_app()

if __name__ == '__main__':
    app.run(
        host='0.0.0.0',
        port=8080,
        debug=True,
        use_reloader=False  # Disable reloader to avoid Google Drive issues
    )
