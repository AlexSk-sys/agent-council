"""Watch a project and record whether the hook chain actually fires.

This exists because "silence" has several causes - no change, no git repo, a
cooldown, a dead child - and they look identical from the outside. The watcher
writes a JSONL timeline of diff changes, state transitions, new run directories
and pending verdicts, so the question "did it run?" is answered by evidence
instead of by waiting and hoping.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .gitutil import collect_diff
from .hooks import PENDING_REL, fingerprint, load_state


def snapshot(project: Path) -> dict:
    project = Path(project)
    diff, _ = collect_diff(project)
    runs_dir = project / ".council" / "runs"
    runs = sorted(p.name for p in runs_dir.iterdir() if p.is_dir()) if runs_dir.is_dir() else []
    pending = project / PENDING_REL
    first_line = ""
    if pending.exists():
        try:
            first_line = pending.read_text(encoding="utf-8").splitlines()[0]
        except (OSError, IndexError):
            first_line = ""
    return {
        "diff_chars": len(diff),
        "fingerprint": fingerprint(diff) if diff.strip() else "",
        "state": load_state(project),
        "runs": runs,
        "pending": first_line,
    }


def watch(project: Path, interval: float = 10, duration: float = 7200,
          log_path: Path | None = None, on_event=None) -> Path:
    project = Path(project)
    log_path = Path(log_path or project / ".council" / "watch.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def emit(kind: str, detail: dict) -> None:
        record = {"ts": round(time.time(), 3), "event": kind, **detail}
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        if on_event:
            on_event(kind, detail)

    prev = snapshot(project)
    emit("start", {"diff_chars": prev["diff_chars"], "runs": len(prev["runs"]),
                   "duration_s": duration, "interval_s": interval})
    deadline = time.time() + duration
    try:
        while time.time() < deadline:
            time.sleep(interval)
            cur = snapshot(project)
            if cur["fingerprint"] != prev["fingerprint"]:
                emit("diff", {"chars": cur["diff_chars"], "fp": cur["fingerprint"][:8]})
            if cur["state"] != prev["state"]:
                worktree = cur["state"].get("worktree") or {}
                emit("state", {"decision": worktree.get("decision", ""),
                               "run": Path(worktree.get("run_dir", "")).name,
                               "last_spawn": cur["state"].get("last_spawn")})
            fresh = [r for r in cur["runs"] if r not in prev["runs"]]
            if fresh:
                emit("run", {"runs": fresh})
            if cur["pending"] and cur["pending"] != prev["pending"]:
                emit("pending", {"line": cur["pending"][:240]})
            prev = cur
    except KeyboardInterrupt:
        pass
    emit("stop", {})
    return log_path


def spawn_watch(project: Path, interval: float, duration: float) -> int:
    """Run the watcher detached so it survives this turn."""
    project = Path(project)
    council_dir = project / ".council"
    council_dir.mkdir(parents=True, exist_ok=True)
    log = council_dir / "watch.out"
    args = [sys.executable, "-m", "council", "--project", str(project), "watch",
            "--interval", str(interval), "--duration", str(duration)]
    kwargs: dict = {
        "cwd": str(project),
        "stdin": subprocess.DEVNULL,
        "stderr": subprocess.STDOUT,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    with log.open("a", encoding="utf-8") as fh:
        kwargs["stdout"] = fh
        proc = subprocess.Popen(args, **kwargs)
    (council_dir / "watch.pid").write_text(str(proc.pid), encoding="utf-8")
    return proc.pid


def stop_watch(project: Path) -> int:
    pid_file = Path(project) / ".council" / "watch.pid"
    if not pid_file.exists():
        return 0
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid_file.unlink(missing_ok=True)
        return 0
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, text=True) if os.name == "nt" else os.kill(pid, 15)
    except OSError:
        pass
    pid_file.unlink(missing_ok=True)
    return pid
