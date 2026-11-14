# STATUS — agent-council

## Now
Always-on mode, quiet: both hooks run `pythonw.exe` in exec form (no shell, no console window), and
the reviewed project is resolved from the session transcript when cwd is not a repo. 63 unit tests
green. Verified on the user's other live session (the animated stream cat): its Stop hook resolved
`stream-cat`, three agents reviewed 9400 chars, verdict `approve` with 5 evidenced findings.

## Next (max 3)
1. Use it on a real repo: `council review --diff HEAD` on the next non-trivial change in the user's active project.
2. Decide whether the pre-commit gate is worth its cost per commit (~$0.1, ~2 min).
3. Optional: per-project model override so the three peers are not all the same model route.

## Blocked
- Nothing. `council doctor` passes for all three harnesses.

## Verified (dated evidence)
- **2026-11-14** — headless contracts probed: claude 18.1 s; `--bare` 7.5 s at 1350 input tokens
  ($0.0055) vs 32k ($0.128); dsh 5.7 s; opencode 6.1 s.
- **2026-11-14** — `py -3 -m unittest discover -s tests` → `Ran 44 tests ... OK`.
- **2026-11-14** — multi-line argv truncation proven: a two-line prompt to `dsh` arrived as
  `LINE1_MARK` only; all three reported "your message ended at the preamble". Fixed by
  `_materialize_prompt` (file + one-line pointer).
- **2026-11-14** — seeded-bug review: all 3 agents found the off-by-one, all 3 found the test that
  certifies the bug → `BLOCK: 4 blocker(s), 14 finding(s)`, exit 2, 118 s, Claude $0.121.
- **2026-11-14** — `work` loop on the same bug: opencode executed (`range(lo, hi + 1)` + 3 new
  tests), 2 peers verified, result `APPROVE ... 0 blocker(s) remain`. Independent check:
  `python -m pytest -q` → `4 passed`; `parse_range('1-5')` → `[1, 2, 3, 4, 5]`.
- **2026-11-14** — self-review bug found and fixed: the loop reported "334361 chars changed" on a
  one-line fix because `.council/` and `__pycache__` were in the diff. After the pathspec fix the
  same diff measures **899 chars**. `:(exclude)**/__pycache__` matches nothing in this git
  version — only `:(exclude)__pycache__/**` works (tested both).
- **2026-11-14** — clean re-review of the repaired diff: `APPROVE: 0 blocker(s), 9 finding(s)`,
  3/3 answered, 109 s, Claude $0.109.
- **2026-11-14** — `council init --hook` writes config + pre-commit gate; `council status` reads
  the latest run and prints its synthesis.
- **2026-11-14** — hooks wired into `~/.claude/settings.json` (Stop + UserPromptSubmit), all four
  previous hooks kept; `json.load` reports OK and lists all five commands. Pipe-test outside a git
  repo: both return exit 0 with no output. `py -3 -m unittest discover -s tests` → 54 tests OK.
  Pre-commit regenerated with `--if-changed` and fail-open on exit 1.
- **2026-11-14** — full hook chain exercised for real in `sandbox/`: `{} | council hook --event
  stop` → exit 0 → detached child → **new run dir + `3 agents, 947 chars of diff`** → review
  finished in ~104 s → `state.json` got `worktree` (fingerprint, `decision: approve`, run_dir) and
  `.council/pending.md` holds `APPROVE: 13 finding(s) with evidence`.
- **2026-11-14** — bug found by that run: the spawn passed `--project` *after* the subcommand, so
  argparse rejected it and the first detached child died silently under `DEVNULL` (no run dir, no
  process). Fixed the argument order, redirected child output to `.council/background.log`, added
  two regression tests.
- **2026-11-14** — CORRECTION to an earlier entry: `C:\Users\AgentPC\source\repos` **is** a git
  repository (branch `fix/bugs-and-ui-redesign`); a `Glob` for `.git/HEAD` missed the hidden dir and
  I wrongly concluded it was not. The real cause of the silent Stop hook was different: that session's
  cwd was `C:\Users\AgentPC` — the home directory, which is not under git.
- **2026-11-14** — Stop hook now resolves the project from the session transcript when cwd is not a
  repo. Verified on the live session transcript: `resolve_project` → `C:\Users\AgentPC\stream-cat`
  (its topic is the animated stream cat; it edits `js/cat.js`). That folder had no `.git`
  (593 files, 23.7 MB), so it was made a repository with a baseline commit `1ff90ac` — 9 tracked
  files, `captures/` ignored — and `git_summary` now reports "git repository, clean tree".
  `py -3 -m unittest discover -s tests` → 60 tests OK.
- **2026-11-14** — hooks re-added in exec form with `pythonw.exe` after the user rejected cmd windows
  popping up every turn. `pythonw` verified to write to a redirected stdout
  (`Start-Process -Wait -RedirectStandardOutput` → `STDOUT_OK`), so it suppresses the console without
  losing the verdict text.
- **2026-11-14** — END TO END on the user's other live session (topic: animated stream cat, edits
  `C:\Users\AgentPC\stream-cat\js\cat.js`). Its real transcript fired the Stop hook from
  `cwd=C:\Users\AgentPC`; `resolve_project` returned `C:\Users\AgentPC\stream-cat`; a review of
  9400 chars started; **3/3 agents answered** (dsh 190.7 s, opencode 571.6 s) → `approve`,
  5 findings with evidence, 1 major: the comment claims the bib is shadow-free while the rendered
  band at design y 762–774 is still dark (145,140,136 vs 244,239,229).
- **2026-11-14** — `council watch` added: a detached JSONL timeline of diff/state/run/pending events,
  so "did the hook fire?" is answerable without waiting. 63 tests OK.
