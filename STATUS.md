# STATUS — agent-council

## Now
Working CLI: `review`, `work`, `task`, `ask`, `status`, `doctor`, `init`. 44 unit tests green.
Both E2E paths verified on a seeded bug: detection (3/3) and repair (executor fixed it, tests pass).

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
