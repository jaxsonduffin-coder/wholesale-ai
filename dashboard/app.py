"""
FastAPI Web Dashboard — AI Dream Team-style PWA.
Pipeline funnel, AI agent activity cards, deal metrics, live feed.
Mobile-first dark theme with gradient accents.
"""
import os
import json
import hashlib
from datetime import datetime, timedelta, date
from typing import Optional
from fastapi import FastAPI, Request, Depends, HTTPException, Form, Body
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, and_
import jwt

from config.settings import settings
from utils.database import (
    SessionLocal, Property, Owner, Deal, ContactLog,
    SMSDrip, EmailCampaign,
)
from contracts.generator import ContractGenerator
from pipeline import WholesalePipeline

app = FastAPI(title="Wholesale AI", version="2.0")

# ── Auth ─────────────────────────────────────────────────────────

SECRET_KEY = settings.server.secret_key
ALGORITHM = settings.server.jwt_algorithm


def create_token(data: dict) -> str:
    expire = datetime.utcnow() + timedelta(hours=settings.server.jwt_expiration_hours)
    return jwt.encode({**data, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)


def verify_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── API Routes ───────────────────────────────────────────────────

@app.post("/api/login")
async def login(password: str = Form(...)):
    if hashlib.sha256(password.encode()).hexdigest()[:16] == hashlib.sha256(SECRET_KEY.encode()).hexdigest()[:16]:
        token = create_token({"user": "admin"})
        return {"token": token}
    raise HTTPException(status_code=401, detail="Invalid password")


@app.get("/api/dashboard")
async def dashboard_stats(db: Session = Depends(get_db)):
    """Full dashboard stats — pipeline funnel + agent activity + metrics."""
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = today - timedelta(days=7)

    # Pipeline funnel counts
    total_leads = db.query(Property).count()
    skip_traced = db.query(Owner).filter(Owner.phone_1 != None).count()
    total_deals = db.query(Deal).count()
    contacted = db.query(Deal).filter(Deal.status.in_(["contacted", "interested", "negotiating", "under_contract", "closed"])).count()
    interested = db.query(Deal).filter(Deal.status.in_(["interested", "negotiating", "under_contract", "closed"])).count()
    under_contract = db.query(Deal).filter(Deal.status.in_(["under_contract", "closed"])).count()
    signed = db.query(Deal).filter(Deal.contract_signed == True).count()
    sent_jv = db.query(Deal).filter(Deal.sent_to_jv == True).count()

    # Today's agent activity
    calls_today = db.query(ContactLog).filter(
        and_(ContactLog.method == "voice", ContactLog.created_at >= today)
    ).count()
    sms_today = db.query(ContactLog).filter(
        and_(ContactLog.method == "sms", ContactLog.created_at >= today)
    ).count()
    emails_today = db.query(ContactLog).filter(
        and_(ContactLog.method == "email", ContactLog.created_at >= today)
    ).count()
    leads_today = db.query(Property).filter(Property.created_at >= today).count()

    # This week activity
    calls_week = db.query(ContactLog).filter(
        and_(ContactLog.method == "voice", ContactLog.created_at >= week_ago)
    ).count()
    sms_week = db.query(ContactLog).filter(
        and_(ContactLog.method == "sms", ContactLog.created_at >= week_ago)
    ).count()
    emails_week = db.query(ContactLog).filter(
        and_(ContactLog.method == "email", ContactLog.created_at >= week_ago)
    ).count()
    leads_week = db.query(Property).filter(Property.created_at >= week_ago).count()

    # Deal grades
    a_deals = db.query(Deal).filter(Deal.grade == "A").count()
    b_deals = db.query(Deal).filter(Deal.grade == "B").count()
    c_deals = db.query(Deal).filter(Deal.grade == "C").count()

    # Response rate
    total_outbound = db.query(ContactLog).filter(ContactLog.direction == "outbound").count()
    total_responses = db.query(ContactLog).filter(ContactLog.responded == True).count()
    response_rate = round((total_responses / max(total_outbound, 1)) * 100, 1)

    # Revenue potential (sum of assignment fees for signed deals)
    revenue = db.query(func.sum(Deal.assignment_fee)).filter(Deal.contract_signed == True).scalar() or 0

    # Daily leads for the past 7 days (for sparkline)
    daily_leads = []
    for i in range(6, -1, -1):
        day_start = today - timedelta(days=i)
        day_end = day_start + timedelta(days=1)
        count = db.query(Property).filter(
            and_(Property.created_at >= day_start, Property.created_at < day_end)
        ).count()
        daily_leads.append({"day": (today - timedelta(days=i)).strftime("%a"), "count": count})

    return {
        "pipeline": {
            "leads": total_leads,
            "skip_traced": skip_traced,
            "analyzed": total_deals,
            "contacted": contacted,
            "interested": interested,
            "under_contract": under_contract,
            "signed": signed,
            "sent_to_jv": sent_jv,
        },
        "agents": {
            "calls_today": calls_today,
            "sms_today": sms_today,
            "emails_today": emails_today,
            "leads_today": leads_today,
            "calls_week": calls_week,
            "sms_week": sms_week,
            "emails_week": emails_week,
            "leads_week": leads_week,
        },
        "grades": {"A": a_deals, "B": b_deals, "C": c_deals},
        "metrics": {
            "response_rate": response_rate,
            "revenue_potential": revenue,
            "total_outbound": total_outbound,
        },
        "daily_leads": daily_leads,
    }


@app.get("/api/activity")
async def recent_activity(limit: int = 20, db: Session = Depends(get_db)):
    """Live activity feed — recent contacts, deals, events."""
    contacts = (
        db.query(ContactLog)
        .join(Property)
        .order_by(ContactLog.created_at.desc())
        .limit(limit)
        .all()
    )
    activities = []
    for c in contacts:
        icon = {"sms": "💬", "email": "📧", "voice": "📞"}.get(c.method, "📌")
        lang = f" ({c.language.upper()})" if c.language == "es" else ""
        addr = f"{c.property.street}, {c.property.city}" if c.property else "Unknown"
        status = "Responded ✅" if c.responded else "Sent"
        activities.append({
            "icon": icon,
            "text": f"{icon} {c.method.upper()}{lang} → {addr}",
            "detail": status,
            "time": c.created_at.isoformat() if c.created_at else None,
        })
    return activities


@app.get("/api/deals")
async def list_deals(
    grade: str = None,
    status: str = None,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    query = db.query(Deal).join(Property).join(Owner, isouter=True)
    if grade:
        query = query.filter(Deal.grade == grade.upper())
    if status:
        query = query.filter(Deal.status == status)
    deals = query.order_by(Deal.score.desc()).limit(limit).all()
    return [
        {
            "id": d.id,
            "address": f"{d.property.street} {d.property.city}, {d.property.state}",
            "owner": d.property.owner.full_name if d.property.owner else "Unknown",
            "language": d.property.owner.preferred_language if d.property.owner else "en",
            "grade": d.grade,
            "score": d.score,
            "arv": d.arv,
            "max_offer": d.max_offer,
            "repair_estimate": d.repair_estimate,
            "assignment_fee": d.assignment_fee,
            "motivation_score": d.motivation_score,
            "status": d.status,
            "contract_signed": d.contract_signed,
            "sent_to_jv": d.sent_to_jv,
        }
        for d in deals
    ]


@app.get("/api/deals/{deal_id}")
async def get_deal(deal_id: int, db: Session = Depends(get_db)):
    deal = db.query(Deal).filter_by(id=deal_id).first()
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")
    prop = deal.property
    owner = prop.owner
    contacts = (
        db.query(ContactLog)
        .filter_by(property_id=prop.id)
        .order_by(ContactLog.created_at.desc())
        .all()
    )
    return {
        "deal": {
            "id": deal.id, "grade": deal.grade, "score": deal.score,
            "arv": deal.arv, "max_offer": deal.max_offer,
            "repair_estimate": deal.repair_estimate, "repair_level": deal.repair_level,
            "assignment_fee": deal.assignment_fee, "motivation_score": deal.motivation_score,
            "status": deal.status, "offered_price": deal.offered_price,
            "accepted_price": deal.accepted_price, "contract_signed": deal.contract_signed,
            "sent_to_jv": deal.sent_to_jv,
        },
        "property": {
            "street": prop.street, "city": prop.city, "county": prop.county,
            "state": prop.state, "zip_code": prop.zip_code,
            "type": prop.property_type, "bedrooms": prop.bedrooms,
            "bathrooms": prop.bathrooms, "sqft": prop.sqft,
            "year_built": prop.year_built, "tax_assessed_value": prop.tax_assessed_value,
            "tax_delinquent": prop.tax_delinquent, "pre_foreclosure": prop.pre_foreclosure,
            "vacant": prop.vacant,
        },
        "owner": {
            "name": owner.full_name if owner else "",
            "phone": owner.phone_1 if owner else "",
            "email": owner.email_1 if owner else "",
            "language": owner.preferred_language if owner else "en",
        } if owner else None,
        "contacts": [
            {
                "method": c.method, "language": c.language,
                "direction": c.direction, "message": c.message,
                "response": c.response_text,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in contacts
        ],
    }


@app.post("/api/deals/{deal_id}/approve")
async def approve_deal(deal_id: int, offered_price: float = Body(...), db: Session = Depends(get_db)):
    deal = db.query(Deal).filter_by(id=deal_id).first()
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")
    deal.offered_price = offered_price
    deal.status = "negotiating"
    db.commit()
    return {"status": "approved", "offered_price": offered_price}


@app.post("/api/deals/{deal_id}/generate-contract")
async def generate_contract(deal_id: int, db: Session = Depends(get_db)):
    deal = db.query(Deal).filter_by(id=deal_id).first()
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")
    prop = deal.property
    owner = prop.owner
    lang = owner.preferred_language if owner else "en"
    gen = ContractGenerator()
    result = gen.generate_contract(
        property_data={
            "street": prop.street, "city": prop.city, "state": prop.state,
            "zip_code": prop.zip_code, "county": prop.county,
            "parcel_id": prop.parcel_id, "property_type": prop.property_type,
            "sqft": prop.sqft,
        },
        owner_data={
            "full_name": owner.full_name if owner else "",
            "mailing_address": owner.mailing_address if owner else "",
            "phone_1": owner.phone_1 if owner else "",
            "email_1": owner.email_1 if owner else "",
        },
        deal_data={
            "max_offer": deal.max_offer,
            "offered_price": deal.offered_price or deal.max_offer,
        },
        language=lang,
    )
    deal.contract_path = result["contract_path"]
    deal.status = "under_contract"
    db.commit()
    return {"status": "generated", "contract_path": result["contract_path"]}


@app.post("/api/deals/{deal_id}/sign")
async def sign_contract(
    deal_id: int, request: Request,
    signature_data: str = Body(...), db: Session = Depends(get_db),
):
    deal = db.query(Deal).filter_by(id=deal_id).first()
    if not deal or not deal.contract_path:
        raise HTTPException(status_code=404, detail="Contract not found")
    gen = ContractGenerator()
    ip = request.client.host if request.client else "unknown"
    audit = gen.apply_signature(
        contract_path=deal.contract_path,
        signature_data=signature_data,
        signer="buyer",
        signer_name=settings.contract.buyer_name,
        ip_address=ip,
    )
    deal.contract_signed = True
    deal.contract_signed_at = datetime.utcnow()
    deal.signature_data = signature_data[:500]
    db.commit()
    pdf_path = gen.generate_pdf(deal.contract_path)
    if pdf_path:
        deal.contract_path = pdf_path
        db.commit()
    return {"status": "signed", "audit": audit}


@app.post("/api/deals/{deal_id}/send-to-jv")
async def send_to_jv(deal_id: int, db: Session = Depends(get_db)):
    pipeline = WholesalePipeline()
    success = pipeline.forward_to_jv_partner(deal_id)
    if success:
        return {"status": "sent"}
    raise HTTPException(status_code=500, detail="Failed to send to JV partner")


# ── Telnyx Voice Webhook (Outbound AI Agent) ────────────────────

@app.post("/api/telnyx/voice-webhook")
async def telnyx_voice_webhook(request: Request):
    """Handle Telnyx Call Control events for the outbound AI voice agent."""
    from voice_agent.telnyx_caller import telnyx_agent
    try:
        event = await request.json()
        await telnyx_agent.handle_webhook(event)
    except Exception as e:
        logging.getLogger(__name__).error(f"Telnyx webhook error: {e}")
    # Always return 200 so Telnyx doesn't retry
    return JSONResponse({"status": "ok"})


@app.post("/api/outbound-call")
async def trigger_outbound_call(
    phone: str = Form(...),
    language: str = Form("en"),
    property_address: str = Form(""),
    owner_name: str = Form(""),
):
    """Manually trigger an outbound AI call from the dashboard."""
    from voice_agent.telnyx_caller import telnyx_agent
    result = await telnyx_agent.make_call(
        phone_number=phone,
        property_info={
            "street": property_address,
            "owner_name": owner_name,
        },
        language=language,
    )
    if result:
        return JSONResponse(result)
    raise HTTPException(status_code=500, detail="Failed to initiate call")


# ── Frontend (Single HTML Page — PWA) ────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def frontend():
    return DASHBOARD_HTML


# ══════════════════════════════════════════════════════════════════
# DASHBOARD HTML — AI Dream Team style
# ══════════════════════════════════════════════════════════════════

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="theme-color" content="#0a0a1a">
    <title>Wholesale AI</title>
    <link rel="manifest" href="/manifest.json">
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        :root{
            --bg:#06060f;
            --bg2:#0d0d1a;
            --card:#12122a;
            --card-hover:#181840;
            --border:#1e1e3a;
            --accent:#00e5a0;
            --accent-glow:rgba(0,229,160,.15);
            --accent2:#6366f1;
            --accent2-glow:rgba(99,102,241,.15);
            --accent3:#f472b6;
            --text:#e8eaed;
            --text2:#9ca3af;
            --muted:#6b7280;
            --danger:#ef4444;
            --warning:#f59e0b;
            --success:#22c55e;
            --radius:14px;
        }
        body{
            font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
            background:var(--bg);color:var(--text);min-height:100dvh;
            padding:0 0 90px 0;overflow-x:hidden;
        }

        /* ── Header ─────────────────────── */
        .header{
            background:linear-gradient(135deg,#0d0d1a 0%,#12122a 100%);
            padding:20px 16px 16px;border-bottom:1px solid var(--border);
            position:sticky;top:0;z-index:100;backdrop-filter:blur(20px);
        }
        .header-top{display:flex;justify-content:space-between;align-items:center}
        .logo{display:flex;align-items:center;gap:10px}
        .logo-icon{
            width:36px;height:36px;border-radius:10px;
            background:linear-gradient(135deg,var(--accent),var(--accent2));
            display:flex;align-items:center;justify-content:center;
            font-size:18px;font-weight:800;color:#000;
        }
        .logo-text{font-size:1.15rem;font-weight:700;letter-spacing:-.5px}
        .logo-sub{font-size:.65rem;color:var(--accent);text-transform:uppercase;letter-spacing:1px;font-weight:600}
        .status-dot{
            width:8px;height:8px;border-radius:50%;background:var(--accent);
            box-shadow:0 0 8px var(--accent);animation:pulse 2s infinite;
        }
        @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
        .header-status{display:flex;align-items:center;gap:6px;font-size:.75rem;color:var(--accent)}

        /* ── Bottom Nav ────────────────── */
        .bottom-nav{
            position:fixed;bottom:0;left:0;right:0;
            background:var(--bg2);border-top:1px solid var(--border);
            display:flex;padding:8px 0 max(8px,env(safe-area-inset-bottom));
            z-index:100;
        }
        .nav-item{
            flex:1;display:flex;flex-direction:column;align-items:center;gap:3px;
            font-size:.6rem;color:var(--muted);cursor:pointer;padding:6px 0;
            transition:color .15s;border:none;background:none;
        }
        .nav-item.active{color:var(--accent)}
        .nav-item svg{width:22px;height:22px;fill:currentColor}

        /* ── Cards ──────────────────────── */
        .page{padding:16px;max-width:600px;margin:0 auto;display:none}
        .page.active{display:block}

        .card{
            background:var(--card);border-radius:var(--radius);
            border:1px solid var(--border);padding:16px;margin-bottom:12px;
            transition:background .15s;
        }
        .card:hover{background:var(--card-hover)}
        .card-title{
            font-size:.7rem;text-transform:uppercase;letter-spacing:1px;
            color:var(--muted);font-weight:600;margin-bottom:12px;
            display:flex;align-items:center;gap:6px;
        }
        .card-title-dot{width:6px;height:6px;border-radius:50%}

        /* ── Pipeline Funnel ─────────── */
        .funnel{display:flex;flex-direction:column;gap:6px}
        .funnel-row{
            display:flex;align-items:center;gap:10px;padding:8px 12px;
            border-radius:10px;position:relative;overflow:hidden;
        }
        .funnel-bar{
            position:absolute;left:0;top:0;bottom:0;border-radius:10px;
            opacity:.12;transition:width .6s ease;
        }
        .funnel-label{
            font-size:.8rem;font-weight:500;color:var(--text2);flex:1;
            position:relative;z-index:1;
        }
        .funnel-count{
            font-size:1.1rem;font-weight:700;position:relative;z-index:1;
            font-variant-numeric:tabular-nums;
        }
        .funnel-pct{
            font-size:.7rem;color:var(--muted);width:42px;text-align:right;
            position:relative;z-index:1;
        }

        /* ── Agent Cards ──────────────── */
        .agents-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
        .agent-card{
            background:var(--card);border:1px solid var(--border);
            border-radius:var(--radius);padding:14px;text-align:center;
            position:relative;overflow:hidden;
        }
        .agent-card::before{
            content:'';position:absolute;top:0;left:0;right:0;height:3px;
        }
        .agent-card.calls::before{background:linear-gradient(90deg,var(--accent),var(--accent2))}
        .agent-card.sms::before{background:linear-gradient(90deg,var(--accent2),var(--accent3))}
        .agent-card.emails::before{background:linear-gradient(90deg,var(--accent3),var(--warning))}
        .agent-card.leads::before{background:linear-gradient(90deg,var(--warning),var(--accent))}
        .agent-icon{font-size:1.5rem;margin-bottom:6px}
        .agent-number{font-size:1.6rem;font-weight:800;font-variant-numeric:tabular-nums}
        .agent-label{font-size:.7rem;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-top:2px}
        .agent-sub{font-size:.65rem;color:var(--text2);margin-top:6px}

        /* ── Metrics Row ─────────────── */
        .metrics-row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}
        .metric{
            background:var(--card);border:1px solid var(--border);
            border-radius:var(--radius);padding:14px;text-align:center;
        }
        .metric-value{font-size:1.3rem;font-weight:700}
        .metric-label{font-size:.65rem;color:var(--muted);text-transform:uppercase;margin-top:4px}

        /* ── Grade Badges ────────────── */
        .grades-row{display:flex;gap:10px}
        .grade-pill{
            flex:1;display:flex;align-items:center;justify-content:center;gap:8px;
            padding:12px;border-radius:12px;font-weight:700;font-size:1rem;
        }
        .grade-pill.A{background:rgba(34,197,94,.1);color:var(--success);border:1px solid rgba(34,197,94,.2)}
        .grade-pill.B{background:rgba(0,229,160,.1);color:var(--accent);border:1px solid rgba(0,229,160,.2)}
        .grade-pill.C{background:rgba(245,158,11,.1);color:var(--warning);border:1px solid rgba(245,158,11,.2)}
        .grade-letter{font-size:.75rem;font-weight:600;opacity:.7}

        /* ── Activity Feed ───────────── */
        .feed-item{
            display:flex;gap:12px;padding:10px 0;
            border-bottom:1px solid rgba(255,255,255,.04);
            align-items:flex-start;
        }
        .feed-item:last-child{border-bottom:none}
        .feed-icon{
            width:34px;height:34px;border-radius:10px;
            display:flex;align-items:center;justify-content:center;
            font-size:.9rem;flex-shrink:0;background:var(--bg2);
        }
        .feed-text{font-size:.82rem;line-height:1.4;flex:1}
        .feed-status{font-size:.7rem;font-weight:600}
        .feed-time{font-size:.65rem;color:var(--muted);margin-top:2px}

        /* ── Deal Cards ─────────────── */
        .deal-card{
            background:var(--card);border:1px solid var(--border);
            border-radius:var(--radius);padding:16px;margin-bottom:10px;
            cursor:pointer;transition:all .15s;position:relative;overflow:hidden;
        }
        .deal-card:active{transform:scale(.98)}
        .deal-card::before{
            content:'';position:absolute;left:0;top:0;bottom:0;width:4px;
        }
        .deal-card.grade-A::before{background:var(--success)}
        .deal-card.grade-B::before{background:var(--accent)}
        .deal-card.grade-C::before{background:var(--warning)}
        .deal-card.grade-D::before,.deal-card.grade-F::before{background:var(--danger)}

        .deal-top{display:flex;justify-content:space-between;align-items:flex-start}
        .deal-addr{font-weight:600;font-size:.92rem}
        .deal-owner{color:var(--text2);font-size:.78rem;margin-top:2px;display:flex;align-items:center;gap:6px}
        .deal-grade-badge{
            font-size:1rem;font-weight:800;width:38px;height:38px;
            display:flex;align-items:center;justify-content:center;
            border-radius:10px;
        }
        .deal-grade-badge.A{background:rgba(34,197,94,.12);color:var(--success)}
        .deal-grade-badge.B{background:rgba(0,229,160,.12);color:var(--accent)}
        .deal-grade-badge.C{background:rgba(245,158,11,.12);color:var(--warning)}
        .deal-grade-badge.D,.deal-grade-badge.F{background:rgba(239,68,68,.12);color:var(--danger)}

        .deal-nums{display:flex;gap:16px;margin-top:10px}
        .deal-num{font-size:.8rem}
        .deal-num span{color:var(--muted);font-size:.7rem;display:block}
        .deal-num strong{color:var(--text);font-weight:600}

        .lang-badge{
            font-size:.6rem;padding:2px 6px;border-radius:4px;
            background:rgba(99,102,241,.15);color:var(--accent2);
            text-transform:uppercase;font-weight:700;
        }

        /* ── Tabs ───────────────────── */
        .tabs{
            display:flex;gap:4px;margin-bottom:14px;
            background:var(--card);border-radius:12px;padding:4px;
            border:1px solid var(--border);
        }
        .tab{
            flex:1;padding:9px;text-align:center;border-radius:9px;
            font-size:.75rem;font-weight:600;cursor:pointer;
            color:var(--muted);transition:all .15s;border:none;background:none;
        }
        .tab.active{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#000}

        /* ── Detail View ────────────── */
        .detail-section{margin-bottom:16px}
        .detail-row{
            display:flex;justify-content:space-between;padding:9px 0;
            border-bottom:1px solid rgba(255,255,255,.04);
        }
        .detail-label{color:var(--muted);font-size:.82rem}
        .detail-value{font-weight:600;font-size:.82rem}
        .back-btn{
            display:inline-flex;align-items:center;gap:6px;
            color:var(--accent);font-size:.85rem;cursor:pointer;
            margin-bottom:14px;background:none;border:none;font-weight:600;
        }
        .status-badge{
            padding:4px 12px;border-radius:20px;font-size:.7rem;
            font-weight:600;text-transform:uppercase;letter-spacing:.5px;
        }
        .status-new{background:rgba(0,229,160,.12);color:var(--accent)}
        .status-contacted{background:rgba(99,102,241,.12);color:var(--accent2)}
        .status-interested{background:rgba(244,114,182,.12);color:var(--accent3)}
        .status-negotiating{background:rgba(245,158,11,.12);color:var(--warning)}
        .status-under_contract{background:rgba(34,197,94,.12);color:var(--success)}
        .status-signed{background:rgba(34,197,94,.12);color:var(--success)}

        /* ── Buttons ────────────────── */
        .btn{
            display:inline-flex;align-items:center;justify-content:center;
            padding:13px 24px;border-radius:12px;border:none;
            font-weight:700;font-size:.9rem;cursor:pointer;transition:all .15s;
            width:100%;
        }
        .btn:active{transform:scale(.97)}
        .btn-primary{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#000}
        .btn-success{background:var(--success);color:#fff}
        .btn-outline{background:transparent;border:1px solid var(--border);color:var(--text)}

        /* ── Signature ─────────────── */
        .sig-container{
            background:#fff;border-radius:12px;margin:12px 0;
            touch-action:none;
        }
        .sig-container canvas{border-radius:12px;width:100%;height:200px}

        /* ── Sparkline (CSS) ─────── */
        .sparkline{display:flex;align-items:flex-end;gap:3px;height:40px;margin-top:8px}
        .spark-bar{
            flex:1;border-radius:3px 3px 0 0;
            background:linear-gradient(to top,var(--accent),var(--accent2));
            transition:height .4s ease;min-height:2px;
        }
        .spark-labels{display:flex;gap:3px;margin-top:4px}
        .spark-label{flex:1;text-align:center;font-size:.55rem;color:var(--muted)}

        .loading{text-align:center;padding:40px;color:var(--muted)}
        .section-gap{margin-bottom:16px}
    </style>
</head>
<body>

<!-- ── Header ───────────────────────────────────────────────── -->
<div class="header">
    <div class="header-top">
        <div class="logo">
            <div class="logo-icon">W</div>
            <div>
                <div class="logo-text">Wholesale AI</div>
                <div class="logo-sub">24/7 AI Sales Force</div>
            </div>
        </div>
        <div class="header-status">
            <div class="status-dot"></div>
            <span>Pipeline Active</span>
        </div>
    </div>
</div>

<!-- ── Pages ─────────────────────────────────────────────────── -->

<!-- Dashboard Page -->
<div class="page active" id="page-dashboard">
    <div id="agents-section"></div>
    <div id="pipeline-section"></div>
    <div id="metrics-section"></div>
    <div id="grades-section"></div>
    <div id="sparkline-section"></div>
</div>

<!-- Deals Page -->
<div class="page" id="page-deals">
    <div class="tabs" id="deal-tabs">
        <button class="tab active" onclick="filterDeals('all',this)">All</button>
        <button class="tab" onclick="filterDeals('A',this)">A Deals</button>
        <button class="tab" onclick="filterDeals('B',this)">B Deals</button>
        <button class="tab" onclick="filterDeals('signed',this)">Signed</button>
    </div>
    <div id="deals-list"></div>
</div>

<!-- Activity Page -->
<div class="page" id="page-activity">
    <div class="card">
        <div class="card-title"><div class="card-title-dot" style="background:var(--accent)"></div>Live Activity Feed</div>
        <div id="activity-feed"><p class="loading">Loading activity...</p></div>
    </div>
</div>

<!-- Deal Detail Page -->
<div class="page" id="page-detail">
    <button class="back-btn" onclick="showPage('deals')">← Back to Deals</button>
    <div id="deal-detail"></div>
</div>

<!-- ── Bottom Nav ────────────────────────────────────────────── -->
<nav class="bottom-nav">
    <button class="nav-item active" onclick="showPage('dashboard')">
        <svg viewBox="0 0 24 24"><path d="M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z"/></svg>
        <span>Dashboard</span>
    </button>
    <button class="nav-item" onclick="showPage('deals')">
        <svg viewBox="0 0 24 24"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm-5 14H7v-2h7v2zm3-4H7v-2h10v2zm0-4H7V7h10v2z"/></svg>
        <span>Deals</span>
    </button>
    <button class="nav-item" onclick="showPage('activity')">
        <svg viewBox="0 0 24 24"><path d="M13 3c-4.97 0-9 4.03-9 9H1l3.89 3.89.07.14L9 12H6c0-3.87 3.13-7 7-7s7 3.13 7 7-3.13 7-7 7c-1.93 0-3.68-.79-4.94-2.06l-1.42 1.42C8.27 19.99 10.51 21 13 21c4.97 0 9-4.03 9-9s-4.03-9-9-9zm-1 5v5l4.28 2.54.72-1.21-3.5-2.08V8H12z"/></svg>
        <span>Activity</span>
    </button>
</nav>

<script>
const API='';
let allDeals=[];
let dashData=null;

// ── Navigation ───────────────────────────────────
function showPage(page){
    document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
    document.getElementById('page-'+page).classList.add('active');
    // highlight nav
    const navs=document.querySelectorAll('.nav-item');
    if(page==='dashboard')navs[0].classList.add('active');
    else if(page==='deals')navs[1].classList.add('active');
    else if(page==='activity'){navs[2].classList.add('active');loadActivity();}
    else if(page==='detail')navs[1].classList.add('active');
}

// ── Load Dashboard ───────────────────────────────
async function loadDashboard(){
    try{
        const [statsRes,dealsRes]=await Promise.all([
            fetch(API+'/api/dashboard'),
            fetch(API+'/api/deals?limit=100')
        ]);
        dashData=await statsRes.json();
        allDeals=await dealsRes.json();
        renderAgents(dashData.agents);
        renderPipeline(dashData.pipeline);
        renderMetrics(dashData.metrics,dashData.pipeline);
        renderGrades(dashData.grades);
        renderSparkline(dashData.daily_leads);
        renderDeals(allDeals);
    }catch(e){
        document.getElementById('agents-section').innerHTML='<p class="loading">Unable to connect. Retrying...</p>';
        setTimeout(loadDashboard,5000);
    }
}

// ── Render: AI Agents ────────────────────────────
function renderAgents(a){
    document.getElementById('agents-section').innerHTML=`
        <div class="card section-gap">
            <div class="card-title"><div class="card-title-dot" style="background:var(--accent)"></div>AI Agents — Today</div>
            <div class="agents-grid">
                <div class="agent-card calls">
                    <div class="agent-icon">📞</div>
                    <div class="agent-number" style="color:var(--accent)">${a.calls_today}</div>
                    <div class="agent-label">Voice Calls</div>
                    <div class="agent-sub">${a.calls_week} this week</div>
                </div>
                <div class="agent-card sms">
                    <div class="agent-icon">💬</div>
                    <div class="agent-number" style="color:var(--accent2)">${a.sms_today}</div>
                    <div class="agent-label">SMS Sent</div>
                    <div class="agent-sub">${a.sms_week} this week</div>
                </div>
                <div class="agent-card emails">
                    <div class="agent-icon">📧</div>
                    <div class="agent-number" style="color:var(--accent3)">${a.emails_today}</div>
                    <div class="agent-label">Emails Sent</div>
                    <div class="agent-sub">${a.emails_week} this week</div>
                </div>
                <div class="agent-card leads">
                    <div class="agent-icon">🔍</div>
                    <div class="agent-number" style="color:var(--warning)">${a.leads_today}</div>
                    <div class="agent-label">New Leads</div>
                    <div class="agent-sub">${a.leads_week} this week</div>
                </div>
            </div>
        </div>`;
}

// ── Render: Pipeline Funnel ──────────────────────
function renderPipeline(p){
    const stages=[
        {label:'Leads Found',count:p.leads,color:'var(--accent2)'},
        {label:'Skip Traced',count:p.skip_traced,color:'var(--accent)'},
        {label:'Deals Analyzed',count:p.analyzed,color:'#22d3ee'},
        {label:'Contacted',count:p.contacted,color:'var(--accent3)'},
        {label:'Interested',count:p.interested,color:'var(--warning)'},
        {label:'Under Contract',count:p.under_contract,color:'var(--success)'},
        {label:'Signed',count:p.signed,color:'#a78bfa'},
        {label:'Sent to JV',count:p.sent_to_jv,color:'#f472b6'},
    ];
    const max=Math.max(...stages.map(s=>s.count),1);
    document.getElementById('pipeline-section').innerHTML=`
        <div class="card section-gap">
            <div class="card-title"><div class="card-title-dot" style="background:var(--accent2)"></div>Pipeline Funnel</div>
            <div class="funnel">
                ${stages.map(s=>{
                    const pct=max>0?Math.round((s.count/max)*100):0;
                    const convPct=stages[0].count>0?Math.round((s.count/stages[0].count)*100):0;
                    return `<div class="funnel-row">
                        <div class="funnel-bar" style="width:${pct}%;background:${s.color}"></div>
                        <span class="funnel-label">${s.label}</span>
                        <span class="funnel-count" style="color:${s.color}">${s.count.toLocaleString()}</span>
                        <span class="funnel-pct">${convPct}%</span>
                    </div>`;
                }).join('')}
            </div>
        </div>`;
}

// ── Render: Key Metrics ──────────────────────────
function renderMetrics(m,p){
    document.getElementById('metrics-section').innerHTML=`
        <div class="metrics-row section-gap">
            <div class="metric">
                <div class="metric-value" style="color:var(--accent)">${m.response_rate}%</div>
                <div class="metric-label">Response Rate</div>
            </div>
            <div class="metric">
                <div class="metric-value" style="color:var(--success)">$${Math.round(m.revenue_potential).toLocaleString()}</div>
                <div class="metric-label">Revenue</div>
            </div>
            <div class="metric">
                <div class="metric-value" style="color:var(--accent2)">${m.total_outbound.toLocaleString()}</div>
                <div class="metric-label">Total Outreach</div>
            </div>
        </div>`;
}

// ── Render: Grades ───────────────────────────────
function renderGrades(g){
    document.getElementById('grades-section').innerHTML=`
        <div class="grades-row section-gap">
            <div class="grade-pill A"><span class="grade-letter">A</span>${g.A}</div>
            <div class="grade-pill B"><span class="grade-letter">B</span>${g.B}</div>
            <div class="grade-pill C"><span class="grade-letter">C</span>${g.C}</div>
        </div>`;
}

// ── Render: Sparkline ────────────────────────────
function renderSparkline(data){
    if(!data||!data.length)return;
    const max=Math.max(...data.map(d=>d.count),1);
    document.getElementById('sparkline-section').innerHTML=`
        <div class="card">
            <div class="card-title"><div class="card-title-dot" style="background:var(--warning)"></div>Leads — Last 7 Days</div>
            <div class="sparkline">
                ${data.map(d=>`<div class="spark-bar" style="height:${Math.max((d.count/max)*100,5)}%"></div>`).join('')}
            </div>
            <div class="spark-labels">
                ${data.map(d=>`<span class="spark-label">${d.day}</span>`).join('')}
            </div>
        </div>`;
}

// ── Render: Deals List ───────────────────────────
function renderDeals(deals){
    const el=document.getElementById('deals-list');
    if(!deals.length){el.innerHTML='<p class="loading">No deals yet — pipeline is hunting...</p>';return;}
    el.innerHTML=deals.map(d=>`
        <div class="deal-card grade-${d.grade}" onclick="showDealDetail(${d.id})">
            <div class="deal-top">
                <div>
                    <div class="deal-addr">${d.address}</div>
                    <div class="deal-owner">${d.owner} ${d.language==='es'?'<span class="lang-badge">ES</span>':''}</div>
                </div>
                <div class="deal-grade-badge ${d.grade}">${d.grade}</div>
            </div>
            <div class="deal-nums">
                <div class="deal-num"><span>ARV</span><strong>$${fmt(d.arv)}</strong></div>
                <div class="deal-num"><span>Offer</span><strong>$${fmt(d.max_offer)}</strong></div>
                <div class="deal-num"><span>Fee</span><strong style="color:var(--success)">$${fmt(d.assignment_fee)}</strong></div>
                <div class="deal-num"><span>Score</span><strong>${Math.round(d.score||0)}</strong></div>
            </div>
        </div>`).join('');
}

function filterDeals(filter,el){
    document.querySelectorAll('#deal-tabs .tab').forEach(t=>t.classList.remove('active'));
    if(el)el.classList.add('active');
    let filtered=allDeals;
    if(filter==='A')filtered=allDeals.filter(d=>d.grade==='A');
    else if(filter==='B')filtered=allDeals.filter(d=>d.grade==='B');
    else if(filter==='signed')filtered=allDeals.filter(d=>d.contract_signed);
    renderDeals(filtered);
}

// ── Activity Feed ────────────────────────────────
async function loadActivity(){
    try{
        const res=await fetch(API+'/api/activity');
        const items=await res.json();
        const el=document.getElementById('activity-feed');
        if(!items.length){el.innerHTML='<p class="loading">No activity yet...</p>';return;}
        el.innerHTML=items.map(a=>{
            const t=a.time?timeAgo(new Date(a.time)):'';
            return `<div class="feed-item">
                <div class="feed-icon">${a.icon}</div>
                <div style="flex:1">
                    <div class="feed-text">${a.text}</div>
                    <div class="feed-time">${t}</div>
                </div>
                <div class="feed-status" style="color:${a.detail.includes('✅')?'var(--success)':'var(--muted)'}">${a.detail}</div>
            </div>`;
        }).join('');
    }catch(e){
        document.getElementById('activity-feed').innerHTML='<p class="loading">Unable to load activity.</p>';
    }
}

// ── Deal Detail ──────────────────────────────────
async function showDealDetail(id){
    showPage('detail');
    const res=await fetch(API+'/api/deals/'+id);
    const data=await res.json();
    const d=data.deal,p=data.property,o=data.owner;
    document.getElementById('deal-detail').innerHTML=`
        <div class="card" style="margin-bottom:16px">
            <div class="deal-top" style="margin-bottom:8px">
                <div>
                    <div style="font-size:1.1rem;font-weight:700">${p.street}</div>
                    <div style="color:var(--text2);font-size:.85rem">${p.city}, ${p.state} ${p.zip_code}</div>
                </div>
                <div class="deal-grade-badge ${d.grade}" style="font-size:1.3rem;width:46px;height:46px">${d.grade}</div>
            </div>
            <span class="status-badge status-${d.status}">${d.status.replace(/_/g,' ')}</span>
        </div>

        <div class="card">
            <div class="card-title"><div class="card-title-dot" style="background:var(--success)"></div>Deal Numbers</div>
            <div class="detail-row"><span class="detail-label">ARV</span><span class="detail-value">$${fmt(d.arv)}</span></div>
            <div class="detail-row"><span class="detail-label">Repairs (${d.repair_level||'est'})</span><span class="detail-value">$${fmt(d.repair_estimate)}</span></div>
            <div class="detail-row"><span class="detail-label">Max Offer (50%)</span><span class="detail-value" style="color:var(--accent)">$${fmt(d.max_offer)}</span></div>
            <div class="detail-row"><span class="detail-label">Assignment Fee</span><span class="detail-value" style="color:var(--success)">$${fmt(d.assignment_fee)}</span></div>
            <div class="detail-row"><span class="detail-label">Motivation</span><span class="detail-value">${d.motivation_score||0}/100</span></div>
        </div>

        <div class="card">
            <div class="card-title"><div class="card-title-dot" style="background:var(--accent2)"></div>Property Details</div>
            <div class="detail-row"><span class="detail-label">Type</span><span class="detail-value">${p.type||'N/A'}</span></div>
            <div class="detail-row"><span class="detail-label">Bed / Bath</span><span class="detail-value">${p.bedrooms||'?'}bd / ${p.bathrooms||'?'}ba</span></div>
            <div class="detail-row"><span class="detail-label">Sqft</span><span class="detail-value">${p.sqft?p.sqft.toLocaleString():'N/A'}</span></div>
            <div class="detail-row"><span class="detail-label">Year Built</span><span class="detail-value">${p.year_built||'N/A'}</span></div>
            <div class="detail-row"><span class="detail-label">Tax Value</span><span class="detail-value">${p.tax_assessed_value?'$'+fmt(p.tax_assessed_value):'N/A'}</span></div>
            ${p.tax_delinquent?'<div class="detail-row"><span class="detail-label" style="color:var(--danger)">Tax Delinquent</span><span class="detail-value" style="color:var(--danger)">Yes</span></div>':''}
            ${p.pre_foreclosure?'<div class="detail-row"><span class="detail-label" style="color:var(--danger)">Pre-Foreclosure</span><span class="detail-value" style="color:var(--danger)">Yes</span></div>':''}
            ${p.vacant?'<div class="detail-row"><span class="detail-label" style="color:var(--warning)">Vacant</span><span class="detail-value" style="color:var(--warning)">Yes</span></div>':''}
        </div>

        ${o?`<div class="card">
            <div class="card-title"><div class="card-title-dot" style="background:var(--accent3)"></div>Owner ${o.language==='es'?'<span class="lang-badge">Español</span>':''}</div>
            <div class="detail-row"><span class="detail-label">Name</span><span class="detail-value">${o.name}</span></div>
            <div class="detail-row"><span class="detail-label">Phone</span><span class="detail-value"><a href="tel:${o.phone}" style="color:var(--accent)">${o.phone||'N/A'}</a></span></div>
            <div class="detail-row"><span class="detail-label">Email</span><span class="detail-value">${o.email||'N/A'}</span></div>
        </div>`:''}

        <div style="margin-top:12px">
            ${!d.contract_signed?`
                <button class="btn btn-primary" onclick="genContract(${d.id})" style="margin-bottom:8px">Generate Contract</button>
                <div id="sig-area"></div>
            `:`${!d.sent_to_jv?`<button class="btn btn-success" onclick="sendJV(${d.id})">Send to JV Partner</button>`:'<div style="text-align:center;padding:16px;color:var(--success);font-weight:600">✅ Sent to JV Partner</div>'}`}
        </div>

        ${data.contacts.length?`<div class="card" style="margin-top:12px">
            <div class="card-title"><div class="card-title-dot" style="background:var(--accent)"></div>Contact History</div>
            ${data.contacts.slice(0,10).map(c=>`
                <div class="feed-item">
                    <div class="feed-icon">${{sms:'💬',email:'📧',voice:'📞'}[c.method]||'📌'}</div>
                    <div style="flex:1">
                        <div class="feed-text">${c.method.toUpperCase()} ${c.language==='es'?'(ES)':''} — ${c.direction}</div>
                        <div class="feed-time">${c.created_at?timeAgo(new Date(c.created_at)):''}</div>
                    </div>
                </div>`).join('')}
        </div>`:''}
    `;
}

// ── Contract & Signature ─────────────────────────
async function genContract(id){
    const res=await fetch(API+'/api/deals/'+id+'/generate-contract',{method:'POST'});
    if(res.ok){
        document.getElementById('sig-area').innerHTML=`
            <div class="card" style="margin-top:12px">
                <div class="card-title"><div class="card-title-dot" style="background:var(--accent)"></div>Sign Contract</div>
                <p style="color:var(--text2);font-size:.82rem;margin-bottom:10px">Draw your signature below:</p>
                <div class="sig-container"><canvas id="sig-canvas" width="340" height="200"></canvas></div>
                <div style="display:flex;gap:8px;margin-top:8px">
                    <button class="btn btn-primary" onclick="submitSig(${id})" style="flex:2">Sign & Submit</button>
                    <button class="btn btn-outline" onclick="clearSig()" style="flex:1">Clear</button>
                </div>
            </div>`;
        initSigPad();
    }
}

let sigCanvas,sigCtx,drawing=false;
function initSigPad(){
    sigCanvas=document.getElementById('sig-canvas');
    sigCtx=sigCanvas.getContext('2d');
    sigCtx.strokeStyle='#000';sigCtx.lineWidth=2;sigCtx.lineCap='round';
    const pos=e=>{const r=sigCanvas.getBoundingClientRect();const t=e.touches?e.touches[0]:e;return{x:t.clientX-r.left,y:t.clientY-r.top}};
    sigCanvas.onmousedown=e=>{drawing=true;sigCtx.beginPath();const p=pos(e);sigCtx.moveTo(p.x,p.y)};
    sigCanvas.onmousemove=e=>{if(!drawing)return;const p=pos(e);sigCtx.lineTo(p.x,p.y);sigCtx.stroke()};
    sigCanvas.onmouseup=()=>drawing=false;
    sigCanvas.ontouchstart=e=>{e.preventDefault();drawing=true;sigCtx.beginPath();const p=pos(e);sigCtx.moveTo(p.x,p.y)};
    sigCanvas.ontouchmove=e=>{e.preventDefault();if(!drawing)return;const p=pos(e);sigCtx.lineTo(p.x,p.y);sigCtx.stroke()};
    sigCanvas.ontouchend=()=>drawing=false;
}
function clearSig(){sigCtx.clearRect(0,0,sigCanvas.width,sigCanvas.height)}
async function submitSig(id){
    const data=sigCanvas.toDataURL('image/png');
    const res=await fetch(API+'/api/deals/'+id+'/sign',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    if(res.ok){alert('Contract signed! ✅');showDealDetail(id)}
}
async function sendJV(id){
    if(!confirm('Send signed contract to JV partner?'))return;
    const res=await fetch(API+'/api/deals/'+id+'/send-to-jv',{method:'POST'});
    if(res.ok){alert('Sent to JV partner! 📤');showDealDetail(id)}
}

// ── Helpers ──────────────────────────────────────
function fmt(n){return n?Math.round(n).toLocaleString():'0'}
function timeAgo(d){
    const s=Math.floor((Date.now()-d)/1000);
    if(s<60)return 'just now';
    if(s<3600)return Math.floor(s/60)+'m ago';
    if(s<86400)return Math.floor(s/3600)+'h ago';
    return Math.floor(s/86400)+'d ago';
}

// ── Init ─────────────────────────────────────────
loadDashboard();
setInterval(loadDashboard,300000); // refresh every 5 min
</script>
</body>
</html>"""


# ── PWA Manifest ─────────────────────────────────────────────────

@app.get("/manifest.json")
async def manifest():
    return JSONResponse({
        "name": "Wholesale AI",
        "short_name": "Wholesale",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#06060f",
        "theme_color": "#00e5a0",
        "icons": [],
    })
