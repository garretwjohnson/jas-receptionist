"""HTTP surface: call lifecycle, brief (JSON + HTML), office card data, leads."""
from conftest import ALICE, BOB, FIXTURE_TRANSCRIPT


def test_call_lifecycle_and_brief(client):
    r = client.post("/api/call/start", json={"caller": "480-555-0142"}).json()
    assert r["caller"] == ALICE and r["returning"] is False and r["caller_name"] is None

    r = client.post("/api/call/end", json={"caller": "480-555-0142", "transcript": FIXTURE_TRANSCRIPT,
                                           "outcome": "demo call"}).json()
    assert r["ok"] and r["summary"]["phone"] == ALICE
    assert r["summary"]["extracted_by"] == "fallback"
    assert "transcript" not in r["summary"]

    r = client.post("/api/call/start", json={"caller": "+1 (480) 555-0142"}).json()
    assert r["returning"] is True and r["caller_name"] == "Maria"

    j = client.get(f"/api/brief/{ALICE}").json()
    assert j["caller"]["phone"] == ALICE and len(j["summaries"]) == 1
    assert "transcript" not in j["summaries"][0]
    assert j["caller"]["profile"]["preferred_name"] == "Maria"

    h = client.get("/brief/480-555-0142")           # any format resolves
    assert h.status_code == 200 and "text/html" in h.headers["content-type"]
    assert "Maria" in h.text and "Emergency / tooth pain" in h.text

    assert client.get("/brief/+15555550000").status_code == 404
    assert client.get("/api/brief/nonsense").status_code == 404


def test_call_end_without_caller_still_logs(client):
    r = client.post("/api/call/end", json={"transcript": "Caller: hi", "outcome": "demo"}).json()
    assert r == {"ok": True, "summary": None}


def test_office_lists_recent_callers(client):
    client.post("/api/call/start", json={"caller": ALICE})
    client.post("/api/call/end", json={"caller": ALICE, "transcript": FIXTURE_TRANSCRIPT})
    d = client.get("/api/office").json()
    assert d["callers"][0]["phone"] == ALICE
    assert d["callers"][0]["name"] == "Maria Lopez"
    assert client.get("/office").status_code == 200


def test_leads_endpoint_is_cursor_based(client):
    assert client.get("/api/leads").json() == {"business": "desert-smiles-dental", "leads": [],
                                               "cursor": 0, "count": 0}
    client.post("/api/call/end", json={"caller": ALICE, "transcript": FIXTURE_TRANSCRIPT})
    client.post("/api/call/end", json={"caller": BOB, "history": [
        {"role": "user", "content": "Hi, Bob here, how much is a cleaning?"}]})
    d = client.get("/api/leads").json()
    assert d["count"] == 2 and [l["phone"] for l in d["leads"]] == [ALICE, BOB]
    d2 = client.get(f"/api/leads?since={d['cursor']}").json()
    assert d2["count"] == 0 and d2["cursor"] == d["cursor"]
    d3 = client.get(f"/api/leads?since={d['leads'][0]['summary_id']}").json()
    assert [l["phone"] for l in d3["leads"]] == [BOB]


def test_turn_passes_caller_to_brain(client, fake_llm, biz):
    from app import callers
    callers.touch_caller(biz, ALICE)
    callers.record_call(biz, ALICE, transcript=FIXTURE_TRANSCRIPT)
    r = client.post("/api/turn", json={"history": [{"role": "user", "content": "hi"}],
                                       "caller": ALICE}).json()
    assert r["reply"] == "Okay." and r["trace"] == []
    assert "RETURNING CALLER" in fake_llm.calls[-1]["system"]
