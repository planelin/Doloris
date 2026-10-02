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


class ResponsePositionBindingTests(unittest.TestCase):
    """read_agy_latest_response 的旧轮响应拒绝 (position binding)。

    历史缺陷: 只要窗口内存在携带 request_id 的 USER_INPUT, 任何 DONE
    PLANNER_RESPONSE 都会被采信 —— 包括上一轮迟到的回答。修复后只有
    出现在我们输入行之后的响应才有效, 且非协议裁决必须回带 request_id。
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="afk-pos-test-")
        self.addCleanup(self.temporary.cleanup)
        self.brain = Path(self.temporary.name) / "brain"
        self.cid = "cid-pos"
        log_dir = self.brain / self.cid / ".system_generated" / "logs"
        log_dir.mkdir(parents=True)
        self.transcript = log_dir / "transcript.jsonl"

    def write(self, *objs):
        self.transcript.write_text(
            "".join(json.dumps(o) + "\n" for o in objs), encoding="utf-8")
        import os
        past = time.time() - 30
        os.utime(self.transcript, (past, past))

    def read(self, request_id="req-cur", min_line_idx=0):
        with patch.object(bridge, "get_agy_brain_dir", return_value=self.brain):
            return bridge.read_agy_latest_response(self.cid, min_line_idx=min_line_idx, request_id=request_id)

    def test_late_previous_round_answer_before_our_input_is_rejected(self):
        """上一轮迟到的 DONE 回答位于我们的 USER_INPUT 之前: 绝不可采信。"""
        self.write(
            {"type": "PLANNER_RESPONSE", "status": "DONE", "content": "旧轮 PASS 裁决文本"},
            {"type": "USER_INPUT", "status": "DONE", "content": "【本次请求ID: req-cur】新请求"},
        )
        self.assertIsNone(self.read(), "输入行之前的响应属于旧轮, 不可采信")

    def test_response_after_our_input_is_accepted(self):
        self.write(
            {"type": "USER_INPUT", "status": "DONE", "content": "【本次请求ID: req-cur】新请求"},
            {"type": "PLANNER_RESPONSE", "status": "DONE", "content": "已按指令完成本轮审查: PASS (req-cur)"},
        )
        result = self.read()
        self.assertIsNotNone(result)

    def test_system_message_injection_counts_as_our_input_marker(self):
        """send-message 注入以 SYSTEM_MESSAGE 落盘 (实测 2026-10), 同样构成输入边界。"""
        self.write(
            {"type": "PLANNER_RESPONSE", "status": "DONE", "content": "旧轮裁决"},
            {"type": "SYSTEM_MESSAGE", "status": "DONE", "content": "【本次请求ID: req-cur】注入请求"},
        )
        self.assertIsNone(self.read())

    def test_nonprotocol_response_echo_required_only_in_fallback_mode(self):
        """无法定位输入行 (回退模式) 时, 非协议裁决必须回带 request_id;
        位置绑定成功时纯文本裁决无需回声 (位置即信任边界)。"""
        # 回退模式: 窗口内没有我们的输入行
        self.write({"type": "PLANNER_RESPONSE", "status": "DONE", "content": "PASS"})
        self.assertIsNone(self.read(), "回退模式下无回声的非协议裁决不可采信")
        self.write({"type": "PLANNER_RESPONSE", "status": "DONE", "content": "PASS (req-cur)"})
        self.assertIsNotNone(self.read(), "带回声的非协议裁决应被接受")
        # 位置绑定模式: 响应在输入行之后, 纯文本 PASS 无需回声
        self.write(
            {"type": "USER_INPUT", "status": "DONE", "content": "【本次请求ID: req-cur】新请求"},
            {"type": "PLANNER_RESPONSE", "status": "DONE", "content": "PASS"},
        )
        self.assertIsNotNone(self.read(), "位置绑定成功后纯文本裁决无需回声")

    def test_protocol_response_after_input_accepted_even_without_echo(self):
        """协议载荷即使未回声也可接受 (下游有 expected_request_id 硬校验兜底)。"""
        payload = {"protocol": "afk_agy_protocol_v1", "request_id": "req-other",
                   "task_id": "t", "mode": "REVIEW", "reviewed_revision": "r",
                   "verdict": "PASS", "criteria": [], "blockers": [],
                   "next_action": {"type": "terminate_success", "instructions": ""}, "repairs": []}
        self.write(
            {"type": "USER_INPUT", "status": "DONE", "content": "【本次请求ID: req-cur】新请求"},
            {"type": "PLANNER_RESPONSE", "status": "DONE", "content": "```json\n" + json.dumps(payload) + "\n```"},
        )
        self.assertIsNotNone(self.read())


if __name__ == "__main__":
    unittest.main()
