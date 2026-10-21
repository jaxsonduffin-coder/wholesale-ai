"""Inbound intent detection.

The whole point of the AI doing the outreach is that you should only be pulled
in when a seller is actually interested enough to talk numbers or sign. That
requires reading a reply like a human would, which is what this module does.

Design rules:
  * Negative beats positive. A reply containing "not interested" is never hot,
    even if it also contains "offer" ("not interested in your offer").
  * A price question is the strongest buying signal there is. Someone who asks
    "how much would you give me" has already decided they might sell.
  * Ambiguity is not interest. "who is this" stays `question`, because handing
    you a lead that only asked who you are wastes your time.
  * `stop` is handled earlier by compliance.classify_reply and never reaches
    here as interest.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ─────────────────────────────────────────────────────────────
# Signal vocabularies. Phrase-level, matched against normalised text.
# ─────────────────────────────────────────────────────────────
NEGATIVE_PHRASES = [
    "not interested", "no interest", "no thanks", "no thank you", "not for sale",
    "not selling", "not sell", "won't sell", "wont sell", "will not sell",
    "already sold", "sold it", "sold the house", "under contract",
    "already listed", "it's listed", "its listed", "have a realtor",
    "have an agent", "working with an agent", "keep the house", "keeping it",
    "not right now", "not at this time", "maybe later", "never",
    "wrong number", "no longer own", "dont own", "don't own", "not mine",
    "leave me alone", "do not contact", "don't contact", "dont contact",
    "remove me", "take me off", "stop texting", "stop calling", "harassing",
    "scam", "spam",
]

# Asking what we would pay is the single strongest signal in the business.
PRICE_PHRASES = [
    "how much", "what's your offer", "whats your offer", "what is your offer",
    "your offer", "make an offer", "what would you pay", "what will you pay",
    "what can you pay", "what would you give", "what can you offer",
    "what's the price", "whats the price", "price", "how much would",
    "what's your best", "whats your best", "number for it", "give me a number",
    "send me a number", "cash offer", "your best offer",
    "what are you thinking", "what did you have in mind", "how much are you",
    "what kind of number", "ballpark",
]

# Explicit statements of intent. These are worth waking you up for.
STRONG_APPROVAL_PHRASES = [
    "interested", "i'm interested", "im interested", "very interested",
    "i want to sell", "want to sell", "willing to sell", "looking to sell",
    "need to sell", "ready to sell", "thinking about selling",
    "consider selling", "would consider", "planning to sell",
    "let's do it", "lets do it", "let's talk", "lets talk",
    "tell me more", "more info", "more information", "send it",
    "sell the house", "sell my house", "sell the property",
]

# Bare affirmatives. Could be answering "is this the right number?" — real, but
# not enough on their own to pull a human in.
WEAK_APPROVAL_PHRASES = [
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "sounds good",
    "go ahead", "please do", "i do", "that works", "works for me",
    "send me", "possibly", "maybe", "perhaps",
]

APPOINTMENT_PHRASES = [
    "call me", "give me a call", "call me back", "phone me", "ring me",
    "when can you", "what time", "what day", "let's schedule",
    "lets schedule", "schedule a", "set up a time", "available",
    "i'm available", "im available", "free tomorrow", "free today",
    "come by", "stop by", "meet", "meeting", "appointment",
    "text me at", "call me at", "reach me at", "best time to call",
]

URGENCY_PHRASES = [
    "asap", "as soon as possible", "urgent", "right away", "immediately",
    "foreclosure", "behind on taxes", "back taxes", "need it gone",
    "need to close", "quickly", "this week", "relocating", "divorce",
    "probate", "inherited", "estate", "moving out of state", "job transfer",
    "medical", "hospital",
]

QUESTION_ONLY_PHRASES = [
    "who is this", "who are you", "how did you get", "where did you get",
    "is this a scam", "what company", "what do you do", "why are you texting",
    "unsubscribe", "is this", "who's this", "whos this",
]

# Weights determine ordering: a price question outranks a bare "yes".
WEIGHTS = {
    "negative": -100,
    "price": 45,            # asking what we pay = already selling in their head
    "appointment": 30,      # a scheduling signal alone is enough
    "strong_approval": 32,
    "weak_approval": 18,
    "urgency": 12,
    "question_only": -6,
}

# Thresholds on the summed score.
HOT_AT = 30               # pull a human in
APPOINTMENT_AT = 30       # a meeting request qualifies on its own


@dataclass
class Intent:
    label: str                        # interested | appointment | question | negative | other
    score: int
    matched: list[str] = field(default_factory=list)
    is_hot: bool = False
    wants_price: bool = False
    wants_meeting: bool = False
    is_negative: bool = False
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "score": self.score,
            "matched": self.matched,
            "is_hot": self.is_hot,
            "wants_price": self.wants_price,
            "wants_meeting": self.wants_meeting,
            "is_negative": self.is_negative,
            "summary": self.summary,
        }


def _norm(text: str) -> str:
    t = (text or "").lower()
    t = t.replace("\u2019", "'").replace("\u2018", "'")
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _hits(norm: str, phrases: list[str]) -> list[str]:
    """Phrase match with word boundaries so 'no' does not fire inside 'not'."""
    found = []
    for p in phrases:
        if " " in p:
            if p in norm:
                found.append(p)
        else:
            if re.search(rf"(?<![a-z]){re.escape(p)}(?![a-z])", norm):
                found.append(p)
    return found


def classify_inbound(text: str) -> Intent:
    """Read an inbound reply and decide how hot it is."""
    norm = _norm(text)
    if not norm:
        return Intent(label="other", score=0, summary="empty message")

    neg = _hits(norm, NEGATIVE_PHRASES)
    if neg:
        return Intent(
            label="negative",
            score=WEIGHTS["negative"],
            matched=neg,
            is_negative=True,
            summary=f"declined: {', '.join(neg[:2])}",
        )

    price = _hits(norm, PRICE_PHRASES)
    appt = _hits(norm, APPOINTMENT_PHRASES)
    strong = _hits(norm, STRONG_APPROVAL_PHRASES)
    weak = _hits(norm, WEAK_APPROVAL_PHRASES)
    urgency = _hits(norm, URGENCY_PHRASES)
    qonly = _hits(norm, QUESTION_ONLY_PHRASES)

    score = 0
    matched: list[str] = []
    for group, weight in (
        (price, WEIGHTS["price"]),
        (appt, WEIGHTS["appointment"]),
        (strong, WEIGHTS["strong_approval"]),
        (weak, WEIGHTS["weak_approval"]),
        (urgency, WEIGHTS["urgency"]),
    ):
        if group:
            # Diminishing returns; three synonyms are not three times as hot.
            score += weight if len(group) == 1 else weight + 8
            matched.extend(group)
    if qonly:
        score += WEIGHTS["question_only"]
        matched.extend(qonly)

    wants_price = bool(price)
    wants_meeting = bool(appt)

    positive = bool(price or appt or strong or weak or urgency)

    if positive:
        label = "appointment" if wants_meeting else "interested"
    elif qonly:
        label = "question"
    else:
        label = "other"

    # A bare "who is this" is not a hot lead even if it scores above zero;
    # asking the price is, even when they also asked who we are.
    is_hot = (
        label in ("interested", "appointment")
        and score >= HOT_AT
        and not (qonly and not (price or appt or strong))
    )

    if wants_price and wants_meeting:
        summary = "asked for a number and wants to talk"
    elif wants_price:
        summary = "asked what we would pay"
    elif wants_meeting:
        summary = "wants a call or meeting"
    elif label == "interested":
        summary = "said yes / interested"
    elif qonly:
        summary = "asked a question but has not engaged"
    else:
        summary = label

    return Intent(
        label=label,
        score=score,
        matched=matched[:8],
        is_hot=is_hot,
        wants_price=wants_price,
        wants_meeting=wants_meeting,
        summary=summary,
    )
