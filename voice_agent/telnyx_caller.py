"""
Outbound AI Voice Agent using Telnyx Call Control API.
Makes bilingual (English/Spanish) calls to motivated sellers.
Uses Telnyx's built-in TTS + speech gathering with an LLM
for natural conversation flow.

Call flow:
  1. Initiate outbound call via Call Control API
  2. On answer → speak greeting via TTS
  3. Gather speech from seller → send to LLM → speak response
  4. Loop conversation turns until call ends or max duration hit
"""
import os
import json
import logging
import httpx
from datetime import datetime
from typing import Optional
from config.settings import settings

logger = logging.getLogger(__name__)

TELNYX_API = "https://api.telnyx.com/v2"

# ── System Prompts ──────────────────────────────────────────────────

ENGLISH_SYSTEM_PROMPT = """You are Jessica, a friendly real estate investor's assistant calling about a property on behalf of JD North LLC.
Your goal is to:
1. Confirm you're speaking with the property owner
2. Ask if they've considered selling the property
3. If interested, find out their asking price and timeline
4. Gauge their motivation level (scale 1-10)
5. If the price is reasonable, mention you can make a cash offer and close quickly (2-3 weeks)
6. Get their preferred contact method for follow-up

Be warm, professional, and conversational. Never be pushy.
If they're not interested, thank them politely and end the call.
If they ask who you are, say you work with JD North, a local real estate investment company
that buys houses in any condition for cash.

Key phrases to use:
- "We buy houses in any condition"
- "We can close in as little as 2-3 weeks"
- "No repairs needed — we buy as-is"
- "No agent fees or commissions"
- "We pay all closing costs"

IMPORTANT: Keep responses SHORT (1-3 sentences). This is a phone call, not an essay.
Respond naturally as if you're having a real conversation.
If the person says very little or seems confused, ask a simple clarifying question.
If they want to end the call, say goodbye politely.

Respond ONLY with what you would say next. No stage directions, no quotes, no labels."""

SPANISH_SYSTEM_PROMPT = """Eres Jessica, una asistente amigable de un inversionista de bienes raíces llamando sobre una propiedad en nombre de JD North LLC.
Tu objetivo es:
1. Confirmar que estás hablando con el dueño de la propiedad
2. Preguntar si han considerado vender la propiedad
3. Si están interesados, averiguar su precio y plazo deseado
4. Evaluar su nivel de motivación (escala 1-10)
5. Si el precio es razonable, mencionar que pueden hacer una oferta en efectivo y cerrar rápido (2-3 semanas)
6. Obtener su método de contacto preferido para seguimiento

Sé cálida, profesional y conversacional. Nunca seas agresiva.
Si no están interesados, agradéceles amablemente y termina la llamada.
Si preguntan quién eres, di que trabajas con JD North, una compañía local de inversión en bienes raíces
que compra casas en cualquier condición por efectivo.

Frases clave para usar:
- "Compramos casas en cualquier condición"
- "Podemos cerrar en tan solo 2-3 semanas"
- "No se necesitan reparaciones — compramos tal como está"
- "Sin comisiones ni honorarios de agente"
- "Nosotros pagamos todos los costos de cierre"

IMPORTANTE: Mantén las respuestas CORTAS (1-3 oraciones). Es una llamada telefónica, no un ensayo.
Responde naturalmente como si estuvieras en una conversación real.

Responde SOLO con lo que dirías a continuación. Sin direcciones de escena, sin comillas, sin etiquetas."""


GREETING_EN = (
    "Hi! My name is Jessica and I'm calling on behalf of JD North. "
    "We're a local real estate company and I'm reaching out about your property. "
    "Do you have a moment to chat?"
)

GREETING_ES = (
    "¡Hola! Mi nombre es Jessica y estoy llamando de parte de JD North. "
    "Somos una compañía local de bienes raíces y me comunico sobre su propiedad. "
    "¿Tiene un momento para platicar?"
)


class TelnyxOutboundAgent:
    """AI voice agent that calls sellers using Telnyx Call Control API."""

    def __init__(self):
        self.api_key = settings.telnyx.api_key
        self.from_number = settings.telnyx.phone_number
        # Store active call conversations: call_control_id -> messages list
        self.conversations: dict[str, list] = {}
        self.call_metadata: dict[str, dict] = {}

    async def make_call(
        self,
        phone_number: str,
        property_info: dict,
        language: str = "en",
        webhook_url: str = "",
    ) -> Optional[dict]:
        """
        Initiate an outbound call to a property owner.

        Args:
            phone_number: Owner's phone number
            property_info: Dict with property details for AI context
            language: 'en' or 'es'
            webhook_url: URL for Telnyx to send call events to

        Returns:
            Call result dict with call_control_id, or None on failure
        """
        if not self._is_within_call_hours():
            logger.info("Outside calling hours (9 AM - 8 PM), skipping call")
            return None

        # Format phone number
        phone = self._format_phone(phone_number)
        if not phone:
            logger.error(f"Invalid phone number: {phone_number}")
            return None

        # Build the webhook URL — Telnyx posts call events here
        if not webhook_url:
            port = settings.server.port
            webhook_url = os.getenv("WEBHOOK_BASE_URL", f"https://your-app.railway.app")
            webhook_url = f"{webhook_url}/api/telnyx/voice-webhook"

        payload = {
            "connection_id": os.getenv("TELNYX_SIP_CONNECTION_ID", ""),
            "to": phone,
            "from": self.from_number,
            "webhook_url": webhook_url,
            "webhook_url_method": "POST",
            "answering_machine_detection": "detect_words",
            "answering_machine_detection_config": {
                "after_greeting_silence_millis": 800,
                "total_analysis_time_millis": 5000,
            },
            "client_state": json.dumps({
                "language": language,
                "property": property_info,
            }).encode().hex() if property_info else None,
            "timeout_secs": 30,
        }

        # Remove None values
        payload = {k: v for k, v in payload.items() if v is not None}

        # If no SIP connection, use the simpler approach
        if not payload.get("connection_id"):
            payload.pop("connection_id", None)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    f"{TELNYX_API}/calls",
                    json=payload,
                    headers=headers,
                    timeout=30,
                )
                if resp.status_code in (200, 201):
                    data = resp.json().get("data", {})
                    call_control_id = data.get("call_control_id", "")
                    call_leg_id = data.get("call_leg_id", "")
                    logger.info(
                        f"Outbound call initiated to {phone} | "
                        f"call_control_id={call_control_id}"
                    )
                    # Initialize conversation state
                    self.conversations[call_control_id] = []
                    self.call_metadata[call_control_id] = {
                        "phone": phone,
                        "language": language,
                        "property_info": property_info,
                        "started_at": datetime.now().isoformat(),
                        "turns": 0,
                        "transcript": [],
                    }
                    return {
                        "call_control_id": call_control_id,
                        "call_leg_id": call_leg_id,
                        "status": "initiated",
                        "to": phone,
                    }
                else:
                    logger.error(
                        f"Failed to initiate call: {resp.status_code} {resp.text}"
                    )
                    return None
            except Exception as e:
                logger.error(f"Error initiating call: {e}")
                return None

    async def handle_webhook(self, event: dict) -> Optional[dict]:
        """
        Handle incoming Telnyx call control webhook events.
        Returns a command dict to send back to Telnyx, or None.
        """
        event_type = event.get("data", {}).get("event_type", "")
        payload = event.get("data", {}).get("payload", {})
        call_control_id = payload.get("call_control_id", "")

        logger.info(f"Telnyx webhook: {event_type} | cc_id={call_control_id[:12]}...")

        if event_type == "call.initiated":
            return None  # Wait for answer

        elif event_type == "call.answered":
            return await self._on_call_answered(call_control_id, payload)

        elif event_type == "call.machine.detection.ended":
            return await self._on_amd_result(call_control_id, payload)

        elif event_type == "call.gather.ended":
            return await self._on_gather_ended(call_control_id, payload)

        elif event_type == "call.speak.ended":
            return await self._on_speak_ended(call_control_id, payload)

        elif event_type in ("call.hangup", "call.machine.greeting.ended"):
            return await self._on_call_ended(call_control_id, payload)

        return None

    async def _on_call_answered(self, cc_id: str, payload: dict) -> dict:
        """Call was answered — start the greeting."""
        meta = self.call_metadata.get(cc_id, {})
        lang = meta.get("language", "en")
        greeting = GREETING_EN if lang == "en" else GREETING_ES

        # Add greeting to transcript
        if cc_id in self.call_metadata:
            self.call_metadata[cc_id]["transcript"].append({
                "role": "agent",
                "text": greeting,
            })

        return await self._speak(cc_id, greeting, lang)

    async def _on_amd_result(self, cc_id: str, payload: dict) -> Optional[dict]:
        """Answering machine detection result."""
        result = payload.get("result", "")
        if result == "machine":
            logger.info(f"Voicemail detected on {cc_id}, hanging up")
            return await self._hangup(cc_id)
        # If human answered, greeting was already sent via call.answered
        return None

    async def _on_speak_ended(self, cc_id: str, payload: dict) -> dict:
        """Agent finished speaking — now gather the seller's response."""
        meta = self.call_metadata.get(cc_id, {})
        lang = meta.get("language", "en")
        return await self._gather_speech(cc_id, lang)

    async def _on_gather_ended(self, cc_id: str, payload: dict) -> Optional[dict]:
        """Seller finished speaking — process with LLM and respond."""
        speech = payload.get("transcription", "")
        digits = payload.get("digits", "")
        status = payload.get("status", "")

        meta = self.call_metadata.get(cc_id, {})
        lang = meta.get("language", "en")

        # Timeout or no speech detected
        if status == "call_hangup" or not speech:
            if meta.get("turns", 0) == 0:
                # First turn with no response — try again once
                prompt = (
                    "Hello? Are you there?" if lang == "en"
                    else "¿Hola? ¿Está ahí?"
                )
                return await self._speak(cc_id, prompt, lang)
            else:
                # Already had conversation, end politely
                goodbye = (
                    "It seems like you might be busy. We'll follow up another time. Have a great day!"
                    if lang == "en" else
                    "Parece que está ocupado. Le contactaremos en otro momento. ¡Que tenga un buen día!"
                )
                if cc_id in self.call_metadata:
                    self.call_metadata[cc_id]["transcript"].append({
                        "role": "agent", "text": goodbye
                    })
                await self._speak(cc_id, goodbye, lang)
                return None

        # Record seller's speech in transcript
        if cc_id in self.call_metadata:
            self.call_metadata[cc_id]["transcript"].append({
                "role": "seller", "text": speech
            })
            self.call_metadata[cc_id]["turns"] += 1

        # Check if we should end the call (max turns)
        if meta.get("turns", 0) >= 15:
            goodbye = (
                "Thank you so much for your time! We'll be in touch with more details. Have a wonderful day!"
                if lang == "en" else
                "¡Muchas gracias por su tiempo! Nos pondremos en contacto con más detalles. ¡Que tenga un excelente día!"
            )
            if cc_id in self.call_metadata:
                self.call_metadata[cc_id]["transcript"].append({
                    "role": "agent", "text": goodbye
                })
            await self._speak(cc_id, goodbye, lang)
            return None

        # Generate AI response
        ai_response = await self._get_ai_response(cc_id, speech, lang)

        if cc_id in self.call_metadata:
            self.call_metadata[cc_id]["transcript"].append({
                "role": "agent", "text": ai_response
            })

        # Check if AI decided to end the call
        if any(phrase in ai_response.lower() for phrase in [
            "have a great day", "goodbye", "take care",
            "que tenga", "adiós", "hasta luego"
        ]):
            await self._speak(cc_id, ai_response, lang)
            return None  # Will hang up after speak ends naturally

        return await self._speak(cc_id, ai_response, lang)

    async def _on_call_ended(self, cc_id: str, payload: dict) -> None:
        """Call ended — save transcript and clean up."""
        meta = self.call_metadata.pop(cc_id, {})
        self.conversations.pop(cc_id, None)

        if meta:
            logger.info(
                f"Call ended to {meta.get('phone')} | "
                f"turns={meta.get('turns', 0)} | "
                f"transcript_lines={len(meta.get('transcript', []))}"
            )
        return None

    # ── Telnyx Call Control Commands ─────────────────────────────────

    async def _speak(self, cc_id: str, text: str, language: str = "en") -> dict:
        """Send a TTS speak command to the call."""
        voice = "female" if language == "en" else "female"
        lang_code = "en-US" if language == "en" else "es-MX"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "payload": text,
            "voice": voice,
            "language": lang_code,
            "payload_type": "text",
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{TELNYX_API}/calls/{cc_id}/actions/speak",
                json=payload,
                headers=headers,
                timeout=15,
            )
            if resp.status_code == 200:
                logger.debug(f"Speak command sent: {text[:60]}...")
            else:
                logger.error(f"Speak failed: {resp.status_code} {resp.text}")

        return {"action": "speak", "status": "sent"}

    async def _gather_speech(self, cc_id: str, language: str = "en") -> dict:
        """Gather speech input from the caller."""
        lang_code = "en-US" if language == "en" else "es-MX"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "minimum_digits": 1,
            "maximum_digits": 1,
            "inter_digit_timeout_millis": 5000,
            "gather_speech": {
                "language": lang_code,
                "end_silence_timeout_millis": 2000,  # 2s silence = done talking
                "speech_timeout_millis": 15000,  # Max 15s per response
            },
            "timeout_millis": 20000,
            "valid_digits": "",  # We mainly want speech, not DTMF
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{TELNYX_API}/calls/{cc_id}/actions/gather",
                json=payload,
                headers=headers,
                timeout=15,
            )
            if resp.status_code == 200:
                logger.debug("Gather command sent")
            else:
                logger.error(f"Gather failed: {resp.status_code} {resp.text}")

        return {"action": "gather", "status": "sent"}

    async def _hangup(self, cc_id: str) -> dict:
        """Hang up the call."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{TELNYX_API}/calls/{cc_id}/actions/hangup",
                json={},
                headers=headers,
                timeout=15,
            )

        return {"action": "hangup", "status": "sent"}

    # ── AI Response Generation ──────────────────────────────────────

    async def _get_ai_response(
        self, cc_id: str, seller_speech: str, language: str
    ) -> str:
        """Generate an AI response using Claude (Anthropic API)."""
        meta = self.call_metadata.get(cc_id, {})
        prop = meta.get("property_info", {})

        # Build system prompt with property context
        system = ENGLISH_SYSTEM_PROMPT if language == "en" else SPANISH_SYSTEM_PROMPT
        context = self._build_context(prop, language)
        system = f"{system}\n\n{context}"

        # Build conversation history (Anthropic format: no system in messages)
        messages = []
        for turn in meta.get("transcript", []):
            role = "assistant" if turn["role"] == "agent" else "user"
            messages.append({"role": role, "content": turn["text"]})

        # Add current seller speech
        messages.append({"role": "user", "content": seller_speech})

        # Call Claude via Anthropic Messages API
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        model = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")

        if not api_key:
            # Fallback: use scripted responses
            return self._scripted_response(seller_speech, language, meta.get("turns", 0))

        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        payload = {
            "model": model,
            "system": system,
            "messages": messages,
            "max_tokens": 150,  # Keep responses short for phone calls
            "temperature": 0.7,
        }

        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    json=payload,
                    headers=headers,
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    # Anthropic returns content as a list of blocks
                    content = data.get("content", [])
                    if content and content[0].get("type") == "text":
                        return content[0]["text"].strip()
                    return self._scripted_response(
                        seller_speech, language, meta.get("turns", 0)
                    )
                else:
                    logger.error(f"Claude API error: {resp.status_code} {resp.text}")
                    return self._scripted_response(
                        seller_speech, language, meta.get("turns", 0)
                    )
            except Exception as e:
                logger.error(f"Claude API exception: {e}")
                return self._scripted_response(
                    seller_speech, language, meta.get("turns", 0)
                )

    def _scripted_response(
        self, speech: str, language: str, turn: int
    ) -> str:
        """Fallback scripted responses when LLM is unavailable."""
        speech_lower = speech.lower()

        if language == "es":
            if any(w in speech_lower for w in ["no", "interesado", "momento"]):
                return "Entiendo completamente. Gracias por su tiempo. ¡Que tenga un excelente día!"
            if any(w in speech_lower for w in ["cuánto", "precio", "oferta"]):
                return ("Nos encantaría hacerle una oferta justa en efectivo. "
                        "¿Tiene algún precio en mente?")
            if turn < 2:
                return ("Compramos casas en cualquier condición por efectivo. "
                        "Podemos cerrar en 2-3 semanas. "
                        "¿Le interesaría saber más?")
            return ("Perfecto. ¿Cuál sería la mejor forma de contactarle "
                    "para darle más detalles?")

        # English fallback
        if any(w in speech_lower for w in ["no", "not interested", "busy", "stop"]):
            return "I completely understand. Thank you for your time. Have a great day!"
        if any(w in speech_lower for w in ["how much", "price", "offer", "what"]):
            return ("We'd love to make you a fair, no-obligation cash offer. "
                    "Do you have a price in mind?")
        if turn < 2:
            return ("We buy houses in any condition for cash. "
                    "We can close in as little as 2-3 weeks, and you pay nothing in fees. "
                    "Would you be open to hearing more?")
        return ("Great! What would be the best way to reach you "
                "so we can send over more details?")

    def _build_context(self, prop: dict, language: str) -> str:
        """Build property context for the AI."""
        if not prop:
            return ""

        addr = f"{prop.get('street', '')} {prop.get('city', '')}, {prop.get('state', 'TX')}"

        if language == "es":
            return f"""Contexto de la propiedad:
Dirección: {addr}
Dueño: {prop.get('owner_name', 'desconocido')}
Tipo: {prop.get('property_type', 'casa')}

No menciones impuestos atrasados o ejecución hipotecaria directamente."""

        return f"""Property context:
Address: {addr}
Owner: {prop.get('owner_name', 'unknown')}
Type: {prop.get('property_type', 'house')}

Don't mention tax delinquency or foreclosure directly — let the owner bring it up."""

    # ── Helpers ──────────────────────────────────────────────────────

    def _format_phone(self, phone_number: str) -> Optional[str]:
        """Format phone number to E.164."""
        phone = "".join(c for c in phone_number if c.isdigit() or c == "+")
        if phone.startswith("+"):
            return phone
        if len(phone) == 10:
            return f"+1{phone}"
        if len(phone) == 11 and phone.startswith("1"):
            return f"+{phone}"
        return None

    def _is_within_call_hours(self) -> bool:
        """Check if current time is within allowed calling hours (9 AM - 8 PM)."""
        now = datetime.now()
        return 9 <= now.hour < 20

    def get_call_transcript(self, cc_id: str) -> list:
        """Get the conversation transcript for a call."""
        meta = self.call_metadata.get(cc_id, {})
        return meta.get("transcript", [])


# ── Singleton for use across the app ─────────────────────────────
telnyx_agent = TelnyxOutboundAgent()
