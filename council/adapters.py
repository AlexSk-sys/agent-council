"""Headless adapters for the three agent harnesses.

Every harness is driven the same way: one prompt in, one text answer out,
in a chosen working directory, with a hard timeout.  Nothing here knows
about the council protocol; that lives in protocol.py.

Verified contracts (2026-11-14, this machine):
  claude   `claude -p --bare --output-format json`  -> stdout JSON, .result
  dsh      `dsh --profile headless <task>`          -> stdout text
  opencode `opencode run --standalone --format json` -> stdout JSONL, text parts

Notes that cost real time to learn:
  * `--bare` drops the skill/plugin listing: 1350 input tokens vs 32k,
    $0.0055 vs $0.128 per call. Always use it for peer calls.
  * dsh refuses to run when TEMP is inside the workspace; it needs
    C:\\dsh-tmp (see dsh-setup/start-dsh-web.ps1).
  * On Windows the npm entry points are .cmd shims; a .ps1 cannot be
    executed by subprocess without a PowerShell wrapper.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .win import kwargs as _no_window

AGENTS = ("claude", "dsh", "opencode")

# One gateway (`opencode.ai/inference/go/anthropic`), three different models, so
# a slow queue on one model cannot stall the whole council. Verified live
# 2026-11-18 on this machine, each pinned explicitly below:
#   claude -> glm-5.3-flash        (2.0 s trivial call)
#   dsh    -> deepseek-v4.1-flash  (5.2 s, via a --patch overlay; the profile
#                                   default is glm-5.3-flash at effort max)
#   opencode -> qwen3.8-flash      (5.6 s)
# A project's .council/config.json `model` key overrides any of these.
DEFAULT_MODELS = {
    "claude": "glm-5.3-flash",
    "dsh": "deepseek-v4.1-flash",
    "opencode": "opencode-go/qwen3.8-flash",
}

# dsh names a provider and a model separately; the rest of the id is what the
# gateway expects after the "opencode-go/" prefix used by opencode.
DSH_PROVIDER = "opencode-go"

# dsh's Windows sandbox rejects a TEMP directory inside the workspace.
DSH_TEMP = "C:\\dsh-tmp"


@dataclass
class AgentResult:
    agent: str
    ok: bool
    text: str = ""
    error: str = ""
    elapsed_ms: int = 0
    cost_usd: float | None = None
    stderr: str = ""
    command: str = ""

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "ok": self.ok,
            "text": self.text,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
            "cost_usd": self.cost_usd,
            "stderr": self.stderr[-4000:],
            "command": self.command,
        }


@dataclass
class AgentConfig:
    """Per-agent knobs, overridable from .council/config.json.

    posture controls what the harness is allowed to do:
      read_only   read files and search; no shell, no writes
      shell       read + run commands (to verify claims); file edits still denied
      autonomous  read + run + edit; nothing prompts, nothing asks a human
    """

    name: str
    enabled: bool = True
    timeout_s: int = 420
    model: str | None = None
    extra_args: list[str] = field(default_factory=list)
    posture: str = "shell"
    prompt_dir: str | None = None
    # "low" | "high" | "max" where the harness supports it; None keeps the
    # harness default. Peers do a reading-and-judging task, not a proof, so a
    # lower effort is often 2x faster with the same verdict.
    effort: str | None = None


POSTURES = ("read_only", "shell", "autonomous")


def _materialize_prompt(prompt: str, cfg: AgentConfig) -> str:
    """Pass long or multi-line prompts by file, not by argv.

    Every harness is launched through an npm .cmd shim, and cmd.exe truncates an
    argument at its first newline. Verified on this machine: a two-line prompt to
    dsh arrived as its first line only, and all three harnesses reported "your
    message ended at the preamble" on the first real council run. A pointer line
    avoids the shim entirely and also dodges the ~8k command-line limit that a
    full diff would blow through.
    """
    if not cfg.prompt_dir or ("\n" not in prompt and len(prompt) < 1200):
        return prompt
    target = Path(cfg.prompt_dir) / f"prompt-{cfg.name}-{uuid.uuid4().hex[:8]}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(prompt, encoding="utf-8")
    return (
        f"Read the file {target} and carry out the instructions in it exactly. "
        "That file is the complete task; nothing was omitted. "
        "Your entire reply is parsed by a program, so follow its output format."
    )


def _exe(name: str) -> str:
    """Resolve an npm-installed CLI to something subprocess can execute."""
    found = shutil.which(name)
    if found:
        return found
    npm = Path(os.environ.get("APPDATA", "")) / "npm" / f"{name}.cmd"
    if npm.exists():
        return str(npm)
    raise FileNotFoundError(
        f"{name} not found on PATH or in {npm.parent}. Is it installed?"
    )


@dataclass
class _Finished:
    """The few attributes the runners read from a completed subprocess."""

    returncode: int
    stdout: str
    stderr: str


def _kill_tree(pid: int) -> None:
    """Kill a harness and everything it spawned.

    The harnesses are npm .cmd shims: cmd.exe starts node, which starts more
    node. `subprocess.run(timeout=)` kills only the shim, then blocks reading a
    pipe that the grandchildren still hold - a 300 s timeout was measured
    returning after 375 s, with idle node processes left behind on every lost
    voice. /T walks the whole tree so the timeout is a real deadline.
    """
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=20, **_no_window())
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(os.getpgid(pid), 9)
        except OSError:
            pass


def _run(args: list[str], cwd: str, timeout_s: int, env_extra: dict | None = None):
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    started = time.monotonic()
    spawn = dict(
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        env=env,
    )
    if os.name != "nt":
        # Its own process group: _kill_tree calls killpg(getpgid(pid)), and
        # without this the child shares the council's group, so one timed-out
        # voice SIGKILLs the whole run and its parent shell.
        spawn["start_new_session"] = True
    try:
        proc = subprocess.Popen(args, **_no_window(), **spawn)
    except FileNotFoundError as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        return None, elapsed, f"executable not found: {exc}"
    except OSError as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        return None, elapsed, f"os error: {exc}"
    try:
        out, err = proc.communicate(timeout=timeout_s)
        elapsed = int((time.monotonic() - started) * 1000)
        return _Finished(proc.returncode, out, err), elapsed, ""
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        # Drain what the child already wrote; do not wait for it beyond 10 s.
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        elapsed = int((time.monotonic() - started) * 1000)
        return None, elapsed, f"timeout after {timeout_s}s"


# --------------------------------------------------------------------------
# claude
# --------------------------------------------------------------------------


def run_claude(prompt: str, cwd: str, cfg: AgentConfig) -> AgentResult:
    args = [
        _exe("claude"),
        "-p",
        "--bare",
        "--output-format",
        "json",
        "--permission-prompts",
        "none",
    ]
    if cfg.posture == "autonomous":
        args += ["--permission-mode", "bypassPermissions"]
    elif cfg.posture == "shell":
        # Bash is pre-approved, Edit/Write are not: reviewers may prove a claim
        # by running a command but cannot silently change the code they review.
        args += ["--allowed-tools=Read,Grep,Glob,Bash"]
    else:
        args += ["--allowed-tools=Read,Grep,Glob"]
    if cfg.model:
        args += ["--model", cfg.model]
    if cfg.effort:
        args += ["--effort", cfg.effort]
    args += cfg.extra_args
    args.append(_materialize_prompt(prompt, cfg))

    proc, elapsed, err = _run(args, cwd, cfg.timeout_s)
    res = AgentResult(agent="claude", ok=False, elapsed_ms=elapsed,
                      command=" ".join(args[:-1]) + " <prompt>")
    if proc is None:
        res.error = err
        return res

    res.stderr = proc.stderr or ""
    if proc.returncode != 0:
        res.error = f"exit {proc.returncode}: {(proc.stderr or proc.stdout)[-500:]}"
        return res
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        res.error = f"unparseable stdout: {proc.stdout[:300]}"
        return res

    res.cost_usd = payload.get("total_cost_usd")
    if payload.get("is_error"):
        res.error = str(payload.get("result") or "is_error=true")[:500]
        return res
    res.text = payload.get("result") or ""
    res.ok = bool(res.text.strip())
    if not res.ok:
        res.error = "empty result"
    return res


# --------------------------------------------------------------------------
# dsh
# --------------------------------------------------------------------------


def _dsh_model_patch(cfg: AgentConfig) -> str | None:
    """Write a --patch overlay that pins dsh's model and reasoning effort.

    `dsh --profile headless` has no --model flag: the model lives in the
    profile's `agent-default-model` plugin config. A --patch overlay replaces
    that config object wholesale (it is not a deep merge), so `provider` must
    be written too or the plugin fails to activate with
    "ValidationError: invalid config: $.provider".
    """
    if not cfg.model:
        return None
    provider, _, model = cfg.model.rpartition("/")
    if not provider:
        provider, model = DSH_PROVIDER, cfg.model
    lines = [
        "- id: agent-default-model",
        "  config:",
        f"    provider: {provider}",
        f"    model: {model}",
    ]
    if cfg.effort:
        lines.append(f"    reasoningEffort: {cfg.effort}")
    base = Path(cfg.prompt_dir) if cfg.prompt_dir else Path(DSH_TEMP)
    base.mkdir(parents=True, exist_ok=True)
    target = base / f"dsh-model-{cfg.name}-{uuid.uuid4().hex[:8]}.yml"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(target)


def run_dsh(prompt: str, cwd: str, cfg: AgentConfig) -> AgentResult:
    args = [
        _exe("dsh"),
        "--profile",
        "headless",
    ]
    # --patch is a launcher flag: the launcher stops parsing at the first app
    # option, so it must come before --permission-mode or dsh reports
    # "unknown option '--patch'" and exits 1.
    patch = _dsh_model_patch(cfg)
    if patch:
        args += ["--patch", patch]
    args += [
        "--permission-mode",
        "read-only" if cfg.posture == "read_only" else "workspace-write",
        "--approval-policy",
        "never",
    ]
    args += cfg.extra_args
    args.append(_materialize_prompt(prompt, cfg))

    proc, elapsed, err = _run(
        args, cwd, cfg.timeout_s, env_extra={"TEMP": DSH_TEMP, "TMP": DSH_TEMP}
    )
    res = AgentResult(agent="dsh", ok=False, elapsed_ms=elapsed,
                      command=" ".join(args[:-1]) + " <prompt>")
    if proc is None:
        res.error = err
        return res

    res.stderr = proc.stderr or ""
    if proc.returncode != 0:
        res.error = f"exit {proc.returncode}: {(proc.stderr or proc.stdout)[-500:]}"
        return res
    res.text = proc.stdout.strip()
    res.ok = bool(res.text)
    if not res.ok:
        res.error = "empty stdout"
    return res


# --------------------------------------------------------------------------
# opencode
# --------------------------------------------------------------------------


def run_opencode(prompt: str, cwd: str, cfg: AgentConfig) -> AgentResult:
    args = [
        _exe("opencode"),
        "run",
        "--standalone",
        "--format",
        "json",
    ]
    if cfg.posture != "read_only":
        args.append("--auto")
    if cfg.model:
        args += ["--model", cfg.model]
    args += cfg.extra_args
    args.append(_materialize_prompt(prompt, cfg))

    # `opencode run` has no effort flag, and a `#variant` suffix is rejected
    # by the gateway ("Variant unavailable for ..."). Effort on this harness is
    # a different model id (`qwen3.8-max`), chosen via overrides.<agent>.model.
    # Say so instead of silently dropping a configured knob.
    effort_note = ""
    if cfg.effort:
        effort_note = (f"council: effort {cfg.effort!r} was not passed to opencode; "
                       f"it has no effort flag - use overrides.opencode.model "
                       f"(e.g. opencode-go/qwen3.8-max) instead\n")

    proc, elapsed, err = _run(args, cwd, cfg.timeout_s)
    res = AgentResult(agent="opencode", ok=False, elapsed_ms=elapsed,
                      command=" ".join(args[:-1]) + " <prompt>")
    if proc is None:
        res.error = err
        res.stderr = effort_note
        return res

    res.stderr = effort_note + (proc.stderr or "")
    if proc.returncode != 0:
        # A failed run is not an opinion, even when it emitted a text event
        # before dying: a truncated stream used to become an authoritative
        # vote while the harness error was discarded.
        res.error = (f"exit {proc.returncode}: "
                     f"{(proc.stderr or proc.stdout)[-500:]}")
        return res

    chunks: list[str] = []
    cost = 0.0
    saw_event = False
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        saw_event = True
        etype = event.get("type")
        part = event.get("part") or {}
        if etype == "text" and part.get("type") == "text":
            chunks.append(part.get("text", ""))
        elif etype == "step-finish":
            cost += float(part.get("cost") or 0.0)

    if not saw_event:
        res.error = f"no JSON events on stdout: {proc.stdout[:300]}"
        return res
    if cost:
        res.cost_usd = round(cost, 6)
    res.text = "\n".join(c for c in chunks if c.strip()).strip()
    res.ok = bool(res.text)
    if not res.ok:
        res.error = "no text parts in output"
    return res


RUNNERS = {
    "claude": run_claude,
    "dsh": run_dsh,
    "opencode": run_opencode,
}


def run_agent(agent: str, prompt: str, cwd: str, cfg: AgentConfig | None = None) -> AgentResult:
    cfg = cfg or AgentConfig(name=agent)
    runner = RUNNERS.get(agent)
    if runner is None:
        return AgentResult(agent=agent, ok=False, error=f"unknown agent {agent!r}")
    try:
        return runner(prompt, cwd, cfg)
    except FileNotFoundError as exc:
        return AgentResult(agent=agent, ok=False, error=str(exc))
    except Exception as exc:  # never let one harness kill the council
        return AgentResult(agent=agent, ok=False, error=f"{type(exc).__name__}: {exc}")
