"""
Wholesale AI - Configuration Settings
All settings for the wholesale real estate automation system.
"""
import os
from dataclasses import dataclass, field
from typing import Optional

# ── Buy Box Settings ──────────────────────────────────────────────
@dataclass
class BuyBox:
    max_arv_percentage: float = 0.50          # Max offer = ARV * this
    min_assignment_fee: float = 5000.0         # Minimum profit per deal
    min_arv: float = 50000.0                   # Skip super-cheap properties
    max_arv: float = 2000000.0                 # Skip mansions
    max_repair_estimate: float = 150000.0      # Skip tear-downs
    property_types: list = field(default_factory=lambda: [
        "single_family", "multi_family", "duplex", "triplex",
        "quadplex", "townhouse", "condo", "mobile_home", "land"
    ])
    target_states: list = field(default_factory=lambda: ["TX"])
    target_counties: list = field(default_factory=lambda: [
        "Harris", "Bexar", "Dallas", "Tarrant", "Travis",
        "El Paso", "Hidalgo", "Fort Bend", "Collin", "Denton"
    ])

# ── Database ──────────────────────────────────────────────────────
@dataclass
class Database:
    url: str = os.getenv("DATABASE_URL", "")
    # Supabase free tier
    supabase_url: str = os.getenv("SUPABASE_URL", "")
    supabase_key: str = os.getenv("SUPABASE_KEY", "")

# ── Telnyx (SMS) ~$0.004/msg — half the cost of Twilio ───────────
@dataclass
class Telnyx:
    api_key: str = os.getenv("TELNYX_API_KEY", "")
    phone_number: str = os.getenv("TELNYX_PHONE_NUMBER", "")
    messaging_profile_id: str = os.getenv("TELNYX_MESSAGING_PROFILE_ID", "")
    # SMS drip campaign settings
    max_sms_per_day: int = 200
    sms_delay_seconds: int = 30  # Between messages to avoid spam flags

# ── Vapi.ai (Voice Agent) ────────────────────────────────────────
@dataclass
class Vapi:
    api_key: str = os.getenv("VAPI_API_KEY", "")
    # Voice agent settings
    max_calls_per_day: int = 100
    call_hours_start: int = 9    # 9 AM local time
    call_hours_end: int = 20     # 8 PM local time
    max_call_duration: int = 300  # 5 min max per call
    languages: list = field(default_factory=lambda: ["en", "es"])

# ── Email ─────────────────────────────────────────────────────────
@dataclass
class Email:
    smtp_host: str = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    from_name: str = os.getenv("EMAIL_FROM_NAME", "Property Solutions")
    from_name_es: str = os.getenv("EMAIL_FROM_NAME_ES", "Soluciones de Propiedades")
    max_emails_per_day: int = 100
    # Email verification
    quickemail_api_key: str = os.getenv("QUICKEMAIL_API_KEY", "")

# ── Scraping ──────────────────────────────────────────────────────
@dataclass
class Scraping:
    # Rate limiting
    requests_per_minute: int = 10
    delay_between_requests: float = 3.0  # seconds
    max_retries: int = 3
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    # Proxy rotation (optional, for scale)
    proxy_list: list = field(default_factory=list)

# ── Deal Analysis ─────────────────────────────────────────────────
@dataclass
class DealAnalysis:
    comp_radius_miles: float = 0.5        # Search radius for comps
    comp_months_back: int = 6             # How far back for comps
    min_comps: int = 3                    # Minimum comps needed
    repair_cost_per_sqft_light: float = 15.0
    repair_cost_per_sqft_medium: float = 30.0
    repair_cost_per_sqft_heavy: float = 50.0

# ── Contract / E-Signature ────────────────────────────────────────
@dataclass
class Contract:
    buyer_name: str = os.getenv("BUYER_NAME", "")
    buyer_entity: str = os.getenv("BUYER_ENTITY", "")  # LLC name
    buyer_phone: str = os.getenv("BUYER_PHONE", "")
    buyer_email: str = os.getenv("BUYER_EMAIL", "")
    earnest_money_default: float = 100.0
    inspection_period_days: int = 14
    closing_period_days: int = 30
    # JV Partner — Primary (MaxDispo, fee-on-top)
    jv_partner_name: str = os.getenv("JV_PARTNER_NAME", "")
    jv_partner_email: str = os.getenv("JV_PARTNER_EMAIL", "")
    jv_partner_phone: str = os.getenv("JV_PARTNER_PHONE", "")
    # JV Partner — Backup (Cash Home Investors, 60/40)
    jv_backup_name: str = os.getenv("JV_BACKUP_NAME", "")
    jv_backup_email: str = os.getenv("JV_BACKUP_EMAIL", "")
    jv_backup_phone: str = os.getenv("JV_BACKUP_PHONE", "")

# ── Pipeline ──────────────────────────────────────────────────────
@dataclass
class Pipeline:
    cycle_interval_hours: int = 6
    max_scrape_per_cycle: int = 200
    max_skip_trace_per_cycle: int = 50
    max_analyze_per_cycle: int = 100
    max_outreach_per_cycle: int = 50

# ── Server ────────────────────────────────────────────────────────
@dataclass
class Server:
    host: str = "0.0.0.0"
    port: int = 8000
    secret_key: str = os.getenv("SECRET_KEY", "change-me-in-production")
    jwt_algorithm: str = "HS256"
    jwt_expiration_hours: int = 720  # 30 days

# ── Master Config ─────────────────────────────────────────────────
@dataclass
class Settings:
    buy_box: BuyBox = field(default_factory=BuyBox)
    database: Database = field(default_factory=Database)
    telnyx: Telnyx = field(default_factory=Telnyx)
    vapi: Vapi = field(default_factory=Vapi)
    email: Email = field(default_factory=Email)
    scraping: Scraping = field(default_factory=Scraping)
    deal_analysis: DealAnalysis = field(default_factory=DealAnalysis)
    contract: Contract = field(default_factory=Contract)
    pipeline: Pipeline = field(default_factory=Pipeline)
    server: Server = field(default_factory=Server)

settings = Settings()
