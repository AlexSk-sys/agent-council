# CLAUDE.md — agent-council

## What this is
A CLI that runs Claude Code, DeepSeek Harness (`dsh`) and OpenCode against the same repository so
they review, and in `work` mode fix, each other's output. Single package, stdlib only.

## Commands
```powershell
py -3 -m unittest discover -s tests -v      # verify (95 tests, no network)
py -3 -m pip install -e .                   # installs the `council` entry point
council doctor                              # prove all 3 harnesses answer headless
council review --path sandbox               # cross-review a diff
council --project sandbox work "<task>"     # plan -> execute -> verify -> fix
council --project <p> consult --prompt-file <f>  # three agents join a user message
```

## Automatic mode (already wired)
`~/.claude/settings.json` runs two hooks:
- `UserPromptSubmit` → `council hook --event prompt` → detached `council consult`
  on every user message: the message, transcript tail and uncommitted diff go
  to all three agents. Verdict appears in `.council/pending.md` and is printed
  into the next user turn.
- `Stop` → `council hook --event stop` → diff review, as before.
A shared lock (`.council/running.json`, TTL 900 s, child pid checked for liveness, O_EXCL create)
means only one council run per project at a time. A Stop review that arrives while a consult is
running is **deferred, not dropped**: its diff fingerprint is stored as `review_due` and the next
free Stop runs it. A consult is advisory and never writes the diff gate's cache (`worktree`) —
`council review --if-changed` keeps reporting the last real review. While a run holds the lock, a
new user message is queued (latest wins) and the running child drains the queue on exit.

## Layout
- `council/adapters.py` — one runner per harness. **All CLI flags live here.** Changing a flag
  elsewhere is a bug.
- `council/protocol.py` — rounds, three-strategy parsers, deterministic merge, work loop,
  the review pipeline (lenses, repro, cross-examination, hard exit).
- `council/project.py` — model-free context: verify detection/run, impact (changed files, callers,
  commits), docs, lessons, REPRO execution and the disposable worktree snapshot.
- `council/prompts.py` — prompts. The output contract goes **last** in each prompt.
- `council/hooks.py` — Stop and UserPromptSubmit entry points, the shared run lock, the
  transcript digest, and detached spawns. Hooks must stay fast and never raise.
- `council/gitutil.py` — diff collection; untracked files are included as full additions.
- `council/store.py` — `.council/runs/<id>/`; the only shared state between harnesses.
- `council/cli.py` — argparse. Exit codes: 0 approve, 2 block, 1 error.
- `sandbox/` — gitignored fixture repo for the E2E test.

## Quirks that cost time (do not "simplify" these away)
1. **Multi-line args are truncated at the first newline by `cmd.exe`.** All three CLIs are npm
   `.cmd` shims. `_materialize_prompt` writes the prompt to a file and passes a pointer. Removing
   it silently degrades every run to "your message ended at the preamble".
2. **`--allowed-tools` is variadic** — `--allowed-tools=A,B` (equals form) or it swallows the
   positional prompt.
3. **`claude --bare`** is 23× cheaper than the full prompt path. Peer calls must use it.
4. **`dsh` needs `TEMP=C:\dsh-tmp`**; its sandbox rejects a temp dir inside the workspace.
5. **Agents do not reliably answer in JSON.** Keep the line-format parser first and the markdown
   fallback; both were needed on real output.
6. **Only one agent may write.** `work` promotes one executor to `autonomous`; reviewers stay
   `shell` so they cannot edit what they review.
7. **Hooks must never break a turn.** `council hook` catches everything and returns 0. The Stop
   hook spawns a detached child; it does not wait for the review. Dedup is by diff hash, plus a
   300 s cooldown, because the hook fires after *every* assistant turn.
8. **`--project` is a top-level flag, so it must come BEFORE the subcommand.** `council review
   --project X` is rejected by argparse, and a background child dies silently that way. The
   spawn now writes `.council/background.log` instead of `DEVNULL` — never pipe a detached child's
   stderr to nowhere; that is how this bug hid.
9. **The project is resolved from the transcript, not only from cwd.** `resolve_project` falls back
   to the newest `file_path` in the session transcript when cwd is not a repo. A folder without
   `.git` can never be reviewed; `council doctor` prints that in one line, and `git_summary` in
   `gitutil.py` is the single place that wording lives.
10. **Hooks run `pythonw.exe` through the exec form (`command` + `args`), never a shell.** A
    console-subsystem `python` started by a GUI-less parent allocates a console and flashes a cmd
    window on every turn; the user rejected that. `pythonw` writes to a redirected stdout normally
    (verified with `Start-Process -RedirectStandardOutput`).
11. **The consult hook is prompt-time, not diff-time.** It fires on every user message; the
    message is written to `.council/inbox/` and passed as a file path (newline truncation, see
    quirk 1). If a run holds `.council/running.json` (O_EXCL create, TTL 900 s, pid checked for
    liveness), the message is queued (`consult_queued`, latest wins) and the running child drains
    it on exit. The 30 s cooldown applies only when no message is queued. The Stop review checks
    the same lock, so six models never run on one tree, and a review that arrives during a run is
    deferred (`review_due`). The child releases the lock only with the token it was given - never
    another run's.
12. **The review pipeline is deterministic-first.** Verify command → snapshot → round 1 → REPRO
    execution → cross-examination → hard exit. A failing verify command is a confirmed blocker
    without asking a model. Each stage can be disabled in `.council/config.json` under `pipeline`.
13. **REPRO commands are model output and are executed.** Only first tokens in `REPRO_TOKENS` are
    allowed, `REPRO_DENY` is checked, the command runs with a 120 s timeout **inside the
    disposable worktree**, never in the live tree. Exit non-zero confirms, zero refutes. If
    isolation is unavailable, a repro still runs in the project root - keep the allowlist tight.
14. **Only CONFIRMED blocker/major findings block.** Confirmation is an executed repro, a peer
    CHECK, or a deterministic verify failure. Unverified findings are shown but never veto -
    otherwise one agent's unproven assertion can block any change.
15. **Lessons are deduped and never include verify failures.** `.council/checklist.md` is a list
    of confirmed failure classes, injected into later prompts. A flaky test run is not a class.
16. **Tiers are chosen from signals, not from a fixed policy.** `project.choose_tier` reads diff
    size, file count, security paths, test gap and action verbs, and returns lean/standard/full;
    `apply_tier` resolves `"auto"` stage flags, while an explicit boolean in the config wins.
    The chosen tier and reason are in `run.json`. Never hard-code a tier in `protocol.py`.

## Rules for changes
- A new harness flag goes in `adapters.py` and nowhere else.
- A new finding format needs a parser branch plus a unit test in `tests/test_protocol.py`.
- Never let an unproven finding block a change; `test_unevidenced_blocker_does_not_block` guards it.
