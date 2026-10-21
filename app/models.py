"""SQLAlchemy models.

Design notes:
  * A Lead is one property + its owner + our analysis, denormalised. Property
    records in this business are always worked as a single unit, so splitting
    into Property/Owner tables adds joins without buying anything.
  * Every outbound attempt is a Contact row. There is no "sent" boolean
    anywhere — activity is derived from the log, so the dashboard can never
    report an action that did not happen.
  * RejectedLead preserves what the validation layer threw away, so you can
    audit your lead source instead of wondering where the volume went.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


# ─────────────────────────────────────────────────────────────
# Lead lifecycle states (ordered)
# ─────────────────────────────────────────────────────────────
STATUSES = [
    "new",            # ingested, not yet contacted
    "contacted",      # at least one outbound attempt
    "engaged",        # prospect replied / answered
    "qualified",      # motivation + numbers confirmed by a human
    "appointment",    # call booked
    "offer_made",     # written or verbal offer delivered
    "under_contract", # purchase agreement executed
    "signed",         # fully executed, ready for a buyer
    "sent_to_jv",     # marketed to buyers / JV partners
    "closed",         # assigned, fee collected
    "dead",           # exhausted or disqualified
]

CHANNELS = ["call", "sms", "email", "mail", "note"]


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(primary_key=True)

    # --- property -------------------------------------------------
    street: Mapped[str] = mapped_column(String(255), index=True)
    city: Mapped[str] = mapped_column(String(120), default="", index=True)
    county: Mapped[str] = mapped_column(String(120), default="")
    state: Mapped[str] = mapped_column(String(2), default="", index=True)
    zip_code: Mapped[str] = mapped_column(String(10), default="", index=True)
    property_type: Mapped[str] = mapped_column(String(40), default="single_family")
    bedrooms: Mapped[float | None] = mapped_column(Float, nullable=True)
    bathrooms: Mapped[float | None] = mapped_column(Float, nullable=True)
    sqft: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lot_sqft: Mapped[int | None] = mapped_column(Integer, nullable=True)
    year_built: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- owner ----------------------------------------------------
    owner_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    owner_first_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    owner_phone: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    owner_phone_2: Mapped[str | None] = mapped_column(String(32), nullable=True)
    owner_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    owner_language: Mapped[str] = mapped_column(String(8), default="en")
    owner_mailing_address: Mapped[str | None] = mapped_column(String(400), nullable=True)
    is_absentee: Mapped[bool] = mapped_column(Boolean, default=False)
    is_out_of_state: Mapped[bool] = mapped_column(Boolean, default=False)
    years_owned: Mapped[float | None] = mapped_column(Float, nullable=True)
    equity_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    # --- distress signals -----------------------------------------
    tax_assessed_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    tax_delinquent: Mapped[bool] = mapped_column(Boolean, default=False)
    tax_delinquent_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    pre_foreclosure: Mapped[bool] = mapped_column(Boolean, default=False)
    vacant: Mapped[bool] = mapped_column(Boolean, default=False)
    probate_or_inherited: Mapped[bool] = mapped_column(Boolean, default=False)
    tired_landlord: Mapped[bool] = mapped_column(Boolean, default=False)
    code_violations: Mapped[bool] = mapped_column(Boolean, default=False)
    asking_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    # --- analysis -------------------------------------------------
    arv: Mapped[float] = mapped_column(Float, default=0.0)
    comp_count: Mapped[int] = mapped_column(Integer, default=0)
    comp_source: Mapped[str] = mapped_column(String(40), default="")
    repair_estimate: Mapped[float] = mapped_column(Float, default=0.0)
    repair_level: Mapped[str] = mapped_column(String(20), default="light")
    max_offer: Mapped[float] = mapped_column(Float, default=0.0)
    assignment_fee: Mapped[float] = mapped_column(Float, default=10000.0)
    projected_spread: Mapped[float] = mapped_column(Float, default=0.0)
    motivation_score: Mapped[float] = mapped_column(Float, default=0.0)
    motivation_reasons: Mapped[list] = mapped_column(JSON, default=list)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    grade: Mapped[str] = mapped_column(String(1), default="F", index=True)
    analyzed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    # --- pipeline -------------------------------------------------
    status: Mapped[str] = mapped_column(String(24), default="new", index=True)
    contract_signed: Mapped[bool] = mapped_column(Boolean, default=False)
    sent_to_jv: Mapped[bool] = mapped_column(Boolean, default=False)
    offered_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    accepted_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    closed_fee: Mapped[float | None] = mapped_column(Float, nullable=True)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    # --- compliance -----------------------------------------------
    # lead_class drives which channels may ever be used (see app/policy.py).
    # inbound | referral | list | probate. Anything unlabelled is treated as
    # cold list data, which can never be texted.
    lead_class: Mapped[str] = mapped_column(String(16), default="list", index=True)
    consent_sms: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_call: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_source: Mapped[str] = mapped_column(String(60), default="")
    # Free-text proof: form URL, call transcript reference, screenshot id.
    consent_evidence: Mapped[str] = mapped_column(Text, default="")
    consent_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    dnc_checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    dnc_listed: Mapped[bool] = mapped_column(Boolean, default=False)
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False)
    opted_out_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    opt_out_reason: Mapped[str] = mapped_column(String(120), default="")
    timezone: Mapped[str] = mapped_column(String(64), default="America/Chicago")

    # --- human handoff ---------------------------------------------
    # True only when a seller has said something that means "talk to me".
    # Everything else runs autonomously; this is the queue that gets a human.
    needs_attention: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    attention_reason: Mapped[str] = mapped_column(String(255), default="")
    attention_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    interested_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    intent_label: Mapped[str] = mapped_column(String(24), default="")
    intent_score: Mapped[int] = mapped_column(Integer, default=0)

    # --- provenance ------------------------------------------------
    source: Mapped[str] = mapped_column(String(60), default="manual", index=True)
    source_ref: Mapped[str] = mapped_column(String(255), default="")
    address_key: Mapped[str] = mapped_column(String(64), default="", index=True)
    enrichment_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_enriched_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    contacts: Mapped[list["Contact"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan", order_by="Contact.id"
    )
    enrollments: Mapped[list["Enrollment"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("address_key", name="uq_lead_address_key"),
        Index("ix_lead_status_grade", "status", "grade"),
    )

    # --- presentation ----------------------------------------------
    @property
    def address(self) -> str:
        parts = [self.street, self.city, f"{self.state} {self.zip_code}".strip()]
        return ", ".join(p for p in parts if p and p.strip(", "))

    @property
    def is_contactable(self) -> bool:
        return bool(self.owner_phone or self.owner_email)

    @property
    def sms_consent_ok(self) -> bool:
        """Verifiable SMS consent. Delegates to the policy module so there is
        exactly one definition of 'eligible to text'."""
        from .policy import sms_eligible

        return sms_eligible(self)[0]

    @property
    def can_be_worked(self) -> bool:
        """A lead is only workable if it is contactable, not suppressed, and
        we actually know what the house is worth. This is the guard that
        blocked all seven of the original fake leads."""
        return (
            self.is_contactable
            and not self.opted_out
            and not self.dnc_listed
            and self.arv > 0
            and self.status not in ("dead", "closed")
        )


class RejectedLead(Base):
    """Garbage that the ingestion filter refused. Kept for source auditing."""

    __tablename__ = "rejected_leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(60), default="")
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Comp(Base):
    """A comparable sale used to derive ARV."""

    __tablename__ = "comps"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    address: Mapped[str] = mapped_column(String(255), default="")
    sold_price: Mapped[float] = mapped_column(Float, default=0.0)
    sold_date: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    sqft: Mapped[int] = mapped_column(Integer, default=0)
    distance_miles: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_per_sqft: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Contact(Base):
    """Every outbound or inbound touch. The single source of truth for activity."""

    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(16), index=True)   # call|sms|email|note
    direction: Mapped[str] = mapped_column(String(16), default="outbound")
    body: Mapped[str] = mapped_column(Text, default="")
    subject: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    # queued | simulated | sent | delivered | failed | answered | no_answer |
    # voicemail | busy | replied | opted_out | blocked_compliance
    provider_id: Mapped[str] = mapped_column(String(120), default="")
    error: Mapped[str] = mapped_column(String(500), default="")
    cadence_step: Mapped[str] = mapped_column(String(40), default="")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)

    lead: Mapped["Lead"] = relationship(back_populates="contacts")


class Cadence(Base):
    """A named outreach sequence. Steps live in CadenceStep, ordered."""

    __tablename__ = "cadences"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    # Cadences are selected per lead class so the channel mix can never drift.
    lead_class: Mapped[str] = mapped_column(String(16), default="list", index=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    steps: Mapped[list["CadenceStep"]] = relationship(
        back_populates="cadence", cascade="all, delete-orphan", order_by="CadenceStep.position"
    )


class CadenceStep(Base):
    __tablename__ = "cadence_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    cadence_id: Mapped[int] = mapped_column(ForeignKey("cadences.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    channel: Mapped[str] = mapped_column(String(16))          # call|sms|email
    # Hours from ENROLLMENT START (not from the previous step), so the
    # schedule is readable and gaps stay correct if a step is disabled.
    delay_hours: Mapped[float] = mapped_column(Float, default=0.0)
    template_key: Mapped[str] = mapped_column(String(60), default="")
    # Optional inline override of the template body
    body_override: Mapped[str] = mapped_column(Text, default="")
    subject_override: Mapped[str] = mapped_column(String(255), default="")
    cadence: Mapped["Cadence"] = relationship(back_populates="steps")


class Enrollment(Base):
    """A lead's position inside a cadence."""

    __tablename__ = "enrollments"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    cadence_id: Mapped[int] = mapped_column(ForeignKey("cadences.id", ondelete="CASCADE"))
    step_index: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(24), default="active", index=True)
    # active | paused | completed | stopped
    next_run_at: Mapped[dt.datetime | None] = mapped_column(DateTime, index=True, nullable=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    stopped_reason: Mapped[str] = mapped_column(String(120), default="")
    lead: Mapped["Lead"] = relationship(back_populates="enrollments")


class MessageTemplate(Base):
    """Outreach copy. Editable from the dashboard, so no redeploy to change a script."""

    __tablename__ = "message_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(60), unique=True)
    channel: Mapped[str] = mapped_column(String(16), default="sms")
    label: Mapped[str] = mapped_column(String(120), default="")
    subject: Mapped[str] = mapped_column(String(255), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SuppressionEntry(Base):
    """Opt-outs, DNC hits, and manual blocks. Never contacted again."""

    __tablename__ = "suppressions"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(32), index=True, default="")
    email: Mapped[str] = mapped_column(String(255), index=True, default="")
    reason: Mapped[str] = mapped_column(String(120), default="opt_out")
    source: Mapped[str] = mapped_column(String(60), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class ConsentLog(Base):
    """Immutable audit trail for TCPA defence."""

    __tablename__ = "consent_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"), nullable=True)
    phone: Mapped[str] = mapped_column(String(32), default="")
    action: Mapped[str] = mapped_column(String(40), default="")  # granted|revoked|opt_out|dnc_hit
    channel: Mapped[str] = mapped_column(String(16), default="")
    source: Mapped[str] = mapped_column(String(120), default="")
    evidence: Mapped[str] = mapped_column(Text, default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    trigger: Mapped[str] = mapped_column(String(20), default="manual")
    status: Mapped[str] = mapped_column(String(20), default="running", index=True)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(255), default="")
    action: Mapped[str] = mapped_column(String(120), default="")
    target: Mapped[str] = mapped_column(String(255), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Contract(Base):
    """Generated purchase agreement + captured signature.

    v1 set `contract_signed = true` on a house with no owner name, no price and
    ARV 0. Here a Contract row is required evidence before a deal can be
    considered under contract, and the signature is stored with its source.
    """

    __tablename__ = "contracts"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), index=True
    )
    body: Mapped[str] = mapped_column(Text, default="")
    offer_price: Mapped[float] = mapped_column(Float, default=0.0)
    signed: Mapped[bool] = mapped_column(Boolean, default=False)
    signed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    signed_by: Mapped[str] = mapped_column(String(255), default="")
    signature_data: Mapped[str] = mapped_column(Text, default="")
    signature_ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Alert(Base):
    """An owner notification: "this seller wants to talk".

    Separate from Contact because these are messages to YOU, not to the lead.
    Keeping them apart means the activity feed can never confuse an outbound
    seller message with an internal ping.
    """

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int | None] = mapped_column(
        ForeignKey("leads.id", ondelete="SET NULL"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), default="hot_lead", index=True)
    # hot_lead | appointment | contract_ready | inbound_form | system
    title: Mapped[str] = mapped_column(String(255), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    channel: Mapped[str] = mapped_column(String(16), default="in_app")
    # in_app | sms | email
    status: Mapped[str] = mapped_column(String(20), default="sent", index=True)
    error: Mapped[str] = mapped_column(String(300), default="")
    read_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
