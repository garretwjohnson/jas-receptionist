"""Per-caller memory — who called, what they wanted, what happens next.

Keyed by the caller's phone number in E.164. Everything lives in the same
per-business SQLite file as the calendar (data/<business_id>.db), so a
caller's history is scoped to the business they called and never leaves
the machine. See CALLER-MEMORY.md.

  touch_caller     upsert on call start (call_count += 1)
  context_block    <= 600-char "returning caller" block for the system prompt
  record_call      post-call: extraction -> call_summaries + profile merge
  brief            profile + last three summaries (the pre-meeting brief)
  new_leads        cursor-based export jas-ops can poll
"""
import datetime as dt
import json
import re

from . import booking, extraction

CONTEXT_CAP = 600


def normalize_phone(raw):
    """E.164 or None. US-centric: 10 digits -> +1XXXXXXXXXX."""
    if not raw:
        return None
    s = str(raw).strip()
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    if s.startswith("+"):
        return "+" + digits if 8 <= len(digits) <= 15 else None
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits[0] == "1":
        return "+" + digits
    return None


def _now():
    return dt.datetime.now().isoformat(timespec="seconds")


def _row(r):
    if not r:
        return None
    d = dict(r)
    for k in ("profile", "pain_points", "objections"):
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                d[k] = {} if k == "profile" else []
    return d


def get_caller(biz, phone):
    phone = normalize_phone(phone)
    if not phone:
        return None
    c = booking.conn(biz["id"])
    r = c.execute("SELECT * FROM callers WHERE phone=?", (phone,)).fetchone()
    c.close()
    return _row(r)


def touch_caller(biz, phone, name=None):
    """Mark a call from `phone` as started; returns the caller row (or None if
    the number can't be normalised — anonymous / blocked caller ID)."""
    phone = normalize_phone(phone)
    if not phone:
        return None
    now = _now()
    c = booking.conn(biz["id"])
    c.execute("""INSERT INTO callers (phone, name, first_seen, last_seen, call_count)
                 VALUES (?,?,?,?,1)
                 ON CONFLICT(phone) DO UPDATE SET
                   last_seen=excluded.last_seen,
                   call_count=call_count+1,
                   name=COALESCE(excluded.name, name)""", (phone, name, now, now))
    c.commit()
    r = c.execute("SELECT * FROM callers WHERE phone=?", (phone,)).fetchone()
    c.close()
    return _row(r)


def update_profile(biz, phone, patch, name=None):
    phone = normalize_phone(phone)
    if not phone:
        return None
    c = booking.conn(biz["id"])
    r = c.execute("SELECT profile, name FROM callers WHERE phone=?", (phone,)).fetchone()
    if not r:
        c.close()
        return None
    prof = _row(r)["profile"] or {}
    prof.update({k: v for k, v in (patch or {}).items() if v not in (None, "", [], {})})
    c.execute("UPDATE callers SET profile=?, name=COALESCE(?, name) WHERE phone=?",
              (json.dumps(prof), name or None, phone))
    c.commit()
    c.close()
    return prof


def summaries(biz, phone, limit=3):
    phone = normalize_phone(phone)
    if not phone:
        return []
    c = booking.conn(biz["id"])
    rows = [_row(r) for r in c.execute(
        "SELECT * FROM call_summaries WHERE phone=? ORDER BY id DESC LIMIT ?", (phone, limit))]
    c.close()
    return rows


def _when(iso):
    try:
        return dt.datetime.fromisoformat(iso).strftime("%b %-d, %Y")
    except (TypeError, ValueError):
        return iso or "unknown date"


def _clipped(text, n):
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def context_block(biz, phone):
    """Short returning-caller block for the brain's system prompt, or "" for a
    new/unknown number. Hard-capped at CONTEXT_CAP characters: the fixed facts
    (name, last call, next step, profile notes) always fit; the free-text
    summary takes whatever budget is left."""
    caller = get_caller(biz, phone)
    if not caller:
        return ""
    last = summaries(biz, phone, limit=1)
    if not last and caller["call_count"] <= 1:
        return ""  # first call, nothing learned yet
    prof = caller.get("profile") or {}
    name = prof.get("preferred_name") or caller.get("name") or "name unknown"
    parts = [f"RETURNING CALLER (caller ID {caller['phone']}): {name}."]
    s = last[0] if last else {}
    if s:
        asked = s.get("requested_service") or s.get("intent") or "unknown"
        parts.append(f"Last called {_when(s['started'])} ({caller['call_count']} call"
                     f"{'s' if caller['call_count'] != 1 else ''} on record), asked about {asked}.")
        if s.get("next_step"):
            parts.append(f"Next step was: {s['next_step'].rstrip('.')}.")
    extras = [f"{k.replace('_', ' ')}: {v}" for k, v in prof.items() if k != "preferred_name"]
    if extras:
        parts.append("Notes: " + "; ".join(extras) + ".")
    if s.get("objections"):
        hes = _clipped(s["objections"][0].rstrip("."), 100)
        parts.append("Hesitation: " + hes + ("" if hes.endswith("…") else "."))
    closing = ("Greet them by name, don't re-ask what you already know (confirm it), "
               "and pick up where they left off.")
    fixed = " ".join(parts + [closing])
    if len(fixed) > CONTEXT_CAP:                      # oversized profile: trim from the end
        return fixed[:CONTEXT_CAP - 1].rstrip() + "…"
    room = CONTEXT_CAP - len(fixed) - len(" Summary: ")
    if s.get("summary") and room >= 40:
        summ = s["summary"]
        if len(summ) > room:
            summ = summ[:room - 1].rstrip() + "…"
        parts.append(f"Summary: {summ}")
    text = " ".join(parts + [closing])
    assert len(text) <= CONTEXT_CAP
    return text


def _appointment_on_call(biz, phone, started):
    """Appointment created for this caller during the call, if any."""
    c = booking.conn(biz["id"])
    rows = [dict(r) for r in c.execute(
        "SELECT id, start, type, phone FROM appointments "
        "WHERE cancelled=0 AND created >= ? ORDER BY id DESC", (started,))]
    c.close()
    for r in rows:
        if normalize_phone(r.get("phone")) == phone:
            return r
    return None


def _message_on_call(biz, phone, started):
    c = booking.conn(biz["id"])
    rows = [dict(r) for r in c.execute(
        "SELECT id, reason, urgent, phone FROM messages WHERE created >= ? ORDER BY id DESC",
        (started,))]
    c.close()
    for r in rows:
        if normalize_phone(r.get("phone")) == phone:
            return r
    return None


def transcript_from_history(history):
    return "\n".join(("Caller: " if h.get("role") == "user" else "Ivy: ") + str(h.get("content", ""))
                     for h in history or [])


def record_call(biz, phone, transcript=None, history=None, started=None):
    """Post-call extraction. Writes one call_summaries row, merges what the
    caller volunteered into their profile. Returns the summary dict, or None
    when the number can't be normalised (still logged by booking.log_call)."""
    phone = normalize_phone(phone)
    if not phone:
        return None
    if not transcript:
        transcript = transcript_from_history(history)
    started = started or (dt.datetime.now() - dt.timedelta(hours=1)).isoformat(timespec="seconds")
    if not get_caller(biz, phone):
        touch_caller(biz, phone)  # call/end without a prior turn (e.g. hung up on greeting)
    appt = _appointment_on_call(biz, phone, started)
    msg = _message_on_call(biz, phone, started)
    fields, by = extraction.extract(transcript, biz, appointment=appt, message=msg)
    ended = _now()
    c = booking.conn(biz["id"])
    cur = c.execute(
        "INSERT INTO call_summaries (phone, started, ended, transcript, summary, intent, "
        "pain_points, objections, requested_service, next_step, interest, appointment_id, "
        "extracted_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (phone, started, ended, transcript, fields["summary"], fields["intent"],
         json.dumps(fields["pain_points"]), json.dumps(fields["objections"]),
         fields["requested_service"], fields["next_step"], fields["interest"],
         appt["id"] if appt else None, by))
    c.commit()
    sid = cur.lastrowid
    c.close()
    update_profile(biz, phone, fields.get("profile"), name=fields.get("name"))
    out = dict(fields, id=sid, phone=phone, started=started, ended=ended,
               appointment_id=appt["id"] if appt else None, extracted_by=by)
    return out


def brief(biz, phone):
    """Pre-meeting brief: caller profile + last three summaries. None if unknown."""
    caller = get_caller(biz, phone)
    if not caller:
        return None
    return {"business": biz["name"], "caller": caller,
            "summaries": summaries(biz, caller["phone"], limit=3)}


def recent(biz, limit=20):
    """Callers with their latest summary, newest first — the /office card."""
    c = booking.conn(biz["id"])
    rows = [_row(r) for r in c.execute("""
        SELECT ca.phone, ca.name, ca.last_seen, ca.call_count, ca.profile,
               s.interest, s.next_step, s.requested_service
        FROM callers ca
        LEFT JOIN call_summaries s ON s.id = (
            SELECT id FROM call_summaries WHERE phone = ca.phone ORDER BY id DESC LIMIT 1)
        ORDER BY ca.last_seen DESC LIMIT ?""", (limit,))]
    c.close()
    return rows


def new_leads(biz, since_id=0, limit=100):
    """Call summaries with id > since_id, oldest first, shaped as leads for
    jas-ops. The poller keeps the cursor (max id it has seen)."""
    c = booking.conn(biz["id"])
    rows = [_row(r) for r in c.execute("""
        SELECT s.*, ca.name AS caller_name, ca.profile, ca.call_count
        FROM call_summaries s JOIN callers ca ON ca.phone = s.phone
        WHERE s.id > ? ORDER BY s.id LIMIT ?""", (since_id, limit))]
    c.close()
    leads = []
    for r in rows:
        leads.append({
            "source_id": f"jas-receptionist:{biz['id']}:call:{r['id']}",
            "source": "jas-receptionist",
            "business_id": biz["id"],
            "summary_id": r["id"],
            "name": r["caller_name"] or "Unknown caller",
            "phone": r["phone"],
            "call_count": r["call_count"],
            "started": r["started"], "ended": r["ended"],
            "intent": r["intent"], "requested_service": r["requested_service"],
            "interest": r["interest"], "next_step": r["next_step"],
            "pain_points": r["pain_points"], "objections": r["objections"],
            "appointment_id": r["appointment_id"],
            "message": r["summary"],
            "profile": r["profile"],
            "extracted_by": r["extracted_by"],
        })
    cursor = rows[-1]["id"] if rows else since_id
    return leads, cursor


# ----------------------------------------------------------------- HTML ----

def _esc(s):
    import html
    return html.escape("" if s is None else str(s))


def brief_html(b):
    """One-screen pre-meeting brief. Same look as /office."""
    ca, prof = b["caller"], b["caller"].get("profile") or {}
    name = _esc(prof.get("preferred_name") or ca.get("name") or "Unknown caller")
    prof_rows = "".join(f"<tr><th>{_esc(k.replace('_', ' '))}</th><td>{_esc(v)}</td></tr>"
                        for k, v in prof.items()) or "<tr><td class=empty>Nothing learned yet.</td></tr>"
    cards = []
    for s in b["summaries"]:
        bullets = "".join(f"<li>{_esc(x)}</li>" for x in (s.get("pain_points") or []))
        objs = "".join(f"<li>{_esc(x)}</li>" for x in (s.get("objections") or []))
        appt = (f' · <span class=pill>APT-{s["appointment_id"]:04d}</span>'
                if s.get("appointment_id") else "")
        cards.append(f"""
    <div class="card call">
      <div class="meta">{_esc(_when(s['started']))} · {_esc(s.get('intent'))} ·
        <span class="pill {_esc(s.get('interest'))}">{_esc(s.get('interest'))} interest</span>{appt}
        <span class="dim">· {_esc(s.get('extracted_by'))}</span></div>
      <p class="sum">{_esc(s.get('summary'))}</p>
      <div class="kv"><b>Asked about</b> {_esc(s.get('requested_service') or '—')}</div>
      <div class="kv"><b>Next step</b> {_esc(s.get('next_step') or '—')}</div>
      {'<div class="kv"><b>Pain points</b><ul>' + bullets + '</ul></div>' if bullets else ''}
      {'<div class="kv"><b>Objections</b><ul>' + objs + '</ul></div>' if objs else ''}
    </div>""")
    calls = "".join(cards) or '<div class="card"><div class="empty">No call summaries yet.</div></div>'
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Brief — {name} — JAS Receptionist</title>
<style>
  :root {{ --bg:#f4f6f9; --panel:#fff; --edge:#dde3ea; --ink:#1c2733; --dim:#68788c; --accent:#1f8f63; }}
  * {{ margin:0; box-sizing:border-box; }}
  body {{ background:var(--bg); color:var(--ink); font:15px/1.5 -apple-system, Segoe UI, sans-serif; padding:32px 20px; }}
  .wrap {{ max-width: 760px; margin: 0 auto; }}
  .eyebrow {{ font-size:11px; letter-spacing:.14em; text-transform:uppercase; color:var(--dim); }}
  h1 {{ font-size:24px; margin:2px 0 4px; }}
  .sub {{ color:var(--dim); margin-bottom:18px; }}
  .card {{ background:var(--panel); border:1px solid var(--edge); border-radius:14px; padding:18px 20px; margin-bottom:14px; }}
  h2 {{ font-size:15px; margin-bottom:10px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13.5px; }}
  th {{ text-align:left; color:var(--dim); font-weight:600; padding:5px 8px 5px 0; width:34%; vertical-align:top; }}
  td {{ padding:5px 0; }}
  .pill {{ display:inline-block; background:#e7f5ee; color:var(--accent); border-radius:99px; padding:1px 9px; font-size:12px; font-weight:600; }}
  .pill.low {{ background:#eef1f5; color:var(--dim); }} .pill.high {{ background:#fdeaea; color:#c33; }}
  .meta {{ font-size:12.5px; color:var(--dim); margin-bottom:6px; }}
  .sum {{ margin-bottom:8px; }}
  .kv {{ font-size:13.5px; margin-top:4px; }} .kv b {{ color:var(--dim); font-weight:600; margin-right:6px; }}
  .kv ul {{ margin:2px 0 0 18px; }}
  .empty, .dim {{ color:var(--dim); font-size:13px; }}
  a {{ color:var(--accent); text-decoration:none; font-size:13px; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="eyebrow">{_esc(b['business'])} · Pre-meeting brief</div>
  <h1>{name}</h1>
  <div class="sub">{_esc(ca['phone'])} · {ca['call_count']} call{'s' if ca['call_count'] != 1 else ''} ·
    first {_esc(_when(ca['first_seen']))} · last {_esc(_when(ca['last_seen']))} · <a href="/office">← office view</a></div>
  <div class="card"><h2>What we know</h2><table>{prof_rows}</table></div>
  <h2 style="margin:6px 0 10px">Last {len(b['summaries'])} call{'s' if len(b['summaries']) != 1 else ''}</h2>
  {calls}
</div>
</body>
</html>"""
