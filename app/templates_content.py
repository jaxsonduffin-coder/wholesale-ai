"""Default outreach copy and cadence.

The copy here follows what actually gets replies in wholesaling, which is
almost never "I buy houses for cash". The rules baked into these templates:

  1. Say who you are in the first line. Unknown numbers get deleted.
  2. Name the property. Proves it is not a blast.
  3. Ask ONE question that is easy to answer. "Are you open to an offer" beats
     "would you like to sell".
  4. Never quote a price in message one. Price questions are the appointment.
  5. Say "not a realtor" — it removes the single biggest objection up front.
  6. Give an easy exit ("just reply STOP"). This raises reply rate AND
     satisfies opt-out handling.
  7. Keep SMS under 320 characters so it never splits into 2+ segments.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Cadence, CadenceStep, MessageTemplate

TEMPLATES: list[dict] = [
    # ── SMS ────────────────────────────────────────────────────
    {
        "key": "sms_opener",
        "channel": "sms",
        "label": "SMS — Opener (day 0)",
        "body": (
            "Hi {first_name}, this is {agent_name} — I'm not a realtor. "
            "I'm looking at the house on {street} in {city}. "
            "Would you be open to an offer if the number made sense? "
            "Reply STOP to opt out."
        ),
    },
    {
        "key": "sms_followup_day1",
        "channel": "sms",
        "label": "SMS — Follow-up (day 1)",
        "body": (
            "Hi {first_name}, following up on {street}. I can close "
            "quickly, pay your closing costs, and you don't pay commissions. "
            "Open to a quick conversation? Reply STOP to opt out."
        ),
    },
    {
        "key": "sms_angle_taxes",
        "channel": "sms",
        "label": "SMS — Tax angle (day 8)",
        "body": (
            "{first_name}, I noticed the taxes on {street} may be behind. "
            "If selling would take that off your plate, I can make an offer "
            "this week. Interested? Reply STOP to opt out."
        ),
    },
    {
        "key": "sms_angle_inherited",
        "channel": "sms",
        "label": "SMS — Inherited/probate angle",
        "body": (
            "Hi {first_name} — if you've inherited {street} and it's more "
            "trouble than it's worth, I buy houses as-is, no repairs needed. "
            "Want a number? Reply STOP to opt out."
        ),
    },
    {
        "key": "sms_angle_landlord",
        "channel": "sms",
        "label": "SMS — Tired landlord angle",
        "body": (
            "{first_name}, are you still renting out {street}? If the tenants "
            "or the upkeep are wearing you down, I'll buy it with them still "
            "in place. Worth a conversation? Reply STOP to opt out."
        ),
    },
    {
        "key": "sms_last_touch",
        "channel": "sms",
        "label": "SMS — Last touch (day 18)",
        "body": (
            "{first_name}, I'll stop reaching out after this — just didn't "
            "want you to lose my number if your plans for {street} change. "
            "{agent_name}, {agent_phone}. Reply STOP to opt out."
        ),
    },
    {
        "key": "sms_after_call",
        "channel": "sms",
        "label": "SMS — Missed-call follow-up (2 min after dial)",
        "body": (
            "Hi {first_name}, I just tried calling about {street}. "
            "It's {agent_name} — are you the right person to talk to about "
            "selling it? Reply STOP to opt out."
        ),
    },
    # ── Email ──────────────────────────────────────────────────
    {
        "key": "email_intro",
        "channel": "email",
        "label": "Email — Intro",
        "subject": "Question about {street}",
        "body": (
            "Hi {first_name},\n\n"
            "My name is {agent_name} with {agent_company}. I'm a local "
            "investor, not a real estate agent.\n\n"
            "I'm looking for a property in {city} and yours at {street} came "
            "up. If you've ever considered selling — or just want to know "
            "what it would bring as-is — I'd be glad to give you a number "
            "with no obligation and no pressure.\n\n"
            "A few things that make this different from listing it:\n"
            "  - I buy as-is. No repairs, no cleaning, no showings.\n"
            "  - I cover all closing costs.\n"
            "  - No commissions, no agent fees.\n"
            "  - You pick the closing date.\n\n"
            "Would a quick 10-minute call this week be useful? "
            "You can grab a time here: {calendar_link}\n\n"
            "Either way, best of luck with it.\n\n"
            "{agent_name}\n"
            "{agent_company}\n"
            "{agent_phone}\n\n"
            "If you'd rather not hear from me again, reply STOP and I'll "
            "remove you immediately."
        ),
    },
    {
        "key": "email_offer",
        "channel": "email",
        "label": "Email — Offer range",
        "subject": "An offer range for {street}",
        "body": (
            "Hi {first_name},\n\n"
            "Following up on {street}. Based on what I'm seeing sell nearby, "
            "I can work in the {offer_low} – {offer_high} range, cash, "
            "as-is, with closing costs covered.\n\n"
            "I don't know your situation, so that range may be low or it may "
            "be more than you expected — a 10-minute call tells us both.\n\n"
            "{calendar_link}\n\n"
            "{agent_name}\n{agent_phone}\n\n"
            "Reply STOP to be removed."
        ),
    },
    # ── Email: cold list ───────────────────────────────────────
    {
        "key": "email_list_intro",
        "channel": "email",
        "label": "Email — List / cold data, first touch",
        "subject": "Question about {street}",
        "body": (
            "Hi {first_name},\n\n"
            "I'm {agent_name} with {agent_company}. I'm a local investor, not a "
            "real estate agent, so there's no commission on either side of this.\n\n"
            "I'm buying in {city} and your property at {street} caught my "
            "attention. I'd rather deal with the owner directly than wait for "
            "something to hit the market.\n\n"
            "If you've ever thought about selling, here's what it looks like:\n\n"
            "  - I buy as-is. No repairs, no cleaning, no showings.\n"
            "  - I pay all normal closing costs.\n"
            "  - No commission, no listing agreement.\n"
            "  - You pick the closing date, including a fast one.\n\n"
            "I'm not asking you to decide anything from one email. If a short "
            "conversation would be useful, reply to this or call me at "
            "{agent_phone} and I'll give you a straight number with no "
            "obligation attached.\n\n"
            "If you'd rather not hear from me, just reply STOP and I'll remove "
            "you and not write again.\n\n"
            "{agent_name}\n"
            "{agent_company}\n"
            "{agent_phone}\n"
            "{postal_address}\n\n"
            "You are receiving this because your property is on public record. "
            "Reply STOP to be removed."
        ),
    },
    {
        "key": "email_list_followup",
        "channel": "email",
        "label": "Email — List / cold data, second touch",
        "subject": "Following up on {street}",
        "body": (
            "Hi {first_name},\n\n"
            "I wrote to you recently about {street} and haven't heard back, "
            "which is completely fine. Following up once in case it arrived at "
            "a busy time.\n\n"
            "I'm still buying in {city} and I can make a cash offer on your "
            "house as it sits — no repairs, no agent, no commission, and I cover "
            "closing costs.\n\n"
            "If selling is on your mind at all this year, a two-minute call is "
            "all I need to tell you whether I can beat what you're expecting.\n\n"
            "{agent_name}\n{agent_phone}\n\n"
            "Reply STOP and I won't email again.\n\n"
            "{agent_company}\n{postal_address}"
        ),
    },
    # ── Email: probate / inherited ─────────────────────────────
    {
        "key": "email_probate_intro",
        "channel": "email",
        "label": "Email — Probate / inherited, first touch (empathy first)",
        "subject": "{street} — a quick note, and no pressure",
        "body": (
            "Dear {first_name},\n\n"
            "Please accept my condolences on your loss. I know a message like "
            "this can land badly, so I'll be brief and I won't call you "
            "unexpectedly.\n\n"
            "I'm {agent_name} with {agent_company}. I'm a local investor, not "
            "an agent. I'm writing because I sometimes help families with a "
            "house they've inherited — usually the last thing anyone wants to "
            "manage on top of everything else.\n\n"
            "If it would help, I can take the property as-is, with the contents "
            "still inside, and handle the cleanout myself. No repairs, no "
            "showings, no commission, and I cover closing costs. If the house "
            "is better kept in the family, that's a perfectly good answer too "
            "and I won't write again.\n\n"
            "There's no timeline and no pressure from me. If a short "
            "conversation would be useful, you can reach me at {agent_phone}. "
            "If you'd rather not hear from me, just reply STOP and I'll remove "
            "you from my list.\n\n"
            "With sympathy and respect,\n\n"
            "{agent_name}\n"
            "{agent_company}\n"
            "{agent_phone}\n"
            "{postal_address}"
        ),
    },
    {
        "key": "email_probate_followup",
        "channel": "email",
        "label": "Email — Probate / inherited, follow-up",
        "subject": "One more note about {street}",
        "body": (
            "Dear {first_name},\n\n"
            "I wrote to you a few weeks ago about the property at {street} and "
            "wanted to leave one more note in case the timing is better now.\n\n"
            "Estates often take longer than anyone expects, and a house that "
            "sits empty costs money every month in taxes, insurance and upkeep. "
            "If selling would simplify things, I can buy it as-is and close on "
            "a date that suits you and the other heirs.\n\n"
            "If you've already made other arrangements, I'm glad to hear it — "
            "just let me know at {agent_phone} and I'll close my file.\n\n"
            "Wishing you well,\n\n"
            "{agent_name}\n{agent_company}\n\n"
            "Reply STOP and I won't email again.\n\n"
            "{postal_address}"
        ),
    },
    # ── Physical mail (queued, never emailed; opt-in per class) ──
    {
        "key": "mail_list_intro",
        "channel": "mail",
        "label": "Letter — List / cold data, first touch",
        "body": (
            "{agent_name}\n{agent_company}\n{agent_phone}\n\n"
            "{today}\n\n"
            "{owner_name}\n{mailing_address}\n\n"
            "Re: {address}\n\n"
            "Dear {first_name},\n\n"
            "My name is {agent_name} and I buy houses in {city} directly — "
            "I am an investor, not a real estate agent, so there is no "
            "commission on either side of this.\n\n"
            "I am writing about your property at {address}. I am looking to "
            "buy in that area and I would rather deal with the owner "
            "directly than wait for something to come on the market.\n\n"
            "If you have ever considered selling, here is what working with "
            "me looks like:\n\n"
            "  - I buy in as-is condition. No repairs, no cleaning, no "
            "showings.\n"
            "  - I pay all normal closing costs.\n"
            "  - There is no commission and no listing agreement.\n"
            "  - You choose the closing date, including a fast one.\n\n"
            "I am not asking you to decide anything from this letter. If a "
            "short conversation would be useful, call or text me at "
            "{agent_phone} and I will give you a straight number with no "
            "obligation attached.\n\n"
            "If you are not interested, no problem at all — call me at "
            "{agent_phone} or return this letter and I will remove you from "
            "my list and not write again.\n\n"
            "Thank you for your time,\n\n"
            "{agent_name}\n{agent_company}\n{agent_phone}"
        ),
    },
    {
        "key": "mail_list_followup",
        "channel": "mail",
        "label": "Letter — List / cold data, second touch",
        "body": (
            "{agent_name}\n{agent_company}\n{agent_phone}\n\n"
            "{today}\n\n"
            "{owner_name}\n{mailing_address}\n\n"
            "Re: {address} — second note\n\n"
            "Hi {first_name},\n\n"
            "I wrote to you recently about {address} and I have not heard "
            "back, which is completely fine. I am following up once in case "
            "my first letter arrived at a busy time.\n\n"
            "I am still buying in {city} and I can make a cash offer on your "
            "house as it sits — no repairs, no agent, no commission, and I "
            "cover closing costs.\n\n"
            "If selling is on your mind at all this year, a two-minute call "
            "is all I need to tell you whether I can beat what you expect. "
            "{agent_phone}.\n\n"
            "If you would prefer I stop writing, tell me at {agent_phone} "
            "and this is the last letter you will get from me.\n\n"
            "{agent_name}\n{agent_company}"
        ),
    },
    {
        "key": "mail_probate_intro",
        "channel": "mail",
        "label": "Letter — Probate / inherited, first touch (empathy first)",
        "body": (
            "{agent_name}\n{agent_company}\n{agent_phone}\n\n"
            "{today}\n\n"
            "{owner_name}\n{mailing_address}\n\n"
            "Re: {address}\n\n"
            "Dear {first_name},\n\n"
            "Please accept my condolences on your loss. I know a letter like "
            "this can land badly, so I will be brief and I will not call you "
            "unexpectedly.\n\n"
            "I am a local investor, not an agent. I am writing because I "
            "sometimes help families with a house they have inherited — "
            "usually the last thing anyone wants to manage on top of "
            "everything else.\n\n"
            "If it would help, I can take the property as-is, with the "
            "contents still inside, and handle the cleanup myself. No "
            "repairs, no showings, no commission, and I cover closing costs. "
            "If it turns out the house is better kept in the family, that is "
            "a perfectly good answer too and I will not write again.\n\n"
            "There is no timeline and no pressure from me. If a short "
            "conversation would be useful, you can reach me at "
            "{agent_phone}. If you would rather not hear from me, say so at "
            "that same number and I will remove you from my list.\n\n"
            "With sympathy and respect,\n\n"
            "{agent_name}\n{agent_company}\n{agent_phone}"
        ),
    },
    {
        "key": "mail_probate_followup",
        "channel": "mail",
        "label": "Letter — Probate / inherited, follow-up",
        "body": (
            "{agent_name}\n{agent_company}\n{agent_phone}\n\n"
            "{today}\n\n"
            "{owner_name}\n{mailing_address}\n\n"
            "Re: {address}\n\n"
            "Dear {first_name},\n\n"
            "I wrote to you a few weeks ago about the property at {address} "
            "and I wanted to leave one more note in case the timing is better "
            "now.\n\n"
            "Estates often take longer than anyone expects, and a house that "
            "sits empty costs money every month in taxes, insurance and "
            "upkeep. If selling would simplify things, I can buy it as-is "
            "and close on a date that suits you and the other heirs.\n\n"
            "If you have already made other arrangements, I am glad to hear "
            "it — just let me know at {agent_phone} and I will close my file "
            "and stop writing.\n\n"
            "Wishing you well,\n\n"
            "{agent_name}\n{agent_company}\n{agent_phone}"
        ),
    },
    # ── Voice ──────────────────────────────────────────────────
    {
        "key": "voice_script",
        "channel": "call",
        "label": "Voice — Live agent script (Telnyx TTS)",
        "body": (
            "Hi, this is {agent_name}. I'm calling about the property on "
            "{street} in {city} — I'm an investor, not a realtor, and I'm "
            "not calling to list it. I'm looking to buy a house in that area "
            "and wanted to ask: have you ever thought about selling it?"
        ),
    },
    {
        "key": "voice_voicemail",
        "channel": "call",
        "label": "Voice — Voicemail drop",
        "body": (
            "Hi, this is {agent_name}, {agent_phone}. I'm calling about "
            "{street} in {city} — I'm an investor looking to buy in that "
            "area, not a realtor. If selling is ever something you'd "
            "consider, I'd love to give you a number. Again, {agent_name}, "
            "{agent_phone}. Thanks."
        ),
    },
]

# ─────────────────────────────────────────────────────────────
# Cadences, one per lead class.
#
# The channel mix here is a compliance boundary, not a preference. A cadence
# that contains an sms step for a list or probate lead would be a Texas SB 140
# problem, so those cadences simply do not contain one. The sender also
# independently checks app.policy for every step (defence in depth).
#
# Step tuple: (hours_from_enrollment, channel, template_key)
# ─────────────────────────────────────────────────────────────
CADENCES: list[dict] = [
    {
        "lead_class": "inbound",
        "name": "Inbound — Speed to Lead",
        "description": (
            "They contacted us, so speed is the whole game. Call in the first "
            "minute, text 2 minutes later while the lead is still on the site, "
            "then a tight follow-up over 30 days. SMS fires here because these "
            "leads arrived inbound WITH captured consent — the only class where "
            "texting is defensible."
        ),
        "steps": [
            (0, "call", "voice_script"),
            (0.05, "sms", "sms_after_call"),          # ~3 minutes after the call
            (1, "sms", "sms_followup_day1"),
            (2, "email", "email_intro"),
            (24, "call", "voice_script"),
            (48, "sms", "sms_angle_taxes"),
            (96, "call", "voice_script"),
            (168, "sms", "sms_last_touch"),
            (30 * 24, "email", "email_offer"),
        ],
    },
    {
        "lead_class": "list",
        "name": "List — Call + Email",
        "description": (
            "Purchased or pulled data (absentee, tax delinquent, driving for "
            "dollars). No SMS: cold texting a list is the highest litigation-risk "
            "action available to a wholesaler under Texas SB 140 and Florida's "
            "FTSA. Calls and email only."
        ),
        "steps": [
            (0, "call", "voice_script"),
            (24, "email", "email_list_intro"),
            (48, "call", "voice_script"),
            (120, "call", "voice_script"),
            (10 * 24, "email", "email_list_followup"),
            (18 * 24, "call", "voice_script"),
            (30 * 24, "call", "voice_script"),
        ],
    },
    {
        "lead_class": "probate",
        "name": "Probate — Written first, then calls",
        "description": (
            "Inherited and estate properties. Written outreach goes first on "
            "purpose: a stranger calling a grieving family is the fastest way to "
            "lose the lead, and the email gives them time and a way to opt out "
            "before anyone dials. No SMS."
        ),
        "steps": [
            (0, "email", "email_probate_intro"),
            (4 * 24, "call", "voice_script"),
            (10 * 24, "email", "email_probate_followup"),
            (16 * 24, "call", "voice_script"),
            (25 * 24, "call", "voice_script"),
            (40 * 24, "email", "email_probate_followup"),
        ],
    },
    {
        "lead_class": "referral",
        "name": "Referral — Call + Email",
        "description": (
            "A named third party sent them to us. Warm enough for email, but not "
            "a consented text relationship, so no SMS."
        ),
        "steps": [
            (0, "call", "voice_script"),
            (24, "email", "email_intro"),
            (4 * 24, "call", "voice_script"),
            (7 * 24, "email", "email_list_followup"),
            (14 * 24, "call", "voice_script"),
            (30 * 24, "email", "email_offer"),
        ],
    },
]


def cadence_for_class(lead_class: str) -> dict:
    from .policy import normalize_class

    target = normalize_class(lead_class)
    for c in CADENCES:
        if c["lead_class"] == target:
            return c
    return CADENCES[1]  # list is the safe default


def seed_templates(db: Session) -> None:
    """Insert any missing default templates. Never overwrites your edits."""
    existing = {t.key for t in db.scalars(select(MessageTemplate)).all()}
    added = 0
    for t in TEMPLATES:
        if t["key"] in existing:
            continue
        db.add(
            MessageTemplate(
                key=t["key"],
                channel=t["channel"],
                label=t.get("label", ""),
                subject=t.get("subject", ""),
                body=t["body"],
            )
        )
        added += 1
    if added:
        db.flush()


def seed_cadences(db: Session) -> list[Cadence]:
    """Create one cadence per lead class. Idempotent, and safe to run on a
    database that already has the original single 'default' cadence."""
    made: list[Cadence] = []
    for spec in CADENCES:
        existing = db.scalar(select(Cadence).where(Cadence.name == spec["name"]))
        if existing:
            # Keep the class label correct even if it pre-dates this column.
            existing.lead_class = spec["lead_class"]
            made.append(existing)
            continue
        cadence = Cadence(
            name=spec["name"],
            description=spec["description"],
            lead_class=spec["lead_class"],
            is_default=False,
            is_active=True,
        )
        db.add(cadence)
        db.flush()
        for position, (delay_hours, channel, template_key) in enumerate(spec["steps"]):
            db.add(
                CadenceStep(
                    cadence_id=cadence.id,
                    position=position,
                    channel=channel,
                    delay_hours=float(delay_hours),
                    template_key=template_key,
                )
            )
        made.append(cadence)

    # The inbound cadence is the "default" used when a class is somehow unknown.
    for c in made:
        c.is_default = c.lead_class == "inbound"
    db.flush()
    return made


def seed_default_cadence(db: Session) -> Cadence:
    """Backwards-compatible shim used by the boot sequence."""
    made = seed_cadences(db)
    for c in made:
        if c.is_default:
            return c
    return made[0]


def get_template(db: Session, key: str) -> MessageTemplate | None:
    return db.scalar(select(MessageTemplate).where(MessageTemplate.key == key))
