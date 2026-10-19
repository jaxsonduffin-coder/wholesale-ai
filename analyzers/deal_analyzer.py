"""
Deal Analyzer Engine.
Calculates ARV, repair estimates, max offer, and grades each deal.
Uses the 50% Rule: Max Offer = ARV × 0.50 − Repairs − Assignment Fee
"""
import logging
import re
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass, field
from config.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class CompData:
    """A comparable sale."""
    address: str = ""
    sale_price: float = 0
    sale_date: str = ""
    sqft: int = 0
    bedrooms: int = 0
    bathrooms: float = 0
    price_per_sqft: float = 0
    distance_miles: float = 0
    source: str = ""


@dataclass
class DealAnalysis:
    """Complete deal analysis results."""
    # Property
    address: str = ""
    property_type: str = ""
    sqft: int = 0
    bedrooms: int = 0
    bathrooms: float = 0
    year_built: int = 0

    # Valuation
    arv: float = 0                     # After Repair Value
    comp_count: int = 0
    comp_avg_price: float = 0
    comp_avg_price_per_sqft: float = 0
    comps: list = field(default_factory=list)

    # Repairs
    repair_level: str = "medium"       # light, medium, heavy
    repair_cost_per_sqft: float = 0
    repair_estimate: float = 0

    # Offer
    max_offer: float = 0              # What we can pay
    assignment_fee: float = 5000.0
    arv_percentage: float = 0.50

    # Scoring
    grade: str = "F"                  # A, B, C, D, F
    score: float = 0                  # 0-100
    motivation_score: float = 0       # Seller motivation 0-100
    profit_potential: float = 0

    # Flags
    is_deal: bool = False
    reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


class DealAnalyzer:
    """Analyzes properties to determine if they're good wholesale deals."""

    def __init__(self):
        self.config = settings.deal_analysis
        self.buy_box = settings.buy_box

    def analyze(self, property_data: dict, comps: list[dict] = None) -> DealAnalysis:
        """
        Full deal analysis on a property.

        Args:
            property_data: Dict with property details from scraper
            comps: List of comparable sales (if already fetched)

        Returns:
            DealAnalysis with all calculations
        """
        analysis = DealAnalysis(
            address=f"{property_data.get('street', '')} {property_data.get('city', '')}, {property_data.get('state', 'TX')}",
            property_type=property_data.get("property_type", "single_family"),
            sqft=property_data.get("sqft") or 0,
            bedrooms=property_data.get("bedrooms") or 0,
            bathrooms=property_data.get("bathrooms") or 0,
            year_built=property_data.get("year_built") or 0,
            assignment_fee=self.buy_box.min_assignment_fee,
            arv_percentage=self.buy_box.max_arv_percentage,
        )

        # Step 1: Calculate ARV from comps
        if comps:
            analysis = self._calculate_arv(analysis, comps)
        elif property_data.get("tax_assessed_value"):
            # Rough ARV from tax assessed value (usually 80-90% of market)
            tax_val = float(property_data["tax_assessed_value"])
            analysis.arv = tax_val * 1.15  # Tax value is usually ~85% of market
            analysis.comp_count = 0
            analysis.warnings.append("ARV estimated from tax value — needs comps")

        # Step 2: Estimate repairs
        analysis = self._estimate_repairs(analysis, property_data)

        # Step 3: Calculate max offer
        analysis = self._calculate_max_offer(analysis)

        # Step 4: Calculate motivation score
        analysis = self._calculate_motivation(analysis, property_data)

        # Step 5: Grade the deal
        analysis = self._grade_deal(analysis, property_data)

        return analysis

    def _calculate_arv(self, analysis: DealAnalysis, comps: list[dict]) -> DealAnalysis:
        """Calculate After Repair Value from comparable sales."""
        valid_comps = []

        for comp in comps:
            try:
                price = float(comp.get("sale_price", 0))
                sqft = int(comp.get("sqft", 0))
                if price > 0 and sqft > 0:
                    valid_comps.append(CompData(
                        address=comp.get("address", ""),
                        sale_price=price,
                        sale_date=comp.get("sale_date", ""),
                        sqft=sqft,
                        bedrooms=int(comp.get("bedrooms", 0)),
                        bathrooms=float(comp.get("bathrooms", 0)),
                        price_per_sqft=price / sqft,
                        distance_miles=float(comp.get("distance_miles", 0)),
                        source=comp.get("source", ""),
                    ))
            except (ValueError, TypeError):
                continue

        if not valid_comps:
            analysis.warnings.append("No valid comps found")
            return analysis

        # Sort by relevance: prefer same bed count, closer distance, more recent
        valid_comps.sort(key=lambda c: (
            abs(c.bedrooms - analysis.bedrooms),
            c.distance_miles,
        ))

        # Take top comps (max 6)
        top_comps = valid_comps[:6]
        analysis.comps = [vars(c) for c in top_comps]
        analysis.comp_count = len(top_comps)

        # Calculate ARV
        if analysis.sqft > 0:
            # Price-per-sqft method (more accurate)
            avg_ppsf = sum(c.price_per_sqft for c in top_comps) / len(top_comps)
            analysis.arv = avg_ppsf * analysis.sqft
            analysis.comp_avg_price_per_sqft = avg_ppsf
        else:
            # Straight average of comp prices
            analysis.arv = sum(c.sale_price for c in top_comps) / len(top_comps)

        analysis.comp_avg_price = sum(c.sale_price for c in top_comps) / len(top_comps)

        if len(top_comps) < self.config.min_comps:
            analysis.warnings.append(f"Only {len(top_comps)} comps (need {self.config.min_comps}+)")

        return analysis

    def _estimate_repairs(self, analysis: DealAnalysis, prop: dict) -> DealAnalysis:
        """Estimate repair costs based on property condition indicators."""
        sqft = analysis.sqft or 1200  # Default if unknown

        # Determine repair level from property signals
        repair_signals = 0

        # Age-based
        if analysis.year_built:
            age = datetime.now().year - analysis.year_built
            if age > 50:
                repair_signals += 3
            elif age > 30:
                repair_signals += 2
            elif age > 15:
                repair_signals += 1

        # Distress indicators
        if prop.get("tax_delinquent"):
            repair_signals += 2
        if prop.get("vacant"):
            repair_signals += 3
        if prop.get("code_violations"):
            repair_signals += 3
        if prop.get("pre_foreclosure"):
            repair_signals += 1

        # Source-based (Craigslist "fixer upper" etc.)
        source = prop.get("source", "").lower()
        if "craigslist" in source:
            repair_signals += 1

        # Determine level
        if repair_signals >= 5:
            analysis.repair_level = "heavy"
            analysis.repair_cost_per_sqft = self.config.repair_cost_per_sqft_heavy
        elif repair_signals >= 3:
            analysis.repair_level = "medium"
            analysis.repair_cost_per_sqft = self.config.repair_cost_per_sqft_medium
        else:
            analysis.repair_level = "light"
            analysis.repair_cost_per_sqft = self.config.repair_cost_per_sqft_light

        analysis.repair_estimate = analysis.repair_cost_per_sqft * sqft

        return analysis

    def _calculate_max_offer(self, analysis: DealAnalysis) -> DealAnalysis:
        """
        Calculate maximum offer using the 50% Rule:
        Max Offer = ARV × 0.50 − Repairs − Assignment Fee
        """
        if analysis.arv <= 0:
            analysis.max_offer = 0
            analysis.warnings.append("Cannot calculate offer — no ARV")
            return analysis

        analysis.max_offer = (
            analysis.arv * analysis.arv_percentage
            - analysis.repair_estimate
            - analysis.assignment_fee
        )

        analysis.profit_potential = (
            analysis.arv
            - analysis.max_offer
            - analysis.repair_estimate
        )

        if analysis.max_offer < 0:
            analysis.max_offer = 0
            analysis.warnings.append("Deal is underwater — repairs exceed margin")

        return analysis

    def _calculate_motivation(self, analysis: DealAnalysis, prop: dict) -> DealAnalysis:
        """Score seller motivation from 0-100."""
        score = 0

        # High-motivation indicators
        if prop.get("tax_delinquent"):
            score += 25
            amount = prop.get("tax_delinquent_amount", 0)
            if amount and float(amount) > 5000:
                score += 10
        if prop.get("pre_foreclosure"):
            score += 30
        if prop.get("probate"):
            score += 20
        if prop.get("vacant"):
            score += 20
        if prop.get("code_violations"):
            score += 15

        # Source-based motivation
        source = prop.get("source", "").lower()
        if "craigslist" in source:
            score += 10  # FSBO on Craigslist = motivated
        if "fsbo" in source:
            score += 10
        if "foreclosure" in source:
            score += 25

        # Absentee owner (mailing address != property address)
        if prop.get("owner_mailing_address"):
            mail_addr = prop.get("owner_mailing_address", "").lower()
            prop_addr = prop.get("street", "").lower()
            if mail_addr and prop_addr and mail_addr != prop_addr:
                score += 15  # Absentee owner

        analysis.motivation_score = min(score, 100)
        return analysis

    def _grade_deal(self, analysis: DealAnalysis, prop: dict) -> DealAnalysis:
        """Grade the deal A through F."""
        score = 0

        # Must have positive max offer
        if analysis.max_offer <= 0:
            analysis.grade = "F"
            analysis.score = 0
            analysis.is_deal = False
            analysis.reasons.append("No viable offer price")
            return analysis

        # Profit potential (40 points max)
        if analysis.profit_potential >= 30000:
            score += 40
        elif analysis.profit_potential >= 20000:
            score += 30
        elif analysis.profit_potential >= 10000:
            score += 20
        elif analysis.profit_potential >= 5000:
            score += 10

        # Assignment fee viability (20 points max)
        if analysis.assignment_fee >= 10000:
            score += 20
        elif analysis.assignment_fee >= 5000:
            score += 15
        elif analysis.assignment_fee >= 3000:
            score += 5

        # Motivation (20 points max)
        score += int(analysis.motivation_score * 0.2)

        # Comp confidence (10 points max)
        if analysis.comp_count >= 5:
            score += 10
        elif analysis.comp_count >= 3:
            score += 7
        elif analysis.comp_count >= 1:
            score += 3

        # ARV range check (10 points max)
        if self.buy_box.min_arv <= analysis.arv <= self.buy_box.max_arv:
            score += 10

        analysis.score = min(score, 100)

        # Assign grade
        if score >= 80:
            analysis.grade = "A"
            analysis.is_deal = True
            analysis.reasons.append("Hot deal — auto-pursue")
        elif score >= 60:
            analysis.grade = "B"
            analysis.is_deal = True
            analysis.reasons.append("Good deal — worth pursuing")
        elif score >= 40:
            analysis.grade = "C"
            analysis.is_deal = True
            analysis.reasons.append("Marginal — review before pursuing")
        elif score >= 20:
            analysis.grade = "D"
            analysis.is_deal = False
            analysis.reasons.append("Weak deal — likely skip")
        else:
            analysis.grade = "F"
            analysis.is_deal = False
            analysis.reasons.append("No deal")

        return analysis

    def format_analysis(self, analysis: DealAnalysis, language: str = "en") -> str:
        """Format analysis as readable text (English or Spanish)."""
        if language == "es":
            return self._format_spanish(analysis)
        return self._format_english(analysis)

    def _format_english(self, a: DealAnalysis) -> str:
        return f"""
━━━ DEAL ANALYSIS ━━━━━━━━━━━━━━━━━━━━━
📍 {a.address}
🏠 {a.property_type} | {a.bedrooms}bd/{a.bathrooms}ba | {a.sqft:,} sqft | Built {a.year_built}

💰 ARV (After Repair Value): ${a.arv:,.0f}
🔧 Repairs ({a.repair_level}): ${a.repair_estimate:,.0f}
📊 Max Offer (50% Rule): ${a.max_offer:,.0f}
💵 Assignment Fee: ${a.assignment_fee:,.0f}
📈 Profit Potential: ${a.profit_potential:,.0f}

📋 Comps: {a.comp_count} found | Avg ${a.comp_avg_price:,.0f}
🎯 Motivation Score: {a.motivation_score:.0f}/100
⭐ Deal Grade: {a.grade} ({a.score:.0f}/100)

{"✅ " + " | ".join(a.reasons) if a.reasons else ""}
{"⚠️ " + " | ".join(a.warnings) if a.warnings else ""}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

    def _format_spanish(self, a: DealAnalysis) -> str:
        repair_level_es = {"light": "ligera", "medium": "media", "heavy": "pesada"}
        return f"""
━━━ ANÁLISIS DE NEGOCIO ━━━━━━━━━━━━━━━
📍 {a.address}
🏠 {a.property_type} | {a.bedrooms}rec/{a.bathrooms}baño | {a.sqft:,} pies² | Construido {a.year_built}

💰 Valor Después de Reparación (ARV): ${a.arv:,.0f}
🔧 Reparaciones ({repair_level_es.get(a.repair_level, a.repair_level)}): ${a.repair_estimate:,.0f}
📊 Oferta Máxima (Regla 50%): ${a.max_offer:,.0f}
💵 Tarifa de Asignación: ${a.assignment_fee:,.0f}
📈 Potencial de Ganancia: ${a.profit_potential:,.0f}

📋 Comparables: {a.comp_count} encontrados | Promedio ${a.comp_avg_price:,.0f}
🎯 Puntuación de Motivación: {a.motivation_score:.0f}/100
⭐ Calificación: {a.grade} ({a.score:.0f}/100)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
