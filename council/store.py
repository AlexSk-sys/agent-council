"""Run storage: one directory per council run, append-only log.

Everything the three agents ever saw or produced lands on disk under
.council/runs/<run_id>/.  That directory is the only shared memory between
harnesses - Claude, DSH and OpenCode each keep private session stores, so a
run that is not written here is invisible to the other two.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

COUNCIL_DIR = ".council"


def _slug(text: str, limit: int = 40) -> str:
    cleaned = "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned[:limit].rstrip("-") or "run"


class RunStore:
    def __init__(self, project: Path, run_id: str | None = None):
        self.project = Path(project).resolve()
        self.root = self.project / COUNCIL_DIR
        if run_id is None:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            run_id = f"{stamp}-{_slug(self._pending_label)}"
        self.run_id = run_id
        self.dir = self.root / "runs" / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.root / "latest").write_text(run_id, encoding="utf-8")

    _pending_label = "run"

    @classmethod
    def for_task(cls, project: Path, task: str) -> "RunStore":
        cls._pending_label = _slug(task, 32)
        try:
            return cls(project)
        finally:
            cls._pending_label = "run"

    # -- writing -----------------------------------------------------------

    def write(self, relpath: str, text: str) -> Path:
        path = self.dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def log(self, event: dict) -> None:
        event = dict(event)
        event.setdefault("ts", time.time())
        with (self.dir / "events.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    def path(self, relpath: str) -> Path:
        return self.dir / relpath

    # -- metadata ----------------------------------------------------------

    def write_meta(self, meta: dict) -> None:
        meta = dict(meta)
        meta["run_id"] = self.run_id
        meta["project"] = str(self.project)
        self.write("run.json", json.dumps(meta, indent=2, ensure_ascii=False))

    @staticmethod
    def digest(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def latest_run(project: Path) -> Path | None:
    root = Path(project) / COUNCIL_DIR
    pointer = root / "latest"
    if not pointer.exists():
        return None
    run_id = pointer.read_text(encoding="utf-8").strip()
    candidate = root / "runs" / run_id
    return candidate if candidate.is_dir() else None
