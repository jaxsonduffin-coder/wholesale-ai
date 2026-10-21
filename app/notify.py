"""Notify the owner when a seller is actually interested.

The promise of this system is that you only get pulled in when it matters. This
module is the other half of that promise: when a lead crosses the "wants to
talk" line, you get told — by SMS and email to your own number, not to the
lead's — with everything you need to close in one message.

Every notification is also recorded as an Alert row so nothing depends on you
having seen a text: the dashboard's "Needs you" queue is the source of truth.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from .channels import email as email_channel
from .channels import telnyx
from .config import settings
from .models import Alert, Lead, utcnow

log = logging.getLogger("wholesale_ai.notify")


def owner_phone() -> str:
    return (settings.owner_alert_sms or settings.agent_phone or "").strip()


def owner_email() -> str:
    return (settings.owner_alert_email or settings.agent_email or "").strip()


def _money(v: float | None) -> str:
    return f"${v:,.0f}" if v else "—"


def build_hot_lead_message(lead: Lead, reason: str, reply: str = "") -> str:
    """A single SMS you can act on from your lock screen."""
    lines = [f"🔥 {lead.owner_name or 'Seller'} wants to talk"]
    lines.append(lead.address)
    if lead.max_offer:
        lines.append(f"Offer ready: {_money(lead.max_offer)} (ARV {_money(lead.arv)})")
    if reply:
        clean = " ".join(reply.split())[:140]
        lines.append(f'They said: "{clean}"')
    lines.append(reason)
    if lead.owner_phone:
        lines.append(f"Call: {lead.owner_phone}")
    return "\n".join(lines)


def build_hot_lead_email(lead: Lead, reason: str, reply: str = "") -> tuple[str, str]:
    subject = f"Hot lead: {lead.address} — wants to talk"
    body = (
        f"{lead.owner_name or 'The owner'} at {lead.address} replied and looks "
        f"ready to talk.\n\n"
        f"WHY YOU ARE BEING NOTIFIED\n"
        f"  {reason}\n\n"
        f"WHAT THEY SAID\n"
        f'  "{" ".join((reply or "").split())[:600]}"\n\n'
        f"THE NUMBERS\n"
        f"  Max offer : {_money(lead.max_offer)}\n"
        f"  ARV       : {_money(lead.arv)} ({lead.comp_count} comps)\n"
        f"  Repairs   : {_money(lead.repair_estimate)}\n"
        f"  Target fee: {_money(lead.assignment_fee)}\n\n"
        f"CONTACT\n"
        f"  Phone: {lead.owner_phone or '—'}\n"
        f"  Email: {lead.owner_email or '—'}\n"
        f"  Grade: {lead.grade}   Class: {lead.lead_class}\n\n"
        f"Open the deal: {settings.public_base_url or ''}/?deal={lead.id}\n\n"
        f"Automation has been paused on this lead so nothing else goes out "
        f"while you work it."
    )
    return subject, body


def notify_owner(
    db: Session,
    lead: Lead | None,
    *,
    kind: str = "hot_lead",
    title: str,
    body: str,
    sms_body: str = "",
) -> list[Alert]:
    """Record an alert and push it to you. Never raises on send failure."""
    made: list[Alert] = []

    # 1. In-app: always succeeds, and is the queue the dashboard reads.
    inapp = Alert(
        lead_id=lead.id if lead else None,
        kind=kind,
        title=title[:255],
        body=body,
        channel="in_app",
        status="sent",
    )
    db.add(inapp)
    made.append(inapp)

    if kind in ("system",):
        return made

    # 2. SMS to you. This is the one that actually gets your attention.
    phone = owner_phone()
    if phone and sms_body:
        res = telnyx.send_sms(phone, sms_body)
        row = Alert(
            lead_id=lead.id if lead else None,
            kind=kind,
            title=title[:255],
            body=sms_body,
            channel="sms",
            status=res.status if res.ok else "failed",
            error=(res.error or "")[:300],
        )
        db.add(row)
        made.append(row)
        if not res.ok:
            log.warning("owner alert sms failed: %s", res.error)

    # 3. Email to you, with the full detail.
    to = owner_email()
    if to and body:
        res = email_channel.send_email(to, title[:200], body)
        row = Alert(
            lead_id=lead.id if lead else None,
            kind=kind,
            title=title[:255],
            body=body,
            channel="email",
            status=res.status if res.ok else "failed",
            error=(res.error or "")[:300],
        )
        db.add(row)
        made.append(row)
        if not res.ok:
            log.warning("owner alert email failed: %s", res.error)

    db.flush()
    return made


def flag_for_human(db: Session, lead: Lead, *, reason: str, intent=None) -> bool:
    """Put a lead in the "needs you" queue.

    Returns True if this is a *new* escalation, so the caller only sends one
    notification per burst of replies rather than one per message.
    """
    already = bool(lead.needs_attention)
    lead.needs_attention = True
    lead.attention_reason = reason[:255]
    lead.attention_at = utcnow()
    if intent is not None:
        lead.intent_label = intent.label[:24]
        lead.intent_score = int(intent.score)
    if not lead.interested_at:
        lead.interested_at = utcnow()

    # A human is taking over; stop the automation immediately.
    for enr in lead.enrollments:
        if enr.state == "active":
            enr.state = "paused"
            enr.stopped_reason = f"human handoff: {reason}"[:120]
            enr.next_run_at = None

    db.flush()
    return not already


def resolve_attention(db: Session, lead: Lead, note: str = "") -> None:
    """Mark the handoff as handled so the lead leaves the queue."""
    lead.needs_attention = False
    lead.attention_reason = ""
    lead.attention_at = None
    db.flush()
