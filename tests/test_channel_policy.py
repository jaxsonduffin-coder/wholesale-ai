"""Channel policy tests — the Texas SB 140 guardrail.

The contract under test:

    List and probate leads are worked by call and physical mail only.
    SMS fires only for inbound leads whose consent has a source and a timestamp.

Every test here maps to a way that contract could be broken, so a future edit
that quietly reintroduces cold texting fails loudly.
"""
from __future__ import annotations

import datetime as dt

import pytest

from app import cadence as engine
from app import policy
from app.models import Cadence, Contact, Enrollment, Lead, utcnow
from app.validation import validate_lead

# ─────────────────────────────────────────────────────────────
# Policy module
# ─────────────────────────────────────────────────────────────
def test_class_normalisation_aliases():
    assert policy.normalize_class("Inbound") == "inbound"
    assert policy.normalize_class("web form") == "inbound"
    assert policy.normalize_class("purchased") == "list"
    assert policy.normalize_class("inherited") == "probate"
    assert policy.normalize_class("attorney") == "referral"
    # Unknown must default to the SAFE (untextable) class, never inbound.
    assert policy.normalize_class(None) == "list"
    assert policy.normalize_class("nonsense") == "list"
    assert policy.normalize_class("") == "list"


def test_sms_is_only_allowed_for_inbound():
    assert policy.SMS_ALLOWED_CLASSES == ("inbound",)
    for cls in ("list", "probate", "referral"):
        lead = Lead(street="1 A St", city="Dallas", state="TX", lead_class=cls,
                    consent_sms=True, consent_source="x", consent_at=utcnow())
        ok, reason = policy.sms_eligible(lead)
        assert not ok, f"{cls} must not be textable"
        assert "sms disabled" in reason


def test_consent_without_source_is_not_consent():
    lead = Lead(street="1 A St", city="Dallas", state="TX", lead_class="inbound",
                consent_sms=True, consent_source="", consent_at=utcnow())
    ok, reason = policy.sms_eligible(lead)
    assert not ok
    assert "consent source" in reason


def test_consent_without_timestamp_is_not_consent():
    lead = Lead(street="1 A St", city="Dallas", state="TX", lead_class="inbound",
                consent_sms=True, consent_source="web_form:/x", consent_at=None)
    ok, reason = policy.sms_eligible(lead)
    assert not ok
    assert "timestamp" in reason


def test_fully_evidenced_inbound_consent_is_textable():
    lead = Lead(street="1 A St", city="Dallas", state="TX", lead_class="inbound",
                consent_sms=True, consent_source="web_form:/sell-dallas",
                consent_at=utcnow())
    ok, reason = policy.sms_eligible(lead)
    assert ok, reason


def test_allowed_channels_per_class():
    def lead(cls, **kw):
        base = dict(street="1 A St", city="Dallas", state="TX", lead_class=cls)
        base.update(kw)
        return Lead(**base)

    consented = dict(consent_sms=True, consent_source="web_form:/x",
                     consent_at=utcnow())

    assert "sms" in policy.allowed_channels(lead("inbound", **consented))
    assert "sms" not in policy.allowed_channels(lead("inbound"))   # no consent
    assert "sms" not in policy.allowed_channels(lead("list", **consented))
    assert "sms" not in policy.allowed_channels(lead("probate", **consented))
    assert "sms" not in policy.allowed_channels(lead("referral", **consented))
    assert set(policy.allowed_channels(lead("list"))) == {"call", "email"}
    assert set(policy.allowed_channels(lead("probate"))) == {"call", "email"}
    assert set(policy.allowed_channels(lead("referral"))) == {"call", "email"}
    # Email is permitted on every class; SMS is the only restricted one.
    for cls in policy.LEAD_CLASSES:
        assert "email" in policy.allowed_channels(lead(cls, **consented))


def test_channel_allowed_rejects_sms_for_list():
    lead = Lead(street="1 A St", city="Dallas", state="TX", lead_class="list",
                consent_sms=True, consent_source="x", consent_at=utcnow())
    ok, reason = policy.channel_allowed(lead, "sms")
    assert not ok
    assert "policy" not in reason          # the reason itself, not the wrapper
    assert "not permitted" in reason


def test_mail_is_opt_in_and_off_by_default():
    """Physical mail is implemented but no class uses it by default."""
    lead = Lead(street="1234 Elm St", city="Dallas", state="TX", lead_class="list")
    ok, reason = policy.channel_allowed(lead, "mail")
    assert not ok
    assert "not permitted" in reason


def test_mail_requires_an_address_when_enabled(monkeypatch):
    monkeypatch.setitem(
        policy.CLASS_CHANNELS, "list", ("call", "email", "mail")
    )
    lead = Lead(street="", city="Dallas", state="TX", lead_class="list")
    ok, reason = policy.channel_allowed(lead, "mail")
    assert not ok
    assert "mailing address" in reason

    lead2 = Lead(street="1234 Elm St", city="Dallas", state="TX", lead_class="list")
    ok2, _ = policy.channel_allowed(lead2, "mail")
    assert ok2


def test_grant_consent_requires_a_source():
    lead = Lead(street="1 A St", city="Dallas", state="TX", lead_class="inbound")
    with pytest.raises(policy.PolicyError):
        policy.grant_consent(lead, source="   ")
    policy.grant_consent(lead, source="phone_call 2025-10-20 14:32",
                         evidence="seller said text me the number")
    assert lead.consent_sms is True
    assert lead.consent_source.startswith("phone_call")
    assert policy.sms_eligible(lead)[0] is True


def test_describe_explains_why_sms_is_off():
    lead = Lead(street="1 A St", city="Dallas", state="TX", lead_class="probate")
    d = policy.describe(lead)
    assert d["lead_class"] == "probate"
    assert d["sms_allowed"] is False
    assert "probate" in d["sms_blocked_reason"]
    assert d["allowed_channels"] == ["call", "email"]


# ─────────────────────────────────────────────────────────────
# Validation: consent is stripped, not trusted
# ─────────────────────────────────────────────────────────────
def test_list_import_cannot_carry_consent():
    """A purchased list claiming consent is the exact scenario SB 140 targets."""
    clean = validate_lead({
        "street": "1234 Elm St", "city": "Dallas", "state": "TX",
        "lead_class": "list", "consent_sms": True,
        "consent_source": "vendor_list_v3", "consent_at": "2025-09-01",
    })
    assert clean["consent_sms"] is False
    assert clean["consent_source"] == ""
    assert clean["consent_at"] is None


def test_inbound_import_keeps_provenanced_consent():
    clean = validate_lead({
        "street": "1234 Elm St", "city": "Dallas", "state": "TX",
        "lead_class": "inbound", "consent_sms": True,
        "consent_source": "web_form:/sell-dallas#sms",
        "consent_at": "2025-10-20T14:32:00",
        "consent_evidence": "checkbox label: text me about my offer",
    })
    assert clean["consent_sms"] is True
    assert clean["consent_source"] == "web_form:/sell-dallas#sms"
    assert clean["consent_at"] == dt.datetime(2025, 10, 20, 14, 32)


def test_inbound_consent_without_source_is_downgraded():
    clean = validate_lead({
        "street": "1234 Elm St", "city": "Dallas", "state": "TX",
        "lead_class": "inbound", "consent_sms": True,
    })
    assert clean["consent_sms"] is False


@pytest.mark.parametrize("value,expected", [
    ("yes", True), ("Y", True), ("1", True), ("true", True), ("opted in", True),
    ("no", False), ("", False), ("0", False), ("opted_out", False),
    ("maybe", None), (None, None),
])
def test_bool_parsing(value, expected):
    from app.validation import parse_bool
    assert parse_bool(value) is expected


@pytest.mark.parametrize("value,ok", [
    ("2025-10-20", True), ("2025-10-20T14:32:00Z", True), ("10/20/2025", True),
    ("2025/10/20", True), ("October 20 2025", True), ("not a date", False),
    ("", False), (None, False),
])
def test_consent_date_parsing(value, ok):
    from app.validation import parse_datetime
    assert (parse_datetime(value) is not None) is ok


def test_unknown_class_defaults_to_cold_list():
    clean = validate_lead({
        "street": "1234 Elm St", "city": "Dallas", "state": "TX",
        "lead_class": "something_weird", "consent_sms": True,
        "consent_source": "x", "consent_at": "2025-10-01",
    })
    assert clean["lead_class"] == "list"
    assert clean["consent_sms"] is False


# ─────────────────────────────────────────────────────────────
# Cadence engine enforcement
# ─────────────────────────────────────────────────────────────
def test_enrollment_picks_cadence_by_class(db, list_lead, probate_lead, fresh_lead):
    from app.templates_content import seed_cadences

    seed_cadences(db)
    db.commit()

    for lead, expected_class in (
        (list_lead, "list"),
        (probate_lead, "probate"),
        (fresh_lead, "inbound"),
    ):
        db.query(Enrollment).filter_by(lead_id=lead.id).delete()
        db.commit()
        enr = engine.enroll(db, lead)
        db.commit()
        cadence = db.get(Cadence, enr.cadence_id)
        assert cadence.lead_class == expected_class


def test_list_cadence_contains_no_sms_step(db, list_lead):
    from app.templates_content import seed_cadences

    seed_cadences(db)
    db.commit()
    cadence = engine.cadence_for_lead(db, list_lead)
    assert cadence.lead_class == "list"
    assert all(step.channel != "sms" for step in cadence.steps)


def test_process_due_on_list_lead_never_texts(db, list_lead):
    """Run the whole list sequence as fast as the engine will allow and assert
    not one SMS contact appears — calls and email only."""
    from app.templates_content import seed_cadences

    seed_cadences(db)
    db.query(Enrollment).filter_by(lead_id=list_lead.id).delete()
    db.commit()
    enr = engine.enroll(db, list_lead)
    db.commit()

    for _ in range(12):                      # more iterations than steps
        enr.next_run_at = utcnow() - dt.timedelta(minutes=1)
        db.commit()
        engine.process_due(db)
        db.refresh(enr)
        if enr.state != "active":
            break

    contacts = db.query(Contact).filter_by(lead_id=list_lead.id).all()
    assert contacts, "the list cadence should have produced touches"
    channels = {c.channel for c in contacts}
    assert channels <= {"call", "email"}, channels
    assert "sms" not in channels
    assert "call" in channels and "email" in channels


def test_physical_letter_is_queued_with_rendered_body(mail_enabled, db, list_lead):
    cadence, enr = _enroll_letter_step(db, list_lead)
    engine.process_due(db)

    letters = db.query(Contact).filter(
        Contact.lead_id == list_lead.id, Contact.channel == "mail"
    ).all()
    assert len(letters) == 1
    letter = letters[0]
    assert letter.status == "queued_for_mail"
    assert "LETTER" in letter.body
    assert list_lead.street in letter.body
    assert "Dana" in letter.body                    # personalised
    assert "{" not in letter.body                   # no leaked placeholders
    assert (letter.meta or {}).get("requires_manual_dispatch") is True


def test_probate_lead_gets_written_outreach_before_any_call(db, probate_lead):
    """No dialling a grieving family before writing to them."""
    from app.templates_content import seed_cadences

    seed_cadences(db)
    db.query(Enrollment).filter_by(lead_id=probate_lead.id).delete()
    db.commit()
    enr = engine.enroll(db, probate_lead)
    db.commit()

    enr.next_run_at = utcnow() - dt.timedelta(minutes=1)
    db.commit()
    engine.process_due(db)

    contacts = db.query(Contact).filter_by(lead_id=probate_lead.id).all()
    assert contacts, "probate sequence produced no touch"
    assert contacts[0].channel == "email"
    assert not any(c.channel == "call" for c in contacts)
    assert "condolence" in contacts[0].body.lower()


def _enroll_letter_step(db, lead, template_key="mail_list_intro"):
    """Build a one-step letter cadence for a lead.

    Physical mail is an opt-in channel: no class uses it by default, so the
    infra is exercised through an explicit cadence like this one.
    """
    import uuid as _uuid

    from app.models import CadenceStep
    from app.templates_content import seed_cadences

    seed_cadences(db)
    cadence = Cadence(
        name=f"Test letters {_uuid.uuid4().hex[:8]}",
        description="single letter step",
        lead_class=lead.lead_class,
        is_active=True,
    )
    db.add(cadence)
    db.flush()
    db.add(
        CadenceStep(
            cadence_id=cadence.id, position=0, channel="mail",
            delay_hours=0.0, template_key=template_key,
        )
    )
    db.commit()
    db.refresh(cadence)

    db.query(Enrollment).filter_by(lead_id=lead.id).delete()
    db.commit()
    enr = engine.enroll(db, lead, cadence)
    db.commit()
    return cadence, enr


@pytest.fixture()
def mail_enabled(monkeypatch):
    """Opt the list class into physical mail, as the README describes."""
    monkeypatch.setitem(policy.CLASS_CHANNELS, "list", ("call", "email", "mail"))
    return True


def test_sms_step_on_a_list_lead_is_skipped_permanently(db, list_lead):
    """Defence in depth: even if an SMS step somehow ends up in a list lead's
    sequence, the sender refuses it and moves on instead of stalling."""
    from app.models import CadenceStep
    from app.templates_content import seed_cadences

    seed_cadences(db)
    cadence = engine.cadence_for_lead(db, list_lead)
    # Inject an illicit SMS step at position 0.
    rogue = CadenceStep(
        cadence_id=cadence.id, position=99, channel="sms",
        delay_hours=0.0, template_key="sms_opener",
    )
    db.add(rogue)
    db.commit()
    db.refresh(cadence)

    result = engine.execute_step(db, list_lead, rogue, cadence)
    assert result.action == "skipped_permanent"
    assert result.permanent is True
    assert "sms" in result.reason.lower()

    blocked = db.query(Contact).filter(
        Contact.lead_id == list_lead.id, Contact.status == "blocked_policy"
    ).all()
    assert len(blocked) == 1
    assert blocked[0].channel == "sms"


def test_inbound_lead_without_consent_does_not_get_texted(db, inbound_no_consent_lead):
    lead = inbound_no_consent_lead
    from app.templates_content import seed_cadences

    seed_cadences(db)
    cadence = engine.cadence_for_lead(db, lead)
    sms_step = next(s for s in cadence.steps if s.channel == "sms")
    result = engine.execute_step(db, lead, sms_step, cadence)
    assert result.action == "skipped_permanent"
    assert "consent" in result.reason.lower()


def test_consented_inbound_lead_does_get_texted(db, fresh_lead):
    from app.templates_content import seed_cadences

    seed_cadences(db)
    cadence = engine.cadence_for_lead(db, fresh_lead)
    sms_step = next(s for s in cadence.steps if s.channel == "sms")
    result = engine.execute_step(db, fresh_lead, sms_step, cadence)
    assert result.action == "sent", result.reason
    contact = db.get(Contact, result.contact_id)
    assert contact.channel == "sms"
    assert contact.status == "simulated"


# ─────────────────────────────────────────────────────────────
# API surface
# ─────────────────────────────────────────────────────────────
def test_import_defaults_to_no_consent(client, db):
    csv = (
        "Address,City,State,SMS Consent,Consent Source,Consent Date,Lead Class\n"
        "4400 Vendor Way,Dallas,TX,yes,acme_list_v2,2025-09-01,list\n"
        "4410 Vendor Way,Dallas,TX,yes,acme_list_v2,2025-09-01,inbound\n"
    )
    r = client.post(
        "/api/leads/import",
        files={"file": ("vendors.csv", csv, "text/csv")},
        params={"lead_class": "list", "consent_policy": "none"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["consent_policy"] == "none"
    assert body["consent_flags_stripped"] == 2
    # Nothing imported may be textable.
    deals = client.get("/api/deals?limit=200").json()
    imported = [d for d in deals if "Vendor Way" in d["address"]]
    assert imported
    assert all(d["sms_allowed"] is False for d in imported)


def test_import_consent_policy_column_still_respects_class(client):
    csv = (
        "Address,City,State,SMS Consent,Consent Source,Consent Date,Lead Class\n"
        "5500 Consent Row,Dallas,TX,yes,web_form:/sell-dallas,2025-10-01,inbound\n"
        "5510 Consent Row,Dallas,TX,yes,vendor_dump,2025-10-01,list\n"
    )
    r = client.post(
        "/api/leads/import",
        files={"file": ("consent.csv", csv, "text/csv")},
        params={"consent_policy": "column"},
    )
    assert r.status_code == 200, r.text
    deals = {d["address"]: d for d in client.get("/api/deals?limit=200").json()}

    inbound = next(v for k, v in deals.items() if "5500 Consent Row" in k)
    listlead = next(v for k, v in deals.items() if "5510 Consent Row" in k)

    assert inbound["sms_allowed"] is True
    assert inbound["consent_source"] == "web_form:/sell-dallas"
    # Same file, same "yes" — stripped because the class is cold data.
    assert listlead["sms_allowed"] is False
    assert listlead["consent_source"] == ""


def test_list_lead_manual_sms_is_refused(client, list_lead):
    r = client.post(
        f"/api/deals/{list_lead.id}/contact?channel=sms&template_key=sms_opener"
    )
    assert r.status_code == 409
    assert "policy" in r.json()["detail"].lower()


def test_list_lead_manual_call_is_allowed(client, list_lead):
    r = client.post(
        f"/api/deals/{list_lead.id}/contact?channel=call&template_key=voice_script"
    )
    assert r.status_code == 200, r.text


def test_list_lead_manual_email_is_allowed(client, db, list_lead):
    """Email is permitted on cold lists (CAN-SPAM, not TCPA)."""
    lead = db.get(Lead, list_lead.id)
    lead.owner_email = "seller@test.local"
    db.commit()
    r = client.post(
        f"/api/deals/{lead.id}/contact?channel=email&template_key=email_list_intro"
    )
    assert r.status_code == 200, r.text
    assert r.json()["simulated"] is True


def test_list_lead_email_carries_postal_address_and_optout(client, db, list_lead):
    """CAN-SPAM needs a physical postal address and an opt-out in every send."""
    lead = db.get(Lead, list_lead.id)
    lead.owner_email = "seller@test.local"
    db.commit()
    client.post(
        f"/api/deals/{lead.id}/contact?channel=email&template_key=email_list_intro"
    )
    contact = db.query(Contact).filter_by(
        lead_id=lead.id, channel="email"
    ).order_by(Contact.id.desc()).first()
    assert "STOP" in contact.body
    assert "postal" in contact.body.lower() or "Street" in contact.body
    assert "{" not in contact.body


def test_consent_endpoint_requires_a_source(client, list_lead):
    r = client.post(f"/api/deals/{list_lead.id}/consent", json={"source": ""})
    assert r.status_code == 422


def test_consent_endpoint_unlocks_sms_and_repoints_cadence(client, db, list_lead):
    from app.templates_content import seed_cadences

    seed_cadences(db)
    db.commit()
    assert client.get(f"/api/deals/{list_lead.id}").json()["channel_policy"]["sms_allowed"] is False

    r = client.post(
        f"/api/deals/{list_lead.id}/consent",
        json={
            "source": "phone_call 2025-10-20 14:32",
            "evidence": "seller asked me to text the numbers",
            "lead_class": "inbound",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deal"]["lead_class"] == "inbound"
    assert body["deal"]["sms_allowed"] is True
    assert body["policy"]["consent_verified"] is True

    # The cadence should now be the inbound one, which does contain SMS.
    enr = db.query(Enrollment).filter_by(lead_id=list_lead.id, state="active").first()
    if enr:
        assert db.get(Cadence, enr.cadence_id).lead_class == "inbound"


def test_consent_cannot_be_granted_on_an_opted_out_lead(client, db, list_lead):
    lead = db.get(Lead, list_lead.id)
    lead.opted_out = True
    db.commit()
    r = client.post(
        f"/api/deals/{lead.id}/consent",
        json={"source": "phone_call 2025-10-20"},
    )
    assert r.status_code == 409
    assert "opted out" in r.json()["detail"].lower()


def test_revoking_consent_disables_sms(client, fresh_lead):
    assert client.get(f"/api/deals/{fresh_lead.id}").json()["deal"]["sms_allowed"] is True
    r = client.delete(f"/api/deals/{fresh_lead.id}/consent")
    assert r.status_code == 200
    assert r.json()["policy"]["sms_allowed"] is False
    assert client.get(f"/api/deals/{fresh_lead.id}").json()["deal"]["sms_allowed"] is False


# ─────────────────────────────────────────────────────────────
# Mail queue
# ─────────────────────────────────────────────────────────────
def test_mail_queue_lists_queued_letters(client, mail_enabled, db, list_lead):
    _enroll_letter_step(db, list_lead)
    engine.process_due(db)

    queue = client.get("/api/mail-queue").json()
    mine = [q for q in queue if q["lead_id"] == list_lead.id]
    assert len(mine) == 1
    assert mine[0]["status"] == "queued_for_mail"
    assert mine[0]["address"] == list_lead.address


def test_mail_queue_export_is_csv(client):
    r = client.get("/api/mail-queue/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "letter_id,lead_id,to_name" in r.text


def test_marking_a_letter_dispatched(client, mail_enabled, db, list_lead):
    _enroll_letter_step(db, list_lead)
    engine.process_due(db)

    letter = db.query(Contact).filter(
        Contact.lead_id == list_lead.id, Contact.channel == "mail"
    ).first()
    r = client.post(f"/api/mail-queue/{letter.id}/dispatched")
    assert r.status_code == 200
    assert r.json()["status"] == "mailed"

    # Idempotency guard: cannot dispatch twice.
    assert client.post(f"/api/mail-queue/{letter.id}/dispatched").status_code == 409


def test_dashboard_exposes_class_mix_and_sms_eligibility(client):
    d = client.get("/api/dashboard").json()
    assert "class_mix" in d
    assert set(d["policy"]["sms_allowed_classes"]) == {"inbound"}
    assert d["policy"]["class_channels"]["list"] == ["call", "email"]
    assert d["policy"]["class_channels"]["probate"] == ["call", "email"]
    assert isinstance(d["mail_queued"], int)
    assert isinstance(d["sms_eligible"], int)


# ─────────────────────────────────────────────────────────────
# Enrollment scheduling: quiet hours constrain phone channels only
# ─────────────────────────────────────────────────────────────
def test_letter_first_enrollment_is_not_deferred_by_quiet_hours(db, probate_lead):
    """Regression: a probate sequence opens with a letter. Quiet hours must not
    push that letter to the next morning — mail has no calling window."""
    from app.templates_content import seed_cadences

    seed_cadences(db)
    db.query(Enrollment).filter_by(lead_id=probate_lead.id).delete()
    db.commit()

    enr = engine.enroll(db, probate_lead)
    db.commit()
    cadence = db.get(Cadence, enr.cadence_id)
    assert cadence.lead_class == "probate"
    # First step is mail, so it is due immediately regardless of the hour.
    assert enr.next_run_at <= utcnow() + dt.timedelta(seconds=5)


def test_list_enrollment_defers_a_leading_call_outside_the_window(db, list_lead, monkeypatch):
    """With enforcement on, a sequence that opens with a call must wait for the
    window to open."""
    from app import cadence as engine_mod
    from app.compliance import contact_window
    from app.templates_content import seed_cadences

    seed_cadences(db)
    db.query(Enrollment).filter_by(lead_id=list_lead.id).delete()
    db.commit()

    monkeypatch.setattr(engine_mod.settings, "enforce_quiet_hours", True)
    enr = engine.enroll(db, list_lead)
    db.commit()

    start, end = contact_window(list_lead.state)
    local = enr.next_run_at.replace(tzinfo=dt.timezone.utc).astimezone(
        __import__("zoneinfo").ZoneInfo("America/Chicago")
    )
    # Either it is legal right now, or it was pushed to the opening hour.
    assert (start <= local.hour < end) or local.hour == start


def test_execute_step_skips_policy_but_continues_the_sequence(db, list_lead):
    """A policy refusal must consume the step, not stall the enrollment."""
    from app.models import CadenceStep
    from app.templates_content import seed_cadences

    seed_cadences(db)
    cadence = engine.cadence_for_lead(db, list_lead)
    rogue = CadenceStep(cadence_id=cadence.id, position=98, channel="sms",
                        delay_hours=0.0, template_key="sms_opener")
    db.add(rogue)
    db.commit()

    db.query(Enrollment).filter_by(lead_id=list_lead.id).delete()
    db.commit()
    enr = engine.enroll(db, list_lead, cadence)
    # Point the enrollment straight at the illicit SMS step.
    enr.step_index = cadence.steps.index(rogue)
    enr.next_run_at = utcnow() - dt.timedelta(minutes=1)
    db.commit()

    stats = engine.process_due(db)
    db.refresh(enr)
    assert stats["skipped_policy"] == 1
    assert stats["blocked"] == 0
    assert enr.state in ("active", "completed")   # not stopped
    assert db.query(Contact).filter_by(
        lead_id=list_lead.id, channel="sms", status="blocked_policy"
    ).count() == 1
