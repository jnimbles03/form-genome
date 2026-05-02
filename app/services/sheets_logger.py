# app/services/sheets_logger.py
"""
Google Sheets logging for login tracking
"""
import os
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# Lazy-load gspread to avoid import errors if not configured
_gc = None
_sheet = None
_last_error = None

def _get_sheet():
    """
    Get or create the Google Sheets client and worksheet.
    Uses service account credentials from environment.
    """
    global _gc, _sheet, _last_error

    if _sheet is not None:
        return _sheet

    try:
        import gspread
        from google.oauth2.service_account import Credentials

        # Get service account credentials
        creds_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS")
        sheet_id = os.environ.get("LOGIN_SHEET_ID")

        if not creds_json or not sheet_id:
            _last_error = "GOOGLE_SHEETS_CREDENTIALS or LOGIN_SHEET_ID not set"
            return None

        # Create credentials
        scopes = [
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive'
        ]

        # Parse JSON from env var
        import json
        creds_dict = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)

        # Authorize gspread
        _gc = gspread.authorize(creds)

        # Open the sheet
        spreadsheet = _gc.open_by_key(sheet_id)
        _sheet = spreadsheet.sheet1  # Use first sheet

        # Ensure headers exist
        try:
            first_row = _sheet.row_values(1)
            if not first_row or first_row[0] != "Timestamp":
                _sheet.insert_row(
                    ["Timestamp", "Date", "Time", "Email", "Name", "IP Address", "User Agent"],
                    index=1
                )
        except Exception:
            # If sheet is empty, add headers
            _sheet.append_row(
                ["Timestamp", "Date", "Time", "Email", "Name", "IP Address", "User Agent"]
            )

        logger.info(f"✓ Connected to Google Sheet: {sheet_id}")
        return _sheet

    except Exception as e:
        _last_error = str(e)
        logger.error(f"Failed to connect to Google Sheets: {e}")
        return None

def log_to_sheet(
    email: str,
    name: Optional[str] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None
) -> bool:
    """
    Append a login event to Google Sheets.
    Returns True if successful, False otherwise.
    """
    sheet = _get_sheet()
    if sheet is None:
        logger.warning(f"Sheets logging disabled: {_last_error}")
        return False

    try:
        now = datetime.utcnow()
        timestamp = now.isoformat() + "Z"
        date = now.strftime("%Y-%m-%d")
        time = now.strftime("%H:%M:%S UTC")

        # Truncate user agent to avoid cell size limits
        user_agent_short = (user_agent or "")[:200]

        row = [
            timestamp,
            date,
            time,
            email or "",
            name or "",
            ip_address or "",
            user_agent_short
        ]

        sheet.append_row(row, value_input_option='RAW')
        logger.info(f"✓ Logged to Google Sheets: {email}")
        return True

    except Exception as e:
        logger.error(f"Failed to log to Google Sheets: {e}")
        return False

def get_status() -> dict:
    """
    Get the status of Google Sheets logging.
    """
    sheet = _get_sheet()
    if sheet is None:
        return {
            "enabled": False,
            "error": _last_error or "Not configured"
        }

    try:
        row_count = len(sheet.get_all_values())
        return {
            "enabled": True,
            "sheet_id": os.environ.get("LOGIN_SHEET_ID"),
            "row_count": row_count,
            "url": f"https://docs.google.com/spreadsheets/d/{os.environ.get('LOGIN_SHEET_ID')}"
        }
    except Exception as e:
        return {
            "enabled": False,
            "error": str(e)
        }
