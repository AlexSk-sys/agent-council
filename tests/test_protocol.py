"""Unit tests for the parts that can be wrong silently: parsing and merging.

No network, no model calls. Run: py -3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from council.adapters import AgentConfig, _materialize_prompt
from council.gitutil import is_noise
from council.cli import _collect_diff
from council.protocol import (
    Finding,
    PeerReview,
    extract_json_objects,
    merge_diff_review,
    merge_proposals,
    parse_diff_review,
    parse_peer_reviews,
)
from council.store import RunStore, latest_run


class TestExtractJson(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(extract_json_objects('{"a":1}'), [{"a": 1}])

    def test_fenced_and_prose(self):
        text = 'Here you go:\n```json\n{"a": 1}\n```\nHope that helps.'
        self.assertEqual(extract_json_objects(text), [{"a": 1}])

    def test_two_objects(self):
        text = '{"a":1}\n{"b":2}'
        self.assertEqual(extract_json_objects(text), [{"a": 1}, {"b": 2}])

    def test_trailing_comma(self):
        self.assertEqual(extract_json_objects('{"a":[1,2,],}'), [{"a": [1, 2]}])

    def test_brace_inside_string_is_not_a_boundary(self):
        self.assertEqual(extract_json_objects('{"a":"}"}'), [{"a": "}"}])

    def test_escaped_quote_inside_string(self):
        self.assertEqual(extract_json_objects(r'{"a":"x\"}"}'), [{"a": 'x"}'}])

    def test_nested(self):
        obj = {"a": {"b": {"c": 1}}, "d": 2}
        self.assertEqual(extract_json_objects(json.dumps(obj)), [obj])

    def test_garbage_returns_empty(self):
        self.assertEqual(extract_json_objects("no json here"), [])


class TestParsePeerReviews(unittest.TestCase):
    PEERS = {"claude", "dsh"}

    def test_valid(self):
        text = json.dumps({
            "peer": "dsh", "verdict": "amend",
            "must_fix": [{"issue": "x", "evidence": "f.py:3", "severity": "blocker"}],
            "notes": "n",
        })
        out = parse_peer_reviews("claude", text, self.PEERS)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].target, "dsh")
        self.assertEqual(out[0].verdict, "amend")
        self.assertTrue(out[0].must_fix[0].has_evidence)

    def test_self_review_rejected(self):
        text = json.dumps({"peer": "claude", "verdict": "approve"})
        self.assertEqual(parse_peer_reviews("claude", text, self.PEERS), [])

    def test_unknown_peer_rejected(self):
        text = json.dumps({"peer": "gpt", "verdict": "approve"})
        self.assertEqual(parse_peer_reviews("claude", text, self.PEERS), [])

    def test_bad_verdict_coerced(self):
        text = json.dumps({"peer": "dsh", "verdict": "maybe"})
        self.assertEqual(parse_peer_reviews("claude", text, self.PEERS)[0].verdict, "amend")

    def test_bad_severity_coerced(self):
        text = json.dumps({"peer": "dsh", "verdict": "amend",
                           "must_fix": [{"issue": "x", "evidence": "e", "severity": "catastrophic"}]})
        self.assertEqual(parse_peer_reviews("claude", text, self.PEERS)[0].must_fix[0].severity, "minor")

    def test_missing_issue_dropped(self):
        text = json.dumps({"peer": "dsh", "verdict": "amend",
                           "must_fix": [{"evidence": "e", "severity": "blocker"}]})
        self.assertEqual(parse_peer_reviews("claude", text, self.PEERS)[0].must_fix, [])


class TestParseDiffReview(unittest.TestCase):
    def test_valid(self):
        text = "```json\n" + json.dumps({
            "verdict": "block",
            "findings": [{"severity": "blocker", "file": "a.py", "line": 7,
                          "issue": "off by one", "evidence": "ran x, got 1 expected 2", "fix": "s/e/= f/"}],
        }) + "\n```"
        verdict, findings = parse_diff_review("dsh", text)
        self.assertEqual(verdict, "block")
        self.assertEqual(findings[0].file, "a.py")
        self.assertEqual(findings[0].line, 7)

    def test_unknowable_verdict_defaults_approve(self):
        verdict, _ = parse_diff_review("dsh", '{"verdict":"READY"}')
        self.assertEqual(verdict, "approve")

    def test_bad_line_number(self):
        verdict, findings = parse_diff_review("dsh", json.dumps({
            "verdict": "block",
            "findings": [{"severity": "minor", "issue": "x", "evidence": "e", "line": "n/a"}],
        }))
        self.assertEqual(findings[0].line, 0)

    def test_no_json(self):
        self.assertEqual(parse_diff_review("dsh", "I could not review this."), ("", []))


def _rev(reviewer, target, verdict, findings=None):
    return PeerReview(reviewer=reviewer, target=target, verdict=verdict,
                      must_fix=findings or [], notes="")


def _finding(severity="major", evidence="f.py:1", issue="x", source="a"):
    return Finding(severity=severity, issue=issue, evidence=evidence, source=source)


class TestMergeProposals(unittest.TestCase):
    def test_all_approved(self):
        proposals = {"claude": "A", "dsh": "B", "opencode": "C"}
        reviews = [
            _rev("claude", "dsh", "approve"), _rev("claude", "opencode", "approve"),
            _rev("dsh", "claude", "approve"), _rev("dsh", "opencode", "approve"),
            _rev("opencode", "claude", "approve"), _rev("opencode", "dsh", "approve"),
        ]
        decision, recommended, findings = merge_proposals(proposals, reviews)
        self.assertEqual(decision, "approve")
        self.assertIn(recommended, proposals)
        self.assertEqual(findings, [])

    def test_reject_removes_candidate(self):
        proposals = {"claude": "A", "dsh": "B"}
        reviews = [_rev("claude", "dsh", "reject"), _rev("dsh", "claude", "approve")]
        decision, recommended, _ = merge_proposals(proposals, reviews)
        self.assertEqual(decision, "approve")
        self.assertEqual(recommended, "claude")

    def test_all_rejected_blocks(self):
        proposals = {"claude": "A", "dsh": "B"}
        reviews = [_rev("claude", "dsh", "reject"), _rev("dsh", "claude", "reject")]
        self.assertEqual(merge_proposals(proposals, reviews)[0], "block")

    def test_evidenced_blocker_blocks(self):
        proposals = {"claude": "A", "dsh": "B"}
        reviews = [
            _rev("claude", "dsh", "amend", [_finding("blocker", "traceback in x.py:9", source="claude")]),
            _rev("dsh", "claude", "approve"),
        ]
        decision, _, findings = merge_proposals(proposals, reviews)
        self.assertEqual(decision, "approve")
        self.assertEqual(len(findings), 1)
        # dsh is blocked, claude survives
        _, recommended, _ = merge_proposals(proposals, reviews)
        self.assertEqual(recommended, "claude")

    def test_unevidenced_blocker_does_not_block(self):
        """A claim with no evidence must not be able to veto a plan."""
        proposals = {"claude": "A", "dsh": "B"}
        reviews = [
            _rev("claude", "dsh", "amend", [_finding("blocker", "   ", source="claude")]),
            _rev("dsh", "claude", "approve"),
        ]
        decision, _, findings = merge_proposals(proposals, reviews)
        self.assertEqual(decision, "approve")
        self.assertEqual(findings, [])

    def test_deterministic_tie_break(self):
        proposals = {"claude": "A", "dsh": "B", "opencode": "C"}
        reviews = [_rev("claude", "dsh", "approve"), _rev("dsh", "claude", "approve")]
        first = merge_proposals(proposals, reviews)[1]
        for _ in range(5):
            self.assertEqual(merge_proposals(proposals, reviews)[1], first)


class TestMergeDiff(unittest.TestCase):
    def test_evidenced_blocker_blocks_even_in_minority(self):
        findings = [_finding("blocker")]
        self.assertEqual(merge_diff_review({"claude": "approve", "dsh": "approve", "opencode": "block"}, findings), "block")

    def test_unevidenced_blocker_does_not_block(self):
        findings = [_finding("blocker", evidence="")]
        self.assertEqual(merge_diff_review({"claude": "approve", "dsh": "approve", "opencode": "block"}, findings), "approve")

    def test_majority_block_blocks(self):
        self.assertEqual(merge_diff_review({"claude": "block", "dsh": "block", "opencode": "approve"}, []), "block")

    def test_minority_block_passes(self):
        self.assertEqual(merge_diff_review({"claude": "block", "dsh": "approve", "opencode": "approve"}, []), "approve")


class TestLineFormat(unittest.TestCase):
    def test_verdict_and_findings(self):
        text = (
            "VERDICT: BLOCK\n"
            "FINDING: blocker | sandbox/rangeutil.py:7 | off by one | '1-5' -> [1,2,3,4]\n"
            "FINDING: minor | sandbox/a.py:3 | typo | read a.py\n"
        )
        verdict, findings = parse_diff_review("dsh", text)
        self.assertEqual(verdict, "block")
        self.assertEqual(len(findings), 2)
        self.assertEqual(findings[0].file, "sandbox/rangeutil.py")
        self.assertEqual(findings[0].line, 7)
        self.assertTrue(findings[0].has_evidence)

    def test_approve_without_findings(self):
        verdict, findings = parse_diff_review("dsh", "VERDICT: APPROVE")
        self.assertEqual((verdict, findings), ("approve", []))

    def test_verdict_inferred_from_blocker(self):
        verdict, _ = parse_diff_review("dsh", "FINDING: blocker | a.py:1 | x | y")
        self.assertEqual(verdict, "block")

    def test_evidence_defaults_to_location(self):
        _, findings = parse_diff_review("dsh", "FINDING: major | a.py:9 | x |")
        self.assertEqual(findings[0].evidence, "a.py:9")


class TestMarkdownFallback(unittest.TestCase):
    """A real dsh answer from the first E2E run: correct finding, wrong format."""

    SAMPLE = """## Review

**Verdict: Request changes**

### 1. Off-by-one: `parse_range` is not inclusive on the high end (blocker)

`sandbox/rangeutil.py:5` documents [1..5] but line 10 returns `range(lo, hi)`.

### 2. The test encodes the bug instead of catching it (major)

`sandbox/test_rangeutil.py:5` asserts the broken behaviour.
"""

    def test_findings_salvaged(self):
        verdict, findings = parse_diff_review("dsh", self.SAMPLE)
        self.assertEqual(verdict, "block")
        self.assertEqual(len(findings), 2)
        self.assertEqual(findings[0].severity, "blocker")
        self.assertEqual(findings[0].file, "sandbox/rangeutil.py")
        self.assertTrue(findings[0].has_evidence)

    def test_plain_prose_is_not_parsed(self):
        verdict, findings = parse_diff_review("dsh", "Looks fine to me.")
        self.assertEqual((verdict, findings), ("", []))


class TestPeerLineFormat(unittest.TestCase):
    def test_review_and_fix_lines(self):
        text = (
            "REVIEW: dsh | amend | mostly fine\n"
            "FIX: dsh | blocker | no rollback on error | read db.py:44\n"
            "REVIEW: opencode | approve | good\n"
        )
        reviews = parse_peer_reviews("claude", text, {"dsh", "opencode"})
        by_target = {r.target: r for r in reviews}
        self.assertEqual(by_target["dsh"].verdict, "amend")
        self.assertEqual(by_target["opencode"].verdict, "approve")
        self.assertEqual(len(by_target["dsh"].must_fix), 1)
        self.assertTrue(by_target["dsh"].must_fix[0].has_evidence)

    def test_self_and_unknown_filtered(self):
        text = "REVIEW: claude | approve | self\nREVIEW: gpt | approve | unknown\n"
        self.assertEqual(parse_peer_reviews("claude", text, {"claude", "dsh"}), [])


class TestPromptMaterialization(unittest.TestCase):
    """cmd.exe truncates argv at the first newline - verified on this machine."""

    def test_multiline_goes_to_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AgentConfig(name="dsh", prompt_dir=tmp)
            pointer = _materialize_prompt("line1\nline2", cfg)
            self.assertNotIn("\n", pointer)
            files = list(Path(tmp).glob("prompt-*.md"))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_text(encoding="utf-8"), "line1\nline2")
            self.assertIn(str(files[0]), pointer)

    def test_long_single_line_goes_to_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AgentConfig(name="dsh", prompt_dir=tmp)
            pointer = _materialize_prompt("x" * 5000, cfg)
            self.assertLess(len(pointer), 500)

    def test_short_single_line_stays_in_argv(self):
        cfg = AgentConfig(name="dsh", prompt_dir=None)
        self.assertEqual(_materialize_prompt("PONG", cfg), "PONG")


class TestNoiseExclusion(unittest.TestCase):
    """The council must never review its own logs: a one-line fix was once
    reported as a 334k-char change because .council/ and __pycache__ were in."""

    def test_is_noise(self):
        self.assertTrue(is_noise(".council/latest"))
        self.assertTrue(is_noise("sandbox/.council/runs/x/prompts/a.md"))
        self.assertTrue(is_noise("pkg/__pycache__/m.cpython-311.pyc"))
        self.assertFalse(is_noise("council/protocol.py"))

    def test_collect_diff_skips_run_dir_and_bytecode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "real.py").write_text("x = 1\n", encoding="utf-8")
            (root / ".council" / "runs" / "r1").mkdir(parents=True)
            (root / ".council" / "runs" / "r1" / "prompt.md").write_text("noise\n", encoding="utf-8")
            (root / "pkg" / "__pycache__").mkdir(parents=True)
            (root / "pkg" / "__pycache__" / "m.pyc").write_text("binary-ish\n", encoding="utf-8")
            diff, _ = _collect_diff(root, staged=False, ref=None, path=None)
            self.assertIn("real.py", diff)
            self.assertNotIn(".council", diff)
            self.assertNotIn("__pycache__", diff)


class TestStore(unittest.TestCase):
    def test_run_dir_and_latest_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore.for_task(Path(tmp), "Add a slugify helper")
            store.write("r1/claude.md", "hello")
            store.log({"event": "x"})
            self.assertTrue((store.dir / "r1" / "claude.md").exists())
            self.assertEqual(latest_run(Path(tmp)), store.dir)
            self.assertIn("slugify", store.run_id)


class TestDiffCollection(unittest.TestCase):
    def test_untracked_file_is_included(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "new.py").write_text("print('hi')\n", encoding="utf-8")
            diff, stat = _collect_diff(root, staged=False, ref=None, path=None)
            self.assertIn("new.py", diff)
            self.assertIn("print('hi')", diff)
            self.assertIn("new file", stat)

    def test_tracked_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "a.txt").write_text("one\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a",
                            "commit", "-qm", "init"], cwd=root, check=True)
            (root / "a.txt").write_text("two\n", encoding="utf-8")
            diff, _ = _collect_diff(root, staged=False, ref=None, path=None)
            self.assertIn("-one", diff)
            self.assertIn("+two", diff)


if __name__ == "__main__":
    unittest.main()
