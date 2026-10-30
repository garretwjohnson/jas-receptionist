# Caller memory — per-caller profile, post-call extraction, pre-meeting brief

Built 9/15/2026 from cfongai triage item 4 (`~/Johnson AI Solutions/research/cfongai-TRIAGE.md`).
The receptionist now remembers who called, what they wanted, and what happens
next — keyed by the caller's phone number — and hands that back to Ivy on the
next call and to staff as a one-screen brief.

## What was built

| Piece | Where | Notes |
|---|---|---|
| `callers` table | `app/booking.py` schema | E.164 `phone` PK, `name`, `first_seen`, `last_seen`, `call_count`, JSON `profile` (preferred_name, address, language/pace notes, anything volunteered). Lives in the same `data/<business>.db` as the calendar, so memory is scoped per business. `CREATE IF NOT EXISTS` — existing DBs gain the tables on first open. |
| `call_summaries` table | same | one row per finished call: `transcript`, `summary`, `intent`, `pain_points[]`, `objections[]`, `requested_service`, `next_step`, `interest` (low/medium/high), `appointment_id`, `extracted_by` (claude/fallback). FK → `callers.phone`. |
| Caller memory module | `app/callers.py` | `normalize_phone`, `touch_caller` (call start), `context_block` (≤600 chars), `record_call` (post-call), `brief`, `brief_html`, `recent` (office card), `new_leads` (export). |
| Post-call extraction | `app/extraction.py` | One Claude Haiku pass via `app/llm.py` (subscription CLI; JSON-only reply) → `_coerce` into the fixed shape. **Deterministic fallback** when Claude is unavailable or the call fails: regex name/address, appointment-type keyword match, intent/interest rules, pain/objection sentence picking. Same output shape either way. |
| Prompt injection | `app/brain.py` | `system_prompt(biz, caller_context)` appends a `CALLER CONTEXT` section; `reply(business, history, caller=...)` builds it from `callers.context_block`. New/unknown/anonymous numbers get nothing. On the CLI path tools are a text protocol (`_reply_cli`): one-line JSON tool call → `TOOL RESULT` line → continue. |
| Claude access | `app/llm.py` | `claude -p` on the subscription by default (CLAUDE* env stripped, `--tools ""`, `--setting-sources user`, MCP servers disabled, stdin closed, one retry on non-timeout failure); SDK only with an explicit `ANTHROPIC_API_KEY`. |
| HTTP | `app/main.py` | `POST /api/call/start`, `POST /api/call/end` (now returns the summary), `GET /brief/<phone>` (HTML), `GET /api/brief/<phone>` (JSON), `GET /api/leads?since=` (export), `/api/office` now includes `callers`. |
| UI | `app/static/index.html`, `office.html` | demo phone has a Caller ID field (remembered in localStorage) and shows "Returning caller" + the post-call notes; office view has a "Recent callers" card with interest pill, next step, and a Brief link. |
| Tests | `tests/` (38) | new vs returning prompt injection (unit + through `/api/turn` with a fake Claude), extraction on a fixture transcript (fallback, fake-Claude JSON coercion, failure → fallback), brief rendering + HTML escaping, per-number scoping, leads cursor export, call lifecycle over HTTP, schema migration. |

Docs: `TELEPHONY.md` has the hook table for the Twilio layer and the exact
`/api/leads` shape jas-ops should poll. jas-ops itself was not modified.

## The returning-caller block

Example (599 chars, deterministic fallback, generated 9/15):

```
RETURNING CALLER (caller ID +14805550199): Dana. Last called Sep 15, 2026 (2 calls on
record), asked about Emergency / tooth pain. Next step was: Caller said they'd think it
over / call back — nothing booked; offer times if they don't call within a day or two.
Hesitation: I chipped a front tooth and want to get it looked at this week, but I'm
nervous about the cost sinc… Summary: Dana Reyes called about Emergency / tooth pain:
"Hi, this is Dana Reyes. I chipped a front tooth and want to get… Greet them by name,
don't re-ask what you already know (confirm it), and pick up where they left off.
```

Budgeting: name, last-call date, next step, profile notes and one hesitation
are placed first; the summary takes whatever is left of the 600-char cap.
A pathological profile is hard-truncated; an assertion guards the cap.

## Privacy

- Nothing leaves the machine except the prompt sent to Claude (the subscription
  CLI by default) for the brain and for extraction — the same boundary the
  brain already crossed. With no `claude` binary and no key, nothing leaves at all.
- No calls are placed, no SMS sent, nothing contacts anyone. `/api/leads` is a
  pull endpoint; jas-ops has to come and get it.
- `/brief`, `/api/brief`, `/api/leads` sit behind exactly the auth `/office`
  has, which is **none** — the server binds to 127.0.0.1 and that is the
  perimeter. Before this server is exposed (cloudflared tunnel, VPS), put a
  shared-secret header or basic auth on all four office-side routes together.
- Transcripts are stored in `call_summaries` but never exported via
  `/api/leads` or `/api/brief`; the brief page renders summaries only.
- Anonymous / blocked caller ID (`normalize_phone` → None) is never stored;
  the call is still logged to `call_log` as before.

## Claude access and measured latency (9/15)

No API key. `app/llm.py` runs every call through `claude -p` on Garret's
subscription (README "Claude on the subscription"); the SDK path exists only
for an explicit `ANTHROPIC_API_KEY`. Live run through `/api/turn` and
`/api/call/end` on a scratch DB, Haiku, this machine (VERIFIED):

| Call | Wall time | What happened |
|---|---|---|
| Brain turn 1 ("do you take Aetna, cleaning next Tuesday morning?") | **11.5 s** | model emitted a `check_availability` tool call, we ran it, second CLI pass produced the spoken reply with real slots — two CLI round-trips |
| Brain turn 2 ("Tuesday at 9, please book it") | **10.6 s** | `book_appointment` → APT-0001 in the calendar, correct date math (Sep 22) |
| Post-call extraction | **16.3 s** | real JSON: name, intent, service, next step, `interest: high`, profile {insurance: Aetna, preferred_time: morning}, linked `appointment_id: 1`, `extracted_by: claude` |

A single text-only haiku call is ~4 s; a receptionist turn is 2 calls when
a tool is needed. Before the `--strict-mcp-config` override the same call
took **57 s** — the CLI was booting every user-level MCP server on each
spawn. The user-level hooks (ruflo) cost nothing measurable.

**Trade-off for the voice demo:** 10–12 s of silence per turn is fine for a
typed/browser demo (the phone UI shows "Thinking… Ns") and unusable on a
real phone line. A live deployment needs either the SDK path with a
dedicated key (sub-second first token, native tool use — same brain, set
`ANTHROPIC_API_KEY`) or a faster path. That decision is Garret's, later.

## Not verified live

- Ivy *using* the returning-caller block on a second call from the same
  number (greeting by name, resuming the next step). The block is injected
  (tested); a live second call through the CLI is a 1-minute manual check on
  the demo page — enter the same Caller ID twice.
- SDK path against a real key (fake client only): nothing is configured and
  that is by decision.
- Twilio phase 2: `From` arriving as E.164 and the call/start → turn →
  call/end hooks firing from the Pipecat pipeline.

## Known limits / next

- Fallback extraction is keyword-driven and dental-flavoured (`_PAIN`,
  `_OBJECT` lists); it is a floor, not a substitute for the Claude pass.
  It will mis-read sarcasm, negation ("not in pain"), and non-English calls.
- `name` from extraction overwrites the stored name whenever non-empty; a
  hallucinated name would stick until the next call corrects it.
- `record_call` searches appointments created after `started` for a phone
  matching the caller ID; a caller who books under a *different* number
  (booking for a spouse) won't be linked.
- One profile per number: a shared household line merges people.
- Phone normalisation is US-centric (10 digits → +1).
