# STATUS — agent-council

## Now
Prompt-time council with a quality pipeline shared by `review` and `consult`:
1. the project verify command runs first (config or ecosystem detection); a failure is a
   **confirmed blocker** with real output;
2. changed files are expanded with callers and recent commits; README/STATUS/CLAUDE claims and
   `.council/checklist.md` lessons are injected;
3. all three agents run in a **disposable git worktree snapshot**, each with its own lens
   (correctness / regression & contracts / tests & docs);
4. every finding may carry a `REPRO:` one-liner that the council executes in the snapshot —
   non-zero confirms, zero refutes;
5. a cross-examination round lets each agent confirm/refute the others' findings and add new ones;
6. **only confirmed blocker/major findings block**; confirmed classes are appended to the lessons
   checklist. An adaptive tier (lean / standard / full) picks the stages from real signals — diff
   size, changed files, security paths, test gap, action verbs — so a conversational turn costs
   three small calls while a risky change gets the full pipeline.
Every verdict carries the reviewed commit and time, and a pending verdict for an older HEAD is
prefixed `[STALE: ...]`. `--tier` can force a tier for one run. `work` now runs verify first and
uses verify/repro/lessons too. A seeded-defect benchmark (8 defects, fixture repo) runs lean /
standard / full and records recall, false blockers, time and cost. 134 unit tests green.

## Next (max 3)
1. Finish the seeded benchmark, record the table (recall / false blockers / time / $ per tier) and
   tune the lenses against any missed defect.
2. Measure the automatic-mode cost of a real day: lean consults per message, standard Stop reviews.
3. Decide whether `work` should also review a snapshot (it currently reviews the live tree).

## Blocked
- Nothing.

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

## Verified (dated evidence)
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
