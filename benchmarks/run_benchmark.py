"""Seeded-defect benchmark for the three-agent council.

Resets the fixture repo, applies the eight known defects, runs one full review
per tier, and reports recall / false blockers / time / cost. The fixture's own
tests pass on the seeded tree, so a green verify command proves nothing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

BENCH = Path(r"C:\Users\AgentPC\source\repos\council-benchmark")
AGENT_COUNCIL = Path(r"C:\Users\AgentPC\source\repos\agent-council")
TIERS = ("lean", "standard", "full")
RESULTS = AGENT_COUNCIL / "benchmarks" / "results.json"
KEPT = AGENT_COUNCIL / "benchmarks" / "runs"

DEFECTS = [
    {"id": "range-off-by-one", "file": "calc.py",
     "terms": ["off-by-one", "exclusive", "hi + 1", "upper bound", "range(lo, hi)",
               "half-open"]},
    {"id": "average-empty", "file": "calc.py",
     "terms": ["empty", "len(values)", "zero values", "average([])"]},
    {"id": "paginate-overlap", "file": "calc.py",
     "terms": ["overlap", "per_page", "extra item", "one extra"]},
    {"id": "group-aliasing", "file": "calc.py",
     "terms": ["alias", "shallow", "same list", "returns rows", "caller's own",
               "instead of a copy", "corrupts the input", "mutating the result"]},
    {"id": "file-leak", "file": "calc.py",
     "terms": ["close", "leak", "file handle", "open("]},
    {"id": "mutable-default", "file": "calc.py",
     "terms": ["mutable default", "shared default", "default argument", "tags=[]",
               "created once", "tags: list[str]", "persist", "accumulate"]},
    {"id": "swallowed-error", "file": "calc.py",
     "terms": ["swallow", "returns none", "silent", "safe_divide"]},
    {"id": "unicode-slug", "file": "calc.py",
     "terms": ["unicode", "isascii", "non-ascii"]},
]


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(BENCH), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120)


def reset_and_seed() -> None:
    git("checkout", "--", ".")
    council = BENCH / ".council"
    # Each tier must be cold: lessons written by an earlier tier would turn the
    # benchmark into a test of hint-following.
    shutil.rmtree(council / "runs", ignore_errors=True)
    shutil.rmtree(council / "inbox", ignore_errors=True)
    for name in ("state.json", "pending.md", "running.json", "checklist.md", "latest"):
        try:
            (council / name).unlink()
        except OSError:
            pass
    proc = git("apply", "seeded.patch")
    if proc.returncode != 0:
        raise SystemExit(f"seeded.patch failed: {proc.stderr}")


def newest_run(started: float) -> Path | None:
    runs = BENCH / ".council" / "runs"
    if not runs.is_dir():
        return None
    candidates = []
    for run in runs.iterdir():
        meta = run / "run.json"
        if meta.is_file() and meta.stat().st_mtime >= started - 1:
            candidates.append((meta.stat().st_mtime, run))
    return max(candidates)[1] if candidates else None


def _file_matches(finding: dict) -> bool:
    path = str(finding.get("file", "")).replace("\\", "/").lower()
    return path.endswith("calc.py") or path.endswith("tests/test_calc.py")


def score(confirmed: list[dict]) -> tuple[list[str], list[str], list[str]]:
    """Match confirmed findings to seeded defects; duplicates do not inflate recall."""
    matched_ids: set[str] = set()
    false_blockers: list[str] = []
    for finding in confirmed:
        text = " ".join(str(finding.get(key, "")) for key in
                        ("issue", "evidence", "repro", "repro_output")).lower()
        hit = next((d for d in DEFECTS
                    if _file_matches(finding)
                    and any(term.lower() in text for term in d["terms"])), None)
        if hit is not None:
            matched_ids.add(hit["id"])
        elif finding.get("severity") in ("blocker", "major"):
            false_blockers.append(str(finding.get("issue", ""))[:120])
    missed = [d["id"] for d in DEFECTS if d["id"] not in matched_ids]
    return sorted(matched_ids), missed, false_blockers


def run_tier(tier: str) -> dict:
    reset_and_seed()
    started = time.time()
    proc = subprocess.run(
        [sys.executable, "-m", "council", "--project", str(BENCH),
         "review", "--tier", tier, "--json"],
        cwd=str(BENCH), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=1800,
    )
    elapsed = time.time() - started
    run = newest_run(started)
    if run is None:
        return {"tier": tier, "error": "no run dir", "stderr": proc.stderr[-1000:]}

    meta = json.loads((run / "run.json").read_text(encoding="utf-8"))
    confirmed, refuted = [], []
    cost = 0.0
    for line in (run / "events.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == "finalize":
            confirmed = event.get("confirmed") or []
            refuted = event.get("refuted") or []
        if event.get("cost_usd"):
            cost += float(event["cost_usd"])

    matched, missed, false_blockers = score(confirmed)
    row = {
        "tier": tier,
        "tier_reason": meta.get("tier_reason"),
        "decision": "block" if proc.returncode == 2 else (
            "approve" if proc.returncode == 0 else "error"),
        "exit": proc.returncode,
        "recall": f"{len(matched)}/{len(DEFECTS)}",
        "matched": matched,
        "missed": missed,
        "confirmed": len(confirmed),
        "refuted": len(refuted),
        "false_blockers": false_blockers,
        "seconds": round(elapsed, 1),
        "cost_usd": round(cost, 4),
        "run": run.name if run else "",
    }
    # The fixture's .council is wiped before the next tier; keep this run's
    # evidence so --recompute can rescore without another model call.
    kept = KEPT / f"{tier}-{run.name}"
    try:
        if kept.exists():
            shutil.rmtree(kept)
        shutil.copytree(run, kept)
        row["kept"] = str(kept)
    except OSError:
        pass
    return row


def _matches_text(text: str) -> bool:
    lowered = (text or "").lower()
    return any(term.lower() in lowered
               for defect in DEFECTS for term in defect["terms"])


def recompute() -> int:
    rows = json.loads(RESULTS.read_text(encoding="utf-8"))
    for row in rows:
        events = None
        if row.get("kept"):
            events = Path(row["kept"]) / "events.jsonl"
        elif row.get("run"):
            events = BENCH / ".council" / "runs" / row["run"] / "events.jsonl"
        try:
            text = events.read_text(encoding="utf-8") if events else ""
        except OSError:
            # Old runs are gone; the stored unmatched texts still let us drop
            # duplicates of defects that were already matched.
            row["false_blockers"] = [t for t in (row.get("false_blockers") or [])
                                     if not _matches_text(t)]
            print(json.dumps({k: row.get(k) for k in
                              ("tier", "recall", "confirmed", "false_blockers")},
                             ensure_ascii=False))
            continue
        confirmed = []
        for line in text.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") == "finalize":
                confirmed = event.get("confirmed") or []
        matched, missed, false_blockers = score(confirmed)
        row["recall"] = f"{len(matched)}/{len(DEFECTS)}"
        row["matched"] = matched
        row["missed"] = missed
        row["confirmed"] = len(confirmed)
        row["false_blockers"] = false_blockers
        print(json.dumps({k: row.get(k) for k in
                          ("tier", "recall", "confirmed", "false_blockers")},
                         ensure_ascii=False))
    RESULTS.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"rescored: {RESULTS}")
    return 0


def main() -> int:
    if "--recompute" in sys.argv:
        return recompute()
    if not (BENCH / "seeded.patch").is_file():
        raise SystemExit(f"no seeded.patch in {BENCH}")
    if RESULTS.exists():
        archive = RESULTS.with_name("results-first-pass.json")
        if not archive.exists():
            archive.write_text(RESULTS.read_text(encoding="utf-8"), encoding="utf-8")
    results = []
    for tier in TIERS:
        print(f"== {tier} ==", flush=True)
        row = run_tier(tier)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        results.append(row)
    git("checkout", "--", ".")
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"results: {RESULTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
