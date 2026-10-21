"""Scoring invariants — the guard that would have prevented v1's fake contracts."""
from __future__ import annotations

import pytest

from app.scoring import (
    Comp,
    MA_PERCENT,
    analyze,
    estimate_arv,
    estimate_repairs,
    grade_lead,
    motivation,
)


def base_lead(**over):
    lead = {
        "sqft": 1800,
        "year_built": 1975,
        "tax_assessed_value": None,
        "asking_price": None,
        "assignment_fee": 12000,
        "repair_level": "light",
        "owner_phone": "+12145550143",
        "owner_email": None,
        "status": "new",
    }
    lead.update(over)
    return lead


def comps(n=3, ppsf=140, sqft=1800):
    return [
        Comp(sold_price=ppsf * sqft, sqft=sqft, sold_days_ago=60, distance_miles=0.4)
        for _ in range(n)
    ]


# ── ARV ────────────────────────────────────────────────────────────────
def test_arv_from_comps():
    arv, count, source, _ = estimate_arv(1800, comps())
    assert source == "comps"
    assert count == 3
    assert arv == pytest.approx(140 * 1800, rel=0.02)


def test_arv_requires_three_comps():
    arv, count, source, warnings = estimate_arv(1800, comps(n=2))
    assert source != "comps"
    assert any("no comps" in w for w in warnings)


def test_arv_falls_back_to_assessed_with_warning():
    arv, count, source, warnings = estimate_arv(1800, [], fallback_value=200_000)
    assert source == "assessed_proxy"
    assert arv > 200_000                     # assessed is below market
    assert any("provisional" in w.lower() for w in warnings)


def test_arv_is_zero_with_no_evidence():
    arv, count, source, _ = estimate_arv(1800, [], None)
    assert arv == 0.0
    assert source == "none"


def test_comp_outlier_is_contained():
    mixed = comps(n=4, ppsf=140) + [
        Comp(sold_price=1_000_000, sqft=1000, sold_days_ago=10, distance_miles=0.1)
    ]
    arv, _, _, warnings = estimate_arv(1800, mixed)
    # The $1000/sqft comp must not drag the ARV anywhere near $1.8M.
    assert arv < 500_000
    assert warnings


# ── Repairs ────────────────────────────────────────────────────────────
def test_repair_tiers_scale_with_condition():
    light, _ = estimate_repairs(1000, "light")
    medium, _ = estimate_repairs(1000, "medium")
    heavy, _ = estimate_repairs(1000, "heavy")
    assert light < medium < heavy


def test_old_house_light_flip_upgraded_to_medium():
    _, level = estimate_repairs(1500, "light", year_built=1948)
    assert level == "medium"


# ── Motivation ─────────────────────────────────────────────────────────
def test_motivation_rewards_distress():
    score, reasons = motivation(
        {
            "pre_foreclosure": True,
            "tax_delinquent": True,
            "tax_delinquent_amount": 9000,
            "vacant": True,
            "is_absentee": True,
            "years_owned": 25,
            "equity_pct": 80,
        }
    )
    assert score >= 70
    assert any("foreclosure" in r for r in reasons)


def test_motivation_is_zero_for_a_clean_retail_owner():
    score, reasons = motivation({"years_owned": 2, "equity_pct": 10})
    assert score == 0
    assert reasons == []


def test_motivation_is_capped():
    score, _ = motivation(
        {
            "pre_foreclosure": True, "tax_delinquent": True,
            "probate_or_inherited": True, "vacant": True,
            "code_violations": True, "tired_landlord": True,
            "is_absentee": True, "is_out_of_state": True,
            "years_owned": 40, "equity_pct": 100, "year_built": 1930,
        }
    )
    assert score == 100.0


# ── THE INVARIANT ──────────────────────────────────────────────────────
def test_no_arv_means_grade_f():
    """v1 showed '3 Under Contract' on leads with ARV 0. Impossible now."""
    score, grade = grade_lead(
        arv=0.0, max_offer=0.0, motivation_score=100.0,
        is_contactable=True, comp_count=5,
    )
    assert grade == "F"
    assert score == 0.0


def test_no_contact_path_never_grades_above_f():
    score, grade = grade_lead(
        arv=250_000, max_offer=150_000, motivation_score=100.0,
        is_contactable=False, comp_count=5,
    )
    assert grade == "F"


@pytest.mark.parametrize("status", ["dead", "closed"])
def test_dead_leads_grade_f(status):
    _, grade = grade_lead(
        arv=250_000, max_offer=150_000, motivation_score=100.0,
        is_contactable=True, comp_count=5, status=status,
    )
    assert grade == "F"


def test_grade_a_needs_real_comps():
    """A tax-assessed proxy is a guess — it can never earn an A."""
    _, grade = grade_lead(
        arv=250_000, max_offer=150_000, motivation_score=100.0,
        is_contactable=True, comp_count=0, comp_source="assessed_proxy",
    )
    assert grade != "A"


def test_strong_lead_with_comps_earns_a():
    _, grade = grade_lead(
        arv=250_000, max_offer=150_000, motivation_score=85.0,
        is_contactable=True, comp_count=4, comp_source="comps",
    )
    assert grade == "A"


def test_asking_price_under_max_offer_boosts_grade():
    common = dict(
        arv=250_000, max_offer=150_000, motivation_score=45.0,
        is_contactable=True, comp_count=1, comp_source="assessed_proxy",
    )
    without_price, _ = grade_lead(**common)
    asking_below_max, _ = grade_lead(**common, asking_price=140_000)
    asking_way_above, _ = grade_lead(**common, asking_price=300_000)
    # A seller already under our max offer is the most attractive of the three.
    assert asking_below_max > without_price
    assert asking_below_max > asking_way_above


# ── Full analysis ──────────────────────────────────────────────────────
def test_max_offer_follows_the_70_percent_rule():
    result = analyze(base_lead(), comps())
    expected = result.arv * MA_PERCENT - result.repair_estimate - result.assignment_fee
    assert result.max_offer == pytest.approx(expected, abs=200)


def test_analysis_reports_no_comps_warning():
    result = analyze(base_lead(tax_assessed_value=None))
    assert result.arv == 0
    assert result.grade == "F"
    assert any("ARV" in w for w in result.warnings)


def test_analysis_flags_uncontactable():
    result = analyze(base_lead(owner_phone=None, owner_email=None), comps())
    assert result.grade == "F"
    assert any("skip traced" in w for w in result.warnings)


def test_spread_is_fee_when_no_asking_price():
    """Without an asking price the spread carries no information, so we report
    the expected fee rather than a mathematically circular number."""
    result = analyze(base_lead(), comps())
    assert result.projected_spread == result.assignment_fee


def test_spread_is_gap_above_max_offer():
    """projected_spread = asking - max_offer. Positive means the seller wants
    more than we can pay (negotiation needed); negative means they are already
    inside our number (act immediately)."""
    above = analyze(base_lead(asking_price=400_000), comps())
    assert above.projected_spread > 0
    below = analyze(base_lead(asking_price=100_000), comps())
    assert below.projected_spread < 0
