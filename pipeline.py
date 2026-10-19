"""
Main Pipeline — The 24/7 Automation Engine.
Orchestrates: Scrape → Skip Trace → Analyze → Contact → Negotiate → Contract → JV Forward.
Runs on a loop with configurable intervals.
"""
import asyncio
import logging
import random
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import os

from config.settings import settings
from utils.database import (
    init_db, SessionLocal, Property, Owner, Deal, ContactLog, SMSDrip, EmailCampaign,
)
from scrapers.texas_tax_scraper import TexasTaxScraper
from scrapers.listing_scraper import ZillowFSBOScraper, CraigslistScraper, PreForeclosureScraper
from scrapers.auction_scraper import (
    AuctionDotComScraper, HUDHomesScraper, ProbateScraper,
    CodeViolationScraper, HomepathScraper, FreddieMacScraper,
)
from skip_trace.finder import SkipTracer
from analyzers.deal_analyzer import DealAnalyzer
from voice_agent.caller import VapiVoiceAgent
from voice_agent.telnyx_caller import TelnyxOutboundAgent
from voice_agent.sms_campaign import SMSCampaignManager
from voice_agent.email_campaign import EmailCampaignManager
from contracts.generator import ContractGenerator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("pipeline")


class WholesalePipeline:
    """The main automation pipeline that runs 24/7."""

    def __init__(self):
        self.analyzer = DealAnalyzer()
        self.contract_gen = ContractGenerator()
        self.sms_manager = SMSCampaignManager()
        self.email_manager = EmailCampaignManager()
        self.voice_agent = VapiVoiceAgent()       # Inbound calls
        self.outbound_agent = TelnyxOutboundAgent()  # Outbound calls

        # Track Vapi assistants (inbound)
        self.vapi_assistant_en = None
        self.vapi_assistant_es = None

    async def run_forever(self):
        """
        Continuous pipeline — always finding leads.

        Schedule:
        - Every 1 hour: Craigslist + Pre-foreclosure (fast, rarely block)
        - Every 2 hours: County tax records + Code violations
        - Every 4 hours: Zillow FSBO, Auction.com, HUD, HomePath, Freddie, Probate
        - Every cycle: Skip trace, analyze, outreach, drips (process whatever's new)
        """
        logger.info("🚀 Wholesale AI Pipeline starting (continuous mode)...")
        init_db()
        logger.info("✅ Database initialized")

        # Create Vapi assistants
        if settings.vapi.api_key:
            self.vapi_assistant_en = await self.voice_agent.create_assistant("en")
            self.vapi_assistant_es = await self.voice_agent.create_assistant("es")

        cycle_count = 0

        while True:
            try:
                cycle_count += 1
                logger.info(f"━━━ Pipeline cycle #{cycle_count} starting ━━━")

                # ── Scraping tiers — stagger to avoid detection ──

                # TIER 1: Every cycle (hourly) — fast, bot-friendly sources
                logger.info("🔍 Tier 1: Craigslist + Pre-foreclosure...")
                await self.phase_scrape_tier1()

                # TIER 2: Every cycle — county records (user wants max county data)
                logger.info("🔍 Tier 2: County tax records + Code violations...")
                await self.phase_scrape_tier2()

                # TIER 3: Every 4 cycles (~4 hours) — sites that block more aggressively
                if cycle_count % 4 == 0:
                    logger.info("🔍 Tier 3: Zillow, Auction, HUD, HomePath, Freddie, Probate...")
                    await self.phase_scrape_tier3()

                # ── Always process whatever leads we have ──
                await self.phase_skip_trace()
                await self.phase_analyze()
                await self.phase_outreach()
                await self.phase_process_responses()
                await self.phase_drip_campaigns()

                logger.info(f"━━━ Cycle #{cycle_count} complete ━━━")
                # Wait 1 hour between cycles
                wait = 3600 + random.randint(0, 300)  # 60-65 min (random to avoid patterns)
                logger.info(f"Next cycle in ~{wait // 60} minutes...")
                await asyncio.sleep(wait)

            except Exception as e:
                logger.error(f"Pipeline error: {e}", exc_info=True)
                await asyncio.sleep(300)  # Wait 5 min on error

    # ── Scraping (tiered for stealth + continuous leads) ───────────

    def _save_properties(self, all_properties: list[dict]) -> int:
        """Save scraped properties to database, skip duplicates. Returns count saved."""
        db = SessionLocal()
        new_count = 0
        try:
            for prop_data in all_properties:
                parcel_id = prop_data.get("parcel_id")
                if parcel_id:
                    existing = db.query(Property).filter_by(parcel_id=parcel_id).first()
                    if existing:
                        continue

                # Also dedupe by address
                street = prop_data.get("street", "").strip()
                if street:
                    existing = db.query(Property).filter_by(
                        street=street,
                        city=prop_data.get("city", ""),
                        state=prop_data.get("state", "TX"),
                    ).first()
                    if existing:
                        continue

                prop = Property(
                    street=street,
                    city=prop_data.get("city", ""),
                    county=prop_data.get("county", ""),
                    state=prop_data.get("state", "TX"),
                    zip_code=prop_data.get("zip_code", ""),
                    property_type=prop_data.get("property_type"),
                    bedrooms=prop_data.get("bedrooms"),
                    bathrooms=prop_data.get("bathrooms"),
                    sqft=prop_data.get("sqft"),
                    lot_size_sqft=prop_data.get("lot_size_sqft"),
                    year_built=prop_data.get("year_built"),
                    parcel_id=parcel_id,
                    tax_assessed_value=prop_data.get("tax_assessed_value"),
                    tax_delinquent=prop_data.get("tax_delinquent", False),
                    tax_delinquent_amount=prop_data.get("tax_delinquent_amount"),
                    pre_foreclosure=prop_data.get("pre_foreclosure", False),
                    probate=prop_data.get("probate", False),
                    vacant=prop_data.get("vacant", False),
                    source=prop_data.get("source"),
                    source_url=prop_data.get("source_url"),
                    scraped_at=prop_data.get("scraped_at"),
                )
                db.add(prop)

                if prop_data.get("owner_name"):
                    db.flush()
                    owner = Owner(
                        property_id=prop.id,
                        full_name=prop_data["owner_name"],
                        mailing_address=prop_data.get("owner_mailing_address", ""),
                    )
                    db.add(owner)

                new_count += 1

            db.commit()
        except Exception as e:
            logger.error(f"Save error: {e}", exc_info=True)
            db.rollback()
        finally:
            db.close()
        return new_count

    async def _run_scrapers(self, scrapers: dict) -> list[dict]:
        """Run a dict of {name: coroutine} scrapers concurrently, return all properties."""
        results = await asyncio.gather(
            *scrapers.values(), return_exceptions=True
        )
        all_properties = []
        for name, result in zip(scrapers.keys(), results):
            if isinstance(result, Exception):
                logger.error(f"{name} scraper failed: {result}")
            elif isinstance(result, list):
                all_properties.extend(result)
                logger.info(f"  {name}: {len(result)} properties")
        return all_properties

    async def phase_scrape(self):
        """Full scrape — all tiers at once. Used by manual trigger."""
        logger.info("📡 Full scrape (all sources)...")
        await self.phase_scrape_tier1()
        await self.phase_scrape_tier2()
        await self.phase_scrape_tier3()

    async def phase_scrape_tier1(self):
        """TIER 1 (hourly): Craigslist + Pre-foreclosure — fast, rarely blocks."""
        all_counties = settings.buy_box.target_counties

        async def run_craigslist():
            async with CraigslistScraper() as s:
                return await s.scrape()

        async def run_preforeclosure():
            async with PreForeclosureScraper() as s:
                return await s.scrape(counties=all_counties)

        props = await self._run_scrapers({
            "Craigslist": run_craigslist(),
            "Pre-foreclosure": run_preforeclosure(),
        })
        saved = self._save_properties(props)
        logger.info(f"✅ Tier 1: Saved {saved} new leads (from {len(props)} scraped)")

    async def phase_scrape_tier2(self):
        """TIER 2 (every 2 hours): County tax records + code violations."""
        all_counties = settings.buy_box.target_counties

        async def run_tax():
            async with TexasTaxScraper(counties=all_counties) as s:
                return await s.scrape()

        async def run_code_violations():
            async with CodeViolationScraper() as s:
                return await s.scrape()

        props = await self._run_scrapers({
            "Tax delinquent": run_tax(),
            "Code violations": run_code_violations(),
        })
        saved = self._save_properties(props)
        logger.info(f"✅ Tier 2: Saved {saved} new leads (from {len(props)} scraped)")

    async def phase_scrape_tier3(self):
        """TIER 3 (every 4 hours): Zillow, Auction, HUD, HomePath, Freddie, Probate."""
        all_counties = settings.buy_box.target_counties

        async def run_zillow():
            # Randomize city order each time to spread the load
            cities = [
                "Houston-TX", "San-Antonio-TX", "Dallas-TX", "Fort-Worth-TX",
                "Austin-TX", "El-Paso-TX", "Arlington-TX", "Corpus-Christi-TX",
                "Plano-TX", "Laredo-TX", "Lubbock-TX", "Garland-TX",
                "Irving-TX", "Amarillo-TX", "Grand-Prairie-TX",
                "Brownsville-TX", "McKinney-TX", "Frisco-TX",
                "Pasadena-TX", "Mesquite-TX", "McAllen-TX",
                "Round-Rock-TX", "Sugar-Land-TX", "Denton-TX",
                "Killeen-TX", "Midland-TX", "Odessa-TX",
                "Beaumont-TX", "Conroe-TX", "Pearland-TX",
                "Waco-TX", "Tyler-TX", "Wichita-Falls-TX",
                "San-Angelo-TX", "Galveston-TX", "San-Marcos-TX",
                "Temple-TX", "Abilene-TX", "Lewisville-TX",
                "Allen-TX", "Edinburg-TX", "Pflugerville-TX",
            ]
            random.shuffle(cities)
            async with ZillowFSBOScraper() as s:
                return await s.scrape(cities=cities)

        async def run_auction():
            async with AuctionDotComScraper() as s:
                return await s.scrape()

        async def run_hud():
            async with HUDHomesScraper() as s:
                return await s.scrape()

        async def run_probate():
            async with ProbateScraper() as s:
                return await s.scrape(counties=all_counties)

        async def run_homepath():
            async with HomepathScraper() as s:
                return await s.scrape()

        async def run_freddiemac():
            async with FreddieMacScraper() as s:
                return await s.scrape()

        props = await self._run_scrapers({
            "Zillow FSBO": run_zillow(),
            "Auction.com": run_auction(),
            "HUD Homes": run_hud(),
            "Probate": run_probate(),
            "HomePath": run_homepath(),
            "HomeSteps": run_freddiemac(),
        })
        saved = self._save_properties(props)
        logger.info(f"✅ Tier 3: Saved {saved} new leads (from {len(props)} scraped)")

    # ── Phase 2: Skip Trace ──────────────────────────────────────

    async def phase_skip_trace(self):
        """Find contact info for owners without it."""
        logger.info("🔍 Phase 2: Skip tracing...")
        db = SessionLocal()

        try:
            # Find owners without contact info
            owners = (
                db.query(Owner)
                .filter(
                    Owner.phone_1.is_(None),
                    Owner.email_1.is_(None),
                    Owner.full_name.isnot(None),
                )
                .limit(50)  # Process 50 per cycle
                .all()
            )

            logger.info(f"Skip tracing {len(owners)} owners...")

            async with SkipTracer() as tracer:
                for owner in owners:
                    if not owner.full_name:
                        continue

                    prop = owner.property
                    city = prop.city if prop else ""

                    result = await tracer.trace_owner(owner.full_name, city, "TX")

                    if result["phones"]:
                        owner.phone_1 = result["phones"][0]
                        if len(result["phones"]) > 1:
                            owner.phone_2 = result["phones"][1]
                        owner.phone_verified = True

                    if result["emails"]:
                        owner.email_1 = result["emails"][0]
                        if len(result["emails"]) > 1:
                            owner.email_2 = result["emails"][1]
                        owner.email_verified = True

                    owner.preferred_language = result.get("preferred_language", "en")
                    owner.skip_trace_source = ", ".join(result.get("sources", []))
                    owner.skip_traced_at = datetime.utcnow()

            db.commit()
            logger.info(f"✅ Skip traced {len(owners)} owners")

        except Exception as e:
            logger.error(f"Skip trace error: {e}", exc_info=True)
            db.rollback()
        finally:
            db.close()

    # ── Phase 3: Analyze ─────────────────────────────────────────

    async def phase_analyze(self):
        """Run deal analysis on properties without deals."""
        logger.info("📊 Phase 3: Analyzing deals...")
        db = SessionLocal()

        try:
            # Find properties without deal analysis
            properties = (
                db.query(Property)
                .filter(~Property.deal.has())
                .limit(100)
                .all()
            )

            logger.info(f"Analyzing {len(properties)} properties...")
            grade_counts = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}

            for prop in properties:
                prop_data = {
                    "street": prop.street,
                    "city": prop.city,
                    "county": prop.county,
                    "state": prop.state,
                    "property_type": prop.property_type,
                    "bedrooms": prop.bedrooms,
                    "bathrooms": prop.bathrooms,
                    "sqft": prop.sqft,
                    "year_built": prop.year_built,
                    "tax_assessed_value": prop.tax_assessed_value,
                    "tax_delinquent": prop.tax_delinquent,
                    "tax_delinquent_amount": prop.tax_delinquent_amount,
                    "pre_foreclosure": prop.pre_foreclosure,
                    "probate": prop.probate,
                    "vacant": prop.vacant,
                    "source": prop.source,
                    "owner_mailing_address": prop.owner.mailing_address if prop.owner else "",
                }

                analysis = self.analyzer.analyze(prop_data)

                deal = Deal(
                    property_id=prop.id,
                    arv=analysis.arv,
                    repair_estimate=analysis.repair_estimate,
                    repair_level=analysis.repair_level,
                    max_offer=analysis.max_offer,
                    assignment_fee=analysis.assignment_fee,
                    comp_count=analysis.comp_count,
                    comp_avg_price=analysis.comp_avg_price,
                    comp_data=analysis.comps,
                    grade=analysis.grade,
                    score=analysis.score,
                    motivation_score=analysis.motivation_score,
                    status="new",
                )
                db.add(deal)
                grade_counts[analysis.grade] = grade_counts.get(analysis.grade, 0) + 1

            db.commit()
            logger.info(f"✅ Analyzed {len(properties)} deals: {grade_counts}")

        except Exception as e:
            logger.error(f"Analysis error: {e}", exc_info=True)
            db.rollback()
        finally:
            db.close()

    # ── Phase 4: Outreach ────────────────────────────────────────

    async def phase_outreach(self):
        """Contact owners of A and B grade deals."""
        logger.info("📞 Phase 4: Outreach to motivated sellers...")
        db = SessionLocal()

        try:
            # Get A and B deals that haven't been contacted
            deals = (
                db.query(Deal)
                .join(Property)
                .join(Owner)
                .filter(
                    Deal.grade.in_(["A", "B"]),
                    Deal.status == "new",
                    Owner.phone_1.isnot(None),
                )
                .limit(20)
                .all()
            )

            logger.info(f"Contacting {len(deals)} high-grade leads...")

            for deal in deals:
                prop = deal.property
                owner = prop.owner
                if not owner:
                    continue

                lang = owner.preferred_language or "en"

                # 1. Start SMS drip campaign
                if owner.phone_1:
                    address = f"{prop.street} {prop.city}"
                    sequence = self.sms_manager.create_drip_sequence(
                        owner_name=owner.full_name or "",
                        phone=owner.phone_1,
                        address=address,
                        language=lang,
                    )
                    for sms in sequence:
                        drip = SMSDrip(
                            property_id=prop.id,
                            sequence_number=sms["sequence_number"],
                            language=sms["language"],
                            next_send_at=sms["send_at"],
                            message=sms["message"],
                        )
                        db.add(drip)

                # 2. Start email campaign
                if owner.email_1 and owner.email_verified:
                    address = f"{prop.street} {prop.city}"
                    email_seq = self.email_manager.create_email_sequence(
                        owner_name=owner.full_name or "",
                        email=owner.email_1,
                        address=address,
                        language=lang,
                    )
                    for em in email_seq:
                        campaign = EmailCampaign(
                            property_id=prop.id,
                            sequence_number=em["sequence_number"],
                            language=em["language"],
                            subject=em["subject"],
                            body=em["body"],
                            next_send_at=em["send_at"],
                        )
                        db.add(campaign)

                # 3. Schedule voice call (for A-grade deals only)
                if deal.grade == "A" and owner.phone_1 and self.vapi_assistant_en:
                    assistant_id = self.vapi_assistant_es if lang == "es" else self.vapi_assistant_en
                    prop_info = {
                        "street": prop.street,
                        "city": prop.city,
                        "state": prop.state,
                        "property_type": prop.property_type,
                        "bedrooms": prop.bedrooms,
                        "bathrooms": prop.bathrooms,
                        "sqft": prop.sqft,
                        "year_built": prop.year_built,
                        "owner_name": owner.full_name,
                        "tax_delinquent": prop.tax_delinquent,
                        "pre_foreclosure": prop.pre_foreclosure,
                        "vacant": prop.vacant,
                    }
                    call_result = await self.outbound_agent.make_call(
                        phone_number=owner.phone_1,
                        property_info=prop_info,
                        language=lang,
                    )
                    if call_result:
                        log = ContactLog(
                            property_id=prop.id,
                            method="voice",
                            language=lang,
                            direction="outbound",
                            phone_number=owner.phone_1,
                        )
                        db.add(log)

                deal.status = "contacted"

            db.commit()
            logger.info(f"✅ Outreach complete for {len(deals)} leads")

        except Exception as e:
            logger.error(f"Outreach error: {e}", exc_info=True)
            db.rollback()
        finally:
            db.close()

    # ── Phase 5: Process Responses ───────────────────────────────

    async def phase_process_responses(self):
        """Check for seller responses and generate contracts for interested sellers."""
        logger.info("📋 Phase 5: Processing responses...")
        db = SessionLocal()

        try:
            # Find deals with interested sellers
            interested_deals = (
                db.query(Deal)
                .filter(Deal.status == "interested")
                .all()
            )

            for deal in interested_deals:
                prop = deal.property
                owner = prop.owner
                if not owner:
                    continue

                lang = owner.preferred_language or "en"

                # Generate contract
                prop_data = {
                    "street": prop.street,
                    "city": prop.city,
                    "state": prop.state,
                    "zip_code": prop.zip_code,
                    "county": prop.county,
                    "parcel_id": prop.parcel_id,
                    "property_type": prop.property_type,
                    "sqft": prop.sqft,
                }
                owner_data = {
                    "full_name": owner.full_name,
                    "mailing_address": owner.mailing_address or "",
                    "phone_1": owner.phone_1 or "",
                    "email_1": owner.email_1 or "",
                }
                deal_data = {
                    "max_offer": deal.max_offer,
                    "offered_price": deal.offered_price or deal.max_offer,
                }

                result = self.contract_gen.generate_contract(
                    property_data=prop_data,
                    owner_data=owner_data,
                    deal_data=deal_data,
                    language=lang,
                )

                deal.contract_path = result["contract_path"]
                deal.status = "under_contract"

                # Generate PDF
                pdf_path = self.contract_gen.generate_pdf(result["contract_path"])
                if pdf_path:
                    deal.contract_path = pdf_path

                logger.info(f"Contract generated for {prop.street}")

            db.commit()

        except Exception as e:
            logger.error(f"Response processing error: {e}", exc_info=True)
            db.rollback()
        finally:
            db.close()

    # ── Phase 6: Drip Campaigns ──────────────────────────────────

    async def phase_drip_campaigns(self):
        """Send pending SMS and email drip messages."""
        logger.info("💬 Phase 6: Processing drip campaigns...")
        db = SessionLocal()
        try:
            sms_count = await self.sms_manager.process_pending_messages(db)
            email_count = await self.email_manager.process_pending_emails(db)
            logger.info(f"✅ Sent {sms_count} SMS, {email_count} emails")
        except Exception as e:
            logger.error(f"Drip campaign error: {e}", exc_info=True)
        finally:
            db.close()

    # ── JV Partner Forward ───────────────────────────────────────

    def forward_to_jv_partner(self, deal_id: int) -> bool:
        """
        Forward a signed contract + deal package to JV partner via email.
        Called after buyer signs the contract.
        """
        db = SessionLocal()
        try:
            deal = db.query(Deal).filter_by(id=deal_id).first()
            if not deal or not deal.contract_signed:
                return False

            prop = deal.property
            owner = prop.owner
            lang = owner.preferred_language if owner else "en"

            # Build deal package
            analysis = self.analyzer.analyze({
                "street": prop.street, "city": prop.city, "state": prop.state,
                "property_type": prop.property_type, "bedrooms": prop.bedrooms,
                "bathrooms": prop.bathrooms, "sqft": prop.sqft,
                "year_built": prop.year_built,
                "tax_assessed_value": prop.tax_assessed_value,
                "tax_delinquent": prop.tax_delinquent,
            })
            deal_summary = self.analyzer.format_analysis(analysis)

            # Email to JV partner(s) — primary first, then backup
            jv_primary = settings.contract.jv_partner_email
            jv_backup = settings.contract.jv_backup_email
            jv_targets = []
            if jv_primary:
                jv_targets.append((jv_primary, settings.contract.jv_partner_name))
            if jv_backup:
                jv_targets.append((jv_backup, settings.contract.jv_backup_name))

            if not jv_targets:
                logger.warning("No JV partner email configured")
                return False

            for jv_email, jv_name in jv_targets:
                subject = f"New Deal: {prop.street} {prop.city}, TX — ${deal.accepted_price or deal.max_offer:,.0f}"

                body = f"""Hi {jv_name},

New wholesale deal ready for review!

{deal_summary}

Contract Status: SIGNED ✅
Assignment Fee: ${deal.assignment_fee:,.0f}

The signed purchase agreement is attached.

Property: {prop.street}, {prop.city}, {prop.state} {prop.zip_code}
ARV: ${deal.arv:,.0f}
Offer: ${deal.accepted_price or deal.max_offer:,.0f}
Repairs: ${deal.repair_estimate:,.0f}

Please review and let me know if you'd like to proceed.

Best regards,
{settings.contract.buyer_name}
{settings.contract.buyer_phone}
"""

                self._send_email_with_attachment(
                    to=jv_email,
                    subject=subject,
                    body=body,
                    attachment_path=deal.contract_path,
                )
                logger.info(f"Deal emailed to JV: {jv_name} ({jv_email})")

            deal.sent_to_jv = True
            deal.sent_to_jv_at = datetime.utcnow()
            db.commit()

            logger.info(f"✅ Deal forwarded to JV partner: {prop.street}")
            return True

        except Exception as e:
            logger.error(f"JV forward error: {e}", exc_info=True)
            return False
        finally:
            db.close()

    def _send_email_with_attachment(self, to: str, subject: str, body: str, attachment_path: str):
        """Send email with file attachment."""
        cfg = settings.email
        msg = MIMEMultipart()
        msg["From"] = f"{cfg.from_name} <{cfg.smtp_user}>"
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        if attachment_path and os.path.exists(attachment_path):
            with open(attachment_path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
                encoders.encode_base64(part)
                filename = os.path.basename(attachment_path)
                part.add_header("Content-Disposition", f"attachment; filename={filename}")
                msg.attach(part)

        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port) as server:
            server.starttls()
            server.login(cfg.smtp_user, cfg.smtp_password)
            server.send_message(msg)


# ── Entry Point ──────────────────────────────────────────────────

async def main():
    pipeline = WholesalePipeline()
    await pipeline.run_forever()

if __name__ == "__main__":
    asyncio.run(main())
