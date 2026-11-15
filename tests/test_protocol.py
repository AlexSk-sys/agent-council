"""Unit tests for the parts that can be wrong silently: parsing and merging.

No network, no model calls. Run: py -3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from council import prompts
from council import project as project_mod
from council.adapters import AgentConfig, AgentResult, _materialize_prompt
from council.gitutil import git_summary, is_noise
from council.cli import _collect_diff, main
from council.hooks import (
    MAX_RUN_S, _print_pending, _spawn_consult, _spawn_review, acquire_lock,
    council_busy, drain_queue, fingerprint, load_state, prompt_hook,
    release_lock, resolve_project, save_state, stop_hook, transcript_digest,
    write_pending,
)
from council.protocol import (
    CouncilOutcome,
    Finding,
    PeerReview,
    _prepare_review,
    decide_review,
    extract_json_objects,
    merge_diff_review,
    merge_proposals,
    parse_checks,
    parse_diff_review,
    parse_peer_reviews,
    run_session_consult,
    run_work_loop,
)
from council.store import RunStore, latest_run
from council.watch import snapshot, stop_watch, watch


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


class TestHooks(unittest.TestCase):
    """Hooks must be free when nothing changed and must never break a turn."""

    def _repo_with_change(self, root: Path) -> str:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        (root / "a.py").write_text("x = 1\n", encoding="utf-8")
        diff, _ = _collect_diff(root, staged=False, ref=None, path=None)
        return fingerprint(diff)

    def test_fingerprint_stable(self):
        self.assertEqual(fingerprint("abc"), fingerprint("abc"))
        self.assertNotEqual(fingerprint("abc"), fingerprint("abd"))

    def test_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_state(root, {"worktree": {"fingerprint": "f", "decision": "approve"}})
            self.assertEqual(load_state(root)["worktree"]["decision"], "approve")

    def test_stop_hook_no_diff_is_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            with mock.patch("sys.stdin", io.StringIO("{}")):
                self.assertEqual(stop_hook(root), 0)
            self.assertEqual(load_state(root), {})

    def test_stop_hook_skips_already_reviewed_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fp = self._repo_with_change(root)
            save_state(root, {"worktree": {"fingerprint": fp}, "last_spawn": 0})
            with mock.patch("sys.stdin", io.StringIO("{}")), \
                 mock.patch("council.hooks._spawn_review") as spawn:
                stop_hook(root)
            spawn.assert_not_called()

    def test_stop_hook_respects_stop_hook_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._repo_with_change(root)
            with mock.patch("sys.stdin", io.StringIO('{"stop_hook_active": true}')), \
                 mock.patch("council.hooks._spawn_review") as spawn:
                stop_hook(root)
            spawn.assert_not_called()

    def test_stop_hook_spawns_on_new_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._repo_with_change(root)
            with mock.patch("sys.stdin", io.StringIO("{}")), \
                 mock.patch("council.hooks._spawn_review") as spawn:
                stop_hook(root)
            spawn.assert_called_once()

    def test_spawn_puts_project_before_subcommand(self):
        """`council review --project X` is rejected by argparse; order matters."""
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("council.hooks.subprocess.Popen") as popen:
            _spawn_review(Path(tmp))
        args = popen.call_args[0][0]
        self.assertLess(args.index("--project"), args.index("review"))
        self.assertIn("--background", args)

    def test_spawn_writes_a_log_instead_of_devnull(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("council.hooks.subprocess.Popen"):
            _spawn_review(Path(tmp))
            self.assertTrue((Path(tmp) / ".council" / "background.log").exists())

    def test_prompt_hook_prints_once_then_clears(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".council").mkdir()
            (root / ".council" / "pending.md").write_text("BLOCK: 1 blocker\n", encoding="utf-8")
            buffer = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO("{}")), mock.patch("sys.stdout", buffer):
                prompt_hook(root)
            self.assertIn("1 blocker", buffer.getvalue())
            self.assertFalse((root / ".council" / "pending.md").exists())

    def test_review_if_changed_short_circuits_without_model_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fp = self._repo_with_change(root)
            save_state(root, {"worktree": {"fingerprint": fp, "decision": "block",
                                           "run_dir": "seeded"}})
            with mock.patch("council.protocol.run_diff_review") as review:
                code = main(["--project", str(root), "review", "--if-changed"])
            review.assert_not_called()
            self.assertEqual(code, 2)


class TestProjectResolution(unittest.TestCase):
    """A session opened in the home directory still edits real repositories."""

    def _repo(self, root: Path) -> Path:
        repo = root / "proj"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
        return repo

    def _transcript(self, root: Path, edited: Path) -> Path:
        line = json.dumps({
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Edit", "input": {"file_path": str(edited)}}
            ]}
        })
        path = root / "t.jsonl"
        path.write_text(json.dumps({"type": "atis-latch"}) + "\n" + line + "\n", encoding="utf-8")
        return path

    def test_cwd_inside_repo_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repo(root)
            self.assertEqual(resolve_project({}, repo), repo.resolve())

    def test_resolves_from_edited_files_when_cwd_is_not_a_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repo(root)
            home = root / "home"
            home.mkdir()
            transcript = self._transcript(root, repo / "a.py")
            payload = {"transcript_path": str(transcript)}
            self.assertEqual(resolve_project(payload, home), repo.resolve())

    def test_returns_none_when_nothing_is_in_a_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            loose = root / "loose.py"
            loose.write_text("x\n", encoding="utf-8")
            payload = {"transcript_path": str(self._transcript(root, loose))}
            self.assertIsNone(resolve_project(payload, home))


class TestGitSummary(unittest.TestCase):
    """Silence from the Stop hook has two very different causes; name them."""

    def test_not_a_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn("not a git repository", git_summary(Path(tmp)))

    def test_clean_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            self.assertIn("clean tree", git_summary(root))

    def test_repo_with_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "a.py").write_text("x = 1\n", encoding="utf-8")
            self.assertIn("will review this", git_summary(root))


class TestWatch(unittest.TestCase):
    def test_snapshot_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "a.py").write_text("x = 1\n", encoding="utf-8")
            snap = snapshot(root)
            self.assertGreater(snap["diff_chars"], 0)
            self.assertTrue(snap["fingerprint"])
            self.assertEqual(snap["runs"], [])

    def test_watch_writes_start_and_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            log = watch(root, interval=0.05, duration=0.2)
            events = [json.loads(ln)["event"] for ln in log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[0], "start")
            self.assertEqual(events[-1], "stop")

    def test_stop_without_watcher_is_harmless(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(stop_watch(Path(tmp)), 0)


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


class TestTranscriptDigest(unittest.TestCase):
    def test_extracts_roles_text_and_tool_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            rows = [
                {"message": {"role": "user", "content": [
                    {"type": "text", "text": "fix the parser"}]}},
                {"message": {"role": "assistant", "content": [
                    {"type": "text", "text": "reading it"},
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py"}},
                ]}},
            ]
            path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
            digest = transcript_digest(path)
            self.assertIn("[user] fix the parser", digest)
            self.assertIn("[assistant] reading it [Edit a.py]", digest)

    def test_tool_results_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            row = {"message": {"role": "user", "content": [
                {"type": "tool_result", "content": "x" * 5000}]}}
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            self.assertEqual(transcript_digest(path), "")

    def test_caps_length_keeping_the_newest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            rows = [{"message": {"role": "user", "content": [
                {"type": "text", "text": f"turn {i} " + "x" * 100}]}} for i in range(60)]
            path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
            digest = transcript_digest(path, max_entries=10, max_chars=600)
            self.assertLessEqual(len(digest), 603)
            self.assertIn("turn 59", digest)


class TestConsultPrompt(unittest.TestCase):
    def test_contains_message_context_and_contract(self):
        text = prompts.consult_prompt(
            "dsh", "C:/p", "fix the parser", "digest line",
            "git repository, clean tree", "+++ b/a.py", False,
        )
        self.assertIn("fix the parser", text)
        self.assertIn("digest line", text)
        self.assertIn("VERDICT:", text)
        self.assertIn("FINDING:", text)

    def test_braces_in_the_user_message_are_literal(self):
        text = prompts.consult_prompt("dsh", "C:/p", "use {x} and {y}", "", "", "", False)
        self.assertIn("{x} and {y}", text)

    def test_empty_diff_is_rendered_as_empty(self):
        text = prompts.consult_prompt("dsh", "C:/p", "hi", "", "", "", False)
        self.assertIn("(no uncommitted change)", text)

    def test_lean_finding_cap(self):
        text = prompts.consult_prompt("dsh", "C:/p", "hi", "", "", "", max_findings=3)
        self.assertIn("at most 3", text)


class TestSessionConsult(unittest.TestCase):
    def _fake(self, texts: dict[str, str], checks: dict[str, str] | None = None):
        def fake(agent, prompt, *_args):
            if "REFUTE" in prompt:
                text = (checks or {}).get(agent, "VERDICT: APPROVE")
            else:
                text = texts.get(agent, "VERDICT: APPROVE")
            return AgentResult(agent=agent, ok=True, text=text)
        return fake

    def _force_cross(self, root: Path) -> None:
        (root / ".council").mkdir(exist_ok=True)
        (root / ".council" / "config.json").write_text(
            json.dumps({"pipeline": {"cross": True}}), encoding="utf-8")

    def test_all_three_answer_approve(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("council.protocol.run_agent", side_effect=self._fake({})):
                out = run_session_consult(Path(tmp), "hello", "digest", "", "", "clean")
            self.assertEqual(out.mode, "consult")
            self.assertEqual(out.decision, "approve")
            self.assertEqual(len(out.results), 3)
            self.assertTrue((out.run_dir / "synthesis.md").exists())
            self.assertTrue((out.run_dir / "session.md").exists())
            self.assertTrue((out.run_dir / "context.md").exists())

    def test_repro_exit_nonzero_confirms_and_blocks(self):
        texts = {"dsh": "VERDICT: BLOCK\n"
                        "FINDING: blocker | a.py:1 | boom | read a.py:1\n"
                        "REPRO: python -c \"raise SystemExit(1)\""}
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("council.protocol.run_agent", side_effect=self._fake(texts)):
                out = run_session_consult(Path(tmp), "hello")
            self.assertEqual(out.decision, "block")
            self.assertEqual(out.findings[0].status, "confirmed")

    def test_repro_exit_zero_refutes_and_approves(self):
        texts = {"dsh": "VERDICT: BLOCK\n"
                        "FINDING: blocker | a.py:1 | boom | read a.py:1\n"
                        "REPRO: python -c \"raise SystemExit(0)\""}
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("council.protocol.run_agent", side_effect=self._fake(texts)):
                out = run_session_consult(Path(tmp), "hello")
            self.assertEqual(out.findings[0].status, "refuted")
            self.assertEqual(out.decision, "approve")

    def test_peer_confirm_makes_a_finding_block(self):
        texts = {"dsh": "VERDICT: BLOCK\nFINDING: major | a.py:1 | boom | read a.py:1"}
        checks = {"claude": "CHECK: 0 | CONFIRM | read a.py:1"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._force_cross(root)
            with mock.patch("council.protocol.run_agent",
                            side_effect=self._fake(texts, checks)):
                out = run_session_consult(root, "hello")
            self.assertEqual(out.findings[0].status, "confirmed")
            self.assertEqual(out.decision, "block")

    def test_unconfirmed_finding_does_not_block(self):
        texts = {"dsh": "VERDICT: BLOCK\nFINDING: major | a.py:1 | boom | read a.py:1"}
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("council.protocol.run_agent", side_effect=self._fake(texts)):
                out = run_session_consult(Path(tmp), "hello")
            self.assertEqual(out.findings[0].status, "unverified")
            self.assertEqual(out.decision, "approve")

    def test_peer_confirm_beats_a_bad_zero_exit_repro(self):
        """A repro that exits 0 despite a real defect must not veto a peer's proof."""
        texts = {"dsh": "VERDICT: BLOCK\n"
                        "FINDING: major | a.py:1 | boom | read a.py:1\n"
                        "REPRO: py -3 -c \"raise SystemExit(0)\""}
        checks = {"claude": "CHECK: 0 | CONFIRM | read a.py:1, the term is absent"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._force_cross(root)
            with mock.patch("council.protocol.run_agent",
                            side_effect=self._fake(texts, checks)):
                out = run_session_consult(root, "hello")
            self.assertEqual(out.findings[0].status, "confirmed")
            self.assertEqual(out.decision, "block")

    def test_auto_cross_is_skipped_for_minor_only(self):
        calls: list[str] = []

        def fake(agent, _prompt, *_args):
            calls.append(agent)
            return AgentResult(agent=agent, ok=True,
                               text="VERDICT: APPROVE\n"
                                    "FINDING: minor | a.py:1 | typo | read a.py:1")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "big.py").write_text("x" * 2000, encoding="utf-8")
            with mock.patch("council.protocol.run_agent", side_effect=fake):
                run_session_consult(root, "hello", "digest", "x" * 2000)
            self.assertEqual(len(calls), 3)

    def test_auto_cross_escalates_on_a_major(self):
        calls: list[str] = []

        def fake(agent, prompt, *_args):
            calls.append(agent)
            if "REFUTE" in prompt:
                return AgentResult(agent=agent, ok=True, text="VERDICT: APPROVE")
            if agent == "dsh":
                return AgentResult(agent=agent, ok=True,
                                   text="VERDICT: BLOCK\n"
                                        "FINDING: major | a.py:1 | boom | read a.py:1")
            return AgentResult(agent=agent, ok=True, text="VERDICT: APPROVE")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "big.py").write_text("x" * 2000, encoding="utf-8")
            with mock.patch("council.protocol.run_agent", side_effect=fake):
                run_session_consult(root, "hello", "digest", "x" * 2000)
            self.assertEqual(len(calls), 5)  # 3 first round + claude and opencode cross

    def test_lean_keeps_the_newest_transcript_tail(self):
        seen: list[str] = []

        def fake(agent, prompt, *_args):
            seen.append(prompt)
            return AgentResult(agent=agent, ok=True, text="VERDICT: APPROVE")

        long = "OLD_START_MARK" + "x" * 7000 + "NEWEST_MARK"
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("council.protocol.run_agent", side_effect=fake):
                run_session_consult(Path(tmp), "hello", long)
        self.assertTrue(seen)
        self.assertTrue(all("NEWEST_MARK" in p for p in seen))
        self.assertFalse(any("OLD_START_MARK" in p for p in seen))

    def test_unevidenced_blocker_does_not_block(self):
        texts = {"dsh": "VERDICT: BLOCK\nFINDING: blocker | | boom |"}
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("council.protocol.run_agent", side_effect=self._fake(texts)):
                out = run_session_consult(Path(tmp), "hello")
            self.assertEqual(out.decision, "approve")

    def test_fewer_than_two_answers_is_error(self):
        def fake(agent, *_args):
            _ = _args
            if agent == "claude":
                return AgentResult(agent=agent, ok=True, text="VERDICT: APPROVE")
            return AgentResult(agent=agent, ok=False, error="down")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("council.protocol.run_agent", side_effect=fake):
                out = run_session_consult(Path(tmp), "hello")
            self.assertEqual(out.decision, "error")


class TestCouncilLock(unittest.TestCase):
    def test_busy_until_the_owner_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            acquire_lock(root, "consult", "token-a")
            self.assertTrue(council_busy(root))
            release_lock(root, "wrong-token")
            self.assertTrue(council_busy(root))
            release_lock(root, "token-a")
            self.assertFalse(council_busy(root))

    def test_expired_lock_is_ignored_and_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            acquire_lock(root, "consult", "old")
            path = root / ".council" / "running.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["started_at"] = time.time() - MAX_RUN_S - 10
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertFalse(council_busy(root))
            self.assertFalse(path.exists())

    def test_second_acquire_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertTrue(acquire_lock(root, "consult", "first"))
            self.assertFalse(acquire_lock(root, "review", "second"))
            lock = json.loads((root / ".council" / "running.json").read_text(encoding="utf-8"))
            self.assertEqual(lock["token"], "first")

    def test_runtime_files_are_gitignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            acquire_lock(root, "consult", "tok")
            gitignore = root / ".council" / ".gitignore"
            self.assertTrue(gitignore.exists())
            text = gitignore.read_text(encoding="utf-8")
            self.assertIn("*", text)
            self.assertIn("!config.json", text)


class TestPromptConsultHook(unittest.TestCase):
    """The user writes - all three agents join the turn."""

    def _payload(self, root: Path, message: str = "please fix it") -> str:
        return json.dumps({"prompt": message,
                           "transcript_path": str(root / "t.jsonl")})

    def test_spawns_consult_on_user_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("sys.stdin", io.StringIO(self._payload(root))), \
                 mock.patch("sys.stdout", io.StringIO()), \
                 mock.patch("council.hooks._spawn_consult", return_value=0) as spawn:
                prompt_hook(root)
            spawn.assert_called_once()
            prompt_file = spawn.call_args[0][1]
            self.assertEqual(prompt_file.read_text(encoding="utf-8"), "please fix it")
            self.assertTrue(council_busy(root))
            lock = json.loads((root / ".council" / "running.json").read_text(encoding="utf-8"))
            self.assertEqual(lock["kind"], "consult")

    def test_skips_while_another_council_run_is_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            acquire_lock(root, "review", "busy")
            with mock.patch("sys.stdin", io.StringIO(self._payload(root))), \
                 mock.patch("sys.stdout", io.StringIO()), \
                 mock.patch("council.hooks._spawn_consult") as spawn:
                prompt_hook(root)
            spawn.assert_not_called()

    def test_busy_hook_queues_the_message_instead_of_dropping_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            acquire_lock(root, "review", "busy")
            with mock.patch("sys.stdin", io.StringIO(self._payload(root))), \
                 mock.patch("sys.stdout", io.StringIO()), \
                 mock.patch("council.hooks._spawn_consult") as spawn:
                prompt_hook(root)
            spawn.assert_not_called()
            queued = load_state(root).get("consult_queued") or {}
            queued_file = Path(str(queued.get("prompt_file") or ""))
            self.assertTrue(queued_file.is_file())
            self.assertEqual(queued_file.read_text(encoding="utf-8"), "please fix it")

    def test_latest_queued_message_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            acquire_lock(root, "review", "busy")
            for text in ("first message", "second message"):
                with mock.patch("sys.stdin", io.StringIO(self._payload(root, text))), \
                     mock.patch("sys.stdout", io.StringIO()), \
                     mock.patch("council.hooks._spawn_consult"):
                    prompt_hook(root)
            inbox = root / ".council" / "inbox"
            files = list(inbox.glob("prompt-*.md"))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_text(encoding="utf-8"), "second message")

    def test_drain_starts_the_queued_consult(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            acquire_lock(root, "review", "busy")
            with mock.patch("sys.stdin", io.StringIO(self._payload(root))), \
                 mock.patch("sys.stdout", io.StringIO()), \
                 mock.patch("council.hooks._spawn_consult"):
                prompt_hook(root)
            release_lock(root, "busy")
            with mock.patch("council.hooks._spawn_consult", return_value=0) as spawn:
                self.assertTrue(drain_queue(root))
            spawn.assert_called_once()
            self.assertNotIn("consult_queued", load_state(root))
            self.assertTrue(council_busy(root))

    def test_skips_inside_the_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_state(root, {"consult_last_spawn": time.time()})
            with mock.patch("sys.stdin", io.StringIO(self._payload(root))), \
                 mock.patch("sys.stdout", io.StringIO()), \
                 mock.patch("council.hooks._spawn_consult") as spawn:
                prompt_hook(root)
            spawn.assert_not_called()

    def test_stale_lock_does_not_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            acquire_lock(root, "consult", "old")
            path = root / ".council" / "running.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["started_at"] = time.time() - MAX_RUN_S - 10
            path.write_text(json.dumps(data), encoding="utf-8")
            with mock.patch("sys.stdin", io.StringIO(self._payload(root))), \
                 mock.patch("sys.stdout", io.StringIO()), \
                 mock.patch("council.hooks._spawn_consult") as spawn:
                prompt_hook(root)
            spawn.assert_called_once()

    def test_without_a_prompt_it_only_surfaces_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("sys.stdin", io.StringIO("{}")), \
                 mock.patch("sys.stdout", io.StringIO()), \
                 mock.patch("council.hooks._spawn_consult") as spawn:
                prompt_hook(root)
            spawn.assert_not_called()

    def test_spawn_orders_project_before_subcommand(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("council.hooks.subprocess.Popen") as popen:
            prompt_file = Path(tmp) / "msg.md"
            prompt_file.write_text("hi", encoding="utf-8")
            _spawn_consult(Path(tmp), prompt_file, "t.jsonl", "tok")
        args = popen.call_args[0][0]
        self.assertLess(args.index("--project"), args.index("consult"))
        self.assertIn("--prompt-file", args)
        self.assertIn("--transcript", args)
        self.assertIn("--lock-token", args)

    def test_stop_hook_defers_review_while_consult_runs(self):
        """The blocker the council found: a busy lock must defer, not drop."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "a.py").write_text("x = 1\n", encoding="utf-8")
            acquire_lock(root, "consult", "busy")
            with mock.patch("sys.stdin", io.StringIO("{}")), \
                 mock.patch("council.hooks._spawn_review") as spawn:
                stop_hook(root)
            spawn.assert_not_called()
            due = load_state(root).get("review_due")
            self.assertTrue(due)

            release_lock(root, "busy")
            with mock.patch("sys.stdin", io.StringIO("{}")), \
                 mock.patch("council.hooks._spawn_review") as spawn2:
                stop_hook(root)
            spawn2.assert_called_once()

    def test_dead_pid_clears_the_lock_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            acquire_lock(root, "consult", "tok", pid=2_147_483_647)
            self.assertFalse(council_busy(root))
            self.assertFalse((root / ".council" / "running.json").exists())

    def test_pending_keeps_both_verdicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_pending(root, "block", [], root / "r1", mode="review")
            write_pending(root, "approve", [], root / "r2", mode="consult")
            pending = root / ".council" / "pending.md"
            text = pending.read_text(encoding="utf-8")
            self.assertIn("[background council review] BLOCK", text)
            self.assertIn("[background council consult] APPROVE", text)

            buffer = io.StringIO()
            with mock.patch("sys.stdout", buffer):
                _print_pending(root)
            self.assertIn("BLOCK", buffer.getvalue())
            self.assertIn("APPROVE", buffer.getvalue())
            self.assertFalse(pending.exists())

    def test_pending_survives_a_failed_print(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_pending(root, "block", [], root / "r1")
            with mock.patch("builtins.print", side_effect=AttributeError("no stdout")):
                with self.assertRaises(AttributeError):
                    _print_pending(root)
            self.assertTrue((root / ".council" / "pending.md").exists())

    def test_stop_hook_skips_while_consult_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "a.py").write_text("x = 1\n", encoding="utf-8")
            acquire_lock(root, "consult", "busy")
            with mock.patch("sys.stdin", io.StringIO("{}")), \
                 mock.patch("council.hooks._spawn_review") as spawn:
                stop_hook(root)
            spawn.assert_not_called()


class TestConsultCommand(unittest.TestCase):
    def test_background_writes_pending_and_releases_its_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt_file = root / "msg.md"
            prompt_file.write_text("hello", encoding="utf-8")
            outcome = CouncilOutcome("consult", "approve", [], [], [],
                                     root / "run", "", "APPROVE: 0 blocker(s)")
            acquire_lock(root, "consult", "tok")
            with mock.patch("council.cli.run_session_consult", return_value=outcome):
                code = main(["--project", str(root), "consult",
                             "--prompt-file", str(prompt_file),
                             "--background", "--lock-token", "tok"])
            self.assertEqual(code, 0)
            self.assertTrue((root / ".council" / "pending.md").exists())
            self.assertFalse((root / ".council" / "running.json").exists())
            state = load_state(root)
            self.assertEqual(state["consult"]["decision"], "approve")

    def test_consult_does_not_overwrite_the_diff_gate_cache(self):
        """A consult verdict is about the message, never about the diff gate."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "a.py").write_text("x = 1\n", encoding="utf-8")
            save_state(root, {"worktree": {"fingerprint": "real-review", "decision": "block"}})
            prompt_file = root / "msg.md"
            prompt_file.write_text("thanks", encoding="utf-8")
            outcome = CouncilOutcome("consult", "approve", [], [], [],
                                     root / "run", "", "APPROVE")
            with mock.patch("council.cli.run_session_consult", return_value=outcome):
                main(["--project", str(root), "consult",
                      "--prompt-file", str(prompt_file), "--background"])
            worktree = load_state(root).get("worktree") or {}
            self.assertEqual(worktree.get("fingerprint"), "real-review")
            self.assertEqual(worktree.get("decision"), "block")

    def test_failed_consult_leaves_the_diff_for_the_stop_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "a.py").write_text("x = 1\n", encoding="utf-8")
            prompt_file = root / "msg.md"
            prompt_file.write_text("hello", encoding="utf-8")
            outcome = CouncilOutcome("consult", "error", [], [], [],
                                     root / "run", "", "ERROR")
            with mock.patch("council.cli.run_session_consult", return_value=outcome):
                main(["--project", str(root), "consult",
                      "--prompt-file", str(prompt_file), "--background"])
            self.assertNotIn("worktree", load_state(root))

    def test_consult_forwards_max_chars(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt_file = root / "msg.md"
            prompt_file.write_text("hello", encoding="utf-8")
            outcome = CouncilOutcome("consult", "approve", [], [], [],
                                     root / "run", "", "APPROVE")
            with mock.patch("council.cli.run_session_consult",
                            return_value=outcome) as run:
                main(["--project", str(root), "consult",
                      "--prompt-file", str(prompt_file), "--max-chars", "1234"])
            self.assertEqual(run.call_args.kwargs["max_chars"], 1234)


class TestReproParsing(unittest.TestCase):
    def test_repro_attaches_to_the_preceding_finding(self):
        text = ("VERDICT: BLOCK\n"
                "FINDING: major | a.py:7 | boom | read a.py:7\n"
                "REPRO: node -e \"process.exit(1)\"\n"
                "FINDING: minor | b.py:1 | typo | read b.py:1\n")
        verdict, findings = parse_diff_review("dsh", text)
        self.assertEqual(verdict, "block")
        self.assertEqual(findings[0].repro, 'node -e "process.exit(1)"')
        self.assertEqual(findings[1].repro, "")

    def test_json_repro(self):
        text = json.dumps({"verdict": "block", "findings": [
            {"severity": "major", "issue": "x", "evidence": "e", "repro": "node -e 1"}]})
        _, findings = parse_diff_review("dsh", text)
        self.assertEqual(findings[0].repro, "node -e 1")


class TestParseChecks(unittest.TestCase):
    def test_checks_and_new_findings(self):
        text = ("CHECK: 0 | CONFIRM | ran x, saw y\n"
                "CHECK: 1 | REFUTE | handled at a.py:9\n"
                "NEW: major | c.py:3 | missed | read c.py:3\n"
                "REPRO: py -3 -c \"raise SystemExit(1)\"\n")
        checks, new = parse_checks("claude", text)
        self.assertEqual(checks[0], ("confirm", "ran x, saw y"))
        self.assertEqual(checks[1][0], "refute")
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0].repro, 'py -3 -c "raise SystemExit(1)"')


class TestDecideReview(unittest.TestCase):
    def test_confirmed_major_blocks(self):
        f = Finding("major", "x", "ev", "dsh", status="confirmed")
        self.assertEqual(decide_review({"claude": "approve"}, [f]), "block")

    def test_unverified_does_not_block(self):
        f = Finding("blocker", "x", "ev", "dsh", status="unverified")
        self.assertEqual(decide_review({"claude": "approve", "dsh": "block"}, [f]),
                         "approve")

    def test_majority_block_blocks(self):
        self.assertEqual(
            decide_review({"claude": "block", "dsh": "block", "opencode": "approve"}, []),
            "block")


class TestProjectContext(unittest.TestCase):
    def test_detect_verify_from_package_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text('{"scripts": {"test": "node --test"}}',
                                               encoding="utf-8")
            self.assertEqual(project_mod.detect_verify_commands(root), ["npm test"])

    def test_config_override_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".council").mkdir()
            (root / ".council" / "config.json").write_text(
                json.dumps({"verify": ["node --test"]}), encoding="utf-8")
            self.assertEqual(project_mod.detect_verify_commands(root), ["node --test"])

    def test_repro_allowlist(self):
        self.assertTrue(project_mod.repro_allowed('node -e "process.exit(1)"'))
        self.assertTrue(project_mod.repro_allowed("py -3 -c \"raise SystemExit(1)\""))
        self.assertFalse(project_mod.repro_allowed("rm -rf /"))
        self.assertFalse(project_mod.repro_allowed("curl https://example.com"))
        self.assertFalse(project_mod.repro_allowed(""))

    def test_verify_repros_confirms_refutes_and_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad = Finding("major", "x", "e", "dsh",
                          repro='py -3 -c "raise SystemExit(1)"')
            good = Finding("major", "y", "e", "dsh",
                           repro='py -3 -c "raise SystemExit(0)"')
            none = Finding("minor", "z", "e", "dsh")
            blocked = Finding("major", "w", "e", "dsh", repro="rm -rf /")
            project_mod.verify_repros(root, [bad, good, none, blocked])
            self.assertEqual(bad.status, "confirmed")
            self.assertEqual(good.status, "refuted")
            self.assertEqual(none.status, "unverified")
            self.assertEqual(blocked.status, "unverified")
            self.assertTrue(blocked.repro_output.startswith("blocked"))

    def test_lessons_are_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = Finding("major", "silent child death", "hooks.py:1", "claude",
                        status="confirmed")
            self.assertEqual(project_mod.append_lessons(root, [f], "run-1"), 1)
            self.assertEqual(project_mod.append_lessons(root, [f], "run-2"), 0)
            text = project_mod.load_lessons(root)
            self.assertEqual(text.count("silent child death"), 1)

    def test_lessons_dedupe_the_same_defect_from_another_peer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = Finding("major", "bonus has no test", "logic.js:41", "claude",
                            status="confirmed")
            second = Finding("major", "no test covers the bonus", "logic.js:41", "dsh",
                             status="confirmed")
            self.assertEqual(project_mod.append_lessons(root, [first], "run-1"), 1)
            self.assertEqual(project_mod.append_lessons(root, [second], "run-2"), 0)

    def test_pipeline_options_defaults_and_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            defaults = project_mod.pipeline_options(root)
            self.assertEqual(defaults["cross"], "auto")
            self.assertEqual(defaults["impact"], "auto")
            (root / ".council").mkdir()
            (root / ".council" / "config.json").write_text(
                json.dumps({"pipeline": {"cross": False}}), encoding="utf-8")
            opts = project_mod.pipeline_options(root)
            self.assertFalse(opts["cross"])
            self.assertTrue(opts["verify"])

    def _repo_with(self, root: Path, files: list[str]) -> None:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        for name in files:
            (root / name).write_text("x\n", encoding="utf-8")

    def test_choose_tier_manual_is_full(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(project_mod.choose_tier(Path(tmp), "", "", [], "manual")[0],
                             "full")

    def test_choose_tier_prompt_without_diff_is_lean(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(project_mod.choose_tier(Path(tmp), "", "hello", [], "prompt")[0],
                             "lean")

    def test_choose_tier_actionable_change_without_tests_is_full(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._repo_with(root, ["game.js"])
            tier, _ = project_mod.choose_tier(root, "x" * 100, "fix the score", [], "prompt")
            self.assertEqual(tier, "full")

    def test_choose_tier_actionable_change_with_a_test_is_standard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._repo_with(root, ["game.js", "logic.test.js"])
            tier, _ = project_mod.choose_tier(root, "x" * 100, "fix the score", [], "prompt")
            self.assertEqual(tier, "standard")

    def test_choose_tier_security_path_is_full(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._repo_with(root, ["auth.py"])
            tier, _ = project_mod.choose_tier(root, "x" * 100, "hello", [], "prompt")
            self.assertEqual(tier, "full")

    def test_choose_tier_verify_failure_stays_standard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._repo_with(root, ["game.js"])
            tier, _ = project_mod.choose_tier(root, "x" * 100, "fix it",
                                              [{"code": 1}], "prompt")
            self.assertEqual(tier, "standard")

    def test_choose_tier_untracked_only_is_standard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._repo_with(root, ["big.bin"])
            tier, reason = project_mod.choose_tier(root, "", "hello", [], "prompt")
            self.assertEqual(tier, "standard")
            self.assertIn("not representable", reason)

    def test_apply_tier_respects_forced_docs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".council").mkdir()
            (root / ".council" / "config.json").write_text(
                json.dumps({"pipeline": {"docs": True}}), encoding="utf-8")
            lean = project_mod.apply_tier(project_mod.pipeline_options(root), "lean")
            self.assertTrue(lean["docs"])

    def test_prepare_review_honours_a_forced_tier(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _prepare_review(root, project_mod.pipeline_options(root),
                                  "prompt", "", "hello", tier_override="full")
            self.assertEqual(ctx.tier, "full")
            self.assertIn("forced", ctx.tier_reason)
            self.assertTrue(ctx.options["cross"])

    def test_choose_tier_stop_is_standard_or_full(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._repo_with(root, ["game.js"])
            self.assertEqual(
                project_mod.choose_tier(root, "x" * 100, "", [], "stop")[0], "standard")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._repo_with(root, ["auth.py"])
            self.assertEqual(
                project_mod.choose_tier(root, "x" * 100, "", [], "stop")[0], "full")

    def test_apply_tier_lean_standard_full(self):
        with tempfile.TemporaryDirectory() as tmp:
            opts = project_mod.pipeline_options(Path(tmp))
            lean = project_mod.apply_tier(opts, "lean")
            self.assertFalse(lean["impact"])
            self.assertFalse(lean["cross"])
            standard = project_mod.apply_tier(opts, "standard")
            self.assertTrue(standard["impact"])
            self.assertEqual(standard["cross"], "auto")
            full = project_mod.apply_tier(opts, "full")
            self.assertTrue(full["cross"])
            self.assertTrue(full["docs"])

    def test_apply_tier_respects_forced_cross(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".council").mkdir()
            (root / ".council" / "config.json").write_text(
                json.dumps({"pipeline": {"cross": True}}), encoding="utf-8")
            lean = project_mod.apply_tier(project_mod.pipeline_options(root), "lean")
            self.assertTrue(lean["cross"])

    def test_lean_context_does_not_claim_there_are_no_docs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _prepare_review(root, project_mod.pipeline_options(root),
                                  "prompt", "", "hello")
            self.assertEqual(ctx.tier, "lean")
            self.assertIn("not preloaded", ctx.docs)
            self.assertNotEqual(ctx.docs, "(no project docs)")
            self.assertIn("not preloaded", ctx.impact)

    def test_standard_context_preloads_impact_but_not_docs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "big.py").write_text("x = 1\n" * 400, encoding="utf-8")
            ctx = _prepare_review(root, project_mod.pipeline_options(root),
                                  "prompt", "x" * 2000, "hello")
            self.assertEqual(ctx.tier, "standard")
            self.assertIn("big.py", ctx.impact)
            self.assertIn("not preloaded", ctx.docs)

    def test_snapshot_copies_the_working_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "a.py").write_text("one\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a",
                            "commit", "-qm", "init"], cwd=root, check=True)
            (root / "a.py").write_text("two\n", encoding="utf-8")
            (root / "new.py").write_text("new\n", encoding="utf-8")
            snap = project_mod.Snapshot(root)
            workdir = snap.prepare()
            try:
                self.assertNotEqual(workdir, root.resolve())
                self.assertEqual((workdir / "a.py").read_text(encoding="utf-8"), "two\n")
                self.assertEqual((workdir / "new.py").read_text(encoding="utf-8"), "new\n")
            finally:
                snap.cleanup()
            self.assertFalse(workdir.exists())


class TestWorkPipeline(unittest.TestCase):
    def test_work_aborts_before_models_when_verify_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("council.project.run_verify",
                            return_value=[{"command": "false", "code": 1,
                                           "output": "boom"}]), \
                 mock.patch("council.protocol.run_agent") as agent:
                out = run_work_loop(root, "do it")
            agent.assert_not_called()
            self.assertEqual(out.decision, "block")
            self.assertEqual(out.findings[0].source, "verify")
            self.assertEqual(out.findings[0].status, "confirmed")


class TestPendingStaleness(unittest.TestCase):
    def test_pending_stamp_uses_the_run_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            run_dir.mkdir()
            (run_dir / "run.json").write_text(
                json.dumps({"commit": "abc1234", "ts": 1700000000}), encoding="utf-8")
            write_pending(root, "block", [], run_dir)
            text = (root / ".council" / "pending.md").read_text(encoding="utf-8")
            self.assertIn("reviewed abc1234", text)

    def test_stale_pending_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".council").mkdir()
            (root / ".council" / "pending.md").write_text(
                "[background council review] BLOCK: 0 confirmed "
                "(reviewed deadbeef @ 2026-01-01 00:00)\n", encoding="utf-8")
            buffer = io.StringIO()
            with mock.patch("council.gitutil.head_commit", return_value="cafebabe"), \
                 mock.patch("sys.stdout", buffer):
                _print_pending(root)
            self.assertIn("[STALE:", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
