"""The council protocol: propose, cross-review, merge.

Three harnesses are asked the same thing independently, then each is shown the
other two answers and told to attack them. The merge step is deliberately
deterministic - no model gets to declare consensus, because a model asked to
summarise three opinions will invent agreement that is not there.
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from . import prompts
from . import project as project_mod
from .adapters import AGENTS, POSTURES, AgentConfig, AgentResult, run_agent
from .gitutil import collect_diff, head_commit
from .store import RunStore

MIN_AGENTS = 2
SEVERITY_ORDER = {"blocker": 0, "major": 1, "minor": 2}


# --------------------------------------------------------------------------
# tolerant JSON extraction
# --------------------------------------------------------------------------


def extract_json_objects(text: str) -> list:
    """Pull every top-level JSON object out of free-form model output.

    Models wrap JSON in prose, fences, or trailing commas. A plain
    json.loads() on the whole reply fails in every one of those cases, so we
    scan for balanced braces and skip quoted strings while doing it.
    """
    out: list = []
    i, n = 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth, in_str, esc, start = 0, False, False, i
        j = i
        while j < n:
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        chunk = text[start : j + 1]
                        try:
                            out.append(json.loads(chunk))
                        except json.JSONDecodeError:
                            fixed = _strip_trailing_commas(chunk)
                            try:
                                out.append(json.loads(fixed))
                            except json.JSONDecodeError:
                                pass
                        i = j
                        break
            j += 1
        i += 1
    return out


def _strip_trailing_commas(text: str) -> str:
    out, in_str, esc = [], False, False
    for idx, ch in enumerate(text):
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            continue
        if ch == ",":
            rest = text[idx + 1 :].lstrip()
            if rest[:1] in ("}", "]"):
                continue
        out.append(ch)
    return "".join(out)


# Three output strategies, tried in order. Models do not reliably follow an
# "output JSON only" instruction when the task is "write a code review" - two
# out of three answered in markdown on the first real run, and both had found a
# genuine blocker. Discarding that because the format slipped is unacceptable,
# so the parser accepts a strict line format, plain JSON, and markdown headings.

_LINE_VERDICT = re.compile(r"^\s*VERDICT\s*:\s*(APPROVE|BLOCK)\b", re.I | re.M)
_LINE_FINDING = re.compile(r"^\s*FINDING\s*:\s*(.+?)\s*$", re.I | re.M)
_LINE_REPRO = re.compile(r"^\s*REPRO\s*:\s*(.+?)\s*$", re.I | re.M)
_LINE_CHECK = re.compile(r"^\s*CHECK\s*:\s*(\d+)\s*\|\s*(CONFIRM|REFUTE)\s*\|\s*(.*)$", re.I)
_LINE_NEW = re.compile(r"^\s*NEW\s*:\s*(.+?)\s*$", re.I)
_LINE_REVIEW = re.compile(r"^\s*REVIEW\s*:\s*(.+?)\s*$", re.I | re.M)
_LINE_FIX = re.compile(r"^\s*FIX\s*:\s*(.+?)\s*$", re.I | re.M)
_LOCATION = re.compile(r"([A-Za-z0-9_./\\-]+\.[A-Za-z0-9]{1,6}):(\d+)")
_HEADING = re.compile(r"^\s*#{2,4}\s")
_SEVERITY_WORD = re.compile(r"\b(blocker|critical|major|minor)\b", re.I)


def _unquote(text: str) -> str:
    return text.strip().strip("`'\"*_ ").strip()


def _split_fields(line: str, count: int) -> list[str]:
    return [p.strip() for p in line.split("|", count)]


def _finding_from_line(line: str, source: str) -> Finding | None:
    """`severity | file:line | issue | evidence`"""
    parts = _split_fields(line, 3)
    if not parts or not parts[0]:
        return None
    severity = _unquote(parts[0]).lower()
    if severity not in SEVERITY_ORDER:
        severity = "minor"
    loc_text = _unquote(parts[1]) if len(parts) > 1 else ""
    match = _LOCATION.search(loc_text)
    issue = parts[2].strip() if len(parts) > 2 else loc_text
    evidence = parts[3].strip() if len(parts) > 3 else ""
    if not issue and not evidence:
        return None
    # The record's own file:line is itself checkable, so it counts as evidence
    # when the reviewer did not quote a command.
    if not evidence and match:
        evidence = match.group(0)
    return Finding(
        severity=severity,
        issue=issue[:400],
        evidence=evidence,
        source=source,
        file=match.group(1) if match else "",
        line=int(match.group(2)) if match else 0,
    )


def _markdown_findings(text: str, source: str) -> list[Finding]:
    """Salvage findings from a human-style review with `### ... [BLOCKER]` headings.

    The severity word may sit anywhere in the heading - `### 1. Off-by-one
    (blocker)` and `### [blocker] Off-by-one` both occur in practice.
    """
    out: list[Finding] = []
    for block in re.split(r"\n(?=#{2,4}\s)", text):
        first = next((ln for ln in block.splitlines() if ln.strip()), "")
        if not _HEADING.match(first):
            continue
        sev_match = _SEVERITY_WORD.search(first)
        if not sev_match:
            continue
        severity = sev_match.group(1).lower()
        if severity == "critical":
            severity = "blocker"
        loc = _LOCATION.search(block)
        issue = re.sub(r"^#{1,6}\s*", "", first).strip().rstrip("*").strip()
        out.append(
            Finding(
                severity=severity,
                issue=issue[:400],
                evidence=loc.group(0) if loc else "",
                source=source,
                file=loc.group(1) if loc else "",
                line=int(loc.group(2)) if loc else 0,
            )
        )
    return out


def _markdown_verdict(text: str, findings: list[Finding]) -> str:
    if any(f.severity == "blocker" for f in findings):
        return "block"
    if re.search(r"request[ds]? changes|verdict\s*[:*]*\s*\**\s*(fail|block)", text, re.I):
        return "block"
    return "approve"


def _parse_target_reviews(reviewer: str, text: str, valid_peers: set[str]) -> list[PeerReview]:
    by_target: dict[str, PeerReview] = {}

    def slot(target: str) -> PeerReview | None:
        target = _unquote(target)
        if target not in valid_peers or target == reviewer:
            return None
        if target not in by_target:
            by_target[target] = PeerReview(reviewer=reviewer, target=target, verdict="amend")
        return by_target[target]

    for match in _LINE_REVIEW.finditer(text):
        parts = _split_fields(match.group(1), 2)
        entry = slot(parts[0]) if parts else None
        if entry is None:
            continue
        verdict = _unquote(parts[1]).lower() if len(parts) > 1 else "amend"
        entry.verdict = verdict if verdict in ("approve", "amend", "reject") else "amend"
        entry.notes = parts[2].strip() if len(parts) > 2 else ""

    for match in _LINE_FIX.finditer(text):
        parts = _split_fields(match.group(1), 3)
        if len(parts) < 2:
            continue
        entry = slot(parts[0])
        if entry is None:
            continue
        severity = _unquote(parts[1]).lower()
        if severity not in SEVERITY_ORDER:
            severity = "minor"
        issue = parts[2].strip() if len(parts) > 2 else ""
        evidence = parts[3].strip() if len(parts) > 3 else ""
        loc = _LOCATION.search(issue + " " + evidence)
        if not evidence and loc:
            evidence = loc.group(0)
        if issue:
            entry.must_fix.append(
                Finding(
                    severity=severity, issue=issue[:400], evidence=evidence,
                    source=reviewer, file=loc.group(1) if loc else "",
                    line=int(loc.group(2)) if loc else 0,
                )
            )
    return list(by_target.values())


# --------------------------------------------------------------------------
# result types
# --------------------------------------------------------------------------


@dataclass
class Finding:
    severity: str
    issue: str
    evidence: str
    source: str
    file: str = ""
    line: int = 0
    fix: str = ""
    repro: str = ""
    repro_output: str = ""
    status: str = ""  # "", "unverified", "confirmed", "refuted"
    checks: list[str] = field(default_factory=list)

    @property
    def has_evidence(self) -> bool:
        return bool(self.evidence and self.evidence.strip())

    @property
    def blocks(self) -> bool:
        return self.status == "confirmed" and self.severity in ("blocker", "major")

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "issue": self.issue,
            "evidence": self.evidence,
            "file": self.file,
            "line": self.line,
            "fix": self.fix,
            "source": self.source,
            "repro": self.repro,
            "repro_output": self.repro_output,
            "status": self.status,
            "checks": self.checks,
        }


@dataclass
class PeerReview:
    reviewer: str
    target: str
    verdict: str
    must_fix: list[Finding] = field(default_factory=list)
    notes: str = ""


@dataclass
class CouncilOutcome:
    mode: str
    decision: str  # "approve" | "block" | "error"
    findings: list[Finding]
    reviews: list[PeerReview]
    results: list[AgentResult]
    run_dir: Path
    synthesis: str
    summary: str

    @property
    def exit_code(self) -> int:
        if self.decision == "error":
            return 1
        return 0 if self.decision == "approve" else 2


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def parse_peer_reviews(reviewer: str, text: str, valid_peers: set[str]) -> list[PeerReview]:
    reviews: list[PeerReview] = []
    for obj in extract_json_objects(text):
        if not isinstance(obj, dict) or "verdict" not in obj:
            continue
        target = str(obj.get("peer") or "").strip()
        if target not in valid_peers or target == reviewer:
            continue
        verdict = str(obj.get("verdict", "")).strip().lower()
        if verdict not in ("approve", "amend", "reject"):
            verdict = "amend"
        items = []
        for raw in obj.get("must_fix") or []:
            if not isinstance(raw, dict):
                continue
            finding = Finding(
                severity=str(raw.get("severity", "minor")).lower(),
                issue=str(raw.get("issue", "")).strip(),
                evidence=str(raw.get("evidence", "")).strip(),
                source=reviewer,
            )
            if finding.severity not in SEVERITY_ORDER:
                finding.severity = "minor"
            if finding.issue:
                items.append(finding)
        reviews.append(
            PeerReview(
                reviewer=reviewer,
                target=target,
                verdict=verdict,
                must_fix=items,
                notes=str(obj.get("notes", "")).strip(),
            )
        )
    if reviews:
        return reviews
    return _parse_target_reviews(reviewer, text, valid_peers)


def parse_diff_review(reviewer: str, text: str) -> tuple[str, list[Finding]]:
    for obj in extract_json_objects(text):
        if not isinstance(obj, dict) or "verdict" not in obj:
            continue
        verdict = str(obj.get("verdict", "")).strip().lower()
        verdict = "block" if verdict == "block" else "approve"
        findings = []
        for raw in obj.get("findings") or []:
            if not isinstance(raw, dict):
                continue
            severity = str(raw.get("severity", "minor")).lower()
            if severity not in SEVERITY_ORDER:
                severity = "minor"
            issue = str(raw.get("issue", "")).strip()
            if not issue:
                continue
            try:
                line = int(raw.get("line") or 0)
            except (TypeError, ValueError):
                line = 0
            findings.append(
                Finding(
                    severity=severity,
                    issue=issue,
                    evidence=str(raw.get("evidence", "")).strip(),
                    file=str(raw.get("file", "")).strip(),
                    line=line,
                    fix=str(raw.get("fix", "")).strip(),
                    repro=str(raw.get("repro", "")).strip(),
                    source=reviewer,
                )
            )
        return verdict, findings

    line_verdict = _LINE_VERDICT.search(text)
    line_findings: list[Finding] = []
    last: Finding | None = None
    for line in text.splitlines():
        fm = _LINE_FINDING.match(line)
        if fm:
            last = _finding_from_line(fm.group(1), reviewer)
            if last is not None:
                line_findings.append(last)
            continue
        rm = _LINE_REPRO.match(line)
        if rm and last is not None:
            last.repro = rm.group(1).strip()
    if line_verdict or line_findings:
        verdict = line_verdict.group(1).lower() if line_verdict else ""
        if not verdict:
            verdict = "block" if any(f.severity == "blocker" for f in line_findings) else "approve"
        return verdict, line_findings

    markdown = _markdown_findings(text, reviewer)
    if markdown:
        return _markdown_verdict(text, markdown), markdown
    return "", []


def parse_checks(reviewer: str, text: str) -> tuple[dict[int, tuple[str, str]], list[Finding]]:
    """Parse the cross-examination round: CHECK lines and newly found defects."""
    checks: dict[int, tuple[str, str]] = {}
    new_findings: list[Finding] = []
    last: Finding | None = None
    for line in text.splitlines():
        cm = _LINE_CHECK.match(line)
        if cm:
            checks[int(cm.group(1))] = (cm.group(2).lower(), cm.group(3).strip())
            last = None
            continue
        nm = _LINE_NEW.match(line)
        if nm:
            last = _finding_from_line(nm.group(1), reviewer)
            if last is not None:
                new_findings.append(last)
            continue
        rm = _LINE_REPRO.match(line)
        if rm and last is not None:
            last.repro = rm.group(1).strip()
    return checks, new_findings


# --------------------------------------------------------------------------
# merging
# --------------------------------------------------------------------------


def merge_proposals(
    proposals: dict[str, str], reviews: list[PeerReview]
) -> tuple[str, str, list[Finding]]:
    """Return (decision, recommended_author, findings).

    Decision is "approve" when at least one proposal survives review without a
    reject or a blocker, else "block".
    """
    blocked: dict[str, list[str]] = {}
    findings: list[Finding] = []
    for review in reviews:
        reasons = []
        if review.verdict == "reject":
            reasons.append(f"{review.reviewer} rejected")
        for finding in review.must_fix:
            if not finding.has_evidence:
                continue  # unverified claims do not count as control
            findings.append(finding)
            if finding.severity == "blocker":
                reasons.append(f"{review.reviewer} blocker: {finding.issue}")
        if reasons:
            blocked.setdefault(review.target, []).extend(reasons)

    survivors = [name for name in proposals if name not in blocked]
    if not survivors:
        return "block", "", findings

    scores = {}
    for name in survivors:
        own = [f for f in findings if f.source != name and _target_of(reviews, name, f.source)]
        scores[name] = (
            sum(1 for f in own if f.severity == "blocker"),
            sum(1 for f in own if f.severity == "major"),
            name,
        )
    best = min(survivors, key=lambda n: scores[n][:2] + (n,))
    return "approve", best, findings


def _target_of(reviews: list[PeerReview], target: str, reviewer: str) -> bool:
    return any(r.target == target and r.reviewer == reviewer for r in reviews)


def merge_diff_review(
    verdicts: dict[str, str], findings: list[Finding]
) -> str:
    """block if anyone proved a blocker, or if a majority voted block."""
    if any(f.severity == "blocker" and f.has_evidence for f in findings):
        return "block"
    blocks = sum(1 for v in verdicts.values() if v == "block")
    if blocks * 2 > len(verdicts):
        return "block"
    return "approve"


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def render_proposal_synthesis(
    task: str, proposals: dict[str, str], reviews: list[PeerReview],
    decision: str, recommended: str, findings: list[Finding],
) -> str:
    lines = [f"# Council synthesis", "", f"**Task:** {task}", "", "## Verdict", ""]
    if decision == "approve":
        lines.append(f"**PROCEED** with the plan from `{recommended}`.")
    else:
        lines.append("**BLOCKED** - no proposal survived cross-review.")
    lines += ["", "## Votes", "", "| proposal | " + " | ".join(
        sorted({r.reviewer for r in reviews})) + " |", "|---|" + "---|" * len({r.reviewer for r in reviews})]
    reviewers = sorted({r.reviewer for r in reviews})
    for name in sorted(proposals):
        cells = []
        for rev in reviewers:
            match = next((r for r in reviews if r.reviewer == rev and r.target == name), None)
            cells.append(match.verdict if match else "-")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")

    ranked = sorted(
        [f for f in findings if f.has_evidence],
        key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.source),
    )
    lines += ["", "## Verified findings", ""]
    if not ranked:
        lines.append("None with evidence.")
    for f in ranked:
        lines.append(f"- **[{f.severity}]** {f.issue}")
        lines.append(f"  - evidence: {f.evidence}")
        lines.append(f"  - raised by: {f.source}")

    undated = [f for f in findings if not f.has_evidence]
    if undated:
        lines += ["", "## Discarded (no evidence supplied)", ""]
        for f in undated:
            lines.append(f"- [{f.severity}] {f.issue} ({f.source})")

    lines += ["", "## Full proposals", ""]
    for name in sorted(proposals):
        lines += [f"<details><summary>{name}</summary>", "", proposals[name].strip(), "", "</details>", ""]
    return "\n".join(lines)


def _render_finding(f: Finding) -> list[str]:
    loc = f" `{f.file}:{f.line}`" if f.file else ""
    lines = [f"- **[{f.severity}]**{loc} {f.issue}"]
    lines.append(f"  - evidence: {f.evidence[:400]}  _(from {f.source})_")
    if f.repro:
        first = (f.repro_output or "").splitlines()
        tail = first[-1][:120] if first else "not run"
        lines.append(f"  - repro: `{f.repro}` -> {tail}")
    for check in f.checks[:4]:
        lines.append(f"  - cross-check: {check[:300]}")
    return lines


def _render_findings_sections(findings: list[Finding]) -> list[str]:
    confirmed = [f for f in findings if f.status == "confirmed" and f.has_evidence]
    unverified = [f for f in findings if f.status in ("", "unverified") and f.has_evidence]
    refuted = [f for f in findings if f.status == "refuted"]
    discarded = [f for f in findings if not f.has_evidence and f.severity != "info"]

    lines: list[str] = []
    lines += ["", "## Confirmed (blocking if blocker/major)", ""]
    if confirmed:
        for f in sorted(confirmed, key=lambda x: (SEVERITY_ORDER.get(x.severity, 9), x.source)):
            lines += _render_finding(f)
    else:
        lines.append("None.")
    lines += ["", "## Unverified (not blocking)", ""]
    if unverified:
        for f in unverified:
            lines += _render_finding(f)
    else:
        lines.append("None.")
    if refuted:
        lines += ["", "## Refuted (repro exit 0 or a peer refuted it)", ""]
        for f in refuted:
            lines += _render_finding(f)
    if discarded:
        lines += ["", "## Discarded (no evidence supplied)", ""]
        for f in discarded:
            lines.append(f"- [{f.severity}] {f.issue} ({f.source})")
    return lines


def _stamp(commit: str) -> str:
    return f"commit `{commit or 'no-commit'}` · {time.strftime('%Y-%m-%d %H:%M')}"


def render_diff_synthesis(
    decision: str, verdicts: dict[str, str], findings: list[Finding], diff_stat: str,
    isolation: str = "", stamp: str = "",
) -> str:
    lines = ["# Council review", "", f"**Changed:** {diff_stat}", ""]
    if stamp:
        lines += [f"**Reviewed:** {stamp}", ""]
    if isolation:
        lines += [f"**Snapshot:** {isolation}", ""]
    lines += ["## Verdict", ""]
    lines.append("**BLOCK**" if decision == "block" else "**APPROVE**")
    blocking = [f for f in findings if f.blocks]
    lines.append(f"\n{len(blocking)} confirmed blocker/major finding(s).")
    lines += ["", "| agent | verdict |", "|---|---|"]
    for name in sorted(verdicts):
        lines.append(f"| {name} | {verdicts[name] or 'unparsed'} |")
    lines += _render_findings_sections(findings)
    return "\n".join(lines)


def render_consult_synthesis(
    decision: str, verdicts: dict[str, str], findings: list[Finding],
    user_message: str, diff_stat: str, isolation: str = "", stamp: str = "",
) -> str:
    first = next((ln for ln in user_message.strip().splitlines() if ln.strip()), "")
    lines = ["# Council session consult", "", f"**User message:** {first[:300]}", ""]
    if stamp:
        lines += [f"**Reviewed:** {stamp}", ""]
    if diff_stat:
        lines += [f"**Uncommitted:** {diff_stat}", ""]
    if isolation:
        lines += [f"**Snapshot:** {isolation}", ""]
    lines += ["## Verdict", ""]
    if decision == "block":
        lines.append("**BLOCK**")
    elif decision == "error":
        lines.append("**ERROR** - not enough agents answered")
    else:
        lines.append("**APPROVE**")
    lines.append(f"\n{len([f for f in findings if f.blocks])} confirmed blocker/major finding(s).")
    lines += ["", "| agent | verdict |", "|---|---|"]
    for name in sorted(verdicts):
        lines.append(f"| {name} | {verdicts[name] or 'unparsed'} |")
    lines += _render_findings_sections(findings)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------


def _configs(agents: list[str], overrides: dict[str, dict] | None,
             posture: str = "shell") -> dict[str, AgentConfig]:
    overrides = overrides or {}
    out = {}
    for name in agents:
        raw = overrides.get(name) or {}
        chosen = str(raw.get("posture") or posture)
        if chosen not in POSTURES:
            chosen = "shell"
        out[name] = AgentConfig(
            name=name,
            timeout_s=int(raw.get("timeout_s", 600)),
            model=raw.get("model"),
            extra_args=list(raw.get("extra_args") or []),
            enabled=bool(raw.get("enabled", True)),
            posture=chosen,
        )
    return out


def _attach_prompt_dir(cfgs: dict[str, AgentConfig], store: RunStore) -> None:
    """Prompts are shipped by file; see adapters._materialize_prompt for why."""
    for cfg in cfgs.values():
        cfg.prompt_dir = str(store.dir / "prompts")


def _parallel(calls: dict[str, tuple[str, str]], cfgs: dict[str, AgentConfig],
              on_done=None) -> list[AgentResult]:
    results: list[AgentResult] = []
    with ThreadPoolExecutor(max_workers=max(1, len(calls))) as pool:
        futures = {
            pool.submit(run_agent, name, prompt, cwd, cfg): name
            for name, (prompt, cwd) in calls.items()
            for cfg in [cfgs[name]]
        }
        for fut in as_completed(futures):
            res = fut.result()
            results.append(res)
            if on_done:
                on_done(res)
    results.sort(key=lambda r: AGENTS.index(r.agent) if r.agent in AGENTS else 99)
    return results


@dataclass
class ReviewContext:
    project: Path
    workdir: Path
    snapshot: project_mod.Snapshot | None
    options: dict
    verify_results: list[dict]
    impact: str
    docs: str
    lessons: str
    tier: str = "standard"
    tier_reason: str = ""

    @property
    def verify_text(self) -> str:
        return prompts.render_verify(self.verify_results)

    @property
    def isolation(self) -> str:
        if self.snapshot is None:
            return "off"
        meta = self.snapshot.meta
        if meta.get("isolation") == "worktree":
            return f"worktree {meta.get('files', 0)} file(s) copied from the working tree"
        return f"none ({meta.get('reason', 'unavailable')})"


def _prepare_review(project: Path, base_options: dict, trigger: str,
                    diff: str, message: str,
                    tier_override: str | None = None) -> ReviewContext:
    verify_results = project_mod.run_verify(project) if base_options.get("verify") else []
    if tier_override in ("lean", "standard", "full"):
        tier, reason = tier_override, "forced by --tier"
    else:
        tier, reason = project_mod.choose_tier(project, diff, message,
                                               verify_results, trigger)
    options = project_mod.apply_tier(base_options, tier)
    snapshot = project_mod.Snapshot(project) if options.get("isolate") else None
    workdir = snapshot.prepare() if snapshot else Path(project)
    impact = docs = ""
    if options.get("impact"):
        paths = project_mod.changed_paths(project)
        impact = project_mod.collect_impact(project, paths)
    else:
        impact = ("(impact context was not preloaded for this tier; inspect callers "
                  "yourself if your lens needs them)")
    if options.get("docs"):
        docs = project_mod.collect_docs(project)
    else:
        docs = ("(docs were not preloaded for this tier; read README/STATUS/CLAUDE "
                "yourself if your lens needs them)")
    lessons = project_mod.load_lessons(project) if options.get("lessons") else ""
    return ReviewContext(Path(project), Path(workdir), snapshot, options,
                         verify_results, impact, docs, lessons, tier, reason)


def _verify_failures(ctx: ReviewContext) -> list[Finding]:
    """A failing verify command is a fact, not a model opinion: it blocks."""
    out: list[Finding] = []
    for res in ctx.verify_results:
        if res.get("code") == 0:
            continue
        out.append(Finding(
            severity="blocker",
            issue=f"project verify command failed: {res.get('command', '')}",
            evidence=(res.get("output") or "no output")[-800:],
            source="verify", status="confirmed",
        ))
    return out


def _should_cross(ctx: ReviewContext, findings: list[Finding]) -> bool:
    mode = ctx.options.get("cross")
    if mode is False:
        return False
    if mode is True:
        return True
    # auto: pay for the second round only when something serious was raised
    return any(f.severity in ("blocker", "major") and f.status != "refuted"
               for f in findings)


def _cross_round(store, ctx: ReviewContext, agents: list[str],
                 cfgs: dict[str, AgentConfig], findings: list[Finding],
                 on_done) -> list[AgentResult]:
    if not _should_cross(ctx, findings):
        return []
    numbered = [(i, f) for i, f in enumerate(findings)
                if f.has_evidence and f.source != "verify"][:8]
    if not numbered:
        return []
    calls: dict[str, tuple[str, str]] = {}
    for agent in agents:
        peers = [(n, f) for n, f in numbered if f.source != agent]
        if not peers:
            continue
        lines: list[str] = []
        for n, f in peers:
            lines.append(f"{n}. [{f.severity}] {f.file}:{f.line} {f.issue} -- {f.evidence[:300]}")
            if f.repro:
                lines.append(f"   REPRO: {f.repro}")
        calls[agent] = (
            prompts.verify_findings_prompt(
                agent, str(ctx.workdir), "\n".join(lines),
                cfgs[agent].posture != "read_only",
                f"VERIFY OUTPUT:\n{ctx.verify_text}",
            ),
            str(ctx.workdir),
        )
    if not calls:
        return []
    results = _parallel(calls, {a: cfgs[a] for a in calls}, on_done)
    for res in results:
        store.write(f"r2/{res.agent}.raw.md", res.text or f"ERROR: {res.error}\n")
        store.log({"event": "cross", **res.to_dict()})
        if not res.ok:
            continue
        checks, fresh = parse_checks(res.agent, res.text)
        for n, (verdict, evidence) in checks.items():
            if n < 0 or n >= len(findings) or not evidence:
                continue
            finding = findings[n]
            finding.checks.append(f"{res.agent}: {verdict} -- {evidence[:240]}")
            if finding.status == "confirmed":
                continue  # an executed non-zero repro is final
            if verdict == "confirm":
                finding.status = "confirmed"  # a peer's proof beats a bad repro
            elif finding.status != "refuted":
                finding.status = "refuted"
        findings.extend(fresh)
    return results


def decide_review(verdicts: dict[str, str], findings: list[Finding]) -> str:
    """A proven blocker/major blocks; otherwise a majority of block votes does."""
    if any(f.blocks for f in findings):
        return "block"
    answered = [v for v in verdicts.values() if v]
    if answered and sum(1 for v in answered if v == "block") * 2 > len(answered):
        return "block"
    return "approve"


def _finalize_review(store, ctx: ReviewContext, verdicts: dict[str, str],
                     findings: list[Finding], agents: list[str],
                     cfgs: dict[str, AgentConfig], on_done):
    project_mod.verify_repros(ctx.workdir, findings)
    before = len(findings)
    cross_results = _cross_round(store, ctx, agents, cfgs, findings, on_done)
    project_mod.verify_repros(ctx.workdir, findings[before:])
    decision = decide_review(verdicts, findings)
    added = 0
    if ctx.options.get("lessons"):
        added = project_mod.append_lessons(ctx.project, findings, store.run_id)
    store.log({
        "event": "finalize", "decision": decision,
        "confirmed": [f.to_dict() for f in findings if f.status == "confirmed"],
        "refuted": [f.to_dict() for f in findings if f.status == "refuted"],
        "lessons_added": added,
    })
    return cross_results, decision, added


def _plan_phase(
    store: RunStore,
    project: Path,
    task: str,
    agents: list[str],
    cfgs: dict[str, AgentConfig],
    context: str | None,
    on_done,
):
    """Three independent plans, then each agent attacks the other two."""
    started = time.monotonic()
    proposals_res = _parallel(
        {a: (prompts.propose_prompt(
                a, task, str(project), context, cfgs[a].posture != "read_only"),
             str(project)) for a in agents},
        cfgs,
        on_done=on_done,
    )
    proposals = {r.agent: r.text for r in proposals_res if r.ok}
    for res in proposals_res:
        store.write(f"r1/{res.agent}.md", res.text or f"ERROR: {res.error}\n")
        store.log({"event": "propose", **res.to_dict()})

    if len(proposals) < MIN_AGENTS:
        return proposals_res, proposals, [], "error", "", [], []

    review_calls = {}
    for agent in proposals:
        peers = {n: t for n, t in proposals.items() if n != agent}
        review_calls[agent] = (
            prompts.review_proposals_prompt(
                agent, task, str(project), peers, cfgs[agent].posture != "read_only"
            ),
            str(project),
        )
    review_res = _parallel(review_calls, cfgs, on_done=on_done)

    reviews: list[PeerReview] = []
    valid = set(proposals)
    for res in review_res:
        store.write(f"r2/{res.agent}.raw.md", res.text or f"ERROR: {res.error}\n")
        store.log({"event": "review", **res.to_dict()})
        parsed = parse_peer_reviews(res.agent, res.text, valid) if res.ok else []
        store.write(f"r2/{res.agent}.json",
                    json.dumps([r.__dict__ for r in parsed], default=str, indent=2))
        reviews.extend(parsed)

    decision, recommended, findings = merge_proposals(proposals, reviews)
    all_results = proposals_res + review_res
    store.log({
        "event": "plan", "decision": decision, "recommended": recommended,
        "findings": [f.to_dict() for f in findings],
        "elapsed_s": round(time.monotonic() - started, 1),
    })
    return all_results, proposals, reviews, decision, recommended, findings, []


def run_task_council(
    project: Path,
    task: str,
    agents: list[str] | None = None,
    overrides: dict[str, dict] | None = None,
    context: str | None = None,
    on_done=None,
    posture: str = "shell",
) -> CouncilOutcome:
    project = Path(project).resolve()
    agents = agents or list(AGENTS)
    cfgs = _configs(agents, overrides, posture)
    store = RunStore.for_task(project, task)
    store.write("task.md", task + "\n")
    store.write_meta({"mode": "task", "task": task, "agents": agents, "posture": posture})
    _attach_prompt_dir(cfgs, store)
    store.log({"event": "start", "mode": "task", "task": task, "agents": agents,
               "posture": posture})

    results, proposals, reviews, decision, recommended, findings, _ = _plan_phase(
        store, project, task, agents, cfgs, context, on_done
    )

    if decision == "error":
        synthesis = render_proposal_synthesis(task, proposals, [], "error", "", [])
        store.write("synthesis.md", synthesis)
        return CouncilOutcome("task", "error", [], [], results, store.dir, synthesis,
                              _short("error", "not enough agents answered"))

    synthesis = render_proposal_synthesis(task, proposals, reviews, decision, recommended, findings)
    store.write("synthesis.md", synthesis)
    store.log({"event": "synthesis", "decision": decision, "recommended": recommended})
    return CouncilOutcome("task", decision, findings, reviews, results, store.dir, synthesis,
                          _short(decision, f"recommended plan: {recommended or 'none'}"))


# --------------------------------------------------------------------------
# full work loop: plan -> execute -> review -> fix -> re-review
# --------------------------------------------------------------------------


def _finding_line(f: Finding) -> str:
    loc = f" {f.file}:{f.line}" if f.file else ""
    fix = f" Fix: {f.fix}" if f.fix else ""
    return f"[{f.severity}]{loc} {f.issue} -- evidence: {f.evidence}.{fix}"


def run_work_loop(
    project: Path,
    task: str,
    agents: list[str] | None = None,
    overrides: dict[str, dict] | None = None,
    max_rounds: int = 3,
    context: str | None = None,
    on_done=None,
) -> CouncilOutcome:
    """Plan with three, execute with one, verify with the other two, fix, repeat."""
    project = Path(project).resolve()
    agents = agents or list(AGENTS)
    overrides = overrides or {}

    # Planners and reviewers get shell but not the edit tools; only the single
    # executor writes, so three writers can never race over the same file.
    review_cfgs = _configs(agents, overrides, "shell")
    store = RunStore.for_task(project, task)
    store.write("task.md", task + "\n")
    store.write_meta({"mode": "work", "task": task, "agents": agents,
                      "max_rounds": max_rounds, "base_commit": head_commit(project)})
    _attach_prompt_dir(review_cfgs, store)
    store.log({"event": "start", "mode": "work", "task": task, "agents": agents})

    options = project_mod.pipeline_options(project)
    verify_results = project_mod.run_verify(project) if options.get("verify") else []
    verify_text = prompts.render_verify(verify_results)
    impact = project_mod.collect_impact(project, project_mod.changed_paths(project))
    docs = project_mod.collect_docs(project)
    lessons = project_mod.load_lessons(project)
    verify_failures: list[Finding] = []
    for res in verify_results:
        if res.get("code") != 0:
            verify_failures.append(Finding(
                severity="blocker",
                issue=f"project verify command failed: {res.get('command', '')}",
                evidence=(res.get("output") or "no output")[-800:],
                source="verify", status="confirmed",
            ))
    if verify_failures:
        synthesis = render_work_synthesis(task, "(not started)", {}, [],
                                          verify_failures, "", "block")
        store.write("synthesis.md", synthesis)
        store.log({"event": "synthesis", "decision": "block",
                   "reason": "verify command failed; nothing executed"})
        return CouncilOutcome("work", "block", verify_failures, [], [], store.dir, synthesis,
                              _short("block", "verify command failed; nothing executed"))

    results, proposals, reviews, decision, recommended, findings, _ = _plan_phase(
        store, project, task, agents, review_cfgs, context, on_done
    )
    if decision == "error":
        synthesis = render_proposal_synthesis(task, proposals, [], "error", "", [])
        store.write("synthesis.md", synthesis)
        return CouncilOutcome("work", "error", [], [], results, store.dir, synthesis,
                              _short("error", "not enough agents answered"))
    if decision != "approve" or not recommended:
        synthesis = render_proposal_synthesis(task, proposals, reviews, decision, "", findings)
        store.write("synthesis.md", synthesis + "\n\nNothing was executed.\n")
        return CouncilOutcome("work", "block", findings, reviews, results, store.dir, synthesis,
                              _short("block", "no plan survived review; nothing executed"))

    executor = recommended
    exec_override = dict(overrides.get(executor) or {})
    exec_override["posture"] = "autonomous"
    exec_cfg = _configs([executor], {executor: exec_override}, "autonomous")[executor]
    exec_cfg.prompt_dir = str(store.dir / "prompts")
    reviewers = [a for a in agents if a != executor] or [executor]

    exec_res = run_agent(
        executor,
        prompts.execute_prompt(executor, str(project), task, proposals[executor], executor, True),
        str(project),
        exec_cfg,
    )
    if on_done:
        on_done(exec_res)
    results.append(exec_res)
    store.write("r3/execute.md", exec_res.text or f"ERROR: {exec_res.error}\n")
    store.log({"event": "execute", **exec_res.to_dict()})
    changelog = [exec_res.text]

    if not exec_res.ok:
        synthesis = render_proposal_synthesis(task, proposals, reviews, decision, executor, findings)
        store.write("synthesis.md", synthesis + f"\n\nExecution failed: {exec_res.error}\n")
        return CouncilOutcome("work", "error", findings, reviews, results, store.dir, synthesis,
                              _short("error", f"executor {executor} failed: {exec_res.error[:120]}"))

    remaining: list[Finding] = []
    # max_rounds fixes means max_rounds + 1 reviews: the last fix must be
    # re-reviewed, otherwise the verdict describes code that no longer exists.
    for round_no in range(max_rounds + 1):
        diff, stat = collect_diff(project)
        if not diff.strip():
            store.log({"event": "empty_diff", "round": round_no})
            break
        store.write(f"r4/diff-{round_no}.patch", diff)

        review_calls = {
            a: (prompts.review_diff_prompt(a, str(project), diff,
                                           review_cfgs[a].posture != "read_only",
                                           verify=verify_text, impact=impact,
                                           docs=docs, lessons=lessons),
                str(project))
            for a in reviewers
        }
        round_results = _parallel(review_calls, {a: review_cfgs[a] for a in reviewers}, on_done)
        results.extend(round_results)

        verdicts: dict[str, str] = {}
        round_findings: list[Finding] = []
        for res in round_results:
            store.write(f"r4/{res.agent}-{round_no}.raw.md", res.text or f"ERROR: {res.error}\n")
            store.log({"event": "diff_review", "round": round_no, **res.to_dict()})
            if not res.ok:
                verdicts[res.agent] = ""
                continue
            verdict, parsed = parse_diff_review(res.agent, res.text)
            verdicts[res.agent] = verdict
            round_findings.extend(parsed)

        project_mod.verify_repros(project, round_findings)
        blocking = [f for f in round_findings
                    if f.severity in ("blocker", "major") and f.has_evidence
                    and f.status != "refuted"]
        remaining = blocking
        store.log({"event": "round", "round": round_no, "verdicts": verdicts,
                   "blocking": [f.to_dict() for f in blocking], "diff_stat": stat})
        if not blocking:
            break
        if round_no >= max_rounds:
            break

        fix_res = run_agent(
            executor,
            prompts.fix_prompt(executor, str(project), [_finding_line(f) for f in blocking], True),
            str(project),
            exec_cfg,
        )
        if on_done:
            on_done(fix_res)
        results.append(fix_res)
        store.write(f"r5/fix-{round_no}.md", fix_res.text or f"ERROR: {fix_res.error}\n")
        store.log({"event": "fix", "round": round_no, **fix_res.to_dict()})
        changelog.append(fix_res.text)
        if not fix_res.ok:
            break

    final_diff, final_stat = collect_diff(project)
    store.write("final.patch", final_diff)
    decision_out = "approve" if not remaining else "block"
    if options.get("lessons"):
        project_mod.append_lessons(project, remaining, store.run_id)
    synthesis = render_work_synthesis(task, executor, proposals, changelog, remaining,
                                      final_stat, decision_out)
    store.write("synthesis.md", synthesis)
    store.log({"event": "synthesis", "decision": decision_out,
               "remaining": [f.to_dict() for f in remaining]})

    blockers = len([f for f in remaining if f.severity == "blocker"])
    return CouncilOutcome("work", decision_out, remaining, reviews, results, store.dir, synthesis,
                          _short(decision_out,
                                 f"executed by {executor}; {blockers} blocker(s) remain, "
                                 f"{len(final_diff)} chars changed"))


def _trim_stat(stat: str, limit: int = 1200) -> str:
    stat = stat or "(no diff)"
    return stat if len(stat) <= limit else stat[:limit] + "\n... [stat truncated]"


def render_work_synthesis(task: str, executor: str, proposals: dict[str, str],
                          changelog: list[str], remaining: list[Finding],
                          diff_stat: str, decision: str) -> str:
    lines = ["# Council work", "", f"**Task:** {task}", f"**Executor:** {executor}", ""]
    lines += ["## Verdict", ""]
    lines.append(
        "**DONE** - no proven blocker or major finding remains."
        if decision == "approve"
        else f"**STOPPED** - {len(remaining)} proven finding(s) remain."
    )
    lines += ["", f"**Changed:** {_trim_stat(diff_stat)}", ""]
    lines += ["## What the executor did", ""]
    for i, entry in enumerate(changelog, 1):
        lines += [f"### Pass {i}", "", entry.strip(), ""]
    if remaining:
        lines += ["## Still broken", ""]
        for f in remaining:
            lines.append(f"- **[{f.severity}]** {_finding_line(f)}  _(raised by {f.source})_")
    return "\n".join(lines)



def run_diff_review(
    project: Path,
    diff: str,
    diff_stat: str = "",
    agents: list[str] | None = None,
    overrides: dict[str, dict] | None = None,
    on_done=None,
    posture: str = "shell",
    trigger: str = "manual",
    tier_override: str | None = None,
) -> CouncilOutcome:
    project = Path(project).resolve()
    agents = agents or list(AGENTS)
    base_options = project_mod.pipeline_options(project)
    commit = head_commit(project)
    stamp = _stamp(commit)
    cfgs = _configs(agents, overrides, posture)
    store = RunStore.for_task(project, f"review-{diff_stat or 'diff'}")
    store.write("diff.patch", diff)
    _attach_prompt_dir(cfgs, store)
    ctx = _prepare_review(project, base_options, trigger, diff, diff_stat, tier_override)
    try:
        store.write_meta({
            "mode": "review", "diff_stat": diff_stat, "agents": agents,
            "posture": posture, "options": ctx.options,
            "tier": ctx.tier, "tier_reason": ctx.tier_reason,
            "commit": commit, "ts": time.time(),
            "isolation": ctx.snapshot.meta if ctx.snapshot else {"isolation": "off"},
            "workdir": str(ctx.workdir),
            "verify": [r.get("command") for r in ctx.verify_results],
        })
        store.log({"event": "start", "mode": "review", "diff_stat": diff_stat,
                   "agents": agents, "posture": posture, "options": ctx.options,
                   "tier": ctx.tier, "tier_reason": ctx.tier_reason,
                   "workdir": str(ctx.workdir)})

        results = _parallel(
            {a: (prompts.review_diff_prompt(
                    a, str(ctx.workdir), diff, cfgs[a].posture != "read_only",
                    lens=prompts.lens_for(a), verify=ctx.verify_text,
                    impact=ctx.impact, docs=ctx.docs, lessons=ctx.lessons),
                 str(ctx.workdir)) for a in agents},
            cfgs,
            on_done=on_done,
        )

        verdicts: dict[str, str] = {}
        findings: list[Finding] = _verify_failures(ctx)
        for res in results:
            store.write(f"r1/{res.agent}.raw.md", res.text or f"ERROR: {res.error}\n")
            store.log({"event": "diff_review", **res.to_dict()})
            if not res.ok:
                verdicts[res.agent] = ""
                continue
            verdict, parsed = parse_diff_review(res.agent, res.text)
            verdicts[res.agent] = verdict
            findings.extend(parsed)

        answered = [v for v in verdicts.values() if v]
        if len(answered) < MIN_AGENTS:
            synthesis = render_diff_synthesis("error", verdicts, findings, diff_stat,
                                              ctx.isolation, stamp)
            store.write("synthesis.md", synthesis)
            return CouncilOutcome("review", "error", findings, [], results, store.dir, synthesis,
                                  _short("error", "not enough agents answered"))

        cross_results, decision, _ = _finalize_review(
            store, ctx, verdicts, findings, agents, cfgs, on_done)
        results = results + cross_results
        synthesis = render_diff_synthesis(decision, verdicts, findings, diff_stat,
                                          ctx.isolation, stamp)
        store.write("synthesis.md", synthesis)
        store.log({
            "event": "synthesis", "decision": decision, "verdicts": verdicts,
            "findings": [f.to_dict() for f in findings],
        })
        blockers = sum(1 for f in findings if f.blocks)
        return CouncilOutcome("review", decision, findings, [], results, store.dir, synthesis,
                              _short(decision,
                                     f"{blockers} confirmed blocker/major, "
                                     f"{len(findings)} finding(s)"))
    finally:
        if ctx.snapshot is not None:
            ctx.snapshot.cleanup()


def run_session_consult(
    project: Path,
    user_message: str,
    transcript: str = "",
    diff: str = "",
    diff_stat: str = "",
    git_state: str = "",
    agents: list[str] | None = None,
    overrides: dict[str, dict] | None = None,
    on_done=None,
    posture: str = "read_only",
    max_chars: int = 60000,
    tier_override: str | None = None,
) -> CouncilOutcome:
    """All three agents join a live session the moment the user writes.

    Not a diff gate: the message, the transcript tail and the uncommitted
    change are all context. The evidence rule is unchanged - a finding without
    proof is discarded, so a verdict in pending.md is worth reading.
    """
    project = Path(project).resolve()
    agents = agents or list(AGENTS)
    base_options = project_mod.pipeline_options(project)
    commit = head_commit(project)
    stamp = _stamp(commit)
    cfgs = _configs(agents, overrides, posture)
    store = RunStore.for_task(project, f"consult-{user_message.strip()}")
    store.write("session.md", user_message + "\n")
    store.write("context.md", transcript or "(no transcript)\n")
    if diff:
        store.write("diff.patch", diff)
    _attach_prompt_dir(cfgs, store)
    ctx = _prepare_review(project, base_options, "prompt", diff, user_message,
                          tier_override)
    try:
        store.write_meta({
            "mode": "consult", "agents": agents, "posture": posture,
            "prompt_chars": len(user_message), "diff_stat": diff_stat,
            "options": ctx.options,
            "tier": ctx.tier, "tier_reason": ctx.tier_reason,
            "commit": commit, "ts": time.time(),
            "isolation": ctx.snapshot.meta if ctx.snapshot else {"isolation": "off"},
            "workdir": str(ctx.workdir),
            "verify": [r.get("command") for r in ctx.verify_results],
        })
        store.log({"event": "start", "mode": "consult", "agents": agents,
                   "prompt_chars": len(user_message), "diff_chars": len(diff),
                   "options": ctx.options, "tier": ctx.tier,
                   "tier_reason": ctx.tier_reason, "workdir": str(ctx.workdir)})

        if ctx.tier == "lean" and len(transcript) > 6000:
            prompt_transcript = "..." + transcript[-6000:]
        else:
            prompt_transcript = transcript
        max_findings = 3 if ctx.tier == "lean" else 6
        results = _parallel(
            {a: (prompts.consult_prompt(
                    a, str(ctx.workdir), user_message, prompt_transcript, git_state, diff,
                    cfgs[a].posture != "read_only", max_chars,
                    lens=prompts.lens_for(a), verify=ctx.verify_text,
                    impact=ctx.impact, docs=ctx.docs, lessons=ctx.lessons,
                    max_findings=max_findings),
                 str(ctx.workdir)) for a in agents},
            cfgs,
            on_done=on_done,
        )

        verdicts: dict[str, str] = {}
        findings: list[Finding] = _verify_failures(ctx)
        for res in results:
            store.write(f"r1/{res.agent}.raw.md", res.text or f"ERROR: {res.error}\n")
            store.log({"event": "consult", **res.to_dict()})
            if not res.ok:
                verdicts[res.agent] = ""
                continue
            verdict, parsed = parse_diff_review(res.agent, res.text)
            verdicts[res.agent] = verdict
            findings.extend(parsed)

        answered = [v for v in verdicts.values() if v]
        if len(answered) < MIN_AGENTS:
            synthesis = render_consult_synthesis("error", verdicts, findings,
                                                 user_message, diff_stat, ctx.isolation,
                                                 stamp)
            store.write("synthesis.md", synthesis)
            return CouncilOutcome("consult", "error", findings, [], results, store.dir, synthesis,
                                  _short("error", "not enough agents answered"))

        cross_results, decision, _ = _finalize_review(
            store, ctx, verdicts, findings, agents, cfgs, on_done)
        results = results + cross_results
        synthesis = render_consult_synthesis(decision, verdicts, findings,
                                             user_message, diff_stat, ctx.isolation, stamp)
        store.write("synthesis.md", synthesis)
        store.log({
            "event": "synthesis", "decision": decision, "verdicts": verdicts,
            "findings": [f.to_dict() for f in findings],
        })
        blockers = sum(1 for f in findings if f.blocks)
        return CouncilOutcome("consult", decision, findings, [], results, store.dir, synthesis,
                              _short(decision,
                                     f"{blockers} confirmed blocker/major, "
                                     f"{len(findings)} finding(s)"))
    finally:
        if ctx.snapshot is not None:
            ctx.snapshot.cleanup()


def _short(decision: str, detail: str) -> str:
    return f"{decision.upper()}: {detail}"
