"""
Email Campaign Manager.
Sends bilingual (English/Spanish) email sequences to property owners.
Uses SMTP (Gmail or any provider) — no paid email service needed.
"""
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from typing import Optional
from config.settings import settings

logger = logging.getLogger(__name__)


# ── Email Templates (English) ────────────────────────────────────

EMAIL_TEMPLATES_EN = [
    {
        "name": "initial_en",
        "delay_days": 0,
        "subject": "Cash Offer for Your Property at {address}",
        "body": """Hi {owner_name},

I hope this message finds you well. My name is {buyer_name} and I'm a local real estate investor.

I'm reaching out because I'm interested in purchasing your property at {address}. We buy houses in any condition — no repairs, cleaning, or staging needed.

Here's what we offer:
• Fair cash offer within 24 hours
• Close in as little as 2-3 weeks
• We pay ALL closing costs
• No agent commissions or fees
• We buy as-is — no repairs needed

If you've been thinking about selling, I'd love to chat. Even if you're just curious about what your property might be worth, I'm happy to provide a free, no-obligation estimate.

Would you be open to a quick conversation?

Best regards,
{buyer_name}
{buyer_phone}
""",
    },
    {
        "name": "followup1_en",
        "delay_days": 4,
        "subject": "Quick follow-up — {address}",
        "body": """Hi {owner_name},

Just wanted to follow up on my previous email about your property at {address}.

I understand selling a property is a big decision, and I want you to know there's absolutely no pressure. I simply wanted to make sure my message reached you.

If you have any questions or would like to explore a cash offer, I'm here to help. The process is simple:

1. We chat briefly about the property
2. I send you a fair cash offer within 24 hours
3. If you accept, we close on YOUR timeline

No showings, no open houses, no hassle.

Feel free to reply to this email or call me directly at {buyer_phone}.

Best,
{buyer_name}
""",
    },
    {
        "name": "followup2_en",
        "delay_days": 10,
        "subject": "Still interested in {address}",
        "body": """Hi {owner_name},

I know you're busy, so I'll keep this brief. I'm still very interested in your property at {address}.

If your situation has changed or you're now considering selling, I'd love to hear from you. We can work around any timeline — whether you need to close fast or need a few months.

Just reply "interested" and I'll get back to you right away.

Wishing you all the best,
{buyer_name}
{buyer_phone}
""",
    },
]

# ── Email Templates (Spanish) ────────────────────────────────────

EMAIL_TEMPLATES_ES = [
    {
        "name": "initial_es",
        "delay_days": 0,
        "subject": "Oferta en Efectivo por su Propiedad en {address}",
        "body": """Hola {owner_name},

Espero que se encuentre bien. Mi nombre es {buyer_name} y soy un inversionista local de bienes raíces.

Me comunico con usted porque estoy interesado en comprar su propiedad en {address}. Compramos casas en cualquier condición — no se necesitan reparaciones, limpieza ni preparación.

Lo que ofrecemos:
• Oferta justa en efectivo en 24 horas
• Cierre en tan solo 2-3 semanas
• Pagamos TODOS los costos de cierre
• Sin comisiones de agente ni honorarios
• Compramos tal como está — sin reparaciones

Si ha estado pensando en vender, me encantaría platicar. Incluso si solo tiene curiosidad sobre el valor de su propiedad, con gusto le proporciono una estimación gratuita sin compromiso.

¿Estaría abierto/a a una conversación breve?

Saludos cordiales,
{buyer_name}
{buyer_phone}
""",
    },
    {
        "name": "followup1_es",
        "delay_days": 4,
        "subject": "Seguimiento rápido — {address}",
        "body": """Hola {owner_name},

Solo quería darle seguimiento a mi correo anterior sobre su propiedad en {address}.

Entiendo que vender una propiedad es una decisión importante, y quiero que sepa que no hay ninguna presión. Simplemente quería asegurarme de que mi mensaje le haya llegado.

Si tiene alguna pregunta o desea explorar una oferta en efectivo, estoy aquí para ayudar. El proceso es simple:

1. Platicamos brevemente sobre la propiedad
2. Le envío una oferta justa en efectivo en 24 horas
3. Si acepta, cerramos en SU plazo

Sin visitas, sin casas abiertas, sin complicaciones.

No dude en responder a este correo o llamarme directamente al {buyer_phone}.

Saludos,
{buyer_name}
""",
    },
    {
        "name": "followup2_es",
        "delay_days": 10,
        "subject": "Aún interesado en {address}",
        "body": """Hola {owner_name},

Sé que está ocupado/a, así que seré breve. Sigo muy interesado en su propiedad en {address}.

Si su situación ha cambiado o ahora está considerando vender, me encantaría saber de usted. Podemos adaptarnos a cualquier plazo — ya sea que necesite cerrar rápido o necesite unos meses.

Solo responda "interesado" y me pondré en contacto de inmediato.

Le deseo lo mejor,
{buyer_name}
{buyer_phone}
""",
    },
]


class EmailCampaignManager:
    """Manages email outreach campaigns via SMTP."""

    def __init__(self):
        self.email_config = settings.email
        self.contract_config = settings.contract

    def get_templates(self, language: str = "en") -> list[dict]:
        return EMAIL_TEMPLATES_ES if language == "es" else EMAIL_TEMPLATES_EN

    def create_email_sequence(
        self,
        owner_name: str,
        email: str,
        address: str,
        language: str = "en",
    ) -> list[dict]:
        """Create an email drip sequence for a lead."""
        templates = self.get_templates(language)
        now = datetime.utcnow()

        buyer_name = self.contract_config.buyer_name or "Property Solutions Team"
        buyer_phone = self.contract_config.buyer_phone or ""

        sequence = []
        for i, template in enumerate(templates):
            send_at = now + timedelta(days=template["delay_days"])
            subject = template["subject"].format(address=address)
            body = template["body"].format(
                owner_name=owner_name.split()[0] if owner_name else "there",
                address=address,
                buyer_name=buyer_name,
                buyer_phone=buyer_phone,
            )
            sequence.append({
                "sequence_number": i + 1,
                "template_name": template["name"],
                "subject": subject,
                "body": body,
                "language": language,
                "email": email,
                "send_at": send_at,
                "sent": False,
            })
        return sequence

    def send_email(self, to: str, subject: str, body: str, language: str = "en") -> bool:
        """Send a single email via SMTP."""
        cfg = self.email_config
        if not cfg.smtp_user or not cfg.smtp_password:
            logger.error("SMTP not configured")
            return False

        try:
            msg = MIMEMultipart("alternative")
            msg["From"] = f"{cfg.from_name_es if language == 'es' else cfg.from_name} <{cfg.smtp_user}>"
            msg["To"] = to
            msg["Subject"] = subject

            # Plain text
            msg.attach(MIMEText(body, "plain", "utf-8"))

            # HTML version (simple)
            html_body = body.replace("\n", "<br>")
            html = f"<html><body style='font-family: Arial, sans-serif; line-height: 1.6;'>{html_body}</body></html>"
            msg.attach(MIMEText(html, "html", "utf-8"))

            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port) as server:
                server.starttls()
                server.login(cfg.smtp_user, cfg.smtp_password)
                server.send_message(msg)

            logger.info(f"Email sent to {to}: {subject}")
            return True
        except Exception as e:
            logger.error(f"Email failed to {to}: {e}")
            return False

    async def process_pending_emails(self, db_session) -> int:
        """Process pending email sends. Called by scheduler."""
        from utils.database import EmailCampaign
        now = datetime.utcnow()
        sent_count = 0

        pending = (
            db_session.query(EmailCampaign)
            .filter(
                EmailCampaign.sent == False,
                EmailCampaign.active == True,
                EmailCampaign.next_send_at <= now,
            )
            .order_by(EmailCampaign.next_send_at)
            .limit(self.email_config.max_emails_per_day)
            .all()
        )

        for campaign in pending:
            if sent_count >= self.email_config.max_emails_per_day:
                break

            success = self.send_email(
                to=campaign.recipient_email,
                subject=campaign.subject,
                body=campaign.body,
                language=campaign.language,
            )
            if success:
                campaign.sent = True
                sent_count += 1

            import asyncio
            await asyncio.sleep(2)  # Don't spam SMTP server

        db_session.commit()
        logger.info(f"Sent {sent_count} emails")
        return sent_count
