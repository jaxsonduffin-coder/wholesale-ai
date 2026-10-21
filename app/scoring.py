"""Deal analysis: ARV from comps, repairs, max offer, motivation, grade.

v1 hardcoded `repair_estimate = 18000` and `assignment_fee = 5000` for every
lead, left ARV at 0, and then reported "3 Under Contract, 1 Signed" on houses
it knew nothing about. The grading function must be *unable* to return a good
grade when ARV is unknown — that is the invariant enforced here.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

# Wholesale offer rule of thumb. 0.70 is standard; tighten to 0.65 in hot
# markets, loosen to 0.75 in slow ones. Exposed as a setting later.
MA_PERCENT = 0.70

# Repair $/sqft by condition tier. These are real numbers, not a flat guess.
REPAIR_PER_SQFT = {
    "light": 18.0,      # cosmetic: paint, carpet, fixtures
    "medium": 38.0,     # kitchen/bath, roof, HVAC
    "heavy": 65.0,      # full gut, structural, systems
}
DEFAULT_SQFT = 1400      # market median fallback for missing sqft
MIN_ASSIGNMENT_FEE = 8_000.0
DEFAULT_ASSIGNMENT_FEE = 12_000.0


@dataclass
class Comp:
    sold_price: float
    sqft: int
    sold_days_ago: int = 90
    distance_miles: float = 0.5
    address: str = ""


@dataclass
class Analysis:
    arv: float
    comp_count: int
    comp_source: str
    repair_estimate: float
    repair_level: str
    max_offer: float
    assignment_fee: float
    projected_spread: float
    motivation_score: float
    motivation_reasons: list[str] = field(default_factory=list)
    score: float = 0.0
    grade: str = "F"
    warnings: list[str] = field(default_factory=list)


def estimate_arv(sqft: int | None, comps: list[Comp], fallback_value: float | None = None) -> tuple[float, int, str, list[str]]:
    """Return (arv, comp_count, source, warnings).

    Prefers real comps. Falls back to the tax-assessed value with a warning,
    because assessed value is usually 70-90% of market in Texas and using it
    blind overstates your offer. Returns ARV 0 when there is nothing credible,
    which downstream code treats as "not analysable".
    """
    warnings: list[str] = []

    usable = [c for c in comps if c.sold_price > 0 and c.sqft > 0]
    if len(usable) >= 3:
        # Weight closer + more recent comps higher.
        weighted: list[tuple[float, float]] = []
        for c in usable:
            w = 1.0
            w *= max(0.35, 1 - (c.distance_miles or 0.5) / 3.0)
            w *= max(0.35, 1 - (c.sold_days_ago or 90) / 365.0)
            weighted.append((c.sold_price / c.sqft, w))
        total_w = sum(w for _, w in weighted) or 1.0
        ppsf = sum(v * w for v, w in weighted) / total_w

        # Guard against one crazy comp dragging the median.
        ppsf_values = sorted(c.sold_price / c.sqft for c in usable)
        med = statistics.median(ppsf_values)
        if med > 0 and not (0.6 <= ppsf / med <= 1.6):
            ppsf = med
            warnings.append("comp $/sqft outlier detected; used median instead")

        if not sqft:
            sqft = int(statistics.median(c.sqft for c in usable))
            warnings.append(f"subject sqft missing; used comp median {sqft}")
        return round(ppsf * sqft, -2), len(usable), "comps", warnings

    if fallback_value and fallback_value > 0:
        warnings.append(
            "no comps available — ARV derived from tax-assessed value "
            "(typically 70-90% of market); treat as provisional"
        )
        if not sqft:
            sqft = DEFAULT_SQFT
            warnings.append(f"subject sqft missing; assumed {DEFAULT_SQFT}")
        # Assessed values in TX sit near 85% of market; scale up.
        return round((fallback_value / 0.85), -2), 0, "assessed_proxy", warnings

    return 0.0, 0, "none", ["no comps and no assessed value — ARV unknown"]


def estimate_repairs(sqft: int | None, level: str, year_built: int | None = None) -> tuple[float, str]:
    """Repair cost from condition tier, with age nudging the tier upward."""
    level = (level or "light").lower()
    if level not in REPAIR_PER_SQFT:
        level = "light"
    if year_built and year_built < 1960 and level == "light":
        level = "medium"          # pre-1960 light flips still need real work
    size = sqft or DEFAULT_SQFT
    return round(REPAIR_PER_SQFT[level] * size, -2), level


def motivation(lead_like: dict) -> tuple[float, list[str]]:
    """Distress-signal score 0-100 with the reasons spelled out.

    Wholesaling works on pain, not on price. A house with no pain at market
    value is not a lead no matter how good the numbers look.
    """
    score = 0.0
    reasons: list[str] = []

    def add(points: float, reason: str) -> None:
        nonlocal score
        score += points
        reasons.append(f"+{points:g} {reason}")

    if lead_like.get("pre_foreclosure"):
        add(28, "pre-foreclosure / notice of default")
    if lead_like.get("tax_delinquent"):
        amt = lead_like.get("tax_delinquent_amount") or 0
        add(24, f"tax delinquent${f' (${amt:,.0f})' if amt else ''}")
    if lead_like.get("probate_or_inherited"):
        add(22, "probate / inherited property")
    if lead_like.get("vacant"):
        add(16, "vacant / likely no occupant")
    if lead_like.get("code_violations"):
        add(12, "open code violations")
    if lead_like.get("tired_landlord"):
        add(12, "long-term landlord / tenant fatigue")
    if lead_like.get("is_absentee"):
        add(14, "absentee owner")
    if lead_like.get("is_out_of_state"):
        add(10, "out-of-state owner")

    years = lead_like.get("years_owned")
    if years and years >= 20:
        add(12, f"owned {years:.0f} years (deep equity)")
    elif years and years >= 10:
        add(8, f"owned {years:.0f} years")

    equity = lead_like.get("equity_pct")
    if equity and equity >= 60:
        add(10, f"{equity:.0f}% equity")
    elif equity and equity >= 35:
        add(5, f"{equity:.0f}% equity")

    # Condition/age pressure
    yb = lead_like.get("year_built")
    if yb and yb <= 1975:
        add(6, f"built {yb} (deferred maintenance likely)")

    return min(100.0, score), reasons


def grade_lead(
    *,
    arv: float,
    max_offer: float,
    motivation_score: float,
    is_contactable: bool,
    comp_count: int,
    asking_price: float | None = None,
    comp_source: str = "comps",
    status: str = "new",
) -> tuple[float, str]:
    """Return (score 0-100, grade A/B/C/F).

    Grading is intentionally conservative, because a grade is a promise about
    how much of your day this lead deserves.

      A  real comps behind the ARV + strong motivation + contactable
      B  usable numbers + some motivation + contactable
      C  contactable and priced, but thin on either motive or evidence
      F  cannot be worked at all (no ARV, no contact path, suppressed)

    INVARIANTS: no ARV -> F. No contact path -> F. Dead/closed -> F.
    This is what stops a dashboard from claiming deals it cannot support.
    """
    if arv <= 0 or max_offer <= 0:
        return 0.0, "F"
    if not is_contactable:
        return 0.0, "F"
    if status in ("dead", "closed"):
        return 0.0, "F"

    # Motivation is the dominant signal — it is what makes a seller say yes.
    score = motivation_score * 0.62

    # Confidence in the ARV. Real comps are worth a lot; a tax-assessed proxy
    # is a guess and earns very little.
    if comp_source == "comps":
        score += {0: 0.0, 1: 8.0, 2: 15.0}.get(comp_count, 22.0)
    elif comp_source == "assessed_proxy":
        score += 6.0

    # Deal viability bonus only when we actually know what the seller wants.
    # (Without an asking price there is nothing to negotiate against, so the
    # "spread" is just our own desired fee and carries no information.)
    if asking_price and asking_price > 0:
        ratio = asking_price / max_offer if max_offer else 99
        if ratio <= 1.0:
            score += 18.0          # already at or below our max — act now
        elif ratio <= 1.15:
            score += 12.0          # small gap, very winnable
        elif ratio <= 1.35:
            score += 5.0

    score = round(min(100.0, score), 1)

    if score >= 58 and comp_count >= 3 and comp_source == "comps":
        grade = "A"
    elif score >= 38:
        grade = "B"
    elif is_contactable:
        grade = "C"
    else:
        grade = "F"
    return score, grade


def analyze(lead_like: dict, comps: list[Comp] | None = None) -> Analysis:
    """Full analysis pass for one lead."""
    comps = comps or []
    sqft = lead_like.get("sqft")
    arv, comp_count, comp_source, warnings = estimate_arv(
        sqft, comps, lead_like.get("tax_assessed_value")
    )

    repair_level = lead_like.get("repair_level") or "light"
    repairs, repair_level = estimate_repairs(sqft, repair_level, lead_like.get("year_built"))

    fee = lead_like.get("assignment_fee") or DEFAULT_ASSIGNMENT_FEE
    fee = max(MIN_ASSIGNMENT_FEE, float(fee))

    is_contactable = bool(lead_like.get("owner_phone") or lead_like.get("owner_email"))
    motive, reasons = motivation(lead_like)

    asking = lead_like.get("asking_price")
    if arv > 0:
        max_offer = round(max(0.0, arv * MA_PERCENT - repairs - fee), -2)
        if asking and asking > 0:
            # The negotiation gap: how far above our max the seller is asking.
            # Negative means they are already under our max — buy it.
            spread = round(asking - max_offer, -2)
        else:
            # No asking price: report the fee we expect to earn, not a fake
            # "spread" that is mathematically identical to our own assumption.
            spread = fee
    else:
        max_offer = 0.0
        spread = 0.0

    score, grade = grade_lead(
        arv=arv,
        max_offer=max_offer,
        motivation_score=motive,
        is_contactable=is_contactable,
        comp_count=comp_count,
        asking_price=asking,
        comp_source=comp_source,
        status=lead_like.get("status", "new"),
    )

    if not is_contactable:
        warnings.append("no phone or email — cannot be worked until skip traced")

    return Analysis(
        arv=arv,
        comp_count=comp_count,
        comp_source=comp_source,
        repair_estimate=repairs,
        repair_level=repair_level,
        max_offer=max_offer,
        assignment_fee=fee,
        projected_spread=spread,
        motivation_score=motive,
        motivation_reasons=reasons,
        score=score,
        grade=grade,
        warnings=warnings,
    )
