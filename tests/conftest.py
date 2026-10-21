"""Test fixtures. Environment must be set before the app package is imported,
because app.config builds its Settings singleton at import time.
"""
from __future__ import annotations

import itertools
import os
import tempfile
import uuid

# Monotonic counter so every generated test address is unique by construction —
# the leads table has a unique index on address_key, and a random house number
# will eventually collide and produce a confusing IntegrityError.
_LEAD_SEQ = itertools.count(1)

_TMP = tempfile.mkdtemp(prefix="wai-test-")
os.environ.update(
    {
        "APP_ENV": "test",
        "SECRET_KEY": "test-secret-key-not-for-production",
        "DATABASE_URL": f"sqlite:///{_TMP}/test.db",
        "ADMIN_EMAIL": "owner@test.local",
        "ADMIN_PASSWORD": "test-password-123",
        "ADMIN_NAME": "Test Owner",
        "AGENT_NAME": "Sam Reeves",
        "AGENT_COMPANY": "Reeves Home Offers",
        "AGENT_PHONE": "+12145550188",
        "AGENT_EMAIL": "sam@reeves.test",
        "LEAD_PROVIDER": "demo",
        "DNC_PROVIDER": "off",
        # Disable real-time quiet hours so cadence tests can run at any hour.
        "ENFORCE_QUIET_HOURS": "false",
        "MAX_SMS_PER_LEAD_PER_DAY": "1",
        "MAX_CALL_ATTEMPTS_PER_LEAD_PER_WEEK": "3",
        "TELNYX_API_KEY": "",
        "TELNYX_FROM_NUMBER": "",
        "EMAIL_PROVIDER": "",
        "RESEND_API_KEY": "",
    }
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="session")
def client():
    from app.main import app

    with TestClient(app) as c:
        r = c.post(
            "/api/login",
            json={"email": "owner@test.local", "password": "test-password-123"},
        )
        assert r.status_code == 200, r.text
        yield c


@pytest.fixture()
def db():
    from app.db import SessionLocal, init_db
    from app.templates_content import seed_cadences, seed_templates

    init_db()  # tests that never touch the HTTP client still need the schema
    session = SessionLocal()
    # Mirror what the boot sequence does, so a session-only test has message
    # copy and cadences to work with.
    try:
        seed_templates(session)
        seed_cadences(session)
        session.commit()
    except Exception:
        session.rollback()
    try:
        yield session
    finally:
        session.close()


def _make_lead(db, *, lead_class="inbound", consent=True, seq_prefix="Testbed"):
    """Build a valid, analysed lead. Defaults to an inbound, consented lead so
    that SMS-path tests exercise the *allowed* branch; pass
    lead_class="list" for the restricted branch."""
    from app.models import Lead
    from app.pipeline import analyze_lead
    from app.validation import validate_lead

    seq = next(_LEAD_SEQ)
    raw = {
        "street": f"{1000 + seq} {seq_prefix} Trail",
            "city": "Dallas",
            "state": "TX",
            "zip_code": "75209",
            "sqft": 1800,
            "year_built": 1962,
        "owner_name": "Dana Whitfield",
        "owner_phone": "+1214555" + str(uuid.uuid4().int)[:4],
        "owner_email": f"owner{seq}@test.local",
        "tax_assessed_value": 240000,
        "tax_delinquent": True,
        "tax_delinquent_amount": 6400,
        "years_owned": 22,
        "equity_pct": 71,
        "source": "test",
        "lead_class": lead_class,
    }
    if consent and lead_class == "inbound":
        raw["consent_sms"] = True
        raw["consent_source"] = "web_form:/test-landing"
        raw["consent_at"] = "2025-10-01T12:00:00"
    clean = validate_lead(raw)
    lead = Lead(**clean)
    db.add(lead)
    db.flush()
    analyze_lead(db, lead)
    db.commit()
    return lead


@pytest.fixture()
def fresh_lead(db):
    """Inbound, consented, workable lead — the only class that may be texted."""
    return _make_lead(db, lead_class="inbound", consent=True)


@pytest.fixture()
def list_lead(db):
    """Cold list lead: calls and mail only, never SMS."""
    return _make_lead(db, lead_class="list", consent=False, seq_prefix="Coldlist")


@pytest.fixture()
def probate_lead(db):
    """Probate lead: letter first, then calls. Never SMS."""
    return _make_lead(db, lead_class="probate", consent=False, seq_prefix="Estate")


@pytest.fixture()
def inbound_no_consent_lead(db):
    """Inbound but with no consent provenance -> still cannot be texted."""
    return _make_lead(
        db, lead_class="inbound", consent=False, seq_prefix="Noconsent"
    )
