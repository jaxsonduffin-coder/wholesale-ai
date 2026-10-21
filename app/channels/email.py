"""Email channel — Resend or SMTP, with a dry-run fallback."""
from __future__ import annotations

import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

import httpx

from ..config import settings


@dataclass
class EmailResult:
    ok: bool
    status: str
    provider_id: str = ""
    error: str = ""
    simulated: bool = False


def send_email(to: str, subject: str, body: str) -> EmailResult:
    if not settings.email_live:
        return EmailResult(
            ok=True, status="simulated", simulated=True,
            error="Email not configured — logged, not sent",
        )
    if settings.email_provider == "resend":
        return _send_resend(to, subject, body)
    return _send_smtp(to, subject, body)


def _send_resend(to: str, subject: str, body: str) -> EmailResult:
    try:
        with httpx.Client(timeout=25) as client:
            r = client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {settings.resend_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": settings.email_from,
                    "to": [to],
                    "subject": subject,
                    "text": body,
                },
            )
        if r.status_code >= 400:
            return EmailResult(ok=False, status="failed", error=f"{r.status_code}: {r.text[:300]}")
        return EmailResult(ok=True, status="sent", provider_id=(r.json() or {}).get("id", ""))
    except httpx.HTTPError as e:
        return EmailResult(ok=False, status="failed", error=f"network: {e}")


def _send_smtp(to: str, subject: str, body: str) -> EmailResult:
    msg = EmailMessage()
    msg["From"] = settings.email_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        if settings.smtp_port == 465:
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=25) as s:
                if settings.smtp_user:
                    s.login(settings.smtp_user, settings.smtp_password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=25) as s:
                s.starttls()
                if settings.smtp_user:
                    s.login(settings.smtp_user, settings.smtp_password)
                s.send_message(msg)
        return EmailResult(ok=True, status="sent")
    except Exception as e:  # smtplib raises a wide range
        return EmailResult(ok=False, status="failed", error=str(e)[:300])
