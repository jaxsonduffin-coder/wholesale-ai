"""The regression test that matters most: the exact records v1 accepted."""
from __future__ import annotations

import pytest

from app.validation import (
    ValidationError,
    address_key,
    is_absentee,
    normalize_email,
    normalize_phone,
    split_owner_name,
    validate_lead,
    validate_street,
)

# ── The seven records v1 shipped to production as "leads" ──────────────
V1_GARBAGE = [
    ("Houston Houston", "Houston", "TX"),
    ("", "Dallas", "TX"),
    ("Navigation Links", "Dallas", "TX"),
    ("About Dcad", "Dallas", "TX"),
    ("Nsewnenwsesw", "Dallas", "TX"),
    ("Commercial", "Dallas", "TX"),
    ("Annual Report", "Dallas", "TX"),
]


@pytest.mark.parametrize("street,city,state", V1_GARBAGE)
def test_v1_production_leads_are_all_rejected(street, city, state):
    """Every lead that v1 actually served on its dashboard must be refused."""
    with pytest.raises(ValidationError):
        validate_lead({"street": street, "city": city, "state": state})


@pytest.mark.parametrize(
    "street",
    [
        "4501 W Mockingbird Ln",
        "1234 Elm St",
        "77 County Road 210",
        "8821 FM 1960 Rd W",
        "1600 Pennsylvania Ave NW",
        "3141 Springfield Dr",
        "902 Wintergreen Ct",
        "55 Northwest Expy",
        "1200 N Central Expy",
        "3027 Southeast Blvd",
        "900 Nw 12th Ave",
        "17700 Dallas Pkwy",
    ],
)
def test_real_addresses_are_accepted(street):
    out = validate_street(street)
    assert out == street


@pytest.mark.parametrize(
    "street",
    [
        "404 Page Not Found",
        "Contact Us 2024",
        "123 Main St",
        "Skip to Content",
        "Click Here 5",
        "Privacy Policy 2023",
        "Search Results 12",
    ],
)
def test_numbers_do_not_rescue_page_chrome(street):
    """The house-number rule alone is not enough; boilerplate is caught too."""
    with pytest.raises(ValidationError):
        validate_street(street)


def test_city_must_not_duplicate_street_noise():
    with pytest.raises(ValidationError):
        validate_lead({"street": "123 Elm St", "city": "Dallas Cad", "state": "TX"})


def test_state_must_be_real():
    with pytest.raises(ValidationError):
        validate_lead({"street": "123 Elm St", "city": "Dallas", "state": "ZZ"})


def test_zip_is_normalized():
    r = validate_lead({"street": "123 Elm St", "city": "Dallas", "state": "TX",
                       "zip_code": "75209-1234"})
    assert r["zip_code"] == "75209"


# ── Phone / email ──────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("214-555-0143", "+12145550143"),
        ("(214) 555 0143", "+12145550143"),
        ("12145550143", "+12145550143"),
        ("+1 214 555 0143", "+12145550143"),
        ("2145550143", "+12145550143"),
    ],
)
def test_phone_normalization(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "555-0143", "0000000000", "1111111111", "0145550143", "2140550143",
     "abc", None, "123"],
)
def test_junk_phones_rejected(raw):
    assert normalize_phone(raw) is None


@pytest.mark.parametrize(
    "raw", ["", "not-an-email", "a@b", "x@example.com", "noreply@foo.com", None]
)
def test_junk_emails_rejected(raw):
    assert normalize_email(raw) is None


def test_real_email_kept():
    assert normalize_email("Owner@Gmail.com") == "owner@gmail.com"


# ── Owner names ────────────────────────────────────────────────────────
def test_navigation_links_is_not_a_person():
    first, full = split_owner_name("Navigation Links")
    assert first is None and full is None


def test_annual_report_is_not_a_person():
    first, full = split_owner_name("Annual Report")
    assert first is None and full is None


def test_real_owner_name_parsed():
    first, full = split_owner_name("Robert J. Martinez")
    assert first == "Robert"
    assert full == "Robert J. Martinez"


# ── Dedupe + absentee ──────────────────────────────────────────────────
def test_address_key_is_stable_across_formatting():
    a = address_key("1234 Elm Street", "Dallas", "TX")
    b = address_key("1234  ELM st.", "dallas", "tx")
    assert a == b


def test_address_key_differs_by_city():
    assert address_key("1234 Elm St", "Dallas", "TX") != address_key(
        "1234 Elm St", "Houston", "TX"
    )


def test_absentee_detection():
    assert is_absentee("1234 Elm St, Dallas TX", "99 Other Rd, Austin TX 78701")
    assert not is_absentee("1234 Elm St, Dallas TX", "1234 Elm St, Dallas TX")


def test_out_of_state_flag():
    r = validate_lead(
        {
            "street": "1234 Elm St",
            "city": "Dallas",
            "state": "TX",
            "owner_mailing_address": "55 Ocean Dr, Miami, FL 33139",
        }
    )
    assert r["is_out_of_state"] is True
    assert r["is_absentee"] is True
