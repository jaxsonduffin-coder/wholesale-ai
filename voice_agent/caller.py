"""
AI Voice Agent using Vapi.ai
Makes bilingual (English/Spanish) calls to motivated sellers.
Negotiates purchase price, gauges motivation, sets appointments.
"""
import os
import logging
import httpx
from datetime import datetime
from typing import Optional
from config.settings import settings

logger = logging.getLogger(__name__)

VAPI_BASE = "https://api.vapi.ai"


# ── Voice Agent Prompts ──────────────────────────────────────────

ENGLISH_SYSTEM_PROMPT = """You are a friendly real estate investor's assistant calling about a property.
Your goal is to:
1. Confirm you're speaking with the property owner
2. Ask if they've considered selling the property
3. If interested, find out their asking price and timeline
4. Gauge their motivation level (scale 1-10)
5. If the price is reasonable, mention you can make a cash offer and close quickly (2-3 weeks)
6. Get their preferred contact method for follow-up

Be warm, professional, and conversational. Never be pushy.
If they're not interested, thank them politely and end the call.
If they ask who you are, say you work with a local real estate investment company
that buys houses in any condition for cash.

Key phrases to use:
- "We buy houses in any condition"
- "We can close in as little as 2-3 weeks"
- "No repairs needed — we buy as-is"
- "No agent fees or commissions"
- "We pay all closing costs"

Property details will be provided. Use them to personalize the conversation.
"""

SPANISH_SYSTEM_PROMPT = """Eres un asistente amigable de un inversionista de bienes raíces llamando sobre una propiedad.
Tu objetivo es:
1. Confirmar que estás hablando con el dueño de la propiedad
2. Preguntar si han considerado vender la propiedad
3. Si están interesados, averiguar su precio y plazo deseado
4. Evaluar su nivel de motivación (escala 1-10)
5. Si el precio es razonable, mencionar que pueden hacer una oferta en efectivo y cerrar rápido (2-3 semanas)
6. Obtener su método de contacto preferido para seguimiento

Sé cálido, profesional y conversacional. Nunca seas agresivo.
Si no están interesados, agradéceles amablemente y termina la llamada.
Si preguntan quién eres, di que trabajas con una compañía local de inversión en bienes raíces
que compra casas en cualquier condición por efectivo.

Frases clave para usar:
- "Compramos casas en cualquier condición"
- "Podemos cerrar en tan solo 2-3 semanas"
- "No se necesitan reparaciones — compramos tal como está"
- "Sin comisiones ni honorarios de agente"
- "Nosotros pagamos todos los costos de cierre"

Se proporcionarán detalles de la propiedad. Úsalos para personalizar la conversación.
"""


class VapiVoiceAgent:
    """AI voice agent that calls sellers using Vapi.ai."""

    def __init__(self):
        self.api_key = settings.vapi.api_key
        self.config = settings.vapi

    async def create_assistant(self, language: str = "en") -> Optional[str]:
        """Create a Vapi assistant for making calls."""
        prompt = ENGLISH_SYSTEM_PROMPT if language == "en" else SPANISH_SYSTEM_PROMPT
        voice = "jennifer-playht" if language == "en" else "lucia-playht"

        payload = {
            "name": f"Wholesale Agent ({language.upper()})",
            "model": {
                "provider": "openai",
                "model": "gpt-4o-mini",
                "messages": [{"role": "system", "content": prompt}],
                "temperature": 0.7,
            },
            "voice": {
                "provider": "playht",
                "voiceId": voice,
            },
            "firstMessage": (
                "Hi! My name is Jessica and I'm calling about your property. Do you have a moment to chat?"
                if language == "en" else
                "¡Hola! Mi nombre es Jessica y estoy llamando sobre su propiedad. ¿Tiene un momento para platicar?"
            ),
            "endCallMessage": (
                "Thank you for your time! We'll be in touch soon. Have a great day!"
                if language == "en" else
                "¡Gracias por su tiempo! Nos pondremos en contacto pronto. ¡Que tenga un excelente día!"
            ),
            "recordingEnabled": True,
            "maxDurationSeconds": self.config.max_call_duration,
            "silenceTimeoutSeconds": 30,
            "responseDelaySeconds": 0.5,
            "endCallFunctionEnabled": True,
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{VAPI_BASE}/assistant",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=30,
            )
            if resp.status_code == 201:
                data = resp.json()
                logger.info(f"Created Vapi assistant: {data['id']}")
                return data["id"]
            else:
                logger.error(f"Failed to create assistant: {resp.text}")
                return None

    async def make_call(
        self,
        phone_number: str,
        assistant_id: str,
        property_info: dict,
        language: str = "en",
    ) -> Optional[dict]:
        """
        Make an AI voice call to a property owner.

        Args:
            phone_number: Owner's phone (10 digits)
            assistant_id: Vapi assistant ID
            property_info: Dict with property address, details for context
            language: 'en' or 'es'

        Returns:
            Call result dict or None
        """
        if not self._is_within_call_hours():
            logger.info("Outside calling hours, skipping")
            return None

        # Format phone
        phone = phone_number.replace("-", "").replace("(", "").replace(")", "").replace(" ", "")
        if len(phone) == 10:
            phone = f"+1{phone}"
        elif not phone.startswith("+"):
            phone = f"+{phone}"

        # Build context message for the AI
        context = self._build_context(property_info, language)

        payload = {
            "assistantId": assistant_id,
            "phoneNumberId": settings.twilio.phone_number,
            "customer": {
                "number": phone,
            },
            "assistantOverrides": {
                "model": {
                    "messages": [
                        {"role": "system", "content": context},
                    ],
                },
            },
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{VAPI_BASE}/call/phone",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=30,
            )
            if resp.status_code == 201:
                data = resp.json()
                logger.info(f"Call initiated: {data['id']} to {phone}")
                return data
            else:
                logger.error(f"Failed to make call: {resp.text}")
                return None

    async def get_call_result(self, call_id: str) -> Optional[dict]:
        """Get the result of a completed call."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{VAPI_BASE}/call/{call_id}",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "call_id": call_id,
                    "status": data.get("status"),
                    "duration": data.get("duration"),
                    "transcript": data.get("transcript", ""),
                    "recording_url": data.get("recordingUrl"),
                    "summary": data.get("summary", ""),
                    "cost": data.get("cost", 0),
                    "ended_reason": data.get("endedReason"),
                }
            return None

    def _build_context(self, prop: dict, language: str) -> str:
        """Build property context for the AI to reference during the call."""
        addr = f"{prop.get('street', '')} {prop.get('city', '')}, {prop.get('state', 'TX')}"

        if language == "es":
            return f"""Contexto de la propiedad para esta llamada:
Dirección: {addr}
Tipo: {prop.get('property_type', 'casa')}
Recámaras: {prop.get('bedrooms', 'desconocido')}
Baños: {prop.get('bathrooms', 'desconocido')}
Pies cuadrados: {prop.get('sqft', 'desconocido')}
Año de construcción: {prop.get('year_built', 'desconocido')}
Dueño: {prop.get('owner_name', 'desconocido')}

Notas: {'Impuestos atrasados' if prop.get('tax_delinquent') else ''} {'Pre-ejecución hipotecaria' if prop.get('pre_foreclosure') else ''} {'Vacante' if prop.get('vacant') else ''}

Usa esta información para personalizar la conversación. No menciones los impuestos
atrasados o la ejecución hipotecaria directamente — deja que el dueño lo traiga a la conversación."""

        return f"""Property context for this call:
Address: {addr}
Type: {prop.get('property_type', 'house')}
Bedrooms: {prop.get('bedrooms', 'unknown')}
Bathrooms: {prop.get('bathrooms', 'unknown')}
Sqft: {prop.get('sqft', 'unknown')}
Year built: {prop.get('year_built', 'unknown')}
Owner: {prop.get('owner_name', 'unknown')}

Notes: {'Tax delinquent' if prop.get('tax_delinquent') else ''} {'Pre-foreclosure' if prop.get('pre_foreclosure') else ''} {'Vacant' if prop.get('vacant') else ''}

Use this info to personalize the conversation. Don't mention tax delinquency
or foreclosure directly — let the owner bring it up."""

    def _is_within_call_hours(self) -> bool:
        """Check if current time is within allowed calling hours."""
        now = datetime.now()
        return self.config.call_hours_start <= now.hour < self.config.call_hours_end
