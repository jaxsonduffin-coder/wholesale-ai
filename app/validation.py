"""Lead validation and normalisation.

Every raw dict from a provider passes through `validate_lead()` before it
touches the database. The function either returns a clean dict ready to be
unpacked into Lead(**clean), or raises ValidationError.

Design rules:
  * Hard-reject only garbage that would corrupt the database or waste cadence
    steps (no street, no city, obviously fake data). Everything else can be
    enriched later.
  * Normalise phones and emails in one place so the rest of the code can
    compare them with == instead of guessing about formatting.
  * Never silently drop a field the model expects — set it to a sensible
    default so Lead(**clean) always works.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re


class ValidationError(ValueError):
    """Raised when a record is too broken to import."""


# ─── phone normalisation ───────────────────────────────────────
_PHONE_RE = re.compile(r"[^\d+]")

# NANP: area code 2-9 then two digits, exchange 2-9 then two digits
_NANP_RE = re.compile(r"^[2-9]\d{2}[2-9]\d{6}$")


def normalize_phone(raw: str | None) -> str | None:
    """Return an E.164-ish phone or None.

    Strips everything but digits and a leading +, then prepends +1 if the
    number looks like a bare 10-digit US number. Returns None for numbers
    that fail NANP validation (all-same digits, invalid area code / exchange).
    """
    if not raw:
        return None
    digits = _PHONE_RE.sub("", str(raw).strip())
    if digits.startswith("+"):
        digits_only = digits[1:]
    else:
        digits_only = digits

    # Strip country code if already present as 1XXXXXXXXXX
    if len(digits_only) == 11 and digits_only.startswith("1"):
        digits_only = digits_only[1:]

    if len(digits_only) < 10:
        return None

    # For 10-digit numbers, apply NANP validation.
    if len(digits_only) == 10:
        # Reject all-same-digit numbers (0000000000, 1111111111, etc.)
        if len(set(digits_only)) == 1:
            return None
        # NANP: area code and exchange must start with 2-9
        if not _NANP_RE.match(digits_only):
            return None
        return f"+1{digits_only}"

    # Already has country code — basic length check only.
    return f"+{digits_only}"


# ─── email normalisation ──────────────────────────────────────
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}$")

# Domains that are never real inboxes.
_THROWAWAY_DOMAINS = {
    "example.com", "example.org", "example.net", "test.com",
    "localhost", "invalid", "example",
}

# Local parts that are never real people.
_NOREPLY_LOCALS = {
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster",
}


def normalize_email(raw: str | None) -> str | None:
    """Return a lowered, trimmed email or None."""
    if not raw:
        return None
    email = str(raw).strip().lower()
    if not _EMAIL_RE.match(email):
        return None
    local, domain = email.rsplit("@", 1)
    # Single-char locals are almost never real.
    if len(local) < 2:
        return None
    if domain in _THROWAWAY_DOMAINS:
        return None
    if local in _NOREPLY_LOCALS:
        return None
    return email


# ─── street address checks ────────────────────────────────────
_HOUSE_NUM_RE = re.compile(r"^\d{1,6}\b")
_BOILERPLATE = {
    "home", "about", "contact", "search", "login", "register", "privacy",
    "terms", "faq", "help", "blog", "sitemap", "n/a", "na", "none", "test",
    "unknown", ".", "-", "--",
}
_GIBBERISH_RE = re.compile(r"(.)\1{4,}")  # 5+ repeated chars

# Multi-word web-chrome phrases. If ANY of these appear in the street string
# (case-insensitive), the record is scraped page furniture, not a real address.
_WEB_CHROME_PHRASES = [
    "page not found", "skip to", "click here", "privacy policy",
    "search results", "contact us", "annual report", "navigation links",
    "terms of", "cookie", "sign in", "sign up", "log in", "log out",
    "read more", "learn more", "view all", "see more", "show more",
    "load more", "next page", "previous page", "go back", "go to",
    "select all", "select none",
]

# Well-known placeholder / fictional addresses.
_PLACEHOLDER_STREETS = {
    "123 main st", "123 main street",
    "1234 main st", "1234 main street",
    "1600 pennsylvania ave",
}


def _is_valid_street(street: str) -> tuple[bool, str]:
    """Quick sanity checks. Returns (ok, reason)."""
    s = street.strip()
    if not s:
        return False, "missing street"
    low = s.lower()
    if low in _BOILERPLATE:
        return False, f"boilerplate: {s}"
    if _GIBBERISH_RE.search(s):
        return False, f"gibberish text: {s[:40]}"
    if not _HOUSE_NUM_RE.match(s):
        return False, f"no house number in '{s[:40]}'"
    # Even with a leading house number, web-chrome phrases are not addresses.
    for phrase in _WEB_CHROME_PHRASES:
        if phrase in low:
            return False, f"boilerplate: {s}"
    # Well-known placeholders.
    if low in _PLACEHOLDER_STREETS:
        return False, f"boilerplate: placeholder address {s}"
    return True, ""


def validate_street(street: str) -> str:
    """Public API: return cleaned street or raise ValidationError."""
    s = street.strip()
    ok, reason = _is_valid_street(s)
    if not ok:
        raise ValidationError(reason)
    return s


# ─── US state codes ───────────────────────────────────────────
_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC", "PR", "VI", "GU",
}

# Words that indicate a city field was scraped from a web page, not real data.
_CITY_JUNK = {
    "cad", "isd", "county", "appraisal", "district", "website",
    "page", "results", "search", "login", "report",
}


def address_key(street: str, city: str, state: str, zip_code: str = "") -> str:
    """Deterministic dedup key from address components.

    Public API — callers may omit zip_code for comparison purposes.
    """
    raw = f"{street}|{city}|{state}|{zip_code}".lower().strip()
    # Collapse whitespace and normalise common abbreviations.
    raw = re.sub(r"\s+", " ", raw)
    # Remove periods first so "St." becomes "St", then normalise.
    raw = raw.replace(".", "")
    raw = re.sub(r"\bstreet\b", "st", raw)
    raw = re.sub(r"\bavenue\b", "ave", raw)
    raw = re.sub(r"\bdrive\b", "dr", raw)
    raw = re.sub(r"\bboulevard\b", "blvd", raw)
    raw = re.sub(r"\blane\b", "ln", raw)
    raw = re.sub(r"\broad\b", "rd", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ─── name helpers ─────────────────────────────────────────────
_NAME_JUNK = {
    "navigation links", "annual report", "about", "contact", "home",
    "search", "login", "privacy", "terms", "faq", "help",
    "n/a", "na", "none", "unknown", "test", "commercial",
    "residential", "vacant", "owner", "property",
}


def split_owner_name(full_name: str | None) -> tuple[str | None, str | None]:
    """Return (first_name, full_name) or (None, None) if the name is junk."""
    if not full_name:
        return None, None
    cleaned = full_name.strip()
    if not cleaned or cleaned.lower() in _NAME_JUNK:
        return None, None
    # Reject all-caps single-word names that look like web chrome.
    parts = cleaned.split()
    if len(parts) < 2:
        return None, None
    # Quick gibberish check.
    if _GIBBERISH_RE.search(cleaned):
        return None, None
    first = parts[0].title()
    full = " ".join(p.title() if p.isupper() else p for p in parts)
    return first, full


def _extract_first_name(full_name: str | None) -> str | None:
    first, _ = split_owner_name(full_name)
    return first


# ─── absentee / out-of-state detection ───────────────────────
def _normalise_addr_for_compare(addr: str) -> str:
    """Collapse an address string for rough comparison."""
    s = addr.lower().strip()
    s = re.sub(r"[^a-z0-9 ]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def is_absentee(property_addr: str, mailing_addr: str | None) -> bool:
    """True when the mailing address differs from the property address."""
    if not mailing_addr:
        return False
    return _normalise_addr_for_compare(property_addr) != _normalise_addr_for_compare(mailing_addr)


def _extract_state_from_address(addr: str | None) -> str | None:
    """Try to pull a two-letter state code from a free-form address."""
    if not addr:
        return None
    # Look for a two-letter state code surrounded by word boundaries.
    for token in reversed(addr.replace(",", " ").split()):
        up = token.strip().upper()[:2]
        if up in _US_STATES and len(token.strip()) <= 3:
            return up
    return None


# ─── coercion helpers ─────────────────────────────────────────
def _to_int(val) -> int | None:
    if val is None or val == "":
        return None
    try:
        return int(float(str(val).replace(",", "").replace("$", "")))
    except (ValueError, TypeError):
        return None


def _to_float(val) -> float | None:
    if val is None or val == "":
        return None
    try:
        return float(str(val).replace(",", "").replace("$", ""))
    except (ValueError, TypeError):
        return None


def _to_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    return str(val).strip().lower() in ("1", "true", "yes", "y", "t")


# ─── public coercion helpers ────────────────────────────────
_TRUTHY = {"1", "true", "yes", "y", "t", "opted in", "opted_in", "opt in", "opt_in"}
_FALSY = {"0", "false", "no", "n", "f", "opted out", "opted_out", "opt out", "opt_out"}


def parse_bool(value) -> bool | None:
    """Three-valued bool parser: True / False / None.

    Unlike _to_bool (which always returns bool), this returns None for
    ambiguous or missing values — important for consent fields where "unknown"
    must not be silently treated as False.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if not s:
        return False
    if s in _TRUTHY:
        return True
    if s in _FALSY:
        return False
    return None


# Date formats we accept for consent_at and similar fields.
_DATE_FORMATS = [
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%Y/%m/%d",
    "%B %d %Y",
    "%b %d %Y",
    "%B %d, %Y",
    "%b %d, %Y",
]


def parse_datetime(value) -> dt.datetime | None:
    """Parse a date/datetime string in any common format.

    Returns a naive datetime or None if unparseable.
    """
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day)
    s = str(value).strip()
    if not s:
        return None
    # Try ISO 8601 first (handles microseconds, timezone offsets, etc.)
    try:
        parsed = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None)
    except (ValueError, AttributeError):
        pass

    for fmt in _DATE_FORMATS:
        try:
            parsed = dt.datetime.strptime(s, fmt)
            # Strip tzinfo for consistency — we store UTC-naive internally.
            return parsed.replace(tzinfo=None)
        except ValueError:
            continue
    return None


# Lead classes that may carry SMS consent through validation.
_CONSENT_ALLOWED_CLASSES = {"inbound"}


# ─── main entry point ────────────────────────────────────────
def validate_lead(raw: dict) -> dict:
    """Validate and normalise a raw lead dict from any provider.

    Returns a dict whose keys match Lead model columns, ready to unpack as
    Lead(**result). Raises ValidationError for records too broken to keep.
    """
    street = str(raw.get("street") or "").strip()
    ok, reason = _is_valid_street(street)
    if not ok:
        raise ValidationError(reason)

    city = str(raw.get("city") or "").strip().title()
    if not city or len(city) < 2:
        raise ValidationError(f"missing city for {street[:40]}")

    # Reject cities containing web-scraper junk words.
    city_words = set(city.lower().split())
    if city_words & _CITY_JUNK:
        raise ValidationError(f"city looks like scraped page content: {city}")

    state = str(raw.get("state") or "").strip().upper()[:2]
    if state not in _US_STATES:
        raise ValidationError(f"invalid state '{state}' for {street[:40]}")

    # Normalise zip: keep only the 5-digit prefix.
    raw_zip = str(raw.get("zip_code") or "").strip()
    zip_code = raw_zip.split("-")[0][:5] if raw_zip else ""

    owner_name = str(raw.get("owner_name") or "").strip()[:255] or None
    owner_first_name = _extract_first_name(owner_name)

    phone = normalize_phone(raw.get("owner_phone"))
    phone2 = normalize_phone(raw.get("owner_phone_2"))
    email = normalize_email(raw.get("owner_email"))

    addr_key = address_key(street, city, state, zip_code)

    # Timezone from state — used for quiet-hours enforcement.
    from .compliance import timezone_for_state
    timezone = timezone_for_state(state, city)

    # Absentee / out-of-state detection from mailing address.
    mailing = str(raw.get("owner_mailing_address") or "").strip()[:400] or None
    property_addr = f"{street}, {city} {state}"
    detected_absentee = is_absentee(property_addr, mailing)
    mailing_state = _extract_state_from_address(mailing)
    detected_oos = bool(mailing_state and mailing_state != state)

    # Coerce booleans from provider data (may arrive as strings).
    provider_absentee = _to_bool(raw.get("is_absentee") or raw.get("is_absentee_hint"))

    # ── Consent stripping (SB 140 / TCPA guardrail) ─────────
    # Normalise lead_class: unknown values default to "list" (safe/untextable).
    lead_class_raw = str(raw.get("lead_class") or "list").strip().lower()[:16]
    _KNOWN_CLASSES = {"inbound", "list", "probate", "referral"}
    lead_class = lead_class_raw if lead_class_raw in _KNOWN_CLASSES else "list"

    consent_sms = _to_bool(raw.get("consent_sms"))
    consent_call = _to_bool(raw.get("consent_call"))
    consent_source = str(raw.get("consent_source") or "").strip()[:60]
    consent_at = parse_datetime(raw.get("consent_at") or raw.get("consent_date"))

    if lead_class not in _CONSENT_ALLOWED_CLASSES:
        # Non-inbound leads NEVER carry SMS consent, regardless of what the
        # provider data claims. This is the core SB 140 protection.
        consent_sms = False
        consent_source = ""
        consent_at = None
    elif consent_sms and not consent_source:
        # Inbound but consent has no verifiable source — downgrade.
        consent_sms = False

    return {
        # property
        "street": street,
        "city": city,
        "state": state,
        "zip_code": zip_code,
        "county": str(raw.get("county") or "").strip()[:120],
        "property_type": str(raw.get("property_type") or "single_family").strip()[:40],
        "sqft": _to_int(raw.get("sqft")),
        "bedrooms": _to_float(raw.get("bedrooms")),
        "bathrooms": _to_float(raw.get("bathrooms")),
        "year_built": _to_int(raw.get("year_built")),
        "lot_sqft": _to_int(raw.get("lot_sqft")),
        # owner
        "owner_name": owner_name,
        "owner_first_name": owner_first_name,
        "owner_phone": phone,
        "owner_phone_2": phone2,
        "owner_email": email,
        "owner_mailing_address": mailing,
        "is_absentee": detected_absentee or provider_absentee,
        "is_out_of_state": detected_oos or _to_bool(raw.get("is_out_of_state")),
        "years_owned": _to_float(raw.get("years_owned")),
        "equity_pct": _to_float(raw.get("equity_pct")),
        # distress
        "tax_assessed_value": _to_float(raw.get("tax_assessed_value")),
        "tax_delinquent": _to_bool(raw.get("tax_delinquent")),
        "tax_delinquent_amount": _to_float(raw.get("tax_delinquent_amount")),
        "pre_foreclosure": _to_bool(raw.get("pre_foreclosure")),
        "vacant": _to_bool(raw.get("vacant")),
        "probate_or_inherited": _to_bool(raw.get("probate_or_inherited")),
        "tired_landlord": _to_bool(raw.get("tired_landlord")),
        "code_violations": _to_bool(raw.get("code_violations")),
        "asking_price": _to_float(raw.get("asking_price")),
        # compliance — consent stripping is the SB 140 / TCPA guardrail.
        "lead_class": lead_class,
        "consent_sms": consent_sms,
        "consent_call": consent_call,
        "consent_source": consent_source,
        "consent_at": consent_at,
        # provenance
        "source": str(raw.get("source") or "manual").strip()[:60],
        "source_ref": str(raw.get("source_ref") or "").strip()[:255],
        "address_key": addr_key,
        "timezone": timezone,
    }
