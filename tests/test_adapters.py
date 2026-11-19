"""Adapter contracts that were previously untested.

Each test here pins a defect the council itself confirmed:
- POSIX: the child must have its own process group, or `_kill_tree` SIGKILLs
  the council and its parent shell;
- opencode has no effort flag, so a configured effort must be reported, not
  silently dropped (a `#variant` suffix is rejected by the gateway);
- every runner must carry the configured model id.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from council import adapters
from council.adapters import AgentConfig, _Finished


class FakeProc:
    pid = 4242
    returncode = 0

    def communicate(self, timeout=None):
        return "ok", ""


class HangProc(FakeProc):
    """Raises TimeoutExpired once, then returns the partial output."""

    def __init__(self):
        self.calls = 0

    def communicate(self, timeout=None):
        self.calls += 1
        if self.calls == 1:
            raise adapters.subprocess.TimeoutExpired(cmd="x", timeout=5)
        return "partial", ""


class TestProcessGroup(unittest.TestCase):
    def _spawn_kwargs(self, os_name: str) -> dict:
        with mock.patch.object(adapters.os, "name", os_name), \
                mock.patch.object(adapters.subprocess, "Popen",
                                  return_value=FakeProc()) as popen:
            adapters._run(["x"], ".", 5)
        return popen.call_args.kwargs

    def test_posix_child_gets_its_own_process_group(self):
        self.assertTrue(self._spawn_kwargs("posix").get("start_new_session"))

    def test_windows_spawn_has_no_posix_session_flag(self):
        self.assertNotIn("start_new_session", self._spawn_kwargs("nt"))

    def test_timeout_kills_the_tree_and_reports_the_deadline(self):
        proc = HangProc()
        with mock.patch.object(adapters.subprocess, "Popen", return_value=proc), \
                mock.patch.object(adapters, "_kill_tree") as kill_tree:
            finished, _elapsed, err = adapters._run(["x"], ".", 5)
        self.assertIsNone(finished)
        self.assertIn("timeout after 5s", err)
        kill_tree.assert_called_once_with(4242)

    def test_kill_tree_targets_the_child_group_not_ours(self):
        # getpgid/killpg do not exist on Windows; create=True lets the POSIX
        # branch be tested from this machine.
        with mock.patch.object(adapters.os, "name", "posix"), \
                mock.patch.object(adapters.os, "getpgid", return_value=7777,
                                  create=True) as getpgid, \
                mock.patch.object(adapters.os, "killpg", create=True) as killpg:
            adapters._kill_tree(4242)
        getpgid.assert_called_once_with(4242)
        killpg.assert_called_once_with(7777, 9)


class TestModelForwarding(unittest.TestCase):
    def test_claude_carries_model_and_effort(self):
        cfg = AgentConfig(name="claude", model="glm-5.3-flash", effort="high")
        done = _Finished(0, '{"result": "PONG", "total_cost_usd": 0.0}', "")
        with mock.patch.object(adapters, "_run", return_value=(done, 5, "")) as run:
            res = adapters.run_claude("hi", ".", cfg)
        self.assertTrue(res.ok)
        args = run.call_args.args[0]
        self.assertIn("glm-5.3-flash", args)
        self.assertIn("--effort", args)

    def test_dsh_patch_carries_provider_model_and_effort(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AgentConfig(name="dsh", model="opencode-go/deepseek-v4.1-flash",
                              effort="max", prompt_dir=tmp)
            done = _Finished(0, "PONG", "")
            with mock.patch.object(adapters, "_run", return_value=(done, 5, "")) as run:
                res = adapters.run_dsh("hi", ".", cfg)
            args = run.call_args.args[0]
            patch_path = args[args.index("--patch") + 1]
            patch = Path(patch_path).read_text(encoding="utf-8")
        self.assertTrue(res.ok)
        self.assertIn("provider: opencode-go", patch)
        self.assertIn("model: deepseek-v4.1-flash", patch)
        self.assertIn("reasoningEffort: max", patch)

    def test_opencode_carries_the_model(self):
        cfg = AgentConfig(name="opencode", model="opencode-go/qwen3.8-flash")
        done = _Finished(0, '{"type":"text","part":{"type":"text","text":"PONG"}}\n', "")
        with mock.patch.object(adapters, "_run", return_value=(done, 5, "")) as run:
            res = adapters.run_opencode("hi", ".", cfg)
        self.assertTrue(res.ok)
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("--model") + 1], "opencode-go/qwen3.8-flash")

    def test_opencode_nonzero_exit_is_not_a_verdict(self):
        """A truncated stream must not count as a vote."""
        cfg = AgentConfig(name="opencode", model="opencode-go/qwen3.8-flash")
        done = _Finished(3, '{"type":"text","part":{"type":"text","text":"PONG"}}\n',
                         "provider reset")
        with mock.patch.object(adapters, "_run", return_value=(done, 5, "")):
            res = adapters.run_opencode("hi", ".", cfg)
        self.assertFalse(res.ok)
        self.assertEqual(res.text, "")
        self.assertIn("exit 3", res.error)
        self.assertIn("provider reset", res.error)

    def test_opencode_reports_an_effort_it_cannot_use(self):
        cfg = AgentConfig(name="opencode", model="opencode-go/qwen3.8-flash",
                          effort="high")
        done = _Finished(0, '{"type":"text","part":{"type":"text","text":"PONG"}}\n', "")
        with mock.patch.object(adapters, "_run", return_value=(done, 5, "")) as run:
            res = adapters.run_opencode("hi", ".", cfg)
        self.assertTrue(res.ok)
        self.assertIn("was not passed to opencode", res.stderr)
        self.assertIn("qwen3.8-max", res.stderr)
        self.assertNotIn("#high", " ".join(run.call_args.args[0]))


if __name__ == "__main__":
    unittest.main()
