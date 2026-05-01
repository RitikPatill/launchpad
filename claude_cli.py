"""
Thin wrapper around the `claude` CLI in headless mode.

Why a wrapper: planner.py and builder.py both shell out to Claude, and we want
one place that handles encoding, timeouts, model selection, the
--dangerously-skip-permissions flag, and rate-limit accounting.

The Max plan (interactive Claude Code subscription) is what authenticates the
CLI; we never pass an API key. The orchestrator just borrows your existing
auth. That is the whole "free LLM compute" trick of this project.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import state
from config import (
    CLAUDE_BIN,
    CLAUDE_MODEL,
    CLAUDE_CALL_TIMEOUT_SECONDS,
    CLAUDE_CALLS_PER_5H,
)


# Resolve the real binary path once. On Windows `claude` is a .cmd shim that
# Python's subprocess can't find without the extension or shell=True.
# shutil.which scans PATH with PATHEXT applied, so it returns the .cmd.
_CLAUDE_PATH = shutil.which(CLAUDE_BIN) or CLAUDE_BIN


class RateLimited(Exception):
    """Raised when we are too close to the Max plan's rolling 5h window."""


class ClaudeError(Exception):
    pass


def _check_rate_limit() -> None:
    n = state.claude_calls_in_window(hours=5)
    if n >= CLAUDE_CALLS_PER_5H:
        raise RateLimited(
            f"{n} claude calls in last 5h, cap is {CLAUDE_CALLS_PER_5H}. "
            f"Backing off so the user's interactive session isn't throttled."
        )


def call_claude(
    prompt: str,
    *,
    cwd: Path | str | None = None,
    component: str = "unknown",
    model: str | None = None,
    timeout_s: int | None = None,
    allowed_tools: list[str] | None = None,
    extra_args: list[str] | None = None,
) -> str:
    """
    Invoke `claude -p` headless. Returns the model's text output.

    Prompt is fed via stdin so we don't fight Windows command-line quoting on
    long multi-line prompts.

    The workspace dir (cwd) matters for the builder: when claude is launched
    inside a project dir with --dangerously-skip-permissions, it can freely
    Read/Write/Edit/Bash inside that dir. That sandboxing is why permission
    bypass is acceptable here — the blast radius is one project workspace.

    `allowed_tools` is the multi-agent enforcement knob: pass ["Read","Glob"]
    for an architect-style read-only agent, ["Read","Write","Edit","Bash"]
    for a coder, etc. None = full default toolset.
    """
    _check_rate_limit()

    cmd = [
        _CLAUDE_PATH,
        "-p",
        "--model", model or CLAUDE_MODEL,
        "--output-format", "json",
        "--dangerously-skip-permissions",
    ]
    if allowed_tools:
        cmd.extend(["--allowed-tools", ",".join(allowed_tools)])
    if extra_args:
        cmd.extend(extra_args)

    # Scrub CLAUDECODE markers from the env so a parent Claude Code session
    # (e.g. someone testing this from inside `claude`) doesn't block the
    # subprocess with the "no nested sessions" guard. In normal Task
    # Scheduler / standalone use these vars aren't set anyway.
    child_env = {k: v for k, v in os.environ.items()
                 if not k.startswith("CLAUDECODE") and not k.startswith("CLAUDE_CODE_")}

    started = time.time()
    ok = False
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s or CLAUDE_CALL_TIMEOUT_SECONDS,
            shell=False,
            env=child_env,
        )
    except subprocess.TimeoutExpired as e:
        state.record_claude_call(component, model or CLAUDE_MODEL, time.time() - started, False)
        raise ClaudeError(f"claude timeout after {e.timeout}s") from e
    finally:
        # success bool is set below if we actually got output
        pass

    duration = time.time() - started

    if proc.returncode != 0:
        state.record_claude_call(component, model or CLAUDE_MODEL, duration, False)
        raise ClaudeError(
            f"claude exited {proc.returncode}: "
            f"stderr={proc.stderr[:500]} stdout={proc.stdout[:500]}"
        )

    # JSON output mode emits a single object with a `result` field.
    out = proc.stdout.strip()
    text: str
    try:
        obj = json.loads(out)
        text = obj.get("result") or obj.get("text") or out
    except json.JSONDecodeError:
        # Fall back: maybe the build of CLI emitted plain text. Don't fail
        # the whole pipeline over an output format quirk.
        text = out

    ok = True
    state.record_claude_call(component, model or CLAUDE_MODEL, duration, ok)
    return text


# --- helpers --------------------------------------------------------------- #

_FENCED_OBJ = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_FENCED_ARR = re.compile(r"```(?:json)?\s*(\[.*?\])\s*```", re.DOTALL)
_BARE_OBJ = re.compile(r"(\{.*\})", re.DOTALL)
_BARE_ARR = re.compile(r"(\[.*\])", re.DOTALL)


def extract_json(text: str) -> Any:
    """
    Pull the first JSON value (object OR array) out of a Claude response.
    Tolerates fenced or bare. Returns whatever the JSON parses as.
    """
    for pattern in (_FENCED_OBJ, _FENCED_ARR, _BARE_OBJ, _BARE_ARR):
        m = pattern.search(text)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
    raise ClaudeError(f"no JSON value found in response: {text[:300]}")
