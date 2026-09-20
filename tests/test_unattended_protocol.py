"""Unattended delegation must not regress to a human confirmation gate."""
import unittest

from afk_supervisor.baseline import TaskBaseline
from afk_supervisor.l2.protocol import build_protocol_prompt, validate_protocol_payload


class UnattendedProtocolTests(unittest.TestCase):
    def payload(self, verdict="STOP", action="terminate_blocked"):
        return {
            "protocol": "afk_agy_protocol_v1", "request_id": "req", "task_id": "task",
            "mode": "DECIDE", "reviewed_revision": "", "verdict": verdict,
            "criteria": [], "repairs": [], "blockers": ["Provider quota exhausted"],
            "next_action": {"type": action, "instructions": "Checked configured providers; none available."},
        }

    def test_stop_requires_evidence_and_matching_action(self):
        self.assertTrue(validate_protocol_payload(self.payload())[0])
        payload = self.payload()
        payload["blockers"] = []
        self.assertFalse(validate_protocol_payload(payload)[0])
        self.assertFalse(validate_protocol_payload(self.payload(action="worker_instruction"))[0])

    def test_legacy_human_request_is_not_accepted(self):
        self.assertFalse(validate_protocol_payload(self.payload("DEFER", "request_user"))[0])
        self.assertFalse(validate_protocol_payload(self.payload("PROCEED", "request_user"))[0])

    def test_confirmation_is_context_for_l2_not_human_only_instruction(self):
        baseline = TaskBaseline(task_id="task", original_requirements="向用户确认主视觉方向", human_confirmation_required=["向用户确认主视觉方向"])
        full, short = build_protocol_prompt("DECIDE", baseline, "req", question_or_context="请选择主题")
        self.assertIn("托管期间由 L2 判断并代答", full)
        self.assertNotIn("必须由用户本人确认", full)


if __name__ == "__main__":
    unittest.main()
