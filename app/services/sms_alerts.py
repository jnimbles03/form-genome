"""
SMS alerting service using Twilio with throttling to prevent spam
"""
import os
import time
from typing import Optional

# Global failure tracking (in-memory, resets on restart)
_last_alert_time: Optional[float] = None
_failure_count = 0
_alert_cooldown = 3600  # 1 hour in seconds


def send_llm_failure_alert(error_message: str, force: bool = False):
    """
    Send SMS alert when LLM fails (with throttling)

    Args:
        error_message: Description of the LLM failure
        force: Skip throttling and send immediately
    """
    global _last_alert_time, _failure_count

    _failure_count += 1
    current_time = time.time()

    # Throttling: Only send if:
    # 1. Force flag is set (test alerts), OR
    # 2. First failure, OR
    # 3. Cooldown period has elapsed
    if not force:
        if _last_alert_time is not None:
            time_since_last = current_time - _last_alert_time
            if time_since_last < _alert_cooldown:
                print(f"[ALERT] Throttled ({_failure_count} failures, {int(time_since_last)}s since last alert)")
                return

    # Twilio credentials from environment
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_FROM_NUMBER")  # Your Twilio phone number
    to_number = os.getenv("ALERT_PHONE_NUMBER")    # Your phone number

    # Skip if not configured
    if not all([account_sid, auth_token, from_number, to_number]):
        print("[ALERT] SMS not configured - skipping alert")
        return

    try:
        from twilio.rest import Client
        client = Client(account_sid, auth_token)

        # Include failure count in message
        body = f"🚨 Form Genome LLM Alert\n\n{error_message}\n\nFailures: {_failure_count}\n\nCheck: genome.meyerinterests.com/admin"

        message = client.messages.create(
            body=body,
            from_=from_number,
            to=to_number
        )

        print(f"[ALERT] SMS sent: {message.sid} ({_failure_count} failures)")
        _last_alert_time = current_time
        _failure_count = 0  # Reset counter after alert
    except Exception as e:
        print(f"[ALERT] Failed to send SMS: {e}")


def send_test_alert():
    """Send a test SMS to verify configuration"""
    send_llm_failure_alert("Test alert - SMS notifications are working!", force=True)
