"""API surface tests: auth, evidence guards, cadence execution, opt-out flow."""
from __future__ import annotations

import uuid

import pytest


# ── Authentication ─────────────────────────────────────────────────────
def test_health_is_public(client):
    r = client.get("/health")
    assert r.status_code == 200


PUBLIC_PATHS = ["/health"]


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/dashboard"),
        ("get", "/api/deals"),
        ("get", "/api/activity"),
        ("get", "/api/templates"),
        ("get", "/api/cadence"),
        ("get", "/api/settings"),
        ("get", "/api/pipeline/status"),
        ("get", "/api/compliance/suppressions"),
        ("get", "/api/compliance/consent-log"),
        ("get", "/api/leads/rejected"),
        ("post", "/api/pipeline/run"),
        ("post", "/api/pipeline/send-due"),
        ("post", "/api/outbound-call"),
        ("post", "/api/leads"),
    ],
)
def test_every_endpoint_requires_auth(method, path):
    """v1 exposed all of these anonymously. Regression guard."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anon:
        anon.cookies.clear()
        r = getattr(anon, method)(path)
        assert r.status_code == 401, f"{path} was reachable without auth"


def test_bad_password_rejected():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anon:
        r = anon.post(
            "/api/login",
            json={"email": "owner@test.local", "password": "wrong-password"},
        )
        assert r.status_code == 401


def test_unknown_user_rejected():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anon:
        r = anon.post(
            "/api/login", json={"email": "nobody@test.local", "password": "whatever12"}
        )
        assert r.status_code == 401


def test_me_returns_identity(client):
    r = client.get("/api/me")
    assert r.status_code == 200
    assert r.json()["email"] == "owner@test.local"


# ── Dashboard honesty ──────────────────────────────────────────────────
def test_dashboard_reports_capabilities(client):
    caps = client.get("/api/dashboard").json()["capabilities"]
    # No credentials in the test environment -> everything must report dry-run
    assert caps["sms"]["live"] is False
    assert caps["voice"]["live"] is False
    assert caps["email"]["live"] is False


def test_dashboard_funnel_keys_present(client):
    funnel = client.get("/api/dashboard").json()["pipeline"]
    for key in ["leads", "analyzed", "contacted", "signed", "rejected", "workable"]:
        assert key in funnel


# ── Lead creation + validation through the API ─────────────────────────
def test_garbage_lead_rejected_by_api(client):
    r = client.post(
        "/api/leads",
        json={"street": "Navigation Links", "city": "Dallas", "state": "TX"},
    )
    assert r.status_code == 422
    assert "Rejected" in r.json()["detail"]


def test_valid_lead_accepted_and_analysed(client):
    n = uuid.uuid4().int % 8000
    r = client.post(
        "/api/leads",
        json={
            "street": f"{1200 + n} Maple Ridge Dr",
            "city": "Dallas",
            "state": "TX",
            "zip_code": "75209",
            "owner_name": "Curtis Nakamura",
            "owner_phone": f"+1214555{n:04d}",
            "sqft": 1900,
            "year_built": 1968,
            "tax_assessed_value": 260000,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["arv"] > 0
    assert body["max_offer"] > 0
    assert body["grade"] in ("A", "B", "C")


def test_duplicate_address_conflicts(client):
    n = uuid.uuid4().int % 8000
    payload = {
        "street": f"{3000 + n} Cedar Post Ln",
        "city": "Dallas",
        "state": "TX",
        "owner_phone": f"+1214555{n:04d}",
    }
    assert client.post("/api/leads", json=payload).status_code == 200
    assert client.post("/api/leads", json=payload).status_code == 409


# ── Evidence guards: the v1 "signed contract on a fake house" bug ─────
def test_cannot_advance_without_arv(client, db, fresh_lead):
    from app.models import Lead

    lead = db.get(Lead, fresh_lead.id)
    lead.arv = 0.0
    lead.max_offer = 0.0
    db.commit()
    r = client.patch(f"/api/deals/{lead.id}", json={"status": "under_contract"})
    assert r.status_code == 409
    assert "ARV" in r.json()["detail"]


def test_cannot_advance_without_contact_info(client, db, fresh_lead):
    from app.models import Lead

    lead = db.get(Lead, fresh_lead.id)
    lead.owner_phone = None
    lead.owner_email = None
    db.commit()
    r = client.patch(f"/api/deals/{lead.id}", json={"status": "under_contract"})
    assert r.status_code == 409


def test_contract_cannot_be_generated_without_arv(client, db, fresh_lead):
    from app.models import Lead

    lead = db.get(Lead, fresh_lead.id)
    lead.arv = 0
    db.commit()
    r = client.post(f"/api/deals/{lead.id}/generate-contract")
    assert r.status_code == 409


def test_send_to_jv_requires_a_signed_contract(client, fresh_lead):
    r = client.post(f"/api/deals/{fresh_lead.id}/send-to-jv")
    assert r.status_code == 409
    assert "signed" in r.json()["detail"].lower()


def test_contract_lifecycle(client, fresh_lead):
    gen = client.post(f"/api/deals/{fresh_lead.id}/generate-contract")
    assert gen.status_code == 200, gen.text
    assert "$" in gen.json()["body"]
    assert "AS-IS" in gen.json()["body"]

    sign = client.post(
        f"/api/deals/{fresh_lead.id}/sign",
        json={"signed_by": "Dana Whitfield", "signature_data": "data:image/png;base64,AAA"},
    )
    assert sign.status_code == 200

    jv = client.post(f"/api/deals/{fresh_lead.id}/send-to-jv")
    assert jv.status_code == 200
    assert jv.json()["deal"]["sent_to_jv"] is True


def test_cannot_mark_signed_without_a_contract(client, fresh_lead):
    r = client.patch(f"/api/deals/{fresh_lead.id}", json={"status": "signed"})
    assert r.status_code == 409
    assert "contract" in r.json()["detail"].lower()


def test_invalid_status_rejected(client, fresh_lead):
    r = client.patch(f"/api/deals/{fresh_lead.id}", json={"status": "banana"})
    assert r.status_code == 422


# ── Sending ────────────────────────────────────────────────────────────
def test_manual_sms_is_simulated_without_credentials(client, fresh_lead):
    r = client.post(
        f"/api/deals/{fresh_lead.id}/contact?channel=sms&template_key=sms_opener"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["simulated"] is True          # no Telnyx key -> dry run
    assert body["status"] == "simulated"


def test_simulated_send_is_recorded_in_activity(client, fresh_lead):
    client.post(f"/api/deals/{fresh_lead.id}/contact?channel=sms&template_key=sms_opener")
    feed = client.get("/api/activity").json()
    assert any("simulated" in item["text"].lower() for item in feed)


def test_template_renders_lead_specific_values(client, db, fresh_lead):
    from app.models import Contact

    client.post(f"/api/deals/{fresh_lead.id}/contact?channel=sms&template_key=sms_opener")
    c = (
        db.query(Contact)
        .filter_by(lead_id=fresh_lead.id, channel="sms")
        .order_by(Contact.id.desc())
        .first()
    )
    assert "Dana" in c.body                       # first name rendered
    assert fresh_lead.street in c.body            # address rendered
    assert "{" not in c.body                      # no leaked placeholders
    assert "Sam Reeves" in c.body                 # agent identity


def test_opted_out_lead_cannot_be_contacted(client, db, fresh_lead):
    from app.models import Lead

    lead = db.get(Lead, fresh_lead.id)
    lead.opted_out = True
    db.commit()
    r = client.post(
        f"/api/deals/{lead.id}/contact?channel=sms&template_key=sms_opener"
    )
    assert r.status_code == 409
    assert "compliance" in r.json()["detail"].lower()


def test_sms_daily_cap_enforced(client, db, fresh_lead):
    first = client.post(
        f"/api/deals/{fresh_lead.id}/contact?channel=sms&template_key=sms_opener"
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/deals/{fresh_lead.id}/contact?channel=sms&template_key=sms_followup_day1"
    )
    assert second.status_code == 409
    assert "cap" in second.json()["detail"].lower()


# ── Cadence engine ─────────────────────────────────────────────────────
def test_each_lead_class_has_its_own_cadence(client):
    data = client.get("/api/cadence").json()
    by_class = {c["lead_class"]: c for c in data["cadences"]}
    assert {"inbound", "list", "probate", "referral"} <= set(by_class)


def test_no_cadence_mix_is_invalid(client):
    """The whole point of the split: list and probate sequences must not
    contain an SMS step at all."""
    data = client.get("/api/cadence").json()
    by_class = {c["lead_class"]: c for c in data["cadences"]}
    assert "sms" not in by_class["list"]["channels"]
    assert "sms" not in by_class["probate"]["channels"]
    assert "sms" not in by_class["referral"]["channels"]
    assert "sms" in by_class["inbound"]["channels"]


def test_list_cadence_uses_calls_and_email(client):
    data = client.get("/api/cadence").json()
    by_class = {c["lead_class"]: c for c in data["cadences"]}
    assert set(by_class["list"]["channels"]) == {"call", "email"}
    assert set(by_class["probate"]["channels"]) == {"call", "email"}


def test_probate_sequence_opens_with_written_outreach(client):
    """Calling a grieving family before writing to them is how you lose the
    lead. The first probate touch must be written."""
    data = client.get("/api/cadence").json()
    probate = next(c for c in data["cadences"] if c["lead_class"] == "probate")
    assert probate["steps"][0]["channel"] == "email"


def test_cadence_steps_are_chronological(client):
    for c in client.get("/api/cadence").json()["cadences"]:
        delays = [s["delay_hours"] for s in c["steps"]]
        assert delays == sorted(delays), c["lead_class"]


def test_process_due_sends_first_step(client, db, fresh_lead):
    from app import cadence as engine
    from app.models import Cadence, Enrollment, Lead

    lead = db.get(Lead, fresh_lead.id)
    cadence = db.query(Cadence).filter_by(is_default=True).first()
    db.query(Enrollment).filter_by(lead_id=lead.id).delete()
    db.commit()

    enr = engine.enroll(db, lead, cadence)
    db.commit()
    assert enr is not None

    enr.next_run_at = None  # force it due now
    db.commit()

    from app.models import utcnow

    enr.next_run_at = utcnow() - __import__("datetime").timedelta(minutes=1)
    db.commit()

    stats = engine.process_due(db)
    assert stats["steps_run"] >= 1
    assert lead.status == "contacted"
    assert enr.step_index == 1


def test_cadence_pauses_when_lead_replies(client, db, fresh_lead):
    from app import cadence as engine
    from app.models import Cadence, Enrollment, Lead

    lead = db.get(Lead, fresh_lead.id)
    cadence = db.query(Cadence).filter_by(is_default=True).first()
    db.query(Enrollment).filter_by(lead_id=lead.id).delete()
    db.commit()
    enr = engine.enroll(db, lead, cadence)
    lead.status = "engaged"        # simulates an inbound reply
    # Force the enrollment to be due so the engine actually evaluates it.
    import datetime as _dt

    from app.models import utcnow as _now

    enr.next_run_at = _now() - _dt.timedelta(minutes=1)
    db.commit()

    engine.process_due(db)
    enr = db.query(Enrollment).filter_by(lead_id=lead.id).first()
    assert enr.state == "paused"
    assert "engaged" in enr.stopped_reason


# ── Inbound webhook / opt-out ──────────────────────────────────────────
def test_inbound_stop_opts_out_and_halts_cadence(client, db, fresh_lead):
    from app import cadence as engine
    from app.models import Cadence, Enrollment, Lead

    lead = db.get(Lead, fresh_lead.id)
    cadence = db.query(Cadence).filter_by(is_default=True).first()
    db.query(Enrollment).filter_by(lead_id=lead.id).delete()
    db.commit()
    engine.enroll(db, lead, cadence)
    db.commit()
    phone = lead.owner_phone

    r = client.post(
        "/api/telnyx/message-webhook",
        json={
            "data": {
                "event_type": "message.received",
                "payload": {"from": {"phone_number": phone}, "text": "STOP", "id": "x1"},
            }
        },
    )
    assert r.status_code == 200
    assert r.json()["intent"] == "stop"

    db.expire_all()
    lead = db.get(Lead, fresh_lead.id)
    enr = db.query(Enrollment).filter_by(lead_id=lead.id).first()
    assert lead.opted_out is True
    assert lead.status == "dead"
    assert enr.state == "stopped"


def test_price_question_escalates_to_a_human(client, db, fresh_lead):
    """"how much would you offer" is the strongest buying signal there is —
    it must land in the needs-you queue, not just mark the lead engaged."""
    from app.models import Lead

    phone = db.get(Lead, fresh_lead.id).owner_phone
    r = client.post(
        "/api/telnyx/message-webhook",
        json={
            "data": {
                "event_type": "message.received",
                "payload": {
                    "from": {"phone_number": phone},
                    "text": "how much would you offer?",
                    "id": "x2",
                },
            }
        },
    )
    assert r.status_code == 200
    assert r.json()["hot"] is True
    db.expire_all()
    lead = db.get(Lead, fresh_lead.id)
    assert lead.status == "qualified"
    assert lead.needs_attention is True
    assert "pay" in lead.attention_reason.lower() or "number" in lead.attention_reason.lower()


def test_vague_reply_does_not_escalate(client, db, fresh_lead):
    """"who is this" is a real reply but not interest — do not waste the
    operator's time with it."""
    from app.models import Lead

    phone = db.get(Lead, fresh_lead.id).owner_phone
    r = client.post(
        "/api/telnyx/message-webhook",
        json={
            "data": {
                "event_type": "message.received",
                "payload": {
                    "from": {"phone_number": phone},
                    "text": "who is this?",
                    "id": "x3",
                },
            }
        },
    )
    assert r.status_code == 200
    assert r.json()["hot"] is False
    db.expire_all()
    lead = db.get(Lead, fresh_lead.id)
    assert lead.needs_attention is False
    assert lead.status == "engaged"


def test_negative_reply_kills_the_cadence(client, db, fresh_lead):
    from app.models import Enrollment, Lead

    phone = db.get(Lead, fresh_lead.id).owner_phone
    r = client.post(
        "/api/telnyx/message-webhook",
        json={
            "data": {
                "event_type": "message.received",
                "payload": {
                    "from": {"phone_number": phone},
                    "text": "Not interested, already sold it",
                    "id": "x4",
                },
            }
        },
    )
    assert r.status_code == 200
    assert r.json()["hot"] is False
    db.expire_all()
    lead = db.get(Lead, fresh_lead.id)
    assert lead.status == "dead"
    enr = db.query(Enrollment).filter_by(lead_id=lead.id).first()
    assert enr is None or enr.state in ("stopped", "paused", "completed")


def test_webhook_ignores_unknown_numbers(client):
    r = client.post(
        "/api/telnyx/message-webhook",
        json={
            "data": {
                "event_type": "message.received",
                "payload": {"from": {"phone_number": "+19995550000"}, "text": "hi"},
            }
        },
    )
    assert r.status_code == 200
    assert r.json()["matched"] is False


# ── CSV import ─────────────────────────────────────────────────────────
CSV_WITH_JUNK = """Address,City,State,Zip,Owner,Phone
4501 W Mockingbird Ln,Dallas,TX,75209,Ada Byron,+12145550101
Navigation Links,Dallas,TX,,Annual Report,
About Dcad,Dallas,TX,,,
1234 Elm St,Houston,TX,77002,Luis Ortega,+17135550199
Nsewnenwsesw,Dallas,TX,,,
"""


def test_csv_import_filters_garbage_but_keeps_real_rows(client):
    r = client.post(
        "/api/leads/import",
        files={"file": ("export.csv", CSV_WITH_JUNK, "text/csv")},
    )
    assert r.status_code == 200, r.text
    stats = r.json()["stats"]
    assert stats["accepted"] >= 2
    assert stats["rejected"] >= 2
    assert stats["rejected"] > 0


def test_rejected_rows_are_inspectable(client):
    rows = client.get("/api/leads/rejected").json()
    assert isinstance(rows, list)
    if rows:
        assert "reason" in rows[0]
        assert "street" in rows[0]["raw"]


def test_templates_are_editable(client):
    original = next(
        t for t in client.get("/api/templates").json() if t["key"] == "sms_opener"
    )
    new_body = "Custom opener for {first_name} about {street}. Reply STOP to opt out."
    r = client.patch("/api/templates/sms_opener", json={"body": new_body})
    assert r.status_code == 200
    assert r.json()["body"] == new_body
    # restore
    client.patch("/api/templates/sms_opener", json={"body": original["body"]})


def test_unknown_template_is_404(client):
    assert client.patch("/api/templates/nope", json={"body": "x"}).status_code == 404
