"""Regression tests for lazy goal resolution in the desktop mascot."""

import unittest
from pathlib import Path
from unittest.mock import patch

from afk_supervisor.goal_engine import GoalExtractionError
from doloris_app.mascot import resolve_lazy_goal


class LazyGoalResolutionTests(unittest.TestCase):
    def test_prefers_agy_result_from_shared_goal_engine(self):
        rollout = Path("rollout.jsonl")
        with patch(
            "afk_supervisor.goal_engine.extract_goal_via_agy_agent",
            return_value="优化数据库索引并补充测试",
        ) as agy:
            goal = resolve_lazy_goal(
                rollout,
                title="优化数据库",
                codex_session_id="session-123",
            )

        self.assertEqual(goal, "优化数据库索引并补充测试")
        agy.assert_called_once_with(
            rollout,
            title="优化数据库",
            agy_mgr=None,
            run_dir=None,
            timeout_sec=None,
            codex_session_id="session-123",
            cancel_event=None,
        )

    def test_agy_failure_does_not_put_session_title_in_goal_field(self):
        with patch("afk_supervisor.goal_engine.extract_goal_via_agy_agent", return_value=None):
            with self.assertRaises(GoalExtractionError):
                resolve_lazy_goal(None, title="简单了解本项目doloris作为一个长任务托管系统，目前我们暂时只改进goal模式，首先启动goal模式托管时，若未设定")

    def test_empty_goal_engine_result_is_an_error(self):
        with patch("afk_supervisor.goal_engine.extract_clean_goal", return_value=""):
            with self.assertRaises(GoalExtractionError):
                resolve_lazy_goal(None)


if __name__ == "__main__":
    unittest.main()
