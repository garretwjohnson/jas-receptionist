"""JAS Receptionist — FastAPI server.

  uv run --with fastapi --with "uvicorn[standard]" --with anthropic --with pyyaml \
      uvicorn app.main:app --port 8940

  /            phone-style voice demo (Web Speech API — use Chrome)
  /office      the business's back-office view: appointments + messages + recent callers
  /brief/<phone>      one-screen pre-meeting brief for a caller (HTML)
  /api/brief/<phone>  same, as JSON
  /api/turn    one conversation turn {business, history, caller} -> {reply, trace}
  /api/office  upcoming appointments + messages + recent callers
  /api/call/start  {business, caller} -> touch caller memory, return greeting
  /api/call/end    {business, caller, transcript|history, started} -> log + post-call summary
  /api/leads   ?since=<id>  new call summaries shaped as leads (jas-ops polls this)

No auth: bind to 127.0.0.1 only (the default). The brief and leads endpoints
expose caller data and sit behind exactly the same (lack of) auth as /office —
see CALLER-MEMORY.md before exposing this server publicly.
"""
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from . import booking, brain, callers

app = FastAPI(title="JAS Receptionist")
STATIC = Path(__file__).parent / "static"
DEFAULT_BIZ = "desert-smiles-dental"


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/office")
def office():
    return FileResponse(STATIC / "office.html")


@app.post("/api/turn")
async def turn(body: dict):
    business = body.get("business", DEFAULT_BIZ)
    history = body.get("history", [])
    text, trace = brain.reply(business, history, caller=body.get("caller"))
    return {"reply": text, "trace": trace}


@app.get("/api/office")
def office_state(business: str = DEFAULT_BIZ):
    biz = brain.load_business(business)
    appts, msgs = booking.upcoming(biz)
    return {"business": biz["name"], "appointments": appts, "messages": msgs,
            "callers": callers.recent(biz)}


@app.get("/api/greeting")
def greeting(business: str = DEFAULT_BIZ):
    biz = brain.load_business(business)
    return {"greeting": biz["greeting"], "name": biz["name"]}


@app.post("/api/call/start")
async def call_start(body: dict):
    """Call connected: bump the caller's record and tell the client whether
    this number is known. Twilio phase 2 calls this from the incoming webhook."""
    biz = brain.load_business(body.get("business", DEFAULT_BIZ))
    caller = callers.touch_caller(biz, body.get("caller"))
    returning = bool(caller and (caller["call_count"] > 1))
    name = None
    if caller:
        name = (caller.get("profile") or {}).get("preferred_name") or caller.get("name")
    return {"greeting": biz["greeting"], "name": biz["name"],
            "caller": caller["phone"] if caller else None,
            "returning": returning, "caller_name": name}


@app.post("/api/call/end")
async def call_end(body: dict):
    biz = brain.load_business(body.get("business", DEFAULT_BIZ))
    transcript = body.get("transcript") or callers.transcript_from_history(body.get("history"))
    booking.log_call(biz, transcript, body.get("outcome", ""))
    summary = None
    if body.get("caller"):
        summary = callers.record_call(biz, body["caller"], transcript=transcript,
                                      started=body.get("started"))
        if summary:
            summary.pop("transcript", None)
    return {"ok": True, "summary": summary}


@app.get("/api/brief/{phone}")
def brief_json(phone: str, business: str = DEFAULT_BIZ):
    biz = brain.load_business(business)
    b = callers.brief(biz, phone)
    if not b:
        raise HTTPException(404, "unknown caller")
    for s in b["summaries"]:
        s.pop("transcript", None)
    return b


@app.get("/brief/{phone}", response_class=HTMLResponse)
def brief_page(phone: str, business: str = DEFAULT_BIZ):
    biz = brain.load_business(business)
    b = callers.brief(biz, phone)
    if not b:
        raise HTTPException(404, "unknown caller")
    return callers.brief_html(b)


@app.get("/api/leads")
def leads(since: int = 0, limit: int = 100, business: str = DEFAULT_BIZ):
    """Cursor-based export for jas-ops: pass the last `cursor` you saw as `since`."""
    biz = brain.load_business(business)
    items, cursor = callers.new_leads(biz, since_id=since, limit=min(limit, 500))
    return {"business": biz["id"], "leads": items, "cursor": cursor, "count": len(items)}
