"""The pipeline: ingest -> validate -> dedupe -> analyse -> grade -> enroll.

Each stage is a separate function so it can be tested and re-run in isolation.
The ingest stage is the only place that talks to a lead provider; the
validation stage is the only place that decides whether a record is real.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import cadence as cadence_engine
from .models import Comp, Lead, PipelineRun, RejectedLead, utcnow
from .providers.leads import get_provider
from .scoring import Comp as CompRecord
from .scoring import analyze
from .validation import ValidationError, validate_lead


@dataclass
class IngestStats:
    fetched: int = 0
    accepted: int = 0
    rejected: int = 0
    duplicates: int = 0
    rejected_reasons: dict[str, int] = field(default_factory=dict)
    enrolled: int = 0
    sample_rejects: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "fetched": self.fetched,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "duplicates": self.duplicates,
            "rejected_reasons": self.rejected_reasons,
            "enrolled": self.enrolled,
            "sample_rejects": self.sample_rejects[:10],
        }


def _reason_bucket(reason: str) -> str:
    r = reason.lower()
    if "no house number" in r:
        return "not a situs address (no house number)"
    if "boilerplate" in r:
        return "navigation/page chrome"
    if "gibberish" in r:
        return "gibberish text"
    if "missing city" in r or "missing street" in r:
        return "incomplete record"
    if "invalid state" in r:
        return "invalid state"
    if "city" in r:
        return "bad city field"
    if "gibberish" in r or "placeholder" in r:
        return "gibberish/placeholder"
    return "other"


def ingest_records(
    db: Session, raw_records: list[dict], *, source: str
) -> IngestStats:
    """Validate, dedupe and store a batch of raw provider records."""
    stats = IngestStats(fetched=len(raw_records))

    for raw in raw_records:
        try:
            clean = validate_lead(raw)
        except ValidationError as e:
            stats.rejected += 1
            reason = str(e)
            bucket = _reason_bucket(reason)
            stats.rejected_reasons[bucket] = stats.rejected_reasons.get(bucket, 0) + 1
            if len(stats.sample_rejects) < 10:
                stats.sample_rejects.append(
                    {
                        "street": (raw.get("street") or raw.get("address") or "")[:80],
                        "city": (raw.get("city") or "")[:40],
                        "reason": reason,
                    }
                )
            db.add(
                RejectedLead(
                    source=source,
                    raw={k: str(v)[:200] for k, v in raw.items() if not k.startswith("_")},
                    reason=reason[:255],
                )
            )
            continue

        existing = db.scalar(select(Lead).where(Lead.address_key == clean["address_key"]))
        if existing:
            stats.duplicates += 1
            # Enrich the existing row with anything new we just learned.
            for k, v in clean.items():
                if v not in (None, "", 0, False) and not getattr(existing, k, None):
                    setattr(existing, k, v)
            existing.updated_at = utcnow()
            continue

        lead = Lead(**clean)
        db.add(lead)
        db.flush()  # assigns lead.id so comps can reference it

        # Store any comps that arrived with the lead (demo provider, or a
        # provider that bundles comp data alongside the property record).
        for comp_data in (raw.get("_comps") or []):
            import datetime as _dt
            sold_price = float(comp_data.get("sold_price") or 0)
            comp_sqft = int(comp_data.get("sqft") or 0)
            sold_days_ago = int(comp_data.get("sold_days_ago") or 90)
            db.add(Comp(
                lead_id=lead.id,
                address=str(comp_data.get("address") or "")[:255],
                sold_price=sold_price,
                sqft=comp_sqft,
                sold_date=utcnow() - _dt.timedelta(days=sold_days_ago),
                distance_miles=float(comp_data.get("distance_miles") or 0.5),
                price_per_sqft=round(sold_price / comp_sqft, 2) if comp_sqft else 0.0,
                source=source,
            ))

        stats.accepted += 1

    db.flush()
    return stats


def fetch_and_ingest(db: Session, *, city: str, state: str, limit: int = 100) -> IngestStats:
    provider = get_provider()
    raw = provider.fetch(city=city, state=state, limit=limit)
    return ingest_records(db, raw, source=provider.name)


def skip_trace_missing(db: Session, limit: int = 100) -> int:
    """Enrich leads that have an address but no phone. No-op for the demo
    provider unless a real provider is configured."""
    provider = get_provider()
    if not hasattr(provider, "skip_trace"):
        return 0
    targets = db.scalars(
        select(Lead)
        .where(Lead.owner_phone.is_(None), Lead.status == "new")
        .limit(limit)
    ).all()
    if not targets:
        return 0
    try:
        results = provider.skip_trace(
            [{"street": l.street, "city": l.city, "state": l.state,
              "zip_code": l.zip_code, "owner_name": l.owner_name} for l in targets]
        )
    except Exception:
        return 0
    enriched = 0
    for lead, data in zip(targets, results):
        if not data:
            continue
        from .validation import normalize_email, normalize_phone

        phone = normalize_phone(data.get("owner_phone"))
        email = normalize_email(data.get("owner_email"))
        if phone or email:
            lead.owner_phone = phone or lead.owner_phone
            lead.owner_email = email or lead.owner_email
            lead.owner_phone_2 = normalize_phone(data.get("owner_phone_2")) or lead.owner_phone_2
            lead.last_enriched_at = utcnow()
            lead.enrichment_attempts += 1
            enriched += 1
    db.flush()
    return enriched


def analyze_lead(db: Session, lead: Lead) -> None:
    """Run the analysis pass and persist the result."""
    comps = db.scalars(select(Comp).where(Comp.lead_id == lead.id)).all()
    comp_records = [
        CompRecord(
            sold_price=c.sold_price,
            sqft=c.sqft,
            sold_days_ago=(
                (utcnow() - c.sold_date).days if c.sold_date else 90
            ),
            distance_miles=c.distance_miles or 0.5,
            address=c.address,
        )
        for c in comps
    ]
    result = analyze(
        {
            "sqft": lead.sqft,
            "year_built": lead.year_built,
            "tax_assessed_value": lead.tax_assessed_value,
            "asking_price": lead.asking_price,
            "assignment_fee": lead.assignment_fee,
            "repair_level": lead.repair_level,
            "owner_phone": lead.owner_phone,
            "owner_email": lead.owner_email,
            "pre_foreclosure": lead.pre_foreclosure,
            "tax_delinquent": lead.tax_delinquent,
            "tax_delinquent_amount": lead.tax_delinquent_amount,
            "probate_or_inherited": lead.probate_or_inherited,
            "vacant": lead.vacant,
            "code_violations": lead.code_violations,
            "tired_landlord": lead.tired_landlord,
            "is_absentee": lead.is_absentee,
            "is_out_of_state": lead.is_out_of_state,
            "years_owned": lead.years_owned,
            "equity_pct": lead.equity_pct,
            "status": lead.status,
        },
        comp_records,
    )
    lead.arv = result.arv
    lead.comp_count = result.comp_count
    lead.comp_source = result.comp_source
    lead.repair_estimate = result.repair_estimate
    lead.repair_level = result.repair_level
    lead.max_offer = result.max_offer
    lead.assignment_fee = result.assignment_fee
    lead.projected_spread = result.projected_spread
    lead.motivation_score = result.motivation_score
    lead.motivation_reasons = result.motivation_reasons
    lead.score = result.score
    lead.grade = result.grade
    lead.analyzed_at = utcnow()

    # GUARD: a lead that cannot be worked is not a deal. v1 marked these
    # "under_contract". Reset impossible states to reality.
    if not lead.is_contactable and lead.status in ("under_contract", "signed", "sent_to_jv"):
        lead.status = "new"
        lead.contract_signed = False
    if lead.arv <= 0 and lead.status in ("under_contract", "signed", "sent_to_jv"):
        lead.status = "new"
        lead.contract_signed = False


def analyze_all(db: Session, limit: int = 500) -> int:
    leads = db.scalars(
        select(Lead)
        .where(Lead.status.notin_(["closed", "dead"]))
        .order_by(Lead.analyzed_at.is_(None).desc(), Lead.id)
        .limit(limit)
    ).all()
    for lead in leads:
        analyze_lead(db, lead)
    db.flush()
    return len(leads)


def enroll_workable(db: Session, grades: tuple[str, ...] = ("A", "B"), limit: int = 200) -> int:
    leads = db.scalars(
        select(Lead)
        .where(
            Lead.grade.in_(grades),
            Lead.status == "new",
            Lead.opted_out.is_(False),
            Lead.dnc_listed.is_(False),
        )
        .limit(limit)
    ).all()
    count = 0
    for lead in leads:
        if not lead.can_be_worked:
            continue
        if cadence_engine.enroll(db, lead):
            count += 1
    db.flush()
    return count


def market_pulls(states: list[str] | None, cities_raw: str = "") -> list[tuple[str, str]]:
    """Work out which (city, state) pairs to pull this cycle.

    MARKET_STATES holds the states; MARKET_CITIES can optionally name cities
    per state ("TX:Dallas,Houston;FL:Tampa"). Anything not specified falls back
    to a sane default per state, so adding a market needs one env var, not code.
    """
    from .config import settings as _s

    states = states or _s.market_states or ["TX"]
    default_cities = {
        "TX": "Dallas", "FL": "Tampa", "GA": "Atlanta", "AZ": "Phoenix",
        "TN": "Nashville", "NC": "Charlotte", "SC": "Columbia", "AL": "Birmingham",
        "MS": "Jackson", "LA": "Baton Rouge", "OK": "Oklahoma City",
        "MO": "Kansas City", "KS": "Wichita", "AR": "Little Rock",
        "OH": "Columbus", "IN": "Indianapolis", "KY": "Louisville",
        "MI": "Detroit", "PA": "Philadelphia", "NY": "Buffalo",
        "NJ": "Newark", "VA": "Richmond", "WV": "Charleston",
        "NM": "Albuquerque", "CO": "Colorado Springs", "UT": "Salt Lake City",
        "NV": "Las Vegas", "CA": "Fresno", "OR": "Portland", "WA": "Tacoma",
        "ID": "Boise", "MT": "Billings", "WY": "Cheyenne", "ND": "Fargo",
        "SD": "Sioux Falls", "NE": "Omaha", "IA": "Des Moines",
        "MN": "Minneapolis", "WI": "Milwaukee", "IL": "Peoria",
        "MD": "Baltimore", "DE": "Wilmington", "CT": "Hartford",
        "MA": "Springfield", "RI": "Providence", "NH": "Manchester",
        "ME": "Portland", "VT": "Burlington", "HI": "Honolulu", "AK": "Anchorage",
    }

    overrides: dict[str, list[str]] = {}
    for chunk in (cities_raw or "").split(";"):
        if ":" not in chunk:
            continue
        st, cities = chunk.split(":", 1)
        overrides[st.strip().upper()] = [
            c.strip() for c in cities.split(",") if c.strip()
        ]

    pulls: list[tuple[str, str]] = []
    for st in states:
        st = st.upper()
        for city in overrides.get(st) or [default_cities.get(st, "Dallas")]:
            pulls.append((city, st))
    return pulls


def run_full_cycle(
    db: Session,
    *,
    trigger: str = "manual",
    city: str | None = None,
    state: str | None = None,
    states: list[str] | None = None,
    limit: int = 100,
) -> PipelineRun:
    """One full pass: fetch, validate, analyse, enroll, then send due steps.

    With no explicit city/state this iterates every configured market, splitting
    the per-cycle limit across them, so one env var adds a state.
    """
    from .config import settings as _s

    if city and state:
        pulls = [(city, state)]
    else:
        pulls = market_pulls(states or _s.market_states, _s.market_cities_raw)
    per_market = max(10, limit // max(1, len(pulls)))

    run = PipelineRun(trigger=trigger, status="running", stats={})
    db.add(run)
    db.commit()

    try:
        ingest = IngestStats()
        per_market_stats: dict[str, dict] = {}
        for pull_city, pull_state in pulls:
            part = fetch_and_ingest(
                db, city=pull_city, state=pull_state, limit=per_market
            )
            per_market_stats[f"{pull_city}, {pull_state}"] = part.to_dict()
            ingest.fetched += part.fetched
            ingest.accepted += part.accepted
            ingest.rejected += part.rejected
            ingest.duplicates += part.duplicates
            for k, v in part.rejected_reasons.items():
                ingest.rejected_reasons[k] = ingest.rejected_reasons.get(k, 0) + v
            ingest.sample_rejects.extend(part.sample_rejects[:3])
        db.commit()

        enriched = skip_trace_missing(db)
        analyzed = analyze_all(db)
        db.commit()

        enrolled = enroll_workable(db)
        db.commit()

        send_stats = cadence_engine.process_due(db)

        run.stats = {
            "ingest": ingest.to_dict(),
            "markets": per_market_stats,
            "enriched": enriched,
            "analyzed": analyzed,
            "enrolled": enrolled,
            "outreach": send_stats,
        }
        run.status = "completed"
        run.finished_at = utcnow()
        db.commit()
        return run
    except Exception as e:
        db.rollback()
        run = db.get(PipelineRun, run.id) or run
        run.status = "failed"
        run.error = f"{type(e).__name__}: {e}"[:2000]
        run.finished_at = utcnow()
        db.commit()
        return run
