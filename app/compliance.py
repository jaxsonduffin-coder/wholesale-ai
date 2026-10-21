"""TCPA / DNC / quiet-hours enforcement.

This is not optional decoration. Automated calling and texting of property
owners carries per-message statutory damages ($500-$1,500), and wholesalers
targeting distressed owners have been a favourite plaintiff target.

What we enforce:
  1. Regional calling/SMS windows (federal 8am-9pm, plus stricter states).
  2. A suppression list checked before *every* send.
  3. DNC scrubbing before any call.
  4. Frequency caps per lead.
  5. Instant, permanent opt-out on receipt of a stop keyword.
  6. An immutable consent log (ConsentLog) for defence.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import ConsentLog, Contact, Lead, SuppressionEntry, utcnow

# Court-approved opt-out keywords (CTIA / carrier requirements).
STOP_KEYWORDS = {
    "stop", "stopall", "unsubscribe", "cancel", "end", "quit", "optout",
    "opt out", "remove", "stop texting", "dont text", "do not text",
    "revoke", "no more", "leave me alone", "remove me", "do not call",
    "do not text", "dont call", "dont text", "stop calling", "stop texting",
    "wrong number", "take me off",
}
# NOTE: "yes" is deliberately absent. Carriers treat YES as a re-opt-in token,
# but in a wholesale conversation "yes" almost always means "yes, I'll sell" —
# far too valuable to swallow as a subscription command.
START_KEYWORDS = {"start", "unstop", "resume"}
HELP_KEYWORDS = {"help", "info", "?", "support"}

# ─────────────────────────────────────────────────────────────
# Contact windows, recipient-local time.
# Federal baseline: no calls or texts before 8am or after 9pm.
# States with stricter windows are listed; their window wins.
# ─────────────────────────────────────────────────────────────
FEDERAL_WINDOW = (8, 21)
STATE_WINDOWS: dict[str, tuple[int, int]] = {
    "AL": (8, 20),
    "FL": (8, 20),
    "LA": (8, 20),
    "MS": (8, 20),
    "NY": (8, 20),
    "OK": (8, 20),
    "TX": (9, 21),   # TX: no calls before 9am
    "WA": (8, 20),
    "WY": (8, 20),
    "AR": (8, 20),
    "CT": (9, 20),
    "MA": (8, 20),
    "ME": (8, 20),
    "NH": (8, 20),
    "RI": (8, 20),
    "SC": (8, 20),
    "SD": (8, 20),
    "TN": (8, 20),
    "VT": (8, 20),
}

# US state -> IANA tz for the states we can determine without a lookup service.
STATE_TZ: dict[str, str] = {
    **{s: "America/New_York" for s in
       ["CT", "DE", "DC", "GA", "IN", "KY", "ME", "MD", "MA", "MI", "NH",
        "NJ", "NY", "NC", "OH", "PA", "RI", "SC", "VT", "VA", "WV"]},
    **{s: "America/Chicago" for s in
       ["AL", "AR", "IL", "IA", "KS", "LA", "MN", "MS", "MO", "NE", "ND",
        "OK", "SD", "TN", "TX", "WI"]},
    **{s: "America/Denver" for s in ["CO", "MT", "NM", "UT", "WY"]},
    **{s: "America/Phoenix" for s in ["AZ"]},
    **{s: "America/Los_Angeles" for s in ["CA", "NV", "WA"]},
    "AK": "America/Anchorage",
    "HI": "Pacific/Honolulu",
    "OR": "America/Los_Angeles",
    "ID": "America/Denver",
    "FL": "America/New_York",
    "PR": "America/Puerto_Rico",
}


def timezone_for_state(state: str, city: str = "") -> str:
    # A few well-known tz splits inside states.
    st = (state or "").upper()
    c = (city or "").lower()
    if st == "TX" and any(x in c for x in ("el paso", "socorro", "horizon city")):
        return "America/Denver"
    if st == "FL" and any(x in c for x in ("pensacola", "panama city", "tallahassee")):
        return "America/Chicago"
    if st == "IN" and any(x in c for x in ("gary", "hammond", "evansville")):
        return "America/Chicago"
    if st == "KY" and any(x in c for x in ("louisville", "lexington", "bowling green")):
        return "America/New_York"
    return STATE_TZ.get(st, "America/Chicago")


def contact_window(state: str) -> tuple[int, int]:
    """(start_hour, end_hour) in recipient-local time, inclusive of start,
    exclusive of end."""
    start, end = FEDERAL_WINDOW
    st = (state or "").upper()
    if st in STATE_WINDOWS:
        s2, e2 = STATE_WINDOWS[st]
        start = max(start, s2)
        end = min(end, e2)
    return start, end


def local_now(tz_name: str) -> dt.datetime:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("America/Chicago")
    return dt.datetime.now(tz)


def is_within_window(lead: Lead, at: dt.datetime | None = None) -> tuple[bool, str]:
    tz_name = lead.timezone or timezone_for_state(lead.state, lead.city)
    now = at.astimezone(ZoneInfo(tz_name)) if at else local_now(tz_name)
    start, end = contact_window(lead.state)
    if start <= now.hour < end:
        return True, ""
    return False, (
        f"outside contact window for {lead.state or 'US'}: "
        f"{start}:00-{end}:00 local (now {now:%H:%M} {tz_name})"
    )


def next_allowed_time(lead: Lead, at: dt.datetime | None = None) -> dt.datetime:
    """Earliest UTC moment we may contact this lead.

    If we are already inside the window, that moment is now — returning
    tomorrow 9am would stall every cadence that started during business hours.
    """
    tz_name = lead.timezone or timezone_for_state(lead.state, lead.city)
    tz = ZoneInfo(tz_name)
    now = at.astimezone(tz) if at else dt.datetime.now(tz)
    start, end = contact_window(lead.state)

    if start <= now.hour < end:
        return (
            at.astimezone(dt.timezone.utc).replace(tzinfo=None)
            if at
            else dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        )

    if now.hour < start:
        target = now.replace(hour=start, minute=0, second=0, microsecond=0)
    else:
        target = (now + dt.timedelta(days=1)).replace(
            hour=start, minute=0, second=0, microsecond=0
        )
    return target.astimezone(dt.timezone.utc).replace(tzinfo=None)


# ─────────────────────────────────────────────────────────────
# Suppression
# ─────────────────────────────────────────────────────────────
def is_suppressed(db: Session, phone: str | None, email: str | None) -> bool:
    if phone:
        hit = db.scalar(select(SuppressionEntry.id).where(SuppressionEntry.phone == phone))
        if hit:
            return True
    if email:
        hit = db.scalar(select(SuppressionEntry.id).where(SuppressionEntry.email == email))
        if hit:
            return True
    return False


def add_suppression(
    db: Session,
    *,
    phone: str | None = None,
    email: str | None = None,
    reason: str = "opt_out",
    source: str = "",
) -> None:
    db.add(
        SuppressionEntry(
            phone=phone or "", email=email or "", reason=reason, source=source
        )
    )
    db.flush()


def handle_opt_out(db: Session, lead: Lead, *, text: str, source: str = "sms_reply") -> None:
    """Permanently stop all outreach to this lead. No exceptions, no delay."""
    lead.opted_out = True
    lead.opted_out_at = utcnow()
    lead.opt_out_reason = (text or "")[:120]
    lead.status = "dead"
    add_suppression(
        db,
        phone=lead.owner_phone,
        email=lead.owner_email,
        reason="opt_out",
        source=source,
    )
    db.add(
        ConsentLog(
            lead_id=lead.id,
            phone=lead.owner_phone or "",
            action="opt_out",
            channel="sms",
            source=source,
            evidence=text[:500],
        )
    )
    # Cancel any running cadence immediately.
    for enr in lead.enrollments:
        if enr.state == "active":
            enr.state = "stopped"
            enr.stopped_reason = "opted out"
            enr.next_run_at = None


# First-word triggers. A message this short whose first word is an opt-out verb
# is an opt-out. We deliberately do NOT substring-match, because "stop by
# tomorrow at 3" is a real reply from an interested seller and treating it as an
# opt-out would burn a live lead.
_STOP_LEAD_WORDS = {
    "stop", "stopall", "unsubscribe", "cancel", "end", "quit", "optout",
    "opt", "remove", "revoke", "delete", "block", "dont",
}


def classify_reply(text: str) -> str:
    """Return 'stop' | 'start' | 'help' | 'other' for an inbound SMS."""
    raw = (text or "").strip().lower()
    norm = " ".join(raw.replace(",", " ").replace(".", " ").replace("!", " ").split())
    if not norm:
        return "other"

    if norm in STOP_KEYWORDS:
        return "stop"
    if norm in START_KEYWORDS:
        return "start"
    if norm in HELP_KEYWORDS:
        return "help"

    words = norm.split()
    # Short imperative: "STOP", "stop all", "remove me", "unsubscribe now"
    if len(words) <= 2 and words[0] in _STOP_LEAD_WORDS:
        return "stop"
    # Carrier-style suffixed keyword: "stop123", "unsub-abc"
    if len(words) == 1 and words[0][:4] in {"stop", "unst"} and not words[0].startswith("unst"):
        return "stop"

    return "other"


# ─────────────────────────────────────────────────────────────
# Frequency caps
# ─────────────────────────────────────────────────────────────
def sends_today(db: Session, lead_id: int, channel: str) -> int:
    since = utcnow() - dt.timedelta(hours=24)
    return int(
        db.scalar(
            select(func.count(Contact.id)).where(
                Contact.lead_id == lead_id,
                Contact.channel == channel,
                Contact.direction == "outbound",
                Contact.status.in_(["sent", "delivered", "simulated", "queued"]),
                Contact.created_at >= since,
            )
        )
        or 0
    )


def calls_this_week(db: Session, lead_id: int) -> int:
    since = utcnow() - dt.timedelta(days=7)
    return int(
        db.scalar(
            select(func.count(Contact.id)).where(
                Contact.lead_id == lead_id,
                Contact.channel == "call",
                Contact.direction == "outbound",
                Contact.created_at >= since,
            )
        )
        or 0
    )


def check_send_allowed(
    db: Session,
    lead: Lead,
    channel: str,
    *,
    max_sms_per_day: int,
    max_calls_per_week: int,
    enforce_quiet_hours: bool = True,
) -> tuple[bool, str]:
    """Single gate every outbound attempt must pass. Returns (allowed, reason).

    Order matters: the channel policy runs FIRST, so a list lead cannot be
    texted even if it somehow reaches a send call from anywhere else in the
    codebase (manual send, API endpoint, old cadence row).
    """
    from .policy import channel_allowed

    if channel != "note":
        allowed, reason = channel_allowed(lead, channel)
        if not allowed:
            return False, f"policy: {reason}"

    if lead.opted_out:
        return False, "lead opted out"
    if lead.status in ("dead", "closed"):
        return False, f"lead status is {lead.status}"
    if lead.dnc_listed:
        return False, "number is on the DNC registry"
    if is_suppressed(db, lead.owner_phone, lead.owner_email):
        return False, "number/email is on the suppression list"

    if channel in ("call", "sms"):
        if not lead.owner_phone:
            return False, "no phone number on file"
        if enforce_quiet_hours:
            ok, reason = is_within_window(lead)
            if not ok:
                return False, reason

    if channel == "email":
        if not lead.owner_email:
            return False, "no email on file"

    if channel == "mail":
        # Physical mail has no quiet hours and needs no phone. The policy gate
        # above already confirmed an address exists.
        return True, ""

    if channel == "sms":
        if sends_today(db, lead.id, "sms") >= max_sms_per_day:
            return False, f"sms daily cap reached ({max_sms_per_day})"
    if channel == "call":
        if calls_this_week(db, lead.id) >= max_calls_per_week:
            return False, f"call weekly cap reached ({max_calls_per_week})"

    return True, ""


def log_consent(
    db: Session,
    *,
    lead: Lead | None,
    phone: str,
    action: str,
    channel: str,
    source: str,
    evidence: str = "",
    ip: str = "",
) -> None:
    db.add(
        ConsentLog(
            lead_id=lead.id if lead else None,
            phone=phone,
            action=action,
            channel=channel,
            source=source,
            evidence=evidence[:2000],
            ip=ip,
        )
    )
