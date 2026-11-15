# agent-council

Three agent harnesses — **Claude Code**, **DeepSeek Harness (`dsh`)**, **OpenCode** — working on
the same repository, checking each other, and fixing what the others prove is broken.

The point is not "three chatbots chat". The point is that one agent's confident claim is worthless
as verification, and three independent attempts at *breaking* that claim are not. Every finding the
council accepts must carry evidence: a `file:line`, a quoted fragment, or real command output. A
finding without evidence is discarded with its author named.

## What it does

| command | what happens | exit code |
|---|---|---|
| `council review [--staged\|--diff REF\|--path P]` | three agents attack the actual diff in parallel; proven blockers stop the change | 0 approve, 2 block |
| `council work "<task>"` | three plans → cross-review → one executor writes → the other two verify → executor fixes → re-verify | 0 done, 2 findings remain, 1 error |
| `council task "<task>"` | plan only: three proposals, cross-reviewed, deterministic vote | 0, 2, 1 |
| `council ask --to dsh "<question>"` | one peer question | 0/1 |
| `council consult --prompt-file F` | three agents join a live session: the user's message, the transcript tail and the uncommitted diff | 0 approve, 2 block |
| `council status` | last run in this project | |
| `council doctor` | prove all three harnesses answer headless | |
| `council init [--hook]` | `.council/` config, gitignore, optional pre-commit gate | |

## Install

```powershell
py -3 -m pip install -e C:\Users\AgentPC\source\repos\agent-council
council init --hook          # in a project
```

No third-party dependencies. Requires `claude`, `dsh`, `opencode` on PATH.

## How the councils are decided

Merging is **deterministic code**, not another model call. A model asked to summarise three
opinions invents agreement that is not there.

* `work`: a plan survives if no peer rejected it and no *evidenced* blocker was filed against it.
* `review`: BLOCK if any agent proved a blocker, or if a majority voted block. An unproven blocker
  does not block — otherwise any agent could veto by asserting things it never checked.

## Review pipeline

`review` and `consult` share one pipeline. Before any model is called:

1. the project's verify command runs (configured with `"verify"`, otherwise detected from
   `package.json`, `pyproject.toml`, `go.mod`, `Cargo.toml` or `*.sln`); a non-zero exit becomes a
   **confirmed blocker**, with the real output attached;
2. the changed files are expanded with their callers and recent commits;
3. `README.md` / `STATUS.md` / `CLAUDE.md` claims and `.council/checklist.md` are collected;
4. the working tree is copied into a disposable git worktree, and the agents run **there**.

Round 1 sends three **lenses** — correctness (claude), regression & contracts (dsh), tests & docs
(opencode). Each agent may attach `REPRO: <one-liner>`; the council executes every repro in the
snapshot: non-zero exit **confirms**, zero **refutes**.

Round 2 is cross-examination: each agent is shown the others' findings and must
`CHECK: n | CONFIRM|REFUTE | evidence`, and may add `NEW:` findings (repro-verified too).

Exit is hard: any **confirmed** blocker/major blocks; otherwise a majority of block votes blocks;
otherwise approve. Confirmed blocker/major classes are appended to `.council/checklist.md` and
injected into later prompts.

### Adaptive tiers

The automatic paths do not always pay for the full pipeline. `choose_tier` reads real signals
(diff size, changed files, security-sensitive paths, test gap, action verbs in the user message)
and picks:

| tier | when | what runs |
|---|---|---|
| **lean** | prompt-time, clean or tiny diff, conversational message | verify + round 1; no impact/docs/cross |
| **standard** | ordinary diff; Stop review | verify + impact + round 1; cross **only if a major is raised** |
| **full** | manual/pre-commit, large or security-sensitive diff, actionable change without tests | everything |

The chosen tier and its reason are recorded in `run.json` and `events.jsonl`. Every stage can still
be forced on or off in `.council/config.json`:

```json
{"pipeline": {"verify": true, "cross": "auto", "isolate": true, "lessons": true, "impact": "auto"}}
```

`cross`/`impact`/`docs` accept `true`, `false` or `"auto"` (default). An explicit boolean
overrides the tier. Every verdict records the commit and time it reviewed; if HEAD has moved
by the time it is shown, the pending line starts with `[STALE: ...]`. Force a tier for one run
with `council review --tier lean|standard|full`.

`council work` now runs the project verify command before planning (a failure stops it), passes
verify/impact/docs/lessons to its reviewers, executes their `REPRO` lines and records lessons.

## Benchmark

`benchmarks/run_benchmark.py` resets the fixture repo at
`C:\Users\AgentPC\source\repos\council-benchmark`, applies `seeded.patch` (eight known defects,
all tests still green), and runs one review per tier. It reports recall, false blockers, time
and cost to `benchmarks/results.json`.

## Always on

Two hooks in `~/.claude/settings.json` make the council automatic in every project:

| event | command | what it does |
|---|---|---|
| `UserPromptSubmit` | `pythonw.exe` + `args: [-m, council, hook, --event, prompt]` | on every user message: prints the previous verdict once, then starts a **detached three-agent `consult`** (message + transcript tail + uncommitted diff). If a run already holds the lock, the message is queued (latest wins) and the running child starts it on exit. |
| `Stop` | `pythonw.exe` + `args: [-m, council, hook, --event, stop]` | if the working tree changed, starts a **detached background** review. Skips a diff that was already reviewed and enforces a 300 s cooldown. |

A shared lock (`.council/running.json`, TTL 900 s, dead-pid recovery, O_EXCL create) allows one
council run per project at a time, so a consult and a diff review never put six models on the same
tree. A Stop review that arrives while a consult is running is **deferred, not dropped**
(`review_due`), and the next free Stop runs it. A consult is advisory and never writes the diff
gate's cache; only a real review does. The verdict lands in `.council/pending.md` and is printed
into the next user turn; nothing waits for it.

Both hooks use the exec form (`command` + `args`), so no shell and no console window is created;
`pythonw.exe` still writes to a redirected stdout normally (verified). A console-subsystem `python`
launched by a GUI-less parent flashes a cmd window on every turn — the user rejected that outright.

Plus a hard gate where it matters: `council init --hook` installs a pre-commit that blocks a commit
on a proved blocker. It fails **open** if the review cannot run (exit 1) — a flaky API must not stop
all commits; set `COUNCIL_FAIL_CLOSED=1` to invert that.

Cost control is built in: a diff is reviewed once (hash match) and at most one review starts per
5 minutes per project. Raise the cooldown with `council hook --event stop --cooldown 900`.

### Sessions opened outside a repository

The hook resolves its project from the session's cwd. If that folder is not a git repository (the
home directory, a scratch folder), it reads the session transcript from the hook payload and uses
the repository of the files the session most recently wrote. Verified on a live session opened in
`C:\Users\AgentPC` that edits `C:\Users\AgentPC\stream-cat\js\cat.js`: resolution returns
`C:\Users\AgentPC\stream-cat`.

A project that is not under git cannot be reviewed at all — there is no baseline to diff against.
`council doctor` says exactly that in one line; `git init` plus one baseline commit is enough to
put it in the rotation.

## Postures

| posture | read | run commands | edit files |
|---|---|---|---|
| read_only | yes | no | no |
| shell (default) | yes | yes | no |
| autonomous | yes | yes | yes |

Planners and reviewers run `shell`: they can prove a claim by running it, but they cannot edit the
code they are reviewing. In `work`, exactly one agent is promoted to `autonomous` and is the only
writer — three writers on one working tree is a race, not a review.

## Output contract

Agents are asked for a line format, not JSON:

```
VERDICT: BLOCK
FINDING: blocker | path/file.py:12 | what is wrong | evidence
```

Three parsers are tried in order: strict lines → JSON → markdown headings (`### 1. Off-by-one (blocker)`).
The markdown fallback exists because on the first real run two agents answered in prose and both
had found a genuine blocker; throwing that away because the format slipped would be the tool
failing, not the agent.

## Known quirks on this machine

* **`cmd.exe` truncates an argument at its first newline.** All three harnesses are npm `.cmd`
  shims. Verified: a two-line prompt reached `dsh` as its first line only. Prompts are therefore
  written to `.council/runs/<id>/prompts/` and passed as a one-line pointer.
* **`claude --bare`** drops the skill/plugin listing: 1350 input tokens vs 32k, $0.005 vs $0.128
  per call. Peer calls use it.
* **`dsh` needs `TEMP` outside the workspace** (`C:\dsh-tmp`), or its Windows sandbox refuses to run.
* **`--allowed-tools` is variadic** in the Claude CLI; use `--allowed-tools=A,B`, or it eats the
  prompt that follows.

## Cost

Measured on the seeded-bug review (3 agents, 463-char diff): wall time 118 s,
Claude $0.12, dsh and OpenCode $0 against the OpenCode Go route. A `work` run is roughly
2–3× that. `--bare` is the single biggest lever.

## Layout

```
council/adapters.py   one function per harness; the only place that knows CLI flags
council/protocol.py   rounds, parsers, deterministic merge, work loop
council/prompts.py    every prompt, with the output contract at the end
council/gitutil.py    diff collection, including untracked files
council/store.py      .council/runs/<id>/ — the only shared memory between harnesses
council/cli.py        argparse entry point
tests/                42 unit tests, no network
sandbox/              gitignored fixture repo used for the E2E test
```
