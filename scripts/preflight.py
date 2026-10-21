"""Preflight: refuse to go live in an unsafe configuration.

Run this immediately before you send real traffic:

    python3 scripts/preflight.py

Exits non-zero on anything that would cost you money, get your number blocked,
or expose the app. It is deliberately strict: a warning here means fix it or
accept a known risk, not "probably fine".
"""
from __future__ import annotations

import os
import sys

FAIL: list[str] = []
WARN: list[str] = []
OK: list[str] = []


def check(cond: bool, ok: str, bad: str, *, fatal: bool = True) -> None:
    if cond:
        OK.append(ok)
    elif fatal:
        FAIL.append(bad)
    else:
        WARN.append(bad)


def main() -> int:
    env = os.environ.get
    app_env = (env("APP_ENV") or "").lower()

    print("\n== core ==")
    check(app_env == "production", "APP_ENV=production", "APP_ENV is not 'production' — /docs and /openapi.json will be public")
    secret = env("SECRET_KEY") or ""
    check(
        len(secret) >= 32 and "CHANGE_ME" not in secret and "smoke" not in secret,
        "SECRET_KEY is set and long",
        "SECRET_KEY missing, short, or still the example value (sessions can be forged)",
    )
    pw = env("ADMIN_PASSWORD") or ""
    check(
        len(pw) >= 10 and pw not in ("changeme123", "CHANGE_ME_strong_password", "password"),
        "ADMIN_PASSWORD is set and strong",
        "ADMIN_PASSWORD is missing, short, or a known default — anyone can log in",
    )
    db = env("DATABASE_URL") or ""
    check(
        db.startswith(("postgresql://", "postgres://", "sqlite:")),
        "DATABASE_URL configured",
        "DATABASE_URL not set — data will live on ephemeral disk and vanish on redeploy",
    )
    check("sqlite" not in db, "Postgres in use", "SQLite on Railway loses data on redeploy; attach a Postgres plugin", fatal=False)

    print("\n== identity (used in every message) ==")
    for var in ("AGENT_NAME", "AGENT_COMPANY", "AGENT_PHONE", "AGENT_EMAIL"):
        check(bool(env(var)), f"{var} set", f"{var} not set — outreach will look anonymous")
    check(
        bool(env("AGENT_POSTAL_ADDRESS")),
        "postal address set",
        "AGENT_POSTAL_ADDRESS not set — cold email is not CAN-SPAM compliant without it",
    )
    check(
        bool(env("PUBLIC_BASE_URL")),
        "PUBLIC_BASE_URL set",
        "PUBLIC_BASE_URL not set — Telnyx voice webhooks cannot reach you",
    )

    print("\n== texting ==")
    telnyx_key = env("TELNYX_API_KEY") or ""
    check(bool(telnyx_key), "TELNYX_API_KEY set", "TELNYX_API_KEY not set — SMS and voice are dry-run")
    check(bool(env("TELNYX_FROM_NUMBER")), "sending number set", "TELNYX_FROM_NUMBER not set")
    check(
        bool(env("TELNYX_MESSAGING_PROFILE_ID")),
        "messaging profile set",
        "TELNYX_MESSAGING_PROFILE_ID not set — 10DLC routing is incomplete and messages may be filtered",
    )
    check(
        bool(env("TELNYX_CONNECTION_ID")),
        "voice connection set",
        "TELNYX_CONNECTION_ID not set — AI calls cannot be placed",
    )
    check(
        bool(env("TELNYX_PUBLIC_KEY")),
        "webhook signatures verified",
        "TELNYX_PUBLIC_KEY not set — anyone can POST fake inbound replies to your webhook",
    )

    print("\n== compliance ==")
    check(
        (env("ENFORCE_QUIET_HOURS") or "true").lower() in ("1", "true", "yes", "on"),
        "quiet hours enforced",
        "ENFORCE_QUIET_HOURS is off — you will text and call outside legal hours",
    )
    dnc = (env("DNC_PROVIDER") or "").lower()
    check(dnc == "live", "DNC scrubbing live", f"DNC_PROVIDER={dnc or 'unset'} — you are dialling without a DNC scrub", fatal=False)

    print("\n== markets ==")
    states = (env("MARKET_STATES") or "TX").split(",")
    OK.append(f"markets: {', '.join(s.strip() for s in states if s.strip())}")
    if len(states) > 1:
        WARN.append(
            "multiple markets: state mini-TCPAs differ (FL FTSA, OK/MD/CT written consent, "
            "WA CEMA, TX SB140). SMS stays inbound-only, which is the safe configuration."
        )

    print("\n== email ==")
    prov = (env("EMAIL_PROVIDER") or "").lower()
    if prov == "resend":
        check(bool(env("RESEND_API_KEY")), "Resend configured", "EMAIL_PROVIDER=resend but RESEND_API_KEY is empty")
    elif prov == "smtp":
        check(bool(env("SMTP_HOST")), "SMTP configured", "EMAIL_PROVIDER=smtp but SMTP_HOST is empty")
    else:
        WARN.append("no email provider configured — email steps will be logged, not sent")
    if prov:
        WARN.append(
            "cold email needs a warmed sending domain with SPF, DKIM and DMARC, or it lands in spam"
        )

    width = 60
    if OK:
        print("\n" + "-" * width)
        for line in OK:
            print(f"  OK    {line}")
    if WARN:
        print("\n" + "-" * width)
        for line in WARN:
            print(f"  WARN  {line}")
    if FAIL:
        print("\n" + "-" * width)
        for line in FAIL:
            print(f"  FAIL  {line}")
        print("\n" + "=" * width)
        print(f"  {len(FAIL)} blocking issue(s). Do not send live traffic yet.")
        print("=" * width + "\n")
        return 1

    print("\n" + "=" * width)
    print("  Ready. Configured capabilities and known warnings above.")
    print("=" * width + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
