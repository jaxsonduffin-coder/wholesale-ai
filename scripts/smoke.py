"""End-to-end smoke test against a REAL uvicorn server over real HTTP.

Run this before you deploy. It boots the app exactly as Railway does, then walks
the whole lifecycle: login -> pipeline -> import junk CSV -> send -> opt-out.

    python3 scripts/smoke.py

Exits non-zero on the first failure, so it works in CI.
"""
from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import time

_TMP = tempfile.mkdtemp(prefix="wai-smoke-")
os.environ.update(
    {
        "APP_ENV": "production",          # exercises the hardened config
        "SECRET_KEY": "smoke-test-secret-key-0123456789",
        "DATABASE_URL": f"sqlite:///{_TMP}/smoke.db",
        "ADMIN_EMAIL": "smoke@test.local",
        "ADMIN_PASSWORD": "smoke-password-123",
        "AGENT_NAME": "Sam Reeves",
        "AGENT_COMPANY": "Reeves Home Offers",
        "AGENT_PHONE": "+12145550188",
        "AGENT_EMAIL": "sam@reeves.test",
        "LEAD_PROVIDER": "demo",
        "DNC_PROVIDER": "off",
        "PUBLIC_BASE_URL": "http://127.0.0.1",
        # The smoke test speaks plain HTTP to 127.0.0.1, so the Secure cookie
        # attribute must be off or the cookie is never sent back. Production
        # behind Railway's TLS edge keeps it on.
        "COOKIE_SECURE": "false",
        # Quiet-hour enforcement is verified by unit tests with fixed clocks
        # (tests/test_compliance.py). Here it is disabled so the outreach
        # assertions are deterministic no matter what time CI runs.
        "ENFORCE_QUIET_HOURS": "false",
    }
)

import httpx  # noqa: E402
import uvicorn  # noqa: E402

# Allow `python3 scripts/smoke.py` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(f"{name} {detail}")
        print(f"  FAIL  {name} {detail}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    port = free_port()
    base = f"http://127.0.0.1:{port}"

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    for _ in range(100):
        try:
            if httpx.get(f"{base}/health", timeout=0.5).status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    else:
        print("server never came up")
        return 1

    client = httpx.Client(base_url=base, timeout=60.0, follow_redirects=False)

    print("\n[auth]")
    r = client.get("/health")
    check("health is public", r.status_code == 200 and r.json()["ok"] is True)

    for path in ["/api/dashboard", "/api/deals", "/api/activity", "/api/settings"]:
        r = client.get(path)
        check(f"{path} is 401 unauthenticated", r.status_code == 401, f"got {r.status_code}")

    r = client.post("/api/pipeline/run")
    check("POST /api/pipeline/run is 401 unauthenticated", r.status_code == 401)

    r = client.post("/api/login", json={"email": "smoke@test.local", "password": "wrong"})
    check("wrong password rejected", r.status_code == 401)

    r = client.post(
        "/api/login",
        json={"email": "smoke@test.local", "password": "smoke-password-123"},
    )
    check("login succeeds", r.status_code == 200, r.text[:200])
    check("session cookie is httponly", "httponly" in r.headers.get("set-cookie", "").lower())

    r = client.get("/api/dashboard")
    check("dashboard 200 after login", r.status_code == 200)

    print("\n[docx hardened]")
    r = client.get("/docs")
    check("/docs disabled in production", r.status_code == 404, f"got {r.status_code}")
    r = client.get("/openapi.json")
    check("/openapi.json disabled in production", r.status_code == 404)

    print("\n[header] security headers")
    check("nosniff header present", client.get("/").headers.get("x-content-type-options") == "nosniff")
    check("frame options DENY", client.get("/").headers.get("x-frame-options") == "DENY")

    print("\n[ui]")
    r = client.get("/")
    check("dashboard HTML served", r.status_code == 200 and "Wholesale AI" in r.text)
    check("client JS served", client.get("/static/app.js").status_code == 200)

    print("\n[pipeline]")
    r = client.post("/api/pipeline/run", json={"city": "Dallas", "state": "TX", "limit": 40})
    check("pipeline run completes", r.status_code == 200, r.text[:200])
    body = r.json()
    stats = body.get("stats", {})
    ing = stats.get("ingest", {})
    check("some leads accepted", ing.get("accepted", 0) > 0, str(ing))
    check("leads analysed", stats.get("analyzed", 0) > 0)
    check("leads enrolled in cadence", stats.get("enrolled", 0) > 0)

    d = client.get("/api/dashboard").json()
    check("dashboard shows real grades", sum(d["grades"].values()) > 0, str(d["grades"]))
    check(
        "no deal claims a signature without a contract",
        d["pipeline"]["signed"] == 0,
        str(d["pipeline"]),
    )

    print("\n[validation] the v1 garbage is refused")
    junk = (
        "Address,City,State,Owner\n"
        "Navigation Links,Dallas,TX,Annual Report\n"
        "About Dcad,Dallas,TX,\n"
        "Nsewnenwsesw,Dallas,TX,\n"
        "Houston Houston,Houston,TX,\n"
        "Commercial,Dallas,TX,\n"
        "4501 W Mockingbird Ln,Dallas,TX,Ada Byron\n"
    )
    r = client.post(
        "/api/leads/import",
        files={"file": ("v1_export.csv", junk, "text/csv")},
    )
    check("import returns 200", r.status_code == 200, r.text[:200])
    body_j = r.json()
    st = body_j["stats"]
    check("5 garbage rows rejected", st["rejected"] == 5, str(st["rejected_reasons"]))
    check("1 real row accepted", st["accepted"] == 1, str(st["accepted"]))
    check("import grants no consent by default",
          body_j["consent_policy"] == "none" and body_j["lead_class"] == "list")

    r = client.post(
        "/api/leads", json={"street": "Navigation Links", "city": "Dallas", "state": "TX"}
    )
    check("API refuses a navigation link", r.status_code == 422, r.text[:120])

    print("\n[channel policy]")
    pol = client.get("/api/dashboard").json()["policy"]
    check("list leads are call+email only", pol["class_channels"]["list"] == ["call", "email"])
    check("probate leads are call+email only", pol["class_channels"]["probate"] == ["call", "email"])
    check("sms restricted to inbound", pol["sms_allowed_classes"] == ["inbound"])

    cad = client.get("/api/cadence").json()
    by_class = {c["lead_class"]: c for c in cad["cadences"]}
    check("cadence exists per class", set(by_class) >= {"inbound", "list", "probate", "referral"})
    check("list cadence has no sms step", "sms" not in by_class["list"]["channels"])
    check("probate cadence has no sms step", "sms" not in by_class["probate"]["channels"])
    check("probate cadence opens with written outreach",
          by_class["probate"]["steps"][0]["channel"] == "email")
    check("inbound cadence has sms", "sms" in by_class["inbound"]["channels"])

    print("\n[outreach]")
    deals = client.get("/api/deals?limit=200").json()
    list_lead = next((x for x in deals if x["lead_class"] == "list" and x["is_contactable"]), None)
    inbound_lead = next((x for x in deals if x["lead_class"] == "inbound" and x["sms_allowed"]), None)

    check("found a list lead", list_lead is not None)
    check("found a consented inbound lead", inbound_lead is not None)

    if list_lead:
        r = client.post(
            f"/api/deals/{list_lead['id']}/contact?channel=sms&template_key=sms_opener"
        )
        check("list lead SMS is refused", r.status_code == 409, r.text[:150])
        check("refusal cites policy", "policy" in r.text.lower(), r.text[:150])

        r = client.post(
            f"/api/deals/{list_lead['id']}/contact?channel=call&template_key=voice_script"
        )
        check("list lead call is allowed", r.status_code == 200, r.text[:150])

        r = client.delete(f"/api/deals/{list_lead['id']}/consent")
        check("consent revoke endpoint works", r.status_code == 200)

        r = client.post(f"/api/deals/{list_lead['id']}/consent", json={"source": ""})
        check("consent without a source is refused", r.status_code == 422)

        r = client.post(
            f"/api/deals/{list_lead['id']}/consent",
            json={"source": "phone_call smoke-test", "evidence": "verified in smoke run"},
        )
        check("consent with a source is recorded", r.status_code == 200, r.text[:150])
        if r.status_code == 200:
            check("consent unlocks SMS", r.json()["deal"]["sms_allowed"] is True)
            check("consent reclassifies to inbound", r.json()["deal"]["lead_class"] == "inbound")

    if inbound_lead:
        r = client.post(
            f"/api/deals/{inbound_lead['id']}/contact?channel=sms&template_key=sms_opener"
        )
        check("consented inbound SMS accepted", r.status_code == 200, r.text[:150])
        if r.status_code == 200:
            check("send is simulated without credentials", r.json()["simulated"] is True)

        detail = client.get(f"/api/deals/{inbound_lead['id']}").json()
        body_text = detail["contacts"][-1]["body"] if detail["contacts"] else ""
        name = (inbound_lead.get("owner_first_name") or inbound_lead.get("owner") or "").strip()
        first = name.split()[0] if name else ""
        check("message is personalized", bool(first) and first in body_text, body_text[:120])
        check("no leaked placeholders", "{" not in body_text, body_text[:100])
        check("opt-out notice present", "STOP" in body_text, body_text[:120])
        check("property address rendered", inbound_lead["street"] in body_text, body_text[:120])

        r2 = client.post(
            f"/api/deals/{inbound_lead['id']}/contact?channel=sms&template_key=sms_followup_day1"
        )
        check("second SMS same day blocked by cap", r2.status_code == 409, r2.text[:120])

    print("\n[email]")
    # Drive the engine so the list cadence reaches its email step (step 2).
    for _ in range(3):
        client.post("/api/pipeline/send-due")

    list_with_email = None
    for d in client.get("/api/deals?limit=200").json():
        if (
            d["lead_class"] == "list"
            and d.get("email")
            and "email" in (d["allowed_channels"] or [])
        ):
            list_with_email = d
            break
    check("a list lead can receive email", list_with_email is not None)

    if list_with_email:
        r = client.post(
            f"/api/deals/{list_with_email['id']}/contact"
            f"?channel=email&template_key=email_list_intro"
        )
        check("list lead email sends", r.status_code == 200, r.text[:150])
        if r.status_code == 200:
            check("email is simulated without credentials", r.json()["simulated"] is True)

        detail = client.get(f"/api/deals/{list_with_email['id']}").json()
        mails = [c for c in detail["contacts"] if c["channel"] == "email"]
        check("email body was recorded", len(mails) > 0)
        if mails:
            body_txt = mails[-1]["body"]
            check("email carries a postal address (CAN-SPAM)",
                  "postal" in body_txt.lower() or "Street" in body_txt)
            check("email carries an opt-out", "STOP" in body_txt)
            check("no unreplaced placeholders in email", "{" not in body_txt,
                  body_txt[:120])
            check("email is personalised", list_with_email["street"] in body_txt)

    csv_res = client.get("/api/mail-queue/export")
    check("mail queue export still available", csv_res.status_code == 200)

    print("\n[evidence guards]")
    if list_lead:
        r = client.post(f"/api/deals/{list_lead['id']}/send-to-jv")
        check("cannot market an unsigned deal", r.status_code == 409, r.text[:120])

        r = client.post(f"/api/deals/{list_lead['id']}/generate-contract")
        check("contract generated", r.status_code == 200, r.text[:200])
        if r.status_code == 200:
            check("contract names the property", list_lead["street"] in r.json()["body"])
            check("contract states AS-IS", "AS-IS" in r.json()["body"])

        r = client.post(
            f"/api/deals/{list_lead['id']}/sign",
            json={"signed_by": list_lead["owner"] or "Owner", "signature_data": "sig"},
        )
        check("contract signed", r.status_code == 200, r.text[:200])
        r = client.post(f"/api/deals/{list_lead['id']}/send-to-jv")
        check("signed deal can be marketed", r.status_code == 200, r.text[:200])

    print("\n[opt-out]")
    if inbound_lead:
        phone = inbound_lead["phone"]
        r = client.post(
            "/api/telnyx/message-webhook",
            json={
                "data": {
                    "event_type": "message.received",
                    "payload": {
                        "from": {"phone_number": phone},
                        "text": "STOP",
                        "id": "smoke-1",
                    },
                }
            },
        )
        check("inbound STOP accepted", r.status_code == 200 and r.json()["intent"] == "stop")
        d2 = client.get(f"/api/deals/{inbound_lead['id']}").json()["deal"]
        check("lead is opted out", d2["opted_out"] is True)
        r = client.post(
            f"/api/deals/{inbound_lead['id']}/contact?channel=sms&template_key=sms_opener"
        )
        check("opted-out lead cannot be contacted", r.status_code == 409, r.text[:120])
        r = client.post(
            f"/api/deals/{inbound_lead['id']}/consent",
            json={"source": "phone_call smoke"},
        )
        check("cannot re-consent an opted-out lead", r.status_code == 409, r.text[:120])
        supp = client.get("/api/compliance/suppressions").json()
        check("number added to suppression list", any(x["phone"] == phone for x in supp))

    print("\n[human handoff]")
    # A price question must escalate, pause automation, draft the contract,
    # and raise an alert for the operator.
    hot_lead = None
    for d in client.get("/api/deals?limit=100").json():
        if d["is_contactable"] and d["arv"] > 0:
            hot_lead = d
            break
    check("found a lead to escalate", hot_lead is not None)

    if hot_lead:
        r = client.post(
            "/api/telnyx/message-webhook",
            json={
                "data": {
                    "event_type": "message.received",
                    "payload": {
                        "from": {"phone_number": hot_lead["phone"]},
                        "text": "How much would you pay for it?",
                        "id": "hot-1",
                    },
                }
            },
        )
        check("price question is classified hot", r.json().get("hot") is True, r.text[:150])

        queue = client.get("/api/needs-you").json()
        entry = next((q for q in queue if q["id"] == hot_lead["id"]), None)
        check("lead appears in needs-you", entry is not None)
        if entry:
            check("queue shows what they said",
                  "how much" in (entry["what_they_said"] or "").lower())
            check("contract is drafted and ready", entry["contract_ready"] is True)
            check("next action is to sign/call",
                  "sign" in entry["next_action"].lower())
            check("offer is priced", entry["draft_offer"] > 0)

        alerts = client.get("/api/alerts").json()
        check("operator alert recorded",
              any(a["lead_id"] == hot_lead["id"] for a in alerts))

        detail = client.get(f"/api/deals/{hot_lead['id']}").json()
        check("lead status advanced to qualified+",
              detail["deal"]["status"] in ("qualified", "appointment"),
              detail["deal"]["status"])

        # Second hot reply must not raise a second interruption.
        client.post(
            "/api/telnyx/message-webhook",
            json={"data": {"event_type": "message.received", "payload": {
                "from": {"phone_number": hot_lead["phone"]},
                "text": "and can you call today?", "id": "hot-2"}}},
        )
        sms_alerts = [a for a in client.get("/api/alerts").json()
                      if a["lead_id"] == hot_lead["id"] and a["kind"] != "opt_out"]
        check("only one interruption per burst", len(sms_alerts) <= 2)

        r = client.post(
            f"/api/deals/{hot_lead['id']}/attention/resolve",
            json={"note": "Called, seller wants to see the number"},
        )
        check("handoff can be resolved", r.status_code == 200)
        check("lead leaves the queue",
              not any(q["id"] == hot_lead["id"] for q in client.get("/api/needs-you").json()))

    print("\n[public website form]")
    r = client.post(
        "/api/public/lead",
        json={
            "name": "Marcus Bell",
            "phone": "214-555-0188",
            "email": "marcus@test.local",
            "street": "8815 Fair Oaks Ave",
            "city": "Dallas",
            "state": "TX",
            "zip_code": "75231",
            "situation": "Inherited the property, behind on taxes",
            "timeline": "As soon as possible",
            "sms_consent": True,
            "consent_text": "I agree to receive calls and texts about my property.",
            "page_url": "/sell",
        },
    )
    check("public form accepts a submission", r.status_code == 200, r.text[:150])
    pub_id = r.json().get("id")
    check("public response leaks nothing internal", set(r.json()) == {"ok", "id", "message"})

    if pub_id:
        deal = client.get(f"/api/deals/{pub_id}").json()["deal"]
        check("public lead is classed inbound", deal["lead_class"] == "inbound")
        check("public lead is textable with consent", deal["sms_allowed"] is True)
        check("consent source recorded", deal["consent_source"].startswith("web_form:"))
        check("public lead escalates immediately", deal["needs_attention"] is True)

    no_consent = client.post(
        "/api/public/lead",
        json={"name": "Pat No", "phone": "214-555-0199",
              "street": "9921 Noconsent Pkwy", "city": "Dallas", "state": "TX",
              "sms_consent": False},
    )
    if no_consent.status_code == 200:
        nc_id = no_consent.json()["id"]
        nc = client.get(f"/api/deals/{nc_id}").json()["deal"]
        check("no consent means no SMS", nc["sms_allowed"] is False)

    check("honeypot is silently dropped", client.post(
        "/api/public/lead",
        json={"name": "Bot", "phone": "2145550100", "street": "5500 Bot Trap Rd",
              "city": "Dallas", "state": "TX", "company_website": "spam"},
    ).json().get("id") is None)

    pub = client.get("/sell")
    check("seller landing page is public",
          pub.status_code == 200 and "sms_consent" in pub.text)

    print("\n[texting health]")
    r = client.post("/api/test-sms", json={"to": "+12145550199"})
    check("test-sms endpoint responds", r.status_code == 200, r.text[:150])
    if r.status_code == 200:
        check("test-sms reports dry-run honestly", r.json()["simulated"] is True)
        check("test-sms tells you what to configure", "TELNYX" in r.json()["next"].upper())
    check("test-sms rejects junk numbers",
          client.post("/api/test-sms", json={"to": "123"}).status_code == 422)

    print("\n[markets]")
    r = client.post("/api/pipeline/run", json={"states": ["TX", "FL"], "limit": 40})
    stats = r.json().get("stats", {})
    check("multi-market run completes", r.status_code == 200, r.text[:150])
    check("both markets pulled", len(stats.get("markets", {})) == 2,
          str(stats.get("markets")))

    print("\n[account]")
    r = client.get("/api/me")
    check("/api/me returns identity", r.status_code == 200)
    r = client.post(
        "/api/me/password",
        json={"current_password": "smoke-password-123", "new_password": "short"},
    )
    check("weak new password rejected", r.status_code == 422)

    r = client.post("/api/logout")
    check("logout ok", r.status_code == 200)
    client.cookies.clear()
    check("dashboard locked after logout", client.get("/api/dashboard").status_code == 401)

    server.should_exit = True
    thread.join(timeout=10)

    print(f"\n{'=' * 56}")
    print(f"  {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("\nFailures:")
        for f in FAIL:
            print("  -", f)
    print("=" * 56)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
