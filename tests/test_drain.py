"""drain: a finding is closed only when its own repro flips to zero.

No network: the executor is mocked. The repro commands really run, because
the whole point of drain is that the proof is executed, not asserted.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from council import drain
from council.adapters import AgentResult

REPRO_FAILS = 'py -3 -c "raise SystemExit(1)"'
REPRO_PASSES = 'py -3 -c "raise SystemExit(0)"'
REPRO_NEEDS_FILE = (
    'py -3 -c "import os,sys;sys.exit(0 if os.path.exists('
    "'fixed.txt') else 1)\""
)


def finding(repro, severity="blocker", file="a.py", line=1, issue="boom"):
    return {"severity": severity, "issue": issue, "evidence": "read a.py:1",
            "source": "dsh", "file": file, "line": line, "repro": repro,
            "status": "confirmed"}


def write_run(root: Path, findings: list[dict]) -> Path:
    run = root / ".council" / "runs" / "20260101-000000-test"
    run.mkdir(parents=True)
    event = {"event": "finalize", "decision": "block", "confirmed": findings}
    (run / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
    return run


class TestDrain(unittest.TestCase):
    def test_load_confirmed_dedupes_by_root_cause(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = write_run(root, [
                finding(REPRO_FAILS),
                finding(REPRO_FAILS, issue="same line, other words"),
                finding(REPRO_FAILS, severity="minor", file="b.py", line=2),
                finding(REPRO_FAILS, file="a.py", line=3),
            ])
            items = drain.load_confirmed(run)
            self.assertEqual(len(items), 2)
            self.assertEqual(items[0].file, "a.py")

    def test_dry_run_lists_but_calls_no_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = write_run(root, [finding(REPRO_FAILS)])
            with mock.patch("council.drain.run_agent") as executor:
                report = drain.run_drain(root, run, dry_run=True)
            executor.assert_not_called()
            self.assertEqual(report["open"], 1)
            self.assertEqual(report["fixed"], 0)
            self.assertEqual(report["items"][0]["state"], "would fix")

    def test_a_defect_whose_repro_passes_is_already_fixed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = write_run(root, [finding(REPRO_PASSES)])
            with mock.patch("council.drain.run_agent") as executor:
                report = drain.run_drain(root, run)
            executor.assert_not_called()
            self.assertEqual(report["already"], 1)
            self.assertEqual(report["fixed"], 0)

    def test_fix_is_accepted_only_when_the_repro_flips(self):
        def fake(agent, _prompt, cwd, _cfg):
            (Path(cwd) / "fixed.txt").write_text("done", encoding="utf-8")
            return AgentResult(agent=agent, ok=True, text="fixed the counter")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = write_run(root, [finding(REPRO_NEEDS_FILE)])
            with mock.patch("council.drain.run_agent", side_effect=fake):
                report = drain.run_drain(root, run)
            self.assertEqual(report["fixed"], 1)
            self.assertEqual(report["open"], 0)
            self.assertEqual(drain.exit_code(report), 0)
            self.assertTrue((run / "drained.md").exists())
            self.assertIn("fixed", report["items"][0]["state"])

    def test_an_executor_that_does_nothing_leaves_the_item_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = write_run(root, [finding(REPRO_NEEDS_FILE)])
            with mock.patch("council.drain.run_agent",
                            return_value=AgentResult(agent="claude", ok=True,
                                                     text="looks fine to me")):
                report = drain.run_drain(root, run)
            self.assertEqual(report["fixed"], 0)
            self.assertEqual(report["open"], 1)
            self.assertEqual(drain.exit_code(report), 2)
            self.assertIn("still open", report["items"][0]["state"])

    def test_a_finding_without_a_repro_is_skipped_not_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = write_run(root, [finding("")])
            report = drain.run_drain(root, run, dry_run=True)
            self.assertEqual(report["skipped"], 1)
            self.assertIn("no repro", report["items"][0]["state"])
            # An unprovable finding is not a closed one.
            self.assertEqual(drain.exit_code(report), 2)


if __name__ == "__main__":
    unittest.main()
