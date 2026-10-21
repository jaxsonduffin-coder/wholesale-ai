# Wholesale AI v2

A working wholesale real-estate acquisition system: real lead ingest, hard
validation, comps-based underwriting, per-class outreach cadences (voice / SMS / email /
physical mail), and TCPA-grade compliance controls — including a hard rule that
cold list and probate leads are never texted (Texas SB 140).

This replaces v1, which served scraped county-website navigation menus as
"leads" and had no authentication on any endpoint.

---

## What was broken in v1

Read this first — it is why the rebuild exists, and every item is covered by a
regression test in `tests/`.

| # | Problem | Evidence (v1 production API) | Fixed by |
|---|---|---|---|
| 1 | **Lead source produced non-properties.** The Dallas CAD scraper ingested page chrome. | `"Navigation Links"` (owner `"Annual Report"`), `"About Dcad"`, `"Nsewnenwsesw"`, `"Houston Houston"` | `app/validation.py` — a situs address must contain a house number |
| 2 | **Nothing was ever sent.** | `calls_today: 0, sms_today: 0, emails_today: 0, total_outbound: 0` | `app/cadence.py` + background scheduler in `app/main.py` |
| 3 | **Fake deal progress.** | 3 "under_contract", 1 `contract_signed: true` on a lead with `owner: null`, `arv: 0` | Evidence guards in `app/api.py`; `grade_lead()` cannot return above F without an ARV |
| 4 | **Underwriting was hardcoded.** | `repair_estimate: 18000`, `assignment_fee: 5000` on every lead, `arv: 0` always | `app/scoring.py` — comps → $/sqft → ARV → 70% rule |
| 5 | **Every endpoint was public.** | `GET /api/dashboard`, `POST /api/pipeline/run`, `POST /api/outbound-call` all returned 200 with no auth | `app/security.py`; JWT + httpOnly cookie on all `/api/*` except login and webhooks |
| 6 | **No TCPA controls.** | No DNC check, no quiet hours, no opt-out handling, no consent log | `app/compliance.py` |

---

## Only you get interrupted, and only when it matters

The system runs the list. You get pulled in for one reason: a seller said
something that means *talk to me*.

`app/intent.py` reads every inbound reply and decides how hot it is:

| They said | Reading | What happens |
|---|---|---|
| "how much would you pay?" | **hot** — asking the price is the strongest signal in the business | needs-you queue, cadence paused, contract drafted |
| "yes I'm interested" / "need to sell asap" | **hot** | same |
| "can you call me tomorrow" | **hot, appointment** | same, flagged as a meeting request |
| "who is this?" | question | lead marked engaged, **you are not bothered** |
| "ok" / "yes" | weak | engaged, not escalated — it may just be answering "is this the right number?" |
| "not interested" / "already sold" | negative | lead closed, cadence stopped |
| STOP | opt-out | suppressed permanently |

Negatives always beat positives, so "not interested in your offer" is never hot
even though it contains the word *offer*.

When a lead goes hot, four things happen automatically:

1. **It lands in `Needs you`** (`GET /api/needs-you`) with what they said, the
   numbers, and the next action spelled out.
2. **The cadence pauses** so no automated message goes out while you're working it.
3. **The purchase agreement is drafted** at your max offer — so the only thing
   left is to call and sign. Never drafted against an unknown ARV.
4. **You get a text and an email** (`OWNER_ALERT_SMS` / `OWNER_ALERT_EMAIL`),
   one per burst of replies, not one per message.

The in-app queue is the source of truth; the SMS is just what makes you look.

## Your website: the front door

`/sell` is a public seller landing page — no auth, mobile-first, conversion
focused. Point your domain at it, link to it, or iframe it.

It posts to `POST /api/public/lead` (no auth, rate-limited, honeypot-protected).
Submissions become **inbound** leads and are the only leads that can legally be
texted, because the consent checkbox is the consumer giving permission directly
to you:

- Checkbox is **unchecked by default** and the copy says it is optional.
- Ticking it records `consent_source = web_form:/sell`, a timestamp, and the
  exact wording shown — that is your Texas SB 140 evidence.
- Not ticking it still submits. The lead is callable and emailable, just not textable.
- The response returns only `{ok, id, message}` — no grades or offers leak.

Free-text situations are parsed for distress signals ("behind on taxes" →
tax delinquent, "inherited" → probate, "vacant", "foreclosure", "tenant"), so
motivation scoring works from the first sentence.

## Channel policy — the Texas SB 140 guardrail

Texas SB 140 (effective 1 Sep 2025) folds text messages into "telephone
solicitation" and makes violations automatic Texas DTPA violations: private
right of action, treble damages, attorney's fees. Florida's FTSA adds
$500–$1,500 per text with class-action exposure, and Oklahoma and Maryland
require prior express written consent before an automated system may select *or*
dial.

Cold texting a purchased list is the single highest-risk action available to a
wholesaler. Cold calling and physical mail are materially safer. So this is
enforced structurally rather than left to discipline:

| Lead class | Call | SMS | Email | Physical mail |
|---|---|---|---|---|
| `inbound` (they contacted us) | yes | **only with verified consent** | yes | yes |
| `referral` | yes | no | yes | yes |
| `list` (purchased / pulled) | yes | **no** | no | yes |
| `probate` (inherited) | yes | **no** | no | yes |

Three independent layers enforce it, so a future edit cannot quietly
reintroduce cold texting:

1. **`app/policy.py`** — one allow-list per class, one `sms_eligible()` check.
   Nothing else in the codebase hardcodes a channel decision.
2. **The cadence itself** — there is no SMS step in the list or probate
   sequences. `Cadence.lead_class` selects the sequence at enrollment, and an
   unknown class always falls back to the untextable one.
3. **The sender** — `execute_step()` re-checks the policy before every send and
   returns `skipped_permanent` for a disallowed channel, recording a
   `blocked_policy` contact row. The step is consumed and the sequence continues;
   it never stalls.

### What counts as consent

A bare `consent_sms = True` is worthless in front of a plaintiff's lawyer. SMS
is only enabled when **all three** are present:

- `consent_sms` is true,
- `consent_source` says where it came from (`web_form:/sell-dallas#sms`,
  `phone_call 2025-10-20 14:32`, `sms_reply:YES`),
- `consent_at` records when.

Missing any one of them and `sms_eligible()` returns false with the specific
reason shown in the UI. Consent can be recorded from the dashboard (deal detail
→ *Channel policy* → *Record SMS consent*), which requires a source and writes to
the `consent_log`. It cannot be recorded for an opted-out lead.

### Consent on import

`POST /api/leads/import` takes two parameters that decide this:

```
lead_class      list | probate | referral | inbound       (default: list)
consent_policy  none | column                             (default: none)
```

**The default grants no SMS consent at all.** A purchased list cannot carry
consent *for you* — consent has to come from the consumer to you — so any
consent flags in the file are discarded and counted in the response as
`consent_flags_stripped`.

With `consent_policy=column`, the file's own consent columns are honoured, but
still only for rows classed `inbound`, and still only with a source and
timestamp. Same file, same "yes", different class → stripped:

```
Address,City,State,SMS Consent,Consent Source,Consent Date,Lead Class
5500 Elm St,Dallas,TX,yes,web_form:/sell-dallas,2025-10-01,inbound   -> SMS ok
5510 Elm St,Dallas,TX,yes,vendor_dump_v3,2025-10-01,list            -> stripped
```

### Physical mail

There is no mail API, so letters are **queued, never "sent"** — a letter row is
written with `status = queued_for_mail` and the fully rendered body, and nothing
is ever marked delivered that was not delivered.

- `GET /api/mail-queue` — pending letters
- `GET /api/mail-queue/export` — CSV for a print/mail house
- `POST /api/mail-queue/{id}/dispatched` — mark one as actually mailed

Letters use the owner's mailing address when known, otherwise the property
address. Probate copy leads with condolences and an explicit "I will not call you
unexpectedly"; the probate sequence therefore opens with the letter and only
dials four days later.

## Working more than one state

```
MARKET_STATES=TX,FL,GA
MARKET_CITIES=FL:Tampa,Orlando;TX:Dallas,Houston
```

Every state you add brings its own calling window and timezone automatically
(TX 9am–9pm Central, FL 8am–8pm Eastern, and so on — `app/compliance.py`). The
pipeline sweeps every configured market each cycle, splitting the per-cycle limit
across them, and `run.markets` reports per-market ingest counts.

Because SMS is restricted to inbound leads in **every** state, adding a market
does not add texting risk. That is the whole reason the channel policy is
class-based rather than state-based: the state mini-TCPAs (FL FTSA, OK/MD/CT
written consent, WA CEMA, TX SB140) mostly regulate *solicitation texts*, and
this system does not send those to anyone.

Before adding a state, check two non-code things: whether it requires a
telemarketing registration or bond, and whether it has a state DNC list you must
scrub (set `DNC_PROVIDER=live`).

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then edit
python3 -m uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000 and sign in with `ADMIN_EMAIL` / `ADMIN_PASSWORD`.

Everything starts in **dry-run**: no Telnyx key and no email provider means every
send is written to the database as `simulated` instead of being transmitted.
You can walk the entire pipeline — ingest, validate, underwrite, enrol, "send" —
before spending a cent. The dashboard header shows `DRY RUN` vs `LIVE`.

---

## Verify it works

```bash
python3 -m pytest tests/ -q          # 143 unit + API tests
python3 scripts/smoke.py             # 49 checks against a real HTTP server
```

The smoke test boots uvicorn, walks the full lifecycle over real HTTP, and
exits non-zero on failure:

```
[auth]      health public, every /api/* 401, wrong password rejected, httponly cookie
[hardening] /docs and /openapi.json closed in production, security headers present
[pipeline]  ingest -> analyse -> enrol -> contact
[validation] 5 garbage rows rejected, 1 real row accepted
[outreach]  personalised copy, no leaked placeholders, daily cap enforced
[guards]    cannot market an unsigned deal; cannot sign without a contract
[opt-out]   STOP -> suppressed, cadence halted, further sends refused
```

---

## How the pipeline works

```
provider.fetch()          demo | csv | batchdata
        |
        v
validation.validate_lead()   <-- THE GATE. Rejects anything that is not a real
        |                        situs address. Every rejection is logged to
        |                        rejected_leads with its reason.
        v
 dedupe on address_key        canonicalised street + city + state
        |
        v
 scoring.analyze()           comps -> ARV -> repairs -> max_offer -> grade
        |
        v
 cadence.enroll()            grade A/B only; picks the sequence for the
        |                    lead's CLASS (list/probate have no SMS step)
        |
        v
 cadence.process_due()       every 5 min: render -> compliance gate -> send
                             -> record Contact -> advance step
```

### The validation gate

The single rule that catches everything v1 ingested:

> A real property situs address always contains a numeric house number.

`"Navigation Links"`, `"About Dcad"`, `"Annual Report"`, `"Nsewnenwsesw"`,
`"Commercial"`, `"Houston Houston"` — none contain a house number. All rejected.
Defence in depth also catches page chrome that *does* contain a number
(`"404 Page Not Found"`, `"Privacy Policy 2023"`), gibberish keystrokes, and
boilerplate county-site vocabulary (`cad`, `appraisal`, `parcel`, `propertysearch`).

Run it yourself:

```bash
python3 -c "
from app.validation import validate_lead, ValidationError
for s in ['Navigation Links','About Dcad','Nsewnenwsesw','4501 W Mockingbird Ln']:
    try: validate_lead({'street':s,'city':'Dallas','state':'TX'}); print('ACCEPT', s)
    except ValidationError as e: print('reject', s, '->', e)
"
```

### Grading

| Grade | Meaning |
|---|---|
| **A** | 3+ real comps behind the ARV, strong motivation, contactable |
| **B** | Usable numbers, some motivation, contactable — worth your day |
| **C** | Contactable and priced, thin on evidence or motive |
| **F** | Cannot be worked: no ARV, no contact path, or suppressed |

`grade_lead()` returns `F` and score `0` whenever ARV is 0 or there is no phone
or email. That is the invariant which makes it impossible for the dashboard to
report a deal it cannot support.

### Underwriting

```
ARV        = weighted median comp $/sqft x subject sqft   (3+ comps, else
             tax-assessed / 0.85 with a "provisional" warning, else 0)
repairs    = light $18/sf  ·  medium $38/sf  ·  heavy $65/sf
             (pre-1960 "light" auto-upgrades to medium)
max offer  = ARV x 0.70 - repairs - assignment fee
```

Motivation scoring (0–100) adds for pre-foreclosure, tax delinquency, probate,
vacancy, code violations, tired landlord, absentee, out-of-state, long ownership
and high equity, and returns the reasons so you can see *why*.

### The cadences

One sequence per lead class (`GET /api/cadence` returns all of them, plus the
policy matrix). Copy is editable from the dashboard with no redeploy.

| Class | Shape | Channels |
|---|---|---|
| **Inbound** — speed to lead | Call at 0, text 3 min later, 9 touches over 30 days | call, sms, email |
| **List** — call + mail | Call and letter on day 0, 7 touches over 30 days | call, mail |
| **Probate** — letter first | Letter day 0, first call day 4, 6 touches over 40 days | call, mail |
| **Referral** | Call, email, letter, 6 touches over 30 days | call, email, mail |

The inbound opening move is a call followed by an SMS three minutes later,
because a missed call plus an immediate text converts far better than either
alone. The probate sequence is the inverse: a stranger calling a grieving family
is the fastest way to lose the lead, so the letter goes first and gives them a
way to opt out before anyone dials.

Every step passes `check_send_allowed()` first: channel policy, opt-out, DNC,
suppression list, quiet hours, daily SMS cap, weekly call cap. A lead leaves the
sequence the moment it replies, opts out, or is advanced manually.

---

## Compliance

Non-negotiable, and enforced in code rather than policy.

* **Quiet hours** in the recipient's local timezone. Federal floor 8am–9pm;
  stricter state windows applied automatically (TX 9am–9pm, FL/NY/OK/WA 8am–8pm,
  and ~20 more — see `STATE_WINDOWS` in `app/compliance.py`).
* **Suppression list** checked before every send, including manual ones.
* **Opt-out keywords** (`STOP`, `UNSUBSCRIBE`, `CANCEL`, `END`, `QUIT`, `REMOVE ME`,
  ~20 variants) permanently suppress the number and halt the cadence instantly.
  Short-message-aware: `"stop by tomorrow at 3"` is *not* treated as an opt-out.
* **Frequency caps**: 1 SMS/lead/day and 3 call attempts/lead/week by default.
* **Consent log** (`consent_log`) records every grant, revocation and opt-out
  with source and evidence.
* **Webhooks** are signature-verified (Telnyx ed25519) when
  `TELNYX_PUBLIC_KEY` is set.

> Not legal advice. Automated outreach to property owners carries real exposure
> under the TCPA (and Texas/Florida/Oklahoma mini-TCPAs). The FCC's
> one-to-one consent rule was vacated by the Eleventh Circuit in January 2025,
> but prior express written consent is still required for autodialled or
> prerecorded marketing to cell phones. Get a lawyer to review your scripts
> before you scale volume.

---

## API

All routes require a session cookie or `Authorization: Bearer <jwt>` except
`/health`, `/api/login`, and the two Telnyx webhooks.

```
POST   /api/login                     POST   /api/logout
GET    /api/me                        POST   /api/me/password
GET    /api/dashboard                 GET    /api/activity
GET    /api/deals                     GET    /api/deals/{id}
POST   /api/leads                     POST   /api/leads/import   (CSV upload)
GET    /api/leads/rejected            PATCH  /api/deals/{id}
POST   /api/deals/{id}/approve        POST   /api/deals/{id}/contact
POST   /api/deals/{id}/generate-contract
POST   /api/deals/{id}/sign           POST   /api/deals/{id}/send-to-jv
POST   /api/pipeline/run              GET    /api/pipeline/status
POST   /api/pipeline/send-due         POST   /api/outbound-call
GET    /api/cadence                   GET    /api/templates
POST   /api/deals/{id}/consent        DELETE /api/deals/{id}/consent
POST   /api/test-sms                  GET    /api/needs-you
GET    /api/alerts
POST   /api/alerts/{id}/read          POST   /api/deals/{id}/attention/resolve
GET    /api/mail-queue                GET    /api/mail-queue/export
POST   /api/mail-queue/{id}/dispatched
PATCH  /api/templates/{key}           GET    /api/settings
GET    /api/compliance/suppressions   POST   /api/compliance/suppressions
GET    /api/compliance/consent-log
POST   /api/telnyx/voice-webhook      POST   /api/telnyx/message-webhook
POST   /api/public/lead               GET    /sell        (public, no auth)
```

v1's paths are unchanged, so existing clients keep working — they just need to
authenticate now.

---

## Lead sources

Set `LEAD_PROVIDER`:

* **`demo`** (default) — generates realistic valid records so you can test with
  no paid account.
* **`csv`** — upload a PropStream / BatchData / DealMachine / ListSource export
  from the Deals tab. Column names are auto-mapped (~20 aliases per field).
* **`batchdata`** — live API. Set `BATCHDATA_API_KEY`. Filters for absentee
  owners and includes skip trace (`providers/leads.py :: skip_trace`).

Realistic monthly math once a live feed is wired:

```
1,000 contacts -> ~400 conversations -> ~15 appointments
               -> ~4-5 contracts -> ~2-3 closed assignments
               -> $20k-$35k at an $8k-$12k average fee
```

Volume is the point. 7 fake leads can never produce a deal; the cadence engine
is what makes 1,000 contacts survivable.

---

## Deploy to Railway

A `Dockerfile` is included, so the build is deterministic rather than
Nixpacks-guessing. `scripts/preflight.py` refuses to let you go live in an
unsafe configuration.

```bash
# before you point real traffic at it
python3 scripts/preflight.py
```

1. Push this repo to GitHub, create a Railway project from it.
2. Add a **Postgres** plugin — Railway sets `DATABASE_URL` automatically
   (`postgres://` is normalised to `postgresql://` on boot).
3. Set the variables from `.env.example`. Minimum for a real deployment:

```
SECRET_KEY            openssl rand -hex 32
ADMIN_EMAIL           you@yourdomain.com
ADMIN_PASSWORD        (strong, 10+ chars)
AGENT_NAME / AGENT_COMPANY / AGENT_PHONE / AGENT_EMAIL
PUBLIC_BASE_URL       https://your-app.up.railway.app
```

4. Add channel credentials when you are ready to go live:

```
TELNYX_API_KEY  TELNYX_FROM_NUMBER  TELNYX_CONNECTION_ID
TELNYX_MESSAGING_PROFILE_ID  TELNYX_PUBLIC_KEY
EMAIL_PROVIDER=resend  RESEND_API_KEY  EMAIL_FROM
```

5. Point Telnyx webhooks at:

```
https://YOUR-APP/api/telnyx/voice-webhook
https://YOUR-APP/api/telnyx/message-webhook
```

The background scheduler runs inside the web process every 5 minutes
(`SCHEDULER_INTERVAL_SECONDS`). If you scale to zero or run multiple replicas,
hit `POST /api/pipeline/send-due` from a Railway cron instead — it is safe to
call repeatedly (idempotent per step, and compliance-gated).

### First-run checklist

Run the preflight command; it checks all of these and exits non-zero on anything
blocking:


- [ ] `ADMIN_PASSWORD` is not the default (the server logs a warning)
- [ ] `SECRET_KEY` is a real random value, not `CHANGE_ME`
- [ ] `TELNYX_PUBLIC_KEY` set, so webhooks are verified
- [ ] `/docs` returns 404 (`APP_ENV=production`)
- [ ] Dashboard header reads `LIVE`, not `DRY RUN`
- [ ] **10DLC brand + campaign registered in Telnyx** (nothing sends without it)
- [ ] `POST /api/test-sms` to your own phone and confirm it arrives (proves 10DLC)
- [ ] Reply STOP and confirm the lead goes to `dead` and the suppression list

---

## Verified

```
266 tests passing
101 end-to-end HTTP checks passing
```

The suite specifically covers the channel boundary: list and probate cadences
contain no SMS step, a rogue SMS step injected into a list sequence is refused
and the sequence continues, a consented inbound lead does get texted, an inbound
lead without a source does not, and importing a list with consent columns
results in no textable leads.

```
python3 -m pytest tests/ -q
python3 scripts/smoke.py
```

## Layout

```
app/
  config.py         env-backed settings + capability detection
  db.py             engine, session, init
  models.py         15 tables; Contract and RejectedLead are new in v2
  validation.py     THE GATE — situs-address validation, phone/email/owner cleaning
  scoring.py        ARV, repairs, 70% rule, motivation, grading invariants
  intent.py         reads inbound replies -> hot / question / negative
  notify.py         "a seller wants to talk" alerts to YOU, not the lead
  policy.py         CHANNEL POLICY — which class may receive which channel.
                    Single source of truth for SMS eligibility.
  compliance.py     quiet hours, suppression, opt-out, frequency caps, consent log
  migrations.py     additive column migrations for existing databases
  cadence.py        sequence execution engine
  pipeline.py       ingest -> validate -> analyse -> enrol
  security.py       JWT auth, PBKDF2 hashing, lockout, audit log
  api.py            all HTTP routes
  main.py           startup, scheduler, security headers, static UI
  providers/leads.py  demo | csv | batchdata
  channels/telnyx.py  SMS + voice with signature verification
  channels/email.py   Resend | SMTP
  templates_content.py  cadence + outreach copy
web/                mobile-first dashboard (vanilla JS, no build step): policy
                    matrix, consent capture, and the mail queue with CSV export
tests/              266 tests
scripts/smoke.py   101 end-to-end HTTP checks
scripts/preflight.py  go-live safety check
```
