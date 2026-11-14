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

## Always on

Two hooks in `~/.claude/settings.json` make the review automatic in every project:

| event | command | what it does |
|---|---|---|
| `Stop` | `py -3 -m council hook --event stop` | if the working tree changed, starts a **detached background** review. Never blocks a turn, never fails one. Skips a diff that was already reviewed and enforces a 300 s cooldown. |
| `UserPromptSubmit` | `py -3 -m council hook --event prompt` | prints the last background verdict once, then clears it. |

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
