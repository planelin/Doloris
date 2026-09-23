"""Transport regressions: fake all AGY/process boundaries; never contact live sessions."""

import copy
import hashlib
import json
import subprocess
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from afk_supervisor.l2 import transport
from afk_supervisor.l2.protocol import build_protocol_prompt, validate_protocol_payload
from afk_supervisor.models import AgyResponseResult, DeadlineBudget, EvidenceItem, L2Result
from tests.test_debug_regressions import DebugFixture
from tests.test_supervisor_loops import FakeClock


class TransportRegressions(DebugFixture):
    def setUp(self):
        super().setUp()
        (self.ws / "result.txt").write_text("verified output", encoding="utf-8")
        self.evidence = self.collect()
        self.evidence.request_id = "req-transport"
        self.clock = FakeClock()
        self.driver = SimpleNamespace(_agy_cid="fake-agy", _agy_port=1234, title="Debug", session_id="worker")
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(transport, "time", self.clock))
        self.stack.enter_context(patch.object(transport, "get_workspace_root", return_value=self.ws))
        self.stack.enter_context(patch.object(transport, "discover_antigravity_bridge", return_value=("fake-csrf", [1234], transport.Path(sys.executable))))
        self.stack.enter_context(patch.object(transport, "is_agy_working", return_value=(False, "idle")))
        self.idle = self.stack.enter_context(patch.object(transport, "wait_for_agy_idle", return_value=True))
        self.stack.enter_context(patch.object(transport, "check_agy_transcript_error", return_value=""))
        self.stack.enter_context(patch.object(transport, "get_skill_metadata", return_value=("test", "hash")))
        self.stack.enter_context(patch.object(transport.Path, "home", return_value=self.root))
        self.bridge = self.stack.enter_context(patch.object(transport.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=b'{"conversationId":"fake-agy"}', stderr=b"")))
        payload = self.payload(self.evidence)
        self.reader = self.stack.enter_context(patch("supervise.read_agy_latest_response", return_value=AgyResponseResult("PASS", json.dumps(payload), payload=payload)))

    def call_agy(self, context="current behavior output", **kwargs):
        full, short = build_protocol_prompt("REVIEW", self.baseline, self.evidence.request_id, self.evidence, context)
        result = transport.run_l2_antigravity(
            self.run, full, "LEGACY SHORT PROMPT WITHOUT EVIDENCE", 1, str(self.ws),
            conv_holder=self.driver, request_id=self.evidence.request_id, mode="REVIEW",
            task_baseline=self.baseline, evidence_packet=self.evidence, timeout_sec=7, **kwargs,
        )
        return result, full

    def dispatch(self, cmd="agy", **kwargs):
        return transport.l2_dispatch(
            cmd, self.args, None, self.run, self.driver, str(self.ws), 1,
            mode="REVIEW", task_baseline=self.baseline, evidence_packet=self.evidence,
            request_id=self.evidence.request_id, **kwargs,
        )

    def test_reused_session_receives_complete_current_packet_and_exact_archive(self):
        self.baseline.add_authorized_change("user", "Require the additional export")
        result, full = self.call_agy("CURRENT-OUTPUT-DETAILS")
        self.assertEqual(result.verdict, "PASS")
        args = self.bridge.call_args.args[0]
        self.assertEqual(args[1:4], ["agentapi", "send-message", "fake-agy"])
        sent = args[-1]
        self.assertEqual(sent, full)
        for expected in ("CURRENT-OUTPUT-DETAILS", "Require the additional export", self.evidence.items[0].id, self.evidence.reviewed_revision):
            self.assertIn(expected, sent)
        for name in ("request-req-transport.txt", "prompt-req-transport.txt", "prompt-req-transport-attempt-1.txt"):
            self.assertEqual((self.run / name).read_bytes(), sent.encode("utf-8"))
        self.assertTrue((self.run / "protocol-req-transport.json").is_file())

    def test_oversized_request_uses_hash_bound_complete_file_not_truncated_evidence(self):
        result, full = self.call_agy("完整证据" * 10000 + "END-OF-CURRENT-EVIDENCE")
        self.assertEqual(result.verdict, "PASS")
        sent = self.bridge.call_args.args[0][-1]
        request_file = self.run / "request-req-transport.txt"
        raw = request_file.read_bytes()
        self.assertEqual(raw, full.encode("utf-8"))
        self.assertIn("END-OF-CURRENT-EVIDENCE", raw.decode("utf-8"))
        self.assertIn(str(request_file.resolve()), sent)
        self.assertIn(hashlib.sha256(raw).hexdigest(), sent)
        self.assertIn(self.evidence.reviewed_revision, sent)
        self.assertLess(len(subprocess.list2cmdline([sent]).encode("utf-16-le")), 32000)
        self.assertEqual((self.run / "prompt-req-transport.txt").read_bytes(), sent.encode("utf-8"))

    def test_duplicate_request_id_cannot_overwrite_archived_request(self):
        self.call_agy("first request")
        request_file = self.run / "request-req-transport.txt"
        original = request_file.read_bytes()
        result, _ = self.call_agy("different request")
        self.assertEqual(result.verdict, "PROTOCOL_ERROR")
        self.assertEqual(request_file.read_bytes(), original)
        self.assertEqual(self.bridge.call_count, 1)

    def test_payloadless_legacy_review_cannot_crash_or_become_pass(self):
        self.reader.return_value = AgyResponseResult("PASS", "PASS", payload=None)
        result, _ = self.call_agy()
        self.assertEqual(result.verdict, "INCONCLUSIVE")
        self.assertIsNone(result.payload)

    def test_bridge_nonzero_exit_cannot_accept_any_response(self):
        self.bridge.return_value = SimpleNamespace(returncode=1, stdout=b"", stderr=b"bridge failed")
        result, _ = self.call_agy()
        self.assertEqual(result.verdict, "NO-VERDICT")
        self.reader.assert_not_called()

    def test_busy_wait_bridge_and_polling_share_one_deadline(self):
        start = self.clock.now
        self.reader.return_value = None
        with patch.object(transport, "is_agy_working", return_value=(True, "working")):
            self.idle.side_effect = lambda *a, **kw: (self.clock.sleep(5) or True)
            result, _ = self.call_agy()
        self.assertEqual(result.verdict, "NO-VERDICT")
        self.assertLessEqual(self.idle.call_args.kwargs["timeout_sec"], 7)
        self.assertLessEqual(self.bridge.call_args.kwargs["timeout"], 2)
        self.assertLessEqual(self.clock.now - start, 7)

    def test_dispatch_forwards_configured_agy_timeout(self):
        with patch.object(transport, "run_l2_antigravity", return_value=L2Result("NO-VERDICT", "", self.run / "l2.log")) as call:
            self.dispatch()
        self.assertEqual(call.call_args.kwargs["timeout_sec"], 7)

    def test_busy_session_never_creates_another_conversation(self):
        with patch.object(transport, "is_agy_working", return_value=(True, "working")):
            self.idle.return_value = False
            result, _ = self.call_agy()
        self.assertEqual(result.verdict, "NO-VERDICT")
        self.bridge.assert_not_called()
        self.assertEqual(self.driver._agy_cid, "fake-agy")

    def test_timeout_retry_polls_original_request_without_resending(self):
        response = self.reader.return_value
        self.reader.return_value = None
        first, _ = self.call_agy()
        self.assertEqual(first.verdict, "NO-VERDICT")
        self.assertTrue((self.run / "agy_pending_request.json").exists())
        self.reader.return_value = response
        with patch.object(transport, "is_agy_working", return_value=(True, "working")):
            second, _ = self.call_agy()
        self.assertEqual(second.verdict, "PASS")
        self.assertEqual(self.bridge.call_count, 1)
        self.assertFalse((self.run / "agy_pending_request.json").exists())

    def test_stale_session_is_not_replaced(self):
        self.bridge.return_value = SimpleNamespace(returncode=0, stdout=b'{"error":"conversation not found"}', stderr=b"")
        result, _ = self.call_agy()
        self.assertEqual(result.verdict, "NO-VERDICT")
        self.assertEqual(self.driver._agy_cid, "fake-agy")
        self.assertEqual(self.bridge.call_count, 1)

    def test_generic_dispatch_rejects_stale_structured_review(self):
        payload = self.payload(self.evidence)
        payload["reviewed_revision"] = "old-revision"
        with patch.object(transport, "run_l2_agent", return_value=L2Result("PASS", json.dumps(payload), self.run / "l2.log", payload=payload)):
            result = self.dispatch("fake-agent")
        self.assertEqual(result.verdict, "PROTOCOL_ERROR")

    def test_generic_dispatch_rejects_unstructured_pass(self):
        with patch.object(transport, "run_l2_agent", return_value=L2Result("PASS", "PASS", self.run / "l2.log")):
            result = self.dispatch("fake-agent")
        self.assertEqual(result.verdict, "INCONCLUSIVE")

    def test_generic_dispatch_honors_remaining_budget(self):
        budget = DeadlineBudget(3, self.clock)
        self.clock.sleep(1)
        with patch.object(transport, "run_l2_agent", return_value=L2Result("NO-VERDICT", "", self.run / "l2.log")) as call:
            self.dispatch("fake-agent", budget=budget)
        self.assertEqual(call.call_args.kwargs["timeout_sec"], 2)

    def test_timed_out_generic_process_never_accepts_partial_pass_and_closes_handles(self):
        handles = []
        def timeout(args, **kwargs):
            handles.extend((kwargs["stdin"], kwargs["stdout"]))
            kwargs["stdout"].write(b"PASS")
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        self.bridge.side_effect = timeout
        result = transport.run_l2_agent("fake-agent", self.run, "prompt", 2, None, timeout_sec=1)
        self.assertEqual(result.verdict, "NO-VERDICT")
        self.assertIsNone(result.payload)
        self.assertTrue(all(handle.closed for handle in handles))

    def test_failed_generic_process_never_accepts_printed_pass(self):
        def failure(args, **kwargs):
            kwargs["stdout"].write(b"PASS")
            return SimpleNamespace(returncode=1)
        self.bridge.side_effect = failure
        result = transport.run_l2_agent("fake-agent", self.run, "prompt", 2, None)
        self.assertEqual(result.verdict, "NO-VERDICT")
        self.assertIsNone(result.payload)

    def test_multi_port_fallback_on_connection_error(self):
        self.driver._agy_cid = None
        self.driver._agy_port = None
        ports_contacted = []
        def side_effect(cmd, **kwargs):
            addr = kwargs["env"].get("ANTIGRAVITY_LS_ADDRESS", "")
            port = addr.split(":")[-1]
            ports_contacted.append(port)
            if port == "62297":
                return SimpleNamespace(
                    returncode=1,
                    stdout=b'{"response":{},"error":"rpc error: code = Unavailable desc = connection error: desc = \\"error reading server preface: EOF\\""}',
                    stderr=b"",
                )
            return SimpleNamespace(returncode=0, stdout=b'{"conversationId":"new-agy-conv"}', stderr=b"")

        self.bridge.side_effect = side_effect
        with patch.object(transport, "discover_antigravity_bridge", return_value=("fake-csrf", ["62297", "62298"], transport.Path(sys.executable))):
            result, _ = self.call_agy()

        self.assertEqual(result.verdict, "PASS")
        self.assertEqual(ports_contacted, ["62297", "62298"])
        self.assertEqual(self.driver._agy_cid, "new-agy-conv")
        self.assertEqual(self.driver._agy_port, "62298")

    def test_failed_new_conversation_unlinks_pending_file(self):
        self.driver._agy_cid = None
        self.driver._agy_port = None
        self.bridge.return_value = SimpleNamespace(
            returncode=1,
            stdout=b'{"response":{},"error":"rpc error: code = Unavailable desc = connection error: desc = \\"error reading server preface: EOF\\""}',
            stderr=b"",
        )
        with patch.object(transport, "discover_antigravity_bridge", return_value=("fake-csrf", ["62297"], transport.Path(sys.executable))):
            result, _ = self.call_agy()

        self.assertEqual(result.verdict, "NO-VERDICT")
        self.assertFalse((self.run / "agy_pending_request.json").exists(), "新建会话失败后必须清理 pending 文件")


class GuiDeliveryRegressions(DebugFixture):
    """GUI 送达重试只允许改写目标窗口参数，且注入脚本必须在安装态可定位。"""

    def setUp(self):
        super().setUp()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("afk_supervisor.platform.gui._navigate_target"))
        self.stack.enter_context(patch("afk_supervisor.platform.gui.time.sleep"))
        self.probe = self.stack.enter_context(patch(
            "afk_supervisor.platform.gui.probe_codex_gui",
            return_value={"ok": True, "reason": "IDENTITY_OK", "identity_found": True}))

    def test_retry_rewrites_only_target_hwnd(self):
        from afk_supervisor.platform.gui import inject_into_codex_gui
        self.stack.enter_context(patch("afk_supervisor.platform.gui.find_best_codex_window", return_value=4242))
        not_sent = SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
            {"ok": False, "delivery_status": "NOT_SENT", "error": "Selected task identity could not be verified; no input sent"}))
        sent = SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
            {"ok": True, "delivery_status": "SENT", "method": "verified_task_enter"}))
        seen = []

        def fake_run(command, **kwargs):
            seen.append(list(command))
            return not_sent if len(seen) == 1 else sent

        with patch("subprocess.run", side_effect=fake_run):
            result = inject_into_codex_gui(
                "payload text", target_hwnd=1111,
                target_sid="01a0c793-01bc-7c92-a3b6-0e735df4da1d", target_title="Test Title")

        self.assertEqual(result.status, "SENT")
        self.assertEqual(len(seen), 2, "身份未对齐时必须且只能重试一次")
        first, retry = seen
        hwnd_index = first.index("-TargetHwnd") + 1
        self.assertEqual(first[hwnd_index], "1111")
        self.assertEqual(retry[hwnd_index], "4242")
        self.assertEqual(
            [(index, a, b) for index, (a, b) in enumerate(zip(first, retry)) if a != b],
            [(hwnd_index, "1111", "4242")],
            "重试只能改写 -TargetHwnd 的值，不得错位覆盖 payload 或其他参数",
        )
        self.assertTrue(retry[retry.index("-PayloadFile") + 1].endswith(".txt"))
        self.assertEqual(retry[retry.index("-TimeoutMs") + 1], "10000")

    def test_packaged_script_is_found_without_workspace_root_copy(self):
        from afk_supervisor.platform import gui
        with patch.object(gui, "get_workspace_root", return_value=self.root):
            found = gui.find_gui_inject_script()
        self.assertEqual(found, Path(gui.__file__).resolve().parent / "gui_inject.ps1")
        self.assertTrue(found.is_file(), "安装态必须能从包内定位注入脚本")

    def test_missing_script_reports_not_sent_without_spawning(self):
        from afk_supervisor.platform import gui
        with patch.object(gui, "find_gui_inject_script", return_value=None), \
             patch("subprocess.run") as run:
            result = gui.inject_into_codex_gui(
                "payload text", target_hwnd=1111,
                target_sid="01a0c793-01bc-7c92-a3b6-0e735df4da1d", target_title="Test Title")
        self.assertEqual(result.status, "NOT_SENT")
        run.assert_not_called()

    def test_identity_probe_runs_before_injection(self):
        from afk_supervisor.platform.gui import inject_into_codex_gui
        sent = SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
            {"ok": True, "delivery_status": "SENT", "method": "verified_task_enter"}))
        with patch("subprocess.run", return_value=sent) as run:
            result = inject_into_codex_gui(
                "payload text", target_hwnd=1111,
                target_sid="01a0c793-01bc-7c92-a3b6-0e735df4da1d", target_title="Test Title")
        self.assertEqual(result.status, "SENT")
        self.assertEqual(self.probe.call_count, 1, "每次注入前都必须先做只读身份探针")
        self.assertEqual(self.probe.call_args.kwargs.get("target_hwnd"), 1111)
        self.assertEqual(run.call_count, 1, "探针结论可用时注入脚本只跑一次")

    def test_permission_denied_probe_blocks_send_with_readable_reason(self):
        from afk_supervisor.platform.gui import inject_into_codex_gui
        self.probe.return_value = {
            "ok": False, "reason": "UIA_ACCESS_DENIED", "proc_open_error": 5,
            "error": "无法读取桌面端进程(错误 5=拒绝访问)",
        }
        with patch("subprocess.run") as run:
            result = inject_into_codex_gui(
                "payload text", target_hwnd=1111,
                target_sid="01a0c793-01bc-7c92-a3b6-0e735df4da1d", target_title="Test Title")
        self.assertEqual(result.status, "NOT_SENT", "权限不足属于环境性失败，不得伪造在途指令")
        self.assertIn("UIA_ACCESS_DENIED", result.detail)
        self.assertIn("更高权限", result.detail, "回执必须给出可执行的处置建议")
        run.assert_not_called()

    def test_not_focused_probe_keeps_navigation_retry_path(self):
        from afk_supervisor.platform.gui import inject_into_codex_gui
        self.probe.return_value = {"ok": False, "reason": "TARGET_NOT_FOCUSED"}
        not_sent = SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
            {"ok": False, "delivery_status": "NOT_SENT", "error": "Selected task identity could not be verified; no input sent"}))
        sent = SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
            {"ok": True, "delivery_status": "SENT", "method": "verified_task_enter"}))
        with patch("subprocess.run", side_effect=[not_sent, sent]):
            result = inject_into_codex_gui(
                "payload text", target_hwnd=1111,
                target_sid="01a0c793-01bc-7c92-a3b6-0e735df4da1d", target_title="Test Title")
        self.assertEqual(result.status, "SENT", "未聚焦属于可纠正状态，必须保留导航重试")


class GuiProbeRegressions(DebugFixture):
    """只读身份探针：命令形状、机读码解析与阻断策略。"""

    def test_probe_command_is_read_only(self):
        from afk_supervisor.platform import gui
        payload = json.dumps({"ok": True, "identity_found": True, "reason": "IDENTITY_OK"})
        with patch.object(gui, "get_workspace_root", return_value=self.root), \
             patch("subprocess.run", return_value=SimpleNamespace(
                 returncode=0, stderr="", stdout=payload)) as run:
            probe = gui._probe_codex_gui_once(4242, "01a0c793-01bc-7c92-a3b6-0e735df4da1d", "Test Title", 12000)
        self.assertEqual(probe.get("reason"), "IDENTITY_OK")
        command = run.call_args.args[0]
        self.assertIn("-ProbeOnly", command)
        self.assertNotIn("-PayloadFile", command, "探针不得携带指令载荷，必须是只读自检")
        self.assertEqual(command[command.index("-TargetHwnd") + 1], "4242")

    def test_probe_stdout_noise_is_tolerated(self):
        from afk_supervisor.platform.gui import parse_codex_gui_probe
        stdout = "WARNING: legacy banner\n" + json.dumps({"ok": True, "reason": "IDENTITY_OK"}) + "\n"
        self.assertEqual(parse_codex_gui_probe(stdout).get("reason"), "IDENTITY_OK")
        self.assertEqual(parse_codex_gui_probe(""), {})
        self.assertEqual(parse_codex_gui_probe("not json at all"), {})

    def test_probe_blocker_only_stops_on_environmental_failures(self):
        from afk_supervisor.platform.gui import gui_probe_blocker
        self.assertEqual(gui_probe_blocker({}), "", "缺少 reason 的旧脚本回执必须放行")
        self.assertEqual(gui_probe_blocker({"reason": "IDENTITY_OK"}), "")
        self.assertEqual(gui_probe_blocker({"reason": "TARGET_NOT_FOCUSED"}), "")
        denied = gui_probe_blocker({"reason": "UIA_ACCESS_DENIED", "proc_open_error": 5})
        self.assertIn("UIA_ACCESS_DENIED", denied)
        self.assertIn("更高权限", denied)
        self.assertIn("UIA_TREE_UNAVAILABLE", gui_probe_blocker({"reason": "UIA_TREE_UNAVAILABLE"}))

    def test_probe_wrapper_folds_exceptions_into_probable_reason(self):
        from afk_supervisor.platform import gui
        with patch.object(gui, "_probe_codex_gui_once", side_effect=OSError("powershell missing")):
            probe = gui.probe_codex_gui(target_hwnd=1, target_sid="s", target_title="t")
        self.assertEqual(probe.get("reason"), "PROBE_ERROR")
        self.assertIn("powershell missing", probe.get("error", ""))

    def test_probe_receipt_is_decoded_from_bytes_under_any_console_codepage(self):
        """实测故障: PS 5.1 被重定向时按控制台代码页写字节, 中文结论整段变成乱码。"""
        from afk_supervisor.platform import gui
        detail = "无法获取进程句柄(错误 5=拒绝访问)"
        payload = json.dumps({"ok": False, "identity_found": False, "reason": "UIA_ACCESS_DENIED",
                              "proc_open_error": 5, "error": detail}, ensure_ascii=False)
        for codec in ("utf-8", "cp936"):
            with self.subTest(codec=codec):
                with patch.object(gui, "get_workspace_root", return_value=self.root), \
                     patch("subprocess.run", return_value=SimpleNamespace(
                         returncode=0, stderr=b"", stdout=payload.encode(codec))):
                    probe = gui._probe_codex_gui_once(4242, "01a0c793-01bc-7c92-a3b6-0e735df4da1d", "Test Title", 12000)
                self.assertEqual(probe.get("reason"), "UIA_ACCESS_DENIED")
                self.assertEqual(probe.get("error"), detail, f"{codec} 回执不得变成乱码")
                self.assertNotIn("\ufffd", probe.get("error", ""))

    def test_decode_helper_tolerates_str_and_empty_stdout(self):
        from afk_supervisor.platform.gui import decode_powershell_output
        self.assertEqual(decode_powershell_output(None), "")
        self.assertEqual(decode_powershell_output("plain"), "plain", "已解码文本必须原样透传")
        self.assertEqual(decode_powershell_output("身份".encode("utf-8")), "身份")


class FunctionalEvidenceRegressions(DebugFixture):
    def setUp(self):
        super().setUp()
        (self.ws / "result.txt").write_text("result", encoding="utf-8")
        self.baseline.required_criteria = [{"id": "behavior", "type": "functional"}]
        self.evidence = self.collect()
        self.item = EvidenceItem(
            "ev_behavior", "verification_result", "Test fixture, not an actual application test run",
            verification_kind="unit_test", status="PASS",
            artifact_revision=self.evidence.artifact_revision, criterion_ids=["behavior"],
        )
        self.evidence.items.append(self.item)
        self.review = self.payload(self.evidence)
        self.review["criteria"] = [{"id": "behavior", "verdict": "PASS", "evidence_ids": [self.item.id], "reason": "Matching execution evidence"}]

    def validate(self):
        return validate_protocol_payload(self.review, task_baseline=self.baseline, evidence_packet=self.evidence)

    def test_current_execution_evidence_can_pass_corresponding_functional_criterion(self):
        ok, reason = self.validate()
        self.assertTrue(ok, reason)

    def test_stale_execution_cannot_pass(self):
        self.item.artifact_revision = "art-previous"
        self.assertFalse(self.validate()[0])

    def test_failed_or_unknown_execution_cannot_pass(self):
        for status in ("FAIL", "UNKNOWN", ""):
            with self.subTest(status=status):
                self.item.status = status
                self.assertFalse(self.validate()[0])

    def test_execution_for_a_different_criterion_cannot_pass(self):
        self.item.criterion_ids = ["other-behavior"]
        self.assertFalse(self.validate()[0])

    def test_unbound_or_nonbehavioral_results_cannot_pass(self):
        for changes in ({"artifact_revision": ""}, {"verification_kind": "syntax"}, {"category": "worker_statement"}):
            with self.subTest(changes=changes):
                self.evidence.items[-1] = copy.deepcopy(self.item)
                for key, value in changes.items():
                    setattr(self.evidence.items[-1], key, value)
                self.assertFalse(self.validate()[0])

    def test_duplicate_evidence_ids_cannot_silently_override_failure(self):
        failed = copy.deepcopy(self.item)
        failed.status = "FAIL"
        self.evidence.items.insert(0, failed)
        self.assertFalse(self.validate()[0])


if __name__ == "__main__":
    unittest.main()
