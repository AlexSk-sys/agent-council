"""Claude Code hook entry points: review every change without blocking work.

`stop` runs a background cross-review whenever the working tree changed since
the last review. It never blocks a turn and never fails one: a review pipeline
that can take the editor down with it is worse than no pipeline.

`prompt` surfaces the last background verdict on the next user turn, so a
finding cannot sit unread in a run directory.

Both hooks are deliberately dumb and fast. All model work happens in the
detached child (`council review --background`).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

STATE_REL = Path(".council") / "state.json"
PENDING_REL = Path(".council") / "pending.md"
DEFAULT_COOLDOWN_S = 300


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:32]


def state_path(project: Path) -> Path:
    return Path(project) / STATE_REL


def load_state(project: Path) -> dict:
    try:
        return json.loads(state_path(project).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(project: Path, state: dict) -> None:
    path = state_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def scope_key(staged: bool, ref: str | None, path: str | None) -> str:
    scope = "staged" if staged else (f"ref:{ref}" if ref else "worktree")
    return f"{scope}:{path}" if path else scope


def _spawn_review(project: Path) -> None:
    # --project is a top-level flag and MUST precede the subcommand; after it,
    # argparse rejects the whole call. Under DEVNULL that failure is invisible -
    # this exact ordering bug killed the first background review silently.
    args = [sys.executable, "-m", "council", "--project", str(project),
            "review", "--background"]
    log_dir = Path(project) / ".council"
    log_dir.mkdir(parents=True, exist_ok=True)
    kwargs: dict = {
        "cwd": str(project),
        "stdin": subprocess.DEVNULL,
        "stderr": subprocess.STDOUT,
        "close_fds": True,
    }
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: survive this turn.
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    with (log_dir / "background.log").open("a", encoding="utf-8") as log:
        kwargs["stdout"] = log
        subprocess.Popen(args, **kwargs)


def _tail_lines(path: Path, max_bytes: int = 400_000, max_lines: int = 400) -> list[str]:
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(-max_bytes, os.SEEK_END)
            data = fh.read()
    except OSError:
        return []
    return data.decode("utf-8", "replace").splitlines()[-max_lines:]


def _edited_paths(lines: list[str]) -> list[Path]:
    """File paths the session wrote to, newest first."""
    found: list[Path] = []
    for line in reversed(lines):
        if '"file_path"' not in line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = (obj.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            raw = part.get("input")
            if isinstance(raw, dict):
                candidate = raw.get("file_path") or raw.get("path")
                if isinstance(candidate, str) and candidate:
                    found.append(Path(candidate))
    return found[:20]


def _repo_of(path: Path) -> Path | None:
    start = path if path.is_dir() else path.parent
    try:
        proc = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except OSError:
        return None
    if proc.returncode == 0 and proc.stdout.strip():
        return Path(proc.stdout.strip())
    return None


def resolve_project(payload: dict, cwd: Path) -> Path | None:
    """Which repository this session is actually working on.

    A session opened in a folder that is not a repo (the home directory, a
    scratch folder) still edits real repositories. The hook payload carries the
    transcript, so the repository is resolved from the files the session wrote,
    newest first, instead of being guessed from cwd.
    """
    if _repo_of(cwd):
        return Path(cwd).resolve()
    transcript = payload.get("transcript_path")
    if not transcript:
        return None
    for candidate in _edited_paths(_tail_lines(Path(transcript))):
        repo = _repo_of(candidate)
        if repo:
            return repo.resolve()
    return None


def stop_hook(project: Path, cooldown_s: int = DEFAULT_COOLDOWN_S) -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError):
        payload = {}
    if payload.get("stop_hook_active"):
        return 0

    resolved = resolve_project(payload, Path(project))
    if resolved is None:
        return 0  # nothing this session touched lives in a git repo
    project = resolved

    from .gitutil import collect_diff

    try:
        diff, _ = collect_diff(project)
    except Exception:
        return 0
    if not diff.strip():
        return 0

    fp = fingerprint(diff)
    state = load_state(project)
    if (state.get("worktree") or {}).get("fingerprint") == fp:
        return 0  # this exact change was already reviewed
    now = time.time()
    if now - float(state.get("last_spawn") or 0) < cooldown_s:
        return 0  # editing in progress; wait for a quieter moment

    state["last_spawn"] = now
    save_state(project, state)
    try:
        _spawn_review(project)
    except OSError as exc:
        print(f"council: could not start background review: {exc}", file=sys.stderr)
    return 0


def prompt_hook(project: Path) -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError):
        payload = {}
    resolved = resolve_project(payload, Path(project))
    pending = Path(resolved or project) / PENDING_REL
    if not pending.exists():
        return 0
    try:
        text = pending.read_text(encoding="utf-8")
    except OSError:
        return 0
    try:
        pending.unlink()
    except OSError:
        pass
    if text.strip():
        print(text.strip())
    return 0


def write_pending(project: Path, decision: str, findings, run_dir: Path) -> None:
    from .protocol import SEVERITY_ORDER

    ranked = sorted(
        [f for f in findings if f.has_evidence],
        key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.source),
    )
    lines = [f"[background council review] {decision.upper()}: "
             f"{len(ranked)} finding(s) with evidence"]
    for f in ranked[:6]:
        loc = f" {f.file}:{f.line}" if f.file else ""
        lines.append(f"- [{f.severity}]{loc} {f.issue} -- {f.evidence[:180]} ({f.source})")
    lines.append(f"run: {run_dir}")
    pending = Path(project) / PENDING_REL
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text("\n".join(lines) + "\n", encoding="utf-8")
