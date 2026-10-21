"""PropRaven lead provider.

API facts (verified against propraven.com/developers, API v1.2.1):

  Base URL      https://propraven.com/api/v1
  Auth          Authorization: Bearer pz_...
  Parcel ID     state_fips:county_fips:parcel_id   e.g. 48:113:11906402
  List shape    {data, total, limit, offset}  -> page with offset += limit
  Errors        RFC 7807 problem+json with a stable `code`; 429 carries
                Retry-After; 402 means "paid resource, needs a plan upgrade"
  Quota         GET /api/v1/account/usage, plus X-RateLimit-Remaining headers

Endpoints used here:
  GET  /deals/absentee      absentee owners -- the classic wholesale list
  GET  /deals/high-equity   (optional, used as a second source when configured)
  GET  /account/usage       quota check, surfaced in the dashboard
  GET  /coverage            which counties actually have data

BILLING REALITY
---------------
Plans are metered in LOOKUPS, not leads. The free tier is 1,000/month, which is
~33/day -- enough to build and validate, not enough to run volume. The adapter
therefore refuses to over-fetch: it stops when the page returns short, when the
requested limit is reached, or when the remaining-quota header runs low.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from ..config import settings
from .leads import LeadProvider

log = logging.getLogger("wholesale_ai.propraven")

DEFAULT_BASE = "https://propraven.com/api/v1"

# County FIPS for the markets this system can pull out of the box. Extend with
# PROPRAVEN_COUNTY_FIPS, or just use /deals/absentee per county directly.
CITY_COUNTY_FIPS: dict[tuple[str, str], str] = {
    ("TX", "Dallas"): "48113",
    ("TX", "Houston"): "48201",
    ("TX", "Fort Worth"): "48439",
    ("TX", "Arlington"): "48439",
    ("TX", "Plano"): "48085",
    ("TX", "Garland"): "48113",
    ("TX", "Irving"): "48113",
    ("TX", "San Antonio"): "48029",
    ("TX", "Austin"): "48453",
    ("TX", "El Paso"): "48141",
    ("FL", "Tampa"): "12057",
    ("FL", "Orlando"): "12095",
    ("FL", "Jacksonville"): "12031",
    ("FL", "Miami"): "12086",
    ("GA", "Atlanta"): "13121",
    ("AZ", "Phoenix"): "04013",
    ("TN", "Nashville"): "47037",
    ("TN", "Memphis"): "47157",
    ("NC", "Charlotte"): "37119",
    ("OH", "Columbus"): "39049",
    ("IN", "Indianapolis"): "36097",
    ("MO", "Kansas City"): "29095",
    ("LA", "Baton Rouge"): "22033",
    ("OK", "Oklahoma City"): "40109",
    ("AL", "Birmingham"): "01073",
    ("MS", "Jackson"): "28049",
    ("SC", "Columbia"): "45063",
    ("CO", "Denver"): "08031",
    ("NV", "Las Vegas"): "32003",
    ("CA", "Fresno"): "06019",
}


class PropRavenError(RuntimeError):
    """Raised with an actionable message the dashboard can surface verbatim."""


def last_remaining() -> int | None:
    return _LAST_REMAINING


@dataclass
class Quota:
    used: int | None = None
    limit: int | None = None
    remaining: int | None = None
    period: str = ""
    raw: dict | None = None

    @property
    def low(self) -> bool:
        return self.remaining is not None and self.remaining <= 50


class PropRavenProvider(LeadProvider):
    name = "propraven"

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key = (api_key if api_key is not None else settings.propraven_api_key).strip()
        self.base = (base_url or settings.propraven_base_url or DEFAULT_BASE).rstrip("/")

    # ── plumbing ──────────────────────────────────────────────────
    def _headers(self) -> dict:
        if not self.api_key:
            raise PropRavenError(
                "PROPRAVEN_API_KEY is not set. Add it in Railway environment "
                "variables (Settings > API Keys at propraven.com), or upload a "
                "CSV export instead."
            )
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.base}{path}"
        try:
            with httpx.Client(timeout=45) as client:
                r = client.get(url, params=params or {}, headers=self._headers())
        except httpx.HTTPError as e:
            raise PropRavenError(f"PropRaven network error: {e}") from e

        if r.status_code == 401:
            raise PropRavenError("PropRaven rejected the API key (401). Check it has not been revoked.")
        if r.status_code == 402:
            raise PropRavenError(
                "PropRaven returned 402: this resource needs a paid plan. The free "
                "tier covers 1,000 lookups/month on the standard endpoints."
            )
        if r.status_code == 403:
            raise PropRavenError("PropRaven key is inactive or expired (403).")
        if r.status_code == 429:
            raise PropRavenError(
                "PropRaven rate limit hit (429). Wait for the Retry-After window, "
                "or lower the per-cycle limit in Settings."
            )
        if r.status_code >= 400:
            detail = ""
            try:
                body = r.json()
                detail = body.get("detail") or body.get("title") or ""
            except Exception:
                detail = r.text[:200]
            raise PropRavenError(f"PropRaven {r.status_code}: {detail}")

        global _LAST_REMAINING
        remaining = r.headers.get("x-ratelimit-remaining")
        if remaining is not None:
            try:
                _LAST_REMAINING = int(remaining)
            except ValueError:
                pass

        try:
            return r.json()
        except ValueError:
            raise PropRavenError("PropRaven returned a non-JSON body.")

    # ── quota / coverage ──────────────────────────────────────────
    def usage(self) -> Quota:
        data = self._get("/account/usage")
        body = data.get("data", data) if isinstance(data, dict) else {}
        return Quota(
            used=_int(body.get("used") or body.get("lookups_used")),
            limit=_int(body.get("limit") or body.get("quota") or body.get("lookups_limit")),
            remaining=_int(body.get("remaining") or body.get("lookups_remaining")),
            period=str(body.get("period") or body.get("resets_at") or ""),
            raw=body if isinstance(body, dict) else {},
        )

    def coverage(self, state: str | None = None) -> dict:
        return self._get("/coverage", {"state": state} if state else None)

    # ── lead pull ─────────────────────────────────────────────────
    def county_fips(self, state: str, city: str) -> str | None:
        """Resolve a county FIPS for a market, honouring the env override."""
        for chunk in (settings.propraven_county_fips or "").split(";"):
            if "=" not in chunk:
                continue
            key, val = chunk.split("=", 1)
            # Accept "TX:Houston", "TX,Houston" and "TX Houston".
            parts = [
                x.strip() for x in key.replace(",", ":").replace(" ", ":").split(":")
                if x.strip()
            ]
            if len(parts) != 2:
                continue
            st, ct = parts
            if st.lower() == state.lower() and ct.lower() in city.lower():
                return val.strip()
        return CITY_COUNTY_FIPS.get((state.upper(), city))

    def _page(self, endpoint: str, *, county_fips: str, limit: int, offset: int,
              extra: dict | None = None) -> tuple[list[dict], int]:
        params = {"county_fips": county_fips, "limit": limit, "offset": offset}
        if extra:
            params.update(extra)
        payload = self._get(endpoint, params)
        rows = payload.get("data") if isinstance(payload, dict) else payload
        total = _int(payload.get("total")) if isinstance(payload, dict) else None
        return (rows or []), (total or 0)

    def fetch(
        self,
        *,
        city: str,
        state: str,
        limit: int = 100,
        endpoint: str | None = None,
    ) -> list[dict]:
        """Pull absentee-owner leads for one county.

        Pages with offset until the limit is reached or a short page comes back.
        Never asks for more rows than needed -- every row is a billed lookup.
        """
        county = self.county_fips(state, city)
        if not county:
            raise PropRavenError(
                f"No county FIPS known for {city}, {state}. Add it with "
                f'PROPRAVEN_COUNTY_FIPS="{state}:{city}=XXXXXXXX" (county FIPS '
                f"with state prefix, e.g. 48113 for Dallas County TX)."
            )

        endpoint = endpoint or settings.propraven_deals_endpoint or "/deals/absentee"
        page_size = max(1, min(int(settings.propraven_page_size or 100), 100))
        collected: list[dict] = []
        offset = 0
        guard = 0

        while len(collected) < limit and guard < 50:
            guard += 1
            want = min(page_size, limit - len(collected))
            rows, _total = self._page(endpoint, county_fips=county, limit=want, offset=offset)
            if not rows:
                break
            for row in rows:
                mapped = map_propraven_row(row, city=city, state=state)
                if mapped:
                    collected.append(mapped)
            offset += len(rows)
            if len(rows) < want:
                break
            if _quota_exhausted():
                log.warning("propraven: quota low, stopping the pull early")
                break

        return collected


# ─────────────────────────────────────────────────────────────
# Field mapping
# ─────────────────────────────────────────────────────────────
def _first(row: dict, *keys):
    for k in keys:
        v = row.get(k)
        if v not in (None, "", 0, "0", "0.0"):
            return v
    return None


def _num(v):
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return None


def _int(v):
    n = _num(v)
    return int(n) if n is not None else None


def _bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "y", "t")


_LAST_REMAINING: int | None = None


def _quota_exhausted() -> bool:
    """True once the key is nearly out of lookups, so the pager stops early
    instead of hammering a key that is about to 429."""
    return _LAST_REMAINING is not None and _LAST_REMAINING <= 25


def map_propraven_row(row: dict, *, city: str = "", state: str = "") -> dict | None:
    """Translate one PropRaven row into this system's canonical lead dict.

    Tolerant on purpose: field coverage varies by county, so several aliases are
    tried per field and anything missing is simply left absent. Validation
    downstream is the gate that decides whether the result is a real lead.
    """
    if not isinstance(row, dict):
        return None

    address_obj = row.get("address") if isinstance(row.get("address"), dict) else {}
    owner_obj = row.get("owner") if isinstance(row.get("owner"), dict) else {}
    mail_obj = row.get("mailing_address") if isinstance(row.get("mailing_address"), dict) else {}
    sale_obj = row.get("sale") if isinstance(row.get("sale"), dict) else {}

    street = _first(
        row, "situs_address", "property_address", "site_address", "street_address",
        "address_line1", "address",
    )
    if isinstance(street, dict):
        street = street.get("line1") or street.get("full") or street.get("street")
    if not street:
        street = _first(address_obj, "line1", "street", "full", "address")

    row_city = _first(row, "situs_city", "property_city", "city", "site_city") or _first(address_obj, "city") or city
    row_state = _first(row, "situs_state", "property_state", "state", "site_state") or _first(address_obj, "state") or state
    zip_code = _first(row, "situs_zip", "property_zip", "zip", "zip_code", "site_zip") or _first(address_obj, "zip", "zip_code")

    owner_name = (
        _first(row, "owner_name", "owner_full_name", "owner1", "owner")
        or _first(owner_obj, "full_name", "name", "full")
    )
    if isinstance(owner_name, dict):
        owner_name = (
            owner_name.get("full")
            or owner_name.get("full_name")
            or owner_name.get("name")
        )

    mailing = _first(row, "owner_mailing_address", "mailing_address_full", "owner_address")
    if isinstance(mailing, dict):
        mailing = mailing.get("full") or mailing.get("line1")
    if not mailing:
        parts = [
            _first(mail_obj, "line1", "street"),
            _first(mail_obj, "city"),
            _first(mail_obj, "state"),
            _first(mail_obj, "zip"),
        ]
        mailing = ", ".join(str(p) for p in parts if p) or None

    sale_date = _first(row, "sale_date", "last_sale_date") or _first(sale_obj, "sale_date", "date")
    years_owned = _num(_first(row, "years_owned", "ownership_years"))
    if years_owned is None and sale_date:
        years_owned = _years_since(sale_date)

    return {
        "street": street,
        "city": row_city,
        "state": (str(row_state).upper()[:2] if row_state else ""),
        "zip_code": str(zip_code)[:5] if zip_code else "",
        "county": _first(row, "county_name", "county"),
        "property_type": _first(row, "property_type", "property_type_class", "land_use") or "single_family",
        "sqft": _int(_first(row, "building_area_sqft", "living_area_sqft", "building_area", "sqft")),
        "bedrooms": _num(_first(row, "bedrooms", "beds")),
        "bathrooms": _num(_first(row, "bathrooms", "baths")),
        "year_built": _int(_first(row, "year_built")),
        "lot_sqft": _int(_first(row, "lot_size_sqft", "lot_sqft")),
        "owner_name": owner_name,
        "owner_mailing_address": mailing,
        # PropRaven does not return phone/email; skip trace fills these in later.
        "owner_phone": None,
        "owner_email": None,
        "tax_assessed_value": _num(
            _first(row, "total_assessed_value", "assessed_value", "total_value",
                   "market_value", "tax_assessed_value")
        ),
        "tax_delinquent": _bool(_first(row, "delinquent_tax", "tax_delinquent", "tax_delinquent_flag") or False),
        "tax_delinquent_amount": _num(_first(row, "delinquent_tax_amount", "tax_amount_due")),
        "pre_foreclosure": _bool(_first(row, "foreclosure", "pre_foreclosure", "in_foreclosure") or False),
        "vacant": _bool(_first(row, "vacant", "vacancy_flag") or False),
        "is_absentee_hint": _bool(_first(row, "absentee", "is_absentee", "absentee_owner") or False),
        "years_owned": years_owned,
        "equity_pct": _num(_first(row, "equity_percent", "equity_pct")),
        "asking_price": None,
        # Everything arrives as cold list data. Never inbound, never textable.
        "lead_class": "list",
        "source": "propraven",
        "source_ref": str(_first(row, "parcel_id", "prpv_parcel_id", "id") or "")[:255],
    }


def _years_since(date_str) -> float | None:
    import datetime as dt

    text = str(date_str)[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            d = dt.datetime.strptime(text, fmt)
            return round((dt.datetime.utcnow() - d).days / 365.25, 1)
        except ValueError:
            continue
    return None
