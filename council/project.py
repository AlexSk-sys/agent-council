"""Project context for a review: verify command, impact, docs, lessons, snapshot.

Everything here is deterministic and model-free. The point is to give the
three agents facts (a test result, callers, doc claims, past failure classes)
instead of making them spend tokens discovering them, and to run the review
against a disposable snapshot rather than the live tree.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .win import kwargs as _no_window

VERIFY_TIMEOUT_S = 300
REPRO_TIMEOUT_S = 120
MAX_VERIFY_OUTPUT = 6000
MAX_IMPACT_CHARS = 12000
MAX_DOC_CHARS = 6000
MAX_LESSON_CHARS = 4000

REPRO_TOKENS = {
    "node", "python", "python3", "py", "pytest", "npm", "npx", "pnpm", "yarn",
    "bun", "deno", "dotnet", "go", "cargo", "git",
}
REPRO_DENY = (
    "rm -rf", "rm -fr", "del /s", "rd /s", "format ", "shutdown", "git push",
    "git reset --hard", "git clean", "git commit", "git stash", "git checkout --",
    "curl ", "wget ", "invoke-webrequest", "start-process", "reg delete",
    "takeown", "icacls",
)


def _git(project: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(project), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, **_no_window(),
        )
        return proc.stdout if proc.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def run_command(command: str, cwd: Path, timeout_s: int = VERIFY_TIMEOUT_S) -> dict:
    try:
        proc = subprocess.run(
            command, cwd=str(cwd), shell=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_s,
            stdin=subprocess.DEVNULL, **_no_window(),
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
    """Defaults are adaptive; a project can force a stage on or off.

    `cross` and `impact` default to "auto", which the tier decides. A boolean
    in `.council/config.json` overrides the tier.
    """
    raw = _config(project).get("pipeline") or {}
    return {
        "verify": raw.get("verify") if isinstance(raw.get("verify"), bool) else True,
        "cross": raw.get("cross") if isinstance(raw.get("cross"), bool) else "auto",
        "isolate": raw.get("isolate") if isinstance(raw.get("isolate"), bool) else True,
        "lessons": raw.get("lessons") if isinstance(raw.get("lessons"), bool) else True,
        "impact": raw.get("impact") if isinstance(raw.get("impact"), bool) else "auto",
        "docs": raw.get("docs") if isinstance(raw.get("docs"), bool) else "auto",
    }


CODE_EXT = (".py", ".js", ".jsx", ".ts", ".tsx", ".cs", ".go", ".rs", ".java",
            ".rb", ".php", ".c", ".cc", ".cpp", ".h", ".hpp", ".swift")
SECURITY_RE = re.compile(
    r"(auth|payment|billing|secret|token|migration|deploy|credential|permission|\.env)",
    re.I,
)
ACTION_RE = re.compile(
    r"(сдела|исправ|добав|удал|перепиш|запус|разверн|мигрир|"
    r"fix|implement|add|remove|refactor|deploy|migrate|delete|update|change)",
    re.I,
)


def _is_test_path(path: str) -> bool:
    low = path.lower()
    return "test" in low or "spec" in low


def choose_tier(project: Path, diff: str, message: str,
                verify_results: list[dict], trigger: str = "manual") -> tuple[str, str]:
    """Pick lean / standard / full from real signals, not from a fixed policy.

    lean     3 small calls, no impact/docs/cross - conversational turns
    standard verify + impact + round 1; cross only if a major is found
    full     everything, including cross regardless of findings
    """
    if trigger not in ("prompt", "stop"):
        return "full", "manual or pre-commit run"
    if any(res.get("code") != 0 for res in verify_results):
        return "standard", "verify failed; round 1 still reviews, cross is deferred"
    files = changed_paths(project)
    size = len(diff.strip())
    if size == 0 and not files:
        return "lean", "no uncommitted change"
    if size == 0:
        risky_binary = len(files) > 5 or any(SECURITY_RE.search(p) for p in files)
        return (("full", "changed files not representable in the text diff (large/binary)")
                if risky_binary
                else ("standard", "changed files not representable in the text diff"))
    risky = size > 8000 or len(files) > 5 or any(SECURITY_RE.search(p) for p in files)
    code_files = [p for p in files if p.lower().endswith(CODE_EXT)]
    test_gap = bool(code_files) and not any(_is_test_path(p) for p in files)
    actionable = bool(ACTION_RE.search(message or ""))

    if trigger == "prompt":
        if size == 0:
            return "lean", "no uncommitted change"
        if risky or (actionable and test_gap):
            return "full", "risky diff or actionable change without tests"
        if actionable or size > 1500:
            return "standard", "ordinary code change"
        return "lean", "small change, conversational turn"
    if trigger == "stop":
        if risky:
            return "full", "large or security-sensitive diff"
        return "standard", "ordinary diff"
    return "standard", "ordinary prompt-time review"


def apply_tier(options: dict, tier: str) -> dict:
    out = dict(options)
    if tier == "lean":
        out["impact"] = False
        out["docs"] = False
        if out.get("cross") == "auto":
            out["cross"] = False
    elif tier == "standard":
        out["impact"] = True
        out["docs"] = False
        if out.get("cross") == "auto":
            out["cross"] = "auto"
    else:
        out["impact"] = True
        out["docs"] = True
        if out.get("cross") == "auto":
            out["cross"] = True
    # An explicit boolean in the config wins over the tier.
    for key in ("impact", "cross", "docs"):
        if isinstance(options.get(key), bool):
            out[key] = options[key]
    return out


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


_LESSON_LOC = re.compile(r"loc:(.+?):(\d+):(?:blocker|major|minor|info)")
_LESSON_DATE = re.compile(r"\[(\d{4}-\d{2}-\d{2})\]")


def lesson_index(project: Path, exclude_run: str = "") -> dict[str, str]:
    """`file:line` -> the date it entered the checklist.

    Lets a report say "known open since 2026-11-15" instead of presenting an
    old defect as a new error on every turn. `exclude_run` keeps a run from
    recognising its own fresh findings as old.
    """
    path = lessons_path(project)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    index: dict[str, str] = {}
    for line in text.splitlines():
        if exclude_run and exclude_run in line:
            continue
        dated = _LESSON_DATE.search(line)
        when = dated.group(1) if dated else "earlier"
        for match in _LESSON_LOC.finditer(line):
            key = match.group(1).replace("\\", "/") + ":" + match.group(2)
            index.setdefault(key, when)
    return index


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
    """Run each finding's REPRO; non-zero exit confirms, zero refutes.

    A merged entry can carry `extra_repros` (two peers, two defects, one line):
    it is refuted only when every repro exits 0, and confirmed as soon as one
    of them fails.
    """
    for f in findings:
        repros = [r for r in [getattr(f, "repro", "")]
                  + list(getattr(f, "extra_repros", [])) if r]
        if not repros:
            if not f.status:
                f.status = "unverified"
            continue
        if any(not repro_allowed(r) for r in repros):
            f.status = "unverified"
            f.repro_output = "blocked: command not allowed"
            continue
        results = [run_command(r, Path(cwd), timeout_s) for r in repros]
        codes = [res["code"] for res in results]
        failed = next((res for res in results if res["code"] not in (0, -1)), None)
        f.repro_output = (failed or results[0])["output"][-2000:]
        if failed is not None:
            f.status = "confirmed"
        elif all(code == 0 for code in codes):
            f.status = "refuted"
        else:
            f.status = "unverified"


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
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, **_no_window(),
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
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, **_no_window(),
        )
        subprocess.run(
            ["git", "-C", str(self.project), "worktree", "prune"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, **_no_window(),
        )
        if self.dir.exists():
            shutil.rmtree(self.dir, ignore_errors=True)
        self.dir = None
