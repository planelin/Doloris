"""
tests.test_goal_mode — Goal 目标模式与防熄屏电源管理单元测试
=========================================================
验证:
1. set_keep_awake 标志位与显示器常亮控制 (ES_DISPLAY_REQUIRED)；
2. CLI 对 --goal 与 --goal-target 的正确解析与分派；
3. Goal 提示词严格约束为 "/goal [目标]"，绝不多余拼接；
4. 懒人模式零 AGY 纯净目标提炼，绝不添加“推进并完成：”等多余前缀；
5. analyze_goal_pause 对各类暂停模式的自主识别（计划草案审批、request_user_input、文本推荐项、goal paused）；
6. 看门狗自主跟进推荐决策循环，杜绝计划草案单回合 task_complete 误判早退。
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from afk_supervisor.cli import build_arg_parser
from afk_supervisor.goal_engine import (
    analyze_goal_pause,
    check_plan_status,
    extract_clean_goal,
    extract_goal_via_agy,
    extract_goal_via_agy_agent,
    extract_recent_dialogue_summary,
    get_existing_thread_goal,
    resolve_stalled_goal_via_agy,
    run_goal_supervisor,
)
from afk_supervisor.platform.windows import (
    ES_CONTINUOUS,
    ES_DISPLAY_REQUIRED,
    ES_SYSTEM_REQUIRED,
    keep_awake,
    set_keep_awake,
)


class TestScreenSleepKeepAwake(unittest.TestCase):
    """测试 Win32 防熄屏与系统电源控制。"""

    def test_set_keep_awake_flags(self):
        with patch("sys.platform", "win32"), patch("ctypes.windll") as mock_windll:
            mock_windll.kernel32.SetThreadExecutionState.return_value = 1

            # 1. 开启系统 + 显示器防熄屏
            ok = set_keep_awake(enable=True, keep_display=True)
            self.assertTrue(ok)
            expected_flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
            mock_windll.kernel32.SetThreadExecutionState.assert_called_with(expected_flags)

            # 2. 仅系统防休眠 (允许显示器熄屏)
            ok = set_keep_awake(enable=True, keep_display=False)
            self.assertTrue(ok)
            expected_flags_nodisp = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
            mock_windll.kernel32.SetThreadExecutionState.assert_called_with(expected_flags_nodisp)

            # 3. 恢复系统默认策略
            ok = set_keep_awake(enable=False)
            self.assertTrue(ok)
            mock_windll.kernel32.SetThreadExecutionState.assert_called_with(ES_CONTINUOUS)

    def test_keep_awake_backward_compatibility(self):
        with patch("sys.platform", "win32"), patch("afk_supervisor.platform.windows.set_keep_awake") as mock_set:
            keep_awake()
            mock_set.assert_called_with(enable=True, keep_display=True)


class TestGoalModeCli(unittest.TestCase):
    """测试 Goal 模式命令行解析与模式识别。"""

    def test_goal_flag_parsing(self):
        ap = build_arg_parser()
        args = ap.parse_args(["--goal", "--goal-target", "完成全部单测", "--adopt", "last", "--quick"])
        self.assertTrue(args.goal)
        self.assertEqual(args.goal_target, "完成全部单测")

    def test_adopt_mode_goal(self):
        ap = build_arg_parser()
        args = ap.parse_args(["--adopt-mode", "goal", "--adopt", "last", "--quick"])
        self.assertEqual(args.adopt_mode, "goal")


class TestGoalEngine(unittest.TestCase):
    """测试 Goal 引擎的核心逻辑与自主跟进能力。"""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_extract_recent_dialogue_summary(self):
        rollout_file = self.run_dir / "test_rollout.jsonl"
        events = [
            {"type": "event_msg", "payload": {"type": "user_message", "message": "你好，请帮我重构订单服务"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"text": "好的，我已经分析了代码结构。"}]}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "请接着编写单元测试并确保100%通过"}},
        ]
        with open(rollout_file, "w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")

        summary = extract_recent_dialogue_summary(rollout_file, max_turns=3)
        self.assertIn("请帮我重构订单服务", summary)
        self.assertIn("请接着编写单元测试并确保100%通过", summary)

    def test_extract_clean_goal_no_unwanted_prefixes(self):
        """测试纯净目标提取绝不添加“推进并完成：”等噪音前缀。"""
        # 1. 从已有 thread_goal_updated 继承
        rollout_file = self.run_dir / "rollout_with_goal.jsonl"
        with open(rollout_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "event_msg",
                "payload": {
                    "type": "thread_goal_updated",
                    "goal": {"objective": "推进并完成：完善doloris的机制，暂时不用修改动画效果"}
                }
            }) + "\n")
        goal = extract_clean_goal(rollout_file, title="")
        self.assertEqual(goal, "完善doloris的机制，暂时不用修改动画效果")
        self.assertFalse(goal.startswith("推进并完成："))

        # 2. 从 title 提取 (AGY 离线或回退场景)
        with patch("afk_supervisor.goal_engine.extract_goal_via_agy_agent", return_value=None):
            goal_title = extract_clean_goal(None, title="重构认证模块……")
            self.assertEqual(goal_title, "重构认证模块")

    def test_analyze_goal_pause_proposed_plan(self):
        """测试对模型提出的计划草案自动识别为 approve_plan。"""
        rollout_file = self.run_dir / "rollout_plan.jsonl"
        with open(rollout_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "<proposed_plan>\n# 方案：架构优化\n## 1. 阶段目标\n</proposed_plan>"}]
                }
            }) + "\n")
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}) + "\n")

        info = analyze_goal_pause(rollout_file)
        self.assertTrue(info["is_paused"])
        self.assertEqual(info["pause_type"], "proposed_plan")
        self.assertEqual(info["action"], "approve_plan")
        self.assertEqual(info["choice"], "请按计划执行")

    def test_analyze_goal_pause_request_user_input(self):
        """测试对 request_user_input 交互提问自动提取带有 (Recommended) 的推荐选项。"""
        rollout_file = self.run_dir / "rollout_rui.jsonl"
        rui_payload = {
            "type": "function_call",
            "name": "request_user_input",
            "arguments": json.dumps({
                "questions": [
                    {
                        "header": "演进方向",
                        "id": "direction",
                        "options": [
                            {"label": "极简重构 (Recommended)", "description": "旁路观察"},
                            {"label": "只做接管", "description": "保持原样"}
                        ]
                    }
                ]
            })
        }
        with open(rollout_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "response_item", "payload": rui_payload}) + "\n")
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}) + "\n")

        info = analyze_goal_pause(rollout_file)
        self.assertTrue(info["is_paused"])
        self.assertEqual(info["pause_type"], "request_user_input")
        self.assertEqual(info["action"], "inject_choice")
        self.assertEqual(info["choice"], "极简重构 (Recommended)")

    def test_analyze_goal_pause_text_recommended(self):
        """测试对正文中含有 (Recommended) 标记的选项自动提取对应选项序号。"""
        rollout_file = self.run_dir / "rollout_text_rec.jsonl"
        msg = "请选择方案：\n1. 方案一：采用原生注入 (Recommended)\n2. 方案二：采用剪贴板"
        with open(rollout_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": msg}]
                }
            }) + "\n")
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}) + "\n")

        info = analyze_goal_pause(rollout_file)
        self.assertTrue(info["is_paused"])
        self.assertEqual(info["pause_type"], "text_options")
        self.assertEqual(info["action"], "inject_choice")
        self.assertIn("1. 方案一：采用原生注入", info["choice"])

    def test_analyze_goal_pause_goal_paused_status(self):
        """测试对 thread_goal_updated: paused 状态自动识别为 resume_goal。"""
        rollout_file = self.run_dir / "rollout_gpause.jsonl"
        with open(rollout_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "event_msg",
                "payload": {
                    "type": "thread_goal_updated",
                    "goal": {"status": "paused", "objective": "完成任务"}
                }
            }) + "\n")
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}) + "\n")

        info = analyze_goal_pause(rollout_file)
        self.assertTrue(info["is_paused"])
        self.assertEqual(info["pause_type"], "goal_paused")
        self.assertEqual(info["action"], "resume_goal")
        self.assertEqual(info["choice"], "继续推进目标")

    def test_goal_prompt_strict_format(self):
        """测试注入的 Goal 提示词严格只有 '/goal [目标]'，没有多余的前缀与噪音。"""
        rollout_file = self.run_dir / "rollout.jsonl"
        rollout_file.write_text("", encoding="utf-8")

        args = MagicMock()
        args.max_run_sec = 10

        def append_turn_complete(*a, **kw):
            with open(rollout_file, "a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "所有任务已完成"}}) + "\n")

        with patch("afk_supervisor.goal_engine.find_best_codex_window", return_value=0), \
             patch("afk_supervisor.goal_engine.set_keep_awake"), \
             patch("time.sleep", side_effect=append_turn_complete):
            res = run_goal_supervisor(
                sid="sess-test-1234",
                rollout=rollout_file,
                scwd=str(self.run_dir),
                title="测试任务",
                args=args,
                run_dir=self.run_dir,
                goal_target="完成所有测试用例",
            )
            self.assertEqual(res, 0)

            prompt_file = self.run_dir / "goal_prompt.txt"
            self.assertTrue(prompt_file.exists())
            prompt_text = prompt_file.read_text(encoding="utf-8").strip()
            self.assertEqual(prompt_text, "/goal 完成所有测试用例")

            report_file = self.run_dir / "report.md"
            self.assertTrue(report_file.exists())
            report_content = report_file.read_text(encoding="utf-8")
            self.assertIn("SUCCESS", report_content)

    def test_autopilot_approves_plan_and_continues_to_completion(self):
        """测试看门狗遇到计划草案时不早退，自主下发批准后持续守护直至终态。"""
        rollout_file = self.run_dir / "rollout_multi.jsonl"
        rollout_file.write_text("", encoding="utf-8")

        args = MagicMock()
        args.max_run_sec = 15

        step = [0]
        def simulate_codex_turns(*a, **kw):
            step[0] += 1
            if step[0] == 1:
                # 轮次 1: 提出计划草案并 task_complete
                with open(rollout_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "type": "response_item",
                        "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "<proposed_plan>\n# 优化方案\n</proposed_plan>"}]}
                    }) + "\n")
                    f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}) + "\n")
            elif step[0] == 2:
                # 轮次 2: 收到看门狗自主批准后，开始工作并完工
                with open(rollout_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n")
                    f.write(json.dumps({
                        "type": "response_item",
                        "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "已全部完成所有需求与测试"}]}
                    }) + "\n")
                    f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}) + "\n")

        with patch("afk_supervisor.goal_engine.find_best_codex_window", return_value=0), \
             patch("afk_supervisor.goal_engine.set_keep_awake"), \
             patch("time.sleep", side_effect=simulate_codex_turns):
            res = run_goal_supervisor(
                sid="sess-autopilot",
                rollout=rollout_file,
                scwd=str(self.run_dir),
                title="自动推进计划任务",
                args=args,
                run_dir=self.run_dir,
                goal_target="优化系统机制",
            )
            self.assertEqual(res, 0)

            # 验证 interventions.jsonl 中记录了 GOAL_AUTOPILOT
            ivl_file = self.run_dir / "interventions.jsonl"
            self.assertTrue(ivl_file.exists())
            records = [json.loads(line) for line in ivl_file.read_text(encoding="utf-8").splitlines() if line.strip()]
            autopilot_events = [r for r in records if r.get("event") == "GOAL_AUTOPILOT"]
            self.assertEqual(len(autopilot_events), 1)
            self.assertEqual(autopilot_events[0]["pause_type"], "proposed_plan")
            self.assertEqual(autopilot_events[0]["choice"], "请按计划执行")

    def test_paused_session_does_not_falsely_succeed(self):
        """核心防护测试：接管已暂停/打断的会话时，绝不误判为完成！"""
        rollout_file = self.run_dir / "rollout_paused.jsonl"
        with open(rollout_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "turn_aborted"}}) + "\n")

        args = MagicMock()
        args.max_run_sec = 10

        with patch("afk_supervisor.goal_engine.find_best_codex_window", return_value=0), \
             patch("afk_supervisor.goal_engine.set_keep_awake"), \
             patch("time.monotonic", side_effect=[0, 1, 35, 36, 37, 38, 39, 40]):
            res = run_goal_supervisor(
                sid="sess-paused",
                rollout=rollout_file,
                scwd=str(self.run_dir),
                title="已暂停任务",
                args=args,
                run_dir=self.run_dir,
                goal_target="推进并完成",
            )
            self.assertNotEqual(res, 0)
            report_file = self.run_dir / "report.md"
            self.assertTrue(report_file.exists())
            report_content = report_file.read_text(encoding="utf-8")
            self.assertNotIn("SUCCESS", report_content)

    def test_new_turn_aborted_marked_cancelled_not_success(self):
        """测试 Goal 回合中途被打断时判定为 CANCELLED，而非 SUCCESS。"""
        rollout_file = self.run_dir / "rollout_abort.jsonl"
        rollout_file.write_text("", encoding="utf-8")

        args = MagicMock()
        args.max_run_sec = 10

        def append_turn_aborted(*a, **kw):
            with open(rollout_file, "a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "event_msg", "payload": {"type": "turn_aborted"}}) + "\n")

        with patch("afk_supervisor.goal_engine.find_best_codex_window", return_value=0), \
             patch("afk_supervisor.goal_engine.set_keep_awake"), \
             patch("time.sleep", side_effect=append_turn_aborted):
            res = run_goal_supervisor(
                sid="sess-abort",
                rollout=rollout_file,
                scwd=str(self.run_dir),
                title="打断任务",
                args=args,
                run_dir=self.run_dir,
                goal_target="推进并完成",
            )
            self.assertNotEqual(res, 0)
            report_file = self.run_dir / "report.md"
            report_content = report_file.read_text(encoding="utf-8")
            self.assertIn("CANCELLED", report_content)
            self.assertNotIn("SUCCESS", report_content)

    def test_get_existing_thread_goal(self):
        """测试能够从 rollout 中精准检测已有活跃目标，未设立目标时返回 None。"""
        rollout_file = self.run_dir / "rollout_existing_goal.jsonl"
        with open(rollout_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "event_msg",
                "payload": {
                    "type": "thread_goal_updated",
                    "goal": {"status": "active", "objective": "优化系统性能"}
                }
            }) + "\n")
        res = get_existing_thread_goal(rollout_file)
        self.assertEqual(res, "优化系统性能")

        empty_rollout = self.run_dir / "rollout_empty.jsonl"
        empty_rollout.write_text("", encoding="utf-8")
        self.assertIsNone(get_existing_thread_goal(empty_rollout))

    def test_check_plan_status_lifecycle(self):
        """测试 check_plan_status 对 not_set、planning、executing 的准确识别。"""
        rollout_file = self.run_dir / "rollout_phases.jsonl"

        # 1. 目标未设立
        with open(rollout_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "session_meta", "payload": {"id": "sess-1"}}) + "\n")
        self.assertEqual(check_plan_status(rollout_file), "not_set")

        # 2. 目标已设立，正处于计划制定阶段
        with open(rollout_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "thread_goal_updated", "goal": {"status": "active", "objective": "测试目标"}}}) + "\n")
            f.write(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "<proposed_plan>\n# 方案\n</proposed_plan>"}]}}) + "\n")
        self.assertEqual(check_plan_status(rollout_file), "planning")

        # 3. 计划已批准且执行工具已派发，进入 executing
        with open(rollout_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "请按计划执行"}]}}) + "\n")
            f.write(json.dumps({"type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "arguments": "{}"}}) + "\n")
        self.assertEqual(check_plan_status(rollout_file), "executing")

    def test_already_set_goal_skips_injection(self):
        """测试当目标已设立时，Goal 模式跳过指令注入直接进入守护。"""
        rollout_file = self.run_dir / "rollout_skip_inject.jsonl"
        with open(rollout_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "thread_goal_updated", "goal": {"status": "active", "objective": "已有任务"}}}) + "\n")

        args = MagicMock()
        args.max_run_sec = 10

        def append_done(*a, **kw):
            with open(rollout_file, "a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "所有任务已完成"}}) + "\n")

        with patch("afk_supervisor.goal_engine.find_best_codex_window", return_value=0), \
             patch("afk_supervisor.goal_engine.set_keep_awake"), \
             patch("time.sleep", side_effect=append_done):
            res = run_goal_supervisor(
                sid="sess-skip",
                rollout=rollout_file,
                scwd=str(self.run_dir),
                title="跳过注入测试",
                args=args,
                run_dir=self.run_dir,
                goal_target="__ALREADY_SET__",
            )
            self.assertEqual(res, 0)

            prompt_file = self.run_dir / "goal_prompt.txt"
            self.assertTrue(prompt_file.exists())
            self.assertIn("Existing goal: 已有任务", prompt_file.read_text(encoding="utf-8"))

            ivl_file = self.run_dir / "interventions.jsonl"
            records = [json.loads(l) for l in ivl_file.read_text(encoding="utf-8").splitlines() if l.strip()]
            phase1_ev = [r for r in records if r.get("event") == "GOAL_PHASE" and r.get("phase") == 1]
            self.assertTrue(len(phase1_ev) >= 1)
            self.assertEqual(phase1_ev[0]["status"], "ALREADY_SET")

    def test_extract_clean_goal_via_agy_success(self):
        """测试懒人模式下成功调用 AGY 提炼纯净目标并去除噪音前缀。"""
        rollout_file = self.run_dir / "rollout_agy_test.jsonl"
        with open(rollout_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "session_meta", "payload": {"id": "test-sid-agy"}}) + "\n")
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "请优化数据库查询索引并补充单元测试"}}) + "\n")

        with patch("afk_supervisor.goal_engine.extract_goal_via_agy_agent", return_value="优化数据库索引并补充测试用例"):
            goal = extract_clean_goal(rollout_file, title="优化数据库", agy_mgr=MagicMock())
            self.assertEqual(goal, "优化数据库索引并补充测试用例")
            self.assertFalse(goal.startswith("推进并完成"))
            self.assertFalse(goal.startswith("/goal"))

    def test_extract_clean_goal_agy_fallback_to_heuristic(self):
        """测试 AGY 提炼未果时优雅回退至对话启发式提取且不添加噪音。"""
        rollout_file = self.run_dir / "rollout_agy_fallback.jsonl"
        with open(rollout_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "session_meta", "payload": {"id": "test-sid-fb"}}) + "\n")
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "完成重构支付网关接口"}}) + "\n")

        with patch("afk_supervisor.goal_engine.extract_goal_via_agy_agent", return_value=None):
            goal = extract_clean_goal(rollout_file, title="支付网关重构", agy_mgr=MagicMock())
            self.assertEqual(goal, "支付网关重构")
            self.assertFalse(goal.startswith("推进并完成"))

    def test_gui_inject_always_navigates_target_sid(self):
        """测试只要提供了 target_sid，GUI 注入前必然执行深链导航以切换窗口。"""
        from afk_supervisor.platform.gui import inject_into_codex_gui
        with patch("afk_supervisor.platform.gui._navigate_target") as mock_nav, \
             patch("afk_supervisor.platform.gui.find_best_codex_window", return_value=12345), \
             patch("subprocess.run") as mock_sub:
            mock_sub.return_value.stdout = json.dumps({"ok": True, "delivery_status": "SENT", "method": "verified_task_enter"})
            res = inject_into_codex_gui("test text", target_hwnd=12345, target_sid="01a0c793-01bc-7c92-a3b6-0e735df4da1d", target_title="Test Title")
            mock_nav.assert_called_once_with("01a0c793-01bc-7c92-a3b6-0e735df4da1d")
    def test_mascot_prompt_goal_mode_skips_when_goal_exists(self):
        """测试桌宠 prompt_goal_mode 检测到已有活跃目标时跳过弹窗直接启动。"""
        from doloris_app.mascot import DesktopMascot
        root_mock = MagicMock()
        root_mock.winfo_screenwidth.return_value = 1920
        root_mock.winfo_screenheight.return_value = 1080
        root_mock.winfo_x.return_value = 100
        root_mock.winfo_y.return_value = 100

        rollout_file = self.run_dir / "rollout_existing_goal.jsonl"
        with open(rollout_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "session_meta", "payload": {"id": "01a0c8d7-8810-7780-bc8d-511c138c79b4"}}) + "\n")
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "thread_goal_updated", "goal": {"status": "active", "objective": "完善doloris机制"}}}) + "\n")

        with patch.object(DesktopMascot, "_load_current_state_frames"), \
             patch.object(DesktopMascot, "_animate"), \
             patch("doloris_app.mascot.SpeechBubble"), \
             patch("afk_supervisor.sessions.discovery.find_codex_session_by_id", return_value=("01a0c8d7-8810-7780-bc8d-511c138c79b4", rollout_file, str(self.run_dir))):
            mascot = DesktopMascot(root_mock, test_mode=True)
            mascot.show_bubble = MagicMock()
            mascot.start_mode = MagicMock()
            mascot.prompt_goal_mode("01a0c8d7-8810-7780-bc8d-511c138c79b4")
            mascot.start_mode.assert_called_once_with("goal", "01a0c8d7-8810-7780-bc8d-511c138c79b4", goal_target="__ALREADY_SET__")
            mascot.show_bubble.assert_called_once()
            self.assertIn("已设立目标", mascot.show_bubble.call_args[0][0])


    def test_mascot_prompt_goal_mode_opens_dialog_when_no_goal(self):
        """测试未设立目标时正确弹出弹窗，且初始不限时。"""
        from doloris_app.mascot import DesktopMascot
        root_mock = MagicMock()
        root_mock.winfo_screenwidth.return_value = 1920
        root_mock.winfo_screenheight.return_value = 1080
        root_mock.winfo_x.return_value = 100
        root_mock.winfo_y.return_value = 100

        rollout_file = self.run_dir / "rollout_no_goal.jsonl"
        with open(rollout_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "session_meta", "payload": {"id": "01a0c793-01bc-7c92-a3b6-0e735df4da1d"}}) + "\n")
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "继续线程工作"}}) + "\n")

        with patch.object(DesktopMascot, "_load_current_state_frames"), \
             patch.object(DesktopMascot, "_animate"), \
             patch("doloris_app.mascot.SpeechBubble"), \
             patch("afk_supervisor.sessions.discovery.find_codex_session_by_id", return_value=("01a0c793-01bc-7c92-a3b6-0e735df4da1d", rollout_file, str(self.run_dir))), \
             patch("tkinter.Toplevel") as mock_top:
            mascot = DesktopMascot(root_mock, test_mode=True)
            mascot.start_mode = MagicMock()
            mascot.prompt_goal_mode("01a0c793-01bc-7c92-a3b6-0e735df4da1d")
            # 确认弹出了 Toplevel 窗口
            mock_top.assert_called_once()
            # 确认未跳过设定
            mascot.start_mode.assert_not_called()

    def test_analyze_goal_pause_goal_stalled(self):
        """测试对模型进入目标停滞 (update_goal blocked 及阻塞询问) 的精准识别。"""
        rollout_file = self.run_dir / "rollout_stalled.jsonl"
        with open(rollout_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "update_goal",
                    "arguments": json.dumps({"status": "blocked"}),
                }
            }) + "\n")
            f.write(json.dumps({
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "当前任务被阻塞：尚未收到主视觉方向确认，因此不能按你的要求继续制作界面。\n\n请回复 `1`（深色科技感）、`2`（浅色极简）或 `3`（暖色编辑风）。"}]
                }
            }) + "\n")
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}) + "\n")

        info = analyze_goal_pause(rollout_file)
        self.assertTrue(info["is_paused"])
        self.assertEqual(info["pause_type"], "goal_stalled")
        self.assertEqual(info["action"], "inject_choice")
        self.assertIn("1. 深色科技感", info["choice"])

    def test_real_stalled_session_01a0c96f_detected(self):
        """使用真实测试会话 01a0c96f 的轨迹验证目标停滞识别。"""
        real_rollout = Path(r"C:\Users\lastnut\.codex\sessions\2026\09\22\rollout-2026-09-22T22-05-41-01a0c96f-cacb-7651-9553-4bc6524bb4a4.jsonl")
        if real_rollout.exists():
            info = analyze_goal_pause(real_rollout)
            self.assertTrue(info["is_paused"])
            self.assertEqual(info["pause_type"], "goal_stalled")
            self.assertEqual(info["action"], "inject_choice")
            self.assertIn("1. 深色科技感", info["choice"])

    def test_stalled_goal_triggers_agy_breakthrough_in_watchdog(self):
        """测试看门狗遇到目标停滞时不会误判完工退出，而是呼叫 AGY 破局并注入决策继续守护。"""
        rollout_file = self.run_dir / "rollout_stalled_watchdog.jsonl"
        rollout_file.write_text("", encoding="utf-8")

        args = MagicMock()
        args.max_run_sec = 15

        step = [0]
        def simulate_stalled_and_then_done(*a, **kw):
            step[0] += 1
            if step[0] == 1:
                # 轮次 1: 出现阻塞与停滞
                with open(rollout_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "type": "event_msg",
                        "payload": {"type": "thread_goal_updated", "goal": {"status": "blocked", "objective": "制作界面"}}
                    }) + "\n")
                    f.write(json.dumps({
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "当前任务被阻塞：尚未收到主视觉确认。\n\n请回复 `1`（深色科技感）或 `2`（浅色极简）。"}]
                        }
                    }) + "\n")
                    f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}) + "\n")
            elif step[0] == 2:
                # 轮次 2: 破局注入后恢复执行并最终完工
                with open(rollout_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n")
                    f.write(json.dumps({
                        "type": "event_msg",
                        "payload": {"type": "thread_goal_updated", "goal": {"status": "active", "objective": "制作界面"}}
                    }) + "\n")
                    f.write(json.dumps({
                        "type": "response_item",
                        "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "已按深色科技感全部完成界面与单测"}]}
                    }) + "\n")
                    f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}) + "\n")

        with patch("afk_supervisor.goal_engine.find_best_codex_window", return_value=0), \
             patch("afk_supervisor.goal_engine.set_keep_awake"), \
             patch("afk_supervisor.goal_engine.resolve_stalled_goal_via_agy", return_value="1. 深色科技感") as mock_agy, \
             patch("time.sleep", side_effect=simulate_stalled_and_then_done):
            res = run_goal_supervisor(
                sid="sess-stalled-test",
                rollout=rollout_file,
                scwd=str(self.run_dir),
                title="测试破局流转",
                args=args,
                run_dir=self.run_dir,
                goal_target="制作界面",
            )
            self.assertEqual(res, 0)
            mock_agy.assert_called_once()

            ivl_file = self.run_dir / "interventions.jsonl"
            self.assertTrue(ivl_file.exists())
            records = [json.loads(line) for line in ivl_file.read_text(encoding="utf-8").splitlines() if line.strip()]
            stalled_events = [r for r in records if r.get("event") == "GOAL_STALLED"]
            self.assertTrue(len(stalled_events) >= 1)

            autopilot_events = [r for r in records if r.get("event") == "GOAL_AUTOPILOT"]
            self.assertTrue(len(autopilot_events) >= 1)
            self.assertEqual(autopilot_events[0]["pause_type"], "goal_stalled")
            self.assertEqual(autopilot_events[0]["choice"], "1. 深色科技感")

    def test_codex_agy_registry_1to1_binding(self):
        """测试 1 Codex Task <-> 1 AGY Conversation 单一映射与持久化绑定。"""
        from afk_supervisor.l2.bridge import (
            bind_agy_conversation_for_codex,
            get_agy_conversation_for_codex,
        )
        fake_reg = self.run_dir / "test_registry.json"
        fake_brain = self.run_dir / "fake_brain"
        fake_brain.mkdir(parents=True, exist_ok=True)
        (fake_brain / "agy-conv-123").mkdir(parents=True, exist_ok=True)

        with patch("afk_supervisor.l2.bridge.get_codex_agy_registry_file", return_value=fake_reg), \
             patch("afk_supervisor.l2.bridge.get_agy_brain_dir", return_value=fake_brain):
            # 绑定 raw uuid
            sid = "01a0c96f-cacb-7651-9553-4bc6524bb4a4"
            cid = "agy-conv-123"
            bind_agy_conversation_for_codex(sid, cid, run_dir=self.run_dir)

            # 通过 codex://threads/ URL 格式查询也能命中单一映射
            url_sid = f"codex://threads/{sid}"
            found_cid = get_agy_conversation_for_codex(url_sid, run_dir=self.run_dir)
            self.assertEqual(found_cid, "agy-conv-123")

            # 验证 run_dir 中的 agy_session.json 也已同步
            session_json = self.run_dir / "agy_session.json"
            self.assertTrue(session_json.exists())
            data = json.loads(session_json.read_text(encoding="utf-8"))
            self.assertEqual(data.get("agy_conversation_id"), "agy-conv-123")

    def test_extract_goal_reuses_existing_agy_conversation(self):
        """测试已绑定 AGY 会话时，提炼目标调用 send-message 而非重复新建会话。"""
        fake_brain = self.run_dir / "fake_brain"
        fake_brain.mkdir(parents=True, exist_ok=True)
        conv_dir = fake_brain / "agy-cid-reused" / ".system_generated" / "logs"
        conv_dir.mkdir(parents=True, exist_ok=True)
        trans_file = conv_dir / "transcript.jsonl"
        trans_file.write_text(
            json.dumps({"type": "PLANNER_RESPONSE", "status": "DONE", "content": "历史无关对话"}) + "\n",
            encoding="utf-8"
        )

        def append_new_response(cmd, *args, **kwargs):
            if isinstance(cmd, list) and ("send-message" in cmd or "new-conversation" in cmd):
                with open(trans_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"type": "PLANNER_RESPONSE", "status": "DONE", "content": "实现订单自动退款流转"}) + "\n")
                res = MagicMock()
                res.returncode = 0
                res.stdout = b'{"response":{}}'
                return res
            res = MagicMock()
            res.returncode = 0
            res.stdout = b'{"response":{}}'
            return res

        with patch("afk_supervisor.goal_engine.get_agy_conversation_for_codex", return_value="agy-cid-reused"), \
             patch("afk_supervisor.goal_engine.get_agy_brain_dir", return_value=fake_brain), \
             patch("afk_supervisor.goal_engine.discover_antigravity_bridge", return_value=("csrf", ["5555"], Path(sys.executable))), \
             patch("subprocess.run", side_effect=append_new_response) as mock_sub:

            res = extract_goal_via_agy_agent(
                rollout_path=None,
                title="订单模块",
                run_dir=self.run_dir,
                timeout_sec=5.0,
                codex_session_id="codex-sess-order",
            )
            self.assertEqual(res, "实现订单自动退款流转")
            # 校验调用了 send-message
            called_cmd = mock_sub.call_args[0][0]
            self.assertIn("send-message", called_cmd)
            self.assertIn("agy-cid-reused", called_cmd)
            self.assertNotIn("new-conversation", called_cmd)

    def test_resolve_stalled_goal_reuses_existing_agy_conversation(self):
        """测试已绑定 AGY 会话时，目标破局调用 send-message 进行决断。"""
        fake_brain = self.run_dir / "fake_brain"
        fake_brain.mkdir(parents=True, exist_ok=True)
        conv_dir = fake_brain / "agy-cid-stalled" / ".system_generated" / "logs"
        conv_dir.mkdir(parents=True, exist_ok=True)
        trans_file = conv_dir / "transcript.jsonl"
        trans_file.write_text(
            json.dumps({"type": "PLANNER_RESPONSE", "status": "DONE", "content": "历史无关选项"}) + "\n",
            encoding="utf-8"
        )

        def append_breakthrough(cmd, *args, **kwargs):
            if isinstance(cmd, list) and ("send-message" in cmd or "new-conversation" in cmd):
                with open(trans_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"type": "PLANNER_RESPONSE", "status": "DONE", "content": "1"}) + "\n")
                res = MagicMock()
                res.returncode = 0
                res.stdout = b'{"response":{}}'
                return res
            res = MagicMock()
            res.returncode = 0
            res.stdout = b'{"response":{}}'
            return res

        with patch("afk_supervisor.goal_engine.get_agy_conversation_for_codex", return_value="agy-cid-stalled"), \
             patch("afk_supervisor.goal_engine.get_agy_brain_dir", return_value=fake_brain), \
             patch("afk_supervisor.goal_engine.discover_antigravity_bridge", return_value=("csrf", ["5555"], Path(sys.executable))), \
             patch("subprocess.run", side_effect=append_breakthrough) as mock_sub:

            choice = resolve_stalled_goal_via_agy(
                rollout_path=None,
                title="阻塞会话",
                last_msg="请确认选择 1 还是 2",
                run_dir=self.run_dir,
                timeout_sec=5.0,
                codex_session_id="codex-sess-stalled",
            )
            self.assertEqual(choice, "1")
            called_cmd = mock_sub.call_args[0][0]
            self.assertIn("send-message", called_cmd)
            self.assertIn("agy-cid-stalled", called_cmd)
            self.assertNotIn("new-conversation", called_cmd)

    def test_mascot_menu_labels_and_alignment(self):
        """测试桌宠右键菜单与模式子菜单文本符合设计规范并严格对齐。"""
        import inspect
        from doloris_app import mascot
        src = inspect.getsource(mascot.DesktopMascot._create_context_menu)
        self.assertIn('label="🔍 调整显示大小..."', src)
        self.assertNotIn('(输入 5%~500%)', src)

        rebuild_src = inspect.getsource(mascot.DesktopMascot._rebuild_start_menu)
        self.assertIn('"🎯 Goal 目标模式"', rebuild_src)
        self.assertIn('"🔱 Fork 无头并发"', rebuild_src)
        self.assertIn('"⚡ Kill 快速接管"', rebuild_src)
        self.assertIn('"🖥 Gui  有头注入"', rebuild_src)

        dialog_src = inspect.getsource(mascot.DesktopMascot._show_session_picker_dialog)
        self.assertIn('text="🎯 Goal 目标模式"', dialog_src)
        self.assertIn('text="🔱 Fork 无头并发"', dialog_src)
        self.assertIn('text="⚡ Kill 快速接管"', dialog_src)
        self.assertIn('text="🖥 Gui  有头注入"', dialog_src)


if __name__ == "__main__":
    unittest.main()




