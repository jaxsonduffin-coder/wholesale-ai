"""Cadence execution engine — the thing that actually does the following up.

Every step: render copy -> compliance gate -> send -> record Contact ->
advance. A lead that replies, opts out, or converts is pulled out of the
cadence immediately, so no one ever gets the "last touch" text after saying
"call me, let's do the deal".
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .channels import email as email_channel
from .channels import telnyx
from .compliance import check_send_allowed, next_allowed_time
from .config import settings
from .policy import channel_allowed, normalize_class
from .models import (
    Cadence,
    CadenceStep,
    Contact,
    Enrollment,
    Lead,
    utcnow,
)
from .templates_content import get_template


@dataclass
class StepResult:
    action: str
    # sent            - transmitted (or simulated) and recorded
    # skipped         - temporarily impossible (quiet hours, retry later)
    # skipped_permanent - this lead may never receive this channel (policy);
    #                     consume the step and carry on with the sequence
    # blocked         - lead is suppressed/invalid; stop the whole cadence
    # failed          - provider rejected the send
    channel: str
    reason: str = ""
    contact_id: int | None = None
    permanent: bool = False


def _first_name(lead: Lead) -> str:
    if lead.owner_first_name:
        return lead.owner_first_name
    if lead.owner_name:
        return lead.owner_name.split()[0].title()
    return "there"


def render(text: str, lead: Lead) -> str:
    """Fill placeholders. Unknown placeholders collapse to '' rather than
    leaking '{agent_name}' into a live message."""
    if not text:
        return ""
    arv = lead.arv or 0
    max_offer = lead.max_offer or 0
    if max_offer and arv:
        offer_low = f"${max_offer:,.0f}"
        offer_high = f"${min(arv * 0.85, max_offer * 1.22):,.0f}"
    else:
        offer_low = offer_high = "a number that works"

    mapping = {
        "first_name": _first_name(lead),
        "owner_name": lead.owner_name or "",
        "street": lead.street or "",
        "city": lead.city or "",
        "state": lead.state or "",
        "zip": lead.zip_code or "",
        "address": lead.address,
        "agent_name": settings.agent_name,
        "agent_company": settings.agent_company,
        "agent_phone": settings.agent_phone,
        "agent_email": settings.agent_email,
        "calendar_link": settings.agent_calendar_link or "—",
        "postal_address": settings.agent_postal_address
        or f"{settings.agent_company} (add AGENT_POSTAL_ADDRESS)",
        "today": dt.datetime.now().strftime("%B %d, %Y"),
        "mailing_address": (
            lead.owner_mailing_address or lead.address or ""
        ),
        "offer_low": offer_low,
        "offer_high": offer_high,
        "max_offer": f"${max_offer:,.0f}" if max_offer else "",
        "arv": f"${arv:,.0f}" if arv else "",
    }
    out = text
    for key, value in mapping.items():
        out = out.replace("{" + key + "}", str(value))
    return out


def cadence_for_lead(db: Session, lead: Lead) -> Cadence | None:
    """Pick the cadence that matches this lead's class.

    This is how the channel mix stays correct: a list lead can only ever be
    enrolled in a cadence that contains no SMS step.
    """
    cls = normalize_class(getattr(lead, "lead_class", None))
    cadence = db.scalar(
        select(Cadence).where(Cadence.lead_class == cls, Cadence.is_active.is_(True))
    )
    if cadence:
        return cadence
    # Fall back to the untextable "list" cadence rather than the inbound one, so
    # an unknown class can never accidentally get an SMS sequence.
    return db.scalar(
        select(Cadence).where(Cadence.lead_class == "list", Cadence.is_active.is_(True))
    )


def enroll(db: Session, lead: Lead, cadence: Cadence | None = None) -> Enrollment | None:
    """Put a lead at step 0 of the cadence for its class."""
    if lead.opted_out or lead.status in ("dead", "closed"):
        return None
    existing = db.scalar(
        select(Enrollment).where(
            Enrollment.lead_id == lead.id, Enrollment.state == "active"
        )
    )
    if existing:
        return existing
    if cadence is None:
        cadence = cadence_for_lead(db, lead)
    if cadence is None:
        return None

    now = utcnow()
    first_step = cadence.steps[0] if cadence.steps else None
    first_delay = first_step.delay_hours if first_step else 0.0
    next_run = now + dt.timedelta(hours=first_delay)

    # Quiet hours only constrain phone channels. A letter may be queued at any
    # hour, and deferring it to 9am would needlessly delay the whole sequence.
    if (
        settings.enforce_quiet_hours
        and first_step is not None
        and first_step.channel in ("call", "sms")
    ):
        earliest = next_allowed_time(lead)
        if next_run < earliest:
            next_run = earliest

    enr = Enrollment(
        lead_id=lead.id,
        cadence_id=cadence.id,
        step_index=0,
        state="active",
        next_run_at=next_run,
    )
    db.add(enr)
    db.flush()
    return enr


def _record(
    db: Session,
    lead: Lead,
    *,
    channel: str,
    body: str,
    subject: str,
    status: str,
    provider_id: str = "",
    error: str = "",
    cadence_step: str = "",
    meta: dict | None = None,
) -> Contact:
    c = Contact(
        lead_id=lead.id,
        channel=channel,
        direction="outbound",
        body=body,
        subject=subject,
        status=status,
        provider_id=provider_id,
        error=error[:500],
        cadence_step=cadence_step,
        meta=meta or {},
    )
    db.add(c)
    db.flush()
    return c


def _letter(lead: Lead, body: str) -> str:
    """Wrap the letter copy in an envelope-ready block for the mail house."""
    to_addr = (lead.owner_mailing_address or lead.address or "").strip()
    return (
        "----- LETTER -----\n"
        f"TO: {lead.owner_name or 'Current Resident'}\n"
        f"    {to_addr.replace(chr(10), ', ')}\n"
        "\n"
        f"{body.strip()}\n"
        "------------------\n"
    )


def execute_step(
    db: Session, lead: Lead, step: CadenceStep, cadence: Cadence
) -> StepResult:
    """Run a single cadence step for a lead.

    Two independent gates guard the channel:
      1. policy.channel_allowed  - may this CLASS of lead receive this channel?
      2. compliance.check_send_allowed - is this specific send legal right now?
    A failure of (1) is permanent for the step: the sequence skips it and
    carries on. A failure of (2) is either temporary (quiet hours, caps) or
    fatal (suppression, opt-out).
    """
    label = f"{cadence.name}:{step.position}:{step.channel}"

    # ── Gate 1: channel policy for this lead class ─────────────────
    policy_ok, policy_reason = channel_allowed(lead, step.channel)
    if not policy_ok:
        _record(
            db, lead,
            channel=step.channel, body="", subject="",
            status="blocked_policy", error=policy_reason, cadence_step=label,
            meta={"lead_class": lead.lead_class, "policy": policy_reason},
        )
        return StepResult(
            "skipped_permanent", step.channel, policy_reason, permanent=True
        )

    template = get_template(db, step.template_key) if step.template_key else None
    body_tpl = step.body_override or (template.body if template else "")
    subj_tpl = step.subject_override or (template.subject if template else "")

    if not body_tpl:
        return StepResult("skipped", step.channel, "no template copy")

    # ── Gate 2: per-send compliance ────────────────────────────────
    allowed, reason = check_send_allowed(
        db,
        lead,
        step.channel,
        max_sms_per_day=settings.max_sms_per_lead_per_day,
        max_calls_per_week=settings.max_call_attempts_per_lead_per_week,
        enforce_quiet_hours=settings.enforce_quiet_hours,
    )
    if not allowed:
        permanent = any(
            k in reason
            for k in ("opted out", "suppression", "DNC", "dead", "closed", "no phone")
        )
        if permanent:
            _record(
                db, lead,
                channel=step.channel, body="", subject="",
                status="blocked_compliance", error=reason, cadence_step=label,
            )
            return StepResult("blocked", step.channel, reason)
        return StepResult("skipped", step.channel, reason)

    body = render(body_tpl, lead)
    subject = render(subj_tpl, lead)

    if step.channel == "sms":
        res = telnyx.send_sms(lead.owner_phone or "", body)
        contact = _record(
            db, lead,
            channel="sms", body=body, subject="",
            status=res.status, provider_id=res.provider_id,
            error=res.error, cadence_step=label,
            meta={"simulated": res.simulated},
        )
    elif step.channel == "email":
        if not lead.owner_email:
            _record(
                db, lead, channel="email", body=body, subject=subject,
                status="failed", error="no email on file", cadence_step=label,
            )
            return StepResult("failed", "email", "no email on file")
        res = email_channel.send_email(lead.owner_email, subject, body)
        contact = _record(
            db, lead,
            channel="email", body=body, subject=subject,
            status=res.status, provider_id=res.provider_id,
            error=res.error, cadence_step=label,
            meta={"simulated": res.simulated},
        )
    elif step.channel == "call":
        vtpl = get_template(db, "voice_script")
        script = render(vtpl.body if vtpl else body_tpl, lead)
        res = telnyx.start_ai_call(lead.owner_phone or "", script, lead_id=lead.id)
        contact = _record(
            db, lead,
            channel="call", body=script, subject="",
            status=res.status, provider_id=res.provider_id,
            error=res.error, cadence_step=label,
            meta={"simulated": res.simulated},
        )
    elif step.channel == "mail":
        # Physical mail cannot be transmitted from here. It is queued with the
        # fully rendered letter so it can be printed or exported to a mail
        # house. Nothing is ever marked "sent" that we did not actually send.
        letter = _letter(lead, body)
        contact = _record(
            db, lead,
            channel="mail", body=letter, subject="",
            status="queued_for_mail", provider_id="",
            error="", cadence_step=label,
            meta={
                "to_name": lead.owner_name or "Current Resident",
                "to_address": lead.owner_mailing_address or lead.address,
                "template": step.template_key,
                "requires_manual_dispatch": True,
            },
        )
        res = None
    else:
        return StepResult("skipped", step.channel, f"unknown channel {step.channel}")

    if step.channel == "mail":
        # Queuing a letter is a successful step even though nothing left the
        # building yet.
        if lead.status == "new":
            lead.status = "contacted"
        return StepResult("sent", "mail", "queued for printing", contact.id)

    if res is not None and res.ok and lead.status == "new":
        lead.status = "contacted"
    if res is not None and not res.ok:
        return StepResult("failed", step.channel, res.error, contact.id)
    return StepResult("sent", step.channel, "", contact.id)


def next_step_at(cadence: Cadence, step_index: int, enrolled_at: dt.datetime) -> dt.datetime | None:
    """Absolute time for step_index+1, based on enrollment start."""
    nxt = step_index + 1
    if nxt >= len(cadence.steps):
        return None
    return enrolled_at + dt.timedelta(hours=cadence.steps[nxt].delay_hours)


def process_due(db: Session, limit: int = 100) -> dict:
    """Advance every enrollment whose next_run_at has passed.

    Called by the scheduler every few minutes. This is the heartbeat of the
    whole system — if it is not running, nothing gets contacted.
    """
    now = utcnow()
    stats = {
        "steps_run": 0, "sent": 0, "skipped": 0, "skipped_policy": 0,
        "blocked": 0, "failed": 0, "completed": 0,
    }

    due = db.scalars(
        select(Enrollment)
        .where(
            Enrollment.state == "active",
            Enrollment.next_run_at.is_not(None),
            Enrollment.next_run_at <= now,
        )
        .order_by(Enrollment.next_run_at)
        .limit(limit)
    ).all()

    for enr in due:
        lead = db.get(Lead, enr.lead_id)
        cadence = db.get(Cadence, enr.cadence_id)
        if not lead or not cadence:
            enr.state = "stopped"
            enr.stopped_reason = "missing lead or cadence"
            continue

        if lead.opted_out or lead.status in ("dead", "closed"):
            enr.state = "stopped"
            enr.stopped_reason = "lead no longer workable"
            enr.next_run_at = None
            continue

        # Human engagement pauses automation — from here a person drives.
        if lead.status in ("engaged", "qualified", "appointment", "offer_made",
                           "under_contract", "signed", "sent_to_jv"):
            enr.state = "paused"
            enr.stopped_reason = f"lead reached {lead.status}"
            enr.next_run_at = None
            continue

        if enr.step_index >= len(cadence.steps):
            enr.state = "completed"
            enr.next_run_at = None
            stats["completed"] += 1
            continue

        step = cadence.steps[enr.step_index]
        result = execute_step(db, lead, step, cadence)
        stats["steps_run"] += 1
        key = "skipped_policy" if result.action == "skipped_permanent" else result.action
        stats[key if key in stats else "skipped"] += 1

        if result.action == "blocked":
            enr.state = "stopped"
            enr.stopped_reason = result.reason
            enr.next_run_at = None
            continue

        if result.action == "skipped" and not result.permanent:
            # Retry shortly (quiet hours, caps) rather than burning the step.
            enr.next_run_at = now + dt.timedelta(minutes=45)
            continue

        # sent / failed / skipped_permanent all consume the step.
        # Step consumed — schedule the next one.
        enr.step_index += 1
        nxt = next_step_at(cadence, enr.step_index - 1, enr.started_at)
        if nxt is None:
            enr.state = "completed"
            enr.next_run_at = None
            stats["completed"] += 1
        else:
            upcoming = cadence.steps[enr.step_index]
            if upcoming.channel in ("call", "sms") and settings.enforce_quiet_hours:
                enr.next_run_at = max(nxt, next_allowed_time(lead))
            else:
                enr.next_run_at = nxt
        lead.updated_at = utcnow()

    db.commit()
    return stats
