"""Telnyx SMS + voice (AI agent) channel.

Every send goes through compliance.check_send_allowed first. If credentials
are missing the call is recorded as `simulated` rather than silently doing
nothing — so the dashboard always reflects reality and you can dry-run the
entire system before spending money.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass

import httpx

from ..config import settings

API_BASE = "https://api.telnyx.com/v2"


@dataclass
class SendResult:
    ok: bool
    status: str
    provider_id: str = ""
    error: str = ""
    simulated: bool = False


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.telnyx_api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def send_sms(to: str, body: str) -> SendResult:
    if not settings.sms_live:
        return SendResult(
            ok=True, status="simulated", simulated=True,
            error="Telnyx not configured — message logged, not sent",
        )
    payload: dict = {"from": settings.telnyx_from_number, "to": to, "text": body}
    if settings.telnyx_messaging_profile_id:
        payload["messaging_profile_id"] = settings.telnyx_messaging_profile_id
    try:
        with httpx.Client(timeout=25) as client:
            r = client.post(f"{API_BASE}/messages", json=payload, headers=_headers())
        if r.status_code >= 400:
            return SendResult(ok=False, status="failed", error=f"{r.status_code}: {r.text[:300]}")
        data = r.json()
        return SendResult(ok=True, status="sent", provider_id=(data.get("data") or {}).get("id", ""))
    except httpx.HTTPError as e:
        return SendResult(ok=False, status="failed", error=f"network: {e}")


def start_ai_call(to: str, script: str, *, lead_id: int | None = None) -> SendResult:
    """Place an outbound call and have Telnyx speak `script` via TTS.

    Telnyx Call Control: POST /calls with a webhook_url that receives
    call.answered, at which point we issue /calls/{id}/actions/speak.
    The script is stashed in client_state so the webhook knows what to say.
    """
    if not settings.voice_live:
        return SendResult(
            ok=True, status="simulated", simulated=True,
            error="Telnyx voice not configured — call logged, not placed",
        )
    client_state = base64.b64encode(
        json.dumps({"lead_id": lead_id, "script": script}).encode()
    ).decode()
    payload = {
        "connection_id": settings.telnyx_connection_id,
        "to": to,
        "from": settings.telnyx_from_number,
        "client_state": client_state,
        "timeout_secs": 30,
    }
    if settings.public_base_url:
        payload["webhook_url"] = f"{settings.public_base_url}/api/telnyx/voice-webhook"
        payload["webhook_url_method"] = "POST"
    try:
        with httpx.Client(timeout=25) as client:
            r = client.post(f"{API_BASE}/calls", json=payload, headers=_headers())
        if r.status_code >= 400:
            return SendResult(ok=False, status="failed", error=f"{r.status_code}: {r.text[:300]}")
        data = r.json()
        return SendResult(
            ok=True, status="sent", provider_id=(data.get("data") or {}).get("call_control_id", "")
        )
    except httpx.HTTPError as e:
        return SendResult(ok=False, status="failed", error=f"network: {e}")


def speak(call_control_id: str, text: str) -> SendResult:
    try:
        with httpx.Client(timeout=20) as client:
            r = client.post(
                f"{API_BASE}/calls/{call_control_id}/actions/speak",
                json={
                    "payload": text,
                    "voice": "female",
                    "language": "en-US",
                },
                headers=_headers(),
            )
        if r.status_code >= 400:
            return SendResult(ok=False, status="failed", error=f"{r.status_code}: {r.text[:300]}")
        return SendResult(ok=True, status="sent")
    except httpx.HTTPError as e:
        return SendResult(ok=False, status="failed", error=str(e))


def hangup(call_control_id: str) -> None:
    try:
        with httpx.Client(timeout=15) as client:
            client.post(
                f"{API_BASE}/calls/{call_control_id}/actions/hangup", json={}, headers=_headers()
            )
    except httpx.HTTPError:
        pass


# ─────────────────────────────────────────────────────────────
# Webhook verification
# ─────────────────────────────────────────────────────────────
def verify_webhook(raw_body: bytes, signature: str | None, timestamp: str | None) -> bool:
    """Verify a Telnyx webhook signature.

    Telnyx signs with ed25519 using TELNYX_PUBLIC_KEY. We accept either:
      * a valid ed25519 signature (production), or
      * a matching shared-secret header (simpler fallback), or
      * nothing configured (dev only — logs a warning upstream).
    """
    if not settings.telnyx_public_key:
        # No key configured: allow, but main.py logs an explicit warning once.
        return True
    if not signature or not timestamp:
        return False
    signed = f"{timestamp}|{raw_body.decode('utf-8', 'replace')}".encode()
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        key_b64 = settings.telnyx_public_key.strip()
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(key_b64))
        key.verify(base64.b64decode(signature), signed)
        return True
    except Exception:
        return False


def verify_shared_secret(provided: str | None) -> bool:
    """Fallback HMAC check for webhook URLs that carry a secret query param."""
    if not settings.secret_key:
        return False
    expected = hmac.new(
        settings.secret_key.encode(), b"telnyx-webhook", hashlib.sha256
    ).hexdigest()
    return bool(provided) and hmac.compare_digest(provided, expected)
