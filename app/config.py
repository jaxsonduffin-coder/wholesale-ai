"""Application configuration, loaded from environment."""
from __future__ import annotations

import os
import secrets
from functools import lru_cache


class Settings:
    """Plain env-backed settings (no pydantic-settings dependency needed)."""

    def __init__(self) -> None:
        g = os.environ.get
        self.app_env: str = g("APP_ENV", "development")
        self.secret_key: str = g("SECRET_KEY", "") or secrets.token_hex(32)
        self.database_url: str = g("DATABASE_URL", "sqlite:///./data/wholesale.db")

        # Public base URL — required for Telnyx voice webhooks to reach us.
        self.public_base_url: str = (g("PUBLIC_BASE_URL", "") or "").rstrip("/")

        self.admin_email: str = g("ADMIN_EMAIL", "admin@localhost")
        self.admin_password: str = g("ADMIN_PASSWORD", "changeme123")
        self.admin_name: str = g("ADMIN_NAME", "Owner")

        self.agent_name: str = g("AGENT_NAME", "Your Name")
        self.agent_company: str = g("AGENT_COMPANY", "Your Company")
        self.agent_phone: str = g("AGENT_PHONE", "+15125550100")
        self.agent_email: str = g("AGENT_EMAIL", "you@example.com")
        self.agent_calendar_link: str = g("AGENT_CALENDAR_LINK", "")
        # Where "hot lead" alerts go. Defaults to your own agent identity.
        # These are messages to YOU, never to the seller.
        self.owner_alert_sms: str = g("OWNER_ALERT_SMS", "") or g("AGENT_PHONE", "")
        self.owner_alert_email: str = g("OWNER_ALERT_EMAIL", "") or g("AGENT_EMAIL", "")

        # Required by CAN-SPAM: every marketing email must carry a valid
        # physical postal address for the sender.
        self.agent_postal_address: str = g("AGENT_POSTAL_ADDRESS", "")

        self.telnyx_api_key: str = g("TELNYX_API_KEY", "")
        self.telnyx_from_number: str = g("TELNYX_FROM_NUMBER", "")
        self.telnyx_messaging_profile_id: str = g("TELNYX_MESSAGING_PROFILE_ID", "")
        self.telnyx_connection_id: str = g("TELNYX_CONNECTION_ID", "")
        self.telnyx_public_key: str = g("TELNYX_PUBLIC_KEY", "")

        self.email_provider: str = g("EMAIL_PROVIDER", "").strip().lower()
        self.resend_api_key: str = g("RESEND_API_KEY", "")
        self.smtp_host: str = g("SMTP_HOST", "")
        self.smtp_port: int = int(g("SMTP_PORT", "587") or 587)
        self.smtp_user: str = g("SMTP_USER", "")
        self.smtp_password: str = g("SMTP_PASSWORD", "")
        self.email_from: str = g("EMAIL_FROM", "") or self.agent_email

        # ── Markets ───────────────────────────────────────────────
        # Comma-separated states to work, e.g. "TX,FL,GA". The pipeline pulls
        # each one. Quiet hours, timezones and DNC are already per-state, so
        # adding a market is safe by default; SMS stays inbound-only everywhere.
        raw_states = g("MARKET_STATES", "TX")
        self.market_states: list[str] = [
            st.strip().upper() for st in raw_states.split(",") if st.strip()
        ] or ["TX"]
        # Cities per state, used by the demo provider and as the default pull
        # target. Optional: "TX:Dallas,Houston;FL:Tampa,Orlando"
        self.market_cities_raw: str = g("MARKET_CITIES", "")
        # Public URL of your seller landing page, used in copy and alerts.
        self.public_form_url: str = g("PUBLIC_FORM_URL", "")

        self.lead_provider: str = g("LEAD_PROVIDER", "demo").strip().lower()
        self.batchdata_api_key: str = g("BATCHDATA_API_KEY", "")

        self.dnc_provider: str = g("DNC_PROVIDER", "demo").strip().lower()
        self.dnc_api_key: str = g("DNC_API_KEY", "")

        # Cookie `Secure` flag. Defaults to on in production (Railway terminates
        # TLS at the edge, so the browser sees https). Set COOKIE_SECURE=false
        # only for plain-HTTP local testing.
        self.cookie_secure: bool = _bool(
            g("COOKIE_SECURE", "true" if self.app_env == "production" else "false"),
            self.app_env == "production",
        )

        self.enforce_quiet_hours: bool = _bool(g("ENFORCE_QUIET_HOURS", "true"), True)
        self.max_sms_per_lead_per_day: int = int(g("MAX_SMS_PER_LEAD_PER_DAY", "1") or 1)
        self.max_call_attempts_per_lead_per_week: int = int(
            g("MAX_CALL_ATTEMPTS_PER_LEAD_PER_WEEK", "3") or 3
        )

        # Derived capability flags — used by the UI and by the senders.
        self.sms_live: bool = bool(self.telnyx_api_key and self.telnyx_from_number)
        self.voice_live: bool = bool(self.telnyx_api_key and self.telnyx_connection_id)
        self.email_live: bool = bool(
            (self.email_provider == "resend" and self.resend_api_key)
            or (self.email_provider == "smtp" and self.smtp_host)
        )

    def capabilities(self) -> dict:
        return {
            "sms": {"live": self.sms_live, "provider": "telnyx"},
            "voice": {"live": self.voice_live, "provider": "telnyx"},
            "email": {"live": self.email_live, "provider": self.email_provider or "dry_run"},
            "leads": {"live": self.lead_provider in ("batchdata",), "provider": self.lead_provider},
            "dnc": {"live": self.dnc_provider == "live", "provider": self.dnc_provider},
            "markets": {"live": True, "provider": ",".join(self.market_states)},
        }


def _bool(v: str, default: bool) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
