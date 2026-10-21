"""HTTP API.

Every route except /health, /api/login and the Telnyx webhooks requires an
authenticated session. That is the single biggest change from v1, where all of
this was world-readable and world-writable.
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import cadence as cadence_engine
from . import compliance, pipeline
from .channels import telnyx
from .config import settings
from .db import get_db
from .models import (
    STATUSES,
    Cadence,
    Comp,
    Contact,
    Contract,
    Enrollment,
    Lead,
    MessageTemplate,
    PipelineRun,
    RejectedLead,
    SuppressionEntry,
    utcnow,
)
from . import intent as intent_mod
from . import notify
from . import policy
from .providers.leads import CSVProvider
from .security import (
    COOKIE_NAME,
    CurrentUser,
    audit,
    authenticate,
    client_ip,
    create_token,
    hash_password,
    verify_password,
)
from .validation import ValidationError, validate_lead

router = APIRouter(prefix="/api")

# Public endpoints (no session). Kept in their own router so it is obvious at a
# glance what is reachable without logging in.
public_router = APIRouter(prefix="/api/public")

CONTRACT_TEMPLATE = """PURCHASE AND SALE AGREEMENT (ASSIGNMENT OF EQUITABLE INTEREST)

THIS AGREEMENT is made on {date} between:

  SELLER: {seller_name}
  PROPERTY: {address}

1. PROPERTY
Seller agrees to sell and Buyer agrees to purchase the real property located
at {address}, together with all improvements, fixtures and appurtenances.

2. PURCHASE PRICE
The total purchase price is {offer} ("Purchase Price"), payable in cash at
closing. Buyer shall pay all customary closing costs. No real estate commission
is owed by either party.

3. CONDITION
Seller conveys the property in its present "AS-IS" condition, with no warranty
of condition, subject to Buyer's inspection right under Section 5.

4. CLOSING
Closing shall occur on or before {closing_date}, or such later date as the
parties agree in writing, at a title company of Buyer's choosing.

5. INSPECTION PERIOD
Buyer shall have {inspection_days} calendar days from the Effective Date to
inspect the property and, at Buyer's sole discretion, terminate this Agreement
for any reason by written notice to Seller.

6. TITLE
Seller shall convey good and marketable title, free of liens except those
satisfied at closing from the Purchase Price.

7. ASSIGNABILITY
Seller acknowledges that Buyer may assign this Agreement or its equitable
interest to a third party, and consents to such assignment. Seller shall
execute reasonable documents required to close with an assignee.

8. DEFAULT AND REMEDIES
If Seller defaults, Buyer may pursue specific performance or return of earnest
money. If Buyer defaults, Seller's sole remedy is retention of earnest money.

9. ENTIRE AGREEMENT
This Agreement is the entire agreement of the parties and may be amended only
in writing signed by both parties.

SELLER: ______________________________   DATE: ____________
        {seller_name}

BUYER:  ______________________________   DATE: ____________
        {buyer_name}, {buyer_company}

--- SIGNATURE CAPTURE ---
Signed by: {signed_by}
Signed at: {signed_at}
Source IP: {signed_ip}
"""


# ═══════════════════════════════════════════════════════════════
# Auth
# ═══════════════════════════════════════════════════════════════
class LoginPayload(BaseModel):
    email: str
    password: str


@router.post("/login")
def login(payload: LoginPayload, request: Request, db: Session = Depends(get_db)):
    user = authenticate(db, payload.email, payload.password, ip=client_ip(request))
    token = create_token(user)
    resp = JSONResponse(
        {"ok": True, "user": {"id": user.id, "email": user.email, "name": user.name}}
    )
    resp.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        max_age=60 * 60 * 12,
        path="/",
    )
    return resp


@router.post("/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@router.get("/me")
def me(user: CurrentUser):
    return {"id": user.id, "email": user.email, "name": user.name}


class PasswordPayload(BaseModel):
    current_password: str
    new_password: str = Field(min_length=10)


@router.post("/me/password")
def change_password(
    payload: PasswordPayload, user: CurrentUser, db: Session = Depends(get_db)
):
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(400, "Current password is incorrect")
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    audit(db, actor=user.email, action="password_changed", target=user.email)
    db.commit()
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════
# Dashboard
# ═══════════════════════════════════════════════════════════════
def _count(db: Session, *conditions) -> int:
    return int(db.scalar(select(func.count(Lead.id)).where(*conditions)) or 0)


def _contacts_since(db: Session, hours: int, channel: str | None = None) -> int:
    since = utcnow() - dt.timedelta(hours=hours)
    conds = [
        Contact.direction == "outbound",
        Contact.created_at >= since,
        Contact.status.in_(["sent", "delivered", "simulated", "queued"]),
    ]
    if channel:
        conds.append(Contact.channel == channel)
    return int(db.scalar(select(func.count(Contact.id)).where(*conds)) or 0)


@router.get("/dashboard")
def dashboard(user: CurrentUser, db: Session = Depends(get_db)):
    total = _count(db)
    funnel = {
        "leads": total,
        "skip_traced": _count(db, Lead.owner_phone.is_not(None)),
        "analyzed": _count(db, Lead.arv > 0),
        "contacted": _count(db, Lead.status.notin_(["new"])),
        "engaged": _count(db, Lead.status.in_(["engaged", "qualified", "appointment",
                                              "offer_made", "under_contract", "signed",
                                              "sent_to_jv", "closed"])),
        "interested": _count(db, Lead.status.in_(["qualified", "appointment", "offer_made",
                                                  "under_contract", "signed", "sent_to_jv",
                                                  "closed"])),
        "under_contract": _count(db, Lead.status.in_(["under_contract", "signed",
                                                      "sent_to_jv", "closed"])),
        "signed": _count(db, Lead.contract_signed.is_(True)),
        "sent_to_jv": _count(db, Lead.sent_to_jv.is_(True)),
        "closed": _count(db, Lead.status == "closed"),
        "dead": _count(db, Lead.status == "dead"),
        "rejected": int(db.scalar(select(func.count(RejectedLead.id))) or 0),
        "workable": _count(
            db,
            Lead.arv > 0,
            Lead.opted_out.is_(False),
            Lead.status.notin_(["dead", "closed"]),
            (Lead.owner_phone.is_not(None)) | (Lead.owner_email.is_not(None)),
        ),
    }

    agents = {
        "calls_today": _contacts_since(db, 24, "call"),
        "sms_today": _contacts_since(db, 24, "sms"),
        "emails_today": _contacts_since(db, 24, "email"),
        "leads_today": _count(db, Lead.created_at >= utcnow() - dt.timedelta(hours=24)),
        "calls_week": _contacts_since(db, 24 * 7, "call"),
        "sms_week": _contacts_since(db, 24 * 7, "sms"),
        "emails_week": _contacts_since(db, 24 * 7, "email"),
        "leads_week": _count(db, Lead.created_at >= utcnow() - dt.timedelta(days=7)),
    }

    grades = {
        g: _count(db, Lead.grade == g, Lead.status.notin_(["dead", "closed"]))
        for g in ("A", "B", "C", "F")
    }

    outbound = int(
        db.scalar(
            select(func.count(Contact.id)).where(Contact.direction == "outbound")
        )
        or 0
    )
    replied = int(
        db.scalar(
            select(func.count(Contact.id)).where(
                Contact.direction == "inbound", Contact.status == "replied"
            )
        )
        or 0
    )
    response_rate = round((replied / outbound) * 100, 1) if outbound else 0.0

    revenue_potential = float(
        db.scalar(
            select(func.sum(Lead.assignment_fee)).where(
                Lead.grade.in_(["A", "B"]), Lead.status.notin_(["dead", "closed"])
            )
        )
        or 0.0
    )
    collected = float(
        db.scalar(select(func.sum(Lead.closed_fee)).where(Lead.status == "closed")) or 0.0
    )

    daily = []
    for i in range(6, -1, -1):
        day_start = (utcnow() - dt.timedelta(days=i)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        day_end = day_start + dt.timedelta(days=1)
        daily.append(
            {
                "day": day_start.strftime("%a"),
                "count": _count(db, Lead.created_at >= day_start, Lead.created_at < day_end),
                "contacts": _contacts_since(db, 24 * (i + 1), None)
                - _contacts_since(db, 24 * i, None),
            }
        )

    active_enrollments = int(
        db.scalar(
            select(func.count(Enrollment.id)).where(Enrollment.state == "active")
        )
        or 0
    )
    due_now = int(
        db.scalar(
            select(func.count(Enrollment.id)).where(
                Enrollment.state == "active", Enrollment.next_run_at <= utcnow()
            )
        )
        or 0
    )

    class_mix = {
        cls: _count(db, Lead.lead_class == cls, Lead.status.notin_(["dead", "closed"]))
        for cls in policy.LEAD_CLASSES
    }
    mail_queued = int(
        db.scalar(
            select(func.count(Contact.id)).where(
                Contact.channel == "mail", Contact.status == "queued_for_mail"
            )
        )
        or 0
    )
    sms_eligible_count = int(
        db.scalar(
            select(func.count(Lead.id)).where(
                Lead.lead_class.in_(policy.SMS_ALLOWED_CLASSES),
                Lead.consent_sms.is_(True),
                Lead.consent_source != "",
                Lead.consent_at.is_not(None),
                Lead.opted_out.is_(False),
                Lead.status.notin_(["dead", "closed"]),
            )
        )
        or 0
    )

    return {
        "pipeline": funnel,
        "agents": agents,
        "class_mix": class_mix,
        "mail_queued": mail_queued,
        "sms_eligible": sms_eligible_count,
        "policy": {
            "class_channels": {k: list(v) for k, v in policy.CLASS_CHANNELS.items()},
            "sms_allowed_classes": list(policy.SMS_ALLOWED_CLASSES),
        },
        "grades": grades,
        "metrics": {
            "response_rate": response_rate,
            "revenue_potential": revenue_potential,
            "total_outbound": outbound,
            "collected": collected,
            "active_enrollments": active_enrollments,
            "due_now": due_now,
        },
        "daily_leads": daily,
        "capabilities": settings.capabilities(),
        "agent_identity": {
            "name": settings.agent_name,
            "company": settings.agent_company,
            "phone": settings.agent_phone,
        },
    }


# ═══════════════════════════════════════════════════════════════
# Leads / deals
# ═══════════════════════════════════════════════════════════════
def _prepare_for_signature(db: Session, lead: Lead) -> Contract | None:
    """Draft the purchase agreement as soon as a seller is ready to talk, so
    all you have to do when you call back is review the number and sign.

    Only drafts when the numbers actually exist — never fabricates a contract
    against an unknown ARV.
    """
    if lead.arv <= 0 or lead.max_offer <= 0:
        return None
    existing = db.scalar(
        select(Contract)
        .where(Contract.lead_id == lead.id, Contract.signed.is_(False))
        .order_by(Contract.id.desc())
    )
    if existing:
        return existing
    price = lead.offered_price or lead.max_offer
    body = CONTRACT_TEMPLATE.format(
        date=utcnow().strftime("%B %d, %Y"),
        seller_name=lead.owner_name or "____________________",
        address=lead.address,
        offer=f"${price:,.2f}",
        closing_date=(utcnow() + dt.timedelta(days=30)).strftime("%B %d, %Y"),
        inspection_days=10,
        buyer_name=settings.agent_name,
        buyer_company=settings.agent_company,
        signed_by="(not yet signed)",
        signed_at="(not yet signed)",
        signed_ip="",
    )
    contract = Contract(lead_id=lead.id, body=body, offer_price=price)
    db.add(contract)
    db.flush()
    return contract


def _lead_summary(lead: Lead) -> dict:
    return {
        "id": lead.id,
        "address": lead.address,
        "street": lead.street,
        "city": lead.city,
        "state": lead.state,
        "zip_code": lead.zip_code,
        "owner": lead.owner_name,
        "owner_first_name": lead.owner_first_name,
        "phone": lead.owner_phone,
        "email": lead.owner_email,
        "language": lead.owner_language,
        "grade": lead.grade,
        "score": lead.score,
        "motivation_score": lead.motivation_score,
        "arv": lead.arv,
        "comp_count": lead.comp_count,
        "max_offer": lead.max_offer,
        "repair_estimate": lead.repair_estimate,
        "assignment_fee": lead.assignment_fee,
        "projected_spread": lead.projected_spread,
        "status": lead.status,
        "contract_signed": lead.contract_signed,
        "sent_to_jv": lead.sent_to_jv,
        "opted_out": lead.opted_out,
        "dnc_listed": lead.dnc_listed,
        "is_contactable": lead.is_contactable,
        "can_be_worked": lead.can_be_worked,
        "lead_class": lead.lead_class,
        "needs_attention": lead.needs_attention,
        "attention_reason": lead.attention_reason,
        "attention_at": lead.attention_at.isoformat() if lead.attention_at else None,
        "interested_at": lead.interested_at.isoformat() if lead.interested_at else None,
        "intent_label": lead.intent_label,
        "intent_score": lead.intent_score,
        "allowed_channels": list(policy.allowed_channels(lead)),
        "sms_allowed": policy.sms_eligible(lead)[0],
        "sms_blocked_reason": policy.sms_eligible(lead)[1],
        "consent_verified": policy.consent_is_verified(lead),
        "consent_source": lead.consent_source or "",
        "mail_queued": sum(1 for c in lead.contacts if c.channel == "mail"),
        "source": lead.source,
        "sqft": lead.sqft,
        "created_at": lead.created_at.isoformat() if lead.created_at else None,
        "last_contact_at": lead.contacts[-1].created_at.isoformat()
        if lead.contacts
        else None,
        "contact_count": len(lead.contacts),
    }


@router.get("/deals")
def list_deals(
    user: CurrentUser,
    db: Session = Depends(get_db),
    limit: int = Query(100, ge=1, le=500),
    grade: str | None = None,
    status: str | None = None,
):
    stmt = select(Lead).order_by(Lead.score.desc(), Lead.id.desc()).limit(limit)
    if grade:
        stmt = stmt.where(Lead.grade == grade.upper())
    if status:
        stmt = stmt.where(Lead.status == status)
    return [_lead_summary(l) for l in db.scalars(stmt).all()]


@router.get("/deals/{deal_id}")
def deal_detail(deal_id: int, user: CurrentUser, db: Session = Depends(get_db)):
    lead = db.get(Lead, deal_id)
    if not lead:
        raise HTTPException(404, "Deal not found")
    comps = db.scalars(select(Comp).where(Comp.lead_id == lead.id)).all()
    contract = db.scalar(
        select(Contract).where(Contract.lead_id == lead.id).order_by(Contract.id.desc())
    )
    return {
        "deal": _lead_summary(lead),
        "channel_policy": policy.describe(lead),
        "motivation_reasons": lead.motivation_reasons or [],
        "notes": lead.notes,
        "property": {
            "street": lead.street,
            "city": lead.city,
            "county": lead.county,
            "state": lead.state,
            "zip_code": lead.zip_code,
            "type": lead.property_type,
            "bedrooms": lead.bedrooms,
            "bathrooms": lead.bathrooms,
            "sqft": lead.sqft,
            "year_built": lead.year_built,
            "tax_assessed_value": lead.tax_assessed_value,
            "tax_delinquent": lead.tax_delinquent,
            "pre_foreclosure": lead.pre_foreclosure,
            "vacant": lead.vacant,
            "is_absentee": lead.is_absentee,
            "years_owned": lead.years_owned,
            "equity_pct": lead.equity_pct,
        },
        "owner": {
            "name": lead.owner_name,
            "phone": lead.owner_phone,
            "phone_2": lead.owner_phone_2,
            "email": lead.owner_email,
            "language": lead.owner_language,
            "mailing_address": lead.owner_mailing_address,
        },
        "comps": [
            {
                "address": c.address,
                "sold_price": c.sold_price,
                "sold_date": c.sold_date.isoformat() if c.sold_date else None,
                "sqft": c.sqft,
                "price_per_sqft": c.price_per_sqft,
                "distance_miles": c.distance_miles,
            }
            for c in comps
        ],
        "contacts": [
            {
                "id": c.id,
                "channel": c.channel,
                "direction": c.direction,
                "status": c.status,
                "body": c.body,
                "subject": c.subject,
                "error": c.error,
                "created_at": c.created_at.isoformat(),
            }
            for c in lead.contacts
        ],
        "contract": (
            {
                "id": contract.id,
                "offer_price": contract.offer_price,
                "signed": contract.signed,
                "signed_at": contract.signed_at.isoformat() if contract.signed_at else None,
                "signed_by": contract.signed_by,
                "body": contract.body,
            }
            if contract
            else None
        ),
    }


class LeadCreate(BaseModel):
    street: str
    city: str
    state: str
    zip_code: str = ""
    owner_name: str | None = None
    owner_phone: str | None = None
    owner_email: str | None = None
    owner_mailing_address: str | None = None
    sqft: int | None = None
    year_built: int | None = None
    tax_assessed_value: float | None = None
    property_type: str = "single_family"
    # Defaults to cold list data: the safe assumption. Nothing is texted
    # unless the class is explicitly 'inbound' AND consent provenance is given.
    lead_class: str = "list"
    consent_sms: bool = False
    consent_source: str = ""
    consent_at: str | None = None
    consent_evidence: str = ""


@router.post("/leads")
def create_lead(payload: LeadCreate, user: CurrentUser, db: Session = Depends(get_db)):
    try:
        clean = validate_lead(payload.model_dump())
    except ValidationError as e:
        raise HTTPException(422, f"Rejected: {e}")
    if db.scalar(select(Lead).where(Lead.address_key == clean["address_key"])):
        raise HTTPException(409, "That address is already in the pipeline")
    clean["source"] = "manual"
    lead = Lead(**clean)
    db.add(lead)
    db.flush()
    pipeline.analyze_lead(db, lead)
    db.commit()
    audit(db, actor=user.email, action="lead_created", target=lead.address)
    db.commit()
    return _lead_summary(lead)


@router.post("/leads/import")
async def import_leads(
    user: CurrentUser,
    db: Session = Depends(get_db),
    file: UploadFile = File(...),
    enroll: bool = True,
    lead_class: str = Query(
        "list",
        description="Class applied to every row unless the file has its own "
        "lead_class column. list (default) | probate | referral | inbound",
    ),
    consent_policy: str = Query(
        "none",
        description="'none' (default) - discard all consent data in the file, "
        "which is correct for any purchased list. 'column' - honour the file's "
        "consent columns, but only for rows whose class is inbound.",
    ),
):
    """Upload a CSV export (PropStream, BatchData, DealMachine, ListSource,
    county probate export, your own inbound form export).

    CONSENT HANDLING (Texas SB 140):
    By default this import grants NO SMS consent. A purchased or pulled list
    cannot carry consent for you — consent has to come from the consumer to
    *you*. Rows that claim consent are ignored unless you explicitly pass
    consent_policy=column AND the row is classed inbound, in which case the
    source and timestamp are required and recorded.
    """
    raw = await file.read()
    if len(raw) > 25 * 1024 * 1024:
        raise HTTPException(413, "File too large (25MB max)")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    cls = policy.normalize_class(lead_class)
    if consent_policy not in ("none", "column"):
        raise HTTPException(422, "consent_policy must be 'none' or 'column'")

    provider = CSVProvider()
    rows = provider.parse(text, source_name=file.filename or "csv_upload")
    if not rows:
        raise HTTPException(422, "No rows found — is this a CSV with a header row?")

    # Apply the batch class to any row that did not declare its own.
    stripped_consent = 0
    for row in rows:
        if not (row.get("lead_class") or "").strip():
            row["lead_class"] = cls
        claimed = str(row.get("consent_sms", "")).strip().lower()
        if consent_policy == "none" and claimed not in ("", "0", "false", "no", "n", "off"):
            # Deliberately discard. A list file claiming consent is not
            # consent to us and is exactly what gets wholesalers sued.
            stripped_consent += 1
        if consent_policy == "none":
            for k in ("consent_sms", "consent_call", "consent_source",
                      "consent_date", "consent_evidence"):
                row.pop(k, None)

    stats = pipeline.ingest_records(db, rows, source=file.filename or "csv_upload")
    db.commit()
    analyzed = pipeline.analyze_all(db)
    enrolled = pipeline.enroll_workable(db) if enroll else 0
    db.commit()

    by_class = {}
    for lead in db.scalars(
        select(Lead).where(Lead.source == (file.filename or "csv_upload"))
    ).all():
        by_class[lead.lead_class] = by_class.get(lead.lead_class, 0) + 1

    result = {
        "ok": True if stats.accepted else False,
        "stats": stats.to_dict(),
        "analyzed": analyzed,
        "enrolled": enrolled,
        "lead_class": cls,
        "consent_policy": consent_policy,
        "consent_flags_stripped": stripped_consent,
        "by_class": by_class,
        "note": (
            "No SMS consent was granted by this import. Those leads will be "
            "worked by call and mail only."
            if consent_policy == "none"
            else "Consent honoured only for rows classed inbound with a source."
        ),
    }
    audit(
        db,
        actor=user.email,
        action="leads_imported",
        target=file.filename or "csv",
        meta={**stats.to_dict(), "lead_class": cls, "consent_policy": consent_policy},
    )
    db.commit()
    return result


@router.get("/leads/rejected")
def rejected_leads(
    user: CurrentUser, db: Session = Depends(get_db), limit: int = 100
):
    rows = db.scalars(
        select(RejectedLead).order_by(RejectedLead.id.desc()).limit(limit)
    ).all()
    return [
        {
            "id": r.id,
            "source": r.source,
            "reason": r.reason,
            "raw": r.raw,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


class StatusUpdate(BaseModel):
    status: str
    note: str = ""


@router.patch("/deals/{deal_id}")
def update_deal(
    deal_id: int, payload: StatusUpdate, user: CurrentUser, db: Session = Depends(get_db)
):
    lead = db.get(Lead, deal_id)
    if not lead:
        raise HTTPException(404, "Deal not found")
    if payload.status not in STATUSES:
        raise HTTPException(
            422, f"Invalid status. Allowed: {', '.join(STATUSES)}"
        )
    # Evidence guard: you cannot advance a deal you know nothing about.
    if payload.status in ("under_contract", "signed", "sent_to_jv"):
        if lead.arv <= 0:
            raise HTTPException(
                409,
                "Cannot advance: ARV is unknown. Add comps or an assessed value first.",
            )
        if not lead.is_contactable:
            raise HTTPException(
                409, "Cannot advance: no owner contact information on file."
            )
    if payload.status == "signed" and not lead.contract_signed:
        raise HTTPException(
            409, "Cannot mark signed: no signed agreement on file. Generate and sign the contract first."
        )

    old = lead.status
    lead.status = payload.status
    if payload.status == "closed":
        lead.closed_at = utcnow()
    if payload.note:
        db.add(
            Contact(
                lead_id=lead.id,
                channel="note",
                direction="internal",
                body=payload.note,
                status="logged",
            )
        )
    # Stop automation when a human takes over.
    if payload.status not in ("new", "contacted"):
        for enr in lead.enrollments:
            if enr.state == "active":
                enr.state = "paused"
                enr.stopped_reason = f"manual status -> {payload.status}"
                enr.next_run_at = None
    db.commit()
    audit(
        db,
        actor=user.email,
        action="status_changed",
        target=f"lead:{deal_id}",
        meta={"from": old, "to": payload.status},
    )
    db.commit()
    return _lead_summary(lead)


@router.post("/deals/{deal_id}/approve")
def approve_deal(deal_id: int, user: CurrentUser, db: Session = Depends(get_db)):
    """Approve an offer — moves a qualified lead to offer_made."""
    lead = db.get(Lead, deal_id)
    if not lead:
        raise HTTPException(404, "Deal not found")
    if lead.arv <= 0 or lead.max_offer <= 0:
        raise HTTPException(
            409, "Cannot approve an offer: the numbers do not exist yet (ARV/max offer = 0)."
        )
    lead.status = "offer_made"
    lead.offered_price = lead.max_offer
    for enr in lead.enrollments:
        if enr.state == "active":
            enr.state = "paused"
            enr.stopped_reason = "offer made"
            enr.next_run_at = None
    db.commit()
    audit(db, actor=user.email, action="offer_approved", target=f"lead:{deal_id}")
    db.commit()
    return _lead_summary(lead)


@router.post("/deals/{deal_id}/contact")
def manual_contact(
    deal_id: int,
    user: CurrentUser,
    db: Session = Depends(get_db),
    channel: str = Query(..., pattern="^(call|sms|email)$"),
    template_key: str | None = None,
    body: str | None = None,
):
    """Send a one-off message outside the cadence. Still passes compliance."""
    lead = db.get(Lead, deal_id)
    if not lead:
        raise HTTPException(404, "Deal not found")

    text = body or ""
    subject = ""
    if template_key:
        tpl = db.scalar(select(MessageTemplate).where(MessageTemplate.key == template_key))
        if not tpl:
            raise HTTPException(404, f"Unknown template '{template_key}'")
        text = text or tpl.body
        subject = tpl.subject
    if not text:
        raise HTTPException(422, "Provide a body or a template_key")

    allowed, reason = compliance.check_send_allowed(
        db,
        lead,
        channel,
        max_sms_per_day=settings.max_sms_per_lead_per_day,
        max_calls_per_week=settings.max_call_attempts_per_lead_per_week,
        enforce_quiet_hours=settings.enforce_quiet_hours,
    )
    if not allowed:
        raise HTTPException(409, f"Blocked by compliance: {reason}")

    rendered = cadence_engine.render(text, lead)
    rsubject = cadence_engine.render(subject, lead)

    if channel == "sms":
        res = telnyx.send_sms(lead.owner_phone or "", rendered)
    elif channel == "email":
        from .channels import email as email_channel

        if not lead.owner_email:
            raise HTTPException(422, "No email on file for this lead")
        res = email_channel.send_email(lead.owner_email, rsubject, rendered)
    else:
        res = telnyx.start_ai_call(lead.owner_phone or "", rendered, lead_id=lead.id)

    contact = Contact(
        lead_id=lead.id,
        channel=channel,
        direction="outbound",
        body=rendered,
        subject=rsubject,
        status=res.status,
        provider_id=res.provider_id,
        error=res.error,
        meta={"simulated": res.simulated, "manual": True, "by": user.email},
    )
    db.add(contact)
    if res.ok and lead.status == "new":
        lead.status = "contacted"
    db.commit()
    return {
        "ok": res.ok,
        "status": res.status,
        "simulated": res.simulated,
        "error": res.error,
        "contact_id": contact.id,
    }


@router.post("/deals/{deal_id}/generate-contract")
def generate_contract(
    deal_id: int,
    user: CurrentUser,
    db: Session = Depends(get_db),
    offer_price: float | None = None,
    inspection_days: int = 10,
):
    lead = db.get(Lead, deal_id)
    if not lead:
        raise HTTPException(404, "Deal not found")
    if lead.arv <= 0:
        raise HTTPException(409, "Cannot generate a contract with an unknown ARV.")
    price = offer_price or lead.max_offer or lead.offered_price
    if not price or price <= 0:
        raise HTTPException(409, "No offer price available — run the analysis first.")

    body = CONTRACT_TEMPLATE.format(
        date=utcnow().strftime("%B %d, %Y"),
        seller_name=lead.owner_name or "____________________",
        address=lead.address,
        offer=f"${price:,.2f}",
        closing_date=(utcnow() + dt.timedelta(days=30)).strftime("%B %d, %Y"),
        inspection_days=inspection_days,
        buyer_name=settings.agent_name,
        buyer_company=settings.agent_company,
        signed_by="(not yet signed)",
        signed_at="(not yet signed)",
        signed_ip="",
    )
    contract = Contract(lead_id=lead.id, body=body, offer_price=price)
    db.add(contract)
    db.commit()
    audit(db, actor=user.email, action="contract_generated", target=f"lead:{deal_id}")
    db.commit()
    return {"id": contract.id, "body": body, "offer_price": price}


class SignPayload(BaseModel):
    signed_by: str = Field(min_length=2)
    signature_data: str = ""


@router.post("/deals/{deal_id}/sign")
def sign_contract(
    deal_id: int,
    payload: SignPayload,
    request: Request,
    user: CurrentUser,
    db: Session = Depends(get_db),
):
    lead = db.get(Lead, deal_id)
    if not lead:
        raise HTTPException(404, "Deal not found")
    contract = db.scalar(
        select(Contract).where(Contract.lead_id == lead.id).order_by(Contract.id.desc())
    )
    if not contract:
        raise HTTPException(409, "Generate the contract before signing it.")

    ip = client_ip(request)
    stamp = utcnow().strftime("%B %d, %Y at %H:%M UTC")
    contract.signed = True
    contract.signed_at = utcnow()
    contract.signed_by = payload.signed_by
    contract.signature_data = payload.signature_data
    contract.signature_ip = ip
    contract.body = contract.body.replace(
        "Signed by: (not yet signed)", f"Signed by: {payload.signed_by}"
    ).replace("Signed at: (not yet signed)", f"Signed at: {stamp}").replace(
        "Source IP: ", f"Source IP: {ip}"
    )
    lead.contract_signed = True
    lead.status = "signed"
    lead.accepted_price = contract.offer_price
    db.commit()
    audit(
        db,
        actor=user.email,
        action="contract_signed",
        target=f"lead:{deal_id}",
        ip=ip,
        meta={"signed_by": payload.signed_by},
    )
    db.commit()
    return {"ok": True, "signed_at": contract.signed_at.isoformat()}


@router.post("/deals/{deal_id}/send-to-jv")
def send_to_jv(deal_id: int, user: CurrentUser, db: Session = Depends(get_db)):
    lead = db.get(Lead, deal_id)
    if not lead:
        raise HTTPException(404, "Deal not found")
    if not lead.contract_signed:
        raise HTTPException(
            409, "Only signed deals can be marketed to buyers. Sign the contract first."
        )
    lead.sent_to_jv = True
    lead.status = "sent_to_jv"
    db.commit()
    audit(db, actor=user.email, action="sent_to_jv", target=f"lead:{deal_id}")
    db.commit()
    return {"ok": True, "deal": _lead_summary(lead)}


# ═══════════════════════════════════════════════════════════════
# Public seller intake — the front door for your website
# ═══════════════════════════════════════════════════════════════
class PublicLeadIn(BaseModel):
    """Fields a seller fills in on your landing page.

    `sms_consent` is the checkbox. It is the ONLY legitimate source of SMS
    permission in this system, because it is the only case where the consumer
    is giving permission directly to you.
    """

    name: str = Field(min_length=1, max_length=200)
    phone: str = Field(min_length=7, max_length=32)
    email: str = ""
    street: str = Field(min_length=3, max_length=255)
    city: str = Field(min_length=2, max_length=120)
    state: str = Field(min_length=2, max_length=2)
    zip_code: str = ""
    situation: str = ""            # "inherited", "behind on taxes", "tired landlord" ...
    timeline: str = ""             # "asap", "1-3 months", "just curious"
    asking_price: str = ""
    sms_consent: bool = False
    consent_text: str = ""         # the exact wording they agreed to
    page_url: str = ""             # which page the form was on
    # Simple bot trap: real people never fill this in.
    company_website: str = ""


# Per-IP rate limit for the public form. In-process and therefore per-replica;
# good enough to stop casual form spam, not a substitute for a WAF.
_PUBLIC_HITS: dict[str, list[float]] = {}
PUBLIC_RATE_LIMIT = 10          # submissions
PUBLIC_RATE_WINDOW = 3600       # per hour


def _public_rate_ok(ip: str) -> bool:
    import time as _t

    now = _t.time()
    hits = [t for t in _PUBLIC_HITS.get(ip, []) if now - t < PUBLIC_RATE_WINDOW]
    if len(hits) >= PUBLIC_RATE_LIMIT:
        _PUBLIC_HITS[ip] = hits
        return False
    hits.append(now)
    _PUBLIC_HITS[ip] = hits
    return True


@public_router.post("/lead")
def public_lead(payload: PublicLeadIn, request: Request, db: Session = Depends(get_db)):
    """Accept a seller submission from your website.

    This is the highest-value endpoint in the system: an inbound seller with
    captured consent is the only kind of lead that can legally be texted, and it
    converts several times better than any purchased list.
    """
    ip = client_ip(request)

    if not _public_rate_ok(ip):
        raise HTTPException(429, "Too many submissions from this address. Try again later.")

    # Honeypot: filled in means a bot. Accept silently so it does not retry.
    if (payload.company_website or "").strip():
        return {"ok": True, "id": None}

    consent_text = (payload.consent_text or "").strip()
    raw = {
        "street": payload.street,
        "city": payload.city,
        "state": payload.state,
        "zip_code": payload.zip_code,
        "owner_name": payload.name,
        "owner_phone": payload.phone,
        "owner_email": payload.email,
        "asking_price": payload.asking_price or None,
        "property_type": "single_family",
        "lead_class": "inbound",
        "source": "website_form",
        "source_ref": (payload.page_url or "/sell")[:255],
        "consent_sms": payload.sms_consent,
        "consent_source": f"web_form:{payload.page_url or '/sell'}" if payload.sms_consent else "",
        "consent_evidence": consent_text if payload.sms_consent else "",
        "consent_at": utcnow().isoformat() if payload.sms_consent else None,
    }

    try:
        clean = validate_lead(raw)
    except ValidationError:
        # Never echo internal validation detail to a public form.
        raise HTTPException(422, "Please check the property address and try again.")

    if payload.asking_price:
        try:
            clean["asking_price"] = float(
                str(payload.asking_price).replace("$", "").replace(",", "")
            ) or None
        except (TypeError, ValueError):
            clean["asking_price"] = None

    existing = db.scalar(select(Lead).where(Lead.address_key == clean["address_key"]))
    if existing:
        # Same house, new enquiry: enrich rather than duplicate, and honour any
        # fresh consent they just gave.
        for field in ("owner_phone", "owner_email", "owner_name", "asking_price"):
            if clean.get(field):
                setattr(existing, field, clean[field])
        if clean.get("consent_sms"):
            existing.lead_class = "inbound"
            existing.consent_sms = True
            existing.consent_source = clean["consent_source"]
            existing.consent_evidence = clean["consent_evidence"]
            existing.consent_at = utcnow()
            existing.opted_out = False
        existing.updated_at = utcnow()
        lead = existing
    else:
        lead = Lead(**clean)
        db.add(lead)
        db.flush()

    # Situation/timeline are free text from the seller — keep them as a note so
    # nothing is lost, and let them inform the motivation score.
    note = " | ".join(
        x for x in [payload.situation.strip(), payload.timeline.strip()] if x
    )
    if note:
        db.add(
            Contact(
                lead_id=lead.id, channel="note", direction="inbound",
                body=f"Web form: {note}"[:2000], status="logged",
            )
        )
        low = note.lower()
        if any(w in low for w in ("inherited", "estate", "probate", "passed away")):
            lead.probate_or_inherited = True
        if any(w in low for w in ("taxes", "delinquent", "behind on tax")):
            lead.tax_delinquent = True
        if any(w in low for w in ("tenant", "rental", "landlord", "renting")):
            lead.tired_landlord = True
        if any(w in low for w in ("vacant", "empty", "no one living")):
            lead.vacant = True
        if any(w in low for w in ("foreclosure", "auction", "notice of default")):
            lead.pre_foreclosure = True

    lead.lead_class = "inbound"
    pipeline.analyze_lead(db, lead)
    db.commit()

    # An inbound seller is by definition ready to talk. This is the one lead
    # type that should interrupt you immediately.
    notify.flag_for_human(
        db,
        lead,
        reason=f"New website enquiry ({payload.timeline or 'no timeline given'})",
    )
    _prepare_for_signature(db, lead)
    notify.notify_owner(
        db,
        lead,
        kind="inbound_form",
        title=f"New seller enquiry: {lead.address}",
        body=(
            f"{payload.name} just submitted your website form.\n\n"
            f"{lead.address}\n"
            f"Phone: {lead.owner_phone or '—'}\n"
            f"Email: {lead.owner_email or '—'}\n"
            f"Situation: {payload.situation or '—'}\n"
            f"Timeline: {payload.timeline or '—'}\n"
            f"Asking: {payload.asking_price or '—'}\n"
            f"SMS consent: {'YES — you may text' if lead.consent_sms else 'no'}\n\n"
            f"Numbers: max offer ${lead.max_offer:,.0f} / ARV ${lead.arv:,.0f}\n"
            f"Grade: {lead.grade}\n\n"
            f"Respond within 5 minutes. Speed to lead decides this one."
        ),
        sms_body=(
            f"📥 New web lead: {lead.owner_name} — {lead.street}\n"
            f"{payload.timeline or ''} {payload.situation or ''}".strip()
            + f"\nCall now: {lead.owner_phone}"
            + ("\n(Consented to text)" if lead.consent_sms else "")
        ),
    )
    db.commit()

    audit(
        db,
        actor="public",
        action="public_lead",
        target=f"lead:{lead.id}",
        ip=ip,
        meta={"consent": lead.consent_sms, "class": lead.lead_class},
    )
    db.commit()

    return {
        "ok": True,
        "id": lead.id,
        # Deliberately no scores or internal state in a public response.
        "message": "Thanks — we'll be in touch shortly.",
    }


# ═══════════════════════════════════════════════════════════════
# Channel health check — prove texting actually delivers
# ═══════════════════════════════════════════════════════════════
class TestSmsIn(BaseModel):
    to: str = Field(
        min_length=10,
        description="Your own mobile number. Never a prospect.",
    )


@router.post("/test-sms")
def test_sms(payload: TestSmsIn, user: CurrentUser, db: Session = Depends(get_db)):
    """Send one real SMS from your 10DLC number to a number you control.

    This is the only way to know your brand/campaign registration actually
    delivers: a dry-run "simulated" result tells you nothing about carrier
    filtering. Use it before every ramp in volume.
    """
    from .validation import normalize_phone

    to = normalize_phone(payload.to)
    if not to:
        raise HTTPException(422, "That does not look like a valid US mobile number")
    if to == normalize_phone(settings.agent_phone):
        pass  # sending to yourself is the intended use

    body = (
        f"Wholesale AI test message. If you got this, your 10DLC number is "
        f"delivering. Reply STOP to confirm opt-out handling works."
    )
    res = telnyx.send_sms(to, body)

    audit(
        db,
        actor=user.email,
        action="test_sms",
        target=to,
        meta={"status": res.status, "simulated": res.simulated, "error": res.error},
    )
    db.commit()

    return {
        "ok": res.ok,
        "status": res.status,
        "simulated": res.simulated,
        "provider_id": res.provider_id,
        "error": res.error,
        "next": (
            "Dry run — add TELNYX_API_KEY and TELNYX_FROM_NUMBER on Railway, "
            "then try again."
            if res.simulated
            else "Check the handset. Now reply STOP and confirm the lead/suppression "
                 "behaviour if this was sent to a test lead number."
        ),
    }


# ═══════════════════════════════════════════════════════════════
# Needs you — the only queue that requires a human
# ═══════════════════════════════════════════════════════════════
@router.get("/needs-you")
def needs_you(user: CurrentUser, db: Session = Depends(get_db), limit: int = 100):
    """Leads where a seller has actually said something that means "talk to me".

    Nothing else in this system asks for your time. This is the list you work,
    and each entry arrives with the numbers and a drafted contract so the only
    action left is to call and sign.
    """
    rows = db.scalars(
        select(Lead)
        .where(
            Lead.needs_attention.is_(True),
            Lead.opted_out.is_(False),
            Lead.status.notin_(["closed", "dead"]),
        )
        .order_by(Lead.intent_score.desc(), Lead.attention_at.desc())
        .limit(limit)
    ).all()

    out = []
    for lead in rows:
        draft = db.scalar(
            select(Contract)
            .where(Contract.lead_id == lead.id, Contract.signed.is_(False))
            .order_by(Contract.id.desc())
        )
        last_inbound = next(
            (c for c in reversed(lead.contacts) if c.direction == "inbound"), None
        )
        out.append(
            {
                **_lead_summary(lead),
                "what_they_said": last_inbound.body if last_inbound else "",
                "replied_at": (
                    last_inbound.created_at.isoformat() if last_inbound else None
                ),
                "contract_ready": draft is not None,
                "draft_offer": draft.offer_price if draft else None,
                "next_action": (
                    "Sign the drafted agreement"
                    if draft
                    else "Get the numbers before calling — ARV is unknown"
                    if lead.arv <= 0
                    else "Call the seller and confirm the number"
                ),
            }
        )
    return out


class AttentionAction(BaseModel):
    note: str = ""


@router.post("/deals/{deal_id}/attention/resolve")
def resolve_attention(
    deal_id: int,
    user: CurrentUser,
    db: Session = Depends(get_db),
    payload: AttentionAction | None = None,
):
    """Mark a handoff as handled — you called, you signed, or it went cold."""
    lead = db.get(Lead, deal_id)
    if not lead:
        raise HTTPException(404, "Deal not found")
    note = (payload.note if payload else "") or ""
    if note:
        db.add(
            Contact(
                lead_id=lead.id, channel="note", direction="internal",
                body=note, status="logged",
            )
        )
    notify.resolve_attention(db, lead, note)
    db.commit()
    return {"ok": True, "deal": _lead_summary(lead)}


@router.get("/alerts")
def list_alerts(
    user: CurrentUser,
    db: Session = Depends(get_db),
    limit: int = 100,
    unread_only: bool = False,
):
    from .models import Alert

    stmt = select(Alert).where(Alert.channel == "in_app").order_by(Alert.id.desc())
    if unread_only:
        stmt = stmt.where(Alert.read_at.is_(None))
    rows = db.scalars(stmt.limit(limit)).all()
    return [
        {
            "id": a.id,
            "lead_id": a.lead_id,
            "kind": a.kind,
            "title": a.title,
            "body": a.body,
            "read": a.read_at is not None,
            "created_at": a.created_at.isoformat(),
        }
        for a in rows
    ]


@router.post("/alerts/{alert_id}/read")
def mark_alert_read(alert_id: int, user: CurrentUser, db: Session = Depends(get_db)):
    from .models import Alert

    a = db.get(Alert, alert_id)
    if not a:
        raise HTTPException(404, "Alert not found")
    a.read_at = utcnow()
    db.commit()
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════
# Consent capture (Texas SB 140 defence)
# ═══════════════════════════════════════════════════════════════
class ConsentPayload(BaseModel):
    source: str = Field(
        min_length=3,
        description="Where consent came from. Be specific: "
        "'phone_call 2025-10-20 14:32', 'web_form:/sell-dallas#sms', "
        "'sms_reply:YES'",
    )
    evidence: str = Field(
        default="",
        description="Proof: form URL, recording reference, screenshot id, "
        "message text the seller sent you.",
    )
    lead_class: str | None = Field(
        default="inbound",
        description="Recording consent implies the lead is inbound. Set to "
        "'referral' to keep SMS off while noting the call consent.",
    )


@router.post("/deals/{deal_id}/consent")
def record_consent(
    deal_id: int,
    payload: ConsentPayload,
    request: Request,
    user: CurrentUser,
    db: Session = Depends(get_db),
):
    """Record express consent captured outside the app — typically a seller
    saying "yes, text me the number" on a call, or a form submission you are
    entering by hand.

    A source is mandatory. Consent without provenance is not consent under
    Texas SB 140, and this endpoint refuses to pretend otherwise.
    """
    lead = db.get(Lead, deal_id)
    if not lead:
        raise HTTPException(404, "Deal not found")
    if lead.opted_out:
        raise HTTPException(
            409,
            "This lead has opted out. Consent cannot be re-granted by you — "
            "only the consumer can restart contact.",
        )

    try:
        policy.grant_consent(
            lead, channel="sms", source=payload.source, evidence=payload.evidence
        )
    except policy.PolicyError as e:
        raise HTTPException(422, str(e))

    # Consent was given through a human interaction, which makes this an
    # inbound relationship rather than cold data.
    if payload.lead_class:
        lead.lead_class = policy.normalize_class(payload.lead_class)
        if lead.lead_class == "inbound" and lead.status == "new":
            lead.status = "engaged"
        # An inbound consented lead should run the inbound cadence. Re-point any
        # active enrollment that is on a non-SMS cadence.
        target = cadence_engine.cadence_for_lead(db, lead)
        for enr in lead.enrollments:
            if enr.state == "active" and target and enr.cadence_id != target.id:
                enr.cadence_id = target.id
                enr.step_index = 0

    compliance.log_consent(
        db,
        lead=lead,
        phone=lead.owner_phone or "",
        action="granted",
        channel="sms",
        source=payload.source,
        evidence=payload.evidence,
        ip=client_ip(request),
    )
    db.commit()
    audit(
        db,
        actor=user.email,
        action="consent_recorded",
        target=f"lead:{deal_id}",
        ip=client_ip(request),
        meta={"source": payload.source, "class": lead.lead_class},
    )
    db.commit()
    return {
        "ok": True,
        "deal": _lead_summary(lead),
        "policy": policy.describe(lead),
    }


@router.delete("/deals/{deal_id}/consent")
def revoke_consent(
    deal_id: int, request: Request, user: CurrentUser, db: Session = Depends(get_db)
):
    """Withdraw SMS consent without fully opting the lead out."""
    lead = db.get(Lead, deal_id)
    if not lead:
        raise HTTPException(404, "Deal not found")
    policy.revoke_consent(lead, reason="consent revoked by operator")
    compliance.log_consent(
        db,
        lead=lead,
        phone=lead.owner_phone or "",
        action="revoked",
        channel="sms",
        source=f"operator:{user.email}",
        ip=client_ip(request),
    )
    db.commit()
    return {"ok": True, "policy": policy.describe(lead)}


# ═══════════════════════════════════════════════════════════════
# Mail queue — physical letters awaiting dispatch
# ═══════════════════════════════════════════════════════════════
@router.get("/mail-queue")
def mail_queue(
    user: CurrentUser,
    db: Session = Depends(get_db),
    limit: int = Query(200, ge=1, le=1000),
    include_dispatched: bool = False,
):
    """Letters queued for printing. Nothing here has been mailed
    automatically — physical mail is dispatched by you or a mail house."""
    stmt = select(Contact).where(Contact.channel == "mail")
    if not include_dispatched:
        stmt = stmt.where(Contact.status == "queued_for_mail")
    rows = db.scalars(stmt.order_by(Contact.id.desc()).limit(limit)).all()
    out = []
    for c in rows:
        lead = db.get(Lead, c.lead_id)
        out.append(
            {
                "id": c.id,
                "lead_id": c.lead_id,
                "address": lead.address if lead else "",
                "lead_class": lead.lead_class if lead else "",
                "to_name": (c.meta or {}).get("to_name", ""),
                "to_address": (c.meta or {}).get("to_address", ""),
                "status": c.status,
                "template": (c.meta or {}).get("template", ""),
                "body": c.body,
                "created_at": c.created_at.isoformat(),
            }
        )
    return out


@router.get("/mail-queue/export")
def export_mail_queue(
    user: CurrentUser,
    db: Session = Depends(get_db),
    include_dispatched: bool = False,
):
    """CSV of the mail queue for a print/mail house. One row per letter."""
    import csv as _csv
    import io as _io

    stmt = select(Contact).where(Contact.channel == "mail")
    if not include_dispatched:
        stmt = stmt.where(Contact.status == "queued_for_mail")
    rows = db.scalars(stmt.order_by(Contact.id)).all()

    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["letter_id", "lead_id", "to_name", "to_address", "property", "template", "letter"])
    for c in rows:
        lead = db.get(Lead, c.lead_id)
        meta = c.meta or {}
        w.writerow(
            [
                c.id,
                c.lead_id,
                meta.get("to_name", ""),
                (meta.get("to_address") or "").replace("\n", ", "),
                lead.address if lead else "",
                meta.get("template", ""),
                c.body,
            ]
        )
    audit(db, actor=user.email, action="mail_queue_exported", target=f"{len(rows)} letters")
    db.commit()
    from fastapi.responses import Response

    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="mail-queue.csv"'},
    )


@router.post("/mail-queue/{contact_id}/dispatched")
def mark_mail_dispatched(
    contact_id: int, user: CurrentUser, db: Session = Depends(get_db)
):
    """Mark a queued letter as actually mailed."""
    c = db.get(Contact, contact_id)
    if not c or c.channel != "mail":
        raise HTTPException(404, "Letter not found")
    if c.status != "queued_for_mail":
        raise HTTPException(409, f"Letter is already '{c.status}'")
    c.status = "mailed"
    db.commit()
    return {"ok": True, "id": c.id, "status": c.status}


# ═══════════════════════════════════════════════════════════════
# Activity
# ═══════════════════════════════════════════════════════════════
@router.get("/activity")
def activity(user: CurrentUser, db: Session = Depends(get_db), limit: int = 60):
    rows = db.scalars(
        select(Contact).order_by(Contact.id.desc()).limit(limit)
    ).all()
    icons = {"call": "📞", "sms": "💬", "email": "📧", "note": "📝"}
    out = []
    for c in rows:
        lead = db.get(Lead, c.lead_id)
        addr = lead.address if lead else "unknown"
        label = {
            "call": "Call",
            "sms": "SMS",
            "email": "Email",
            "note": "Note",
        }.get(c.channel, c.channel)
        status = c.status
        verb = {
            "sent": "sent",
            "delivered": "delivered",
            "simulated": "simulated (dry run)",
            "failed": "failed",
            "blocked_compliance": "blocked by compliance",
            "no_answer": "no answer",
            "answered": "answered",
            "voicemail": "voicemail",
            "replied": "replied",
            "logged": "logged",
        }.get(status, status)
        out.append(
            {
                "icon": icons.get(c.channel, "•"),
                "text": f"{label} {verb} — {addr}",
                "detail": (c.body or c.error or "")[:140],
                "time": c.created_at.isoformat(),
                "channel": c.channel,
                "status": status,
                "lead_id": c.lead_id,
            }
        )
    return out


# ═══════════════════════════════════════════════════════════════
# Pipeline control
# ═══════════════════════════════════════════════════════════════
class RunPayload(BaseModel):
    # Leave city/state empty to sweep every configured MARKET_STATES market.
    city: str | None = None
    state: str | None = None
    states: list[str] | None = None
    limit: int = Field(default=100, ge=1, le=2000)


@router.post("/pipeline/run")
def run_pipeline(
    user: CurrentUser,
    payload: RunPayload | None = None,
    db: Session = Depends(get_db),
):
    running = db.scalar(
        select(PipelineRun).where(PipelineRun.status == "running").limit(1)
    )
    if running:
        return {"status": "already_running", "run_id": running.id}
    p = payload or RunPayload()
    run = pipeline.run_full_cycle(
        db,
        trigger=f"manual:{user.email}",
        city=p.city,
        state=p.state,
        states=p.states,
        limit=p.limit,
    )
    return {"status": run.status, "run_id": run.id, "stats": run.stats, "error": run.error}


@router.get("/pipeline/status")
def pipeline_status(user: CurrentUser, db: Session = Depends(get_db)):
    running = db.scalar(
        select(PipelineRun).where(PipelineRun.status == "running").limit(1)
    )
    last = db.scalar(select(PipelineRun).order_by(PipelineRun.id.desc()).limit(1))
    return {
        "running": bool(running),
        "last_run": (
            {
                "id": last.id,
                "status": last.status,
                "trigger": last.trigger,
                "stats": last.stats,
                "error": last.error,
                "started_at": last.started_at.isoformat(),
                "finished_at": last.finished_at.isoformat() if last.finished_at else None,
            }
            if last
            else None
        ),
    }


@router.post("/pipeline/send-due")
def send_due(user: CurrentUser, db: Session = Depends(get_db)):
    """Advance the cadence now (the scheduler does this automatically)."""
    stats = cadence_engine.process_due(db)
    return {"ok": True, "stats": stats}


@router.post("/outbound-call")
def outbound_call(
    user: CurrentUser,
    db: Session = Depends(get_db),
    deal_id: int = Query(...),
    script: str | None = None,
):
    lead = db.get(Lead, deal_id)
    if not lead:
        raise HTTPException(404, "Deal not found")
    allowed, reason = compliance.check_send_allowed(
        db, lead, "call",
        max_sms_per_day=settings.max_sms_per_lead_per_day,
        max_calls_per_week=settings.max_call_attempts_per_lead_per_week,
        enforce_quiet_hours=settings.enforce_quiet_hours,
    )
    if not allowed:
        raise HTTPException(409, f"Blocked by compliance: {reason}")
    tpl = db.scalar(select(MessageTemplate).where(MessageTemplate.key == "voice_script"))
    text = cadence_engine.render(script or (tpl.body if tpl else "Hello"), lead)
    res = telnyx.start_ai_call(lead.owner_phone or "", text, lead_id=lead.id)
    db.add(
        Contact(
            lead_id=lead.id, channel="call", direction="outbound", body=text,
            status=res.status, provider_id=res.provider_id, error=res.error,
            meta={"simulated": res.simulated, "by": user.email},
        )
    )
    if res.ok and lead.status == "new":
        lead.status = "contacted"
    db.commit()
    return {"ok": res.ok, "status": res.status, "simulated": res.simulated, "error": res.error}


# ═══════════════════════════════════════════════════════════════
# Cadence + templates
# ═══════════════════════════════════════════════════════════════
@router.get("/cadence")
def get_cadence(user: CurrentUser, db: Session = Depends(get_db)):
    """Every cadence, keyed by lead class, so you can see exactly which
    channels each class of lead will receive."""
    cadences = db.scalars(
        select(Cadence).order_by(Cadence.lead_class, Cadence.id)
    ).all()

    def pack(c: Cadence) -> dict:
        return {
            "id": c.id,
            "name": c.name,
            "description": c.description,
            "lead_class": c.lead_class,
            "active": c.is_active,
            "is_default": c.is_default,
            "channels": sorted({st.channel for st in c.steps}),
            "steps": [
                {
                    "position": st.position,
                    "channel": st.channel,
                    "delay_hours": st.delay_hours,
                    "template_key": st.template_key,
                    "day": round(st.delay_hours / 24, 1),
                }
                for st in c.steps
            ],
        }

    enrollments = {
        state: int(
            db.scalar(
                select(func.count(Enrollment.id)).where(Enrollment.state == state)
            )
            or 0
        )
        for state in ("active", "paused", "completed", "stopped")
    }

    mail_queued = int(
        db.scalar(
            select(func.count(Contact.id)).where(
                Contact.channel == "mail", Contact.status == "queued_for_mail"
            )
        )
        or 0
    )

    return {
        "cadences": [pack(c) for c in cadences],
        # Back-compat with the earlier single-cadence response shape.
        "cadence": pack(cadences[0]) if cadences else None,
        "steps": pack(cadences[0])["steps"] if cadences else [],
        "enrollments": enrollments,
        "mail_queued": mail_queued,
        "policy": {
            cls: list(chans) for cls, chans in policy.CLASS_CHANNELS.items()
        },
        "sms_allowed_classes": list(policy.SMS_ALLOWED_CLASSES),
    }


@router.get("/templates")
def list_templates(user: CurrentUser, db: Session = Depends(get_db)):
    rows = db.scalars(select(MessageTemplate).order_by(MessageTemplate.channel, MessageTemplate.key)).all()
    return [
        {
            "key": t.key,
            "channel": t.channel,
            "label": t.label,
            "subject": t.subject,
            "body": t.body,
        }
        for t in rows
    ]


class TemplateUpdate(BaseModel):
    body: str | None = None
    subject: str | None = None
    label: str | None = None


@router.patch("/templates/{key}")
def update_template(
    key: str, payload: TemplateUpdate, user: CurrentUser, db: Session = Depends(get_db)
):
    tpl = db.scalar(select(MessageTemplate).where(MessageTemplate.key == key))
    if not tpl:
        raise HTTPException(404, "Template not found")
    if payload.body is not None:
        tpl.body = payload.body
    if payload.subject is not None:
        tpl.subject = payload.subject
    if payload.label is not None:
        tpl.label = payload.label
    db.commit()
    audit(db, actor=user.email, action="template_updated", target=key)
    db.commit()
    return {"ok": True, "key": tpl.key, "body": tpl.body, "subject": tpl.subject}


# ═══════════════════════════════════════════════════════════════
# Settings + compliance admin
# ═══════════════════════════════════════════════════════════════
@router.get("/settings")
def get_settings_view(user: CurrentUser):
    return {
        "capabilities": settings.capabilities(),
        "identity": {
            "agent_name": settings.agent_name,
            "agent_company": settings.agent_company,
            "agent_phone": settings.agent_phone,
            "agent_email": settings.agent_email,
            "calendar_link": settings.agent_calendar_link,
        },
        "compliance": {
            "enforce_quiet_hours": settings.enforce_quiet_hours,
            "max_sms_per_lead_per_day": settings.max_sms_per_lead_per_day,
            "max_call_attempts_per_lead_per_week": settings.max_call_attempts_per_lead_per_week,
        },
        "lead_provider": settings.lead_provider,
        "dnc_provider": settings.dnc_provider,
    }


@router.get("/compliance/suppressions")
def list_suppressions(user: CurrentUser, db: Session = Depends(get_db), limit: int = 200):
    rows = db.scalars(select(SuppressionEntry).order_by(SuppressionEntry.id.desc()).limit(limit)).all()
    return [
        {
            "id": s.id,
            "phone": s.phone,
            "email": s.email,
            "reason": s.reason,
            "source": s.source,
            "created_at": s.created_at.isoformat(),
        }
        for s in rows
    ]


@router.post("/compliance/suppressions")
def add_suppression(
    user: CurrentUser,
    db: Session = Depends(get_db),
    phone: str = "",
    email: str = "",
    reason: str = "manual_block",
):
    compliance.add_suppression(db, phone=phone or None, email=email or None, reason=reason, source=user.email)
    db.commit()
    return {"ok": True}


@router.get("/compliance/consent-log")
def consent_log(user: CurrentUser, db: Session = Depends(get_db), limit: int = 200):
    from .models import ConsentLog

    rows = db.scalars(select(ConsentLog).order_by(ConsentLog.id.desc()).limit(limit)).all()
    return [
        {
            "id": r.id,
            "lead_id": r.lead_id,
            "phone": r.phone,
            "action": r.action,
            "channel": r.channel,
            "source": r.source,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


# ═══════════════════════════════════════════════════════════════
# Telnyx webhooks (signature-verified, not session-authenticated)
# ═══════════════════════════════════════════════════════════════
@router.post("/telnyx/voice-webhook")
async def voice_webhook(request: Request, db: Session = Depends(get_db)):
    raw = await request.body()
    sig = request.headers.get("telnyx-signature-ed25519")
    ts = request.headers.get("telnyx-timestamp")
    if not telnyx.verify_webhook(raw, sig, ts):
        raise HTTPException(401, "Invalid webhook signature")
    try:
        event = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    data = event.get("data") or {}
    etype = data.get("event_type", "")
    payload = data.get("payload") or {}
    cid = payload.get("call_control_id", "")

    import base64 as _b64
    import json as _json

    state = {}
    cs = payload.get("client_state")
    if cs:
        try:
            state = _json.loads(_b64.b64decode(cs).decode())
        except Exception:
            state = {}

    lead_id = state.get("lead_id")
    contact = None
    if lead_id:
        contact = db.scalar(
            select(Contact)
            .where(Contact.lead_id == lead_id, Contact.channel == "call")
            .order_by(Contact.id.desc())
        )

    if etype == "call.answered":
        if state.get("script"):
            telnyx.speak(cid, state["script"])
        if contact:
            contact.status = "answered"
    elif etype in ("call.hangup", "call.machine.detection.ended"):
        if contact and contact.status in ("sent", "queued"):
            contact.status = "no_answer"
    elif etype == "call.machine.premium.detection.ended":
        if payload.get("result") == "machine" and contact:
            contact.status = "voicemail"
    elif etype == "call.initiated":
        pass

    db.commit()
    return {"ok": True}


@router.post("/telnyx/message-webhook")
async def message_webhook(request: Request, db: Session = Depends(get_db)):
    """Inbound SMS — handles replies, STOP keywords, and delivery receipts."""
    raw = await request.body()
    sig = request.headers.get("telnyx-signature-ed25519")
    ts = request.headers.get("telnyx-timestamp")
    if not telnyx.verify_webhook(raw, sig, ts):
        raise HTTPException(401, "Invalid webhook signature")
    try:
        event = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    data = event.get("data") or {}
    etype = data.get("event_type", "")
    payload = data.get("payload") or {}

    from .validation import normalize_phone

    if etype == "message.received":
        from_number = normalize_phone(payload.get("from", {}).get("phone_number"))
        text = payload.get("text", "") or ""
        lead = None
        if from_number:
            lead = db.scalar(select(Lead).where(Lead.owner_phone == from_number))
        if not lead:
            return {"ok": True, "matched": False}

        db.add(
            Contact(
                lead_id=lead.id,
                channel="sms",
                direction="inbound",
                body=text,
                status="replied",
                provider_id=payload.get("id", ""),
            )
        )
        kind = compliance.classify_reply(text)

        if kind == "stop":
            compliance.handle_opt_out(db, lead, text=text, source="sms_reply")
            notify.notify_owner(
                db, lead,
                kind="opt_out",
                title=f"Opt-out: {lead.address}",
                body=f"The owner at {lead.address} replied STOP. "
                     f"They are suppressed and the cadence is stopped.",
                sms_body=f"Opt-out recorded for {lead.street} ({lead.owner_phone}). "
                         f"No further outreach.",
            )
        elif kind == "help":
            compliance.log_consent(
                db, lead=lead, phone=from_number or "", action="help_requested",
                channel="sms", source="sms_reply", evidence=text,
            )
        else:
            read = intent_mod.classify_inbound(text)
            compliance.log_consent(
                db, lead=lead, phone=from_number or "", action="replied",
                channel="sms", source="sms_reply", evidence=text,
            )

            if read.is_negative:
                lead.status = "dead"
                lead.intent_label = read.label
                lead.intent_score = read.score
                for enr in lead.enrollments:
                    if enr.state == "active":
                        enr.state = "stopped"
                        enr.stopped_reason = "seller declined"
                        enr.next_run_at = None
            elif read.is_hot:
                # This is the moment the whole system exists for.
                prior = lead.status
                lead.status = (
                    "appointment" if read.label == "appointment" else "qualified"
                )
                lead.intent_label = read.label
                lead.intent_score = read.score
                new_escalation = notify.flag_for_human(
                    db, lead, reason=read.summary, intent=read
                )
                _prepare_for_signature(db, lead)
                if new_escalation:
                    notify.notify_owner(
                        db, lead,
                        kind="appointment" if read.label == "appointment" else "hot_lead",
                        title=f"{'Appointment request' if read.label == 'appointment' else 'Hot lead'}: {lead.address}",
                        body=notify.build_hot_lead_email(lead, read.summary, text)[1],
                        sms_body=notify.build_hot_lead_message(lead, read.summary, text),
                    )
                    audit(
                        db, actor="system", action="hot_lead_escalated",
                        target=f"lead:{lead.id}",
                        meta={"intent": read.label, "score": read.score, "from": prior},
                    )
            else:
                # A real person replied, just not (yet) about selling.
                if lead.status in ("new", "contacted"):
                    lead.status = "engaged"
                lead.intent_label = read.label
                lead.intent_score = read.score
                for enr in lead.enrollments:
                    if enr.state == "active":
                        enr.state = "paused"
                        enr.stopped_reason = "replied"
                        enr.next_run_at = None

        db.commit()
        return {
            "ok": True,
            "matched": True,
            "intent": kind,
            "hot": bool(lead.needs_attention),
        }

    if etype in ("message.sent", "message.finalized"):
        pid = payload.get("id", "")
        if pid:
            c = db.scalar(select(Contact).where(Contact.provider_id == pid))
            if c:
                c.status = "delivered" if etype == "message.finalized" else "sent"
                db.commit()
    return {"ok": True}
