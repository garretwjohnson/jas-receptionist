"""Per-caller memory: prompt injection, record_call, brief, scoping, export."""
import datetime as dt

from app import booking, brain, callers
from conftest import ALICE, BOB, FIXTURE_TRANSCRIPT


def test_normalize_phone():
    n = callers.normalize_phone
    assert n("480-555-0142") == "+14805550142"
    assert n("(480) 555-0142") == "+14805550142"
    assert n("1 480 555 0142") == "+14805550142"
    assert n("+14805550142") == "+14805550142"
    assert n("+44 20 7946 0958") == "+442079460958"
    assert n("") is None and n(None) is None and n("anonymous") is None and n("12345") is None


def test_new_caller_gets_no_context(biz):
    assert callers.context_block(biz, ALICE) == ""
    callers.touch_caller(biz, ALICE)               # first call, nothing learned yet
    assert callers.context_block(biz, ALICE) == ""
    assert "CALLER CONTEXT" not in brain.system_prompt(biz, callers.context_block(biz, ALICE))


def test_returning_caller_context_is_injected_and_capped(biz):
    callers.touch_caller(biz, ALICE)
    s = callers.record_call(biz, ALICE, transcript=FIXTURE_TRANSCRIPT)
    assert s["extracted_by"] == "fallback"
    callers.touch_caller(biz, ALICE)               # second call starts
    block = callers.context_block(biz, ALICE)
    assert block.startswith("RETURNING CALLER")
    assert "Maria" in block and ALICE in block
    assert "Emergency / tooth pain" in block
    assert "Next step was:" in block
    assert len(block) <= callers.CONTEXT_CAP
    prompt = brain.system_prompt(biz, block)
    assert "CALLER CONTEXT" in prompt and block in prompt
    # profile notes ride along
    assert "1234 E Main St" in block and "slower" in block


def test_context_cap_holds_with_huge_profile(biz):
    callers.touch_caller(biz, ALICE)
    callers.record_call(biz, ALICE, transcript=FIXTURE_TRANSCRIPT)
    callers.update_profile(biz, ALICE, {f"note_{i}": "x" * 150 for i in range(10)})
    callers.touch_caller(biz, ALICE)
    assert len(callers.context_block(biz, ALICE)) <= callers.CONTEXT_CAP


def test_reply_injects_context_only_for_returning(biz, fake_llm):
    hist = [{"role": "user", "content": "Hi"}]
    brain.reply(biz["id"], hist, caller=ALICE)     # unknown number
    assert "RETURNING CALLER" not in fake_llm.calls[-1]["system"]
    brain.reply(biz["id"], hist)                   # no caller ID at all
    assert "RETURNING CALLER" not in fake_llm.calls[-1]["system"]

    callers.touch_caller(biz, ALICE)
    callers.record_call(biz, ALICE, transcript=FIXTURE_TRANSCRIPT)
    callers.touch_caller(biz, ALICE)
    brain.reply(biz["id"], hist, caller="480-555-0142")   # same number, different format
    sys_prompt = fake_llm.calls[-1]["system"]
    assert "RETURNING CALLER" in sys_prompt and "Maria" in sys_prompt
    assert "TOOLS" in sys_prompt                   # cli path carries the text tool protocol
    assert fake_llm.calls[-1]["user"].endswith("Caller: Hi\nIvy:")


def test_reply_sdk_path_injects_context_too(biz, fake_claude):
    callers.touch_caller(biz, ALICE)
    callers.record_call(biz, ALICE, transcript=FIXTURE_TRANSCRIPT)
    callers.touch_caller(biz, ALICE)
    text, trace = brain.reply(biz["id"], [{"role": "user", "content": "Hi"}], caller=ALICE)
    assert text == "Okay." and trace == []
    kw = fake_claude.calls[-1]
    assert "RETURNING CALLER" in kw["system"] and kw["tools"] == brain.TOOLS


def test_cli_tool_protocol_runs_tools_and_feeds_results_back(biz, fake_llm):
    day = dt.date.today() + dt.timedelta(days=7)
    while day.weekday() >= 5:
        day += dt.timedelta(days=1)
    fake_llm.replies = [
        'Sure.\n```json\n{"tool": "check_availability", "args": {"date": "%s", "minutes": 30}}\n```' % day.isoformat(),
        "Ivy: I have a few openings that day — would morning or afternoon suit you?",
    ]
    text, trace = brain.reply(biz["id"], [{"role": "user", "content": "Any openings next week?"}])
    assert text.startswith("I have a few openings")          # "Ivy:" prefix stripped
    assert len(trace) == 1 and trace[0]["tool"] == "check_availability"
    assert trace[0]["result"]["available"]
    assert len(fake_llm.calls) == 2
    assert "TOOL RESULT" in fake_llm.calls[1]["user"] and "check_availability" in fake_llm.calls[1]["user"]


def test_cli_tool_protocol_bad_args_do_not_crash(biz, fake_llm):
    fake_llm.replies = ['{"tool": "book_appointment", "args": {"start": "nope"}}', "Sorry, one moment."]
    text, trace = brain.reply(biz["id"], [{"role": "user", "content": "book it"}])
    assert text == "Sorry, one moment." and "error" in trace[0]["result"]


def test_parse_tool_call():
    assert brain._parse_tool_call("Okay, booking now.") is None
    assert brain._parse_tool_call('{"tool": "nope", "args": {}}') is None
    assert brain._parse_tool_call(' {"tool": "take_message", "args": {"name": "A"}} ') == {
        "tool": "take_message", "args": {"name": "A"}}


def test_record_call_links_appointment_and_updates_profile(biz):
    started = dt.datetime.now().isoformat(timespec="seconds")
    callers.touch_caller(biz, ALICE)
    day = dt.date.today() + dt.timedelta(days=7)
    while day.weekday() >= 5:
        day += dt.timedelta(days=1)
    slot = booking.free_slots(biz, day, 30)[0]
    appt_id = booking.book(biz, slot.strftime("%Y-%m-%dT%H:%M"), 30, "Emergency / tooth pain",
                           "Maria Lopez", "480-555-0142")
    assert isinstance(appt_id, int)
    s = callers.record_call(biz, ALICE, transcript=FIXTURE_TRANSCRIPT, started=started)
    assert s["appointment_id"] == appt_id
    assert s["interest"] == "high" and f"APT-{appt_id:04d}" in s["next_step"]
    c = callers.get_caller(biz, ALICE)
    assert c["name"] == "Maria Lopez" and c["call_count"] == 1
    assert c["profile"]["preferred_name"] == "Maria"
    rows = callers.summaries(biz, ALICE)
    assert len(rows) == 1 and rows[0]["transcript"] == FIXTURE_TRANSCRIPT
    assert rows[0]["pain_points"] and isinstance(rows[0]["pain_points"], list)


def test_record_call_from_history_without_prior_touch(biz):
    hist = [{"role": "assistant", "content": "Hello"},
            {"role": "user", "content": "This is Bob. Do you see kids? I was thinking of a cleaning."}]
    s = callers.record_call(biz, BOB, history=hist)
    assert s and s["phone"] == BOB
    assert callers.get_caller(biz, BOB)["call_count"] == 1
    assert s["requested_service"] == "Cleaning & exam"


def test_anonymous_caller_is_not_stored(biz):
    assert callers.touch_caller(biz, "anonymous") is None
    assert callers.record_call(biz, "", transcript=FIXTURE_TRANSCRIPT) is None
    assert callers.recent(biz) == []


def test_data_is_scoped_to_the_callers_number(biz):
    callers.touch_caller(biz, ALICE)
    callers.record_call(biz, ALICE, transcript=FIXTURE_TRANSCRIPT)
    callers.touch_caller(biz, BOB)
    callers.record_call(biz, BOB, history=[
        {"role": "user", "content": "Hi, I'm Bob Chen, I want a whitening consult but it's too expensive."}])
    # brief
    a, b = callers.brief(biz, ALICE), callers.brief(biz, BOB)
    assert a["caller"]["phone"] == ALICE and b["caller"]["phone"] == BOB
    assert all(s["phone"] == ALICE for s in a["summaries"])
    assert "Bob" not in callers.brief_html(a) and "Maria" not in callers.brief_html(b)
    assert "whitening" not in callers.brief_html(a).lower()
    # context
    ca, cb = callers.context_block(biz, ALICE), callers.context_block(biz, BOB)
    assert "Maria" in ca and "Bob" not in ca
    assert "Bob" in cb and "Maria" not in cb and "Whitening" in cb
    # summaries + profile
    assert all(s["phone"] == BOB for s in callers.summaries(biz, BOB))
    assert callers.get_caller(biz, ALICE)["name"] != callers.get_caller(biz, BOB)["name"]
    # a third, unknown number sees nothing
    assert callers.brief(biz, "+15555550000") is None
    assert callers.context_block(biz, "+15555550000") == ""


def test_brief_returns_last_three_only(biz):
    callers.touch_caller(biz, ALICE)
    for i in range(5):
        callers.record_call(biz, ALICE, history=[
            {"role": "user", "content": f"Call number {i}. Question about hours?"}])
    b = callers.brief(biz, ALICE)
    assert len(b["summaries"]) == 3
    assert [s["summary"] for s in b["summaries"]] == sorted(
        [s["summary"] for s in b["summaries"]], reverse=True)   # newest first
    assert "Call number 4" in b["summaries"][0]["summary"]
    assert callers.get_caller(biz, ALICE)["call_count"] == 1    # record_call alone doesn't inflate


def test_brief_html_renders_one_screen(biz):
    callers.touch_caller(biz, ALICE)
    callers.record_call(biz, ALICE, transcript=FIXTURE_TRANSCRIPT)
    html = callers.brief_html(callers.brief(biz, ALICE))
    assert "<title>Brief — Maria" in html
    assert ALICE in html and "1234 E Main St" in html
    assert "Emergency / tooth pain" in html and "Next step" in html
    assert "Pain points" in html and "Objections" in html
    assert "<script" not in html                       # static, no JS needed
    # escaping: a hostile name never becomes markup
    callers.update_profile(biz, ALICE, {"preferred_name": "<img src=x onerror=alert(1)>"})
    assert "<img" not in callers.brief_html(callers.brief(biz, ALICE))


def test_new_leads_cursor_export(biz):
    assert callers.new_leads(biz) == ([], 0)
    callers.touch_caller(biz, ALICE)
    callers.record_call(biz, ALICE, transcript=FIXTURE_TRANSCRIPT)
    callers.touch_caller(biz, BOB)
    callers.record_call(biz, BOB, history=[{"role": "user", "content": "Hi I'm Bob, cleaning please"}])
    leads, cursor = callers.new_leads(biz)
    assert [l["phone"] for l in leads] == [ALICE, BOB]
    assert cursor == leads[-1]["summary_id"]
    assert leads[0]["source_id"] == f"jas-receptionist:{biz['id']}:call:{leads[0]['summary_id']}"
    assert leads[0]["name"] == "Maria Lopez" and leads[0]["source"] == "jas-receptionist"
    assert "transcript" not in leads[0]
    more, cursor2 = callers.new_leads(biz, since_id=cursor)
    assert more == [] and cursor2 == cursor
    only_bob, _ = callers.new_leads(biz, since_id=leads[0]["summary_id"])
    assert [l["phone"] for l in only_bob] == [BOB]


def test_recent_callers_for_office(biz):
    callers.touch_caller(biz, ALICE)
    callers.record_call(biz, ALICE, transcript=FIXTURE_TRANSCRIPT)
    rows = callers.recent(biz)
    assert len(rows) == 1 and rows[0]["phone"] == ALICE
    assert rows[0]["requested_service"] == "Emergency / tooth pain"
    assert rows[0]["interest"] in ("medium", "high")


def test_existing_db_gains_new_tables(biz):
    """Old data/<biz>.db files only had 3 tables; conn() must add the new ones."""
    c = booking.conn(biz["id"])
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    c.close()
    assert {"appointments", "messages", "call_log", "callers", "call_summaries"} <= names
