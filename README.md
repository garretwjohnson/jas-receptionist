# JAS Receptionist

An AI phone receptionist built from scratch (no Retell or Vapi): Claude answers, books against real availability,
takes messages, escalates by rule and remembers repeat callers by caller ID. One YAML file is the whole per-client
setup. Python, FastAPI, SQLite, Claude API; tests run offline with the model stubbed.


Johnson AI Solutions' own AI receptionist — built in-house, no Retell/Vapi.
Claude is the brain; booking, messages, and call logs are real (SQLite).
The browser handles voice for demos; a Twilio phone line is phase 2
(see TELEPHONY.md).

## Run

```bash

uv run --with fastapi --with "uvicorn[standard]" --with anthropic --with pyyaml \
    uvicorn app.main:app --port 8940
```

- http://127.0.0.1:8940/ — the demo "phone". Press Call, talk (Chrome) or type.
- http://127.0.0.1:8940/office — what the client's staff sees: appointments
  the AI booked, messages it took, recent callers. Auto-refreshes; great split-screen demo.
- http://127.0.0.1:8940/brief/480-555-0142 — one-screen pre-meeting brief for a
  caller: what we know about them + their last three calls (see CALLER-MEMORY.md).

Enter a "Caller ID" number on the demo phone before pressing Call — that is
the key the receptionist remembers callers by. Call the same number twice and
Ivy greets you by name and picks up where you left off.

## Tests

```bash
uv run --with fastapi --with "uvicorn[standard]" --with anthropic --with pyyaml \
    --with pytest --with httpx python -m pytest
```

No network, no real data dir — every test runs on a temp SQLite file with the
Claude key stubbed out.

## Adding a client

Copy `businesses/desert-smiles-dental.yaml`, edit name/hours/FAQs/
appointment types/escalation rules. That YAML is the entire per-client setup.

## What the AI will and won't do

Books appointments (with availability checks, confirm-before-book,
one-booking-per-caller dedupe), answers FAQs from the YAML only, takes
messages for anything off-script, escalates billing/emergencies/"give me
a human" to a callback message. It is prompted to never claim a booking
without a successful tool call and never invent business facts.

## Claude on the subscription, not an API key

Every Claude call (the brain in `app/brain.py`, post-call extraction in
`app/extraction.py`) goes through `app/llm.py`, which runs Claude Code
headless on Garret's subscription — the same recipe as `~/Projects/jas-ops`:

    claude -p --output-format text --model haiku --tools "" --no-session-persistence \
        --setting-sources user --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
        --system-prompt "<system>" "<conversation so far>"

from an empty `data/claude-cwd/` with the parent's `CLAUDE*` env stripped
(so it works under launchd and from inside a Claude Code session), stdin
closed, one retry on a non-timeout failure. Tools are a text protocol on this
path: the model replies with a one-line JSON tool call, we run it and feed
back a `TOOL RESULT` line.

The Anthropic SDK (native tool use) is used only if `ANTHROPIC_API_KEY` is
set explicitly in the environment or this project's own `.env`. No key is
borrowed from any other project.

Env: `JAS_MODEL` (default `claude-haiku-4-5-20251001` → alias `haiku`),
`JAS_CLAUDE_TIMEOUT` (default 60 s), `CLAUDE_CLI` (path to the binary).

**Known trade-off — latency.** Measured 9/15 (details in CALLER-MEMORY.md):
a brain turn through the CLI is **10–12 s** when a tool is used (two CLI
round-trips; ~4 s for a plain answer) and post-call extraction is ~16 s, so
the browser demo shows a live "Thinking… Ns" state. Without the
`--strict-mcp-config` override it was ~57 s per turn (the CLI boots every
user-level MCP server). A real phone deployment will need either the SDK
path with a dedicated key or a faster path — Garret's call, later.

## Tested (8/20)

- Scripted emergency-booking call: correct date math, confirm-then-book,
  exactly one appointment in DB, mid-booking insurance question answered.
- Billing-dispute call: escalated to an urgent staff message, not handled.
- Both against the live Claude API (claude-haiku-4-5).

## Tested (9/15) — per-caller memory

- 38 unit/API tests green (`tests/`), CLI and SDK paths both stubbed: new vs returning caller prompt injection,
  extraction on a fixture transcript (fallback + fake-Claude paths), brief
  rendering + escaping, per-number data scoping, cursor-based leads export.
- Live on the subscription (scratch DB): two brain turns with real tool calls
  (availability check, then a booking → APT-0001) and a real Claude extraction
  linked to that booking; /brief and /office rendered (headless Chrome).
