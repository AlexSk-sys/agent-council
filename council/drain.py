"""drain: close confirmed findings for real, with proof.

A review that only reports leaves a growing checklist behind (31 entries and
climbing on this machine). `drain` takes the confirmed blocker/major findings
of a finished run and, for each one:

1. runs its REPRO in the live tree - exit 0 means the defect is already gone;
2. hands it to one executor, with the repro as the acceptance test;
3. re-runs the same REPRO - exit 0 means fixed, anything else means still open;
4. runs the project verify command after every fix, so a fix cannot trade one
   failure for another.

Nothing is called fixed because a model said so. The repro flipping from
non-zero to zero is the only proof accepted here, and a finding without a
repro is reported as "not drainable" instead of being silently dropped.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import project as project_mod
from . import prompts
from .adapters import run_agent
from .protocol import AgentConfig, Finding, _configs, _finding_line, finding_key
from .store import RunStore

BLOCKING = ("blocker", "major")


def _finding_from_dict(raw: dict) -> Finding:
    try:
        line = int(raw.get("line") or 0)
    except (TypeError, ValueError):
        line = 0
    return Finding(
        severity=str(raw.get("severity") or "minor"),
        issue=str(raw.get("issue") or ""),
        evidence=str(raw.get("evidence") or ""),
        source=str(raw.get("source") or "council"),
        file=str(raw.get("file") or ""),
        line=line,
        fix=str(raw.get("fix") or ""),
        repro=str(raw.get("repro") or ""),
        repro_output=str(raw.get("repro_output") or ""),
        status=str(raw.get("status") or "confirmed"),
        checks=list(raw.get("checks") or []),
    )


def load_confirmed(run_dir: Path) -> list[Finding]:
    """Confirmed blocker/major findings of the last finalize event in a run."""
    events = Path(run_dir) / "events.jsonl"
    try:
        lines = events.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    latest: list[Finding] = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("event") != "finalize":
            continue
        latest = [_finding_from_dict(raw) for raw in event.get("confirmed") or []]
    seen: dict[str, Finding] = {}
    for finding in latest:
        if finding.severity not in BLOCKING or not finding.issue:
            continue
        seen.setdefault(finding_key(finding), finding)
    return list(seen.values())


def _executor_config(executor: str, overrides: dict | None,
                     prompt_dir: Path) -> AgentConfig:
    # drain exists to edit: a project override that pins the executor to
    # read_only would turn every fix into a silent no-op.
    raw = dict((overrides or {}).get(executor) or {})
    raw["posture"] = "autonomous"
    cfg = _configs([executor], {executor: raw}, "autonomous")[executor]
    cfg.prompt_dir = str(prompt_dir)
    return cfg


def run_drain(
    project: Path,
    run_dir: Path,
    executor: str = "claude",
    overrides: dict | None = None,
    max_items: int = 10,
    dry_run: bool = False,
    on_done=None,
) -> dict:
    """Close confirmed findings one by one; return a per-item report."""
    project = Path(project).resolve()
    run_dir = Path(run_dir)
    findings = load_confirmed(run_dir)
    store = RunStore(project, run_id=run_dir.name)
    options = project_mod.pipeline_options(project)
    cfg = _executor_config(executor, overrides, store.dir / "prompts")
    store.log({"event": "drain_start", "executor": executor, "dry_run": dry_run,
               "items": len(findings), "run": run_dir.name})

    report: dict = {"executor": executor, "dry_run": dry_run, "items": [],
                    "fixed": 0, "already": 0, "open": 0, "skipped": 0,
                    "verify_failed": False, "stopped": ""}
    attempted = 0
    for finding in findings:
        item = {"key": finding_key(finding), "file": finding.file,
                "line": finding.line, "severity": finding.severity,
                "issue": finding.issue[:300], "state": ""}
        if not finding.repro:
            item["state"] = "skipped: no repro to prove the fix"
            report["skipped"] += 1
        elif not project_mod.repro_allowed(finding.repro):
            item["state"] = "skipped: repro command not allowed"
            report["skipped"] += 1
        else:
            before = project_mod.run_command(
                finding.repro, project, project_mod.REPRO_TIMEOUT_S)
            item["repro_before"] = before["code"]
            if before["code"] == 0:
                item["state"] = "already fixed: repro exits 0"
                report["already"] += 1
            elif dry_run:
                item["state"] = "would fix"
                report["open"] += 1
            else:
                attempted += 1
                item.update(_fix_one(project, store, cfg, executor, finding, item,
                                     options, on_done))
                if item["state"] == "fixed":
                    report["fixed"] += 1
                elif item["state"] == "verify failed":
                    report["open"] += 1
                    report["verify_failed"] = True
                else:
                    report["open"] += 1
        report["items"].append(item)
        store.log({"event": "drain_item", **item})
        if report["verify_failed"]:
            report["stopped"] = "verify command failed after a fix; stopped"
            break
        if item.get("state", "").startswith("error:"):
            report["stopped"] = "executor failed; stopped"
            break
        if attempted >= max_items:
            report["stopped"] = f"max_items={max_items} reached"
            break

    store.write("drained.md", render_report(report))
    store.log({"event": "drain_end", "fixed": report["fixed"],
               "already": report["already"], "open": report["open"],
               "skipped": report["skipped"]})
    return report


def _fix_one(project: Path, store: RunStore, cfg: AgentConfig, executor: str,
             finding: Finding, item: dict, options: dict, on_done) -> dict:
    brief = (_finding_line(finding)
             + f"\n  REPRO (the acceptance test, must exit 0 when fixed): {finding.repro}")
    result = run_agent(
        executor,
        prompts.fix_prompt(executor, str(project), [brief], True),
        str(project),
        cfg,
    )
    if on_done:
        on_done(result)
    store.write(f"drain/{_safe(item['key'])}.md",
                (result.text or f"ERROR: {result.error}") + "\n")
    store.log({"event": "drain_fix", **result.to_dict()})
    if not result.ok:
        return {"state": f"error: executor failed: {result.error[:160]}"}

    after = project_mod.run_command(finding.repro, project, project_mod.REPRO_TIMEOUT_S)
    item["repro_after"] = after["code"]
    if after["code"] != 0:
        return {"state": "still open: repro still exits non-zero",
                "repro_output": after["output"][-400:]}

    if options.get("verify"):
        failures = [r for r in project_mod.run_verify(project) if r.get("code") != 0]
        if failures:
            item["verify"] = [
                {"command": r.get("command"), "output": (r.get("output") or "")[-300:]}
                for r in failures
            ]
            return {"state": "verify failed"}
    return {"state": "fixed"}


def _safe(key: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in key).strip("-")[:60] or "item"


def render_report(report: dict) -> str:
    lines = ["# council drain", "",
             f"**executor:** {report['executor']}"
             + (" (dry run)" if report["dry_run"] else ""), "",
             f"**fixed:** {report['fixed']} · **already fixed:** {report['already']} · "
             f"**still open:** {report['open']} · **skipped:** {report['skipped']}", ""]
    if report.get("stopped"):
        lines += [f"**stopped:** {report['stopped']}", ""]
    lines += ["| state | where | issue |", "|---|---|---|"]
    for item in report["items"]:
        loc = f"{item['file']}:{item['line']}" if item["file"] else "project"
        lines.append(f"| {item['state']} | `{loc}` | {item['issue']} |")
    lines += ["", "A finding is **fixed** only when its own REPRO flips from non-zero "
                  "to zero and the verify command stays green."]
    return "\n".join(lines)


def exit_code(report: dict) -> int:
    """0 only when every finding is fixed or provably gone; skipped is not clear."""
    if report.get("stopped", "").startswith("executor failed"):
        return 1
    return 2 if (report.get("open") or report.get("skipped")) else 0
