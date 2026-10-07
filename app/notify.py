"""Reminder email hook. Sends only when REMINDER_EMAIL_ENABLED and SMTP_* are configured;
otherwise it just logs. Called from the scheduled sync."""
from __future__ import annotations

import logging
import smtplib
from datetime import timedelta
from email.message import EmailMessage

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ImportBatch, utcnow
from app.services import get_state, set_state

log = logging.getLogger(__name__)


def send_email(subject: str, body: str) -> bool:
    s = get_settings()
    if not (s.reminders_enabled and s.smtp_host and s.reminder_email_to):
        log.info("Reminder (email not configured): %s", subject)
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, s.smtp_from or s.smtp_user, s.reminder_email_to
    msg.set_content(body)
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as smtp:
        smtp.starttls()
        if s.smtp_user:
            smtp.login(s.smtp_user, s.smtp_password)
        smtp.send_message(msg)
    return True


def maybe_send_reminders(db: Session, source_statuses: list) -> list[str]:
    """At most one reminder of each type per day."""
    s = get_settings()
    sent: list[str] = []
    today = utcnow().date().isoformat()

    def once(key: str, subject: str, body: str):
        if get_state(db, f"reminder:{key}") == today:
            return
        send_email(subject, body)
        set_state(db, f"reminder:{key}", today)
        sent.append(subject)

    for name, st in source_statuses:
        if st.configured and st.expires_in_seconds is not None and st.expires_in_seconds < 86400:
            once(f"reauth:{name}", f"Trading journal: reconnect {name}",
                 f"Your {name} login {'has expired' if st.expires_in_seconds <= 0 else 'expires within 24h'}. "
                 f"Open {s.base_url}/settings and click Reconnect.")
    last_import = db.scalar(select(func.max(ImportBatch.committed_at)).where(ImportBatch.status == "committed"))
    if last_import and utcnow() - last_import > timedelta(days=s.import_reminder_days):
        once("import", "Trading journal: import due",
             f"No trade import for {s.import_reminder_days}+ days. Export Accounts > History > Transactions "
             f"from schwab.com and drop it on {s.base_url}/import.")
    return sent
