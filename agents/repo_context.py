"""
Collects REAL material about a project for the storyboard agent: README,
file tree and the most relevant source files from the local workspace that
autodev / agent-radar built (read-only). Everything shown on screen (code,
commands, numbers) must come from here - never invented.
"""
from __future__ import annotations

import re
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".mypy_cache",
             ".pytest_cache", "data", ".next", ".idea", ".vscode", "site-packages", ".ruff_cache"}
CODE_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".rb", ".sh"}
README_CHARS = 14000
FILE_LINES = 170
MAX_FILES = 4


def _tree(root: Path, limit: int = 120) -> list[str]:
    out: list[str] = []
    for p in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        if p.is_file():
            out.append(p.relative_to(root).as_posix())
            if len(out) >= limit:
                break
    return out


def _score(rel: str, size: int, readme: str) -> float:
    name = Path(rel).name
    s = 0.0
    if name in readme or rel in readme:
        s += 5                      # README explicitly points at it
    if rel.startswith(("src/", "app/", "lib/")) or "/" not in rel:
        s += 2
    if any(k in name for k in ("main", "app", "core", "agent", "pipeline", "engine", "cli", "server", "router")):
        s += 2
    if "test" in rel.lower() or name.startswith("__init__"):
        s -= 6
    s += min(size, 9000) / 3000     # substantive files
    return s


def _clone_public(repo_url: str) -> Path | None:
    """Shallow-clone the project's PUBLIC GitHub repo into a local cache when
    the builder's workspace is gone (older projects). Read-only use."""
    m = re.match(r"https?://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", repo_url or "")
    if not m:
        return None
    import shutil
    import subprocess
    from config import LOCAL_HOME
    dest = LOCAL_HOME / "repo-cache" / f"{m.group(1)}__{m.group(2)}"
    if (dest / ".git").exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    git = shutil.which("git") or "git"
    try:
        proc = subprocess.run([git, "clone", "--depth", "1", "--quiet", f"https://github.com/{m.group(1)}/{m.group(2)}.git",
                               str(dest)], capture_output=True, text=True, timeout=180,
                              env={**__import__("os").environ, "GIT_TERMINAL_PROMPT": "0"})
        return dest if proc.returncode == 0 and dest.exists() else None
    except Exception:
        return None


def gather(workspace: Path | None, *, title: str = "", description: str = "", repo_url: str = "") -> dict:
    """Return {"readme", "tree", "files": [{path, text}], "has_workspace"}.
    Falls back to a shallow clone of the public repo if the workspace is gone."""
    if not workspace or not Path(workspace).exists():
        workspace = _clone_public(repo_url)
    if not workspace or not Path(workspace).exists():
        return {"readme": "", "tree": [], "files": [], "has_workspace": False}
    root = Path(workspace)
    committed = _tracked(root)

    def read(rel: str) -> str | None:
        # Prefer the COMMITTED version: uncommitted local edits are not the
        # published project and must never be shown as its code.
        if committed:
            txt = _git_show(root, rel)
            if txt is not None:
                return txt
        p = root / rel
        return p.read_text(encoding="utf-8", errors="replace") if p.exists() else None

    readme = ""
    for cand in ("README.md", "readme.md", "README.rst", "README.txt"):
        txt = read(cand)
        if txt:
            readme = txt[:README_CHARS]
            break
    tree = committed or _tree(root)
    scored = []
    for rel in tree:
        p = root / rel
        if p.suffix.lower() not in CODE_EXT:
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size < 200 or size > 200_000:
            continue
        scored.append((_score(rel, size, readme), rel))
    files = []
    for _, rel in sorted(scored, reverse=True)[:MAX_FILES]:
        txt = read(rel)
        if txt is None:
            continue
        files.append({"path": rel, "text": "\n".join(txt.splitlines()[:FILE_LINES])})
    return {"readme": readme, "tree": tree, "files": files, "has_workspace": True}


def _git_show(root: Path, rel: str) -> str | None:
    import shutil
    import subprocess
    try:
        p = subprocess.run([shutil.which("git") or "git", "-C", str(root), "show", f"HEAD:{rel}"],
                           capture_output=True, timeout=60)
    except Exception:
        return None
    return p.stdout.decode("utf-8", errors="replace") if p.returncode == 0 else None


def as_prompt(ctx: dict) -> str:
    parts = []
    if ctx.get("readme"):
        parts.append("=== README.md ===\n" + ctx["readme"])
    if ctx.get("tree"):
        parts.append("=== FILE TREE ===\n" + "\n".join(ctx["tree"]))
    for f in ctx.get("files", []):
        numbered = "\n".join(f"{i + 1:>4}  {ln}" for i, ln in enumerate(f["text"].splitlines()))
        parts.append(f"=== SOURCE: {f['path']} (line numbers are for reference only) ===\n{numbered}")
    return "\n\n".join(parts) if parts else "(no local workspace available - use only the title and description)"


_WS = re.compile(r"\s+")


def _tracked(root: Path, limit: int = 120) -> list[str]:
    """Files committed to git (what the public repo actually contains), so
    unpublished local scratch files are never presented as the project."""
    import shutil
    import subprocess
    if not (root / ".git").exists():
        return []
    try:
        proc = subprocess.run([shutil.which("git") or "git", "-C", str(root), "ls-files"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    files = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    files = [f for f in files if not any(part in SKIP_DIRS for part in Path(f).parts)]
    return sorted(files)[:limit]


def code_is_real(code: str, ctx: dict, filename: str | None = None) -> bool:
    """True only if the snippet is genuinely from the project: every
    non-trivial line appears in the README or a gathered (published) source
    file, at least two substantive lines were matched (so a snippet made of
    tiny generic lines cannot pass by default), and the shown filename -
    if any - exists in the repo. '...' lines are allowed."""
    # Code may come from committed source files or README CODE BLOCKS - never
    # from README prose (an HTML comment or a sentence is not code).
    readme_code = "\n".join(m.group(1) for m in re.finditer(r"```[^\n]*\n(.*?)```", ctx.get("readme", ""), re.S))
    corpus = _WS.sub(" ", readme_code + "\n" + "\n".join(f["text"] for f in ctx.get("files", [])))
    substantive = 0
    for ln in code.splitlines():
        s = _WS.sub(" ", ln).strip()
        if not s or s.strip("#/ ") in ("...", "…") or len(s) < 4:
            continue
        if s not in corpus:
            return False
        if len(s) >= 12:
            substantive += 1
    if substantive < 2:
        return False
    if filename:
        base = Path(str(filename)).name
        paths = set(ctx.get("tree", [])) | {f["path"] for f in ctx.get("files", [])}
        if base and not any(Path(p).name == base for p in paths) and base not in ctx.get("readme", ""):
            return False
    return True
