"""Claude Code hook entry points: the council joins the session as it happens.

Two hooks, both deliberately dumb and fast; all model work happens in a
detached child:

`stop` runs a background cross-review whenever the working tree changed since
the last review.

`prompt` fires on every user message: it surfaces the previous verdict and
starts a three-agent session consult on the message that was just typed, so
the council is present from the first turn instead of only after a diff
appears. A shared lock means a consult and a diff review never run at once -
six model calls on the same tree would be pure waste.

Neither hook blocks a turn or fails one: a review pipeline that can take the
editor down with it is worse than no pipeline.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .win import kwargs as _no_window

STATE_REL = Path(".council") / "state.json"
PENDING_REL = Path(".council") / "pending.md"
LOCK_REL = Path(".council") / "running.json"
INBOX_REL = Path(".council") / "inbox"
_REVIEWED_RE = re.compile(r"reviewed ([0-9a-f]{7,40})")
DEFAULT_COOLDOWN_S = 300
DEFAULT_PROMPT_COOLDOWN_S = 30
MAX_RUN_S = 3600  # verify + rounds of 1200 s agent timeouts can outlive 1800 s

COUNCIL_GITIGNORE = """*
!.gitignore
!config.json
"""


def ensure_council_gitignore(project: Path) -> None:
    """Keep the council's runtime files out of the user's commits.

    `.council/inbox/` can hold a raw user message, and state/lock/log files are
    local noise. The file is self-contained, so it protects projects that never
    ran `council init` too.
    """
    root = Path(project) / ".council"
    target = root / ".gitignore"
    if target.exists():
        return
    try:
        root.mkdir(parents=True, exist_ok=True)
        target.write_text(COUNCIL_GITIGNORE, encoding="utf-8")
    except OSError:
        pass


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:32]


def state_path(project: Path) -> Path:
    return Path(project) / STATE_REL


def load_state(project: Path) -> dict:
    """Read state, retrying once: a concurrent replace can win a read race.

    Returning {} after a transient failure is dangerous, because callers merge
    into what they loaded and save it back - one bad read used to erase the
    worktree verdict the Stop hook depends on.
    """
    path = state_path(project)
    result: dict = {}
    for attempt in (0, 1):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            result = {}
            if not attempt:
                time.sleep(0.05)
    return result


def save_state(project: Path, state: dict) -> None:
    path = state_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_council_gitignore(project)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------
# one council at a time
# --------------------------------------------------------------------------


def lock_path(project: Path) -> Path:
    return Path(project) / LOCK_REL


def _as_pid(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _is_windows() -> bool:
    return os.name == "nt"


def _pid_alive(pid: int) -> bool:
    """Cheap, non-destructive liveness check; never signals the process."""
    if pid <= 0:
        return False
    if _is_windows():
        import ctypes

        synchronize = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        try:
            alive = ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 0x00000102
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
        return alive
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def council_busy(project: Path, ttl_s: float = MAX_RUN_S) -> bool:
    """True while a consult or a review is running in this project.

    A crashed child leaves its lock behind. The pid in the lock answers that
    in one cheap check; the TTL is the fallback for a lock written by a child
    that never got a pid (or by older code) and is longer than any harness
    timeout (1200 s).
    """
    path = lock_path(project)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        started = float(data.get("started_at") or 0)
    except (OSError, json.JSONDecodeError, ValueError):
        # A lock caught mid-write is not a dead lock; only a parse failure
        # older than a few seconds means the writer died.
        try:
            return time.time() - path.stat().st_mtime < 10
        except OSError:
            return False
    pid = _as_pid(data.get("pid"))
    if pid:
        # A live child owns the lock for as long as it lives, even past the
        # TTL: unlinking it there let a second council run on the same tree
        # while the first was still writing state and pending.md.
        if _pid_alive(pid):
            return True
        try:
            path.unlink()
        except OSError:
            pass
        return False
    if time.time() - started >= ttl_s:
        try:
            path.unlink()
        except OSError:
            pass
        return False
    return True


def acquire_lock(project: Path, kind: str, token: str, pid: int = 0, **extra) -> bool:
    """Atomically take the lock unless a live council run already holds it.

    The create is O_EXCL, so two hooks racing on the same project cannot both
    win; the loser reads a fresh lock and backs off. Only a stale lock (dead
    pid or expired TTL) is removed, and only then is the create retried once.
    """
    path = lock_path(project)
    payload = {"kind": kind, "token": token, "pid": _as_pid(pid),
               "started_at": time.time(), **extra}
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_council_gitignore(project)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if council_busy(project):
            return False
        try:
            path.unlink()
        except OSError:
            return False
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            return False
    except OSError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    except OSError:
        return False
    return True


def release_lock(project: Path, token: str | None = None) -> None:
    """Release only the lock this run created; never another run's."""
    path = lock_path(project)
    if token is not None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if data.get("token") != token:
            return
    try:
        path.unlink()
    except OSError:
        pass


def _write_lock(project: Path, payload: dict) -> None:
    path = lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def set_lock_pid(project: Path, token: str, pid: int) -> None:
    """Record the child's pid after the spawn, so a dead child is noticed."""
    path = lock_path(project)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if data.get("token") != token:
        return
    data["pid"] = _as_pid(pid)
    _write_lock(project, data)


def scope_key(staged: bool, ref: str | None, path: str | None) -> str:
    scope = "staged" if staged else (f"ref:{ref}" if ref else "worktree")
    return f"{scope}:{path}" if path else scope


def _spawn_detached(project: Path, args: list[str]) -> int:
    # --project is a top-level flag and MUST precede the subcommand; after it,
    # argparse rejects the whole call. Under DEVNULL that failure is invisible -
    # this exact ordering bug killed the first background review silently.
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
        # detached: DETACHED_PROCESS already means no console; no extra flag.
        proc = subprocess.Popen(args, **kwargs)
    return _as_pid(getattr(proc, "pid", 0))


def _spawn_review(project: Path, lock_token: str | None = None) -> int:
    args = [sys.executable, "-m", "council", "--project", str(project),
            "review", "--background"]
    if lock_token:
        args += ["--lock-token", lock_token]
    return _spawn_detached(project, args)


def _spawn_consult(project: Path, prompt_file: Path,
                   transcript: str | None = None,
                   lock_token: str | None = None) -> int:
    args = [sys.executable, "-m", "council", "--project", str(project),
            "consult", "--prompt-file", str(prompt_file), "--background"]
    if transcript:
        args += ["--transcript", str(transcript)]
    if lock_token:
        args += ["--lock-token", lock_token]
    return _spawn_detached(project, args)


def read_payload() -> dict:
    """Parse the hook payload, tolerating a UTF-8 BOM on stdin.

    PowerShell adds a BOM when it pipes a string into a native command, and
    json.loads refuses it. Failing there sends the hook down the "no payload"
    path, which looks exactly like a healthy no-op - verified while simulating
    a Stop event from a shell.
    """
    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig", "replace")
    except (AttributeError, OSError):
        try:
            raw = sys.stdin.read()
        except OSError:
            return {}
    try:
        return json.loads(raw.strip() or "{}")
    except json.JSONDecodeError:
        return {}


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


def transcript_digest(path: Path, max_entries: int = 30,
                      max_chars: int = 12000) -> str:
    """Compress the session transcript into what a peer needs to be useful.

    Tool results are dropped: they are most of the bytes and none of the
    intent. User text, assistant text, and tool calls (name + target) survive,
    oldest first, so the consult knows what the session is actually doing.
    """
    entries: list[str] = []
    for line in _tail_lines(Path(path), max_bytes=1_000_000, max_lines=600):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = obj.get("message") or {}
        role = message.get("role") or obj.get("type") or "?"
        content = message.get("content")
        parts: list[str] = []
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "text":
                    parts.append(part.get("text") or "")
                elif ptype == "tool_use":
                    raw = part.get("input") or {}
                    target = (raw.get("file_path") or raw.get("path")
                              or raw.get("command") or "")
                    parts.append(f"[{part.get('name', 'tool')} {str(target)[:160]}]")
        text = " ".join(p for p in parts if p).strip()
        if text:
            entries.append(f"[{role}] {text[:1200]}")
    digest = "\n".join(entries[-max_entries:])
    if len(digest) > max_chars:
        digest = "..." + digest[-max_chars:]
    return digest


def _repo_of(path: Path) -> Path | None:
    start = path if path.is_dir() else path.parent
    try:
        proc = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            **_no_window(),
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


def stop_hook(project: Path, cooldown_s: float = DEFAULT_COOLDOWN_S,
              max_run_s: float = MAX_RUN_S) -> int:
    payload = read_payload()
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
    reviewed = (state.get("worktree") or {}).get("fingerprint")
    due = state.get("review_due")
    if reviewed == fp:
        if due == fp:
            state.pop("review_due", None)
            save_state(project, state)
        return 0  # this exact change was already reviewed by a consult or review

    if council_busy(project, max_run_s):
        # Defer, never drop: the next free Stop reviews this fingerprint.
        if due != fp:
            state["review_due"] = fp
            save_state(project, state)
        return 0

    now = time.time()
    due_now = due == fp
    if not due_now and now - float(state.get("last_spawn") or 0) < cooldown_s:
        return 0  # editing in progress; wait for a quieter moment

    token = uuid.uuid4().hex
    if not acquire_lock(project, "review", token):
        if due != fp:
            state["review_due"] = fp
            save_state(project, state)
        return 0
    try:
        pid = _spawn_review(project, token)
    except OSError as exc:
        release_lock(project, token)
        print(f"council: could not start background review: {exc}", file=sys.stderr)
        return 0
    set_lock_pid(project, token, pid)
    # Re-read before writing: the detached child may have saved its verdict
    # while this hook was spawning, and that verdict must not be erased.
    state = load_state(project)
    state["last_spawn"] = now
    state.pop("review_due", None)
    save_state(project, state)
    return 0


def _print_pending(project: Path) -> None:
    pending = Path(project) / PENDING_REL
    if not pending.exists():
        return
    # Consume atomically: rename first, print after. A writer appending between
    # the read and the delete would otherwise lose a verdict unread.
    consumed = pending.with_name(
        f"{pending.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.read"
    )
    replaced = False
    try:
        os.replace(pending, consumed)
        replaced = True
    except OSError:
        consumed = pending  # a writer holds it open: print in place, keep the file
    try:
        text = consumed.read_text(encoding="utf-8")
    except OSError:
        return
    stale = ""
    match = _REVIEWED_RE.search(text)
    if match:
        try:
            from .gitutil import head_commit

            current = head_commit(Path(project))
        except Exception:
            current = ""
        reviewed = match.group(1)
        if current and current not in ("no-commit", reviewed):
            stale = (f"[STALE: this verdict reviewed {reviewed}, "
                     f"HEAD is {current}; re-run the review]\n")
    try:
        if text.strip():
            print(stale + text.strip())
    except Exception:
        # A broken stdout must not destroy a verdict no one has read: put the
        # file back under its original name and let the next turn retry.
        if replaced:
            try:
                os.replace(consumed, pending)
            except OSError:
                pass
        raise
    if replaced:
        _unlink_quiet(consumed)


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _write_inbox(base: Path, message: str) -> Path | None:
    try:
        inbox = base / INBOX_REL
        inbox.mkdir(parents=True, exist_ok=True)
        ensure_council_gitignore(base)
        prompt_file = inbox / (
            f"prompt-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.md"
        )
        prompt_file.write_text(message, encoding="utf-8")
    except OSError:
        return None
    return prompt_file


def _queue_prompt(base: Path, message: str, transcript: str | None) -> None:
    """Latest message wins while a run is active; one queued file, not a pile."""
    prompt_file = _write_inbox(base, message)
    if prompt_file is None:
        return
    state = load_state(base)
    old = state.get("consult_queued") or {}
    old_file = Path(str(old.get("prompt_file") or ""))
    if str(old_file) and old_file != prompt_file:
        _unlink_quiet(old_file)
    state["consult_queued"] = {"prompt_file": str(prompt_file),
                               "transcript": transcript, "ts": time.time()}
    save_state(base, state)


def drain_queue(project: Path, max_run_s: float = MAX_RUN_S) -> bool:
    """Start the consult that was queued while another run held the lock.

    Called at the end of every background run, after its lock is released, so
    a message typed during a consult is not silently skipped.
    """
    project = Path(project)
    if council_busy(project, max_run_s):
        return False
    state = load_state(project)
    queued = state.pop("consult_queued", None)
    if not queued:
        return False
    save_state(project, state)
    prompt_file = Path(str(queued.get("prompt_file") or ""))
    if not prompt_file.is_file():
        return False
    token = uuid.uuid4().hex
    if not acquire_lock(project, "consult", token, prompt_file=str(prompt_file)):
        return False
    try:
        pid = _spawn_consult(project, prompt_file, queued.get("transcript"), token)
    except OSError as exc:
        release_lock(project, token)
        print(f"council: could not start queued consult: {exc}", file=sys.stderr)
        return False
    set_lock_pid(project, token, pid)
    state = load_state(project)
    state["consult_last_spawn"] = time.time()
    save_state(project, state)
    return True


def prompt_hook(project: Path, cooldown_s: float = DEFAULT_PROMPT_COOLDOWN_S,
                max_run_s: float = MAX_RUN_S) -> int:
    """Bring all three agents into the session as soon as the user writes.

    The hook fires on every user message and never blocks the turn:
    - nothing running: the message goes to a detached consult immediately;
    - a consult or review is running: the message is queued (latest wins) and
      the running child starts it on exit (`drain_queue`);
    - the 30 s cooldown only applies when no queued message is waiting.
    The verdict appears at the start of the next user turn.
    """
    payload = read_payload()
    resolved = resolve_project(payload, Path(project))
    base = Path(resolved or project)
    _print_pending(base)

    message = str(payload.get("prompt") or "").strip()
    if not message:
        return 0
    if council_busy(base, max_run_s):
        _queue_prompt(base, message, payload.get("transcript_path"))
        return 0

    state = load_state(base)
    queued = state.pop("consult_queued", None)
    if queued:
        _unlink_quiet(Path(str(queued.get("prompt_file") or "")))
        save_state(base, state)
    now = time.time()
    if not queued and now - float(state.get("consult_last_spawn") or 0) < cooldown_s:
        return 0

    prompt_file = _write_inbox(base, message)
    if prompt_file is None:
        return 0

    token = uuid.uuid4().hex
    if not acquire_lock(base, "consult", token, prompt_file=str(prompt_file)):
        _unlink_quiet(prompt_file)
        return 0
    try:
        pid = _spawn_consult(base, prompt_file, payload.get("transcript_path"), token)
    except OSError as exc:
        release_lock(base, token)
        print(f"council: could not start session consult: {exc}", file=sys.stderr)
        return 0
    set_lock_pid(base, token, pid)
    # The cooldown is recorded only after a successful spawn: a failed start
    # must not mute the next message.
    state = load_state(base)
    state["consult_last_spawn"] = now
    save_state(base, state)
    return 0


def _run_stamp(run_dir: Path) -> str:
    try:
        meta = json.loads((Path(run_dir) / "run.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    commit = str(meta.get("commit") or "")
    ts = float(meta.get("ts") or 0)
    if not commit:
        return ""
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "unknown time"
    return f" (reviewed {commit} @ {when})"


def pending_is_news(project: Path, decision: str, findings, run_dir: Path) -> bool:
    """True when a verdict tells the user something they have not been told.

    An advisory consult that only re-states the existing checklist is not a
    message: printing it on every conversational turn is what made the council
    feel like it produced a fresh pile of errors each time.
    """
    if decision != "advisory":
        return True
    from .protocol import finding_key
    from .project import lesson_index

    known = lesson_index(project, exclude_run=run_dir.name)
    return any(getattr(f, "status", "") == "confirmed" and finding_key(f) not in known
               for f in findings)


def write_pending(project: Path, decision: str, findings, run_dir: Path,
                  mode: str = "review") -> None:
    from .protocol import SEVERITY_ORDER, finding_key
    from .project import lesson_index

    confirmed = [f for f in findings if getattr(f, "status", "") == "confirmed"]
    # A defect already on the checklist is not news. Counting it as "new" on
    # every turn is what made the council feel like a source of fresh errors.
    known = lesson_index(project, exclude_run=run_dir.name)
    fresh = [f for f in confirmed if finding_key(f) not in known]
    old = [f for f in confirmed if finding_key(f) in known]
    pool = fresh or confirmed or [f for f in findings if f.has_evidence]
    ranked = sorted(
        pool,
        key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.source),
    )
    lines = [f"[background council {mode}] {decision.upper()}: "
             f"{len(fresh)} new, {len(old)} known open, "
             f"{len(findings)} finding(s) total{_run_stamp(run_dir)}"]
    for f in ranked[:6]:
        loc = f" {f.file}:{f.line}" if f.file else ""
        status = getattr(f, "status", "") or "unverified"
        if finding_key(f) in known:
            status += "/known"
        lines.append(f"- [{f.severity}/{status}]{loc} {f.issue} -- "
                     f"{f.evidence[:180]} ({f.source})")
    lines.append(f"run: {run_dir}")
    pending = Path(project) / PENDING_REL
    pending.parent.mkdir(parents=True, exist_ok=True)
    ensure_council_gitignore(project)
    # Append, never overwrite: a consult and a Stop review can both finish
    # between two user turns, and one verdict must not destroy the other.
    try:
        had_text = pending.exists() and pending.stat().st_size > 0
    except OSError:
        had_text = False
    with pending.open("a", encoding="utf-8") as fh:
        if had_text:
            fh.write("\n")
        fh.write("\n".join(lines) + "\n")
