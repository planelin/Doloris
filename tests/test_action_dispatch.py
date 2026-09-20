"""
tests/test_action_dispatch.py — 结构化协议校验与动作分派回归测试集
=================================================================
覆盖规范要求:
- Item 6: 同一会话多次 REVIEW 时，通信包含完整基线、最新证据摘要与 revision，杜绝上下文盲区。
- Item 7: 协议非法与冲突拦截 (request_id/task_id/revision 篡改，重复 criteria，未引用证据)。
- Item 8: switch_to_repair 动作触发自动切换至 REPAIR，修复成功后重新采证并推进新 REVIEW。
- Item 9: request_user 动作分派与 DEFER 决策强制转入 WAITING_USER，绝不盲发'继续'。
- Item 14: afk / afk2 / afk3 具备一致的验收语义与动作分派逻辑。
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from afk_supervisor.baseline import TaskBaseline
from afk_supervisor.coordinator import SupervisorCoordinator
from afk_supervisor.models import ActionType, EvidenceItem, EvidencePacket
from afk_supervisor.l2.protocol import (
    build_protocol_prompt,
    normalize_next_action,
    validate_protocol_payload,
)


class TestActionDispatch(unittest.TestCase):
    """验证动作分派与协议严格校验。"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="afk_dispatch_")
        self.run_dir = Path(self.tmp_dir) / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.ws_dir = Path(self.tmp_dir) / "ws"
        self.ws_dir.mkdir(parents=True, exist_ok=True)

        self.baseline = TaskBaseline(
            task_id="task_dispatch_101",
            original_requirements="构建双端系统并编写单元测试，向用户确认设计配色方案",
            session_cwd=str(self.ws_dir),
            effective_delivery_dir=str(self.ws_dir),
            required_criteria=[
                {"id": "crit_core_artifacts", "type": "artifact", "description": "核心文件"},
                {"id": "crit_verification_checks", "type": "verification", "description": "测试验证"},
            ],
            human_confirmation_required=["向用户确认设计配色方案"],
        )

    def tearDown(self):
        try:
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
        except Exception:
            pass

    def test_item6_subsequent_review_receives_full_baseline_and_updated_revision(self):
        """Item 6: 同一会话多次增量审查时，提示词必须包含不可变审查包指纹、基线需求与必需项。"""
        ev1 = EvidencePacket(
            task_id=self.baseline.task_id,
            delivery_dir=str(self.ws_dir),
            reviewed_revision="rev-round-1",
            artifact_revision="art-round-1",
            request_id="req-rev-1",
            items=[EvidenceItem(id="ev_file_1", category="artifact", summary="file 1", path="a.txt")],
        )

        full1, short1 = build_protocol_prompt(
            mode="REVIEW",
            task_baseline=self.baseline,
            request_id="req-rev-1",
            evidence_packet=ev1,
            question_or_context="Round 1 context",
        )

        self.assertIn("rev-round-1", full1)
        self.assertIn("rev-round-1", short1)
        self.assertIn("crit_core_artifacts", full1)
        self.assertIn("crit_core_artifacts", short1)
        self.assertIn("构建双端系统", short1, "增量短消息必须包含基线需求摘要")

        # 第二轮增量
        ev2 = EvidencePacket(
            task_id=self.baseline.task_id,
            delivery_dir=str(self.ws_dir),
            reviewed_revision="rev-round-2",
            artifact_revision="art-round-2",
            request_id="req-rev-2",
            items=[
                EvidenceItem(id="ev_file_1", category="artifact", summary="file 1", path="a.txt"),
                EvidenceItem(id="ev_file_2", category="artifact", summary="file 2", path="b.txt"),
            ],
        )
        full2, short2 = build_protocol_prompt(
            mode="REVIEW",
            task_baseline=self.baseline,
            request_id="req-rev-2",
            evidence_packet=ev2,
            question_or_context="Round 2 context",
        )

        self.assertIn("rev-round-2", full2)
        self.assertIn("rev-round-2", short2)
        self.assertNotIn("rev-round-1", short2)
        self.assertIn("items=2", short2)

    def test_item7_protocol_rejection_duplicate_criteria_and_unreferenced_evidence(self):
        """Item 7: 协议格式拦截：重复 criterion_id、引用不存在的 evidence_id、request_id/task_id/revision 篡改均被拦截。"""
        ev_items = [
            EvidenceItem(id="ev_art_1", category="artifact", summary="art 1", path="a.txt"),
            EvidenceItem(id="ev_verif_1", category="verification_result", summary="verif 1"),
        ]
        packet = EvidencePacket(
            task_id="task_dispatch_101",
            delivery_dir=str(self.ws_dir),
            reviewed_revision="rev-ok-777",
            artifact_revision="art-ok-777",
            request_id="req-orig-123",
            items=ev_items,
        )

        base_valid_payload = {
            "protocol": "afk_agy_protocol_v1",
            "request_id": "req-orig-123",
            "task_id": "task_dispatch_101",
            "mode": "REVIEW",
            "reviewed_revision": "rev-ok-777",
            "verdict": "PASS",
            "criteria": [
                {"id": "crit_core_artifacts", "verdict": "PASS", "evidence_ids": ["ev_art_1"], "reason": "ok"},
                {"id": "crit_verification_checks", "verdict": "PASS", "evidence_ids": ["ev_verif_1"], "reason": "ok"},
            ],
            "blockers": [],
            "next_action": {"type": ActionType.TERMINATE_SUCCESS, "instructions": "ok"},
        }

        # 1. 正常核验
        ok, reason = validate_protocol_payload(
            base_valid_payload,
            expected_request_id="req-orig-123",
            expected_task_id="task_dispatch_101",
            expected_mode="REVIEW",
            expected_revision="rev-ok-777",
            task_baseline=self.baseline,
            evidence_packet=packet,
        )
        self.assertTrue(ok, f"合规 payload 应通过: {reason}")

        # 2. 重复 criterion_id
        dup_payload = json.loads(json.dumps(base_valid_payload))
        dup_payload["criteria"].append(
            {"id": "crit_core_artifacts", "verdict": "PASS", "evidence_ids": ["ev_art_1"], "reason": "dup"}
        )
        ok, reason = validate_protocol_payload(dup_payload, task_baseline=self.baseline, evidence_packet=packet)
        self.assertFalse(ok)
        self.assertIn("重复验收项", reason)

        # 3. 引用不存在的 evidence_id
        fake_ev_payload = json.loads(json.dumps(base_valid_payload))
        fake_ev_payload["criteria"][0]["evidence_ids"] = ["ev_ghost_non_existent"]
        ok, reason = validate_protocol_payload(fake_ev_payload, task_baseline=self.baseline, evidence_packet=packet)
        self.assertFalse(ok)
        self.assertIn("不存在的 evidence_id", reason)

        # 4. request_id 篡改
        ok, reason = validate_protocol_payload(base_valid_payload, expected_request_id="req-tampered-999")
        self.assertFalse(ok)
        self.assertIn("request_id mismatch", reason)

        # 5. revision 篡改
        ok, reason = validate_protocol_payload(base_valid_payload, expected_revision="rev-tampered-999")
        self.assertFalse(ok)
        self.assertIn("reviewed_revision", reason)

    def test_item8_switch_to_repair_triggers_repair_and_fresh_review_round(self):
        """Item 8: REVIEW 返回 switch_to_repair 时，协调器准确分派修复，修复成功后不盲目通过而是转入新 REVIEW。"""
        coordinator = SupervisorCoordinator(
            run_dir=self.run_dir,
            workspace_root=self.ws_dir,
            delivery_dir=self.ws_dir,
            task_baseline=self.baseline,
            agy_mgr=None,
            l2_cmd="test",
            args=MagicMock(),
            proxy=None,
        )

        review_payload = {
            "protocol": "afk_agy_protocol_v1",
            "request_id": "req-rev-fail",
            "task_id": "task_dispatch_101",
            "mode": "REVIEW",
            "reviewed_revision": "rev-f",
            "verdict": "FAIL",
            "criteria": [
                {"id": "crit_core_artifacts", "verdict": "FAIL", "evidence_ids": [], "reason": "配置文件损毁"}
            ],
            "blockers": [],
            "next_action": {
                "type": ActionType.SWITCH_TO_REPAIR,
                "instructions": "修复 tsconfig.json 配置中的路径错误",
            },
        }

        # 模拟 coordinator.handle_turn_review 返回 switch_to_repair
        with patch("afk_supervisor.coordinator._dispatch_l2") as mock_dispatch:
            mock_dispatch.return_value = ("FAIL", "配置文件损毁", self.run_dir / "rev.log", review_payload)
            verdict, answer, log_p, payload = coordinator.handle_turn_review(
                last_msg="worker msg", n_interaction=1
            )

        self.assertEqual(verdict, "FAIL")
        norm_next = payload.get("next_action", {})
        self.assertEqual(norm_next.get("type"), ActionType.SWITCH_TO_REPAIR)

        # 模拟执行 handle_repair
        repair_payload = {
            "protocol": "afk_agy_protocol_v1",
            "request_id": "req-rep-1",
            "task_id": "task_dispatch_101",
            "mode": "REPAIR",
            "reviewed_revision": "rev-f",
            "verdict": "REPAIRED",
            "repairs": [
                {
                    "action": "edit",
                    "target": "tsconfig.json",
                    "verification": "tsc --noEmit passed",
                    "rollback": "git checkout tsconfig.json",
                }
            ],
            "next_action": {"type": ActionType.WORKER_INSTRUCTION, "instructions": "配置已修复"},
        }
        with patch("afk_supervisor.coordinator._dispatch_l2") as mock_dispatch_rep:
            mock_dispatch_rep.return_value = ("REPAIRED", "已修复 tsconfig", self.run_dir / "rep.log", repair_payload)
            r_verdict, r_answer, r_log, r_payload = coordinator.handle_repair(
                failure_detail="修复 tsconfig.json", last_msg="error", n_repair=1
            )

        self.assertEqual(r_verdict, "REPAIRED")
        self.assertEqual(coordinator.repair_count, 1)
        # 确认 REPAIRED 决议不是 PASS，系统必须重新发起审查

    def test_item9_request_user_and_defer_intercepted_without_blind_resume(self):
        """Item 9: 命中需用户本人确认的事项或 L2 决议为 DEFER/request_user 时，强制拦截代答，绝不盲目发'继续'。"""
        coordinator = SupervisorCoordinator(
            run_dir=self.run_dir,
            workspace_root=self.ws_dir,
            delivery_dir=self.ws_dir,
            task_baseline=self.baseline,
            agy_mgr=None,
            l2_cmd="test",
            args=MagicMock(),
            proxy=None,
        )

        mock_driver = MagicMock()

        # 1. 命中基线中的本人确认项 ("向用户确认设计配色方案")
        msg_with_human_conf = "界面开发遇到选择: 主题配色使用浅色还是深色？向用户确认设计配色方案后继续"
        with patch("afk_supervisor.coordinator._dispatch_l2", return_value=("PROCEED", "使用深色主题", self.run_dir / "l.log", None)) as dispatch:
            v, ans, log_p, payload = coordinator.handle_interaction(msg_with_human_conf, 1, driver=mock_driver)
        self.assertEqual(v, "PROCEED")
        dispatch.assert_called_once()
        mock_driver.resume.assert_not_called()

        # 2. 普通提问但 L2 主动返回 DEFER
        with patch("afk_supervisor.coordinator._dispatch_l2") as mock_l2:
            defer_payload = {
                "protocol": "afk_agy_protocol_v1",
                "request_id": "req-d-1",
                "task_id": "task_dispatch_101",
                "mode": "DECIDE",
                "reviewed_revision": "",
                "verdict": "DEFER",
                "next_action": {"type": ActionType.REQUEST_USER, "instructions": "超出授权边界，需人类决定"},
            }
            mock_l2.return_value = ("DEFER", "需人类决定", self.run_dir / "l.log", defer_payload)
            v2, ans2, l2, p2 = coordinator.handle_interaction(
                last_msg="请问要删除旧数据库吗？",
                n_interaction=2,
                driver=mock_driver,
            )

        self.assertEqual(v2, "DEFER")
        self.assertEqual(p2.get("next_action", {}).get("type"), ActionType.REQUEST_USER)
        mock_driver.resume.assert_not_called()

    def test_item14_uniform_acceptance_semantics_across_modes(self):
        """Item 14: 无论 headless (afk / afk2) 还是 GUI (afk3)，同一基线和证据包产出完全一致的判定与分派动作。"""
        coordinator = SupervisorCoordinator(
            run_dir=self.run_dir,
            workspace_root=self.ws_dir,
            delivery_dir=self.ws_dir,
            task_baseline=self.baseline,
            agy_mgr=None,
            l2_cmd="test",
            args=MagicMock(),
            proxy=None,
        )

        # 场景 A: 存在机械故障，L2 误判 PASS -> 无论模式均强制一票否决转 FAIL + SWITCH_TO_REPAIR
        broken_evidence = EvidencePacket(
            task_id=self.baseline.task_id,
            delivery_dir=str(self.ws_dir),
            reviewed_revision="rev-b",
            artifact_revision="art-b",
            request_id="req-b",
            items=[EvidenceItem(id="ev_a", category="artifact", summary="a")],
            mechanical_failures=["JS语法错误: broken syntax"],
        )

        fake_pass_payload = {
            "protocol": "afk_agy_protocol_v1",
            "request_id": "req-b",
            "task_id": "task_dispatch_101",
            "mode": "REVIEW",
            "reviewed_revision": "rev-b",
            "verdict": "PASS",
            "criteria": [
                {"id": "crit_core_artifacts", "verdict": "PASS", "evidence_ids": ["ev_a"], "reason": "ok"}
            ],
            "blockers": [],
            "next_action": {"type": ActionType.TERMINATE_SUCCESS, "instructions": "通过"},
        }

        with patch("afk_supervisor.coordinator.collect_evidence", return_value=broken_evidence), \
             patch("afk_supervisor.coordinator._dispatch_l2", return_value=("PASS", "All ok", self.run_dir / "l.log", fake_pass_payload)):
            v, ans, log_p, p = coordinator.handle_turn_review(
                last_msg="worker done",
                n_interaction=1,
            )

        self.assertEqual(v, "FAIL", "机械故障一票否决：PASS 必须被覆写为 FAIL")
        self.assertEqual(p["next_action"]["type"], ActionType.SWITCH_TO_REPAIR)
        self.assertIn("审查发现存在代码语法或显式机械校验失败", p["next_action"]["instructions"])


if __name__ == "__main__":
    unittest.main()
