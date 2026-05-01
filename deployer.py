"""
HuggingFace Spaces auto-deployer.

Solves the "demo question" from the user: most generated projects are
Streamlit/Gradio apps, but they live as repos with no live demo.
Recruiters want clickable demos, not "git clone it yourself".

HuggingFace Spaces is the right host for this: free, no credit card,
no rate limits worth caring about, and recruiters in AI/ML *expect* to
see HF Spaces in candidates' portfolios.

Flow per project:
  1. Read the project's local clone (autodev's or agent-radar's
     workspace dir).
  2. Detect Streamlit / Gradio / static-site framework.
  3. Create the Space repo via HF API (idempotent — reuses if exists).
  4. Push the project files to the Space's git repo.
  5. Wait for HF to build (usually 30-90s).
  6. Return the live URL: https://huggingface.co/spaces/<user>/<slug>

Auth: HF token from huggingface.co/settings/tokens (free, 30s setup).
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import requests

import state


HF_API = "https://huggingface.co/api"


class DeployError(Exception):
    pass


def _hf_token() -> str:
    token = os.getenv("HF_TOKEN", "").strip()
    if not token:
        raise DeployError(
            "HF_TOKEN missing. Get one at https://huggingface.co/settings/tokens "
            "(role=write), then add HF_TOKEN=... to launchpad/.env"
        )
    return token


def _hf_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_hf_token()}"}


def _hf_username() -> str:
    """Resolve who the token belongs to (cached on first call)."""
    cache = getattr(_hf_username, "_v", None)
    if cache:
        return cache
    r = requests.get(f"{HF_API}/whoami-v2", headers=_hf_headers(), timeout=15)
    if r.status_code != 200:
        raise DeployError(f"HF whoami failed: {r.status_code} {r.text[:200]}")
    user = r.json().get("name") or r.json().get("user") or ""
    if not user:
        raise DeployError(f"could not parse HF username from whoami: {r.text[:300]}")
    _hf_username._v = user
    return user


# --- Framework detection -------------------------------------------------- #

def detect_framework(workspace: Path) -> str | None:
    """
    Return one of:
      "streamlit"  — Streamlit app (best on HF Spaces SDK=streamlit)
      "gradio"     — Gradio app (HF Spaces SDK=gradio)
      "static"     — pure static HTML (HF Spaces SDK=static)
      None         — nothing deployable detected
    """
    # Check requirements.txt for streamlit/gradio.
    req = workspace / "requirements.txt"
    if req.exists():
        text = req.read_text(encoding="utf-8", errors="ignore").lower()
        if "streamlit" in text:
            return "streamlit"
        if "gradio" in text:
            return "gradio"
    # Check pyproject.toml.
    pyproj = workspace / "pyproject.toml"
    if pyproj.exists():
        text = pyproj.read_text(encoding="utf-8", errors="ignore").lower()
        if "streamlit" in text:
            return "streamlit"
        if "gradio" in text:
            return "gradio"
    # Check for Streamlit-shaped files (last-ditch).
    for name in ("streamlit_app.py", "app.py"):
        f = workspace / name
        if f.exists():
            sample = f.read_text(encoding="utf-8", errors="ignore")[:5000].lower()
            if "import streamlit" in sample or "from streamlit" in sample:
                return "streamlit"
            if "import gradio" in sample or "from gradio" in sample:
                return "gradio"
    # Static site fallback: docs/index.html or root index.html.
    if (workspace / "docs" / "index.html").exists() or (workspace / "index.html").exists():
        return "static"
    return None


# --- Space creation + push ----------------------------------------------- #

def _create_space(slug: str, sdk: str, hardware: str = "cpu-basic") -> str:
    """POST /api/repos/create. Idempotent — returns existing if 409."""
    user = _hf_username()
    payload = {
        "name": slug,
        "type": "space",
        "private": False,
        "sdk": sdk,
        "hardware": hardware,
    }
    r = requests.post(
        f"{HF_API}/repos/create", headers=_hf_headers(), json=payload, timeout=30,
    )
    if r.status_code in (200, 201):
        state.log("INFO", "deployer", f"created HF Space {user}/{slug} (sdk={sdk})")
    elif r.status_code == 409:
        state.log("INFO", "deployer", f"HF Space {user}/{slug} already exists, reusing")
    else:
        raise DeployError(f"create space {slug} failed: {r.status_code} {r.text[:300]}")
    return f"https://huggingface.co/spaces/{user}/{slug}"


def _ensure_space_readme(workspace: Path, slug: str, sdk: str,
                         project_title: str, project_description: str) -> None:
    """
    HF Spaces requires a README.md with frontmatter declaring the SDK.
    If the project's README doesn't already have that frontmatter, prepend
    it (without overwriting the original content).
    """
    readme = workspace / "README.md"
    body = readme.read_text(encoding="utf-8", errors="ignore") if readme.exists() else f"# {project_title}\n\n{project_description}\n"
    if body.lstrip().startswith("---"):
        return  # already has frontmatter
    frontmatter = (
        f"---\n"
        f"title: {project_title[:40]}\n"
        f"emoji: \U0001F916\n"
        f"colorFrom: purple\n"
        f"colorTo: blue\n"
        f"sdk: {sdk}\n"
        f"sdk_version: \"1.35.0\"\n" if sdk == "streamlit" else f"sdk: {sdk}\n"
        f"app_file: streamlit_app.py\n" if sdk == "streamlit" and (workspace / "streamlit_app.py").exists()
        else f"app_file: app.py\n"
        f"pinned: false\n"
        f"---\n\n"
    )
    # The above conditional concat is ugly because of f-string limitations.
    # Re-build cleanly:
    fm_lines = [
        "---",
        f"title: {project_title[:40]}",
        "emoji: \U0001F916",
        "colorFrom: purple",
        "colorTo: blue",
        f"sdk: {sdk}",
    ]
    if sdk == "streamlit":
        fm_lines.append("sdk_version: \"1.35.0\"")
        app_file = "streamlit_app.py" if (workspace / "streamlit_app.py").exists() else "app.py"
        fm_lines.append(f"app_file: {app_file}")
    elif sdk == "gradio":
        fm_lines.append("sdk_version: \"4.0.0\"")
        fm_lines.append("app_file: app.py")
    fm_lines += ["pinned: false", "---", "", ""]
    frontmatter = "\n".join(fm_lines)
    readme.write_text(frontmatter + body, encoding="utf-8")


def _push_to_space(workspace: Path, slug: str) -> None:
    """Push the workspace's contents to the new Space's git remote."""
    user = _hf_username()
    token = _hf_token()
    space_url = f"https://{user}:{token}@huggingface.co/spaces/{user}/{slug}"

    def _git(args, check=True):
        return subprocess.run(
            ["git", *args], cwd=str(workspace),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=check,
        )

    # Add (or update) the hf-space remote — idempotent.
    rm = _git(["remote", "remove", "hf-space"], check=False)
    _git(["remote", "add", "hf-space", space_url])

    # Make sure the README frontmatter change (if any) is committed.
    _git(["add", "README.md"], check=False)
    cp = _git(["commit", "-m", "chore: add HF Space frontmatter", "--allow-empty"], check=False)

    # Push to the Space's main branch (HF Spaces uses main).
    push = _git(["push", "hf-space", "HEAD:main", "--force"], check=False)
    if push.returncode != 0:
        raise DeployError(f"push to HF Space failed: {push.stderr[-500:]}")


def _wait_for_space_ready(slug: str, max_seconds: int = 180) -> bool:
    """
    Poll the Space's runtime status until it reaches "RUNNING" or timeout.
    HF builds + boots in 30-90s typically; we cap at 3 minutes.
    """
    user = _hf_username()
    deadline = time.time() + max_seconds
    while time.time() < deadline:
        try:
            r = requests.get(
                f"{HF_API}/spaces/{user}/{slug}",
                headers=_hf_headers(), timeout=15,
            )
            if r.status_code == 200:
                stage = r.json().get("runtime", {}).get("stage", "").upper()
                if stage in ("RUNNING", "RUNNABLE"):
                    return True
                if stage in ("BUILD_ERROR", "RUNTIME_ERROR", "CONFIG_ERROR"):
                    state.log("WARN", "deployer",
                              f"HF Space {slug} reached error stage: {stage}")
                    return False
        except requests.RequestException:
            pass
        time.sleep(8)
    return False


def deploy_project(workspace: Path, slug: str, project_title: str,
                   project_description: str) -> str | None:
    """
    Deploy a project to HF Spaces. Returns the live URL on success, or
    None if the project isn't a deployable framework. Raises DeployError
    on hard failures (auth, network, push).
    """
    if not workspace.exists():
        state.log("WARN", "deployer", f"workspace missing: {workspace}")
        return None

    sdk = detect_framework(workspace)
    if sdk is None:
        state.log("INFO", "deployer",
                  f"{slug}: not a Streamlit/Gradio/static project, skipping deploy")
        return None
    state.log("INFO", "deployer", f"{slug}: detected sdk={sdk}, deploying to HF Space")

    space_url = _create_space(slug, sdk)
    _ensure_space_readme(workspace, slug, sdk, project_title, project_description)
    _push_to_space(workspace, slug)
    ready = _wait_for_space_ready(slug)
    state.log(
        "INFO", "deployer",
        f"{slug} deployed to {space_url} "
        f"({'ready' if ready else 'still building — recorder may catch loading frame'})"
    )
    return space_url
