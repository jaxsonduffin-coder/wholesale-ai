"""
FastAPI Web Dashboard — Mobile-friendly PWA.
Jaxson can bookmark this on his phone like an app.
Features: View deals, approve/reject, e-sign contracts, forward to JV.
"""
import os
import json
import hashlib
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI, Request, Depends, HTTPException, Form, Body
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
import jwt

from config.settings import settings
from utils.database import SessionLocal, Property, Owner, Deal, ContactLog
from contracts.generator import ContractGenerator
from pipeline import WholesalePipeline

app = FastAPI(title="Wholesale AI", version="1.0")

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
    """Simple password login — returns JWT."""
    # Use SECRET_KEY as password for simplicity (change in production)
    if hashlib.sha256(password.encode()).hexdigest()[:16] == hashlib.sha256(SECRET_KEY.encode()).hexdigest()[:16]:
        token = create_token({"user": "admin"})
        return {"token": token}
    raise HTTPException(status_code=401, detail="Invalid password")


@app.get("/api/dashboard")
async def dashboard_stats(db: Session = Depends(get_db)):
    """Dashboard overview stats."""
    total_leads = db.query(Property).count()
    total_deals = db.query(Deal).count()
    a_deals = db.query(Deal).filter(Deal.grade == "A").count()
    b_deals = db.query(Deal).filter(Deal.grade == "B").count()
    under_contract = db.query(Deal).filter(Deal.status == "under_contract").count()
    signed = db.query(Deal).filter(Deal.contract_signed == True).count()

    return {
        "total_leads": total_leads,
        "total_deals": total_deals,
        "a_deals": a_deals,
        "b_deals": b_deals,
        "under_contract": under_contract,
        "signed_contracts": signed,
    }


@app.get("/api/deals")
async def list_deals(
    grade: str = None,
    status: str = None,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    """List deals with optional filters."""
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
    """Get full deal details."""
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
            "id": deal.id,
            "grade": deal.grade,
            "score": deal.score,
            "arv": deal.arv,
            "max_offer": deal.max_offer,
            "repair_estimate": deal.repair_estimate,
            "repair_level": deal.repair_level,
            "assignment_fee": deal.assignment_fee,
            "motivation_score": deal.motivation_score,
            "status": deal.status,
            "offered_price": deal.offered_price,
            "accepted_price": deal.accepted_price,
            "contract_signed": deal.contract_signed,
            "sent_to_jv": deal.sent_to_jv,
        },
        "property": {
            "street": prop.street,
            "city": prop.city,
            "county": prop.county,
            "state": prop.state,
            "zip_code": prop.zip_code,
            "type": prop.property_type,
            "bedrooms": prop.bedrooms,
            "bathrooms": prop.bathrooms,
            "sqft": prop.sqft,
            "year_built": prop.year_built,
            "tax_assessed_value": prop.tax_assessed_value,
            "tax_delinquent": prop.tax_delinquent,
            "pre_foreclosure": prop.pre_foreclosure,
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
                "method": c.method,
                "language": c.language,
                "direction": c.direction,
                "message": c.message,
                "response": c.response_text,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in contacts
        ],
    }


@app.post("/api/deals/{deal_id}/approve")
async def approve_deal(deal_id: int, offered_price: float = Body(...), db: Session = Depends(get_db)):
    """Approve a deal and generate contract."""
    deal = db.query(Deal).filter_by(id=deal_id).first()
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")

    deal.offered_price = offered_price
    deal.status = "negotiating"
    db.commit()

    return {"status": "approved", "offered_price": offered_price}


@app.post("/api/deals/{deal_id}/generate-contract")
async def generate_contract(deal_id: int, db: Session = Depends(get_db)):
    """Generate a purchase agreement for a deal."""
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
    deal_id: int,
    request: Request,
    signature_data: str = Body(...),
    db: Session = Depends(get_db),
):
    """Apply e-signature to contract."""
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
    deal.signature_data = signature_data[:500]  # Store truncated for reference
    db.commit()

    # Generate PDF
    pdf_path = gen.generate_pdf(deal.contract_path)
    if pdf_path:
        deal.contract_path = pdf_path
        db.commit()

    return {"status": "signed", "audit": audit}


@app.post("/api/deals/{deal_id}/send-to-jv")
async def send_to_jv(deal_id: int, db: Session = Depends(get_db)):
    """Forward signed contract to JV partner."""
    pipeline = WholesalePipeline()
    success = pipeline.forward_to_jv_partner(deal_id)

    if success:
        return {"status": "sent"}
    raise HTTPException(status_code=500, detail="Failed to send to JV partner")


# ── Frontend (Single HTML Page — PWA) ────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def frontend():
    """Serve the PWA dashboard — one single HTML file."""
    return DASHBOARD_HTML


# ── Dashboard HTML ───────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="theme-color" content="#1a1a2e">
    <title>Wholesale AI</title>
    <link rel="manifest" href="/manifest.json">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        :root {
            --bg: #0f0f23;
            --card: #1a1a2e;
            --accent: #00d4aa;
            --accent2: #7c3aed;
            --text: #e2e8f0;
            --muted: #94a3b8;
            --danger: #ef4444;
            --warning: #f59e0b;
            --success: #22c55e;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg);
            color: var(--text);
            min-height: 100vh;
            padding: 16px;
            padding-bottom: 80px;
        }
        h1 { font-size: 1.5rem; margin-bottom: 16px; }
        h2 { font-size: 1.1rem; color: var(--accent); margin-bottom: 8px; }

        .stats-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 8px;
            margin-bottom: 20px;
        }
        .stat-card {
            background: var(--card);
            border-radius: 12px;
            padding: 12px;
            text-align: center;
        }
        .stat-number { font-size: 1.5rem; font-weight: 700; color: var(--accent); }
        .stat-label { font-size: 0.7rem; color: var(--muted); text-transform: uppercase; }

        .deal-card {
            background: var(--card);
            border-radius: 12px;
            padding: 16px;
            margin-bottom: 12px;
            border-left: 4px solid var(--muted);
            cursor: pointer;
            transition: transform 0.1s;
        }
        .deal-card:active { transform: scale(0.98); }
        .deal-card.grade-A { border-left-color: var(--success); }
        .deal-card.grade-B { border-left-color: var(--accent); }
        .deal-card.grade-C { border-left-color: var(--warning); }

        .deal-header { display: flex; justify-content: space-between; align-items: center; }
        .deal-grade {
            font-size: 1.2rem; font-weight: 700; width: 36px; height: 36px;
            display: flex; align-items: center; justify-content: center;
            border-radius: 8px; background: rgba(0,212,170,0.15); color: var(--accent);
        }
        .deal-grade.A { background: rgba(34,197,94,0.15); color: var(--success); }
        .deal-grade.B { background: rgba(0,212,170,0.15); color: var(--accent); }
        .deal-grade.C { background: rgba(245,158,11,0.15); color: var(--warning); }

        .deal-address { font-weight: 600; font-size: 0.95rem; }
        .deal-owner { color: var(--muted); font-size: 0.8rem; }
        .deal-numbers { display: flex; gap: 16px; margin-top: 8px; font-size: 0.85rem; }
        .deal-numbers span { color: var(--muted); }
        .deal-numbers strong { color: var(--text); }

        .lang-badge {
            font-size: 0.65rem; padding: 2px 6px; border-radius: 4px;
            background: rgba(124,58,237,0.2); color: var(--accent2);
            text-transform: uppercase; font-weight: 600;
        }

        .btn {
            display: inline-flex; align-items: center; justify-content: center;
            padding: 12px 24px; border-radius: 10px; border: none;
            font-weight: 600; font-size: 0.95rem; cursor: pointer;
            transition: all 0.15s;
        }
        .btn-primary { background: var(--accent); color: #000; width: 100%; }
        .btn-danger { background: var(--danger); color: #fff; }
        .btn-success { background: var(--success); color: #fff; width: 100%; }

        .tabs {
            display: flex; gap: 4px; margin-bottom: 16px;
            background: var(--card); border-radius: 10px; padding: 4px;
        }
        .tab {
            flex: 1; padding: 10px; text-align: center; border-radius: 8px;
            font-size: 0.8rem; font-weight: 600; cursor: pointer;
            color: var(--muted); transition: all 0.15s;
        }
        .tab.active { background: var(--accent); color: #000; }

        /* Signature pad */
        .sig-container {
            background: #fff; border-radius: 12px; margin: 12px 0;
            touch-action: none;
        }
        .sig-container canvas { border-radius: 12px; width: 100%; height: 200px; }

        /* Detail view */
        .detail-section { margin-bottom: 16px; }
        .detail-row { display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid rgba(255,255,255,0.05); }
        .detail-label { color: var(--muted); font-size: 0.85rem; }
        .detail-value { font-weight: 600; font-size: 0.85rem; }

        .back-btn {
            display: inline-flex; align-items: center; gap: 4px;
            color: var(--accent); font-size: 0.9rem; cursor: pointer;
            margin-bottom: 12px; background: none; border: none; font-weight: 600;
        }

        .status-badge {
            padding: 4px 10px; border-radius: 20px; font-size: 0.75rem;
            font-weight: 600; text-transform: uppercase;
        }
        .status-new { background: rgba(0,212,170,0.15); color: var(--accent); }
        .status-contacted { background: rgba(124,58,237,0.15); color: var(--accent2); }
        .status-under_contract { background: rgba(245,158,11,0.15); color: var(--warning); }
        .status-signed { background: rgba(34,197,94,0.15); color: var(--success); }

        #app { max-width: 480px; margin: 0 auto; }
        .loading { text-align: center; padding: 40px; color: var(--muted); }
    </style>
</head>
<body>
    <div id="app">
        <div id="view-list">
            <h1>🏠 Wholesale AI</h1>
            <div id="stats" class="stats-grid"></div>
            <div class="tabs">
                <div class="tab active" onclick="filterDeals('all')">All</div>
                <div class="tab" onclick="filterDeals('A')">🔥 A</div>
                <div class="tab" onclick="filterDeals('B')">👍 B</div>
                <div class="tab" onclick="filterDeals('signed')">✍️ Signed</div>
            </div>
            <div id="deals-list"></div>
        </div>

        <div id="view-detail" style="display:none">
            <button class="back-btn" onclick="showList()">← Back to Deals</button>
            <div id="deal-detail"></div>
        </div>
    </div>

    <script>
        const API = '';
        let allDeals = [];
        let currentFilter = 'all';

        async function loadDashboard() {
            try {
                const [statsRes, dealsRes] = await Promise.all([
                    fetch(API + '/api/dashboard'),
                    fetch(API + '/api/deals?limit=100')
                ]);
                const stats = await statsRes.json();
                const deals = await dealsRes.json();
                allDeals = deals;
                renderStats(stats);
                renderDeals(deals);
            } catch (e) {
                document.getElementById('deals-list').innerHTML =
                    '<p class="loading">Unable to load. Check connection.</p>';
            }
        }

        function renderStats(s) {
            document.getElementById('stats').innerHTML = `
                <div class="stat-card"><div class="stat-number">${s.total_leads}</div><div class="stat-label">Leads</div></div>
                <div class="stat-card"><div class="stat-number">${s.a_deals + s.b_deals}</div><div class="stat-label">Hot Deals</div></div>
                <div class="stat-card"><div class="stat-number">${s.signed_contracts}</div><div class="stat-label">Signed</div></div>
            `;
        }

        function renderDeals(deals) {
            const el = document.getElementById('deals-list');
            if (!deals.length) {
                el.innerHTML = '<p class="loading">No deals yet. Pipeline is running...</p>';
                return;
            }
            el.innerHTML = deals.map(d => `
                <div class="deal-card grade-${d.grade}" onclick="showDeal(${d.id})">
                    <div class="deal-header">
                        <div>
                            <div class="deal-address">${d.address}</div>
                            <div class="deal-owner">${d.owner} ${d.language === 'es' ? '<span class="lang-badge">ES</span>' : ''}</div>
                        </div>
                        <div class="deal-grade ${d.grade}">${d.grade}</div>
                    </div>
                    <div class="deal-numbers">
                        <div><span>ARV</span> <strong>$${fmt(d.arv)}</strong></div>
                        <div><span>Offer</span> <strong>$${fmt(d.max_offer)}</strong></div>
                        <div><span>Fee</span> <strong>$${fmt(d.assignment_fee)}</strong></div>
                    </div>
                </div>
            `).join('');
        }

        function filterDeals(filter) {
            currentFilter = filter;
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            event.target.classList.add('active');

            let filtered = allDeals;
            if (filter === 'A') filtered = allDeals.filter(d => d.grade === 'A');
            else if (filter === 'B') filtered = allDeals.filter(d => d.grade === 'B');
            else if (filter === 'signed') filtered = allDeals.filter(d => d.contract_signed);
            renderDeals(filtered);
        }

        async function showDeal(id) {
            const res = await fetch(API + `/api/deals/${id}`);
            const data = await res.json();

            document.getElementById('view-list').style.display = 'none';
            document.getElementById('view-detail').style.display = 'block';

            const d = data.deal;
            const p = data.property;
            const o = data.owner;

            document.getElementById('deal-detail').innerHTML = `
                <div class="deal-header" style="margin-bottom:16px">
                    <div>
                        <h2>${p.street}</h2>
                        <div class="deal-owner">${p.city}, ${p.state} ${p.zip_code}</div>
                    </div>
                    <div class="deal-grade ${d.grade}" style="font-size:1.5rem;width:48px;height:48px">${d.grade}</div>
                </div>

                <span class="status-badge status-${d.status}">${d.status.replace('_',' ')}</span>

                <div class="detail-section" style="margin-top:16px">
                    <h2>💰 Deal Numbers</h2>
                    <div class="detail-row"><span class="detail-label">ARV</span><span class="detail-value">$${fmt(d.arv)}</span></div>
                    <div class="detail-row"><span class="detail-label">Repairs (${d.repair_level})</span><span class="detail-value">$${fmt(d.repair_estimate)}</span></div>
                    <div class="detail-row"><span class="detail-label">Max Offer (50%)</span><span class="detail-value" style="color:var(--accent)">$${fmt(d.max_offer)}</span></div>
                    <div class="detail-row"><span class="detail-label">Assignment Fee</span><span class="detail-value" style="color:var(--success)">$${fmt(d.assignment_fee)}</span></div>
                    <div class="detail-row"><span class="detail-label">Motivation</span><span class="detail-value">${d.motivation_score}/100</span></div>
                </div>

                <div class="detail-section">
                    <h2>🏠 Property</h2>
                    <div class="detail-row"><span class="detail-label">Type</span><span class="detail-value">${p.type || 'N/A'}</span></div>
                    <div class="detail-row"><span class="detail-label">Bed/Bath</span><span class="detail-value">${p.bedrooms || '?'}bd / ${p.bathrooms || '?'}ba</span></div>
                    <div class="detail-row"><span class="detail-label">Sqft</span><span class="detail-value">${p.sqft ? p.sqft.toLocaleString() : 'N/A'}</span></div>
                    <div class="detail-row"><span class="detail-label">Year Built</span><span class="detail-value">${p.year_built || 'N/A'}</span></div>
                    <div class="detail-row"><span class="detail-label">Tax Value</span><span class="detail-value">${p.tax_assessed_value ? '$'+fmt(p.tax_assessed_value) : 'N/A'}</span></div>
                    ${p.tax_delinquent ? '<div class="detail-row"><span class="detail-label">⚠️ Tax Delinquent</span><span class="detail-value" style="color:var(--danger)">Yes</span></div>' : ''}
                    ${p.pre_foreclosure ? '<div class="detail-row"><span class="detail-label">⚠️ Pre-Foreclosure</span><span class="detail-value" style="color:var(--danger)">Yes</span></div>' : ''}
                    ${p.vacant ? '<div class="detail-row"><span class="detail-label">🏚️ Vacant</span><span class="detail-value" style="color:var(--warning)">Yes</span></div>' : ''}
                </div>

                ${o ? `
                <div class="detail-section">
                    <h2>👤 Owner ${o.language === 'es' ? '<span class="lang-badge">Español</span>' : ''}</h2>
                    <div class="detail-row"><span class="detail-label">Name</span><span class="detail-value">${o.name}</span></div>
                    <div class="detail-row"><span class="detail-label">Phone</span><span class="detail-value"><a href="tel:${o.phone}" style="color:var(--accent)">${o.phone || 'N/A'}</a></span></div>
                    <div class="detail-row"><span class="detail-label">Email</span><span class="detail-value">${o.email || 'N/A'}</span></div>
                </div>` : ''}

                <div style="margin-top:20px">
                    ${!d.contract_signed ? `
                        <button class="btn btn-primary" onclick="generateContract(${d.id})" style="margin-bottom:8px">📄 Generate Contract</button>
                        <div id="sig-area"></div>
                    ` : `
                        ${!d.sent_to_jv ? `<button class="btn btn-success" onclick="sendToJV(${d.id})">📤 Send to JV Partner</button>` : '<p style="text-align:center;color:var(--success)">✅ Sent to JV Partner</p>'}
                    `}
                </div>
            `;
        }

        async function generateContract(dealId) {
            const res = await fetch(API + `/api/deals/${dealId}/generate-contract`, { method: 'POST' });
            if (res.ok) {
                document.getElementById('sig-area').innerHTML = `
                    <h2 style="margin-top:16px">✍️ Sign Contract</h2>
                    <p style="color:var(--muted);font-size:0.85rem;margin-bottom:8px">Draw your signature below:</p>
                    <div class="sig-container">
                        <canvas id="sig-canvas" width="340" height="200"></canvas>
                    </div>
                    <div style="display:flex;gap:8px">
                        <button class="btn btn-primary" onclick="submitSignature(${dealId})" style="flex:2">✅ Sign & Submit</button>
                        <button class="btn" onclick="clearSig()" style="flex:1;background:var(--card);color:var(--text)">Clear</button>
                    </div>
                `;
                initSignaturePad();
            }
        }

        let sigCanvas, sigCtx, isDrawing = false;
        function initSignaturePad() {
            sigCanvas = document.getElementById('sig-canvas');
            sigCtx = sigCanvas.getContext('2d');
            sigCtx.strokeStyle = '#000';
            sigCtx.lineWidth = 2;
            sigCtx.lineCap = 'round';

            const getPos = (e) => {
                const r = sigCanvas.getBoundingClientRect();
                const touch = e.touches ? e.touches[0] : e;
                return { x: touch.clientX - r.left, y: touch.clientY - r.top };
            };

            sigCanvas.addEventListener('mousedown', (e) => { isDrawing = true; sigCtx.beginPath(); const p = getPos(e); sigCtx.moveTo(p.x, p.y); });
            sigCanvas.addEventListener('mousemove', (e) => { if (!isDrawing) return; const p = getPos(e); sigCtx.lineTo(p.x, p.y); sigCtx.stroke(); });
            sigCanvas.addEventListener('mouseup', () => isDrawing = false);

            sigCanvas.addEventListener('touchstart', (e) => { e.preventDefault(); isDrawing = true; sigCtx.beginPath(); const p = getPos(e); sigCtx.moveTo(p.x, p.y); });
            sigCanvas.addEventListener('touchmove', (e) => { e.preventDefault(); if (!isDrawing) return; const p = getPos(e); sigCtx.lineTo(p.x, p.y); sigCtx.stroke(); });
            sigCanvas.addEventListener('touchend', () => isDrawing = false);
        }

        function clearSig() { sigCtx.clearRect(0, 0, sigCanvas.width, sigCanvas.height); }

        async function submitSignature(dealId) {
            const sigData = sigCanvas.toDataURL('image/png');
            const res = await fetch(API + `/api/deals/${dealId}/sign`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(sigData)
            });
            if (res.ok) {
                alert('Contract signed! ✅');
                showDeal(dealId);
            }
        }

        async function sendToJV(dealId) {
            if (!confirm('Send signed contract to JV partner?')) return;
            const res = await fetch(API + `/api/deals/${dealId}/send-to-jv`, { method: 'POST' });
            if (res.ok) {
                alert('Sent to JV partner! 📤');
                showDeal(dealId);
            }
        }

        function showList() {
            document.getElementById('view-detail').style.display = 'none';
            document.getElementById('view-list').style.display = 'block';
        }

        function fmt(n) { return n ? Math.round(n).toLocaleString() : '0'; }

        // Load on start
        loadDashboard();
        // Auto-refresh every 5 minutes
        setInterval(loadDashboard, 300000);
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
        "background_color": "#0f0f23",
        "theme_color": "#00d4aa",
        "icons": [],
    })
