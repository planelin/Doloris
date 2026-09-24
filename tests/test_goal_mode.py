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
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

try:
    from PIL import Image  # noqa: F401
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

from afk_supervisor.cli import build_arg_parser
from afk_supervisor.goal_engine import (
    GOAL_GUARDRAIL_FALLBACK,
    analyze_goal_pause,
    check_plan_status,
    extract_clean_goal,
    extract_clean_goal_with_reason,
    extract_goal_via_agy_agent,
    extract_recent_dialogue_summary,
    get_existing_thread_goal,
    resolve_stalled_goal_via_agy,
    run_goal_supervisor,
    sanitize_goal,
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
        self.run_dir = Path(self.tmp_dir.name).resolve()

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

    def test_review_and_followup_context_keeps_more_than_120_chars(self):
        rollout_file = self.run_dir / "test_long_context.jsonl"
        tail_marker = "关键改进建议完整保留标记"
        long_conclusion = "审查结论：" + ("A" * 180) + tail_marker
        events = [
            {"type": "response_item", "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": long_conclusion + "\n后续建议：落实验收闭环"}],
            }},
        ]
        rollout_file.write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8",
        )

        summary = extract_recent_dialogue_summary(rollout_file, max_turns=3)
        self.assertIn(tail_marker, summary)
        self.assertIn("后续建议：落实验收闭环", summary)

    def test_followup_survives_recent_turn_window(self):
        rollout_file = self.run_dir / "older_followup.jsonl"
        events = [{"type": "response_item", "payload": {
            "type": "message", "role": "assistant",
            "content": [{"text": "后续建议：补齐异常恢复回归测试并验收"}],
        }}]
        events.extend({"type": "event_msg", "payload": {
            "type": "user_message", "message": "继续",
        }} for _ in range(8))
        rollout_file.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        summary = extract_recent_dialogue_summary(rollout_file, max_turns=3)
        self.assertIn("补齐异常恢复回归测试并验收", summary)

    def test_goal_guardrail_blocks_micro_and_workspace_state_targets(self):
        candidates = (
            "提交 Git",
            "将当前改动提交 GitHub",
            "查看日志",
            "运行 pytest",
            "当前工作区状态：改动尚未提交 Git",
            "这个改动还没有提交 Git",
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                goal, reason = sanitize_goal(candidate)
                self.assertEqual(goal, GOAL_GUARDRAIL_FALLBACK)
                self.assertTrue(reason)

    def test_agy_micro_result_is_guarded(self):
        rollout_file = self.run_dir / "rollout_agy_micro.jsonl"
        rollout_file.write_text(json.dumps({
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "全面改进 Doloris 并完成验收"},
        }) + "\n", encoding="utf-8")
        with patch("afk_supervisor.goal_engine.extract_goal_via_agy_agent", return_value="提交 Git"):
            goal, reason = extract_clean_goal_with_reason(rollout_file, title="Doloris 改进")
        self.assertIn("全面改进 Doloris", goal)
        self.assertIn("测试和验收闭环", goal)
        self.assertIn("Git", reason)

    def test_fork_continue_recovers_parent_business_requirement(self):
        parent_id = "parent-session"
        child_id = "child-session"
        parent = self.run_dir / f"rollout-2026-09-23T10-00-00-{parent_id}.jsonl"
        child = self.run_dir / f"rollout-2026-09-23T11-00-00-{child_id}.jsonl"
        # 同名后代 fork 文件会继承父会话 ID，且修改时间更新，绝不能被误当父会话。
        descendant = self.run_dir / f"rollout-2026-09-23T12-00-00-{parent_id}_later-child.jsonl"
        parent.write_text(json.dumps({
            "type": "session_meta",
            "payload": {"id": parent_id, "cwd": str(self.run_dir)},
        }) + "\n" + json.dumps({
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "继续"},
        }) + "\n" + json.dumps({
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "全面审查并改进 Doloris，完成测试与验收闭环"},
        }) + "\n", encoding="utf-8")
        child.write_text(json.dumps({
            "type": "session_meta",
            "payload": {
                "session_id": child_id,
                "forked_from_id": parent_id,
            },
        }) + "\n" + json.dumps({
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "继续"},
        }) + "\n", encoding="utf-8")
        descendant.write_text(json.dumps({
            "type": "session_meta",
            "payload": {"id": parent_id, "forked_from_id": parent_id, "cwd": str(self.run_dir)},
        }) + "\n" + json.dumps({
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "继续"},
        }) + "\n", encoding="utf-8")

        with patch("afk_supervisor.goal_engine.extract_goal_via_agy_agent", return_value=None), \
             patch("afk_supervisor.sessions.discovery.get_codex_sessions_dir",
                   return_value=self.run_dir):
            goal = extract_clean_goal(child, title="继续线程工作")
        self.assertEqual(goal, "全面审查并改进 Doloris，完成测试与验收闭环")

    def test_fork_without_any_user_turn_traces_parent_business_requirement(self):
        """新建 fork 尚无任何用户回合时，也必须沿父会话回溯业务主线。"""
        parent_id = "parent-empty-child"
        child_id = "child-empty-child"
        parent = self.run_dir / f"rollout-2026-09-23T10-00-00-{parent_id}.jsonl"
        child = self.run_dir / f"rollout-2026-09-23T11-00-00-{child_id}.jsonl"
        parent.write_text(json.dumps({
            "type": "session_meta",
            "payload": {"id": parent_id, "cwd": str(self.run_dir)},
        }) + "\n" + json.dumps({
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "重构目标提炼策略并补齐回归测试与验收"},
        }) + "\n", encoding="utf-8")
        child.write_text(json.dumps({
            "type": "session_meta",
            "payload": {"session_id": child_id, "forked_from_id": parent_id},
        }) + "\n" + json.dumps({
            "type": "event_msg",
            "payload": {"type": "thread_settings_applied", "thread_id": child_id},
        }) + "\n", encoding="utf-8")

        with patch("afk_supervisor.goal_engine.extract_goal_via_agy_agent", return_value=None), \
             patch("afk_supervisor.sessions.discovery.get_codex_sessions_dir",
                   return_value=self.run_dir):
            goal = extract_clean_goal(child, title="继续线程工作")
            summary = extract_recent_dialogue_summary(child, max_turns=3)
        self.assertEqual(goal, "重构目标提炼策略并补齐回归测试与验收")
        self.assertIn("Business: 重构目标提炼策略并补齐回归测试与验收", summary)

    def test_fork_chain_walks_up_to_nearest_business_request(self):
        """父会话本身也是 fork 且只有“继续”时，继续向上回溯到最近的真实需求。"""
        root_id = "root-chain"
        mid_id = "mid-chain"
        child_id = "leaf-chain"
        root = self.run_dir / f"rollout-2026-09-23T08-00-00-{root_id}.jsonl"
        mid = self.run_dir / f"rollout-2026-09-23T09-00-00-{mid_id}.jsonl"
        leaf = self.run_dir / f"rollout-2026-09-23T10-00-00-{child_id}.jsonl"
        root.write_text(json.dumps({
            "type": "session_meta",
            "payload": {"id": root_id, "cwd": str(self.run_dir)},
        }) + "\n" + json.dumps({
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "完成 Doloris 全量审查与健壮性改进"},
        }) + "\n", encoding="utf-8")
        mid.write_text(json.dumps({
            "type": "session_meta",
            "payload": {"id": mid_id, "forked_from_id": root_id, "cwd": str(self.run_dir)},
        }) + "\n" + json.dumps({
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "继续"},
        }) + "\n", encoding="utf-8")
        leaf.write_text(json.dumps({
            "type": "session_meta",
            "payload": {"id": child_id, "forked_from_id": mid_id, "cwd": str(self.run_dir)},
        }) + "\n" + json.dumps({
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "继续"},
        }) + "\n", encoding="utf-8")

        with patch("afk_supervisor.goal_engine.extract_goal_via_agy_agent", return_value=None), \
             patch("afk_supervisor.sessions.discovery.get_codex_sessions_dir",
                   return_value=self.run_dir):
            goal = extract_clean_goal(leaf, title="继续线程工作")
        self.assertEqual(goal, "完成 Doloris 全量审查与健壮性改进")

    def test_continuation_phrase_alone_is_never_a_goal(self):
        for candidate in ("继续", "继续线程工作", "请继续", "Continue"):
            with self.subTest(candidate=candidate):
                goal, reason = sanitize_goal(candidate)
                self.assertEqual(goal, GOAL_GUARDRAIL_FALLBACK)
                self.assertIn("继续", reason)

    def test_continue_after_stage_review_uses_long_horizon_followup_goal(self):
        child = self.run_dir / "child_followup.jsonl"
        child.write_text(json.dumps({
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "## 后续建议\n补充回归测试并完成深度健壮性验收。"}],
            },
        }) + "\n" + json.dumps({
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "继续"},
        }) + "\n", encoding="utf-8")
        with patch("afk_supervisor.goal_engine.extract_goal_via_agy_agent", return_value=None):
            goal = extract_clean_goal(child, title="继续线程工作")
        self.assertIn("补充回归测试", goal)
        self.assertIn("验收闭环", goal)

    def test_completed_stage_fallback_prioritizes_unfinished_recommendations(self):
        rollout = self.run_dir / "completed_stage.jsonl"
        events = [
            {"type": "event_msg", "payload": {"type": "user_message", "message": "全面审查并改进 Doloris"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                "content": [{"text": "审查结论：阶段工作完成。\n后续建议：修复异常恢复路径并补齐回归测试。"}]}},
        ]
        rollout.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        with patch("afk_supervisor.goal_engine.extract_goal_via_agy_agent", return_value=None):
            goal = extract_clean_goal(rollout, title="Doloris 审查")
        self.assertIn("全面审查并改进 Doloris", goal)
        self.assertIn("修复异常恢复路径", goal)
        self.assertIn("验收闭环", goal)

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
        self.assertEqual(info["choice"], "继续")

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

    def test_explicit_micro_goal_is_guarded_and_audited(self):
        rollout_file = self.run_dir / "rollout_guardrail.jsonl"
        rollout_file.write_text("", encoding="utf-8")

        args = MagicMock()
        args.max_run_sec = 0

        def append_turn_complete(*a, **kw):
            with open(rollout_file, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "last_agent_message": "所有任务已完成"},
                }) + "\n")

        clock_values = iter([0, 0, 0, 0, 100])
        clock_now = 0.0

        def fake_monotonic():
            nonlocal clock_now
            try:
                clock_now = next(clock_values)
            except StopIteration:
                pass
            return clock_now

        with patch("afk_supervisor.goal_engine.find_best_codex_window", return_value=0), \
             patch("afk_supervisor.goal_engine.set_keep_awake"), \
             patch("time.monotonic", side_effect=fake_monotonic), \
             patch("time.sleep", side_effect=append_turn_complete):
            res = run_goal_supervisor(
                sid="sess-guardrail",
                rollout=rollout_file,
                scwd=str(self.run_dir),
                title="守护测试",
                args=args,
                run_dir=self.run_dir,
                goal_target="提交 Git",
            )

        self.assertEqual(res, 0)
        prompt = (self.run_dir / "goal_prompt.txt").read_text(encoding="utf-8").strip()
        self.assertTrue(prompt.startswith("/goal "))
        self.assertIn("守护测试", prompt)
        self.assertIn("验收闭环", prompt)
        records = [
            json.loads(line)
            for line in (self.run_dir / "interventions.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        guarded = [record for record in records if record.get("event") == "GOAL_GUARDRAIL"]
        self.assertEqual(len(guarded), 1)
        self.assertIn("守护测试", guarded[0]["fallback"])

    def test_empty_goal_never_starts_agy_or_injects_a_goal(self):
        rollout_file = self.run_dir / "rollout_empty_target.jsonl"
        rollout_file.write_text("", encoding="utf-8")
        args = MagicMock()
        with patch("afk_supervisor.goal_engine.set_keep_awake"), \
             patch("afk_supervisor.goal_engine.extract_clean_goal_with_reason") as extract, \
             patch("afk_supervisor.goal_engine.find_best_codex_window") as window, \
             patch("afk_supervisor.goal_engine.generate_final_report", return_value=self.run_dir / "report.md"), \
             patch("afk_supervisor.goal_engine.send_terminal_notification"):
            result = run_goal_supervisor(
                sid="sess-empty", rollout=rollout_file, scwd=str(self.run_dir),
                title="空目标", args=args, run_dir=self.run_dir, goal_target="",
            )
        self.assertNotEqual(result, 0)
        extract.assert_not_called()
        window.assert_not_called()
        self.assertFalse((self.run_dir / "goal_prompt.txt").exists())

    def test_explicit_lazy_goal_does_not_inject_title_when_agy_fails(self):
        rollout_file = self.run_dir / "rollout_agy_unavailable.jsonl"
        rollout_file.write_text("", encoding="utf-8")
        args = MagicMock()
        with patch("afk_supervisor.goal_engine.set_keep_awake"), \
             patch("afk_supervisor.goal_engine.extract_goal_via_agy_agent", return_value=None), \
             patch("afk_supervisor.goal_engine.find_best_codex_window") as window, \
             patch("afk_supervisor.goal_engine.generate_final_report", return_value=self.run_dir / "report.md"), \
             patch("afk_supervisor.goal_engine.send_terminal_notification"):
            result = run_goal_supervisor(
                sid="sess-agy-failed", rollout=rollout_file, scwd=str(self.run_dir),
                title="简单了解本项目doloris作为一个长任务托管系统，目前我们暂时只改进goal模式，首先启动goal模式托管时，若未设定",
                args=args, run_dir=self.run_dir, goal_target="__LAZY__",
            )
        self.assertNotEqual(result, 0)
        window.assert_not_called()
        self.assertFalse((self.run_dir / "goal_prompt.txt").exists())

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

    def test_already_set_goal_injects_long_horizon_directive(self):
        """目标已设立时不再跳过注入：必须下发长程托管续跑指令。

        旧行为 (直接 skip) 会让重构后的长程托管认知永远到不了 LLM，
        模型只看到自己上一轮的结论，把"提交 Git / 查看日志 / 跑单项测试"
        当成阶段目标，一条命令执行完就自动退出。
        """
        rollout_file = self.run_dir / "rollout_skip_inject.jsonl"
        with open(rollout_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "thread_goal_updated", "goal": {"status": "active", "objective": "已有任务"}}}) + "\n")

        args = MagicMock()
        args.max_run_sec = 10

        def append_done(*a, **kw):
            with open(rollout_file, "a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "所有任务已完成"}}) + "\n")

        with patch("afk_supervisor.goal_engine.find_best_codex_window", return_value=0), \
             patch("afk_supervisor.goal_engine.inject_into_codex_gui") as inject_mock, \
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
            # 没有前台窗口时不应尝试注入，但指令内容必须已落盘。
            inject_mock.assert_not_called()

            prompt_file = self.run_dir / "goal_prompt.txt"
            self.assertTrue(prompt_file.exists())
            prompt_text = prompt_file.read_text(encoding="utf-8")
            self.assertIn("长程托管续跑指令", prompt_text)
            self.assertIn("已有任务", prompt_text)
            # 已设立目标不得被 /goal 覆盖。
            self.assertFalse(prompt_text.lstrip().startswith("/goal"))
            # 反模式红线必须随指令一起下发。
            self.assertIn("提交", prompt_text)
            self.assertIn("单项测试", prompt_text)

            ivl_file = self.run_dir / "interventions.jsonl"
            records = [
                json.loads(line)
                for line in ivl_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            phase1_ev = [r for r in records if r.get("event") == "GOAL_PHASE" and r.get("phase") == 1]
            self.assertTrue(len(phase1_ev) >= 1)
            self.assertEqual(phase1_ev[0]["status"], "ALREADY_SET")
            started = [r for r in records if r.get("event") == "GOAL_STARTED"]
            self.assertEqual(started[0]["strategy"], "long_horizon_resume")

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
            inject_into_codex_gui("test text", target_hwnd=12345, target_sid="01a0c793-01bc-7c92-a3b6-0e735df4da1d", target_title="Test Title")
            mock_nav.assert_called_once_with("01a0c793-01bc-7c92-a3b6-0e735df4da1d")
    @unittest.skipUnless(HAS_PIL, "Pillow is required for mascot GUI tests")
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


    @unittest.skipUnless(HAS_PIL, "Pillow is required for mascot GUI tests")
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
             patch("tkinter.Toplevel") as mock_top, \
             patch("doloris_app.mascot.tk.Entry") as mock_entry, \
             patch("doloris_app.mascot.tk.Button") as mock_button, \
             patch("afk_supervisor.goal_engine.extract_goal_via_agy_agent") as mock_agy, \
             patch("doloris_app.mascot.threading.Thread") as mock_thread:
            mascot = DesktopMascot(root_mock, test_mode=True)
            mascot.start_mode = MagicMock()
            mascot.prompt_goal_mode("01a0c793-01bc-7c92-a3b6-0e735df4da1d")
            # 确认弹出了 Toplevel 窗口
            mock_top.assert_called_once()
            # 确认未跳过设定
            mascot.start_mode.assert_not_called()
            mock_top.return_value.after.assert_not_called()
            mock_agy.assert_not_called()

            commands = {call.kwargs.get("text"): call.kwargs.get("command")
                        for call in mock_button.call_args_list}
            mock_entry.return_value.get.return_value = ""
            commands["🚀 启动 Goal 模式"]()
            mascot.start_mode.assert_not_called()
            mock_thread.assert_not_called()
            mock_agy.assert_not_called()

            commands["🤖 懒人模式 (AGY提炼)"]()
            mock_thread.assert_called_once()
            mock_thread.return_value.start.assert_called_once()
            mock_agy.return_value = None
            mock_thread.call_args.kwargs["target"]()
            mock_top.return_value.after.call_args.args[1]()
            mock_entry.return_value.insert.assert_not_called()
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
            prompt = called_cmd[-1]
            self.assertIn("数小时无人值守", prompt)
            self.assertIn("提交 Git/commit", prompt)
            self.assertIn("后续建议", prompt)
            self.assertIn("fork 父会话", prompt)

    def test_lazy_goal_waits_for_agy_done_without_short_deadline(self):
        brain = self.run_dir / "slow_brain"
        transcript = brain / "slow-cid" / ".system_generated" / "logs" / "transcript.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({"type": "PLANNER_RESPONSE", "status": "DONE",
                                          "content": "旧目标"}) + "\n", encoding="utf-8")

        poll_count = [0]

        def finish_after_poll(_seconds):
            poll_count[0] += 1
            response = ({"type": "PLANNER_RESPONSE", "status": "DONE",
                         "content": "正在读取上下文", "tool_calls": [{"name": "read_file"}]}
                        if poll_count[0] == 1 else
                        {"type": "PLANNER_RESPONSE", "status": "DONE",
                         "content": "落实后续建议并完成健壮性验收"})
            with transcript.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(response) + "\n")

        response = MagicMock(returncode=0, stdout=b'{"response":{}}')
        with patch("afk_supervisor.goal_engine.get_agy_conversation_for_codex", return_value="slow-cid"), \
             patch("afk_supervisor.goal_engine.get_agy_brain_dir", return_value=brain), \
             patch("afk_supervisor.goal_engine.discover_antigravity_bridge",
                   return_value=("csrf", ["5555"], Path(sys.executable))), \
             patch("afk_supervisor.goal_engine.subprocess.run", return_value=response) as send, \
             patch("afk_supervisor.goal_engine.time.sleep", side_effect=finish_after_poll):
            goal = extract_clean_goal(
                None, title="长任务审查", codex_session_id="codex-slow",
                require_agy=True,
            )
        self.assertEqual(goal, "落实后续建议并完成健壮性验收")
        self.assertEqual(poll_count[0], 2)
        self.assertIsNone(send.call_args.kwargs["timeout"])

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




