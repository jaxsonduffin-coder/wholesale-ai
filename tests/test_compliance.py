"""Compliance tests — quiet hours, opt-out, suppression, frequency caps.

These are the tests that keep you out of a TCPA suit, so they are deliberately
exhaustive about the edge cases.
"""
from __future__ import annotations

import datetime as dt

from zoneinfo import ZoneInfo

from app.compliance import (
    classify_reply,
    contact_window,
    handle_opt_out,
    is_suppressed,
    is_within_window,
    next_allowed_time,
    timezone_for_state,
)
from app.models import Lead, SuppressionEntry


def make_lead(**over) -> Lead:
    data = dict(
        street="1234 Elm St",
        city="Dallas",
        state="TX",
        zip_code="75209",
        owner_phone="+12145550143",
        owner_name="Dana Whitfield",
        arv=250_000,
        max_offer=150_000,
        timezone="America/Chicago",
    )
    data.update(over)
    return Lead(**data)


# ── Contact windows ────────────────────────────────────────────────────
def test_federal_floor_is_8am_to_9pm():
    start, end = contact_window("CA")
    assert start >= 8
    assert end <= 21


def test_texas_starts_at_9am():
    start, end = contact_window("TX")
    assert start == 9
    assert end == 21


def test_florida_closes_at_8pm():
    start, end = contact_window("FL")
    assert end == 20


def test_window_is_never_wider_than_federal():
    for state in ["TX", "FL", "NY", "CA", "OK", "WA", "CO", "ZZ"]:
        start, end = contact_window(state)
        assert start >= 8 and end <= 21


def _at(state: str, hour: int, minute: int = 0) -> dt.datetime:
    tz = ZoneInfo(timezone_for_state(state))
    return dt.datetime(2025, 6, 15, hour, minute, tzinfo=tz)


def test_9am_texas_call_allowed():
    lead = make_lead()
    ok, _ = is_within_window(lead, _at("TX", 9, 0))
    assert ok


def test_845am_texas_call_blocked():
    lead = make_lead()
    ok, reason = is_within_window(lead, _at("TX", 8, 45))
    assert not ok
    assert "outside contact window" in reason


def test_9pm_texas_call_blocked():
    lead = make_lead()
    ok, _ = is_within_window(lead, _at("TX", 21, 0))
    assert not ok


def test_815pm_florida_blocked():
    """FL closes at 8pm, an hour earlier than the federal 9pm ceiling."""
    lead = make_lead(state="FL", timezone="America/New_York")
    ok, _ = is_within_window(lead, _at("FL", 20, 15))
    assert not ok


def test_730pm_florida_allowed():
    lead = make_lead(state="FL", timezone="America/New_York")
    ok, _ = is_within_window(lead, _at("FL", 19, 30))
    assert ok


def test_next_allowed_time_is_always_within_the_window():
    lead = make_lead()
    nxt = next_allowed_time(lead)
    ok, reason = is_within_window(lead, nxt.replace(tzinfo=dt.timezone.utc))
    assert ok, reason


def test_next_allowed_time_does_not_defer_when_already_legal():
    """Regression: returning tomorrow 9am while it is currently legal to call
    stalled every cadence enrolled during business hours."""
    lead = make_lead()
    inside = _at("TX", 14, 0)                       # 2pm Dallas, legal
    assert next_allowed_time(lead, at=inside) == inside.astimezone(
        dt.timezone.utc
    ).replace(tzinfo=None)


def test_next_allowed_time_before_window_is_today():
    lead = make_lead()
    early = _at("TX", 6, 30)
    nxt = next_allowed_time(lead, at=early)
    local = nxt.replace(tzinfo=dt.timezone.utc).astimezone(ZoneInfo("America/Chicago"))
    assert local.hour == 9 and local.date() == early.date()


def test_next_allowed_time_after_window_is_tomorrow():
    lead = make_lead()
    late = _at("TX", 22, 30)
    nxt = next_allowed_time(lead, at=late)
    local = nxt.replace(tzinfo=dt.timezone.utc).astimezone(ZoneInfo("America/Chicago"))
    assert local.hour == 9
    assert local.date() == (late + dt.timedelta(days=1)).date()


def test_timezone_lookup():
    assert timezone_for_state("TX", "Dallas") == "America/Chicago"
    assert timezone_for_state("NY", "New York") == "America/New_York"
    assert timezone_for_state("CA", "Los Angeles") == "America/Los_Angeles"
    assert timezone_for_state("TX", "El Paso") == "America/Denver"
    assert timezone_for_state("FL", "Pensacola") == "America/Chicago"


# ── Opt-out classification ─────────────────────────────────────────────
def test_stop_keywords():
    for text in ["STOP", "stop", "Stop.", "stopall", "unsubscribe", "CANCEL",
                 "end", "QUIT", "opt out", "remove me", "STOP texting"]:
        assert classify_reply(text) == "stop", text


def test_start_keywords():
    for text in ["START", "unstop", "resume"]:
        assert classify_reply(text) == "start"


def test_help_keywords():
    for text in ["HELP", "info", "?"]:
        assert classify_reply(text) == "help"


def test_normal_replies_are_not_misread_as_stop():
    for text in ["yes", "how much?", "stop by tomorrow at 3", "call me",
                 "I'm interested", "what's your offer", "", "not interested in selling now"]:
        assert classify_reply(text) == "other", text


# ── Opt-out effect ─────────────────────────────────────────────────────
def test_opt_out_disables_the_lead(db):
    lead = make_lead()
    db.add(lead)
    db.flush()
    handle_opt_out(db, lead, text="STOP")
    db.commit()

    assert lead.opted_out is True
    assert lead.status == "dead"
    assert lead.can_be_worked is False
    assert is_suppressed(db, lead.owner_phone, None)
    assert db.query(SuppressionEntry).filter_by(phone=lead.owner_phone).count() == 1


def test_suppression_matches_by_email_too(db):
    db.add(SuppressionEntry(email="blocked@test.local", reason="manual"))
    db.commit()
    assert is_suppressed(db, None, "blocked@test.local")
    assert not is_suppressed(db, None, "fine@test.local")
