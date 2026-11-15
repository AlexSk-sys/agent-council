"""Project context for a review: verify command, impact, docs, lessons, snapshot.

Everything here is deterministic and model-free. The point is to give the
three agents facts (a test result, callers, doc claims, past failure classes)
instead of making them spend tokens discovering them, and to run the review
against a disposable snapshot rather than the live tree.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

VERIFY_TIMEOUT_S = 300
REPRO_TIMEOUT_S = 120
MAX_VERIFY_OUTPUT = 6000
MAX_IMPACT_CHARS = 40000
MAX_DOC_CHARS = 6000
MAX_LESSON_CHARS = 4000

REPRO_TOKENS = {
    "node", "python", "python3", "py", "pytest", "npm", "npx", "pnpm", "yarn",
    "bun", "deno", "dotnet", "go", "cargo", "git",
}
REPRO_DENY = (
    "rm -rf", "rm -fr", "del /s", "rd /s", "format ", "shutdown", "git push",
    "git reset --hard", "git clean", "curl ", "wget ", "invoke-webrequest",
    "start-process", "reg delete", "takeown", "icacls",
)


def _git(project: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(project), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30,
        )
        return proc.stdout if proc.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def run_command(command: str, cwd: Path, timeout_s: int = VERIFY_TIMEOUT_S) -> dict:
    try:
        proc = subprocess.run(
            command, cwd=str(cwd), shell=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_s,
            stdin=subprocess.DEVNULL,
        )
        output = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        return {"command": command, "code": proc.returncode,
                "output": output.strip()[-MAX_VERIFY_OUTPUT:]}
    except subprocess.TimeoutExpired:
        return {"command": command, "code": -1,
                "output": f"timeout after {timeout_s}s"}
    except OSError as exc:
        return {"command": command, "code": -1,
                "output": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------
# verify command
# --------------------------------------------------------------------------


def _config(project: Path) -> dict:
    path = Path(project) / ".council" / "config.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def pipeline_options(project: Path) -> dict:
    """Defaults are all-on; a project can turn a costly stage off."""
    raw = _config(project).get("pipeline") or {}
    return {
        "verify": bool(raw.get("verify", True)),
        "cross": bool(raw.get("cross", True)),
        "isolate": bool(raw.get("isolate", True)),
        "lessons": bool(raw.get("lessons", True)),
        "impact": bool(raw.get("impact", True)),
    }


def detect_verify_commands(project: Path) -> list[str]:
    configured = _config(project).get("verify")
    if isinstance(configured, list) and configured:
        return [str(c) for c in configured]
    if isinstance(configured, str) and configured.strip():
        return [configured]
    if (project / "package.json").exists():
        try:
            pkg = json.loads((project / "package.json").read_text(encoding="utf-8"))
            if (pkg.get("scripts") or {}).get("test"):
                return ["npm test"]
        except (OSError, json.JSONDecodeError):
            pass
    if (project / "go.mod").exists():
        return ["go test ./..."]
    if (project / "Cargo.toml").exists():
        return ["cargo test"]
    if list(project.glob("*.sln")) or list(project.glob("*.csproj")):
        return ["dotnet test --nologo"]
    pyproject = project / "pyproject.toml"
    if pyproject.exists():
        try:
            text = pyproject.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if "pytest" in text or (project / "pytest.ini").exists():
            return ["python -m pytest -q"]
        return ["python -m unittest discover -s tests"]
    if (project / "tests").is_dir():
        return ["python -m unittest discover -s tests"]
    return []


def run_verify(project: Path, commands: list[str] | None = None) -> list[dict]:
    cmds = detect_verify_commands(project) if commands is None else commands
    return [run_command(c, project) for c in cmds]


# --------------------------------------------------------------------------
# impact: changed files, callers, doc claims
# --------------------------------------------------------------------------


def changed_paths(project: Path) -> list[str]:
    out = _git(project, "status", "--porcelain", "-uall")
    paths: list[str] = []
    for line in out.splitlines():
        path = line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ")[-1]
        if not path or path.startswith(".council"):
            continue
        paths.append(path)
    return paths


def collect_impact(project: Path, paths: list[str], max_files: int = 5) -> str:
    project = Path(project)
    blocks: list[str] = []
    used = 0
    for rel in paths:
        if len(blocks) >= max_files:
            break
        src = project / rel
        if not src.is_file():
            continue
        try:
            text = src.read_text(encoding="utf-8", errors="replace")[:20000]
        except OSError:
            continue
        stem = src.stem
        callers = _git(project, "grep", "-l", "--fixed-strings", stem) if len(stem) > 2 else ""
        caller_list = [c for c in callers.splitlines() if c != rel][:8]
        recent = _git(project, "log", "--oneline", "-3", "--", rel).strip()
        block = (f"--- {rel}\n"
                 f"callers: {', '.join(caller_list) or 'none found'}\n"
                 f"recent: {recent or 'no commits'}\n"
                 f"{text}")
        used += len(block)
        if used > MAX_IMPACT_CHARS:
            block = block[: max(0, len(block) - (used - MAX_IMPACT_CHARS))]
        blocks.append(block)
        if used > MAX_IMPACT_CHARS:
            break
    return "\n\n".join(blocks)


def collect_docs(project: Path) -> str:
    project = Path(project)
    parts: list[str] = []
    for name in ("CLAUDE.md", "README.md", "STATUS.md"):
        path = project / name
        if not path.is_file():
            continue
        try:
            parts.append(f"--- {name}\n{path.read_text(encoding='utf-8', errors='replace')[:2500]}")
        except OSError:
            continue
    return "\n\n".join(parts)[:MAX_DOC_CHARS]


# --------------------------------------------------------------------------
# lessons: confirmed failure classes, fed back into later prompts
# --------------------------------------------------------------------------


def lessons_path(project: Path) -> Path:
    return Path(project) / ".council" / "checklist.md"


def load_lessons(project: Path) -> str:
    path = lessons_path(project)
    try:
        return path.read_text(encoding="utf-8")[-MAX_LESSON_CHARS:]
    except OSError:
        return ""


def append_lessons(project: Path, findings, run_id: str) -> int:
    """Record confirmed blocker/major classes once each; return how many were new."""
    path = lessons_path(project)
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        existing = ""
    added = 0
    for f in findings:
        if getattr(f, "status", "") != "confirmed":
            continue
        if getattr(f, "source", "") == "verify":
            continue  # a flaky test run is not a failure class to memorize
        if f.severity not in ("blocker", "major"):
            continue
        digest = hashlib.sha256(f.issue.encode("utf-8")).hexdigest()[:12]
        loc_key = f"loc:{f.file}:{f.line}:{f.severity}"
        if digest in existing or loc_key in existing:
            continue  # same wording, or the same defect described by another peer
        line = (f"- [{time.strftime('%Y-%m-%d')}] [{f.severity}] {f.issue} "
                f"-- evidence: {f.evidence[:160]} ({f.source}; {run_id}) "
                f"<!--{digest} {loc_key}-->")
        existing += line + "\n"
        added += 1
    if added:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(existing, encoding="utf-8")
        except OSError:
            return 0
    return added


# --------------------------------------------------------------------------
# repro: deterministic confirmation of a finding
# --------------------------------------------------------------------------


def repro_allowed(command: str) -> bool:
    text = (command or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if any(bad in lowered for bad in REPRO_DENY):
        return False
    first = text.split()[0].strip("'\"").lower()
    first = Path(first).name
    return first in REPRO_TOKENS


def verify_repros(cwd: Path, findings, timeout_s: int = REPRO_TIMEOUT_S) -> None:
    """Run each finding's REPRO; non-zero exit confirms, zero refutes."""
    for f in findings:
        repro = getattr(f, "repro", "")
        if not repro:
            if not f.status:
                f.status = "unverified"
            continue
        if not repro_allowed(repro):
            f.status = "unverified"
            f.repro_output = "blocked: command not allowed"
            continue
        res = run_command(repro, Path(cwd), timeout_s)
        f.repro_output = res["output"][-2000:]
        if res["code"] == 0:
            f.status = "refuted"
        elif res["code"] == -1:
            f.status = "unverified"
        else:
            f.status = "confirmed"


# --------------------------------------------------------------------------
# isolated snapshot
# --------------------------------------------------------------------------


class Snapshot:
    """A disposable copy of the working tree so reviewers cannot be fooled."""

    def __init__(self, project: Path):
        self.project = Path(project).resolve()
        self.dir: Path | None = None
        self.meta: dict = {"isolation": "none"}

    def prepare(self) -> Path:
        project = self.project
        if not (project / ".git").exists():
            self.meta = {"isolation": "none", "reason": "not a git repository"}
            return project
        head = _git(project, "rev-parse", "--verify", "HEAD").strip()
        if not head:
            self.meta = {"isolation": "none", "reason": "no HEAD commit"}
            return project
        tmp = Path(tempfile.mkdtemp(prefix="council-snap-"))
        try:
            tmp.rmdir()
        except OSError:
            pass
        proc = subprocess.run(
            ["git", "-C", str(project), "worktree", "add", "--detach", str(tmp), head],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        if proc.returncode != 0:
            self.meta = {"isolation": "none",
                         "reason": f"worktree add failed: {proc.stderr.strip()[:200]}"}
            return project
        copied = 0
        for rel in changed_paths(project):
            src = project / rel
            dst = tmp / rel
            try:
                if src.is_file():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    copied += 1
                elif not src.exists() and dst.exists():
                    dst.unlink()
            except OSError:
                continue
        self.dir = tmp
        self.meta = {"isolation": "worktree", "path": str(tmp), "files": copied}
        return tmp

    def cleanup(self) -> None:
        if self.dir is None:
            return
        subprocess.run(
            ["git", "-C", str(self.project), "worktree", "remove", "--force", str(self.dir)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        subprocess.run(
            ["git", "-C", str(self.project), "worktree", "prune"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        if self.dir.exists():
            shutil.rmtree(self.dir, ignore_errors=True)
        self.dir = None
