# STATUS — agent-council

## Now
One defect, one entry, and closure with proof:
1. `finding_key` + `merge_findings` collapse `file:line` duplicates after the cross round
   (real run: 17 findings -> 14 entries -> 9 confirmed blocker/major);
2. findings already in `.council/checklist.md` render under **Known open**, the count reads
   "N new, M known open", and a known defect vetoes only a diff that touches its file;
3. a consult with **no diff** is `ADVISORY` (exit 0) and `pending_is_news` suppresses the
   message entirely when it would only repeat the checklist — a question no longer returns as
   a pile of "new errors"; the Stop review with a diff is still the gate;
4. `council drain` closes confirmed findings only when their own REPRO flips non-zero -> zero
   and the verify command stays green; a finding without a repro is `skipped`, and `skipped`
   is not `clear` (exit 2). `--dry-run` executes the repros only;
5. `council doctor` fails when fewer than three distinct models are configured.
The verify/snapshot/lenses/repro/cross pipeline and the adaptive tiers are unchanged.
168 unit tests green.

## Next (max 3)
1. Drain agent-council's own backlog (`council drain --project .`): known open are
   `adapters.py:169` (POSIX `killpg` without `start_new_session` - one timed-out voice can
   SIGKILL the run) and opencode silently dropping `cfg.effort`.
2. One real `council drain` on yt-stories (7 confirmed: 5 fixable, 2 without a repro) once the
   user approves edits to the stories.
3. Measure one day of automatic consults after the change: count `pending.md` writes - the
   expected result is "only new confirmed findings", i.e. mostly none.

## Blocked
- Nothing.

## Verified (dated evidence)
- **2026-11-19 — noise cut and real closure.** Real run `20261118-233507` (yt-stories, 3 models):
  17 raw findings -> 14 after root-cause merge -> 9 confirmed blocker/major -> 8 new + 1 known
  open. `council drain --dry-run` on run `20261118-235542`: 7 confirmed, 5 "would fix", 2
  "skipped: no repro". Live drain on the `sandbox/drain-e2e` fixture with `glm-5.3-flash`:
  `repro_before=1 -> repro_after=0`, `fixed=1`, 8.3 s, $0.0266, `calc.py` changed `a - b` to
  `a + b`; the entry says fixed only because the repro flipped and verify stayed green.
  `py -3 -m unittest discover -s tests` -> `Ran 168 tests ... OK`.
- **Models split across harnesses** (`adapters.DEFAULT_MODELS`): claude → `glm-5.3-flash`,
  dsh → `deepseek-v4.1-flash` (via `--patch`), opencode → `opencode-go/qwen3.8-flash`.
  Live trivial calls: 2.0 s / 5.2 s / 5.6 s. `council doctor` in skin-arb-app: **all three ok**
  (1.7 s / 5.2 s / 4.6 s) and it now probes the configured model, not the harness default.
  Confirmed again in run `20261118-233507`: the three `command` lines in `events.jsonl` carry
  all three ids. `doctor` now also fails when fewer than three distinct models are configured.
- **The real bottleneck was not the models.** A 12 KB consult prompt sent dsh into 35 tool steps
  (reading a 4455-line file 30 lines per call); opencode died with `ECONNRESET` at 200 s. With a
  tool budget in the prompt ("at most 8 calls, grep, never page files over 300 lines") the same
  task answered in **64 s** with a real evidence-backed finding.
- **A timeout is now a real deadline**: `_run` kills the process tree (`taskkill /F /T`); a
  300 s timeout used to return at 375 s and leave idle `node` processes behind.
- **`_lean_retry` keeps the full config** (`dataclasses.replace`), so prompt_dir/model/extra_args
  survive the retry; the retry prompt used to be cut at its first newline. Pinned by 2 tests.
- 148 unit tests OK (`py -3 -m unittest discover -s tests`), including 4 model-forwarding tests
  (claude/opencode `--model`, dsh patch + ordering + provider split).

## Known limits (by design, not defects)
- A consult's verdict is delivered at the **next** user turn; the hook API cannot inject text into
  the turn that triggered it. If the user stops typing, the verdict stays in `.council/pending.md`.
- The lock is **project-scoped**: a second concurrent session on the same repo waits for the
  running council instead of doubling the model calls.
- Sessions outside a git repo are consulted on purpose (the user asked for every session);
  the Stop diff review remains repo-only because there is nothing to diff.
- The snapshot worktree has no `node_modules`/virtualenv; repros that need installed dependencies
  may fail there. The verify command itself runs in the live project, where they exist.
- REPRO commands are model output. The allowlist, deny list, 120 s timeout and disposable
  snapshot bound the damage, but they still execute code - keep the allowlist tight.
- Unverified findings are shown but never block; a finding is only a gate once a repro, a peer
  CHECK, or the verify command confirms it.

- **2026-11-15** — console windows, second pass: the remaining flashes came from plugin hooks,
  not the council. `security-guidance` ran `bash` on prompt/tool/stop, `hookify` ran `python3`
  on prompt/tool/stop, and `tokeneconomizer` ran a `/bin/sh` launcher on prompt/tool/stop. All
  three are rerouted to `pythonw` in their cache `hooks.json` (hookify uses a launcher that puts
  the plugin root on `sys.path`); `PYTHONUTF8=1` was added globally. Running all three patched
  hooks creates **no new conhost/cmd processes**. `security-guidance` stays enabled; only its
  launcher changed. Plugin cache edits are not durable across plugin updates.
- **2026-11-15** — console windows on every prompt fixed: all global hooks and the status line
  now run `pythonw.exe`, and every child process in the council (`git`, `npm`, the `.cmd` agent
  shims) uses `CREATE_NO_WINDOW` via `council/win.py`; the global hook scripts and statusline set
  the flag inline. Hooks compile, `settings.json` parses, 137/137 tests OK. Visual confirmation
  on the next prompt is the user's check.
- **2026-11-15** — work hardening from its own consult: REPRO lines now run only in a disposable
  snapshot (a `git checkout -- .` repro leaves the live tree untouched,
  `test_work_repros_do_not_touch_the_live_tree`); verify re-runs after every executor pass and
  the fresh output goes to reviewers (`test_work_rechecks_verify_after_execution`); work blocks
  only on confirmed findings. Build: 136/136 OK.
- **2026-11-15** — cold seeded benchmark (8 defects; each tier reset including `.council`;
  de-duplicated matcher; one run per tier):
  | tier | decision | recall | confirmed | false blockers | time | cost |
  |---|---|---|---|---|---|---|
  | lean | BLOCK | 7/8 | 24 | 0 | 321.9 s | $0.4152 |
  | standard | BLOCK | 8/8 | 23 | 0 | 362.6 s | $0.4060 |
  | full | BLOCK | 8/8 | 25 | 0 | 450.9 s | $0.3804 |
  Lean missed the mutable-default defect; standard matched full with ~90 s less wall time;
  full did not improve recall. Cost is not monotonic (output length dominates). One seed run per
  tier, so no variance estimate yet. After the run, the correctness lens gained an explicit
  "shared mutable state (default arguments, aliasing)" hint; the next run must validate it.
  Evidence: `benchmarks/results.json`, `benchmarks/results-first-pass.json` (contaminated pass,
  archived), full run dir kept under the fixture's `.council/runs/`.
- **2026-11-15** — first benchmark pass (preliminary, not publishable): lean `8/8`, 269.5 s,
  $0.4256; standard `6/8`, 452.9 s, $0.5219; full `8/8`, 492.3 s, $0.5150. The standard/full rows
  are contaminated: the fixture's `.council` lessons from the previous tier were injected, and
  the matcher over-counted duplicate descriptions as false blockers. Both are fixed (cold reset
  per tier, deduplicated scoring); a clean re-run is in progress.
- **2026-11-15** — stamps and staleness: `run.json` carries the head commit and ts; synthesis and
  pending show `(reviewed <hash> @ time)`; `_print_pending` prefixes `[STALE: ...]` when HEAD
  moved. Tests: `TestPendingStaleness`.
- **2026-11-15** — `work` pipeline: verify failure aborts before any model
  (`test_work_aborts_before_models_when_verify_fails`), reviewers get verify/impact/docs/lessons,
  REPRO lines are executed and lessons appended.
- **2026-11-15** — tier fixes from the next consult: untracked-only changes no longer report
  "no uncommitted change"; the lean transcript keeps the newest 6k chars; docs is a real config
  key; `--tier` forces a tier. Tests: 134/134 OK.
- **2026-11-15** — benchmark fixture committed at `C:\Users\AgentPC\source\repos\council-benchmark`
  (clean baseline `79e7401`, seed patch `dc09db1`), eight defects across off-by-one, empty input,
  page overlap, aliasing, file leak, mutable default, swallowed error, unicode slug; the seeded
  tree passes its own 8 tests by design. Runner: `benchmarks/run_benchmark.py`.
- **2026-11-15** — correction from the next consult: the live lean run exercised only the
  `size == 0` shortcut; the message / security-path / test-gap branches are covered by unit
  tests, not by a live standard/full run. Also fixed: lean/standard tiers used to render an empty
  docs block as "(no project docs)", a false fact; they now say the context was not preloaded and
  the agent should read the files itself. Tests: lean/standard context assertions, 127/127 OK.
- **2026-11-15** — adaptive tiers verified live: a clean game repo + conversational message chose
  **lean** (`no uncommitted change`), options `cross=false impact=false docs=false`, verify
  `npm test` PASS. 3/3 agents answered (dsh 27.9 s, claude 141.3 s **$0.3155**, opencode 247.3 s),
  no cross round, verdict `APPROVE: 1 confirmed, 9 finding(s)`; cost $0.3155 vs $0.7463 for the
  full review, so the automatic path is ~58% cheaper on this pair. The lean run found a real minor
  in the game (score credited after a mid-frame game over) and an untested `trigger == "stop"`
  tier branch; both fixed with tests (game 14/14, council 125/125).
- **2026-11-15** — full pipeline E2E on the game repo with a seeded defect (a combo-bonus
  scoring change shipped without a test): verify `npm test` **PASS** (13 tests) → snapshot
  worktree with 1 copied file → round 1 (claude 104.0 s $0.2461, dsh 150.0 s, opencode 85.3 s)
  → 9 findings → REPRO runs: **3 confirmed majors**, each agent independently reverted the bonus
  line and showed the suite stays green → cross round (claude 262.1 s $0.5002, dsh 119.6 s,
  opencode 117.6 s) confirmed them → `BLOCK: 3 confirmed blocker/major, 9 finding(s)`, exit 2.
  The same run caught real encoding damage (BOM + cp1251 mojibake) with an executable repro.
  Total model cost **$0.7463**, wall ~7 min. The seeded change was reverted; the game repo is clean.
- **2026-11-15** — regression from that run: a REPRO that exits 0 despite a real defect used to
  refute it; a peer `CHECK ... CONFIRM` now overrides a zero-exit repro. Lessons dedupe by
  `file:line:severity` as well as wording, so three peers describing one defect add one lesson.
  `Ran 113 tests ... OK`.
- **2026-11-15** — quality pipeline implemented: verify detection/run, disposable worktree
  snapshot (`Snapshot`), lenses, `REPRO` parsing + execution, cross-examination (`CHECK`/`NEW`),
  hard exit (`decide_review`), impact context, doc claims, lessons checklist.
  `py -3 -m unittest discover -s tests` → `Ran 111 tests ... OK`.
- **2026-11-15** — new unit coverage: `TestReproParsing`, `TestParseChecks`, `TestDecideReview`,
  `TestProjectContext` (verify detection and override, allowlist, confirmed/refuted/blocked
  repros, lesson dedup, pipeline options, snapshot copies the working tree and cleans up).
- **2026-11-15** — live consult E2E: `{"prompt": "...", "transcript_path": "..."} | pythonw.exe -m
  council hook --event prompt` → exit 0, `.council/running.json` written, detached child,
  `council consult: 3 agents, prompt 142 chars, transcript 2661 chars, diff 37544 chars`.
  3/3 agents answered (dsh 228.1 s); verdict `BLOCK: 17 finding(s) with evidence`; the verdict
  was printed into the next user turn from `pending.md`; run dir
  `.council/runs/20261114-235511-consult-...` with `session.md`, `context.md`, `diff.patch`,
  three raw answers and `synthesis.md`.
- **2026-11-15** — blocker fix from that run: the Stop review used to be dropped while a consult
  held the lock. Now `stop_hook` stores `review_due` and the next free Stop runs it —
  `test_stop_hook_defers_review_while_consult_runs` passes (first Stop: no spawn, `review_due`
  set; second Stop after release: one spawn). Live exercise of this exact path is still pending
  (Next #1).
- **2026-11-15** — lock hardening: `acquire_lock` refuses when a live run holds the lock; the pid
  is recorded after the spawn; a dead pid clears the lock immediately
  (`test_dead_pid_clears_the_lock_immediately`); TTL lowered 1800 → 900 s.
- **2026-11-15** — pending hardening: two verdicts both survive (`[background council review]` and
  `[background council consult]`), and a failed print no longer deletes the file (tests
  `test_pending_keeps_both_verdicts`, `test_pending_survives_a_failed_print`).
- **2026-11-15** — `py -3 -m unittest discover -s tests` → `Ran 90 tests ... OK`.
- **2026-11-15** — live Stop review of the same tree started automatically after the consult
  released the lock (`council review: 3 agents, 45553 chars of diff`, dsh 382.2 s) and returned
  `BLOCK: 17 finding(s) with evidence`. Its blocker (a consult overwriting the diff gate's
  `worktree` cache with an advisory verdict) is fixed and pinned by
  `test_consult_does_not_overwrite_the_diff_gate_cache`: a stored `block` review survives a
  consult on "thanks".
- **2026-11-15** — queue for messages typed while a run is active: the hook stores one
  `consult_queued` entry (latest wins) and `drain_queue` starts it when the running child exits.
  Tests: `test_busy_hook_queues_the_message_instead_of_dropping_it`,
  `test_latest_queued_message_wins`, `test_drain_starts_the_queued_consult`.
- **2026-11-15** — lock is a true test-and-set (`O_CREAT|O_EXCL`); a second acquire is refused
  (`test_second_acquire_is_refused`). Manual `review/task/work/consult` now take the same lock.
- **2026-11-15** — `.council/.gitignore` is written on first use (`*`, `!.gitignore`,
  `!config.json`), so `state.json`, `running.json`, `inbox/` and logs can never be committed;
  a raw user message is also deleted by the child after it reads it.
- **2026-11-15** — `py -3 -m unittest discover -s tests` → `Ran 95 tests ... OK`.
- **2026-11-14** — headless contracts probed: claude 18.1 s; `--bare` 7.5 s at 1350 input tokens
  ($0.0055) vs 32k ($0.128); dsh 5.7 s; opencode 6.1 s.
- **2026-11-14** — seeded-bug review: all 3 agents found the off-by-one, all 3 found the test that
  certifies the bug → `BLOCK: 4 blocker(s), 14 finding(s)`, exit 2, 118 s, Claude $0.121.
- **2026-11-14** — `work` loop on the same bug: opencode executed (`range(lo, hi + 1)` + 3 new
  tests), 2 peers verified, result `APPROVE ... 0 blocker(s) remain`. Independent check:
  `python -m pytest -q` → `4 passed`; `parse_range('1-5')` → `[1, 2, 3, 4, 5]`.
- **2026-11-14** — hooks wired into `~/.claude/settings.json` (Stop + UserPromptSubmit), exec form
  with `pythonw.exe`, no console window.
- **2026-11-14** — END TO END on the user's other live session (stream cat): Stop hook resolved
  `C:\Users\AgentPC\stream-cat` from the transcript, 3/3 agents answered, `approve`, 5 evidenced
  findings including a real rendering defect.
