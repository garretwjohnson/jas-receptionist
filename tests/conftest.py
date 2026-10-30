"""Shared fixtures. Every test runs against a throwaway data dir and with the
Claude key stubbed out, so nothing here touches data/ or the network."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import booking, brain, callers, extraction, llm  # noqa: E402

BIZ_ID = "desert-smiles-dental"
ALICE = "+14805550142"
BOB = "+16025550199"

FIXTURE_TRANSCRIPT = """Ivy: Thank you for calling Desert Smiles Dental, this is Ivy, the virtual receptionist. How can I help you today?
Caller: Hi, my name is Maria Lopez. I cracked a tooth last night and it really hurts. Can I get in today?
Ivy: I'm sorry to hear that, Maria. We hold emergency slots each weekday — let me check today's openings.
Caller: Thanks. Also, how much would it cost? I don't have insurance right now so I'm a little worried about the price.
Ivy: An emergency visit is thirty minutes; the office will confirm the exact cost when you arrive. I have 2:00 or 2:30 today.
Caller: 2:30 works. My number is 480-555-0142, and I live at 1234 E Main St, Mesa. Could you speak a little slower please?
Ivy: Of course. Confirming Maria Lopez at 480-555-0142 for an emergency visit today at 2:30 — shall I book it?
Caller: Yes please, book it.
Ivy: You're booked, Maria. Confirmation APT-0007. We'll see you at 2:30.
Caller: Great, thank you, goodbye."""


REAL_RUN_CLI = llm._run_cli       # captured before the autouse guard replaces them
REAL_API_KEY = llm.api_key


def _no_live_cli(*a, **k):
    raise AssertionError("tests must never spawn the real claude CLI")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(booking, "DATA", tmp_path / "data")
    monkeypatch.setattr(llm, "CLAUDE_CWD", tmp_path / "claude-cwd")
    monkeypatch.setattr(llm, "available", lambda: False)    # -> deterministic fallback
    monkeypatch.setattr(llm, "api_key", lambda: None)       # -> cli mode, never the SDK
    monkeypatch.setattr(llm, "_run_cli", _no_live_cli)      # belt and braces
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    yield tmp_path


@pytest.fixture
def biz():
    return brain.load_business(BIZ_ID)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


class FakeLLM:
    """Stands in for llm.complete: records every (system, user) and answers from a
    queue of canned replies (last one repeats). This is the CLI-path stub."""
    def __init__(self, *replies):
        self.replies = list(replies) or ["Okay."]
        self.calls = []

    def __call__(self, system, user, model=None, max_tokens=400, timeout=None):
        self.calls.append({"system": system, "user": user, "model": model, "max_tokens": max_tokens})
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


@pytest.fixture
def fake_llm(monkeypatch):
    """CLI path with llm.complete stubbed. Use fake_llm.replies to script the model."""
    f = FakeLLM()
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "complete", f)
    return f


class FakeBlock:
    def __init__(self, text):
        self.type, self.text = "text", text


class FakeResponse:
    def __init__(self, text):
        self.stop_reason, self.content = "end_turn", [FakeBlock(text)]


class FakeAnthropic:
    """SDK path stub: captures every messages.create() call; replies with canned text."""
    calls = []

    def __init__(self, reply_text="Okay."):
        self._reply = reply_text
        self.messages = self

    def create(self, **kw):
        FakeAnthropic.calls.append(kw)
        return FakeResponse(self._reply)


@pytest.fixture
def fake_claude(monkeypatch):
    """SDK path: an explicit key is configured and anthropic.Anthropic is faked."""
    import anthropic
    FakeAnthropic.calls = []
    monkeypatch.setattr(llm, "api_key", lambda: "sk-test")
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: FakeAnthropic())
    return FakeAnthropic
