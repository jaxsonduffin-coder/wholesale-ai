"""
Main Pipeline — The 24/7 Automation Engine.
Orchestrates: Scrape → Skip Trace → Analyze → Contact → Negotiate → Contract → JV Forward.
Runs on a loop with configurable intervals.
"""
import asyncio
import logging
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
from skip_trace.finder import SkipTracer
from analyzers.deal_analyzer import DealAnalyzer
from voice_agent.caller import VapiVoiceAgent
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
        self.voice_agent = VapiVoiceAgent()

        # Track assistants
        self.vapi_assistant_en = None
        self.vapi_assistant_es = None

    async def run_forever(self):
        """Main loop — runs the full pipeline on a schedule."""
        logger.info("🚀 Wholesale AI Pipeline starting...")
        init_db()
        logger.info("✅ Database initialized")

        # Create Vapi assistants
        if settings.vapi.api_key:
            self.vapi_assistant_en = await self.voice_agent.create_assistant("en")
            self.vapi_assistant_es = await self.voice_agent.create_assistant("es")

        while True:
            try:
                logger.info("━━━ Pipeline cycle starting ━━━")

                # Phase 1: Scrape new leads
                await self.phase_scrape()

                # Phase 2: Skip trace — find contact info
                await self.phase_skip_trace()

                # Phase 3: Analyze deals
                await self.phase_analyze()

                # Phase 4: Outreach — SMS, email, voice
                await self.phase_outreach()

                # Phase 5: Process responses and send contracts
                await self.phase_process_responses()

                # Phase 6: Send pending SMS/email drips
                await self.phase_drip_campaigns()

                logger.info("━━━ Pipeline cycle complete ━━━")
                logger.info("Next cycle in 6 hours...")
                await asyncio.sleep(6 * 3600)  # Run every 6 hours

            except Exception as e:
                logger.error(f"Pipeline error: {e}", exc_info=True)
                await asyncio.sleep(300)  # Wait 5 min on error

    # ── Phase 1: Scrape ──────────────────────────────────────────

    async def phase_scrape(self):
        """Scrape all sources for new leads."""
        logger.info("📡 Phase 1: Scraping leads...")
        db = SessionLocal()

        try:
            all_properties = []

            # Tax delinquent lists
            async with TexasTaxScraper(counties=settings.buy_box.target_counties[:3]) as scraper:
                props = await scraper.scrape()
                all_properties.extend(props)
                logger.info(f"Tax scraper: {len(props)} properties")

            # Zillow FSBO
            async with ZillowFSBOScraper() as scraper:
                props = await scraper.scrape()
                all_properties.extend(props)
                logger.info(f"Zillow FSBO: {len(props)} properties")

            # Craigslist motivated sellers
            async with CraigslistScraper() as scraper:
                props = await scraper.scrape()
                all_properties.extend(props)
                logger.info(f"Craigslist: {len(props)} properties")

            # Pre-foreclosure
            async with PreForeclosureScraper() as scraper:
                props = await scraper.scrape()
                all_properties.extend(props)
                logger.info(f"Pre-foreclosure: {len(props)} properties")

            # Save to database (skip duplicates by parcel_id)
            new_count = 0
            for prop_data in all_properties:
                parcel_id = prop_data.get("parcel_id")
                if parcel_id:
                    existing = db.query(Property).filter_by(parcel_id=parcel_id).first()
                    if existing:
                        continue

                prop = Property(
                    street=prop_data.get("street", ""),
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

                # Add owner if we have a name
                if prop_data.get("owner_name"):
                    db.flush()  # Get prop.id
                    owner = Owner(
                        property_id=prop.id,
                        full_name=prop_data["owner_name"],
                        mailing_address=prop_data.get("owner_mailing_address", ""),
                    )
                    db.add(owner)

                new_count += 1

            db.commit()
            logger.info(f"✅ Saved {new_count} new properties (from {len(all_properties)} scraped)")

        except Exception as e:
            logger.error(f"Scrape phase error: {e}", exc_info=True)
            db.rollback()
        finally:
            db.close()

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
                    call_result = await self.voice_agent.make_call(
                        phone_number=owner.phone_1,
                        assistant_id=assistant_id,
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

            # Email to JV partner
            jv_email = settings.contract.jv_partner_email
            if not jv_email:
                logger.warning("No JV partner email configured")
                return False

            subject = f"New Deal: {prop.street} {prop.city}, TX — ${deal.accepted_price or deal.max_offer:,.0f}"

            body = f"""New wholesale deal ready for review!

{deal_summary}

Contract Status: SIGNED
Assignment Fee: ${deal.assignment_fee:,.0f}

The signed contract is attached.

Please review and let me know if you'd like to proceed.

Best regards,
{settings.contract.buyer_name}
"""

            # Send with attachment
            self._send_email_with_attachment(
                to=jv_email,
                subject=subject,
                body=body,
                attachment_path=deal.contract_path,
            )

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
