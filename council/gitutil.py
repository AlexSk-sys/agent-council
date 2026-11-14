"""Git helpers shared by the CLI and the work loop."""

from __future__ import annotations

import subprocess
from pathlib import Path


def git(project: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=project, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return proc.stdout if proc.returncode == 0 else ""


# The council's own run directory must never appear in the diff it reviews -
# a 334k-char "change" on a one-line fix was exactly this. Bytecode and pytest
# caches are the same class of noise.
EXCLUDE_PATHSPECS = (
    ":(exclude).council",
    ":(exclude).council/**",
    ":(exclude)**/.council/**",
    # `:(exclude)**/__pycache__` matches nothing in this git version; the
    # trailing /** form is the one that works (verified both ways).
    ":(exclude)__pycache__/**",
    ":(exclude)**/__pycache__/**",
    ":(exclude).pytest_cache/**",
    ":(exclude)**/.pytest_cache/**",
)
NOISE_PARTS = ("/.council/", "__pycache__/", "/.pytest_cache/")


def is_noise(relpath: str) -> bool:
    rel = "/" + relpath.replace("\\", "/").lstrip("/")
    if rel.startswith("/.council/"):
        return True
    return any(part in rel for part in NOISE_PARTS)


def collect_diff(project: Path, staged: bool = False, ref: str | None = None,
                 path: str | None = None) -> tuple[str, str]:
    """Return (diff, stat) for the requested scope, minus council/bytecode noise.

    Untracked files never appear in `git diff`, and a brand-new file is exactly
    the change that most needs review, so it is added as a full addition.
    """
    tail = ([path] if path else []) + list(EXCLUDE_PATHSPECS)
    if ref:
        diff = git(project, "diff", ref, "--", *tail)
        stat = git(project, "diff", "--stat", ref, "--", *tail).strip()
        return diff, stat
    if staged:
        diff = git(project, "diff", "--cached", "--", *tail)
        stat = git(project, "diff", "--cached", "--stat", "--", *tail).strip()
        return diff, stat

    diff = git(project, "diff", "--", *tail)
    stat = git(project, "diff", "--stat", "--", *tail).strip()

    untracked = git(project, "ls-files", "--others", "--exclude-standard").splitlines()
    if path:
        untracked = [u for u in untracked if u == path or u.startswith(path.rstrip("/") + "/")]
    for rel in untracked:
        if is_noise(rel):
            continue
        fpath = project / rel
        if not fpath.is_file() or fpath.stat().st_size > 200_000:
            continue
        try:
            body = fpath.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        diff += f"\n--- /dev/null\n+++ b/{rel}\n" + "".join(
            "+" + ln + "\n" for ln in body.splitlines()
        )
        stat += f"\n {rel} | new file"
    return diff, stat.strip()


def head_commit(project: Path) -> str:
    return git(project, "rev-parse", "--short", "HEAD").strip() or "no-commit"
