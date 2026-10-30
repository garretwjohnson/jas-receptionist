"""app/llm.py: subscription CLI by default, SDK only with an explicit key,
env stripping, retry-once on failure, no retry on timeout."""
import os
import subprocess

import pytest

from app import llm
from conftest import REAL_API_KEY, REAL_RUN_CLI


class Run:
    """Fake subprocess.run recording each invocation; scripted results."""
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def __call__(self, cmd, **kw):
        self.calls.append({"cmd": cmd, **kw})
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class Done:
    def __init__(self, code, out="", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


@pytest.fixture
def cli(monkeypatch, tmp_path):
    monkeypatch.setattr(llm, "_run_cli", REAL_RUN_CLI)   # undo conftest guard
    monkeypatch.setattr(llm, "find_claude", lambda: "/fake/claude")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("KEEP_ME", "yes")


def test_mode_is_cli_unless_key_is_explicit(monkeypatch):
    assert llm.mode() == "cli"
    monkeypatch.setattr(llm, "api_key", lambda: "sk-x")
    assert llm.mode() == "api"


def test_api_key_reads_only_env_or_project_dotenv(monkeypatch, tmp_path):
    monkeypatch.setattr(llm, "api_key", REAL_API_KEY)
    monkeypatch.setattr(llm, "ROOT", tmp_path)
    assert llm.api_key() is None                       # no borrowed key from anywhere
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY='sk-proj'\n")
    assert llm.api_key() == "sk-proj"


def test_cli_command_shape_and_env(cli, monkeypatch):
    run = Run(Done(0, "  Hello there.\n"))
    monkeypatch.setattr(subprocess, "run", run)
    assert llm.complete("SYS", "USER", max_tokens=50) == "Hello there."
    c = run.calls[0]
    cmd = c["cmd"]
    assert cmd[0] == "/fake/claude" and cmd[1] == "-p"
    assert cmd[cmd.index("--model") + 1] == "haiku"
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--no-session-persistence" in cmd and cmd[cmd.index("--setting-sources") + 1] == "user"
    assert "--strict-mcp-config" in cmd and '"mcpServers": {}' in cmd[cmd.index("--mcp-config") + 1]
    assert cmd[cmd.index("--system-prompt") + 1].startswith("SYS") and "at most 50 tokens" in cmd[-2]
    assert cmd[-1] == "USER"
    assert c["stdin"] is subprocess.DEVNULL and c["timeout"] == llm.TIMEOUT
    assert not any(k.startswith("CLAUDE") for k in c["env"]) and c["env"]["KEEP_ME"] == "yes"
    assert c["cwd"] == llm.CLAUDE_CWD and llm.CLAUDE_CWD.is_dir()


def test_cli_retries_once_on_failure(cli, monkeypatch):
    run = Run(Done(1, "", "intermittent"), Done(0, "second try"))
    monkeypatch.setattr(subprocess, "run", run)
    assert llm.complete("S", "U") == "second try"
    assert len(run.calls) == 2


def test_cli_gives_up_after_second_failure(cli, monkeypatch):
    run = Run(Done(1, "", "a"), Done(1, "", "b"))
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(RuntimeError, match="exit 1"):
        llm.complete("S", "U")
    assert len(run.calls) == 2


def test_cli_does_not_retry_on_timeout(cli, monkeypatch):
    run = Run(subprocess.TimeoutExpired("claude", 60), Done(0, "never"))
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(subprocess.TimeoutExpired):
        llm.complete("S", "U")
    assert len(run.calls) == 1


def test_cli_missing_binary(monkeypatch):
    monkeypatch.setattr(llm, "_run_cli", REAL_RUN_CLI)
    monkeypatch.setattr(llm, "find_claude", lambda: None)
    with pytest.raises(RuntimeError, match="not found"):
        llm.complete("S", "U")
