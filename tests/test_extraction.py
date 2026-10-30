"""Post-call extraction: deterministic fallback on a fixture transcript, and
the Claude path's JSON handling (with a fake client — no network)."""
import json

from app import extraction
from conftest import FIXTURE_TRANSCRIPT, FakeAnthropic, FakeLLM
from app import llm


def test_fallback_extracts_the_essentials(biz):
    f, by = extraction.extract(FIXTURE_TRANSCRIPT, biz)
    assert by == "fallback"
    assert f["name"] == "Maria Lopez"
    assert f["intent"] == "book_appointment"
    assert f["requested_service"] == "Emergency / tooth pain"
    assert f["interest"] in ("medium", "high")
    assert any("cracked" in p.lower() for p in f["pain_points"])
    assert any("insurance" in o.lower() or "cost" in o.lower() for o in f["objections"])
    assert f["profile"]["preferred_name"] == "Maria"
    assert f["profile"]["address"].startswith("1234 E Main St")
    assert "slower" in f["profile"]["notes"]
    assert f["summary"] and len(f["summary"]) <= 400
    assert set(f) == set(extraction.KEYS)


def test_fallback_uses_booked_appointment(biz):
    appt = {"id": 7, "start": "2026-09-15T14:30", "type": "Emergency / tooth pain"}
    f, _ = extraction.extract(FIXTURE_TRANSCRIPT, biz, appointment=appt)
    assert f["interest"] == "high"
    assert "APT-0007" in f["next_step"]
    assert f["requested_service"] == "Emergency / tooth pain"


def test_fallback_on_empty_transcript(biz):
    f, by = extraction.extract("", biz)
    assert by == "fallback"
    assert f["intent"] == "other" and f["interest"] == "low"
    assert f["pain_points"] == [] and f["objections"] == [] and f["profile"] == {}


CANNED = json.dumps({"name": "Maria Lopez", "summary": "Maria cracked a tooth and booked 2:30.",
                     "intent": "Book Appointment", "pain_points": "cracked tooth, pain",
                     "objections": ["no insurance"], "requested_service": "Emergency / tooth pain",
                     "next_step": "Arrive 2:30 today", "interest": "VERY HIGH",
                     "profile": {"preferred_name": "Maria", "address": None}})


def test_claude_cli_path_parses_json_and_coerces(biz, fake_llm):
    fake_llm.replies = ["Here you go:\n" + CANNED]
    f, by = extraction.extract(FIXTURE_TRANSCRIPT, biz)
    assert by == "claude"
    assert f["intent"] == "book_appointment"          # normalised
    assert f["interest"] == "medium"                  # unknown value -> medium, not crash
    assert f["pain_points"] == ["cracked tooth, pain"]  # string -> list
    assert f["profile"] == {"preferred_name": "Maria"}  # nulls dropped
    assert FIXTURE_TRANSCRIPT in fake_llm.calls[0]["user"]
    assert "ONE JSON object" in fake_llm.calls[0]["system"]


def test_claude_sdk_path_when_key_is_explicit(biz, fake_claude, monkeypatch):
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: FakeAnthropic(CANNED))
    assert llm.mode() == "api"
    f, by = extraction.extract(FIXTURE_TRANSCRIPT, biz)
    assert by == "claude" and f["name"] == "Maria Lopez"
    assert FIXTURE_TRANSCRIPT in FakeAnthropic.calls[-1]["messages"][0]["content"]


def test_claude_failure_degrades_to_fallback(biz, fake_llm, monkeypatch):
    fake_llm.replies = ["no json here"]
    f, by = extraction.extract(FIXTURE_TRANSCRIPT, biz)
    assert by == "fallback" and f["name"] == "Maria Lopez"

    def boom(*a, **k):
        raise RuntimeError("claude -p exit 1: boom")
    monkeypatch.setattr(llm, "complete", boom)
    f, by = extraction.extract(FIXTURE_TRANSCRIPT, biz)
    assert by == "fallback" and f["name"] == "Maria Lopez"


def test_fallback_reads_callback_as_open_booking(biz):
    t = ("Caller: Hi, this is Dana Reyes. I chipped a front tooth and want to get it looked at this "
         "week, but I'm nervous about the cost since I don't have insurance.\n"
         "Ivy: Would you like me to check openings?\n"
         "Caller: Let me think about it and call back tomorrow. Thanks, bye.")
    f, _ = extraction.extract(t, biz)
    assert f["intent"] == "book_appointment"
    assert f["interest"] == "medium"
    assert "call back" in f["next_step"] and "nothing booked" in f["next_step"]
    assert any("insurance" in o for o in f["objections"])
    assert any("think about it" in o for o in f["objections"])


def test_fallback_emergency_only_for_911_language(biz):
    f, _ = extraction.extract("Caller: I have an emergency, my tooth is killing me, can I come in today?", biz)
    assert f["intent"] == "book_appointment"
    f, _ = extraction.extract("Caller: My husband passed out and can't breathe, what do I do?", biz)
    assert f["intent"] == "emergency"
