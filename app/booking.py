"""Booking engine — real availability, real appointments, SQLite.

Slots are derived from the business's hours in its YAML config; appointments
and messages persist in data/<business_id>.db. This is the part that stays
identical when the voice layer upgrades from browser demo to Twilio.
"""
import datetime as dt
import sqlite3
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
SLOT_MINUTES = 30  # granularity of the schedule grid


def conn(business_id):
    DATA.mkdir(exist_ok=True)
    c = sqlite3.connect(str(DATA / f"{business_id}.db"))
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS appointments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        start TEXT NOT NULL,           -- ISO local datetime
        minutes INTEGER NOT NULL,
        type TEXT, name TEXT, phone TEXT, notes TEXT,
        created TEXT NOT NULL,
        cancelled INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, phone TEXT, reason TEXT, urgent INTEGER DEFAULT 0,
        created TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS call_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started TEXT NOT NULL, transcript TEXT, outcome TEXT
    );
    -- per-caller memory (see CALLER-MEMORY.md); keyed by E.164 caller ID
    CREATE TABLE IF NOT EXISTS callers (
        phone TEXT PRIMARY KEY,        -- E.164, e.g. +14805550142
        name TEXT,
        first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
        call_count INTEGER NOT NULL DEFAULT 0,
        profile TEXT NOT NULL DEFAULT '{}'   -- JSON: preferred_name, address, notes...
    );
    CREATE TABLE IF NOT EXISTS call_summaries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT NOT NULL REFERENCES callers(phone),
        started TEXT NOT NULL, ended TEXT NOT NULL,
        transcript TEXT, summary TEXT, intent TEXT,
        pain_points TEXT, objections TEXT,   -- JSON lists
        requested_service TEXT, next_step TEXT,
        interest TEXT,                       -- low | medium | high
        appointment_id INTEGER,              -- appointments.id booked on this call
        extracted_by TEXT                    -- claude | fallback
    );
    CREATE INDEX IF NOT EXISTS call_summaries_phone ON call_summaries(phone, id);
    """)
    return c


def _day_window(biz, day):
    """(open, close) datetimes for a date, or None if closed."""
    spec = biz["hours"].get(day.strftime("%A").lower(), "closed")
    if not spec or str(spec).lower() == "closed":
        return None
    o, _, c = str(spec).partition("-")
    oh, om = (list(map(int, o.split(":"))) + [0])[:2]
    ch, cm = (list(map(int, c.split(":"))) + [0])[:2]
    return (dt.datetime.combine(day, dt.time(oh, om)),
            dt.datetime.combine(day, dt.time(ch, cm)))


def free_slots(biz, day, minutes, now=None):
    """Open start-times on `day` for an appointment of `minutes`."""
    win = _day_window(biz, day)
    if not win:
        return []
    open_t, close_t = win
    now = now or dt.datetime.now()
    c = conn(biz["id"])
    busy = [(dt.datetime.fromisoformat(r["start"]),
             dt.datetime.fromisoformat(r["start"]) + dt.timedelta(minutes=r["minutes"]))
            for r in c.execute(
                "SELECT start, minutes FROM appointments "
                "WHERE cancelled=0 AND date(start)=?", (day.isoformat(),))]
    c.close()
    out, t = [], open_t
    step = dt.timedelta(minutes=SLOT_MINUTES)
    need = dt.timedelta(minutes=minutes)
    while t + need <= close_t:
        if t > now and not any(b0 < t + need and t < b1 for b0, b1 in busy):
            out.append(t)
        t += step
    return out


def existing_booking(biz, phone, day):
    """An already-booked appointment for this phone number on this date, or None."""
    c = conn(biz["id"])
    r = c.execute("SELECT id, start, type FROM appointments "
                  "WHERE cancelled=0 AND phone=? AND date(start)=?",
                  (phone, day.isoformat())).fetchone()
    c.close()
    return dict(r) if r else None


def book(biz, start_iso, minutes, appt_type, name, phone, notes=""):
    start = dt.datetime.fromisoformat(start_iso)
    dup = existing_booking(biz, phone, start.date())
    if dup:
        return ("already_booked", dup)
    if start.strftime("%H:%M") not in [
            s.strftime("%H:%M") for s in free_slots(biz, start.date(), minutes)]:
        return None  # slot taken or outside hours
    c = conn(biz["id"])
    cur = c.execute(
        "INSERT INTO appointments (start, minutes, type, name, phone, notes, created) "
        "VALUES (?,?,?,?,?,?,?)",
        (start.isoformat(timespec="minutes"), minutes, appt_type, name, phone,
         notes, dt.datetime.now().isoformat(timespec="seconds")))
    c.commit()
    appt_id = cur.lastrowid
    c.close()
    return appt_id


def take_message(biz, name, phone, reason, urgent=False):
    c = conn(biz["id"])
    c.execute("INSERT INTO messages (name, phone, reason, urgent, created) "
              "VALUES (?,?,?,?,?)",
              (name, phone, reason, int(urgent),
               dt.datetime.now().isoformat(timespec="seconds")))
    c.commit()
    c.close()
    return True


def log_call(biz, transcript, outcome):
    c = conn(biz["id"])
    c.execute("INSERT INTO call_log (started, transcript, outcome) VALUES (?,?,?)",
              (dt.datetime.now().isoformat(timespec="seconds"), transcript, outcome))
    c.commit()
    c.close()


def upcoming(biz, limit=50):
    c = conn(biz["id"])
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM appointments WHERE cancelled=0 AND start >= ? "
        "ORDER BY start LIMIT ?",
        (dt.datetime.now().isoformat(timespec="minutes"), limit))]
    msgs = [dict(r) for r in c.execute(
        "SELECT * FROM messages ORDER BY id DESC LIMIT 20")]
    c.close()
    return rows, msgs
