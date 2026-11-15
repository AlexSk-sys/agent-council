"""Seeded-defect benchmark for the three-agent council.

Resets the fixture repo, applies the eight known defects, runs one full review
per tier, and reports recall / false blockers / time / cost. The fixture's own
tests pass on the seeded tree, so a green verify command proves nothing.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

BENCH = Path(r"C:\Users\AgentPC\source\repos\council-benchmark")
AGENT_COUNCIL = Path(r"C:\Users\AgentPC\source\repos\agent-council")
TIERS = ("lean", "standard", "full")
RESULTS = AGENT_COUNCIL / "benchmarks" / "results.json"

DEFECTS = [
    {"id": "range-off-by-one", "file": "calc.py",
     "terms": ["off-by-one", "exclusive", "hi + 1", "range(lo, hi)"]},
    {"id": "average-empty", "file": "calc.py",
     "terms": ["empty", "zerodivision", "len(values)", "zero values"]},
    {"id": "paginate-overlap", "file": "calc.py",
     "terms": ["overlap", "per_page", "extra item", "+ 1"]},
    {"id": "group-aliasing", "file": "calc.py",
     "terms": ["alias", "shallow", "shared", "same list", "returns rows"]},
    {"id": "file-leak", "file": "calc.py",
     "terms": ["close", "leak", "file handle", "open("]},
    {"id": "mutable-default", "file": "calc.py",
     "terms": ["mutable default", "shared default", "default argument", "tags: list"]},
    {"id": "swallowed-error", "file": "calc.py",
     "terms": ["swallow", "silent", "returns none", "zerodivision"]},
    {"id": "unicode-slug", "file": "calc.py",
     "terms": ["unicode", "ascii", "isalnum"]},
]


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(BENCH), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120)


def reset_and_seed() -> None:
    git("checkout", "--", ".")
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

    remaining = list(DEFECTS)
    matched = []
    false_blockers = []
    for finding in confirmed:
        text = " ".join(str(finding.get(k, "")) for k in
                        ("issue", "evidence", "repro", "repro_output")).lower()
        hit = None
        for defect in remaining:
            if "calc" in str(finding.get("file", "")) and \
                    any(term.lower() in text for term in defect["terms"]):
                hit = defect
                break
        if hit:
            remaining.remove(hit)
            matched.append(hit["id"])
        elif finding.get("severity") in ("blocker", "major"):
            false_blockers.append(finding.get("issue", "")[:120])

    return {
        "tier": tier,
        "tier_reason": meta.get("tier_reason"),
        "decision": "block" if proc.returncode == 2 else (
            "approve" if proc.returncode == 0 else "error"),
        "exit": proc.returncode,
        "recall": f"{len(matched)}/{len(DEFECTS)}",
        "matched": matched,
        "missed": [d["id"] for d in remaining],
        "confirmed": len(confirmed),
        "refuted": len(refuted),
        "false_blockers": false_blockers,
        "seconds": round(elapsed, 1),
        "cost_usd": round(cost, 4),
        "run": run.name if run else "",
    }


def main() -> int:
    if not (BENCH / "seeded.patch").is_file():
        raise SystemExit(f"no seeded.patch in {BENCH}")
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
