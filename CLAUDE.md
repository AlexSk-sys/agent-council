# CLAUDE.md — agent-council

## What this is
A CLI that runs Claude Code, DeepSeek Harness (`dsh`) and OpenCode against the same repository so
they review, and in `work` mode fix, each other's output. Single package, stdlib only.

## Commands
```powershell
py -3 -m unittest discover -s tests -v      # verify (181 tests, no network)
py -3 -m pip install -e .                   # installs the `council` entry point
council doctor                              # prove all 3 harnesses answer headless
council review --path sandbox               # cross-review a diff
council --project sandbox work "<task>"     # plan -> execute -> verify -> fix
council --project <p> consult --prompt-file <f>  # three agents join a user message
council --project <p> review --tier full    # force lean | standard | full
council --project <p> drain --dry-run       # what the last run proved, no edits
council --project <p> drain                 # fix each confirmed finding, prove by repro
py -3 benchmarks\run_benchmark.py           # seeded-defect benchmark across tiers
```

## Automatic mode (already wired)
`~/.claude/settings.json` runs two hooks:
- `UserPromptSubmit` → `council hook --event prompt` → detached `council consult`
  on every user message: the message, transcript tail and uncommitted diff go
  to all three agents. Verdict appears in `.council/pending.md` and is printed
  into the next user turn.
- `Stop` → `council hook --event stop` → diff review, as before.
A shared lock (`.council/running.json`, TTL 3600 s (a live child keeps it past the TTL), child pid checked for liveness, O_EXCL create)
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
- `council/drain.py` — closes a run's confirmed findings with proof (repro flips
  non-zero → zero, verify stays green); `--dry-run` executes the repros only.
- `council/gitutil.py` — diff collection; untracked files are included as full additions.
- `council/store.py` — `.council/runs/<id>/`; the only shared state between harnesses.
- `council/cli.py` — argparse. Exit codes: 0 approve, 2 block, 1 error.
- `sandbox/` — gitignored fixture repo for the E2E test.

## Models: three harnesses, three models
All three harnesses talk to one gateway (`opencode.ai/inference/go/anthropic`) but run different
models, so a queue on one model cannot stall the whole council. Defaults live in
`adapters.DEFAULT_MODELS` (verified live 2026-11-18):

| harness | model | how it is passed |
|---|---|---|
| claude | `glm-5.3-flash` | `--model` |
| dsh | `deepseek-v4.1-flash` | `--patch` overlay (`agent-default-model`) |
| opencode | `opencode-go/qwen3.8-flash` | `--model provider/model` |

A project overrides any of them in `.council/config.json` under `overrides.<agent>.model`
(and `.effort`). `council doctor` prints the model it actually probed.

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
    quirk 1). If a run holds `.council/running.json` (O_EXCL create, TTL 3600 s (a live child keeps it past the TTL), pid checked for
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
    `--tier` forces one for a single run (used by the benchmark).
17. **Every verdict is stamped.** `run.json` carries the head `commit` and `ts`; the synthesis
    and `pending.md` show `(reviewed <hash> @ <time>)`. When a pending verdict is printed and
    HEAD has moved, `_print_pending` prefixes `[STALE: ...]`. Never drop the stamp - it is the
    only defence against acting on a verdict for code that no longer exists.
18. **`work` uses the same facts as review.** It runs verify first (failure = block before any
    model), passes verify/impact/docs/lessons to reviewers, executes `REPRO` lines, and appends
    confirmed blocker/major classes to the lessons file. It does not snapshot (the executor
    writes the live tree).
19. **Every child process must be windowless.** The hooks and the status line run under
    `pythonw`, but `git`, `npm` and the `.cmd` harness shims are console apps: without
    `CREATE_NO_WINDOW` each one allocates a console and flashes a cmd window. `council/win.py`
    provides `kwargs()`; `gitutil`, `project`, `adapters` and `hooks` use it, and the global
    hook scripts / statusline set the flag inline. Never add a `subprocess.run` without it.
    Plugin hooks are the same class of problem: `security-guidance` ran `bash`, `hookify` ran
    `python3`, and `tokeneconomizer` ran a `/bin/sh` launcher on every prompt/tool. They are
    rerouted to `pythonw` in their cache `hooks.json` (hookify uses a launcher that puts its root
    on `sys.path`). Plugin updates can overwrite those files - re-apply if windows return.

20. **A timeout must kill the process tree.** `subprocess.run(timeout=)` kills the `.cmd` shim
    only and then blocks on a pipe the grandchildren still hold: a 300 s timeout was measured
    returning after 375 s and leaving idle `node` processes behind on every lost voice.
    `adapters._run` uses Popen + `taskkill /F /T /PID` (`_kill_tree`) so the deadline is real.
    On POSIX the child is spawned with `start_new_session=True`; without it
    `killpg(getpgid(pid))` reaches the council's own group and one slow voice SIGKILLs the
    whole run and its parent shell.
21. **The prompt carries a tool-call budget.** Without it, dsh pages a 4455-line file 30 lines
    per read: 35 steps and no answer at 200 s. With "at most 8 tool calls, grep instead of
    reading files over 300 lines" the same task answered in 64 s with a real finding.
    The block lives in both prompt templates (`prompts.py`, "HOW MUCH TO LOOK AT").
22. **dsh has no `--model` flag.** The model lives in the `agent-default-model` plugin of the
    profile, so it is pinned with a `--patch` overlay written per call. The overlay replaces
    that plugin's config object wholesale (not a deep merge): omitting `provider` fails the
    run with "ValidationError: invalid config: $.provider". `--patch` is a launcher flag and
    must come before the first app option (`--permission-mode`), or dsh exits 1 with
    "unknown option '--patch'".

23. **One defect, one entry.** `finding_key` is `file:line` (or a wording digest
    when no location was named) and `merge_findings` collapses duplicates after
    the cross round. Three peers describing one line are one blocker with
    `source = "claude+dsh+opencode"`. Never count raw findings in a verdict: 17
    findings were 9 defects and 3 root causes on a real run.
24. **Known is not new.** `project.lesson_index` maps `file:line` to the date it
    entered `.council/checklist.md`; the synthesis puts those under "Known open"
    and the count line reads "N new, M known open". A known defect blocks only
    when the current diff touches its file, so an unrelated turn cannot re-veto
    an old backlog item. `append_lessons` runs after the index is read, and
    `exclude_run` keeps a run from recognising its own fresh findings.
25. **A consult without a diff is ADVISORY.** No change, no veto: not even a
    majority of block votes can block, and the verdict is `advisory` (exit 0)
    only when a confirmed blocker/major exists, otherwise `approve`.
    `pending_is_news` suppresses the message entirely when it would only repeat
    the checklist. This is what stops every question from coming back as a pile
    of "new errors". The Stop review with a diff is still the real gate.
26. **`drain` is the only place a finding is called fixed.** A merged entry may
    carry `extra_repros` when two peers described different defects at the same
    `file:line`; every repro must flip, never just the first. For each confirmed
    blocker/major: run its REPRO in the live tree (exit 0 = already gone) →
    one executor fixes → re-run the same REPRO (exit 0 = fixed) → verify command
    green. A finding without a repro is `skipped`, and `skipped` is not `clear`
    (exit 2). No model's "looks fixed" is accepted as evidence.
27. **opencode has no effort flag, and `#variant` is rejected by the gateway**
    (`provider.no-route: Variant unavailable for ...`). Effort on that harness is a different
    model id (`qwen3.8-max`), selected through `overrides.opencode.model`. A configured
    `effort` is reported in `stderr` instead of being silently dropped. A non-zero exit is a
    failed run even when stdout holds a text event - a truncated stream is not a vote.
28. **A timed-out voice gets one more attempt with MORE time.** `needs_lean_retry`
    fires on a `timeout` verdict even when the quorum of two answered, and the retry runs
    at `max(timeout, 600)` on a prompt without impact/docs/lessons. The old 240 s retry
    was a second guaranteed loss for the slowest harness. The cross-round prompt now
    carries the tool budget too - it was the only template without it.
29. **Review is read-only by default, and that is a speed fix, not a depth cut.** dsh's
    `workspace-write` sandbox fails every shell call on this machine
    (`sandbox-local windows-acl temp grant materialization failed`). A shell review spent
    420 s and a 600 s probe spent 506 s failing at sandbox setup; in read-only dsh answers
    the same consult in ~75 s. Reviewers only read and grep - the council runs every REPRO
    itself in the snapshot - so `review` defaults to `read_only`. `--posture shell` stays
    for manual deep runs, and `work` still promotes its one executor to `autonomous`.
30. **`council doctor` fails on a model collision.** Three agents on one model
    share blind spots, so `doctor` compares the resolved model ids and returns 1
    when fewer than three distinct ones are configured.

## Rules for changes
- A new harness flag goes in `adapters.py` and nowhere else.
- A new finding format needs a parser branch plus a unit test in `tests/test_protocol.py`.
- Never let an unproven finding block a change; `test_unevidenced_blocker_does_not_block` guards it.
