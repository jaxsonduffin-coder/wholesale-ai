"""
SMS Drip Campaign Manager.
Sends bilingual (English/Spanish) SMS sequences to motivated sellers via Twilio.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional
from twilio.rest import Client as TwilioClient
from config.settings import settings

logger = logging.getLogger(__name__)


# ── SMS Templates (English) ──────────────────────────────────────

SMS_TEMPLATES_EN = [
    # Day 1: Initial contact
    {
        "name": "initial_en",
        "delay_days": 0,
        "message": (
            "Hi {owner_name}! I'm reaching out about your property at {address}. "
            "We're local investors who buy houses in any condition for cash. "
            "Would you be open to a quick, no-obligation offer? Reply YES or call us!"
        ),
    },
    # Day 3: Follow-up
    {
        "name": "followup1_en",
        "delay_days": 3,
        "message": (
            "Hi {owner_name}, just following up about {address}. "
            "We can close in 2-3 weeks, pay cash, and cover all closing costs. "
            "No repairs needed! Interested in hearing an offer?"
        ),
    },
    # Day 7: Value add
    {
        "name": "followup2_en",
        "delay_days": 7,
        "message": (
            "Hi {owner_name}! Quick question about {address} — "
            "if you received a fair cash offer, would you consider selling? "
            "We make the process easy: no showings, no agents, no fees. "
            "Just reply or give us a call!"
        ),
    },
    # Day 14: Last chance
    {
        "name": "followup3_en",
        "delay_days": 14,
        "message": (
            "Hi {owner_name}, last message about {address}. "
            "If you ever want to explore a cash offer, we're here! "
            "No pressure at all. Just save this number. "
            "Wishing you the best! 🏡"
        ),
    },
    # Day 30: Re-engage
    {
        "name": "reengage_en",
        "delay_days": 30,
        "message": (
            "Hi {owner_name}! Checking in — still interested in buying {address} "
            "if you're open to it. Market conditions change, and we'd love to "
            "make you a competitive cash offer. Any interest?"
        ),
    },
]

# ── SMS Templates (Spanish) ──────────────────────────────────────

SMS_TEMPLATES_ES = [
    {
        "name": "initial_es",
        "delay_days": 0,
        "message": (
            "¡Hola {owner_name}! Le escribo sobre su propiedad en {address}. "
            "Somos inversionistas locales que compramos casas en cualquier condición por efectivo. "
            "¿Estaría abierto/a a una oferta rápida sin compromiso? ¡Responda SÍ o llámenos!"
        ),
    },
    {
        "name": "followup1_es",
        "delay_days": 3,
        "message": (
            "Hola {owner_name}, seguimiento sobre {address}. "
            "Podemos cerrar en 2-3 semanas, pagar en efectivo y cubrir todos los costos de cierre. "
            "¡No necesita hacer reparaciones! ¿Le interesa escuchar una oferta?"
        ),
    },
    {
        "name": "followup2_es",
        "delay_days": 7,
        "message": (
            "¡Hola {owner_name}! Pregunta rápida sobre {address} — "
            "si recibiera una oferta justa en efectivo, ¿consideraría vender? "
            "Hacemos el proceso fácil: sin visitas, sin agentes, sin comisiones. "
            "¡Solo responda o llámenos!"
        ),
    },
    {
        "name": "followup3_es",
        "delay_days": 14,
        "message": (
            "Hola {owner_name}, último mensaje sobre {address}. "
            "Si alguna vez quiere explorar una oferta en efectivo, ¡estamos aquí! "
            "Sin presión. Solo guarde este número. "
            "¡Le deseamos lo mejor! 🏡"
        ),
    },
    {
        "name": "reengage_es",
        "delay_days": 30,
        "message": (
            "¡Hola {owner_name}! Solo para confirmar — seguimos interesados en comprar {address} "
            "si usted está dispuesto/a. Las condiciones del mercado cambian, y nos encantaría "
            "hacerle una oferta competitiva en efectivo. ¿Algún interés?"
        ),
    },
]


class SMSCampaignManager:
    """Manages SMS drip campaigns via Twilio."""

    def __init__(self):
        self.twilio_config = settings.twilio
        self.client = None
        if self.twilio_config.account_sid and self.twilio_config.auth_token:
            self.client = TwilioClient(
                self.twilio_config.account_sid,
                self.twilio_config.auth_token,
            )

    def get_templates(self, language: str = "en") -> list[dict]:
        """Get SMS templates for the given language."""
        return SMS_TEMPLATES_ES if language == "es" else SMS_TEMPLATES_EN

    def create_drip_sequence(
        self,
        owner_name: str,
        phone: str,
        address: str,
        language: str = "en",
    ) -> list[dict]:
        """
        Create a drip sequence for a lead.
        Returns list of scheduled messages.
        """
        templates = self.get_templates(language)
        now = datetime.utcnow()

        sequence = []
        for i, template in enumerate(templates):
            send_at = now + timedelta(days=template["delay_days"])
            message = template["message"].format(
                owner_name=owner_name.split()[0] if owner_name else "there",
                address=address,
            )
            sequence.append({
                "sequence_number": i + 1,
                "template_name": template["name"],
                "message": message,
                "language": language,
                "phone": phone,
                "send_at": send_at,
                "sent": False,
            })

        return sequence

    def send_sms(self, to: str, message: str) -> Optional[str]:
        """Send a single SMS via Twilio. Returns message SID or None."""
        if not self.client:
            logger.error("Twilio not configured")
            return None

        # Format phone
        phone = to.replace("-", "").replace("(", "").replace(")", "").replace(" ", "")
        if len(phone) == 10:
            phone = f"+1{phone}"
        elif not phone.startswith("+"):
            phone = f"+{phone}"

        try:
            msg = self.client.messages.create(
                body=message,
                from_=self.twilio_config.phone_number,
                to=phone,
            )
            logger.info(f"SMS sent to {phone}: {msg.sid}")
            return msg.sid
        except Exception as e:
            logger.error(f"SMS failed to {phone}: {e}")
            return None

    async def process_pending_messages(self, db_session) -> int:
        """
        Process all pending SMS messages that are due.
        Called by the scheduler every few minutes.
        Returns number of messages sent.
        """
        from utils.database import SMSDrip
        now = datetime.utcnow()
        sent_count = 0

        pending = (
            db_session.query(SMSDrip)
            .filter(
                SMSDrip.sent == False,
                SMSDrip.active == True,
                SMSDrip.next_send_at <= now,
            )
            .order_by(SMSDrip.next_send_at)
            .limit(self.twilio_config.max_sms_per_day)
            .all()
        )

        for drip in pending:
            # Check daily limit
            if sent_count >= self.twilio_config.max_sms_per_day:
                break

            # Send
            sid = self.send_sms(drip.message, drip.message)
            if sid:
                drip.sent = True
                sent_count += 1

            # Delay between sends
            import asyncio
            await asyncio.sleep(self.twilio_config.sms_delay_seconds)

        db_session.commit()
        logger.info(f"Sent {sent_count} SMS messages")
        return sent_count
