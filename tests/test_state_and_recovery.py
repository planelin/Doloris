"""
tests/test_state_and_recovery.py — 状态持久化、会话对齐与容灾恢复测试集
======================================================================
覆盖规范要求:
- Item 11: Fork 模式下正确记录并区分 parent_session_id 与 worker_session_id，终态报告引用子会话。
- Item 12: interventions.jsonl、supervisor_state.json 与 report.md 审计信息精准一致。
- Item 13: 中断与重启恢复能加载进度，并通过 should_dispatch 杜绝重复分派已执行动作。
- Item 15: 入口与 CLI 参数兼容性（afk.cmd, afk2.cmd, afk3.cmd 与扩展参数均正常解析）。
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from afk_supervisor.baseline import TaskBaseline
from afk_supervisor.cli import build_arg_parser
from afk_supervisor.models import ActionType
from afk_supervisor.reporting import generate_final_report
from afk_supervisor.state import SupervisorState


class TestStateAndRecovery(unittest.TestCase):
    """验证状态持久化、多会话区分与恢复防重机制。"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="afk_state_")
        self.run_dir = Path(self.tmp_dir) / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.ws_dir = Path(self.tmp_dir) / "ws"
        self.ws_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        try:
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
        except Exception:
            pass

    def test_item11_fork_mode_registers_distinct_sessions_and_reports_child(self):
        """Item 11: Fork 模式下，状态管理必须正确保留 parent_session_id 并绑定子进程 worker_session_id，终态报告引用子会话。"""
        parent_sid = "parent-thread-001"
        child_sid = "forked-child-thread-002"

        # 启动时仅有 parent_sid
        state = SupervisorState(
            run_dir=self.run_dir,
            sid=parent_sid,
            parent_session_id=parent_sid,
            mode="fork",
            ws=self.ws_dir,
        )
        self.assertEqual(state.parent_session_id, parent_sid)

        # 发现并绑定新 Fork 出来的子会话
        state.set_worker_session_id(child_sid)
        self.assertEqual(state.parent_session_id, parent_sid, "父会话ID不能被篡改")
        self.assertEqual(state.worker_session_id, child_sid, "子会话ID必须更新为真实Fork进程ID")
        self.assertEqual(state.sid, child_sid, "对外主会话标识应指向当前活跃的子会话")

        state_dict = state.to_dict()
        self.assertEqual(state_dict["parent_session_id"], parent_sid)
        self.assertEqual(state_dict["worker_session_id"], child_sid)

        # 生成终态报告
        ivl_file = self.run_dir / "interventions.jsonl"
        ivl_file.write_text("", encoding="utf-8")
        report_file = generate_final_report(
            run_dir=self.run_dir,
            state="SUCCESS",
            detail="Fork任务执行达标",
            ts="2026-09-18 18:00:00",
            session_id=state.worker_session_id,
            jsonl_path=Path("fake_rollout.jsonl"),
            providers_tried=["codex"],
            resumes=1,
            chaos=None,
            ivl_path=ivl_file,
            title="Fork测试任务",
        )

        report_text = report_file.read_text(encoding="utf-8")
        self.assertIn(f"`{child_sid}`", report_text, "报告中的会话必须引用子会话标识")
        self.assertNotIn(f"`{parent_sid}`", report_text, "报告中不应将父会话误当做当前活跃交付主体")

    def test_item12_consistency_across_interventions_state_and_report(self):
        """Item 12: interventions.jsonl、supervisor_state.json 与 report.md 的计数器和事件一致性核验。"""
        ivl_file = self.run_dir / "interventions.jsonl"
        events = [
            {"ts": "2026-09-18T18:01:00", "event": "LAUNCH", "session": "s-1"},
            {"ts": "2026-09-18T18:02:00", "event": "L2_CONSULT", "n": 1, "kind": "DECIDE"},
            {"ts": "2026-09-18T18:02:05", "event": "L2_ANSWER", "verdict": "PROCEED"},
            {"ts": "2026-09-18T18:03:00", "event": "L2_CONSULT", "n": 2, "kind": "REVIEW"},
            {"ts": "2026-09-18T18:03:05", "event": "L2_ANSWER", "verdict": "PASS"},
            {"ts": "2026-09-18T18:03:10", "event": "EXIT_OK", "acceptance": "All tests passed"},
        ]
        with open(ivl_file, "w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")

        state = SupervisorState(run_dir=self.run_dir, sid="s-1", mode="fresh", ws=self.ws_dir)
        state.interactions = 2
        state.reviews = 1
        state.repairs = 0
        state.resumes = 1
        state.save()

        baseline = TaskBaseline(
            task_id="task-audit-1",
            original_requirements="构建一致性系统",
            session_cwd=str(self.ws_dir),
            requested_delivery_dir=str(self.ws_dir),
            effective_delivery_dir=str(self.ws_dir),
        )

        report_p = generate_final_report(
            run_dir=self.run_dir,
            state="SUCCESS",
            detail="All tests passed",
            ts="2026-09-18 18:04:00",
            session_id=state.sid,
            jsonl_path=None,
            providers_tried=["codex"],
            resumes=state.resumes,
            chaos=None,
            ivl_path=ivl_file,
            title="一致性核查任务",
            baseline=baseline,
        )

        # 检查 supervisor_state.json
        state_file = self.run_dir / "supervisor_state.json"
        self.assertTrue(state_file.exists())
        saved_state = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(saved_state["interactions"], 2)
        self.assertEqual(saved_state["reviews"], 1)
        self.assertEqual(saved_state["repairs"], 0)

        # 检查 report.md
        rep_text = report_p.read_text(encoding="utf-8")
        self.assertIn("- 干预/续跑次数: 1", rep_text)
        self.assertIn("EXIT_OK", rep_text)
        self.assertIn("L2_CONSULT", rep_text)
        self.assertIn("All tests passed", rep_text)

    def test_item13_state_recovery_and_deduplication(self):
        """Item 13: 状态持久化检查点恢复与动作去重防重发机制。"""
        state = SupervisorState(run_dir=self.run_dir, sid="sess-rec-01", mode="resume", ws=self.ws_dir)
        state.round = 3
        state.resumes = 2

        req_id = "req-action-987"
        # 记录已分派动作
        state.record_dispatched(request_id=req_id, action_type=ActionType.WORKER_INSTRUCTION, command="npm test")
        self.assertFalse(state.should_dispatch(req_id), "刚分派过的请求严禁立即重复分派")
        self.assertTrue(state.should_dispatch("req-action-other"), "新请求允许分派")

        # 模拟看门狗崩溃退出，从磁盘重新 load 检查点
        recovered = SupervisorState.load(self.run_dir)
        self.assertIsNotNone(recovered, "必须成功恢复状态检查点")
        self.assertEqual(recovered.round, 3)
        self.assertEqual(recovered.resumes, 2)
        self.assertEqual(recovered.last_dispatched_request_id, req_id)
        self.assertEqual(recovered.pending_command, "npm test")
        self.assertFalse(recovered.should_dispatch(req_id), "恢复后必须识别已分派指令，防止重复向 Worker 盲发")
        self.assertTrue(recovered.should_dispatch("req-new-999"))

    def test_item15_cli_arguments_compatibility(self):
        """Item 15: afk.cmd, afk2.cmd, afk3.cmd 及扩展参数解析兼容性。"""
        parser = build_arg_parser()

        # 1. afk.cmd 参数组
        args_afk = parser.parse_args(["--adopt", "last", "--quick"])
        self.assertEqual(args_afk.adopt, "last")
        self.assertTrue(args_afk.quick)
        self.assertFalse(args_afk.fork)
        self.assertFalse(args_afk.gui)

        # 2. afk2.cmd 参数组 (含 chaos 与上限)
        args_afk2 = parser.parse_args(["--adopt", "last", "--quick", "--fork", "--chaos", "kill:120", "--max-interactions", "15"])
        self.assertEqual(args_afk2.adopt, "last")
        self.assertTrue(args_afk2.fork)
        self.assertEqual(args_afk2.chaos, "kill:120")
        self.assertEqual(args_afk2.max_interactions, 15)

        # 3. afk3.cmd 参数组 (GUI 双有头与 L2 配置)
        args_afk3 = parser.parse_args(["--adopt", "last", "--quick", "--gui", "--l2-cmd", "antigravity", "--l2-model", "flash"])
        self.assertTrue(args_afk3.gui)
        self.assertEqual(args_afk3.l2_cmd, "antigravity")
        self.assertEqual(args_afk3.l2_model, "flash")

        # 4. 扩展与兼容参数 (--resume, --delivery-dir, --proxy, --timeout-sec)
        args_ext = parser.parse_args([
            "--resume", "sid-custom-123",
            "--delivery-dir", r"C:\custom\output",
            "--proxy", "http://127.0.0.1:7890",
            "--timeout-sec", "90",
            "--quick"
        ])
        self.assertEqual(args_ext.resume, "sid-custom-123")
        self.assertEqual(args_ext.delivery_dir, r"C:\custom\output")
        self.assertEqual(args_ext.proxy, "http://127.0.0.1:7890")
        self.assertEqual(args_ext.timeout_sec, 90)


if __name__ == "__main__":
    unittest.main()
