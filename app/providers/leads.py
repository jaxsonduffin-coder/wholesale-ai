"""Lead data providers.

Pluggable so you can swap PropStream / BatchData / a CSV export without
touching the pipeline. Every provider returns raw dicts; validation happens
downstream in app.validation, never here.
"""
from __future__ import annotations

import csv
import io
import random
from abc import ABC, abstractmethod

import httpx

from ..config import settings


class LeadProvider(ABC):
    name: str = "base"

    @abstractmethod
    def fetch(self, *, city: str, state: str, limit: int = 100) -> list[dict]:
        """Return raw property records."""


# ─────────────────────────────────────────────────────────────
# CSV import — works with PropStream, DealMachine, ListSource exports.
# ─────────────────────────────────────────────────────────────
COLUMN_ALIASES = {
    "street": ["street", "address", "property address", "situs", "situs address",
               "site address", "property_address", "addr"],
    "city": ["city", "property city", "situs city", "site city"],
    "state": ["state", "property state", "situs state", "site state", "st"],
    "zip_code": ["zip", "zipcode", "zip code", "postal code", "property zip",
                 "situs zip", "site zip"],
    "county": ["county", "property county"],
    "owner_name": ["owner", "owner name", "owner1", "owner 1", "owner_name",
                   "first name last name", "full name", "taxpayer"],
    "owner_phone": ["phone", "owner phone", "phone 1", "phone1", "mobile",
                    "cell", "primary phone", "contact phone"],
    "owner_phone_2": ["phone 2", "phone2", "alt phone", "secondary phone"],
    "owner_email": ["email", "owner email", "email 1", "email1"],
    "owner_mailing_address": ["mailing address", "owner mailing address",
                              "mail address", "mailing", "owner address"],
    "sqft": ["sqft", "living area", "building sqft", "heated sqft", "square feet",
             "bldg sqft", "total living area"],
    "bedrooms": ["beds", "bedrooms", "br", "num beds"],
    "bathrooms": ["baths", "bathrooms", "ba", "num baths"],
    "year_built": ["year built", "yr built", "year_built", "built"],
    "lot_sqft": ["lot size", "lot sqft", "lot size sqft", "acreage"],
    "tax_assessed_value": ["assessed value", "tax assessed value", "total av",
                           "assessed", "appraised value", "market value"],
    "tax_delinquent": ["delinquent", "tax delinquent", "delinquent taxes"],
    "tax_delinquent_amount": ["delinquent amount", "tax amount due", "amount due"],
    "pre_foreclosure": ["pre-foreclosure", "preforeclosure", "foreclosure",
                        "notice of default", "nod"],
    "vacant": ["vacant", "vacancy", "occupancy", "vacant flag"],
    "years_owned": ["years owned", "ownership years", "years_held"],
    "equity_pct": ["equity", "equity percent", "equity pct", "ltv"],
    "asking_price": ["asking price", "list price", "price", "asking"],
    "property_type": ["property type", "type", "land use", "use code"],
    # --- channel policy / consent -------------------------------------
    # Most exports carry none of these. That is fine and expected: absence
    # means no consent, which is the correct assumption for purchased data.
    "lead_class": ["lead class", "lead_class", "list type", "list_type",
                   "lead type", "lead_type", "lead source type", "source class",
                   "channel", "acquisition channel"],
    "consent_sms": ["sms consent", "sms_consent", "consent", "opt in", "opt_in",
                    "opted in", "opted_in", "sms opt in", "sms_opt_in",
                    "text consent", "texting consent", "consent to text"],
    "consent_call": ["call consent", "call_consent", "phone consent",
                     "consent to call"],
    "consent_source": ["consent source", "consent_source", "opt in source",
                       "opt_in_source", "consent form", "form url", "form"],
    "consent_date": ["consent date", "consent_date", "opt in date",
                     "opt_in_date", "date consented", "consent timestamp"],
    "consent_evidence": ["consent evidence", "consent_evidence", "opt in proof",
                         "proof of consent", "consent notes"],
}


def _map_row(row: dict) -> dict:
    """Map a provider's column names onto our canonical field names."""
    lowered = {(k or "").strip().lower(): v for k, v in row.items()}
    out: dict = {}
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in lowered and lowered[alias] not in (None, ""):
                out[field] = lowered[alias]
                break
    # Anything unmapped is preserved for debugging.
    out["_raw"] = row
    return out


class CSVProvider(LeadProvider):
    name = "csv"

    def parse(self, text: str, source_name: str = "csv") -> list[dict]:
        # Sniff the dialect; PropStream exports are comma-delimited with quotes.
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        rows: list[dict] = []
        for raw in reader:
            mapped = _map_row({k: v for k, v in raw.items() if k})
            mapped["source"] = source_name
            rows.append(mapped)
        return rows

    def fetch(self, *, city: str, state: str, limit: int = 100) -> list[dict]:
        raise NotImplementedError("CSV provider is used via upload, not fetch()")


# ─────────────────────────────────────────────────────────────
# BatchData — real API integration.
# Docs: https://batchdata.io  (POST /property/search, /contact/enrich)
# ─────────────────────────────────────────────────────────────
class BatchDataProvider(LeadProvider):
    name = "batchdata"
    BASE = "https://api.batchdata.com/api/v1"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {settings.batchdata_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def fetch(self, *, city: str, state: str, limit: int = 100) -> list[dict]:
        if not settings.batchdata_api_key:
            raise RuntimeError(
                "BATCHDATA_API_KEY is not set. Add it in Settings, or use the "
                "demo provider / CSV upload."
            )
        payload = {
            "filter": {
                "properties": {
                    "city": [city],
                    "state": [state],
                },
                # Motivated-seller filters — the whole point of paying for data.
                "owner": {
                    "absenteeOwner": True,
                },
                "demographics": {},
            },
            "options": {"take": min(limit, 100), "skip": 0},
            "sort": {"field": "assessedValue", "order": "asc"},
        }
        with httpx.Client(timeout=45) as client:
            r = client.post(f"{self.BASE}/property/search", json=payload, headers=self._headers())
            if r.status_code >= 400:
                raise RuntimeError(f"BatchData {r.status_code}: {r.text[:300]}")
            data = r.json()

        rows: list[dict] = []
        for item in (data.get("results") or {}).get("properties", []) or []:
            addr = item.get("address") or {}
            owner = item.get("owner") or {}
            sale = item.get("sale") or {}
            valuation = item.get("valuation") or {}
            rows.append(
                {
                    "street": addr.get("street"),
                    "city": addr.get("city"),
                    "state": addr.get("state"),
                    "zip_code": addr.get("zip"),
                    "county": addr.get("county"),
                    "sqft": item.get("buildingAreaSquareFeet"),
                    "bedrooms": item.get("bedrooms"),
                    "bathrooms": item.get("bathrooms"),
                    "year_built": item.get("yearBuilt"),
                    "property_type": item.get("propertyType") or "single_family",
                    "owner_name": owner.get("fullName"),
                    "owner_mailing_address": owner.get("mailingAddress", {}).get("fullAddress"),
                    "owner_email": (owner.get("email") or {}).get("address"),
                    "owner_phone": None,  # needs /contact/enrich
                    "tax_assessed_value": valuation.get("assessedValue")
                    or valuation.get("estimatedValue"),
                    "tax_delinquent": bool(item.get("delinquentTax")),
                    "tax_delinquent_amount": item.get("delinquentTaxAmount"),
                    "pre_foreclosure": bool(item.get("foreclosure")),
                    "vacant": bool(item.get("vacant")),
                    "years_owned": sale.get("yearsOwned"),
                    "asking_price": sale.get("listPrice"),
                    "source": "batchdata",
                    "source_ref": item.get("id") or "",
                }
            )
        return rows

    def skip_trace(self, addresses: list[dict]) -> list[dict]:
        """Enrich records with phones/emails. Returns contact dicts keyed by ref."""
        if not settings.batchdata_api_key or not addresses:
            return []
        payload = {
            "requests": [
                {
                    "propertyAddress": {
                        "street": a.get("street", ""),
                        "city": a.get("city", ""),
                        "state": a.get("state", ""),
                        "zip": a.get("zip_code", ""),
                    },
                    "name": {"full": a.get("owner_name", "")},
                }
                for a in addresses
            ]
        }
        with httpx.Client(timeout=60) as client:
            r = client.post(f"{self.BASE}/contact/enrich", json=payload, headers=self._headers())
            if r.status_code >= 400:
                raise RuntimeError(f"BatchData skip trace {r.status_code}: {r.text[:300]}")
            data = r.json()
        out = []
        for item in data.get("results") or []:
            persons = item.get("persons") or []
            phones = [p for person in persons for p in (person.get("phoneNumbers") or [])]
            emails = [e for person in persons for e in (person.get("emails") or [])]
            out.append(
                {
                    "owner_phone": phones[0].get("number") if phones else None,
                    "owner_phone_2": phones[1].get("number") if len(phones) > 1 else None,
                    "owner_email": emails[0].get("address") if emails else None,
                }
            )
        return out


# ─────────────────────────────────────────────────────────────
# Demo provider — generates realistic, VALID leads so the whole system
# can be tested end to end with no paid credentials.
# ─────────────────────────────────────────────────────────────
class DemoProvider(LeadProvider):
    name = "demo"

    STREETS = [
        "Mockingbird Ln", "Elm St", "Oak Cliff Dr", "Spring Valley Rd",
        "Preston Rd", "Belt Line Rd", "Marsh Ln", "Frankford Rd",
        "Josey Ln", "Rosemeade Pkwy", "Keller Springs Rd", "Inwood Rd",
        "Lemmon Ave", "Henderson Ave", "Zang Blvd", "Davis St",
        "Jefferson Blvd", "Illinois Ave", "Kessler Pkwy", "Clarendon Dr",
    ]
    FIRST = ["James", "Maria", "Robert", "Linda", "Michael", "Patricia",
             "David", "Barbara", "William", "Elizabeth", "Jose", "Nancy"]
    LAST = ["Smith", "Garcia", "Johnson", "Martinez", "Williams", "Brown",
            "Rodriguez", "Davis", "Hernandez", "Miller", "Lopez", "Wilson"]
    CITIES = [("Dallas", "TX"), ("Houston", "TX"), ("Fort Worth", "TX"),
              ("Arlington", "TX"), ("Plano", "TX"), ("Garland", "TX")]

    def fetch(self, *, city: str, state: str = "TX", limit: int = 50) -> list[dict]:
        rng = random.Random()
        rows = []
        for _ in range(limit):
            c, st = (city, state) if city else rng.choice(self.CITIES)
            sqft = rng.randint(950, 2800)
            year = rng.randint(1948, 2005)
            years_owned = rng.choice([3, 6, 9, 14, 21, 27, 33])
            assessed = round(sqft * rng.uniform(95, 165), -2)
            delinquent = rng.random() < 0.3
            owner = f"{rng.choice(self.FIRST)} {rng.choice(self.LAST)}"
            phone = (
                f"+1{rng.randint(200, 989)}{rng.randint(200, 989)}"
                f"{rng.randint(1000, 9999)}"
            )
            street = f"{rng.randint(100, 9899)} {rng.choice(self.STREETS)}"

            # Generate 3-5 synthetic comps so scoring has real data to work with.
            # In production these come from a comp provider; the demo generates
            # plausible nearby sales at price-per-sqft in the right range.
            ppsf_base = assessed / sqft if sqft else 130.0
            num_comps = rng.randint(3, 5)
            comps = []
            for i in range(num_comps):
                comp_sqft = rng.randint(max(700, sqft - 400), sqft + 400)
                # Comps sell at market, which is 10-30% above assessed in TX.
                comp_ppsf = ppsf_base * rng.uniform(1.05, 1.35)
                comp_sold_price = round(comp_ppsf * comp_sqft, -2)
                comps.append({
                    "address": f"{rng.randint(100, 9899)} {rng.choice(self.STREETS)}",
                    "sold_price": comp_sold_price,
                    "sqft": comp_sqft,
                    "sold_days_ago": rng.randint(15, 180),
                    "distance_miles": round(rng.uniform(0.1, 1.5), 2),
                })

            # Some leads have an asking price (roughly 40%).
            asking = None
            if rng.random() < 0.4:
                # Asking somewhere between assessed and market
                asking = round(assessed * rng.uniform(0.85, 1.2), -2)

            rows.append(
                {
                    "street": street,
                    "city": c,
                    "state": st,
                    "zip_code": f"{rng.randint(75000, 79999)}",
                    "county": "Dallas",
                    "sqft": sqft,
                    "bedrooms": rng.choice([2, 3, 3, 4, 4, 5]),
                    "bathrooms": rng.choice([1, 1.5, 2, 2, 2.5, 3]),
                    "year_built": year,
                    "owner_name": owner,
                    "owner_phone": phone,
                    "owner_email": (
                        f"{owner.split()[0].lower()}.{rng.randint(10, 99)}@example-mail.test"
                        if rng.random() < 0.5 else None
                    ),
                    "owner_mailing_address": (
                        f"{rng.randint(100, 9999)} Main St, Austin, TX 78701"
                        if rng.random() < 0.4
                        else None
                    ),
                    "tax_assessed_value": assessed,
                    "tax_delinquent": delinquent,
                    "tax_delinquent_amount": round(rng.uniform(1200, 14000), 2)
                    if delinquent
                    else None,
                    "pre_foreclosure": rng.random() < 0.15,
                    "vacant": rng.random() < 0.18,
                    "probate_or_inherited": rng.random() < 0.1,
                    "tired_landlord": rng.random() < 0.12,
                    "years_owned": years_owned,
                    "equity_pct": rng.choice([15, 32, 48, 61, 78, 95]),
                    "asking_price": asking,
                    "source": "demo",
                    "source_ref": f"demo-{rng.randint(10**6, 10**7)}",
                    # Synthetic comps carried alongside the lead; the pipeline
                    # stores these in the comps table so scoring can use them.
                    "_comps": comps,
                }
            )
        # Exercise the channel policy: roughly a fifth of demo records are
        # inbound with verifiable consent; the rest are cold list data and can
        # only be called or mailed.
        for row in rows:
            if rng.random() < 0.2:
                row["lead_class"] = "inbound"
                row["consent_sms"] = True
                row["consent_source"] = "web_form:/demo-landing"
                row["consent_at"] = (
                    __import__("datetime").datetime.utcnow()
                    - __import__("datetime").timedelta(days=rng.randint(1, 30))
                ).isoformat()
            elif rng.random() < 0.15:
                row["lead_class"] = "probate"
            else:
                row["lead_class"] = "list"
        return rows


def get_provider(name: str | None = None) -> LeadProvider:
    name = (name or settings.lead_provider or "demo").lower()
    if name == "propraven":
        from .propraven import PropRavenProvider

        return PropRavenProvider()
    if name == "batchdata":
        return BatchDataProvider()
    if name == "csv":
        return CSVProvider()
    return DemoProvider()
