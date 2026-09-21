"""Regressions from the local session audit. No live worker/GUI/network calls."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from afk_supervisor.baseline import TaskBaseline, extract_task_baseline
from afk_supervisor.coordinator import SupervisorCoordinator
from afk_supervisor.evidence import collect_evidence, calculate_reviewed_revision
from afk_supervisor.l2.protocol import build_protocol_prompt, validate_protocol_payload
from afk_supervisor.models import EvidenceItem, L2Result
from afk_supervisor.observations import command_accepted, normalize_command_text
from afk_supervisor.state import SupervisorState


class DebugFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="afk-debug-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.ws = self.root / "workspace"
        self.ws.mkdir()
        self.run = self.root / "run"
        self.run.mkdir()
        self.baseline = TaskBaseline(
            task_id="debug-task", original_requirements="Deliver the documented result",
            session_cwd=str(self.ws), delivery_dir=str(self.ws),
            required_criteria=[{"id": "artifact", "type": "artifact"}],
        )
        self.args = SimpleNamespace(l2_max=2, timeout_sec=7, l2_model="flash", l2_project_id="project")

    def collect(self, message="Neutral worker report", **kwargs):
        return collect_evidence(self.ws, message, self.baseline, **kwargs)

    def coordinator(self, **kwargs):
        return SupervisorCoordinator(
            self.run, self.ws, self.ws, self.baseline, None, "agy", self.args, None, **kwargs
        )

    def payload(self, evidence, verdict="PASS", action="terminate_success"):
        artifacts = [item.id for item in evidence.items if item.category == "artifact"]
        return {
            "protocol": "afk_agy_protocol_v1", "request_id": evidence.request_id,
            "task_id": evidence.task_id, "mode": "REVIEW",
            "reviewed_revision": evidence.reviewed_revision, "verdict": verdict,
            "criteria": [{"id": "artifact", "verdict": verdict, "evidence_ids": artifacts, "reason": "Observed file"}],
            "blockers": [], "next_action": {"type": action, "instructions": "Review the actual result"}, "repairs": [],
        }


class EvidenceRegressions(DebugFixture):
    def test_path_punctuation_cannot_create_duplicate_evidence_ids(self):
        (self.ws / "a.b.py").write_text("value = 1\n", encoding="utf-8")
        (self.ws / "a_b.py").write_text("value = 2\n", encoding="utf-8")
        evidence = self.collect()
        ids = [item.id for item in evidence.items]
        self.assertEqual(len(ids), len(set(ids)), ids)
        self.assertFalse(evidence.mechanical_failures)

    def test_tail_change_after_64k_changes_artifact_revision(self):
        target = self.ws / "large.txt"
        target.write_bytes(b"a" * 65536 + b"before")
        before = self.collect()
        target.write_bytes(b"a" * 65536 + b"after!")
        after = self.collect()
        self.assertNotEqual(before.artifact_revision, after.artifact_revision)

    def test_verification_result_and_blockers_are_revision_bound(self):
        item = EvidenceItem("check", "verification_result", "PASS", details="passed")
        before = calculate_reviewed_revision("task", self.ws, [item], "art")
        item.summary, item.details = "FAIL", "assertion failed"
        after = calculate_reviewed_revision("task", self.ws, [item], "art")
        self.assertNotEqual(before, after)
        (self.ws / "result.txt").write_text("result", encoding="utf-8")
        before = self.collect()
        self.baseline.blockers.append("Human approval remains required")
        after = self.collect()
        self.assertNotEqual(before.reviewed_revision, after.reviewed_revision)

    def test_baseline_change_invalidates_review_without_artifact_change(self):
        (self.ws / "result.txt").write_text("result", encoding="utf-8")
        before = self.collect()
        self.baseline.add_authorized_change("user", "Also provide a second output")
        after = self.collect()
        self.assertEqual(before.artifact_revision, after.artifact_revision)
        self.assertNotEqual(before.reviewed_revision, after.reviewed_revision)

    def test_checklist_is_not_business_progress(self):
        (self.ws / "result.txt").write_text("result", encoding="utf-8")
        checklist = self.ws / "PROGRESS.md"
        checklist.write_text("- [ ] implement", encoding="utf-8")
        before = self.collect()
        checklist.write_text("- [x] implement", encoding="utf-8")
        after = self.collect()
        self.assertEqual(before.artifact_revision, after.artifact_revision)
        self.assertNotEqual(before.reviewed_revision, after.reviewed_revision)

    def test_syntax_check_does_not_create_bytecode_in_deliverables(self):
        (self.ws / "app.py").write_text("value = 1\n", encoding="utf-8")
        result = self.collect()
        self.assertFalse(result.mechanical_failures, result.mechanical_failures)
        self.assertFalse((self.ws / "__pycache__").exists())

    def test_sixth_source_file_is_not_silently_unchecked(self):
        for index in range(6):
            (self.ws / f"{index}.py").write_text("value = 1\n" if index < 5 else "def broken(:\n", encoding="utf-8")
        self.assertTrue(self.collect().mechanical_failures)

    def test_syntax_only_cannot_pass_functional_criterion(self):
        (self.ws / "app.py").write_text("def behavior():\n    raise NotImplementedError()\n", encoding="utf-8")
        self.baseline.required_criteria = [{"id": "behavior", "type": "functional"}]
        evidence = self.collect()
        payload = self.payload(evidence)
        payload["criteria"] = [{
            "id": "behavior", "verdict": "PASS", "evidence_ids": ["ev_syntax_py_app_py"],
            "reason": "Syntax alone does not demonstrate behavior",
        }]
        ok, reason = validate_protocol_payload(payload, task_baseline=self.baseline, evidence_packet=evidence, expected_mode="REVIEW")
        self.assertFalse(ok, reason)

    def test_repaired_delivery_directory_does_not_keep_stale_blocker(self):
        delivery = self.ws / "delivery"
        self.baseline = extract_task_baseline(session_cwd=self.ws, explicit_delivery_dir=str(delivery))
        self.assertTrue(self.baseline.blockers)
        delivery.mkdir()
        (delivery / "result.txt").write_text("result", encoding="utf-8")
        self.assertFalse(self.collect().mechanical_failures)


class BaselineAndStateRegressions(DebugFixture):
    def test_explicit_task_respects_delivery_flag_and_confirmation(self):
        task_dir = self.root / "task"
        task_dir.mkdir()
        task = task_dir / "task.md"
        task.write_text("交付结果。向用户确认发布方案。", encoding="utf-8")
        output = self.ws / "output"
        output.mkdir()
        baseline = extract_task_baseline(session_cwd=self.ws, task_md=task, explicit_delivery_dir=str(output))
        self.assertEqual(Path(baseline.delivery_dir), output)
        self.assertTrue(baseline.human_confirmation_required)

    def test_followup_user_requirements_not_discarded(self):
        rollout = self.root / "rollout.jsonl"
        prompts = ["Build a result", "Add export support and 向用户确认发布方案"]
        rollout.write_text("\n".join(json.dumps({"payload": {"role": "user", "content": text}}) for text in prompts), encoding="utf-8")
        baseline = extract_task_baseline(session_cwd=self.ws, rollout_path=rollout)
        self.assertEqual(baseline.original_requirements, prompts[0])
        self.assertTrue(baseline.subsequent_changes)
        self.assertIn("export", baseline.subsequent_changes[0]["change"])
        self.assertTrue(baseline.human_confirmation_required)

    def test_loading_checkpoint_is_read_only_even_on_second_load(self):
        state = SupervisorState(self.run, sid="worker", ws=self.ws)
        state.transition("REPAIRING", reviews=7, round=9)
        state.record_dispatched("req-sent", "worker_fix", "Fix only the failed assertion")
        target = self.run / "supervisor_state.json"
        original = target.read_bytes()
        original_mtime = target.stat().st_mtime_ns
        for _ in range(2):
            loaded = SupervisorState.load(self.run)
            self.assertEqual(loaded.state, "REPAIRING")
            self.assertEqual(loaded.reviews, 7)
            self.assertEqual(loaded.pending_command, state.pending_command)
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(target.stat().st_mtime_ns, original_mtime)

    def test_normalize_command_handles_nested_markdown_escapes(self):
        cmd = r"确认按该主视觉方向继续实施：使用原生 HTML/CSS/JS 在 C:\Agents\codex\double\_re 下构建"
        escaped_ui_msg = r"确认按该主视觉方向继续实施：使用原生 HTML/CSS/JS 在 C:\Agents\codex\double\\\_re 下构建"
        self.assertEqual(normalize_command_text(cmd), normalize_command_text(escaped_ui_msg))
        events = [{"payload": {"role": "user", "content": [{"text": escaped_ui_msg}]}}]
        self.assertTrue(command_accepted(events, cmd))


class CoordinatorAndPromptRegressions(DebugFixture):
    def test_payloadless_l2_errors_return_normally_in_all_modes(self):
        coordinator = self.coordinator()
        with patch("supervise.l2_dispatch", return_value=L2Result("NO-BRIDGE", "Not connected", self.run / "l2.log")):
            results = [
                coordinator.handle_interaction("Which option?", 1),
                coordinator.handle_repair("Infrastructure failed", "Details", 1),
                coordinator.handle_turn_review("Neutral report", 1),
            ]
        self.assertEqual([result[0] for result in results], ["NO-BRIDGE"] * 3)

    def test_local_verifier_exception_cannot_become_pass(self):
        (self.ws / "result.txt").write_text("result", encoding="utf-8")
        def dispatch(*args, **kwargs):
            payload = self.payload(kwargs["evidence_packet"])
            return L2Result("PASS", json.dumps(payload), self.run / "l2.log", payload=payload)
        def verify(**kwargs):
            raise OSError("Cannot read acceptance file")
        with patch("supervise.l2_dispatch", side_effect=dispatch):
            result = self.coordinator().handle_turn_review("Report", 1, verify_fn=verify)
        self.assertNotEqual(result[0], "PASS")

    def test_incremental_prompt_contains_full_current_evidence_and_requirements(self):
        (self.ws / "result.txt").write_text("result", encoding="utf-8")
        self.baseline.original_requirements = "requirement " * 200 + "FINAL_REQUIRED_DETAIL"
        evidence = self.collect()
        full, incremental = build_protocol_prompt("REVIEW", self.baseline, evidence.request_id, evidence)
        for item in evidence.items:
            self.assertIn(item.id, incremental)
        self.assertIn("FINAL_REQUIRED_DETAIL", incremental)
        self.assertIn(self.baseline.task_id, incremental)
        self.assertIn("required_criteria", incremental)


if __name__ == "__main__":
    unittest.main()
