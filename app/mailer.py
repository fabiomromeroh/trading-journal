"""Transactional email (password codes). Free, non-expiring options, chosen by env vars:

* RESEND_API_KEY  - resend.com free plan (3,000/month, 100/day). Without a verified domain the
                    sender is onboarding@resend.dev and Resend only delivers to the address the
                    Resend account was created with - fine for a single-user journal.
* BREVO_API_KEY + EMAIL_FROM - brevo.com free plan (300/day); EMAIL_FROM must be a verified sender.
* SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASSWORD - e.g. Gmail + App Password. NOT usable on Render's
                    free plan (outbound SMTP ports 25/465/587 are blocked there).

Never logs message bodies (they contain one-time codes)."""
from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


class MailError(RuntimeError):
    pass


@dataclass
class MailStatus:
    configured: bool
    provider: str | None
    sender: str | None
    note: str


def status() -> MailStatus:
    s = get_settings()
    if s.resend_api_key:
        sender = s.email_from or "Trading Journal <onboarding@resend.dev>"
        note = ("Resend test sender: only delivers to the email your Resend account uses."
                if "resend.dev" in sender else "Resend")
        return MailStatus(True, "resend", sender, note)
    if s.brevo_api_key:
        if not s.email_from:
            return MailStatus(False, "brevo", None, "BREVO_API_KEY is set but EMAIL_FROM (a verified Brevo sender) is missing.")
        return MailStatus(True, "brevo", s.email_from, "Brevo")
    if s.smtp_host:
        return MailStatus(True, "smtp", s.smtp_from or s.smtp_user, f"SMTP {s.smtp_host}:{s.smtp_port}")
    return MailStatus(False, None, None, "Email is not configured on the server (set RESEND_API_KEY).")


def mask(addr: str) -> str:
    name, _, dom = (addr or "").partition("@")
    if not dom:
        return addr
    return (name[:2] + "•" * max(1, len(name) - 2)) + "@" + dom


def _split_sender(sender: str) -> tuple[str, str]:
    if "<" in sender and sender.endswith(">"):
        name, addr = sender[:-1].split("<", 1)
        return name.strip().strip('"') or "Trading Journal", addr.strip()
    return "Trading Journal", sender.strip()


def send(to: str, subject: str, text: str) -> None:
    st = status()
    if not st.configured:
        raise MailError(st.note)
    s = get_settings()
    try:
        if st.provider == "resend":
            r = httpx.post("https://api.resend.com/emails", timeout=20,
                           headers={"Authorization": f"Bearer {s.resend_api_key}"},
                           json={"from": st.sender, "to": [to], "subject": subject, "text": text})
            if r.status_code >= 300:
                raise MailError(f"Resend rejected the email (HTTP {r.status_code}): {_err(r)}")
        elif st.provider == "brevo":
            name, addr = _split_sender(st.sender)
            r = httpx.post("https://api.brevo.com/v3/smtp/email", timeout=20,
                           headers={"api-key": s.brevo_api_key, "accept": "application/json"},
                           json={"sender": {"name": name, "email": addr}, "to": [{"email": to}],
                                 "subject": subject, "textContent": text})
            if r.status_code >= 300:
                raise MailError(f"Brevo rejected the email (HTTP {r.status_code}): {_err(r)}")
        else:
            msg = EmailMessage()
            msg["Subject"], msg["From"], msg["To"] = subject, st.sender, to
            msg.set_content(text)
            if s.smtp_port == 465:
                smtp = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=20)
            else:
                smtp = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20)
                smtp.starttls()
            with smtp:
                if s.smtp_user:
                    smtp.login(s.smtp_user, s.smtp_password)
                smtp.send_message(msg)
    except MailError:
        raise
    except Exception as exc:  # network / SMTP errors; never include the body
        raise MailError(f"Could not send email via {st.provider}: {type(exc).__name__}") from exc
    log.info("Sent '%s' email via %s to %s", subject.split(":")[0], st.provider, mask(to))


def _err(r: httpx.Response) -> str:
    try:
        d = r.json()
        return str(d.get("message") or d.get("error") or d)[:200]
    except Exception:
        return r.text[:200]
