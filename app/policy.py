"""Channel policy — which outreach channels a given lead may receive.

WHY THIS EXISTS
---------------
Texas SB 140 (effective 1 Sep 2025) folds text messages into "telephone
solicitation" and makes violations automatic Texas DTPA violations: private
right of action, treble damages, attorney's fees. Florida's FTSA adds
$500-$1,500 per text with class-action exposure. Oklahoma and Maryland require
prior express written consent before an automated system may select OR dial.

Cold texting a purchased list is the single highest-risk thing a wholesaler can
do. Cold *calling* and physical *mail* are materially safer.

So the rule is structural, not advisory:

    SMS may only fire for a lead that arrived inbound AND whose consent was
    captured with a verifiable source.

List and probate leads are never texted, no matter what a CSV column claims.
The cadence for those classes does not contain an SMS step at all, and this
module is a second, independent gate inside the sender — defence in depth, so a
future edit to a cadence cannot quietly reintroduce texting.

CHANGING THE POLICY
-------------------
Everything lives in CLASS_CHANNELS and SMS_ALLOWED_CLASSES below. Nothing else
in the codebase hardcodes a channel decision.
"""
from __future__ import annotations

import datetime as dt

# ─────────────────────────────────────────────────────────────
# Lead classes
# ─────────────────────────────────────────────────────────────
INBOUND = "inbound"     # they contacted us: ad, form, call-in, reply
REFERRAL = "referral"   # a named third party sent them to us
LIST = "list"           # purchased / pulled data: absentee, tax delinquent, D4D
PROBATE = "probate"     # inherited / estate leads

LEAD_CLASSES = (INBOUND, REFERRAL, LIST, PROBATE)
DEFAULT_LEAD_CLASS = LIST          # anything unlabelled is treated as cold data

# The channel allow-list per class. THIS is the policy.
CLASS_CHANNELS: dict[str, tuple[str, ...]] = {
    # Inbound: full sequence including SMS (still requires verified consent).
    INBOUND: ("call", "sms", "email"),
    # Referral: warm, but not a consented text relationship.
    REFERRAL: ("call", "email"),
    # Cold data: calls and email. No SMS.
    LIST: ("call", "email"),
    # Probate: written outreach first, then calls. No SMS.
    PROBATE: ("call", "email"),
}

# Physical mail (printed letters) is fully implemented -- queued to a mail
# queue with a CSV export, never auto-sent -- but no lead class uses it by
# default. To enable letters for a class, add "mail" to that class's tuple
# above and swap the email step in the matching cadence for a mail_* template.
SUPPORTS_MAIL = True

# Only these classes may ever be texted.
SMS_ALLOWED_CLASSES = (INBOUND,)

# Channels that need a phone number and obey quiet hours.
PHONE_CHANNELS = ("call", "sms")

# Channels that need a destination address of some kind.
ADDRESSED_CHANNELS = ("mail", "email")


class PolicyError(ValueError):
    """Raised when a channel is requested for a lead that may not receive it."""


def normalize_class(value: str | None) -> str:
    """Coerce arbitrary input onto a known class. Unknown -> cold data."""
    v = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "inbound": INBOUND, "in": INBOUND, "inbound_lead": INBOUND,
        "web": INBOUND, "webform": INBOUND, "web_form": INBOUND,
        "ad": INBOUND, "ppc": INBOUND, "inbound_form": INBOUND,
        "form": INBOUND, "opt_in": INBOUND, "optin": INBOUND,
        "reply": INBOUND, "call_in": INBOUND, "landing_page": INBOUND,
        "referral": REFERRAL, "referred": REFERRAL, "ref": REFERRAL,
        "partner": REFERRAL, "attorney": REFERRAL,
        "list": LIST, "cold": LIST, "purchased": LIST, "absentee": LIST,
        "tax_delinquent": LIST, "d4d": LIST, "driving_for_dollars": LIST,
        "csv": LIST, "batchdata": LIST, "propstream": LIST, "manual": LIST,
        "probate": PROBATE, "inherited": PROBATE, "estate": PROBATE,
        "death": PROBATE, "inherit": PROBATE,
    }
    if v in aliases:
        return aliases[v]
    if v in LEAD_CLASSES:
        return v
    return DEFAULT_LEAD_CLASS


def consent_is_verified(lead) -> bool:
    """SMS consent is only real if we know WHERE and WHEN it came from.

    A bare `consent_sms = True` is worthless in front of a plaintiff's lawyer.
    Under Texas SB 140 you need to produce the form, the source and the
    timestamp. If any of the three is missing, we do not text.
    """
    return bool(
        getattr(lead, "consent_sms", False)
        and (getattr(lead, "consent_source", "") or "").strip()
        and getattr(lead, "consent_at", None)
    )


def sms_eligible(lead) -> tuple[bool, str]:
    """The SMS gate. Returns (allowed, reason_if_not)."""
    cls = normalize_class(getattr(lead, "lead_class", None))
    if cls not in SMS_ALLOWED_CLASSES:
        return False, (
            f"sms disabled for '{cls}' leads "
            f"(permitted classes: {', '.join(SMS_ALLOWED_CLASSES)})"
        )
    if getattr(lead, "opted_out", False):
        return False, "lead opted out"
    if not consent_is_verified(lead):
        missing = []
        if not getattr(lead, "consent_sms", False):
            missing.append("consent flag")
        if not (getattr(lead, "consent_source", "") or "").strip():
            missing.append("consent source")
        if not getattr(lead, "consent_at", None):
            missing.append("consent timestamp")
        return False, f"no verifiable sms consent (missing: {', '.join(missing)})"
    return True, ""


def allowed_channels(lead) -> tuple[str, ...]:
    """Channels this lead may receive, before per-send conditions."""
    cls = normalize_class(getattr(lead, "lead_class", None))
    base = CLASS_CHANNELS.get(cls, CLASS_CHANNELS[DEFAULT_LEAD_CLASS])
    out = []
    for ch in base:
        if ch == "sms":
            ok, _ = sms_eligible(lead)
            if not ok:
                continue
        out.append(ch)
    return tuple(out)


def channel_allowed(lead, channel: str) -> tuple[bool, str]:
    """Authoritative pre-send policy check. Called by the cadence engine."""
    cls = normalize_class(getattr(lead, "lead_class", None))
    base = CLASS_CHANNELS.get(cls, CLASS_CHANNELS[DEFAULT_LEAD_CLASS])

    if channel == "note":
        return True, ""            # internal only, never leaves the system

    if channel not in base:
        return False, (
            f"channel '{channel}' not permitted for '{cls}' leads "
            f"(allowed: {', '.join(base)})"
        )

    if channel == "sms":
        return sms_eligible(lead)

    if channel == "mail":
        if not (getattr(lead, "owner_mailing_address", None) or getattr(lead, "street", None)):
            return False, "no mailing address on file"
        return True, ""

    if channel == "email":
        # No address means no email ever. Treat it as permanent so the sequence
        # skips the step instead of retrying every 45 minutes forever.
        if not (getattr(lead, "owner_email", None) or "").strip():
            return False, "no email address on file"
        return True, ""

    return True, ""


def describe(lead) -> dict:
    """A display-friendly summary of the policy decision for one lead."""
    cls = normalize_class(getattr(lead, "lead_class", None))
    allowed = allowed_channels(lead)
    sms_ok, sms_reason = sms_eligible(lead)
    return {
        "lead_class": cls,
        "allowed_channels": list(allowed),
        "sms_allowed": sms_ok,
        "sms_blocked_reason": "" if sms_ok else sms_reason,
        "consent_verified": consent_is_verified(lead),
        "consent_source": getattr(lead, "consent_source", "") or "",
        "consent_at": (
            lead.consent_at.isoformat() if getattr(lead, "consent_at", None) else None
        ),
    }


def grant_consent(
    lead,
    *,
    channel: str = "sms",
    source: str,
    evidence: str = "",
    at: dt.datetime | None = None,
) -> None:
    """Record express consent on a lead. Source is mandatory and enforced.

    Use this when a seller tells you on a call "yes, text me the offer", or when
    an inbound form captured consent. `source` should be specific enough to be
    defensible: 'phone_call 2025-10-20 14:32', 'web_form:/sell-dallas#sms-checkbox',
    'sms_reply:YES'.
    """
    if not (source or "").strip():
        raise PolicyError("consent requires a source (where did they consent?)")
    lead.consent_sms = True
    lead.consent_source = source.strip()[:60]
    lead.consent_evidence = (evidence or "")[:2000]
    lead.consent_at = at or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def revoke_consent(lead, *, reason: str = "revoked") -> None:
    lead.consent_sms = False
    lead.opt_out_reason = reason[:120]
