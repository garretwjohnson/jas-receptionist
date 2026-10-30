"""Claude access — Garret's subscription via `claude -p` by default.

Same pattern as ~/Projects/jas-ops/ops/adapters.py: Claude Code headless,
tools disabled, no session persistence, user settings only, run from an
empty working directory with the parent session's CLAUDE* env stripped so it
works from a launchd service and from inside a Claude Code session alike.

Two additions over jas-ops, both measured on 9/15:
  * `--strict-mcp-config --mcp-config '{"mcpServers":{}}'` — without it the
    CLI boots every user-level MCP server on each call: 57 s per haiku turn
    instead of ~4 s.
  * the prompt is passed as an argument with stdin=DEVNULL, and one retry on
    a non-timeout failure (the CLI exits 1 intermittently).

The Anthropic SDK is used ONLY when ANTHROPIC_API_KEY is set explicitly (env
or this project's own .env). No key is borrowed from anywhere else.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLAUDE_CWD = ROOT / "data" / "claude-cwd"   # empty dir: no CLAUDE.md / project hooks leak in
MODEL = os.environ.get("JAS_MODEL", "claude-haiku-4-5-20251001")
TIMEOUT = int(os.environ.get("JAS_CLAUDE_TIMEOUT", "60"))
_ALIAS = {"claude-haiku-4-5-20251001": "haiku", "claude-haiku-4-5": "haiku",
          "claude-sonnet-5": "sonnet", "claude-opus-5": "opus"}
_NO_MCP = json.dumps({"mcpServers": {}})


def api_key():
    """ANTHROPIC_API_KEY from the environment or this project's .env — nothing else."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("ANTHROPIC_API_KEY=") and line.split("=", 1)[1].strip():
                os.environ["ANTHROPIC_API_KEY"] = line.split("=", 1)[1].strip().strip("'\"")
                return os.environ["ANTHROPIC_API_KEY"]
    return None


def find_claude():
    exe = os.environ.get("CLAUDE_CLI")
    if exe and Path(exe).exists():
        return exe
    for cand in (shutil.which("claude"), Path.home() / ".local/bin/claude",
                 "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
        if cand and Path(cand).exists():
            return str(cand)
    return None


def mode():
    """'api' only when a key is explicitly configured; otherwise 'cli' (subscription)."""
    return "api" if api_key() else "cli"


def available():
    return mode() == "api" or find_claude() is not None


def _cli_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    env.setdefault("HOME", str(Path.home()))
    return env


def _cli_cmd(exe, system, model):
    return [exe, "-p", "--output-format", "text", "--model", _ALIAS.get(model, model),
            "--tools", "", "--no-session-persistence", "--setting-sources", "user",
            "--strict-mcp-config", "--mcp-config", _NO_MCP,
            "--system-prompt", system]


def _run_cli(system, user, model, timeout):
    exe = find_claude()
    if not exe:
        raise RuntimeError("claude CLI not found (set CLAUDE_CLI=/path/to/claude)")
    CLAUDE_CWD.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(_cli_cmd(exe, system, model) + [user], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, cwd=CLAUDE_CWD, env=_cli_env(),
                       timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"claude -p exit {r.returncode}: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout.strip()


def _api(system, user, model, max_tokens):
    import anthropic
    client = anthropic.Anthropic()
    resp = client.messages.create(model=model, max_tokens=max_tokens, system=system,
                                  messages=[{"role": "user", "content": user}])
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def complete(system, user, model=None, max_tokens=400, timeout=None):
    """One prompt in, text out. Raises on failure (callers decide how to degrade)."""
    model = model or MODEL
    if mode() == "api":
        return _api(system, user, model, max_tokens)
    system = system + f"\n\nAnswer in at most {max_tokens} tokens. Output only the answer, no preamble."
    timeout = timeout or TIMEOUT
    for attempt in (1, 2):
        try:
            return _run_cli(system, user, model, timeout)
        except subprocess.TimeoutExpired:
            raise
        except RuntimeError as e:
            if attempt == 2:
                raise
            print(f"[llm] claude -p failed once, retrying: {e}")
