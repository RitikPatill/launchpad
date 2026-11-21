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
    CLAUDE_MODEL,
    CLAUDE_CALL_TIMEOUT_SECONDS,
    CLAUDE_CALLS_PER_5H,
    FALLBACK_MODEL,
    FALLBACK_MODELS,
    PRIVATE_CLAUDE_DIR,
)


def _claude_path() -> str:
    """Resolve the CLI on every call (not once at import), so launchpad's
    private CLI is used as soon as it exists / after it self-updates.
    On Windows the CLI is a .cmd shim; shutil.which applies PATHEXT."""
    env = os.getenv("CLAUDE_BIN", "").strip()
    if env:
        return shutil.which(env) or env
    private = PRIVATE_CLAUDE_DIR / "node_modules" / ".bin" / "claude.cmd"
    if private.exists():
        return str(private)
    return shutil.which("claude") or "claude"


def _run_tree(cmd: list[str], *, input: str | None, cwd: str | None, timeout: int,
              env: dict | None = None) -> tuple[int, str, str]:
    """subprocess.run with a REAL timeout on Windows: on expiry kill the whole
    process tree (the .cmd shim's cmd.exe AND the node/claude child)."""
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if input is not None else None,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=env,
                            text=True, encoding="utf-8", errors="replace")
    try:
        out, err = proc.communicate(input=input, timeout=timeout)
        return proc.returncode, out or "", err or ""
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        try:
            proc.communicate(timeout=30)
        except Exception:
            pass
        raise


class RateLimited(Exception):
    """Raised when we are too close to the Max plan's rolling 5h window,
    OR when the Anthropic backend itself returned a rate-limit response
    (including the Max-plan 'You've hit your limit' interactive message).
    The orchestrator's tick handler catches this WITHOUT bumping
    attempt_count, so the video stays in its current status and the next
    tick retries — instead of burning two attempts and falling to
    `failed_terminal` like video #7 did on 12 May."""


class ClaudeError(Exception):
    pass


# Strings (case-insensitive) in stderr+stdout that indicate the call
# failed because of capacity / quota / throttling rather than a real
# model error. Used to gate the Haiku fallback retry and to decide
# whether to raise RateLimited (preserves attempt) vs ClaudeError
# (burns attempt).
_RATE_LIMIT_PATTERNS = (
    "rate_limit", "rate limit", "quota", "overloaded", "529",
    "too many requests", "usage limit", "credit balance",
    "5h limit", "weekly limit",
    # Max-plan interactive-tier message — exact text that killed #7:
    #   "You've hit your limit — resets 10:30pm (Europe/Berlin)"
    "hit your limit", "you've hit your limit", "you have hit your limit",
)


def _looks_rate_limited(stderr: str, stdout: str) -> bool:
    blob = (stderr + " " + stdout).lower()
    return any(p in blob for p in _RATE_LIMIT_PATTERNS)


def _check_rate_limit() -> None:
    n = state.claude_calls_in_window(hours=5)
    if n >= CLAUDE_CALLS_PER_5H:
        raise RateLimited(
            f"{n} claude calls in last 5h, cap is {CLAUDE_CALLS_PER_5H}. "
            f"Backing off so the user's interactive session isn't throttled."
        )


def _update_private_cli() -> bool:
    """Bring launchpad's private Claude Code CLI to @latest (hidden npm run).
    Called when a model reports the CLI is too old, and weekly."""
    try:
        PRIVATE_CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
        npm = shutil.which("npm") or "npm"
        rc, _out, err = _run_tree(
            [npm, "install", "--no-fund", "--no-audit", "--loglevel=error",
             "--prefix", str(PRIVATE_CLAUDE_DIR), "@anthropic-ai/claude-code@latest"],
            input=None, cwd=None, timeout=600)
        ok = rc == 0
        state.log("INFO" if ok else "WARN", "claude_cli",
                  f"private Claude CLI update {'ok' if ok else 'failed: ' + err[-300:]}")
        return ok
    except Exception as e:
        state.log("WARN", "claude_cli", f"private Claude CLI update error: {e}")
        return False


def maybe_weekly_cli_update() -> None:
    marker = PRIVATE_CLAUDE_DIR / ".last_update"
    try:
        import datetime as _dt
        if marker.exists():
            age = _dt.datetime.now().timestamp() - marker.stat().st_mtime
            if age < 7 * 86400:
                return
        if _update_private_cli():
            marker.write_text("ok", encoding="utf-8")
    except Exception:
        pass


def call_claude(
    prompt: str,
    *,
    cwd: Path | str | None = None,
    component: str = "unknown",
    model: str | None = None,
    timeout_s: int | None = None,
    allowed_tools: list[str] | None = None,
    extra_args: list[str] | None = None,
    fallback_to_haiku: bool = True,
    effort: str | None = None,
    skip_permissions: bool = True,
) -> str:
    """
    Invoke `claude -p` headless (launchpad's private, current CLI). Returns
    the model's text output. Prompt is fed via stdin.

    `allowed_tools` restricts the toolset ([] = no tools, pure generation).
    `effort` maps to --effort (low|medium|high|xhigh|max).

    Never-stop policy: if the primary model is rate-limited, overloaded or
    unavailable, the SAME prompt is retried down FALLBACK_MODELS (Opus 5.5,
    then Sonnet 5.5). `fallback_to_haiku` (legacy name) disables that.
    If every model is rate-limited -> RateLimited (attempt not burned, the
    tick retries later). If the CLI is too old for a model it is updated
    in place and the call retried once.
    """
    _check_rate_limit()

    primary_model = model or CLAUDE_MODEL
    timeout = timeout_s or CLAUDE_CALL_TIMEOUT_SECONDS

    # Scrub CLAUDECODE markers so a parent Claude Code session can't block
    # the subprocess with the "no nested sessions" guard.
    child_env = {k: v for k, v in os.environ.items()
                 if not k.startswith("CLAUDECODE") and not k.startswith("CLAUDE_CODE_")}

    def _build_cmd(m: str) -> list[str]:
        c = [_claude_path(), "-p", "--model", m, "--output-format", "json"]
        if allowed_tools is not None and len(allowed_tools) == 0:
            # Pure generation (prompts embed untrusted README/code text): no
            # built-in tools, no MCP servers, no permission bypass - so an
            # injected instruction has nothing it can execute.
            c.extend(["--tools", "", "--strict-mcp-config"])
        else:
            if allowed_tools:
                c.extend(["--allowed-tools", ",".join(allowed_tools)])
            if skip_permissions:
                c.append("--dangerously-skip-permissions")
        if effort:
            c.extend(["--effort", effort])
        if extra_args:
            c.extend(extra_args)
        return c

    def _parse(stdout: str) -> str:
        out = stdout.strip()
        try:
            obj = json.loads(out)
            if obj.get("is_error"):
                raise ClaudeError(f"claude returned is_error: {str(obj.get('result'))[:400]}")
            res = obj.get("result") or obj.get("text")
            if not res:
                raise ClaudeError("claude returned an empty result")
            return res
        except json.JSONDecodeError:
            return out

    def _invoke(m: str, t: int) -> tuple[int, str, str, float]:
        started = time.time()
        try:
            rc, out, err = _run_tree(_build_cmd(m), input=prompt, cwd=str(cwd) if cwd else None,
                                     timeout=t, env=child_env)
            return rc, out, err, time.time() - started
        except subprocess.TimeoutExpired as e:
            state.record_claude_call(component, m, time.time() - started, False)
            raise ClaudeError(f"claude {m} timeout after {e.timeout}s") from e

    def _is_error_payload(stdout: str) -> bool:
        try:
            return bool(json.loads(stdout.strip()).get("is_error"))
        except Exception:
            return False

    chain = [primary_model]
    if fallback_to_haiku:
        chain += [m for m in FALLBACK_MODELS if m != primary_model]

    errors: list[str] = []
    all_rate_limited = True
    cli_updated = False
    i = 0
    while i < len(chain):
        m = chain[i]
        t = timeout if i == 0 else max(120, min(timeout, 900))
        rc, stdout, stderr, duration = _invoke(m, t)
        if rc == 0 and stdout.strip() and not _is_error_payload(stdout):
            state.record_claude_call(component, m, duration, True)
            if i > 0:
                state.log("WARN", "claude_cli", f"{component}: served by fallback {m}")
            return _parse(stdout)
        state.record_claude_call(component, m, duration, False)
        blob = (stderr + " " + stdout).lower()
        err = f"claude {m} exited {rc}: stderr={stderr[:300]} stdout={stdout[:300]}"
        errors.append(err)
        if "claude_code_version_too_old" in blob and not cli_updated:
            cli_updated = True
            if _update_private_cli():
                continue            # retry the same model on the updated CLI
        limited = _looks_rate_limited(stderr, stdout)
        unavailable = any(k in blob for k in ("not_found_error", "does not support this model",
                                              "model_not_available", "overloaded"))
        if not limited:
            all_rate_limited = False
        if not (limited or unavailable):
            raise ClaudeError(err)  # a real error; another model won't fix it
        i += 1

    if all_rate_limited:
        raise RateLimited("all models rate-limited: " + " || ".join(errors))
    raise ClaudeError("all models failed: " + " || ".join(errors))


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
    # strict=False: models often put literal newlines/tabs inside JSON
    # strings (multi-line descriptions); those are invalid in strict JSON
    # but unambiguous, so accept them.
    for pattern in (_FENCED_OBJ, _FENCED_ARR):
        m = pattern.search(text)
        if m:
            try:
                return json.loads(m.group(1), strict=False)
            except json.JSONDecodeError:
                continue
    # Decode every top-level JSON value in reading order and return the
    # LARGEST one: the answer is the big blob, not a "[1]" in the preamble.
    dec = json.JSONDecoder(strict=False)
    best, best_len, i, n = None, 0, 0, len(text)
    while i < n:
        if text[i] in "{[":
            try:
                val, end = dec.raw_decode(text, i)
                if end - i > best_len:
                    best, best_len = val, end - i
                i = end
                continue
            except json.JSONDecodeError:
                pass
        i += 1
    if best is not None:
        return best
    raise ClaudeError(f"no JSON value found in response: {text[:300]}")
