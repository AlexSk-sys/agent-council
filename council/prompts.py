"""Prompt construction for the council protocol.

Design rules baked into every prompt:
  * output is machine-parsed, so the shape is stated exactly;
  * a finding without evidence (file:line or command output) is invalid;
  * reviewers are told to attack, not to agree - three models on the same
    route agree by default, and polite agreement is worthless as control;
  * never ask an agent to "be thorough" - ask for specific checks.
"""

from __future__ import annotations

import json

HEADER = """You are one of three independent AI agents reviewing work on a software project.
You are not talking to a human. Your reply is parsed by a program, so follow the
output format exactly and put nothing before or after it.

Who you are: {agent}
Project directory: {project}
Capabilities: {actions}
"""

READ_ONLY_ACTIONS = (
    "you can read files and search them; you CANNOT run shell commands, so evidence "
    "must be a file:line citation or a quoted fragment of the code itself"
)
SHELL_ACTIONS = (
    "you can read files, search them, and run shell commands to verify claims; "
    "evidence should be the command and its actual output"
)

PROPOSE = HEADER + """
TASK:
{task}

{context}
Work read-only: do not create, edit or delete files. You may read files and run
read-only commands.

Reply with exactly this markdown structure:

## INTERPRETATION
One sentence: what is actually being asked, including anything ambiguous you had
to decide.

## PLAN
Numbered steps. Each step names the exact file and the exact change. No step may
say "refactor" or "improve" without naming what changes.

## VERIFICATION
The exact shell command(s) that would prove the change works, and the expected
output. A command that cannot be run as written is not verification.
ACCEPT: <one line: a command that exits 0 only when this task is done. It may
fail before the change - that is expected. Use a command the council can run:
python, py, pytest, node, npm, go, cargo, dotnet, git>

## RISKS
What could break, and the symptom that would reveal it.

## CONFIDENCE
An integer 0-100, then one sentence naming the single biggest reason you might be
wrong.
"""

REVIEW_PROPOSALS = HEADER + """
TASK UNDER REVIEW:
{task}

Two other agents answered the task above. Your job is to break their plans, not to
praise them. Assume each plan has at least one real defect and find it.

{peers}

Reply with ONLY lines in this format, nothing else - no headings, no prose:

REVIEW: <peer name> | <approve|amend|reject> | <one-line note>
FIX: <peer name> | <blocker|major|minor> | <issue> | <evidence>

Rules:
- One REVIEW line per peer, then any FIX lines.
- "reject" only if the plan cannot achieve the task or would damage the repo.
- "amend" means correct but incomplete or risky.
- Every FIX needs evidence you actually obtained by reading a file or running a
  command. If you did not verify it, do not write it.
- At most 5 FIX lines per peer, most severe first.
- If a plan is genuinely correct, REVIEW ... approve with no FIX lines is a valid
  answer - do not invent defects.
"""

REVIEW_DIFF = HEADER + """
A change was just made in this repository and must be checked before it is
accepted. Your job is to find defects in the change below. You have the real
repository: read the surrounding code and verify every claim before you make
it. Use only the capabilities listed above.

YOUR LENS: {lens}
Your peers cover the other lenses, so go deep on yours instead of repeating
general remarks.

VERIFY COMMAND AND ITS REAL OUTPUT (run by the council before this round):
{verify}

CHANGED FILES IN FULL, THEIR CALLERS AND RECENT COMMITS:
{impact}

PROJECT DOCS (claims that must match the code):
{docs}

KNOWN FAILURE CLASSES FROM EARLIER CONFIRMED FINDINGS - check these first:
{lessons}

THE CHANGE:
{diff}

HOW MUCH TO LOOK AT:
- You have a budget of at most 8 tool calls and your whole answer must arrive in time.
- Grep for the symbol instead of reading a large file; never page through a file
  longer than 300 lines. dsh shows ~30 lines per read, so a 4000-line file is
  150 calls and no answer at all - measured: 300 s timeout without this budget,
  64 s and a real finding with it.
- An unfinished review is worthless: when the budget is spent, answer with the
  verdict you can support from what you read.

Check specifically:
1. Does the code do what the change claims, for inputs the author probably did not
   try (empty, None, zero, negative, very large, unicode, concurrent)?
2. Does it break existing callers, tests, or the public interface?
3. Are there error paths that swallow failures, or resources (files, processes)
   left open?
4. Is anything asserted without proof - a comment, docstring or report claiming
   behaviour that the code does not implement?
5. Test gap: for each changed function, name the test that fails if the change is
   reverted. If no such test exists, file a major finding.
6. Docs: if the change makes a claim in README/STATUS/CLAUDE false, file a finding.

Reply with ONLY lines in this format, nothing else - no headings, no prose, no
code fences:

VERDICT: <APPROVE|BLOCK>
FINDING: <blocker|major|minor> | <file>:<line> | <one-line issue> | <evidence>
REPRO: <one-line command, optional, for the FINDING above>

Rules:
- One FINDING line per defect, most severe first, at most 8.
- BLOCK if any blocker exists, otherwise APPROVE.
- Evidence must be what you ran or read (command output, or the exact code).
  A finding with no evidence is invalid - leave it out.
- REPRO must be a command that exits NON-ZERO while the defect is present and
  zero when it is fixed (an assert, a failing test, a one-liner). The council
  runs it in a disposable snapshot; exit 0 refutes the finding.
- REPRO may only start with: node, python, py, pytest, npm, npx, dotnet, go,
  cargo, git. It runs in the repository root.
- Do not restate the diff. Do not praise.
"""

ASK = HEADER + """
Answer this question about the project. Be concrete: name files, lines and
commands. If you do not know, say what you would read to find out.

QUESTION:
{question}
"""

CONSULT = HEADER + """
You are joining an active coding session. The user has just sent the message
below and the main agent is about to act on it. Your job is to catch what that
agent is about to get wrong, before it does.

YOUR LENS: {lens}
Your peers cover the other lenses.

USER'S LATEST MESSAGE:
{message}

RECENT SESSION (transcript tail, oldest first):
{transcript}

REPOSITORY STATE:
{git_state}

VERIFY COMMAND AND ITS REAL OUTPUT (run by the council before this round):
{verify}

CHANGED FILES IN FULL, THEIR CALLERS AND RECENT COMMITS:
{impact}

PROJECT DOCS (claims that must match the code):
{docs}

KNOWN FAILURE CLASSES FROM EARLIER CONFIRMED FINDINGS - check these first:
{lessons}

Uncommitted change under discussion:
{diff}

HOW MUCH TO LOOK AT:
- You have a budget of at most 8 tool calls and your whole answer must arrive in time.
- Grep for the symbol instead of reading a large file; never page through a file
  longer than 300 lines. dsh shows ~30 lines per read, so a 4000-line file is
  150 calls and no answer at all - measured: 300 s timeout without this budget,
  64 s and a real finding with it.
- An unfinished review is worthless: when the budget is spent, answer with the
  verdict you can support from what you read.

Check specifically:
1. What does the user actually want, and which constraint is easy to miss?
2. What would break in the files involved? Read them before claiming anything.
3. Is the session about to assert something it has not verified?
4. If there is an uncommitted diff, does it contain a defect?
5. Test gap: for each changed function, name the test that fails if the change is
   reverted. If no such test exists, file a major finding.
6. Docs: if the change makes a claim in README/STATUS/CLAUDE false, file a finding.

Reply with ONLY lines in this format, nothing else - no headings, no prose, no
code fences:

VERDICT: <APPROVE|BLOCK>
FINDING: <blocker|major|minor> | <file>:<line> | <one-line issue> | <evidence>
REPRO: <one-line command, optional, for the FINDING above>

Rules:
- One FINDING line per defect, most severe first, at most {max_findings}.
- BLOCK only if a blocker exists, otherwise APPROVE.
- Evidence must be a file:line you read or code you quoted. A finding with no
  evidence is invalid - leave it out.
- REPRO must exit NON-ZERO while the defect is present and zero when fixed; the
  council runs it in a disposable snapshot and exit 0 refutes the finding.
- REPRO may only start with: node, python, py, pytest, npm, npx, dotnet, go,
  cargo, git. It runs in the repository root.
- If you have nothing to add, reply with the single line VERDICT: APPROVE.
"""

VERIFY_FINDINGS = HEADER + """
A first review round produced the findings below. Your job is to REFUTE them,
not to agree. For each numbered finding, read the code; run its REPRO when one
is present and your capabilities allow it - the council executes every REPRO
itself afterwards. CONFIRM only if the defect is real and would cause the
stated harm; REFUTE if it is wrong, already handled, or cannot happen.

FINDINGS:
{findings}

{context}

HOW MUCH TO LOOK AT:
- You have a budget of at most 6 tool calls and your whole answer must arrive in time.
- Grep for the symbol instead of reading a large file; never page through a file
  longer than 300 lines. dsh shows ~30 lines per read, so a 4000-line file is
  150 calls and no answer at all.
- An unfinished cross-check is worthless: when the budget is spent, answer with
  the verdict you can support from what you read.

Reply with ONLY lines in this format, nothing else:

CHECK: <number> | <CONFIRM|REFUTE> | <evidence: command output or file:line>
NEW: <blocker|major|minor> | <file>:<line> | <one-line issue> | <evidence>
REPRO: <one-line command for the NEW line above, optional>

Rules:
- One CHECK line per finding. Evidence is required; a CHECK without evidence
  is ignored.
- NEW lines are for defects the first round missed. Keep them evidenced.
- Do not repeat a finding that is already listed.
"""

EXECUTE = HEADER + """
You are the EXECUTOR. Three agents planned the task and the council chose the plan
below. Your job is to implement it in the repository now. You may read files, run
commands, and edit files. Do not run `git commit`, `git push`, or rewrite history:
leaving the change in the working tree is the contract.

Guidance:
- Follow the approved plan, but if you find it is wrong, do the correct thing and
  say so under NOT DONE or CHANGED.
- Keep the change minimal. Match the surrounding code's style and naming.
- Run the plan's verification command yourself and paste the real output. If it
  fails, fix it before answering; if you cannot, say exactly what failed.
- You may spawn subagents inside your own harness if that helps.

TASK:
{task}

APPROVED PLAN (from {author}):
{plan}

Reply exactly:

## CHANGED
- `path`: what changed and why

## VERIFICATION
`command`
<actual output>

## NOT DONE
anything skipped, and why (or "nothing")
"""

FIX = HEADER + """
You are the EXECUTOR and your change was rejected by peers. Fix it now in the
repository. You may read, run commands, and edit files. Do not commit.

PROVEN FINDINGS (each has evidence peers obtained themselves):
{findings}

Rules:
- Fix every blocker and major finding.
- If you believe a finding is wrong, do not argue at length: leave the code as it
  is and explain under NOT DONE with the specific file:line that proves it.
- Re-run the verification command and paste the real output.

Reply exactly:

## CHANGED
- `path`: what changed and why

## VERIFICATION
`command`
<actual output>

## NOT DONE
what you refused to change and the evidence, or "nothing"
"""


def execute_prompt(agent: str, project: str, task: str, plan: str, author: str,
                   allow_shell: bool = True) -> str:
    return EXECUTE.format(
        task=task, plan=plan.strip(), author=author,
        **_header(agent, project, allow_shell),
    )


def fix_prompt(agent: str, project: str, findings: list[str],
               allow_shell: bool = True) -> str:
    rendered = "\n".join(f"- {f}" for f in findings) or "- (none)"
    return FIX.format(
        findings=rendered, **_header(agent, project, allow_shell)
    )


def _header(agent: str, project: str, allow_shell: bool) -> dict:
    return {
        "agent": agent,
        "project": project,
        "actions": SHELL_ACTIONS if allow_shell else READ_ONLY_ACTIONS,
    }


def _context_block(context: str | None) -> str:
    if not context:
        return ""
    return f"PROJECT CONTEXT (provided by the requester):\n{context}\n"


def propose_prompt(agent: str, task: str, project: str, context: str | None = None,
                   allow_shell: bool = False) -> str:
    return PROPOSE.format(
        task=task, context=_context_block(context), **_header(agent, project, allow_shell)
    )


def review_proposals_prompt(agent: str, task: str, project: str, peers: dict[str, str],
                            allow_shell: bool = False) -> str:
    blocks = []
    for name, text in sorted(peers.items()):
        blocks.append(f"--- PEER {name} ---\n{text.strip()}\n--- END PEER {name} ---")
    return REVIEW_PROPOSALS.format(
        task=task, peers="\n\n".join(blocks), **_header(agent, project, allow_shell)
    )


def review_diff_prompt(agent: str, project: str, diff: str, allow_shell: bool = False,
                       max_chars: int = 60000, lens: str | None = None,
                       verify: str = "", impact: str = "", docs: str = "",
                       lessons: str = "") -> str:
    trimmed = diff if len(diff) <= max_chars else diff[:max_chars] + "\n... [diff truncated]"
    return REVIEW_DIFF.format(
        diff=trimmed, lens=lens or lens_for(agent), **_context_blocks(verify, impact, docs, lessons),
        **_header(agent, project, allow_shell),
    )


def ask_prompt(agent: str, project: str, question: str, allow_shell: bool = False) -> str:
    return ASK.format(
        question=question, **_header(agent, project, allow_shell)
    )


def consult_prompt(agent: str, project: str, message: str, transcript: str,
                   git_state: str, diff: str, allow_shell: bool = False,
                   max_chars: int = 60000, lens: str | None = None,
                   verify: str = "", impact: str = "", docs: str = "",
                   lessons: str = "", max_findings: int = 6) -> str:
    trimmed = diff if len(diff) <= max_chars else diff[:max_chars] + "\n... [diff truncated]"
    return CONSULT.format(
        message=message.strip()[:8000],
        transcript=transcript.strip() or "(no recent turns)",
        git_state=git_state or "(unknown)",
        diff=trimmed.strip() or "(no uncommitted change)",
        lens=lens or lens_for(agent),
        max_findings=max_findings,
        **_context_blocks(verify, impact, docs, lessons),
        **_header(agent, project, allow_shell),
    )


def verify_findings_prompt(agent: str, project: str, findings_text: str,
                           allow_shell: bool = False, context: str = "") -> str:
    return VERIFY_FINDINGS.format(
        findings=findings_text.strip() or "(no findings)",
        context=context.strip(),
        **_header(agent, project, allow_shell),
    )


LENSES = {
    "claude": ("CORRECTNESS - invalid inputs, edge cases, error paths, resource "
               "handling and shared mutable state (default arguments, aliasing), "
               "and whether the code does what it claims."),
    "dsh": ("REGRESSION & CONTRACTS - callers, public interfaces, data formats, "
            "concurrency, and what breaks outside the changed lines."),
    "opencode": ("TESTS & DOCS - the test that would fail if the change were "
                 "reverted, coverage gaps, and README/STATUS/CLAUDE claims that "
                 "are now false."),
}


def lens_for(agent: str) -> str:
    return LENSES.get(agent, LENSES["claude"])


def _context_blocks(verify: str, impact: str, docs: str, lessons: str) -> dict:
    return {
        "verify": verify or "(no verify command detected)",
        "impact": impact or "(no changed files to expand)",
        "docs": docs or "(no project docs)",
        "lessons": lessons or "(none recorded yet)",
    }


def render_verify(results: list[dict]) -> str:
    if not results:
        return "(no verify command detected for this project)"
    blocks = []
    for res in results:
        code = res.get("code")
        status = "PASS" if code == 0 else f"FAIL (exit {code})"
        blocks.append(f"$ {res.get('command', '')}\n{status}\n{res.get('output', '')[-3000:]}")
    return "\n\n".join(blocks)


def json_contract_example() -> str:
    """Used by tests and docs to keep the parsers and prompts in sync."""
    return json.dumps(
        {
            "peer": "dsh",
            "verdict": "amend",
            "must_fix": [
                {
                    "issue": "string",
                    "evidence": "string",
                    "severity": "blocker",
                }
            ],
            "notes": "string",
        }
    )
