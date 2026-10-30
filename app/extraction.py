"""Post-call extraction — one Claude pass over the transcript, or a
deterministic fallback when Claude is unavailable (no `claude` CLI and no
explicit API key) or the call fails.

Both paths return the same shape so callers.record_call never cares which
ran:

    {name, summary, intent, pain_points[], objections[], requested_service,
     next_step, interest (low|medium|high), profile{}}

Nothing here leaves the machine except the transcript sent to Claude (the
subscription CLI by default, app/llm.py) — the same boundary the brain
already crosses.
"""
import json
import re

from . import llm

INTERESTS = ("low", "medium", "high")
INTENTS = ("book_appointment", "question", "billing", "emergency", "message", "other")
KEYS = ("name", "summary", "intent", "pain_points", "objections",
        "requested_service", "next_step", "interest", "profile")


def extract(transcript, biz, appointment=None, message=None):
    """Returns (fields, extracted_by). Never raises; degrades to the fallback."""
    if llm.available():
        try:
            return _coerce(_extract_claude(transcript, biz), biz, appointment, message), "claude"
        except Exception as e:  # CLI exit / timeout / parse — the call is already over
            print(f"[extraction] claude pass failed, using fallback: {e!r}")
    return _coerce(extract_fallback(transcript, biz, appointment, message), biz,
                   appointment, message), "fallback"


# ---------------------------------------------------------------- Claude ----

def _extract_claude(transcript, biz):
    types = ", ".join(t["name"] for t in biz.get("appointment_types", []))
    system = f"""You extract structured notes from a finished phone call to {biz['name']}
({biz['industry']} office). Reply with ONE JSON object and nothing else, keys:
name (caller's name as stated, else null), summary (<= 2 sentences, third person),
intent (one of {list(INTENTS)}), pain_points (list of short strings the caller
complained about or is worried about), objections (list of short strings — hesitations,
price/insurance/time concerns), requested_service (one of: {types}; or null),
next_step (one short sentence: what happens next for this caller),
interest (low|medium|high — likelihood they become/stay a customer),
profile (object of things the caller volunteered that would help next time:
preferred_name, address, language, pace, anything else; omit unknowns).
Only use facts stated in the transcript. Never invent."""
    text = llm.complete(system, f"TRANSCRIPT:\n{transcript}\n\nJSON:", max_tokens=600, timeout=120)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON in extraction reply: {text[:120]!r}")
    return json.loads(m.group(0))


# -------------------------------------------------------------- fallback ----

_NAME_RE = re.compile(
    r"\b(?:my name is|my name's|this is|it's|it is|i'm|i am)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b")
_ADDR_RE = re.compile(
    r"\b(\d{2,6}\s+(?:[NSEW]\.?\s+)?[A-Za-z0-9.'-]+(?:\s+[A-Za-z0-9.'-]+){0,3}\s+"
    r"(?:St|Street|Ave|Avenue|Blvd|Boulevard|Rd|Road|Dr|Drive|Ln|Lane|Way|Ct|Court|Pl|Place|Cir|Circle)\b\.?"
    r"(?:,?\s+(?:Apt|Suite|Unit|#)\s*\w+)?)", re.I)
_PAIN = ("pain", "hurt", "hurts", "ache", "broke", "broken", "cracked", "chipped", "swollen",
         "bleeding", "sensitive", "emergency", "urgent", "asap", "as soon as", "knocked out",
         "infection", "leak", "leaking", "not working", "stopped working", "no hot water",
         "clogged", "flooded", "problem", "issue", "worried", "frustrated")
_OBJECT = ("too expensive", "expensive", "can't afford", "cannot afford", "cheaper",
           "not sure", "think about it", "let me think", "call back later", "call you back",
           "don't have insurance", "no insurance", "ahcccs", "medicaid", "busy", "no time",
           "not today", "maybe later", "another place", "somewhere else", "get back to you",
           "how much", "what does it cost", "price", "cost")
_BILLING = ("bill", "billing", "refund", "charge", "charged", "invoice", "overcharged")
_BOOK = ("appointment", "book", "schedule", "come in", "get in", "come by", "see the dentist",
         "see someone", "looked at", "look at it", "earliest", "opening", "available", "slot",
         "visit", "this week", "today", "tomorrow", "get seen")
_CALLBACK = ("call back", "call you back", "call again", "think about it", "let me think",
             "get back to you", "talk to my", "check my schedule")
_911 = ("911", "ambulance", "can't breathe", "cannot breathe", "unconscious", "passed out")
_HUMAN = ("real person", "human", "speak to someone", "talk to someone", "manager")
_LANG = {"spanish": "prefers Spanish", "español": "prefers Spanish", "hard of hearing": "hard of hearing",
         "slower": "asked for a slower pace", "slow down": "asked for a slower pace",
         "speak up": "asked for louder/clearer speech", "repeat that": "asked to have things repeated"}


def caller_lines(transcript):
    out = []
    for line in (transcript or "").splitlines():
        who, sep, said = line.partition(":")
        if sep and who.strip().lower() in ("caller", "user", "customer"):
            out.append(said.strip())
    return out


def _sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _clip(s, n):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def extract_fallback(transcript, biz, appointment=None, message=None):
    lines = caller_lines(transcript)
    said = " ".join(lines)
    low = said.lower()

    name = None
    for line in lines:
        m = _NAME_RE.search(line)
        if m:
            name = m.group(1)
            break

    service = None
    for t in biz.get("appointment_types", []):
        words = [w for w in re.split(r"[^a-z]+", t["name"].lower()) if len(w) > 3]
        if any(w in low for w in words):
            service = t["name"]
            break
    if appointment and appointment.get("type"):
        service = appointment["type"]

    wants_visit = bool(appointment or service or any(k in low for k in _BOOK))
    callback = any(k in low for k in _CALLBACK)
    if any(k in low for k in _BILLING):
        intent = "billing"
    elif any(k in low for k in _911):
        intent = "emergency"
    elif wants_visit:
        intent = "book_appointment"
    elif any(k in low for k in _HUMAN):
        intent = "message"
    elif "?" in said:
        intent = "question"
    else:
        intent = "other"

    pain = [_clip(s, 140) for s in _sentences(said) if any(k in s.lower() for k in _PAIN)]
    objections = [_clip(s, 140) for s in _sentences(said)
                  if any(k in s.lower() for k in _OBJECT + _CALLBACK)]

    if appointment:
        next_step = (f"Appointment booked: {appointment.get('type') or 'visit'} "
                     f"on {appointment.get('start')} (APT-{appointment['id']:04d})")
        interest = "high"
    elif message:
        next_step = f"Office to call back: {_clip(message.get('reason'), 80)}"
        interest = "high" if message.get("urgent") else "medium"
    elif callback and intent == "book_appointment":
        next_step = ("Caller said they'd think it over / call back — nothing booked; "
                     "offer times if they don't call within a day or two")
        interest = "medium"
    elif intent == "book_appointment":
        next_step = "Wanted to book but nothing booked yet — offer times on next contact"
        interest = "high" if not objections else "medium"
    elif intent in ("question", "billing", "emergency", "message"):
        next_step = "Answered on the call; follow up if they call again"
        interest = "medium"
    else:
        next_step = "No action requested"
        interest = "low"

    first = _clip(lines[0], 110) if lines else "no caller speech captured"
    about = service or intent.replace("_", " ")
    summary = f"{name or 'Caller'} called about {about}: \"{first}\""

    profile = {}
    if name:
        profile["preferred_name"] = name.split()[0]
    m = _ADDR_RE.search(said)
    if m:
        profile["address"] = m.group(1).strip()
    notes = [v for k, v in _LANG.items() if k in low]
    if notes:
        profile["notes"] = "; ".join(sorted(set(notes)))

    return {"name": name, "summary": summary, "intent": intent, "pain_points": pain[:5],
            "objections": objections[:5], "requested_service": service,
            "next_step": next_step, "interest": interest, "profile": profile}


# --------------------------------------------------------------- shaping ----

def _coerce(d, biz, appointment=None, message=None):
    """Force the extractor's output into the documented shape."""
    d = dict(d or {})
    out = {}
    out["name"] = _clip(d.get("name") or "", 60) or None
    out["summary"] = _clip(d.get("summary") or "", 400)
    intent = str(d.get("intent") or "other").lower().replace(" ", "_")
    out["intent"] = intent if intent in INTENTS else "other"
    for k in ("pain_points", "objections"):
        v = d.get(k) or []
        if isinstance(v, str):
            v = [v]
        out[k] = [_clip(str(x), 160) for x in v if str(x).strip()][:8]
    out["requested_service"] = _clip(d.get("requested_service") or "", 80) or None
    if appointment and not out["requested_service"]:
        out["requested_service"] = appointment.get("type")
    out["next_step"] = _clip(d.get("next_step") or "", 200)
    if appointment and "APT-" not in out["next_step"]:
        out["next_step"] = (f"Appointment booked: {appointment.get('type') or 'visit'} on "
                            f"{appointment.get('start')} (APT-{appointment['id']:04d}). "
                            + out["next_step"]).strip()
    interest = str(d.get("interest") or "medium").lower()
    out["interest"] = interest if interest in INTERESTS else "medium"
    if appointment:
        out["interest"] = "high"
    prof = d.get("profile") or {}
    out["profile"] = {str(k): _clip(str(v), 160) for k, v in prof.items()
                      if isinstance(prof, dict) and v not in (None, "", [], {})}
    return out
