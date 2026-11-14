"""Command line entry point: `council <command>`."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import prompts
from .adapters import AGENTS, POSTURES, AgentConfig, run_agent
from .gitutil import collect_diff as _collect_diff
from .hooks import fingerprint, load_state, save_state, scope_key, write_pending
from .protocol import (
    SEVERITY_ORDER,
    CouncilOutcome,
    run_diff_review,
    run_task_council,
    run_work_loop,
)
from .store import COUNCIL_DIR, latest_run


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _progress(res) -> None:
    status = "ok " if res.ok else "ERR"
    cost = f" ${res.cost_usd:.4f}" if res.cost_usd else ""
    secs = res.elapsed_ms / 1000
    line = f"  [{res.agent:8s}] {status} {secs:6.1f}s{cost}"
    if not res.ok:
        line += f"  {res.error[:120]}"
    print(line, file=sys.stderr, flush=True)



def _print_outcome(out: CouncilOutcome) -> None:
    print()
    print(out.summary)
    ranked = sorted(
        [f for f in out.findings if f.has_evidence],
        key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.source),
    )
    shown = ranked[:12]
    if shown:
        print()
        for f in shown:
            loc = f" {f.file}:{f.line}" if f.file else ""
            print(f"  [{f.severity}] {f.issue}{loc}")
            print(f"           evidence: {f.evidence[:200]}  ({f.source})")
    discarded = len([f for f in out.findings if not f.has_evidence])
    if discarded:
        print(f"\n  ({discarded} finding(s) discarded: no evidence supplied)")
    print(f"\n  run dir: {out.run_dir}")
    print(f"  synthesis: {out.run_dir / 'synthesis.md'}")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_task(args: argparse.Namespace) -> int:
    project = Path(args.project or ".").resolve()
    context = None
    if args.context:
        context = Path(args.context).read_text(encoding="utf-8")
    agents = _split_agents(args.agents)
    overrides = _load_overrides(project)
    print(f"council: {len(agents)} agents on {project}", file=sys.stderr)
    out = run_task_council(project, args.task, agents, overrides, context,
                           on_done=_progress, posture=args.posture)
    _print_outcome(out)
    if args.json:
        print(json.dumps({
            "decision": out.decision,
            "findings": [f.to_dict() for f in out.findings],
            "run_dir": str(out.run_dir),
        }, indent=2))
    return out.exit_code


def cmd_review(args: argparse.Namespace) -> int:
    project = Path(args.project or ".").resolve()
    diff, stat = _collect_diff(project, args.staged, args.diff, args.path)
    if not diff.strip():
        print("council: nothing to review (empty diff)", file=sys.stderr)
        return 0
    if len(diff) > args.max_chars:
        diff = diff[: args.max_chars] + "\n... [diff truncated by council]"
    agents = _split_agents(args.agents)
    overrides = _load_overrides(project)

    fp = fingerprint(diff)
    scope = scope_key(args.staged, args.diff, args.path)
    if args.if_changed:
        last = load_state(project).get(scope) or {}
        if last.get("fingerprint") == fp and last.get("decision") in ("approve", "block"):
            print(f"council: unchanged since last review "
                  f"({last['decision']}, {last.get('run_dir', '')})", file=sys.stderr)
            return 0 if last["decision"] == "approve" else 2

    print(f"council review: {len(agents)} agents, {len(diff)} chars of diff", file=sys.stderr)
    out = run_diff_review(project, diff, stat, agents, overrides, on_done=_progress,
                          posture=args.posture)

    state = load_state(project)
    state[scope] = {"fingerprint": fp, "decision": out.decision,
                    "run_dir": str(out.run_dir), "ts": time.time()}
    save_state(project, state)
    if args.background:
        write_pending(project, out.decision, out.findings, out.run_dir)

    _print_outcome(out)
    if args.json:
        print(json.dumps({
            "decision": out.decision,
            "findings": [f.to_dict() for f in out.findings],
            "run_dir": str(out.run_dir),
        }, indent=2))
    return out.exit_code


def cmd_work(args: argparse.Namespace) -> int:
    """Plan, execute, verify, fix: the full loop with one writer and two checkers."""
    project = Path(args.project or ".").resolve()
    context = None
    if args.context:
        context = Path(args.context).read_text(encoding="utf-8")
    agents = _split_agents(args.agents)
    overrides = _load_overrides(project)
    print(f"council work: {len(agents)} agents, max {args.rounds} fix round(s), {project}",
          file=sys.stderr)
    out = run_work_loop(project, args.task, agents, overrides, args.rounds, context,
                        on_done=_progress)
    _print_outcome(out)
    if args.json:
        print(json.dumps({
            "decision": out.decision,
            "findings": [f.to_dict() for f in out.findings],
            "run_dir": str(out.run_dir),
        }, indent=2))
    return out.exit_code


def cmd_hook(args: argparse.Namespace) -> int:
    """Entry point for Claude Code Stop / UserPromptSubmit hooks. Never raises."""
    from . import hooks

    project = Path(args.project or ".").resolve()
    try:
        if args.event == "stop":
            return hooks.stop_hook(project, args.cooldown)
        return hooks.prompt_hook(project)
    except Exception as exc:  # a hook must never break the user's turn
        print(f"council hook: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 0


def cmd_ask(args: argparse.Namespace) -> int:
    project = Path(args.project or ".").resolve()
    cfg = AgentConfig(name=args.to, timeout_s=args.timeout, posture=args.posture,
                      prompt_dir=str(project / COUNCIL_DIR / "prompts"))
    print(f"council ask -> {args.to}", file=sys.stderr)
    res = run_agent(args.to, prompts.ask_prompt(args.to, str(project), args.question,
                                                args.posture != "read_only"),
                    str(project), cfg)
    _progress(res)
    if not res.ok:
        print(res.error, file=sys.stderr)
        return 1
    print(res.text)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    project = Path(args.project or ".").resolve()
    run = latest_run(project)
    if run is None:
        print("council: no runs in this project", file=sys.stderr)
        return 1
    meta_path = run / "run.json"
    events_path = run / "events.jsonl"
    print(f"last run: {run.name}")
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        print(f"  mode:  {meta.get('mode')}")
        if meta.get("task"):
            print(f"  task:  {meta['task']}")
        if meta.get("diff_stat"):
            print(f"  diff:  {meta['diff_stat']}")
    if events_path.exists():
        lines = events_path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("event") in ("synthesis", "start"):
                print(f"  {ev['event']}: { {k: v for k, v in ev.items() if k not in ('findings','verdicts','agents','ts','event')} }")
    syn = run / "synthesis.md"
    if syn.exists():
        print(f"\n{'-' * 60}")
        print(syn.read_text(encoding="utf-8")[:4000])
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Prove each harness answers in headless mode before trusting a council."""
    project = Path(args.project or ".").resolve()
    print(f"probing 3 harnesses in {project}")
    from .gitutil import git_summary

    print(f"  git: {git_summary(project)}")
    worst = 0
    for name in AGENTS:
        res = run_agent(name, "Reply with exactly one word: PONG",
                        str(project), AgentConfig(name=name, timeout_s=args.timeout))
        _progress(res)
        if not res.ok:
            worst = 1
        elif "PONG" not in res.text.upper():
            print(f"  [{name}] answered but not PONG: {res.text[:80]!r}", file=sys.stderr)
            worst = 1
    return worst


def cmd_init(args: argparse.Namespace) -> int:
    project = Path(args.project or ".").resolve()
    root = project / COUNCIL_DIR
    root.mkdir(parents=True, exist_ok=True)
    cfg_path = root / "config.json"
    if not cfg_path.exists():
        cfg_path.write_text(json.dumps({
            "agents": list(AGENTS),
            "review": {"max_chars": 60000},
            "overrides": {
                "claude": {"timeout_s": 600},
                "dsh": {"timeout_s": 600},
                "opencode": {"timeout_s": 600},
            },
        }, indent=2), encoding="utf-8")

    gitignore = project / ".gitignore"
    entry = f"{COUNCIL_DIR}/runs/"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if entry not in existing:
        with gitignore.open("a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(f"{entry}\n")

    print(f"council initialised in {project}")
    print(f"  config: {cfg_path}")
    print(f"  ignore: {entry}")

    if args.hook:
        return _install_hook(project)
    return 0


def _install_hook(project: Path) -> int:
    hooks_dir = project / ".git" / "hooks"
    if not hooks_dir.exists():
        print("  hook: not a git repo, skipped", file=sys.stderr)
        return 1
    hook = hooks_dir / "pre-commit"
    hook.write_text(
        "#!/bin/sh\n"
        "# Installed by council init. Bypass with COUNCIL_SKIP=1 or git commit --no-verify.\n"
        '[ -n "$COUNCIL_SKIP" ] && exit 0\n'
        'command -v council >/dev/null 2>&1 || exit 0\n'
        "council review --staged --if-changed\n"
        "status=$?\n"
        'if [ "$status" -eq 1 ]; then\n'
        '  echo "council: review unavailable; allowing commit (COUNCIL_FAIL_CLOSED=1 to block)" >&2\n'
        '  [ -n "$COUNCIL_FAIL_CLOSED" ] && exit 1\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$status" -eq 2 ]; then echo "council: commit blocked by peer review" >&2; fi\n'
        'exit "$status"\n',
        encoding="utf-8",
        newline="\n",
    )
    try:
        hook.chmod(0o755)
    except OSError:
        pass
    print(f"  hook: {hook}")
    return 0


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------


def _split_agents(raw: str | None) -> list[str]:
    if not raw:
        return list(AGENTS)
    names = [a.strip() for a in raw.split(",") if a.strip()]
    unknown = [a for a in names if a not in AGENTS]
    if unknown:
        raise SystemExit(f"unknown agent(s): {', '.join(unknown)}; known: {', '.join(AGENTS)}")
    return names


def _load_overrides(project: Path) -> dict:
    cfg = project / COUNCIL_DIR / "config.json"
    if not cfg.exists():
        return {}
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data.get("overrides") or {}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="council",
        description="Run one task through Claude, DSH and OpenCode, then make them review each other.",
    )
    p.add_argument("--project", help="project directory (default: current)")
    sub = p.add_subparsers(dest="command", required=True)

    t = sub.add_parser("task", help="plan a task with three agents and cross-review the plans")
    t.add_argument("task", help="the task text")
    t.add_argument("--agents", help="comma-separated subset: claude,dsh,opencode")
    t.add_argument("--context", help="file with extra project context")
    t.add_argument("--json", action="store_true", help="also print machine-readable JSON")
    t.add_argument("--posture", choices=POSTURES, default="shell",
                   help="read_only | shell (run commands, no edits) | autonomous (full)")
    t.set_defaults(func=cmd_task)

    w = sub.add_parser(
        "work",
        help="plan, execute, verify and fix: one writer, two checkers, real commands",
    )
    w.add_argument("task", help="the task text")
    w.add_argument("--agents", help="comma-separated subset: claude,dsh,opencode")
    w.add_argument("--context", help="file with extra project context")
    w.add_argument("--rounds", type=int, default=3, help="max fix rounds (default 3)")
    w.add_argument("--json", action="store_true")
    w.set_defaults(func=cmd_work)

    r = sub.add_parser("review", help="cross-review a real diff (the pre-commit gate)")
    r.add_argument("--staged", action="store_true", help="review staged changes")
    r.add_argument("--diff", metavar="REF", help="review changes against a git ref")
    r.add_argument("--path", help="limit review to a path")
    r.add_argument("--agents", help="comma-separated subset")
    r.add_argument("--max-chars", type=int, default=60000)
    r.add_argument("--json", action="store_true")
    r.add_argument("--posture", choices=POSTURES, default="shell",
                   help="read_only | shell (run commands, no edits) | autonomous (full)")
    r.add_argument("--if-changed", action="store_true",
                   help="reuse the last verdict when the diff is byte-identical")
    r.add_argument("--background", action="store_true",
                   help="write .council/pending.md for the next turn (hook mode)")
    r.set_defaults(func=cmd_review)

    h = sub.add_parser("hook", help="entry point for Claude Code hooks")
    h.add_argument("--event", choices=["stop", "prompt"], required=True)
    h.add_argument("--cooldown", type=int, default=300,
                   help="minimum seconds between background reviews (default 300)")
    h.set_defaults(func=cmd_hook)

    a = sub.add_parser("ask", help="ask one agent a single question")
    a.add_argument("--to", required=True, choices=list(AGENTS))
    a.add_argument("question")
    a.add_argument("--timeout", type=int, default=600)
    a.add_argument("--posture", choices=POSTURES, default="shell",
                   help="read_only | shell | autonomous")
    a.set_defaults(func=cmd_ask)

    s = sub.add_parser("status", help="show the latest run in this project")
    s.set_defaults(func=cmd_status)

    d = sub.add_parser("doctor", help="check that all three harnesses answer headless")
    d.add_argument("--timeout", type=int, default=300)
    d.set_defaults(func=cmd_doctor)

    i = sub.add_parser("init", help="create .council/config.json and gitignore entry")
    i.add_argument("--hook", action="store_true", help="also install a pre-commit gate")
    i.set_defaults(func=cmd_init)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ncouncil: interrupted", file=sys.stderr)
        return 130
