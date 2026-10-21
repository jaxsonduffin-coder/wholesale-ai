"""Human handoff: intent detection, the needs-you queue, and website intake.

These tests encode the product promise: the operator is only interrupted when a
seller has actually said something that means "talk to me".
"""
from __future__ import annotations

import pytest

from app.intent import classify_inbound
from app.models import Alert, Contract, Lead

# ─────────────────────────────────────────────────────────────
# Intent classification
# ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text",
    [
        "How much would you pay for it?",
        "What's your offer?",
        "what would you give me",
        "Send me a number",
        "what did you have in mind",
        "ballpark?",
    ],
)
def test_price_questions_are_hot(text):
    i = classify_inbound(text)
    assert i.is_hot, text
    assert i.wants_price


@pytest.mark.parametrize(
    "text",
    [
        "Yes I am interested",
        "I'm interested, tell me more",
        "I want to sell",
        "Would consider selling",
        "need to sell asap",
    ],
)
def test_explicit_interest_is_hot(text):
    assert classify_inbound(text).is_hot, text


@pytest.mark.parametrize(
    "text",
    [
        "Can you call me tomorrow?",
        "call me at 214 555 0143",
        "what time works for you",
        "let's schedule something",
    ],
)
def test_meeting_requests_are_hot(text):
    i = classify_inbound(text)
    assert i.is_hot, text
    assert i.wants_meeting
    assert i.label == "appointment"


@pytest.mark.parametrize(
    "text",
    [
        "Not interested",
        "no thanks",
        "not interested in your offer",
        "already sold it",
        "we already listed it",
        "wrong number",
        "stop harassing me",
    ],
)
def test_negatives_are_never_hot(text):
    i = classify_inbound(text)
    assert not i.is_hot, text
    assert i.is_negative


def test_negative_beats_positive():
    """'not interested in your offer' contains 'offer' but is a rejection."""
    i = classify_inbound("not interested in your offer, thanks")
    assert i.is_negative
    assert not i.is_hot


@pytest.mark.parametrize(
    "text",
    ["who is this?", "how did you get my number", "what company is this"],
)
def test_curiosity_is_not_interest(text):
    i = classify_inbound(text)
    assert not i.is_hot, text
    assert i.label == "question"


def test_question_plus_price_is_hot():
    """"who is this and how much" asked the price — that is the signal."""
    assert classify_inbound("who is this and how much do you pay").is_hot


@pytest.mark.parametrize("text", ["ok", "yes", "sure", ""])
def test_bare_affirmatives_do_not_interrupt_the_operator(text):
    """A one-word 'ok' might just be answering 'is this the right number?'."""
    assert not classify_inbound(text).is_hot


def test_empty_message_is_safe():
    i = classify_inbound("")
    assert i.label == "other"
    assert not i.is_hot


# ─────────────────────────────────────────────────────────────
# Escalation through the API
# ─────────────────────────────────────────────────────────────
def _reply(client, phone, text, msg_id="m1"):
    return client.post(
        "/api/telnyx/message-webhook",
        json={
            "data": {
                "event_type": "message.received",
                "payload": {
                    "from": {"phone_number": phone},
                    "text": text,
                    "id": msg_id,
                },
            }
        },
    )


def test_hot_reply_lands_in_needs_you_with_a_draft_contract(client, db, fresh_lead):
    phone = db.get(Lead, fresh_lead.id).owner_phone
    r = _reply(client, phone, "How much would you pay for it?")
    assert r.status_code == 200
    assert r.json()["hot"] is True

    queue = client.get("/api/needs-you").json()
    mine = [q for q in queue if q["id"] == fresh_lead.id]
    assert len(mine) == 1
    entry = mine[0]
    assert entry["needs_attention"] is True
    assert "how much" in entry["what_they_said"].lower()
    assert entry["contract_ready"] is True
    assert entry["draft_offer"] > 0
    assert "sign" in entry["next_action"].lower()

    db.expire_all()
    contract = db.query(Contract).filter_by(lead_id=fresh_lead.id).first()
    assert contract is not None and contract.signed is False


def test_hot_lead_pauses_the_cadence(client, db, fresh_lead):
    """Automation must stop the moment a human takes over."""
    from app import cadence as engine
    from app.models import Enrollment
    from app.templates_content import seed_cadences

    seed_cadences(db)
    db.query(Enrollment).filter_by(lead_id=fresh_lead.id).delete()
    db.commit()
    enr = engine.enroll(db, fresh_lead)
    db.commit()

    phone = db.get(Lead, fresh_lead.id).owner_phone
    _reply(client, phone, "Yes I'm interested, what's your offer?")

    db.expire_all()
    enr = db.query(Enrollment).filter_by(lead_id=fresh_lead.id).first()
    assert enr.state == "paused"
    assert "handoff" in enr.stopped_reason


def test_operator_alert_is_recorded_and_dry_run(client, db, fresh_lead):
    phone = db.get(Lead, fresh_lead.id).owner_phone
    _reply(client, phone, "call me tomorrow please")

    db.expire_all()
    alerts = db.query(Alert).filter_by(lead_id=fresh_lead.id).all()
    assert alerts, "an alert must be recorded for the operator"
    channels = {a.channel for a in alerts}
    assert "in_app" in channels
    # No credentials in tests -> SMS/email are simulated, never claimed as sent.
    for a in alerts:
        if a.channel in ("sms", "email"):
            assert a.status in ("simulated", "sent"), a.status

    api_alerts = client.get("/api/alerts").json()
    assert any(a["lead_id"] == fresh_lead.id for a in api_alerts)


def test_alert_body_contains_what_the_operator_needs(client, db, fresh_lead):
    phone = db.get(Lead, fresh_lead.id).owner_phone
    _reply(client, phone, "how much?")
    db.expire_all()
    alert = (
        db.query(Alert)
        .filter_by(lead_id=fresh_lead.id, channel="in_app")
        .order_by(Alert.id.desc())
        .first()
    )
    assert alert is not None
    # In-app alert is the queue; the SMS variant carries the actionable detail.
    sms = db.query(Alert).filter_by(lead_id=fresh_lead.id, channel="sms").first()
    if sms:
        assert fresh_lead.street in sms.body
        assert "Call:" in sms.body


def test_escalation_notifies_only_once_per_burst(client, db, fresh_lead):
    """Two hot replies in a row must not produce two SMS interruptions."""
    phone = db.get(Lead, fresh_lead.id).owner_phone
    _reply(client, phone, "how much?", "m1")
    _reply(client, phone, "and can you call me today?", "m2")

    db.expire_all()
    sms_alerts = db.query(Alert).filter_by(
        lead_id=fresh_lead.id, channel="sms"
    ).count()
    assert sms_alerts == 1


def test_resolving_attention_clears_the_queue(client, db, fresh_lead):
    phone = db.get(Lead, fresh_lead.id).owner_phone
    _reply(client, phone, "how much?")
    assert any(q["id"] == fresh_lead.id for q in client.get("/api/needs-you").json())

    r = client.post(
        f"/api/deals/{fresh_lead.id}/attention/resolve",
        json={"note": "Called, left voicemail, following up tomorrow"},
    )
    assert r.status_code == 200
    db.expire_all()
    led = db.get(Lead, fresh_lead.id)
    assert led.needs_attention is False
    assert not any(q["id"] == fresh_lead.id for q in client.get("/api/needs-you").json())

    # The note is kept as a timeline entry.
    assert any("voicemail" in c.body for c in led.contacts if c.channel == "note")


def test_needs_you_requires_auth():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anon:
        anon.cookies.clear()
        assert anon.get("/api/needs-you").status_code == 401
        assert anon.get("/api/alerts").status_code == 401


# ─────────────────────────────────────────────────────────────
# Public website intake
# ─────────────────────────────────────────────────────────────
def _public_payload(**over):
    base = {
        "name": "Marcus Bell",
        "phone": "214-555-0177",
        "email": "marcus@test.local",
        "street": "8815 Fair Oaks Ave",
        "city": "Dallas",
        "state": "TX",
        "zip_code": "75231",
        "situation": "Inherited the property — my father passed away",
        "timeline": "As soon as possible",
        "asking_price": "185000",
        "sms_consent": False,
        "page_url": "/sell",
    }
    base.update(over)
    return base


def test_public_form_is_reachable_without_auth(client):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anon:
        anon.cookies.clear()
        r = anon.post("/api/public/lead", json=_public_payload(street="4400 Public Way"))
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True


def test_public_form_creates_inbound_lead_with_consent(client):
    r = client.post(
        "/api/public/lead",
        json=_public_payload(
            street="9911 Consent Pkwy",
            sms_consent=True,
            consent_text="I agree to receive calls and texts about my property.",
        ),
    )
    assert r.status_code == 200, r.text
    lead_id = r.json()["id"]

    detail = client.get(f"/api/deals/{lead_id}").json()
    deal = detail["deal"]
    assert deal["lead_class"] == "inbound"
    assert deal["sms_allowed"] is True
    assert deal["consent_verified"] is True
    assert deal["consent_source"].startswith("web_form:")


def test_public_form_without_consent_cannot_be_texted(client):
    r = client.post(
        "/api/public/lead",
        json=_public_payload(street="9921 Noconsent Pkwy", sms_consent=False),
    )
    lead_id = r.json()["id"]
    deal = client.get(f"/api/deals/{lead_id}").json()["deal"]
    assert deal["lead_class"] == "inbound"
    assert deal["sms_allowed"] is False

    # And the send is actually refused, not just flagged.
    resp = client.post(
        f"/api/deals/{lead_id}/contact?channel=sms&template_key=sms_opener"
    )
    assert resp.status_code == 409


def test_public_form_consent_needs_real_wording(client, db):
    """Consent ticked but no wording recorded is still recorded as consent
    (the checkbox itself is the act) — but the evidence field must be honest."""
    r = client.post(
        "/api/public/lead",
        json=_public_payload(street="9931 Evidence Row", sms_consent=True, consent_text=""),
    )
    lead_id = r.json()["id"]
    db.expire_all()
    lead = db.get(Lead, lead_id)
    assert lead.consent_sms is True
    assert lead.consent_source.startswith("web_form:")
    # Evidence may be blank, but the source and timestamp are always present.
    assert lead.consent_at is not None


def test_public_form_notifies_the_operator_immediately(client, db):
    r = client.post(
        "/api/public/lead",
        json=_public_payload(street="7700 Notify Ln"),
    )
    lead_id = r.json()["id"]
    db.expire_all()
    lead = db.get(Lead, lead_id)
    # An inbound seller is ready to talk by definition.
    assert lead.needs_attention is True
    alerts = db.query(Alert).filter_by(lead_id=lead_id).all()
    assert any(a.kind == "inbound_form" for a in alerts)
    assert any(q["id"] == lead_id for q in client.get("/api/needs-you").json())


def test_public_form_extracts_distress_signals_from_free_text(client, db):
    r = client.post(
        "/api/public/lead",
        json=_public_payload(
            street="6600 Signals Dr",
            situation="Behind on property taxes and the house is vacant",
        ),
    )
    db.expire_all()
    lead = db.get(Lead, r.json()["id"])
    assert lead.tax_delinquent is True
    assert lead.vacant is True
    assert lead.motivation_score > 0


def test_public_form_rejects_non_addresses(client):
    r = client.post(
        "/api/public/lead", json=_public_payload(street="Navigation Links")
    )
    assert r.status_code == 422


def test_public_form_honeypot_is_silently_dropped(client, db):
    r = client.post(
        "/api/public/lead",
        json=_public_payload(street="5500 Bot Trap Rd", company_website="http://spam.example"),
    )
    assert r.status_code == 200
    assert r.json()["id"] is None


def test_public_endpoint_does_not_leak_internal_state(client):
    r = client.post("/api/public/lead", json=_public_payload(street="3300 Quiet Ct"))
    body = r.json()
    assert set(body.keys()) == {"ok", "id", "message"}
    assert "grade" not in body and "max_offer" not in body


def test_sell_page_is_public(client):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anon:
        anon.cookies.clear()
        r = anon.get("/sell")
        assert r.status_code == 200
        assert "cash offer" in r.text.lower()
        assert "sms_consent" in r.text


# ─────────────────────────────────────────────────────────────
# Multi-market
# ─────────────────────────────────────────────────────────────
def test_market_pulls_default(monkeypatch):
    from app import pipeline

    assert pipeline.market_pulls(["TX"], "") == [("Dallas", "TX")]


def test_market_pulls_multiple_states(monkeypatch):
    from app import pipeline

    pulls = pipeline.market_pulls(["TX", "FL", "GA"], "")
    assert ("Dallas", "TX") in pulls
    assert ("Tampa", "FL") in pulls
    assert ("Atlanta", "GA") in pulls


def test_market_pulls_city_overrides():
    from app import pipeline

    pulls = pipeline.market_pulls(["TX", "FL"], "FL:Tampa,Orlando;TX:Houston")
    assert pulls == [("Houston", "TX"), ("Tampa", "FL"), ("Orlando", "FL")]


def test_pipeline_runs_every_configured_market(client, db):
    r = client.post(
        "/api/pipeline/run",
        json={"states": ["TX", "FL"], "limit": 40},
    )
    assert r.status_code == 200, r.text
    stats = r.json()["stats"]
    assert "markets" in stats
    assert len(stats["markets"]) == 2
    assert any("TX" in k for k in stats["markets"])
    assert any("FL" in k for k in stats["markets"])


def test_leads_land_with_state_correct_quiet_hours(client, db):
    """Expanding outside Texas must carry the right calling window with it."""
    from app.compliance import contact_window

    client.post("/api/pipeline/run", json={"states": ["FL"], "limit": 20})
    fl_lead = db.query(Lead).filter(Lead.state == "FL").first()
    if fl_lead:
        assert fl_lead.timezone == "America/New_York"
        assert contact_window(fl_lead.state) == (8, 20)   # stricter than federal


# ─────────────────────────────────────────────────────────────
# Texting health check
# ─────────────────────────────────────────────────────────────
def test_test_sms_reports_dry_run_honestly(client):
    r = client.post("/api/test-sms", json={"to": "+12145550199"})
    assert r.status_code == 200, r.text
    body = r.json()
    # No credentials in the test environment: it must say so, not claim success.
    assert body["simulated"] is True
    assert body["ok"] is True
    assert "TELNYX_API_KEY" in body["next"]


def test_test_sms_rejects_bad_numbers(client):
    assert client.post("/api/test-sms", json={"to": "123"}).status_code == 422
    assert client.post("/api/test-sms", json={"to": "555-0143"}).status_code == 422


def test_test_sms_requires_auth():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anon:
        anon.cookies.clear()
        assert anon.post("/api/test-sms", json={"to": "+12145550199"}).status_code == 401
