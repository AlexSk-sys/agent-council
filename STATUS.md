# STATUS — agent-council

## Now
Prompt-time council is live: every user message starts a detached three-agent consult
(user message + transcript tail + uncommitted diff). If a run is active, the message is queued
(latest wins) and the running child starts it on exit; verdicts are appended to
`.council/pending.md` and printed into the next user turn. The first live run reviewed this very
change and returned `BLOCK: 17 finding(s) with evidence`; the automatic Stop review that followed
returned `BLOCK` again, including a real blocker: a consult used to overwrite the diff gate's
cache. Fixed since: consult never writes `worktree`, Stop reviews are deferred (`review_due`)
instead of dropped, the lock is an O_EXCL test-and-set with pid liveness and a 900 s TTL, manual
`review/task/work/consult` take the lock too, state writes are atomic, pending is append-only,
`.council/` runtime files are self-ignored, and `--max-chars` is forwarded. 95 unit tests green.

## Next (max 3)
1. Confirm the deferred-review path live: change a file while a consult holds the lock, then let
   the next Stop run and check that a `-review-` run directory appears for the newer diff.
2. Measure the cost envelope of one consult per message (30 s cooldown, read-only posture):
   model calls, latency, pending noise over a day of real use.
3. Optional: per-project model override so the three peers are not all on the same model route.

## Blocked
- Nothing.

## Known limits (by design, not defects)
- A consult's verdict is delivered at the **next** user turn; the hook API cannot inject text into
  the turn that triggered it. If the user stops typing, the verdict stays in `.council/pending.md`.
- The lock is **project-scoped**: a second concurrent session on the same repo waits for the
  running council instead of doubling the model calls.
- Sessions outside a git repo are consulted on purpose (the user asked for every session);
  the Stop diff review remains repo-only because there is nothing to diff.

## Verified (dated evidence)
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
