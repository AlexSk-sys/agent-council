# CLAUDE.md — agent-council

## What this is
A CLI that runs Claude Code, DeepSeek Harness (`dsh`) and OpenCode against the same repository so
they review, and in `work` mode fix, each other's output. Single package, stdlib only.

## Commands
```powershell
py -3 -m unittest discover -s tests -v      # verify (42 tests, no network)
py -3 -m pip install -e .                   # installs the `council` entry point
council doctor                              # prove all 3 harnesses answer headless
council review --path sandbox               # cross-review a diff
council --project sandbox work "<task>"     # plan -> execute -> verify -> fix
```

## Layout
- `council/adapters.py` — one runner per harness. **All CLI flags live here.** Changing a flag
  elsewhere is a bug.
- `council/protocol.py` — rounds, three-strategy parsers, deterministic merge, work loop.
- `council/prompts.py` — prompts. The output contract goes **last** in each prompt.
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

## Rules for changes
- A new harness flag goes in `adapters.py` and nowhere else.
- A new finding format needs a parser branch plus a unit test in `tests/test_protocol.py`.
- Never let an unproven finding block a change; `test_unevidenced_blocker_does_not_block` guards it.
