"""PropRaven provider tests.

The live API is never called. HTTP is stubbed at the `httpx.Client` boundary so
these tests exercise the real code path — pagination, field mapping, quota
tracking and error handling — without spending a single billed lookup.
"""
from __future__ import annotations

import pytest

from app.providers.propraven import (
    PropRavenError,
    PropRavenProvider,
    map_propraven_row,
)


# ─────────────────────────────────────────────────────────────
# Row mapping
# ─────────────────────────────────────────────────────────────
FULL_ROW = {
    "parcel_id": "48:113:11906402",
    "situs_address": "4501 W Mockingbird Ln",
    "situs_city": "Dallas",
    "situs_state": "TX",
    "situs_zip": "75209",
    "county_name": "Dallas",
    "property_type": "SFR",
    "building_area_sqft": 1850,
    "bedrooms": 3,
    "bathrooms": 2,
    "year_built": 1962,
    "lot_size_sqft": 7200,
    "owner_name": "Dana Whitfield",
    "owner_mailing_address": "99 Other Rd, Austin, TX 78701",
    "total_assessed_value": 268000.0,
    "delinquent_tax": True,
    "delinquent_tax_amount": 6400.0,
    "foreclosure": False,
    "vacant": True,
    "absentee": True,
    "sale_date": "2003-05-14",
    "equity_percent": 71.0,
}


def test_maps_a_full_row():
    out = map_propraven_row(FULL_ROW)
    assert out["street"] == "4501 W Mockingbird Ln"
    assert out["city"] == "Dallas"
    assert out["state"] == "TX"
    assert out["zip_code"] == "75209"
    assert out["owner_name"] == "Dana Whitfield"
    assert out["sqft"] == 1850
    assert out["year_built"] == 1962
    assert out["tax_assessed_value"] == 268000.0
    assert out["tax_delinquent"] is True
    assert out["tax_delinquent_amount"] == 6400.0
    assert out["vacant"] is True
    assert out["equity_pct"] == 71.0
    assert out["source"] == "propraven"
    assert out["source_ref"] == "48:113:11906402"


def test_every_propraven_row_is_classed_as_cold_list():
    """A purchased feed can never be inbound, so it can never be texted."""
    out = map_propraven_row(FULL_ROW)
    assert out["lead_class"] == "list"
    assert out["owner_phone"] is None      # skip trace fills this later


def test_years_owned_is_derived_from_sale_date():
    out = map_propraven_row(FULL_ROW)
    assert out["years_owned"] is not None
    assert out["years_owned"] > 20          # 2003 -> 20+ years


def test_maps_nested_address_and_owner_objects():
    row = {
        "parcel_id": "12:057:123",
        "address": {"line1": "1200 Bayshore Blvd", "city": "Tampa",
                    "state": "FL", "zip": "33606"},
        "owner": {"full_name": "Marcus Bell"},
        "mailing_address": {"line1": "1 Remote Way", "city": "Denver",
                            "state": "CO", "zip": "80202"},
        "total_assessed_value": "412000.00",       # pre-1.2.0 string numbers
    }
    out = map_propraven_row(row, city="Tampa", state="FL")
    assert out["street"] == "1200 Bayshore Blvd"
    assert out["city"] == "Tampa"
    assert out["state"] == "FL"
    assert out["owner_name"] == "Marcus Bell"
    assert "1 Remote Way" in out["owner_mailing_address"]
    assert out["tax_assessed_value"] == 412000.0


def test_sparse_row_does_not_crash():
    out = map_propraven_row({"parcel_id": "1:2:3", "situs_address": "9 Bare St"})
    assert out["street"] == "9 Bare St"
    assert out["sqft"] is None
    assert out["tax_assessed_value"] is None


def test_non_dict_row_returns_none():
    assert map_propraven_row("nonsense") is None
    assert map_propraven_row(None) is None


def test_missing_street_still_returns_row_for_validation_to_reject():
    """The provider does not validate; app.validation is the gate."""
    out = map_propraven_row({"parcel_id": "1:2:3", "owner_name": "X"})
    assert out is not None
    assert not out.get("street")


# ─────────────────────────────────────────────────────────────
# County resolution
# ─────────────────────────────────────────────────────────────
def test_county_fips_built_in():
    p = PropRavenProvider(api_key="pz_test")
    assert p.county_fips("TX", "Dallas") == "48113"
    assert p.county_fips("FL", "Tampa") == "12057"
    assert p.county_fips("TX", "Nowhere") is None


def test_county_fips_env_override(monkeypatch):
    from app.providers import propraven as mod

    monkeypatch.setattr(
        mod.settings, "propraven_county_fips", "TX:Nowhere=48999;FL:Miami=12086"
    )
    p = PropRavenProvider(api_key="pz_test")
    assert p.county_fips("TX", "Nowhere") == "48999"


# ─────────────────────────────────────────────────────────────
# HTTP behaviour (stubbed)
# ─────────────────────────────────────────────────────────────
class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = str(payload)

    def json(self):
        return self._payload


class PagedFake:
    """Replays a fixed dataset, honouring limit/offset like the real API."""

    def __init__(self, rows, headers=None):
        self.rows = list(rows)
        self.headers = headers or {}
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None, headers=None):
        params = params or {}
        self.calls.append({"url": url, "params": params, "headers": headers or {}})
        limit = int(params.get("limit", 100))
        offset = int(params.get("offset", 0))
        page = self.rows[offset: offset + limit]
        return FakeResponse(200, {"data": page, "total": len(self.rows)},
                            headers=self.headers)


class FakeClient:
    """Records requests and replays queued responses verbatim."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None, headers=None):
        self.calls.append({"url": url, "params": params or {}, "headers": headers or {}})
        if not self.responses:
            return FakeResponse(200, {"data": [], "total": 0})
        return self.responses.pop(0)


def _patch(monkeypatch, client):
    import httpx

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: client)


def test_fetch_pages_until_limit(monkeypatch):
    """Three pages of 2/2/1 to satisfy a limit of 5."""
    from app.providers import propraven as mod

    monkeypatch.setattr(mod.settings, "propraven_page_size", 2)
    client = PagedFake([_row(i) for i in range(5)])
    _patch(monkeypatch, client)

    p = PropRavenProvider(api_key="pz_test")
    rows = p.fetch(city="Dallas", state="TX", limit=5)

    assert len(rows) == 5
    assert all(r["source"] == "propraven" for r in rows)
    assert len(client.calls) == 3
    assert [c["params"]["offset"] for c in client.calls] == [0, 2, 4]
    assert [c["params"]["limit"] for c in client.calls] == [2, 2, 1]
    assert client.calls[0]["params"]["county_fips"] == "48113"


def test_fetch_does_not_over_fetch(monkeypatch):
    """Asking for 3 must not pull the whole county — every row is billed."""
    from app.providers import propraven as mod

    monkeypatch.setattr(mod.settings, "propraven_page_size", 2)
    client = PagedFake([_row(i) for i in range(50)])
    _patch(monkeypatch, client)

    rows = PropRavenProvider(api_key="pz_test").fetch(
        city="Dallas", state="TX", limit=3
    )
    assert len(rows) == 3
    assert sum(c["params"]["limit"] for c in client.calls) == 3


def test_fetch_stops_on_short_page(monkeypatch):
    client = FakeClient([FakeResponse(200, {"data": [_row(0)], "total": 1})])
    _patch(monkeypatch, client)
    rows = PropRavenProvider(api_key="pz_test").fetch(
        city="Dallas", state="TX", limit=50
    )
    assert len(rows) == 1
    assert len(client.calls) == 1


def test_fetch_sends_bearer_auth(monkeypatch):
    client = FakeClient([FakeResponse(200, {"data": [], "total": 0})])
    _patch(monkeypatch, client)
    PropRavenProvider(api_key="pz_secret").fetch(city="Dallas", state="TX", limit=5)
    assert client.calls[0]["headers"]["Authorization"] == "Bearer pz_secret"


def test_fetch_requires_county_fips():
    p = PropRavenProvider(api_key="pz_test")
    with pytest.raises(PropRavenError) as e:
        p.fetch(city="Nowhere", state="TX", limit=5)
    assert "county FIPS" in str(e.value)


def test_missing_key_raises_actionable_error():
    p = PropRavenProvider(api_key="")
    with pytest.raises(PropRavenError) as e:
        p.fetch(city="Dallas", state="TX", limit=5)
    assert "PROPRAVEN_API_KEY" in str(e.value)


@pytest.mark.parametrize("status,needle", [
    (401, "rejected the API key"),
    (402, "paid plan"),
    (403, "inactive"),
    (429, "rate limit"),
])
def test_error_responses_become_actionable_messages(monkeypatch, status, needle):
    client = FakeClient([FakeResponse(status, {"detail": "nope", "code": "x"})])
    _patch(monkeypatch, client)
    p = PropRavenProvider(api_key="pz_test")
    with pytest.raises(PropRavenError) as e:
        p.usage()
    assert needle in str(e.value)


def test_quota_is_read_from_headers(monkeypatch):
    from app.providers import propraven as mod

    client = PagedFake([], headers={"x-ratelimit-remaining": "12"})
    _patch(monkeypatch, client)
    PropRavenProvider(api_key="pz_test").fetch(city="Dallas", state="TX", limit=1)
    assert mod.last_remaining() == 12
    assert mod._quota_exhausted() is True    # 12 <= 25 -> stop pulling


def test_usage_parses_quota(monkeypatch):
    client = FakeClient(
        [FakeResponse(200, {"data": {"used": 340, "limit": 1000, "remaining": 660,
                                     "period": "2026-10"}})]
    )
    _patch(monkeypatch, client)
    q = PropRavenProvider(api_key="pz_test").usage()
    assert (q.used, q.limit, q.remaining) == (340, 1000, 660)
    assert q.low is False


def test_usage_flags_low_quota(monkeypatch):
    client = FakeClient([FakeResponse(200, {"data": {"used": 990, "limit": 1000,
                                                     "remaining": 10}})])
    _patch(monkeypatch, client)
    assert PropRavenProvider(api_key="pz_test").usage().low is True


def _row(i: int) -> dict:
    return {
        "parcel_id": f"48:113:{i}",
        "situs_address": f"{100 + i} Testbed Rd",
        "situs_city": "Dallas",
        "situs_state": "TX",
        "situs_zip": "75209",
        "owner_name": f"Owner {i}",
        "total_assessed_value": 200000 + i,
    }


# ─────────────────────────────────────────────────────────────
# Through the API
# ─────────────────────────────────────────────────────────────
def test_provider_status_endpoint(client, monkeypatch):
    from app.providers import propraven as mod

    monkeypatch.setattr(mod.settings, "lead_provider", "propraven")
    monkeypatch.setattr(mod.settings, "propraven_api_key", "")

    r = client.get("/api/provider/status")
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "propraven"
    # No key -> loudly unconfigured rather than a silent empty pipeline.
    assert body["configured"] is False
    assert "PROPRAVEN_API_KEY" in body["coverage_note"]


def test_provider_status_requires_auth():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anon:
        anon.cookies.clear()
        assert anon.get("/api/provider/status").status_code == 401


def test_propraven_leads_flow_through_validation_and_cannot_be_texted(client, db, monkeypatch):
    """End-to-end: a mapped PropRaven row becomes a real lead, is analysed,
    and is call/email only because a purchased feed is never inbound."""
    from app.pipeline import analyze_lead, ingest_records
    from app.models import Lead

    mapped = map_propraven_row(
        {
            "parcel_id": "48:113:90001",
            "situs_address": "7788 Provrow Ave",
            "situs_city": "Dallas",
            "situs_state": "TX",
            "situs_zip": "75231",
            "owner_name": "Elena Vasquez",
            "total_assessed_value": 245000,
            "building_area_sqft": 1700,
            "year_built": 1971,
            "delinquent_tax": True,
            "absentee": True,
        }
    )
    stats = ingest_records(db, [mapped], source="propraven")
    db.commit()
    assert stats.accepted == 1

    lead = db.query(Lead).filter(Lead.source_ref == "48:113:90001").first()
    assert lead is not None
    assert lead.lead_class == "list"
    analyze_lead(db, lead)
    db.commit()

    deal = client.get(f"/api/deals/{lead.id}").json()["deal"]
    assert deal["sms_allowed"] is False
    assert set(deal["allowed_channels"]) == {"call", "email"}
    assert deal["arv"] > 0
