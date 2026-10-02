"""AGY 忙碌检测的失败安全回归测试。

历史缺陷: is_agy_working 在转录末行 JSON 损坏 (AGY 流式写盘途中读取) 时抛
json.JSONDecodeError, 被外层 except 兜底成 (False, "检测异常") —— 即"空闲",
导致监管层向正在工作中的 AGY 会话注入新消息。修复后, 只要"转录存在但无法
确认空闲", 一律按工作中处理。
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from afk_supervisor.l2 import bridge


class AgyBusyDetectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="afk-busy-test-")
        self.addCleanup(self.temporary.cleanup)
        self.brain = Path(self.temporary.name) / "brain"
        self.cid = "cid-fake"
        self.log_dir = self.brain / self.cid / ".system_generated" / "logs"
        self.log_dir.mkdir(parents=True)
        self.transcript = self.log_dir / "transcript.jsonl"

    def write_transcript(self, *lines, age_sec=10.0):
        self.transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
        past = time.time() - age_sec
        import os
        os.utime(self.transcript, (past, past))

    def detect(self):
        with patch.object(bridge, "get_agy_brain_dir", return_value=self.brain):
            return bridge.is_agy_working(self.cid)

    def test_missing_transcript_is_idle_for_new_session(self):
        working, reason = self.detect()
        self.assertFalse(working)
        self.assertIn("不存在", reason)

    def test_done_planner_response_is_idle(self):
        self.write_transcript(json.dumps({"type": "PLANNER_RESPONSE", "status": "DONE",
                                          "content": "决策完成", "tool_calls": []}))
        working, _ = self.detect()
        self.assertFalse(working)

    def test_recent_write_is_working(self):
        self.write_transcript(json.dumps({"type": "PLANNER_RESPONSE", "status": "DONE",
                                          "content": "决策完成", "tool_calls": []}), age_sec=0.0)
        working, reason = self.detect()
        self.assertTrue(working)
        self.assertIn("活跃写盘", reason)

    def test_truncated_last_line_must_not_look_idle(self):
        """末行被截断 (流式写盘) 时必须判"工作中", 绝不能判空闲。"""
        self.write_transcript(
            json.dumps({"type": "PLANNER_RESPONSE", "status": "DONE", "content": "上一回合完成"}),
            '{"type":"PLANNER_RESPONSE","status":"RUN","content":"未写完的半行',
        )
        working, reason = self.detect()
        self.assertTrue(working, f"截断末行被误判为空闲: {reason}")
        self.assertIn("损坏", reason)

    def test_running_status_is_working(self):
        self.write_transcript(json.dumps({"type": "PLANNER_RESPONSE", "status": "RUNNING",
                                          "tool_calls": ["call-1"]}))
        working, _ = self.detect()
        self.assertTrue(working)

    def test_unreadable_transcript_must_not_look_idle(self):
        """转录存在但读取失败 (如被写方锁住) 时必须按"工作中"处理。"""
        self.write_transcript(json.dumps({"type": "PLANNER_RESPONSE", "status": "DONE",
                                          "content": "上一回合完成"}))
        with patch.object(bridge, "get_agy_brain_dir", return_value=self.brain), \
                patch.object(Path, "read_text", side_effect=OSError("locked by writer")):
            working, reason = bridge.is_agy_working(self.cid)
        self.assertTrue(working, f"转录不可读被误判为空闲: {reason}")
        self.assertIn("视为工作中", reason)


if __name__ == "__main__":
    unittest.main()
