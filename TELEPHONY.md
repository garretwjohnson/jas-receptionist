# Phase 2 — Putting the receptionist on a real phone number

The brain (`app/brain.py`) and booking engine (`app/booking.py`) are
transport-agnostic — nothing changes in them when we move from the browser
demo to a real phone line. This doc is the upgrade path.

## What Garret needs to sign up for

| Service | What for | Cost |
|---|---|---|
| Twilio | Phone number + call audio streams | ~$1.15/mo per number + $0.0085/min |
| Deepgram | Speech-to-text (caller audio → text) | $0.0059/min streaming (Nova-3) |
| ElevenLabs or Cartesia | Text-to-speech (Ivy's voice) | ~$0.03–0.08/min (Cartesia is the cheap one) |

All-in per-minute cost incl. Claude Haiku: **~$0.06–0.10/min**.
A client doing 400 min/mo costs us ~$25–40. At $199/mo flat that is an
80%+ gross margin per client.

## Architecture

```
Caller dials the client's AI number
   │
Twilio  ──(Media Streams WebSocket, 8kHz mulaw audio)──►  our server
   │                                                        │
   │                                            Deepgram streaming STT
   │                                                        │ text
   │                                            brain.reply()  ← same brain as demo
   │                                                        │ text
   │                                            TTS (Cartesia/ElevenLabs stream)
   ◄──(audio back over the same WebSocket)──────────────────┘
```

Orchestration: use **Pipecat** (open-source, pip install pipecat-ai) —
it handles the hard real-time parts (barge-in/interruption, end-of-speech
detection, latency pipelining). Our brain plugs in as the LLM step; keep
the tool-calling loop exactly as-is.

## Build steps (est. 1–2 days once keys exist)

1. Twilio: buy a local AZ number, point its Voice webhook at
   `POST /twilio/incoming` (returns TwiML `<Connect><Stream url="wss://..."/>`).
2. Expose the server publicly: `cloudflared tunnel` for testing
   (same recipe as the SPY 0DTE agent DEPLOY.md), a $5 VPS or Fly.io for production.
3. Add `app/telephony.py`: Pipecat pipeline wiring
   DeepgramSTTService + our brain + CartesiaTTSService + TwilioFrameSerializer.
4. SMS confirmations: after book_appointment succeeds, send the caller a
   text via Twilio SMS (`sms_confirmations: true` in the business YAML).
5. Per-client: buy one number per client (or port/forward their existing
   number's overflow — "if not answered in 4 rings, forward to AI" is the
   easiest sell: the AI only catches calls they were missing anyway).

## Caller memory hooks (built 9/15 — see CALLER-MEMORY.md)

The brain is caller-aware now. When wiring Twilio, call these from the
telephony layer; the browser demo already does:

| When | Call | Why |
|---|---|---|
| Call connects | `POST /api/call/start {caller: <From number>}` | upserts the caller (call_count += 1); response says `returning: true/false` + `caller_name` |
| Every turn | `POST /api/turn {history, caller}` (or `brain.reply(biz, history, caller=...)`) | injects the ≤600-char returning-caller block into the system prompt |
| Call ends | `POST /api/call/end {caller, transcript \| history, started: <ISO local>}` | logs the call, runs post-call extraction, writes `call_summaries`, merges the profile |

`caller` is Twilio's `From` (already E.164). `started` lets the summary link
the appointment/message created during that call; omit it and the last hour
is searched.

## Lead export — what jas-ops polls

`GET /api/leads?since=<cursor>&limit=100&business=<id>` — read-only, no auth
(same as /office; localhost only). Returns new call summaries, oldest first:

```json
{"business": "desert-smiles-dental", "count": 1, "cursor": 17,
 "leads": [{
   "source_id": "jas-receptionist:desert-smiles-dental:call:17",   // stable, unique -> leads.source_id
   "source": "jas-receptionist", "business_id": "desert-smiles-dental",
   "summary_id": 17, "name": "Maria Lopez", "phone": "+14805550142", "call_count": 2,
   "started": "2026-09-15T11:44:43", "ended": "2026-09-15T11:49:02",
   "intent": "book_appointment",            // book_appointment|question|billing|emergency|message|other
   "requested_service": "Emergency / tooth pain",
   "interest": "high",                      // low|medium|high
   "next_step": "Appointment booked: ... (APT-0007)",
   "pain_points": ["cracked a tooth last night, hurts"],
   "objections": ["no insurance, worried about price"],
   "appointment_id": 7,                     // appointments.id in this receptionist's DB, or null
   "message": "<the 1-2 sentence summary>", // -> leads.message
   "profile": {"preferred_name": "Maria", "address": "1234 E Main St, Mesa"},
   "extracted_by": "claude"                 // or "fallback" (deterministic, no key)
 }]}
```

Poller contract: keep `cursor` (max `summary_id` seen) and pass it back as
`since`; an empty `leads` list with an unchanged `cursor` means nothing new.
`source_id` maps onto jas-ops' `leads.source_id UNIQUE`, so re-polling is
idempotent. Transcripts are deliberately NOT exported — they stay in the
receptionist DB; open `/brief/<phone>` for the full picture. jas-ops is not
modified by this build; wiring the poller is its job.

## Env vars (when ready)

```
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_NUMBER=
DEEPGRAM_API_KEY=
CARTESIA_API_KEY=        # or ELEVENLABS_API_KEY
ANTHROPIC_API_KEY=       # OPTIONAL — only if Garret decides on the SDK path; default is the
                         # subscription via `claude -p` (see README "Claude on the subscription")
JAS_MODEL=claude-haiku-4-5-20251001
JAS_CLAUDE_TIMEOUT=60
```

## Why not just use Retell/Vapi?

They charge $0.07–0.30/min for what the stack above does at $0.06–0.10.
At 1 client the difference is noise; at 20 clients doing 400 min/mo it's
~$400–1,600/mo of margin. The browser demo works TODAY with zero new
accounts, so the platform decision can wait until the first paying client.
If speed-to-first-client matters more than margin, Retell pay-as-you-go
is still a fine phase-2 shortcut — the business YAML + prompts port over.
