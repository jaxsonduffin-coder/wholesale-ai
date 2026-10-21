'use strict';
/* Wholesale AI — dashboard client.
   Every value that comes from the server is inserted with esc() or textContent;
   nothing is concatenated into innerHTML unescaped. */

const API = '';
let state = { page: 'dash', grade: '', deals: [], caps: {}, dash: null };
const $ = (id) => document.getElementById(id);

function esc(v) {
  if (v === null || v === undefined) return '';
  return String(v).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function money(n) {
  const v = Number(n || 0);
  if (!isFinite(v) || v === 0) return '$0';
  const abs = Math.abs(v);
  const sign = v < 0 ? '-' : '';
  if (abs >= 1e6) return sign + '$' + (abs / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
  if (abs >= 1e3) return sign + '$' + Math.round(abs / 1e3) + 'k';
  return sign + '$' + Math.round(abs);
}

function moneyFull(n) {
  const v = Number(n || 0);
  return v.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
}

function timeAgo(iso) {
  if (!iso) return '';
  const d = new Date(iso.endsWith('Z') ? iso : iso + 'Z');
  const s = Math.max(0, (Date.now() - d.getTime()) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  if (s < 604800) return Math.floor(s / 86400) + 'd ago';
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function alertBox(el, kind, text) {
  if (!el) return;
  el.innerHTML = text ? `<div class="msg ${kind || 'info'}">${esc(text)}</div>` : '';
}

async function api(path, opts) {
  const res = await fetch(API + path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...(opts || {}),
  });
  if (res.status === 401) { showLogin(); throw new Error('unauthenticated'); }
  let body = null;
  const ct = res.headers.get('content-type') || '';
  if (ct.includes('application/json')) body = await res.json();
  if (!res.ok) {
    const detail = (body && (body.detail || body.message)) || res.statusText;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return body;
}

/* ── Auth ───────────────────────────────────────────────── */
function showLogin() {
  $('login').style.display = 'flex';
  $('top').style.display = 'none';
  $('tabs').style.display = 'none';
  $('main').style.display = 'none';
}

function showApp() {
  $('login').style.display = 'none';
  $('top').style.display = 'flex';
  $('tabs').style.display = 'flex';
  $('main').style.display = 'block';
}

$('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = $('login-btn');
  btn.disabled = true; btn.textContent = 'Signing in…';
  alertBox($('login-err'), '', '');
  try {
    await api('/api/login', {
      method: 'POST',
      body: JSON.stringify({ email: $('email').value, password: $('password').value }),
    });
    showApp();
    boot();
  } catch (err) {
    alertBox($('login-err'), 'err', err.message || 'Sign in failed');
  } finally {
    btn.disabled = false; btn.textContent = 'Sign in';
  }
});

$('logout-btn').addEventListener('click', async () => {
  try { await api('/api/logout', { method: 'POST' }); } catch (e) { /* ignore */ }
  showLogin();
});

/* ── Navigation ─────────────────────────────────────────── */
function go(page) {
  state.page = page;
  document.querySelectorAll('.page').forEach((p) => p.classList.remove('on'));
  const el = $('page-' + page);
  if (el) el.classList.add('on');
  document.querySelectorAll('nav.tabs button').forEach((b) =>
    b.classList.toggle('on', b.dataset.page === page ||
      (page === 'detail' && b.dataset.page === 'deals')));
  window.scrollTo({ top: 0, behavior: 'instant' });
  if (page === 'needs') loadNeedsYou();
  if (page === 'activity') loadActivity();
  if (page === 'cadence') loadCadence();
  if (page === 'settings') loadSettings();
  if (page === 'deals') loadDeals();
}

document.querySelectorAll('nav.tabs button').forEach((b) =>
  b.addEventListener('click', () => go(b.dataset.page)));

document.querySelectorAll('#grade-chips .chip').forEach((c) =>
  c.addEventListener('click', () => {
    document.querySelectorAll('#grade-chips .chip').forEach((x) => x.classList.remove('on'));
    c.classList.add('on');
    state.grade = c.dataset.grade;
    loadDeals();
  }));

/* ── Dashboard ──────────────────────────────────────────── */
async function loadDash() {
  const d = await api('/api/dashboard');
  state.dash = d;
  state.caps = d.capabilities || {};

  /* live indicator */
  const anyLive = ['sms', 'voice', 'email'].some((k) => d.capabilities?.[k]?.live);
  $('live-dot').className = 'dot' + (anyLive ? '' : ' dry');
  $('live-label').textContent = anyLive ? 'LIVE' : 'DRY RUN';
  $('brand-sub').textContent = d.agent_identity?.company || 'AI Sales Force';

  /* agents */
  const a = d.agents;
  const cards = [
    { n: a.calls_today, w: a.calls_week, l: 'Voice calls', c: 'var(--accent)', ic: '📞', ch: 'voice' },
    { n: a.sms_today, w: a.sms_week, l: 'SMS sent', c: 'var(--accent2)', ic: '💬', ch: 'sms' },
    { n: a.emails_today, w: a.emails_week, l: 'Emails', c: 'var(--accent3)', ic: '📧', ch: 'email' },
    { n: a.leads_today, w: a.leads_week, l: 'New leads', c: 'var(--warn)', ic: '🔍', ch: null },
  ];
  $('agents').innerHTML = cards.map((k) => `
    <div class="stat">
      <div class="n" style="color:${k.c}">${esc(k.n)}</div>
      <div class="l">${esc(k.l)}</div>
      <div class="s">${esc(k.w)} this week</div>
    </div>`).join('');

  const dry = ['sms', 'voice', 'email'].filter((k) => !d.capabilities?.[k]?.live);
  const extras = [];
  if (typeof d.mail_queued === 'number') {
    extras.push(`${d.mail_queued} letter${d.mail_queued === 1 ? '' : 's'} queued`);
  }
  if (typeof d.sms_eligible === 'number') {
    extras.push(`${d.sms_eligible} lead${d.sms_eligible === 1 ? '' : 's'} textable`);
  }
  $('agents-hint').innerHTML =
    (dry.length
      ? `⚠︎ ${esc(dry.join(', '))} ${dry.length === 1 ? 'is' : 'are'} in dry-run — activity is logged but nothing is sent. `
      : 'All channels live. ') +
    (extras.length ? ' · ' + esc(extras.join(' · ')) : '');

  /* alerts for the honest-dashboard problems */
  const p = d.pipeline;
  const warn = [];
  if (p.leads === 0) warn.push('No leads yet. Import a CSV or run the pipeline to pull from your data provider.');
  else if (p.workable === 0) warn.push('You have leads but none are workable — they need an ARV and a phone number.');
  if (p.rejected > 0) warn.push(`${p.rejected} record${p.rejected === 1 ? '' : 's'} were rejected by the validation filter (see Deals → Rejected).`);
  alertBox($('dash-alert'), p.leads === 0 ? 'warn' : 'info', warn.join(' '));

  /* funnel */
  const stages = [
    ['Leads found', p.leads, 'var(--accent2)'],
    ['Skip traced', p.skip_traced, 'var(--accent)'],
    ['Analysed', p.analyzed, 'var(--info)'],
    ['Contacted', p.contacted, 'var(--accent3)'],
    ['Engaged', p.engaged, 'var(--warn)'],
    ['Under contract', p.under_contract, 'var(--ok)'],
    ['Signed', p.signed, '#a78bfa'],
    ['Sent to JV', p.sent_to_jv, 'var(--accent3)'],
    ['Closed', p.closed, 'var(--ok)'],
  ];
  const max = Math.max(...stages.map((s) => s[1]), 1);
  $('funnel').innerHTML = stages.map(([label, n, color]) => {
    const w = Math.round((n / max) * 100);
    const conv = p.leads ? Math.round((n / p.leads) * 100) : 0;
    return `<div class="funnel-row">
      <span class="funnel-lbl">${esc(label)}</span>
      <div style="flex:1"><div class="funnel-bar" style="width:${w}%;background:${color}"></div></div>
      <span class="funnel-n" style="color:${color}">${esc(n)}</span>
      <span class="funnel-p">${conv}%</span>
    </div>`;
  }).join('');
  $('funnel-hint').textContent =
    `${p.workable} workable of ${p.leads} total · ${p.dead} suppressed/dead · ${p.rejected} rejected on ingest`;

  /* grades */
  const g = d.grades;
  $('grades').innerHTML = ['A', 'B', 'C', 'F'].map((k) => `
    <div class="kv"><span class="k"><span class="grade ${k}">${k}</span></span>
    <span class="v">${esc(g[k] || 0)}</span></div>`).join('');

  /* metrics */
  const m = d.metrics;
  $('metrics').innerHTML = `
    <div class="kv"><span class="k">Response rate</span><span class="v">${esc(m.response_rate)}%</span></div>
    <div class="kv"><span class="k">Outbound total</span><span class="v">${esc(m.total_outbound)}</span></div>
    <div class="kv"><span class="k">Fee potential</span><span class="v">${money(m.revenue_potential)}</span></div>
    <div class="kv"><span class="k">Collected</span><span class="v">${money(m.collected)}</span></div>
    <div class="kv"><span class="k">In sequence</span><span class="v">${esc(m.active_enrollments)}</span></div>
    <div class="kv"><span class="k">Due now</span><span class="v">${esc(m.due_now)}</span></div>`;

  /* sparkline */
  const daily = d.daily_leads || [];
  const peak = Math.max(...daily.map((x) => x.count), 1);
  $('spark').innerHTML = daily.map((x) => `
    <div class="funnel-row">
      <span class="funnel-lbl">${esc(x.day)}</span>
      <div style="flex:1"><div class="funnel-bar" style="width:${Math.round((x.count / peak) * 100)}%;background:var(--info)"></div></div>
      <span class="funnel-n">${esc(x.count)}</span>
      <span class="funnel-p">${esc(x.contacts)}</span>
    </div>`).join('') + '<div class="hint">left = new leads · right = outbound contacts</div>';

  /* top deals + feed */
  const deals = await api('/api/deals?limit=6');
  $('top-deals').innerHTML = deals.length
    ? deals.slice(0, 4).map(dealRow).join('')
    : `<div class="empty"><div class="big">◫</div>No deals in the pipeline yet.</div>`;
  bindDealRows($('top-deals'));
  await loadDashNeeds();
  await loadFeed($('dash-feed'), 6);
}

const CLASS_BADGE = {
  inbound: '<span class="badge ok">inbound</span>',
  referral: '<span class="badge info">referral</span>',
  list: '<span class="badge mute">list</span>',
  probate: '<span class="badge warn">probate</span>',
};

function dealRow(d) {
  const badge = d.opted_out ? '<span class="badge bad">Opt-ed out</span>'
    : d.contract_signed ? '<span class="badge ok">Signed</span>'
    : d.can_be_worked ? `<span class="badge info">${esc(d.status)}</span>`
    : '<span class="badge mute">Not workable</span>';
  const cls = CLASS_BADGE[d.lead_class] || esc(d.lead_class || '');
  const sms = d.sms_allowed
    ? '<span class="badge ok" title="SMS permitted">SMS ok</span>'
    : `<span class="badge mute" title="${esc(d.sms_blocked_reason || '')}">no SMS</span>`;
  const mail = d.mail_queued ? `<span class="badge warn">${esc(d.mail_queued)}✉</span>` : '';
  return `<div class="deal" data-id="${esc(d.id)}">
    <span class="grade ${esc(d.grade)}">${esc(d.grade)}</span>
    <div style="flex:1;min-width:0">
      <div class="addr">${esc(d.address || '(no address)')}</div>
      <div class="meta">
        <span>${esc(d.owner || 'no owner')}</span>
        ${cls}${sms}${mail}
        ${badge}
      </div>
    </div>
    <div class="right">
      <div class="money">${d.max_offer ? money(d.max_offer) : '—'}</div>
      <div class="sub">${d.arv ? 'ARV ' + money(d.arv) : 'ARV unknown'}</div>
    </div>
  </div>`;
}

function bindDealRows(root) {
  root.querySelectorAll('.deal').forEach((el) =>
    el.addEventListener('click', () => openDeal(el.dataset.id)));
}

async function loadFeed(target, limit) {
  if (!target) return;
  target.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div>';
  try {
    const rows = await api('/api/activity?limit=' + (limit || 40));
    if (!rows.length) {
      target.innerHTML = `<div class="empty"><div class="big">◔</div>Nothing yet. Run the pipeline or import leads.</div>`;
      return;
    }
    target.innerHTML = rows.map(feedRow).join('');
  } catch (e) {
    target.innerHTML = `<div class="msg err">${esc(e.message)}</div>`;
  }
}

function feedRow(r) {
  return `<div class="feed-item">
    <span class="ic">${esc(r.icon)}</span>
    <span class="tx"><b>${esc(r.text)}</b><small>${esc(r.detail || '')}</small></span>
    <span class="tm">${esc(timeAgo(r.time))}</span>
  </div>`;
}

/* ── Needs you ──────────────────────────────────────────── */
async function loadNeedsYou() {
  const list = $('needs-list');
  if (!list) return;
  list.innerHTML = '<div class="skeleton"></div>';
  try {
    const rows = await api('/api/needs-you');
    updateNeedsBadge(rows.length);

    if (!rows.length) {
      list.innerHTML = `<div class="empty"><div class="big">✅</div>
        Nothing needs you right now.<br>
        <span style="font-size:.74rem">The AI is working the list. You'll be
        pinged here (and by text) the moment a seller wants to talk.</span></div>`;
      return;
    }

    list.innerHTML = rows.map((r) => `
      <div class="card" style="border-color:rgba(0,229,160,.3)" data-need="${esc(r.id)}">
        <div class="card-h">
          <div style="display:flex;gap:10px;align-items:center;min-width:0">
            <span class="grade ${esc(r.grade)}" style="width:32px;height:32px">${esc(r.grade)}</span>
            <div style="min-width:0">
              <div style="font-weight:700;font-size:.95rem">${esc(r.address)}</div>
              <div style="font-size:.7rem;color:var(--muted)">
                ${esc(r.owner || 'owner')} · ${esc(r.phone || 'no phone')} ·
                ${esc(timeAgo(r.attention_at))}
              </div>
            </div>
          </div>
          ${r.contract_ready
            ? '<span class="badge ok">Contract drafted</span>'
            : '<span class="badge warn">Needs numbers</span>'}
        </div>

        <div class="msg info" style="margin-top:4px">
          <b>${esc(r.attention_reason || 'wants to talk')}</b>
          ${r.what_they_said ? `<div style="margin-top:6px;font-style:italic">
            “${esc(r.what_they_said.slice(0, 220))}”</div>` : ''}
        </div>

        <div class="grid4" style="margin-top:10px">
          <div class="stat"><div class="n" style="color:var(--accent)">${r.max_offer ? money(r.max_offer) : '—'}</div><div class="l">Max offer</div></div>
          <div class="stat"><div class="n" style="color:var(--info)">${r.arv ? money(r.arv) : '—'}</div><div class="l">ARV</div></div>
          <div class="stat"><div class="n" style="color:var(--warn)">${money(r.repair_estimate)}</div><div class="l">Repairs</div></div>
          <div class="stat"><div class="n" style="color:var(--accent3)">${money(r.assignment_fee)}</div><div class="l">Target fee</div></div>
        </div>

        <div class="hint" style="margin-top:10px">→ ${esc(r.next_action)}</div>

        <div class="row" style="margin-top:12px">
          ${r.phone ? `<a class="btn" href="tel:${esc(r.phone)}">📞 Call ${esc(r.phone)}</a>` : ''}
          <button class="btn primary" data-open="${esc(r.id)}">Open deal</button>
          <button class="btn" data-done="${esc(r.id)}">Mark handled</button>
        </div>
      </div>`).join('');

    list.querySelectorAll('[data-open]').forEach((b) =>
      b.addEventListener('click', () => openDeal(b.dataset.open)));

    list.querySelectorAll('[data-done]').forEach((b) =>
      b.addEventListener('click', async () => {
        const note = prompt('What happened? (optional note for the timeline)') || '';
        try {
          await api(`/api/deals/${b.dataset.done}/attention/resolve`, {
            method: 'POST', body: JSON.stringify({ note }),
          });
          loadNeedsYou();
        } catch (e) {
          alertBox($('needs-alert'), 'err', e.message);
        }
      }));
  } catch (e) {
    list.innerHTML = `<div class="msg err">${esc(e.message)}</div>`;
  }
}

function updateNeedsBadge(n) {
  const pill = $('needs-pill');
  if (!pill) return;
  $('needs-count').textContent = n;
  pill.style.display = n > 0 ? 'inline-flex' : 'none';

  const card = $('dash-needs-card');
  if (card) card.style.display = n > 0 ? 'block' : 'none';
}

async function loadDashNeeds() {
  try {
    const rows = await api('/api/needs-you?limit=3');
    updateNeedsBadge(rows.length);
    const box = $('dash-needs');
    if (!box || !rows.length) return;
    box.innerHTML = rows.map((r) => `
      <div class="deal" data-id="${esc(r.id)}">
        <span class="grade ${esc(r.grade)}">${esc(r.grade)}</span>
        <div style="flex:1;min-width:0">
          <div class="addr">${esc(r.address)}</div>
          <div class="meta">
            <span>${esc(r.attention_reason || '')}</span>
            ${r.contract_ready ? '<span class="badge ok">ready to sign</span>' : ''}
          </div>
        </div>
        <div class="right">
          <div class="money">${r.max_offer ? money(r.max_offer) : '—'}</div>
          <div class="sub">${esc(timeAgo(r.attention_at))}</div>
        </div>
      </div>`).join('');
    bindDealRows(box);
  } catch (e) { /* non-fatal */ }
}

$('needs-pill') && $('needs-pill').addEventListener('click', () => go('needs'));

/* ── Deals ──────────────────────────────────────────────── */
async function loadDeals() {
  const list = $('deals-list');
  list.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>';
  try {
    const q = state.grade ? '?grade=' + encodeURIComponent(state.grade) : '';
    const rows = await api('/api/deals' + q);
    state.deals = rows;
    list.innerHTML = rows.length
      ? rows.map(dealRow).join('')
      : `<div class="empty"><div class="big">▤</div>
         No deals${state.grade ? ' with grade ' + esc(state.grade) : ''}.<br>
         Import a CSV export from PropStream/BatchData, or add one manually.</div>`;
    bindDealRows(list);
  } catch (e) {
    list.innerHTML = `<div class="msg err">${esc(e.message)}</div>`;
  }
}

$('add-lead-btn').addEventListener('click', async () => {
  const street = prompt('Street address (must include a house number):');
  if (!street) return;
  const city = prompt('City:'); if (!city) return;
  const st = prompt('State (2 letters, e.g. TX):'); if (!st) return;
  const owner = prompt('Owner name (optional):') || '';
  const phone = prompt('Owner phone (optional):') || '';
  const mailing = prompt('Owner mailing address (optional — needed for letters):') || '';
  const assessed = prompt('Tax assessed value (optional, helps ARV):') || '';
  const cls = (prompt('Lead class — list / probate / referral / inbound? (default list)', 'list') || 'list').toLowerCase();
  let consentSource = '';
  if (cls === 'inbound') {
    consentSource = prompt(
      'Where did this lead consent to texts? (e.g. web_form:/sell-dallas, phone_call 2025-10-20)\n' +
      'Leave blank to keep SMS off — the lead will be worked by call and mail.',
      ''
    ) || '';
  }
  try {
    const r = await api('/api/leads', {
      method: 'POST',
      body: JSON.stringify({
        street, city, state: st, owner_name: owner || null,
        owner_phone: phone || null,
        owner_mailing_address: mailing || null,
        tax_assessed_value: assessed ? Number(assessed) : null,
        lead_class: cls,
        consent_sms: Boolean(consentSource),
        consent_source: consentSource,
        consent_at: consentSource ? new Date().toISOString() : null,
      }),
    });
    alert(
      `Added as "${r.lead_class}". Grade ${r.grade}, ARV ${moneyFull(r.arv)}, ` +
      `max offer ${moneyFull(r.max_offer)}.\n\nChannels: ${r.allowed_channels.join(', ')}` +
      (r.sms_allowed ? '\nSMS unlocked (consent recorded).'
                     : `\nNo SMS: ${r.sms_blocked_reason}`)
    );
    loadDeals();
  } catch (e) {
    alert('Rejected: ' + e.message);
  }
});

let importClass = 'list';

$('import-btn').addEventListener('click', () => {
  $('import-modal').classList.add('on');
  alertBox($('import-modal-msg'), '', '');
});

$('import-cancel').addEventListener('click', () => {
  $('import-modal').classList.remove('on');
});

document.querySelectorAll('#import-modal .radio').forEach((el) =>
  el.addEventListener('click', () => {
    document.querySelectorAll('#import-modal .radio').forEach((x) => x.classList.remove('on'));
    el.classList.add('on');
    el.querySelector('input').checked = true;
    importClass = el.dataset.class;
  }));

$('import-go').addEventListener('click', () => {
  importClass = (document.querySelector('#import-modal input[name="ic"]:checked') || {}).value || 'list';
  $('csv-input').click();
});

$('csv-input').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  const policy = $('consent-policy').value;
  alertBox($('import-modal-msg'), 'info', 'Importing and validating…');
  alertBox($('deals-alert'), 'info', 'Importing and validating…');
  try {
    const qs = `?lead_class=${encodeURIComponent(importClass)}&consent_policy=${encodeURIComponent(policy)}`;
    const res = await fetch(API + '/api/leads/import' + qs, {
      method: 'POST', body: fd, credentials: 'same-origin',
    });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || 'Import failed');
    const st = body.stats;
    let msg = `Imported ${st.accepted} of ${st.fetched} as "${body.lead_class}" · ${st.rejected} rejected · ${st.duplicates} duplicates · ${body.enrolled} enrolled. `;
    if (st.rejected_reasons && Object.keys(st.rejected_reasons).length) {
      msg += 'Rejections: ' + Object.entries(st.rejected_reasons)
        .map(([k, v]) => `${v}× ${k}`).join(', ') + '. ';
    }
    if (body.consent_flags_stripped) {
      msg += `${body.consent_flags_stripped} consent flag(s) discarded — those leads are call/mail only. `;
    }
    alertBox($('import-modal-msg'), st.accepted ? 'ok' : 'warn', msg);
    alertBox($('deals-alert'), st.accepted ? 'ok' : 'warn', msg);
    if (st.accepted) {
      setTimeout(() => $('import-modal').classList.remove('on'), 1200);
      loadDeals();
    }
  } catch (err) {
    alertBox($('import-modal-msg'), 'err', err.message);
  } finally {
    e.target.value = '';
  }
});

$('rejected-btn').addEventListener('click', async () => {
  try {
    const rows = await api('/api/leads/rejected?limit=40');
    if (!rows.length) return alertBox($('deals-alert'), 'info', 'Nothing has been rejected yet.');
    const lines = rows.map((r) =>
      `• ${r.raw.street || '(blank)'} / ${r.raw.city || '?'} — ${r.reason}`).join('\n');
    alert('Rejected records (and why):\n\n' + lines);
  } catch (e) {
    alertBox($('deals-alert'), 'err', e.message);
  }
});

function sendBtn(channel, label, tpl, P) {
  const ok = (P.allowed_channels || []).includes(channel);
  const reason = ok ? '' : `Not permitted for ${P.lead_class} leads`;
  return `<button class="btn" data-send="${channel}" data-tpl="${tpl}"
    ${ok ? '' : 'disabled'} title="${esc(reason)}">${esc(label)}</button>`;
}

/* ── Deal detail ────────────────────────────────────────── */
async function openDeal(id) {
  go('detail');
  const root = $('detail');
  root.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div>';
  try {
    const d = await api('/api/deals/' + id);
    const L = d.deal, P = d.property, O = d.owner;
    const P_ = d.channel_policy || {
      lead_class: L.lead_class, allowed_channels: L.allowed_channels,
      sms_allowed: L.sms_allowed, sms_blocked_reason: L.sms_blocked_reason,
      consent_source: '', consent_at: null,
    };
    const reasons = (d.motivation_reasons || []).map((r) => `<span class="badge info">${esc(r)}</span>`).join(' ');

    root.innerHTML = `
      <div class="card">
        <div class="card-h">
          <div style="display:flex;gap:10px;align-items:center;min-width:0">
            <span class="grade ${esc(L.grade)}" style="width:34px;height:34px;font-size:1rem">${esc(L.grade)}</span>
            <div style="min-width:0">
              <div style="font-weight:700;font-size:1rem;line-height:1.25">${esc(L.address)}</div>
              <div style="font-size:.72rem;color:var(--muted)">${esc(L.status)} · score ${esc(L.score)} · ${esc(L.source)}</div>
            </div>
          </div>
        </div>
        <div class="grid4">
          <div class="stat"><div class="n" style="color:var(--accent)">${L.max_offer ? money(L.max_offer) : '—'}</div><div class="l">Max offer</div><div class="s">70% rule</div></div>
          <div class="stat"><div class="n" style="color:var(--info)">${L.arv ? money(L.arv) : '—'}</div><div class="l">ARV</div><div class="s">${esc(L.comp_count)} comps</div></div>
          <div class="stat"><div class="n" style="color:var(--warn)">${money(L.repair_estimate)}</div><div class="l">Repairs</div><div class="s">estimate</div></div>
          <div class="stat"><div class="n" style="color:var(--accent3)">${money(L.assignment_fee)}</div><div class="l">Target fee</div><div class="s">${L.projected_spread < 0 ? 'seller under max' : L.projected_spread > 0 ? 'gap above max' : ''}</div></div>
        </div>
        ${reasons ? `<div style="margin-top:12px">${reasons}</div>` : ''}
        ${!L.can_be_worked ? `<div class="msg warn" style="margin-top:12px">Not workable: ${!L.is_contactable ? 'no phone/email on file. ' : ''}${L.arv <= 0 ? 'ARV unknown — add comps or a tax value. ' : ''}${L.opted_out ? 'Owner opted out. ' : ''}</div>` : ''}
      </div>

      <div class="card">
        <div class="card-t"><span class="pip" style="background:var(--accent2)"></span>Owner</div>
        <div style="margin-top:10px">
          <div class="kv"><span class="k">Name</span><span class="v">${esc(O.name || '—')}</span></div>
          <div class="kv"><span class="k">Phone</span><span class="v">${esc(O.phone || '—')}</span></div>
          <div class="kv"><span class="k">Phone 2</span><span class="v">${esc(O.phone_2 || '—')}</span></div>
          <div class="kv"><span class="k">Email</span><span class="v">${esc(O.email || '—')}</span></div>
          <div class="kv"><span class="k">Mailing</span><span class="v">${esc(O.mailing_address || '—')}</span></div>
        </div>
        <div class="row" style="margin-top:12px">
          ${sendBtn('call', '📞 Call', 'voice_script', P_)}
          ${sendBtn('sms', '💬 Send SMS', 'sms_opener', P_)}
          ${sendBtn('email', '📧 Email', 'email_intro', P_)}
        </div>
        <div id="send-msg"></div>
      </div>

      <div class="card">
        <div class="card-h">
          <div class="card-t"><span class="pip" style="background:var(--warn)"></span>Channel policy</div>
          ${CLASS_BADGE[P_.lead_class] || esc(P_.lead_class)}
        </div>
        <div class="kv"><span class="k">Allowed channels</span>
          <span class="v">${(P_.allowed_channels || []).map((ch) => esc(ch)).join(', ') || '—'}</span></div>
        <div class="kv"><span class="k">SMS</span>
          <span class="v">${P_.sms_allowed ? '<span class="badge ok">permitted</span>' : '<span class="badge mute">blocked</span>'}</span></div>
        ${P_.sms_allowed ? '' : `<div class="msg info" style="margin-top:10px">${esc(P_.sms_blocked_reason)}</div>`}
        <div class="kv"><span class="k">Consent source</span><span class="v">${esc(P_.consent_source || '—')}</span></div>
        <div class="kv"><span class="k">Consent recorded</span><span class="v">${P_.consent_at ? esc(timeAgo(P_.consent_at)) : '—'}</span></div>

        ${P_.lead_class === 'inbound' && !P_.sms_allowed && !L.opted_out ? `
          <div style="margin-top:12px">
            <div class="hint" style="margin-bottom:8px">
              Record consent only if this seller actually told you to text them.
              It needs a source — an unsourced "yes" is not consent under Texas SB 140.
            </div>
            <div class="field"><label>Where did they consent?</label>
              <input id="consent-source" placeholder="phone_call 2025-10-20 14:32"></div>
            <div class="field"><label>Evidence (optional)</label>
              <input id="consent-evidence" placeholder="seller asked me to text the numbers"></div>
            <button class="btn primary" id="consent-btn" style="font-size:.8rem">Record SMS consent</button>
          </div>` : ''}
        ${P_.sms_allowed ? `<div class="row" style="margin-top:10px">
          <button class="btn danger" id="revoke-consent" style="font-size:.76rem">Revoke SMS consent</button>
        </div>` : ''}
        <div id="consent-msg"></div>
      </div>

      <div class="card">
        <div class="card-t"><span class="pip" style="background:var(--info)"></span>Property</div>
        <div style="margin-top:10px">
          <div class="kv"><span class="k">Type</span><span class="v">${esc(P.type)}</span></div>
          <div class="kv"><span class="k">Beds / baths</span><span class="v">${esc(P.bedrooms ?? '—')} / ${esc(P.bathrooms ?? '—')}</span></div>
          <div class="kv"><span class="k">Sqft</span><span class="v">${esc(P.sqft ?? '—')}</span></div>
          <div class="kv"><span class="k">Year built</span><span class="v">${esc(P.year_built ?? '—')}</span></div>
          <div class="kv"><span class="k">Assessed</span><span class="v">${P.tax_assessed_value ? moneyFull(P.tax_assessed_value) : '—'}</span></div>
          <div class="kv"><span class="k">Tax delinquent</span><span class="v">${P.tax_delinquent ? 'Yes' : 'No'}</span></div>
          <div class="kv"><span class="k">Pre-foreclosure</span><span class="v">${P.pre_foreclosure ? 'Yes' : 'No'}</span></div>
          <div class="kv"><span class="k">Vacant</span><span class="v">${P.vacant ? 'Yes' : 'No'}</span></div>
          <div class="kv"><span class="k">Absentee</span><span class="v">${P.is_absentee ? 'Yes' : 'No'}</span></div>
          <div class="kv"><span class="k">Years owned</span><span class="v">${esc(P.years_owned ?? '—')}</span></div>
          <div class="kv"><span class="k">Equity</span><span class="v">${P.equity_pct ? esc(P.equity_pct) + '%' : '—'}</span></div>
        </div>
      </div>

      <div class="card">
        <div class="card-t"><span class="pip" style="background:var(--warn)"></span>Move the deal</div>
        <div class="row" style="margin-top:12px">
          <select id="status-sel">${['new','contacted','engaged','qualified','appointment','offer_made','under_contract','signed','sent_to_jv','closed','dead']
            .map((s) => `<option value="${s}" ${s === L.status ? 'selected' : ''}>${s.replace('_',' ')}</option>`).join('')}</select>
          <button class="btn primary" id="status-btn">Update status</button>
        </div>
        <div class="row" style="margin-top:10px">
          <button class="btn" id="contract-btn">Generate contract</button>
          <button class="btn" id="jv-btn" ${L.contract_signed ? '' : 'disabled'}>Send to JV</button>
        </div>
        <div id="deal-msg"></div>
        ${d.contract ? `<hr>
          <div class="kv"><span class="k">Contract</span><span class="v">${d.contract.signed ? '<span class="badge ok">Signed</span>' : '<span class="badge warn">Unsigned</span>'}</span></div>
          <div class="kv"><span class="k">Offer price</span><span class="v">${moneyFull(d.contract.offer_price)}</span></div>
          ${d.contract.signed_by ? `<div class="kv"><span class="k">Signed by</span><span class="v">${esc(d.contract.signed_by)}</span></div>` : ''}
          <div style="margin-top:10px"><textarea id="contract-body" readonly style="min-height:180px;font-size:.72rem">${esc(d.contract.body)}</textarea></div>` : ''}
      </div>

      <div class="card">
        <div class="card-t"><span class="pip" style="background:var(--accent)"></span>Contact history (${(d.contacts || []).length})</div>
        <div class="tl" style="margin-top:12px">
          ${(d.contacts || []).length ? d.contacts.slice().reverse().map((c) => `
            <div class="tl-item ${esc(c.channel)}">
              <div class="h">${esc(c.channel)} · ${esc(c.status)}${c.direction === 'inbound' ? ' · inbound' : ''}</div>
              ${c.body ? `<div class="b">${esc(c.body)}</div>` : ''}
              ${c.error ? `<div class="m">${esc(c.error)}</div>` : ''}
              <div class="m">${esc(timeAgo(c.created_at))}</div>
            </div>`).join('')
            : '<div class="empty">No contacts yet.</div>'}
        </div>
      </div>

      ${(d.comps || []).length ? `<div class="card">
        <div class="card-t"><span class="pip" style="background:var(--info)"></span>Comparable sales</div>
        <div class="scroll-x" style="margin-top:10px"><table>
          <thead><tr><th>Address</th><th>Sold</th><th>Sqft</th><th>$/sqft</th><th>Mi</th></tr></thead>
          <tbody>${d.comps.map((c) => `<tr>
            <td>${esc(c.address)}</td><td>${moneyFull(c.sold_price)}</td>
            <td>${esc(c.sqft)}</td><td>${moneyFull(c.price_per_sqft)}</td>
            <td>${esc(c.distance_miles ?? '—')}</td></tr>`).join('')}</tbody>
        </table></div></div>` : ''}
    `;

    /* wire actions */
    root.querySelectorAll('[data-send]').forEach((b) =>
      b.addEventListener('click', async () => {
        b.disabled = true;
        const ch = b.dataset.send, tpl = b.dataset.tpl;
        try {
          const r = await api(`/api/deals/${id}/contact?channel=${ch}&template_key=${tpl}`, { method: 'POST' });
          alertBox($('send-msg'), r.simulated ? 'warn' : 'ok',
            r.simulated ? 'Logged as simulated (dry run — no credentials configured).' : 'Sent successfully.');
        } catch (e) {
          alertBox($('send-msg'), 'err', e.message);
        } finally { b.disabled = false; }
      }));

    const consentBtn = $('consent-btn');
    if (consentBtn) {
      consentBtn.addEventListener('click', async () => {
        const source = ($('consent-source').value || '').trim();
        if (source.length < 3) {
          return alertBox($('consent-msg'), 'err',
            'A source is required — where did the seller consent?');
        }
        consentBtn.disabled = true;
        try {
          await api(`/api/deals/${id}/consent`, {
            method: 'POST',
            body: JSON.stringify({
              source,
              evidence: ($('consent-evidence').value || '').trim(),
              lead_class: 'inbound',
            }),
          });
          openDeal(id);
        } catch (e) {
          alertBox($('consent-msg'), 'err', e.message);
          consentBtn.disabled = false;
        }
      });
    }

    const revokeBtn = $('revoke-consent');
    if (revokeBtn) {
      revokeBtn.addEventListener('click', async () => {
        if (!confirm('Revoke SMS consent for this lead? They will still be callable.')) return;
        try {
          await api(`/api/deals/${id}/consent`, { method: 'DELETE' });
          openDeal(id);
        } catch (e) {
          alertBox($('consent-msg'), 'err', e.message);
        }
      });
    }

    $('status-btn').addEventListener('click', async () => {
      try {
        await api(`/api/deals/${id}`, {
          method: 'PATCH',
          body: JSON.stringify({ status: $('status-sel').value }),
        });
        openDeal(id);
      } catch (e) { alertBox($('deal-msg'), 'err', e.message); }
    });

    $('contract-btn').addEventListener('click', async () => {
      try {
        await api(`/api/deals/${id}/generate-contract`, { method: 'POST' });
        openDeal(id);
      } catch (e) { alertBox($('deal-msg'), 'err', e.message); }
    });

    $('jv-btn').addEventListener('click', async () => {
      try {
        await api(`/api/deals/${id}/send-to-jv`, { method: 'POST' });
        openDeal(id);
      } catch (e) { alertBox($('deal-msg'), 'err', e.message); }
    });
  } catch (e) {
    root.innerHTML = `<div class="msg err">${esc(e.message)}</div>`;
  }
}

/* ── Cadence ────────────────────────────────────────────── */
async function loadCadence() {
  try {
    const c = await api('/api/cadence');
    const e = c.enrollments || {};

    $('cadence-info').innerHTML = `
      <div class="grid4" style="margin-bottom:14px">
        <div class="stat"><div class="n" style="color:var(--accent)">${esc(e.active || 0)}</div><div class="l">Active</div></div>
        <div class="stat"><div class="n" style="color:var(--warn)">${esc(e.paused || 0)}</div><div class="l">Paused</div></div>
        <div class="stat"><div class="n" style="color:var(--info)">${esc(e.completed || 0)}</div><div class="l">Completed</div></div>
        <div class="stat"><div class="n" style="color:var(--muted)">${esc(e.stopped || 0)}</div><div class="l">Stopped</div></div>
      </div>
      ${(c.cadences || []).map((cad) => `
        <div style="margin-bottom:14px;padding-bottom:12px;border-bottom:1px solid var(--border)">
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:4px">
            <b style="font-size:.86rem">${esc(cad.name)}</b>
            ${CLASS_BADGE[cad.lead_class] || esc(cad.lead_class)}
            ${(cad.channels || []).map((ch) => `<span class="badge ${
              ch === 'sms' ? 'ok' : ch === 'call' ? 'info' : ch === 'mail' ? 'warn' : 'mute'
            }">${esc(ch)}</span>`).join('')}
          </div>
          <div style="font-size:.74rem;color:var(--muted);line-height:1.5">${esc(cad.description)}</div>
          <div style="margin-top:8px">
            ${cad.steps.map((st, i) => `
              <div class="funnel-row">
                <span class="funnel-lbl">${st.day == 0 ? 'Day 0' : 'Day ' + st.day}</span>
                <span class="badge ${st.channel === 'call' ? 'info' : st.channel === 'sms' ? 'ok' : st.channel === 'mail' ? 'warn' : 'mute'}">${esc(st.channel)}</span>
                <span style="flex:1;font-size:.7rem;color:var(--muted)">${esc(st.template_key || '')}</span>
                <span class="funnel-p">#${i + 1}</span>
              </div>`).join('')}
          </div>
        </div>`).join('')}`;

    /* policy matrix */
    const pol = c.policy || {};
    const classes = ['inbound', 'referral', 'list', 'probate'];
    const channels = ['call', 'sms', 'email', 'mail'];
    $('policy-matrix').innerHTML = `
      <div class="scroll-x"><table>
        <thead><tr><th>Lead class</th>${channels.map((ch) => `<th>${ch}</th>`).join('')}</tr></thead>
        <tbody>${classes.map((cls) => {
          const allowed = pol[cls] || [];
          return `<tr><td>${CLASS_BADGE[cls] || esc(cls)}</td>${channels.map((ch) => {
            const ok = allowed.includes(ch);
            return `<td>${ok ? '<span style="color:var(--ok)">✓</span>' : '<span style="color:#4b5563">—</span>'}</td>`;
          }).join('')}</tr>`;
        }).join('')}</tbody>
      </table></div>
      <div class="hint">SMS column is the compliance boundary: only inbound, and only
        with a recorded consent source and timestamp.</div>`;

    await loadMailQueue();

    /* templates */
    const tpls = await api('/api/templates');
    const groups = { sms: [], call: [], email: [], mail: [] };
    tpls.forEach((t) => (groups[t.channel] || (groups[t.channel] = [])).push(t));
    const label = { sms: 'SMS (inbound only)', call: 'Voice', email: 'Email', mail: 'Physical letters' };

    $('templates').innerHTML = Object.keys(groups).map((ch) => `
      <h2 class="sec" style="margin-top:4px">${esc(label[ch] || ch)}</h2>
      ${groups[ch].map((t) => `
        <div class="card" data-tpl="${esc(t.key)}">
          <div class="card-h">
            <div class="card-t">${esc(t.label || t.key)}</div>
            <span class="badge mute">${esc(t.channel)}</span>
          </div>
          ${t.subject ? `<div class="field"><label>Subject</label><input data-f="subject" value="${esc(t.subject)}"></div>` : ''}
          <textarea data-f="body" style="min-height:${t.channel === 'email' || t.channel === 'mail' ? 220 : 90}px">${esc(t.body)}</textarea>
          <div class="row" style="margin-top:8px">
            <button class="btn primary" data-save="${esc(t.key)}" style="font-size:.78rem">Save</button>
          </div>
          <div data-tplmsg="${esc(t.key)}"></div>
        </div>`).join('')}`).join('');

    $('templates').querySelectorAll('[data-save]').forEach((b) =>
      b.addEventListener('click', async () => {
        const card = b.closest('.card');
        const payload = { body: card.querySelector('[data-f="body"]').value };
        const sub = card.querySelector('[data-f="subject"]');
        if (sub) payload.subject = sub.value;
        try {
          await api('/api/templates/' + encodeURIComponent(b.dataset.save), {
            method: 'PATCH', body: JSON.stringify(payload),
          });
          const box = card.querySelector('[data-tplmsg]');
          alertBox(box, 'ok', 'Saved.');
          setTimeout(() => alertBox(box, '', ''), 1800);
        } catch (err) {
          alertBox(card.querySelector('[data-tplmsg]'), 'err', err.message);
        }
      }));
  } catch (err) {
    alertBox($('cadence-alert'), 'err', err.message);
  }
}

async function loadMailQueue() {
  const box = $('mail-queue');
  if (!box) return;
  try {
    const rows = await api('/api/mail-queue?limit=40');
    if (!rows.length) {
      box.innerHTML = `<div class="empty">No letters queued.<br>
        <span style="font-size:.72rem">List and probate leads queue a letter automatically.</span></div>`;
      return;
    }
    box.innerHTML = rows.map((r) => `
      <div class="deal" style="cursor:default" data-letter="${esc(r.id)}">
        <div style="flex:1;min-width:0">
          <div class="addr">${esc(r.to_name || 'Current Resident')}</div>
          <div class="meta">
            <span>${esc(r.address)}</span>
            ${CLASS_BADGE[r.lead_class] || ''}
            <span>${esc(timeAgo(r.created_at))}</span>
          </div>
        </div>
        <div class="right">
          <button class="btn ghost" style="padding:5px 9px;font-size:.68rem"
            data-preview="${esc(r.id)}">View</button>
        </div>
      </div>`).join('');
    box.querySelectorAll('[data-preview]').forEach((b) =>
      b.addEventListener('click', async (ev) => {
        ev.stopPropagation();
        const row = rows.find((x) => String(x.id) === String(b.dataset.preview));
        const win = window.open('', '_blank');
        win.document.write('<pre style="white-space:pre-wrap;font:13px/1.5 ui-monospace,monospace;padding:24px;max-width:640px">'
          + row.body.replace(/[&<>]/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[ch])) + '</pre>');
        win.document.close();
      }));
  } catch (err) {
    box.innerHTML = `<div class="msg err">${esc(err.message)}</div>`;
  }
}

/* ── Activity ───────────────────────────────────────────── */
async function loadActivity() { await loadFeed($('activity-list'), 60); }

/* ── Settings ───────────────────────────────────────────── */
async function loadSettings() {
  try {
    const s = await api('/api/settings');
    const caps = s.capabilities || {};
    $('caps').innerHTML = Object.entries(caps).map(([k, v]) => `
      <div class="kv"><span class="k">${esc(k)} <span class="badge mute">${esc(v.provider)}</span></span>
      <span class="v">${v.live ? '<span class="badge ok">live</span>' : '<span class="badge warn">dry-run</span>'}</span></div>`).join('');

    const i = s.identity;
    $('identity').innerHTML = `
      <div class="kv"><span class="k">Agent name</span><span class="v">${esc(i.agent_name)}</span></div>
      <div class="kv"><span class="k">Company</span><span class="v">${esc(i.agent_company)}</span></div>
      <div class="kv"><span class="k">Phone</span><span class="v">${esc(i.agent_phone)}</span></div>
      <div class="kv"><span class="k">Email</span><span class="v">${esc(i.agent_email)}</span></div>
      <div class="kv"><span class="k">Calendar link</span><span class="v">${esc(i.calendar_link || '—')}</span></div>`;

    const c = s.compliance;
    $('compliance').innerHTML = `
      <div class="kv"><span class="k">Quiet hours</span><span class="v">${c.enforce_quiet_hours ? '<span class="badge ok">enforced</span>' : '<span class="badge bad">off</span>'}</span></div>
      <div class="kv"><span class="k">Max SMS / lead / day</span><span class="v">${esc(c.max_sms_per_lead_per_day)}</span></div>
      <div class="kv"><span class="k">Max call attempts / week</span><span class="v">${esc(c.max_call_attempts_per_lead_per_week)}</span></div>
      <div class="kv"><span class="k">Lead provider</span><span class="v">${esc(s.lead_provider)}</span></div>
      <div class="kv"><span class="k">DNC provider</span><span class="v">${esc(s.dnc_provider)}</span></div>`;

    loadSuppressions();
  } catch (e) {
    alertBox($('cadence-alert'), 'err', e.message);
  }
}

async function loadSuppressions() {
  try {
    const rows = await api('/api/compliance/suppressions?limit=50');
    $('suppressions').innerHTML = rows.length
      ? `<div class="scroll-x"><table><thead><tr><th>Phone</th><th>Email</th><th>Reason</th><th>When</th></tr></thead>
         <tbody>${rows.map((r) => `<tr><td class="mono">${esc(r.phone || '—')}</td>
         <td>${esc(r.email || '—')}</td><td>${esc(r.reason)}</td>
         <td>${esc(timeAgo(r.created_at))}</td></tr>`).join('')}</tbody></table></div>`
      : '<div class="empty">No suppressed numbers.</div>';
  } catch (e) { /* ignore */ }
}

$('pw-btn').addEventListener('click', async () => {
  try {
    await api('/api/me/password', {
      method: 'POST',
      body: JSON.stringify({ current_password: $('pw-cur').value, new_password: $('pw-new').value }),
    });
    $('pw-cur').value = ''; $('pw-new').value = '';
    alertBox($('pw-msg'), 'ok', 'Password changed.');
  } catch (e) {
    alertBox($('pw-msg'), 'err', e.message);
  }
});

/* ── Run pipeline ───────────────────────────────────────── */
$('run-btn').addEventListener('click', async () => {
  const b = $('run-btn');
  b.disabled = true; b.textContent = '⏳ Running…';
  alertBox($('dash-alert'), 'info', 'Running pipeline: ingest → validate → analyse → enrol → contact…');
  try {
    const r = await api('/api/pipeline/run', {
      method: 'POST', body: JSON.stringify({ city: 'Dallas', state: 'TX', limit: 100 }),
    });
    if (r.status === 'already_running') {
      alertBox($('dash-alert'), 'warn', 'A run is already in progress.');
    } else if (r.status === 'failed') {
      alertBox($('dash-alert'), 'err', r.error || 'Run failed');
    } else {
      const s = r.stats || {};
      const ing = s.ingest || {};
      alertBox($('dash-alert'), 'ok',
        `Accepted ${ing.accepted || 0} of ${ing.fetched || 0} · rejected ${ing.rejected || 0} · duplicates ${ing.duplicates || 0} · analysed ${s.analyzed || 0} · enrolled ${s.enrolled || 0} · touches sent ${(s.outreach || {}).sent || 0}`);
    }
    await loadDash();
  } catch (e) {
    alertBox($('dash-alert'), 'err', e.message);
  } finally {
    b.disabled = false; b.textContent = '▶ Run now';
  }
});

$('mail-export') && $('mail-export').addEventListener('click', () => {
  window.location.href = '/api/mail-queue/export';
});

/* ── Boot ───────────────────────────────────────────────── */
async function boot() {
  try {
    const me = await api('/api/me');
    showApp();
    await loadDash();
  } catch (e) {
    showLogin();
  }
}

boot();
setInterval(() => {
  if ($('main').style.display !== 'none' && state.page === 'dash') {
    loadDash().catch(() => {});
  }
}, 60000);
