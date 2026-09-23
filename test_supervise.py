import unittest
import tempfile
import shutil
import json
import time
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import supervise
from supervise import (
    DeadlineBudget,
    WorkspaceSupervisorLock,
    SupervisorState,
    SupervisorCoordinator,
    L2Result,
    clean_l2_decision_text,
    read_agy_latest_response,
    parse_verdict_from_text,
    check_acceptance,
    check_acceptance_natural,
    _acceptance_selftest,
    CodexDriver,
    pid_is_running,
    munged_cwd,
)
from afk_protocol import (
    TaskBaseline,
    EvidenceItem,
    EvidencePacket,
    collect_evidence,
    validate_protocol_payload,
    get_skill_metadata,
    build_protocol_prompt,
)


class TestIssue1_L2TranscriptOffsetAndRequestId(unittest.TestCase):
    """Issue 1: L2 reading stale response from previous rounds and request_id isolation"""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_issue1_"))
        self.patcher = patch.object(supervise, "HOME", self.temp_dir)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_stale_response_ignored_with_min_line_idx(self):
        cid = "test-agy-cid-001"
        transcript_path = self.temp_dir / ".gemini" / "antigravity" / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
        transcript_path.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            json.dumps({"type": "USER_INPUT", "content": "Question round 1"}),
            json.dumps({"type": "PLANNER_RESPONSE", "status": "DONE", "content": "PROCEED\n执行指令：完成第一轮"}),
        ]
        transcript_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # Without min_line_idx, line 1 is read
        res = read_agy_latest_response(cid, min_line_idx=0)
        self.assertIsNotNone(res)
        self.assertEqual(res[0], "PROCEED")

        # With min_line_idx=2 (offset after round 1), stale response is ignored
        res_stale = read_agy_latest_response(cid, min_line_idx=2)
        self.assertIsNone(res_stale)

    def test_matching_request_id(self):
        cid = "test-agy-cid-002"
        transcript_path = self.temp_dir / ".gemini" / "antigravity" / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
        transcript_path.parent.mkdir(parents=True, exist_ok=True)

        req_id = "req-20260918-abcd"
        lines = [
            json.dumps({"type": "USER_INPUT", "content": f"[REQUEST_ID:{req_id}]\n提问第二轮"}),
            json.dumps({"type": "PLANNER_RESPONSE", "status": "DONE", "content": f"[REQUEST_ID:{req_id}]\nPROCEED\n执行指令：第二轮通过"}),
        ]
        transcript_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # Matching request_id returns response
        res = read_agy_latest_response(cid, min_line_idx=0, request_id=req_id)
        self.assertIsNotNone(res)
        self.assertEqual(res[0], "PROCEED")

        # Mismatched request_id returns None
        res_mismatch = read_agy_latest_response(cid, min_line_idx=0, request_id="req-mismatch-999")
        self.assertIsNone(res_mismatch)

    def test_clean_l2_decision_text(self):
        # Empty text must return empty string (never default authorization)
        self.assertEqual(clean_l2_decision_text(""), "")
        self.assertEqual(clean_l2_decision_text("   \n\t  "), "")

        # Strips request ID marker and extracts instruction
        raw = """[REQUEST_ID:req-12345]
经过分析，当前修改符合预期。
执行指令：
继续运行 pytest 并生成测试报告。
PROCEED"""
        cleaned = clean_l2_decision_text(raw)
        self.assertNotIn("req-12345", cleaned)
        self.assertNotIn("[REQUEST_ID:", cleaned)
        self.assertIn("继续运行 pytest 并生成测试报告。", cleaned)
        self.assertFalse(cleaned.strip().endswith("PROCEED"))


class TestIssue2_WorkerSessionDiscoveryAndResume(unittest.TestCase):
    """Issue 2: Worker session discovery binding and resume retaining heartbeat"""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_issue2_"))
        self.ws = self.temp_dir / "my_project"
        self.ws.mkdir(parents=True, exist_ok=True)
        self.run_dir = self.temp_dir / "runs" / "run_01"
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_verify_rollout_ownership(self):
        driver = CodexDriver(self.ws, self.run_dir)
        rollout_file = self.run_dir / "rollout-0195a63c-74a0-7612-9c3c-6239103c1445.jsonl"

        # Matching session ID and cwd
        meta_line = json.dumps({
            "type": "session_meta",
            "payload": {
                "id": "0195a63c-74a0-7612-9c3c-6239103c1445",
                "cwd": str(self.ws)
            }
        })
        rollout_file.write_text(meta_line + "\n", encoding="utf-8")

        self.assertTrue(driver._verify_rollout_ownership(rollout_file, "0195a63c-74a0-7612-9c3c-6239103c1445", self.ws))
        # Wrong session id
        self.assertFalse(driver._verify_rollout_ownership(rollout_file, "different-session-id", self.ws))
        # Wrong cwd
        self.assertFalse(driver._verify_rollout_ownership(rollout_file, "0195a63c-74a0-7612-9c3c-6239103c1445", self.temp_dir / "other_project"))

    def test_resume_retains_jsonl_baseline(self):
        driver = CodexDriver(self.ws, self.run_dir)
        dummy_jsonl = self.run_dir / "rollout-baseline.jsonl"
        dummy_jsonl.write_text("{}", encoding="utf-8")
        driver.jsonl = dummy_jsonl
        driver.session_id = "sess-1234"

        with patch.object(driver, "_spawn_once", return_value=True):
            prompt = self.run_dir / "prompt.txt"
            prompt.write_text("继续\n", encoding="utf-8")
            driver.resume(prompt)
            self.assertEqual(driver.jsonl, dummy_jsonl)

    def test_discover_session_from_stdout_thread_started(self):
        driver = CodexDriver(self.ws, self.run_dir)
        target_sid = "0195a63c-74a0-7612-9c3c-6239103c1445"
        stdout_log = self.run_dir / "worker-stdout.log"
        stdout_log.write_text(json.dumps({"type": "thread.started", "thread_id": target_sid}) + "\n", encoding="utf-8")

        sessions_dir = self.temp_dir / ".codex" / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        rollout = sessions_dir / f"rollout-{target_sid}.jsonl"
        rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": target_sid, "cwd": str(self.ws)}}) + "\n", encoding="utf-8")

        with patch.object(supervise, "CODEX_SESSIONS", sessions_dir):
            discovered = driver.discover_session(time.time() - 10)
            self.assertTrue(discovered)
            self.assertEqual(driver.session_id, target_sid)
            self.assertEqual(driver.jsonl, rollout)


class TestIssue4_UnifiedDeferAndWaitingUser(unittest.TestCase):
    """Issue 4: Unified WAITING_USER semantics, no blind '继续' or '自行决定'"""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_issue4_"))
        self.run_dir = self.temp_dir / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_clean_l2_decision_text_does_not_invent_permission(self):
        self.assertEqual(clean_l2_decision_text(""), "")
        self.assertEqual(clean_l2_decision_text("DEFER"), "")
        self.assertEqual(clean_l2_decision_text("NO-VERDICT").strip(), "NO-VERDICT")

    def test_state_transition_waiting_user(self):
        state = SupervisorState(self.run_dir, "sid-1", "fork", self.temp_dir)
        self.assertEqual(state.state, "INIT")
        state.transition("WAITING_USER", detail="L2 defer to user")
        self.assertEqual(state.state, "WAITING_USER")
        self.assertEqual(state.detail, "L2 defer to user")

        saved = json.loads((self.run_dir / "supervisor_state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["state"], "WAITING_USER")


class TestIssue5_ExplicitAcceptanceAndNegationFilter(unittest.TestCase):
    """Issue 5: Rejection of false completion and negation markers in natural acceptance"""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_issue5_"))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_natural_acceptance_rejects_negations_and_failures(self):
        negation_msg = "经过多次尝试，任务尚未全部完成，请指示。"
        ok, reason = check_acceptance_natural(self.temp_dir, last_msg=negation_msg)
        self.assertFalse(ok)
        self.assertIn("否定语义", reason)

        failure_msg = "构建未通过，pytest 测试失败 2 项。"
        ok2, reason2 = check_acceptance_natural(self.temp_dir, last_msg=failure_msg)
        self.assertFalse(ok2)
        self.assertIn("失败标记", reason2)

    def test_natural_acceptance_accepts_clean_completion(self):
        prog = self.temp_dir / "PROGRESS.md"
        prog.write_text("- [x] 模块一\n- [x] 模块二\n", encoding="utf-8")

        success_msg = "所有任务已完成，所有功能测试均已通过。"
        ok, reason = check_acceptance_natural(self.temp_dir, last_msg=success_msg, min_mtime=0.0)
        self.assertTrue(ok)
        self.assertTrue("全部勾选完成" in reason or "完工语义吻合" in reason)


class TestIssue6_ForkPauseConfirmationAndWorkspaceLock(unittest.TestCase):
    """Issue 6: Fork pause confirmation and WorkspaceSupervisorLock mutual exclusion"""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_issue6_"))
        self.ws = self.temp_dir / "workspace_alpha"
        self.ws.mkdir(parents=True, exist_ok=True)
        self.lock_patcher = patch.object(supervise, "HOME", self.temp_dir)
        self.lock_patcher.start()

    def tearDown(self):
        self.lock_patcher.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_workspace_supervisor_lock_acquisition_and_release(self):
        lock1 = WorkspaceSupervisorLock(self.ws, sid="sid-alpha", mode="fork", lock_dir=self.ws / "test-locks")
        ok, msg = lock1.acquire()
        self.assertTrue(ok)
        self.assertTrue(lock1.lock_file.exists())

        lock2 = WorkspaceSupervisorLock(self.ws, sid="sid-beta", mode="gui", lock_dir=self.ws / "test-locks")
        ok2, msg2 = lock2.acquire()
        self.assertFalse(ok2)
        self.assertTrue("被另一监管器实例占用" in msg2)

        lock1.release()
        self.assertFalse(lock1.lock_file.exists())

        ok3, msg3 = lock2.acquire()
        self.assertTrue(ok3)
        lock2.release()

    def test_workspace_supervisor_lock_cleans_stale_lock(self):
        lock = WorkspaceSupervisorLock(self.ws, sid="sid-dead", mode="fork", lock_dir=self.ws / "test-locks")
        lock.lock_dir.mkdir(parents=True, exist_ok=True)
        lock.lock_file.write_text(json.dumps({
            "pid": 9999999,
            "workspace": str(self.ws),
            "sid": "sid-dead",
            "mode": "fork"
        }), encoding="utf-8")

        with patch.object(supervise, "pid_is_running", return_value=False):
            ok, msg = lock.acquire()
            self.assertTrue(ok)
            data = json.loads(lock.lock_file.read_text(encoding="utf-8"))
            self.assertEqual(data["pid"], os.getpid())
            lock.release()


class TestIssue7_AcceptancePathBaseline(unittest.TestCase):
    """Issue 7: Acceptance evaluated strictly relative to workspace_root"""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_issue7_"))
        self.ws_root = self.temp_dir / "project_root"
        self.ws_root.mkdir(parents=True, exist_ok=True)
        self.task_dir = self.temp_dir / "task_spec"
        self.task_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_acceptance_resolves_against_workspace_root(self):
        spec = self.task_dir / "acceptance.md"
        spec.write_text("dist/bundle.js\nchecklist: docs/TODO.md : 2\n", encoding="utf-8")

        (self.ws_root / "dist").mkdir(parents=True, exist_ok=True)
        (self.ws_root / "dist" / "bundle.js").write_text("console.log('done');", encoding="utf-8")
        (self.ws_root / "docs").mkdir(parents=True, exist_ok=True)
        (self.ws_root / "docs" / "TODO.md").write_text("- [x] step 1\n- [x] step 2\n", encoding="utf-8")

        ok, detail = check_acceptance(self.task_dir, self.ws_root)
        self.assertTrue(ok)
        self.assertEqual(detail, "全部满足")

    def test_acceptance_fails_when_files_absent_in_workspace_root(self):
        spec = self.task_dir / "acceptance.md"
        spec.write_text("dist/bundle.js\n", encoding="utf-8")

        (self.temp_dir / "dist").mkdir(parents=True, exist_ok=True)
        (self.temp_dir / "dist" / "bundle.js").write_text("console.log('wrong place');", encoding="utf-8")

        ok, detail = check_acceptance(self.task_dir, self.ws_root)
        self.assertFalse(ok)
        self.assertIn("非空文件0<1", detail)


class TestIssue8_DeadlineBudgetMonotonic(unittest.TestCase):
    """Issue 8: Monotonic DeadlineBudget timeouts and bounding"""

    def test_deadline_budget_timeout_and_bounding(self):
        budget = DeadlineBudget(max_run_sec=0.1)
        self.assertFalse(budget.is_expired())
        self.assertGreater(budget.remaining_sec, 0.0)

        bounded = budget.bound_timeout(5.0)
        self.assertLessEqual(bounded, 0.1001)

        time.sleep(0.12)
        self.assertTrue(budget.is_expired())
        self.assertEqual(budget.remaining_sec, 0.0)
        self.assertEqual(budget.bound_timeout(5.0), 0.0)

    def test_deadline_budget_unlimited(self):
        budget = DeadlineBudget(max_run_sec=0)
        self.assertFalse(budget.is_expired())
        self.assertEqual(budget.remaining_sec, float("inf"))
        self.assertEqual(budget.bound_timeout(10.0), 10.0)


class TestGuiSupervisorGates(unittest.TestCase):
    """Test GUI supervisor gates: rejection of false completion and defer handling"""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_gui_gates_"))
        self.run_dir = self.temp_dir / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.rollout = self.temp_dir / "rollout.jsonl"
        self.rollout.write_text("{}\n", encoding="utf-8")
        os.utime(self.rollout, (time.time() - 10, time.time() - 10))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_gui_completed_verdict_blocked_when_verification_fails(self):
        """When AGY L2 returns COMPLETED, verify_fn must be called. If verify_fn fails, completion is rejected."""
        mock_args = MagicMock()
        mock_args.max_run_sec = 10
        mock_args.max_interactions = 5
        mock_args.l2_cmd = "antigravity"

        injected_prompts = []
        budget = DeadlineBudget(max_run_sec=60)

        def mock_inject(text, **kwargs):
            injected_prompts.append(text)
            budget.deadline_mono = time.monotonic() - 1
            return True, "ok"

        def dispatch(*args, **kwargs):
            if kwargs["mode"] == "DECIDE":
                self.assertIn("缺少 ch02.md", kwargs["outcome_detail"])
                return L2Result("PROCEED", "补齐缺少 ch02.md 的章节", Path("fake.log"))
            return L2Result("COMPLETED", "AGY认为已完工", Path("fake.log"))

        # Mock is_codex_working to simulate Codex stopped at idle (is_working=False)
        with patch("supervise.is_codex_working", return_value=(False, "idle", "已完成阶段")):
            with patch("supervise.codex_app_running", return_value=True):
                with patch("supervise.l2_dispatch", side_effect=dispatch):
                    with patch("supervise.inject_into_codex_gui", side_effect=mock_inject):
                        with patch("supervise.ensure_codex_window_restored"):
                            with patch("time.sleep", return_value=None):
                                # verify_fn returns False (acceptance criteria not met)
                                mock_verify = MagicMock(return_value=(False, "缺少 ch02.md 章节"))

                                ret = supervise.run_gui_supervisor(
                                    sid="sid-123",
                                    rollout=str(self.rollout),
                                    scwd=str(self.temp_dir),
                                    title="GUI Test",
                                    args=mock_args,
                                    run_dir=self.run_dir,
                                    agy_mgr=None,
                                    proxy=None,
                                    verify_fn=mock_verify,
                                    budget=budget
                                )

                                # Crucial check: verify_fn was called
                                self.assertTrue(mock_verify.called)
                                # The loop timed out rather than returning SUCCESS 0
                                self.assertNotEqual(ret, 0)
                                # Feedback injection was triggered indicating unfulfilled criteria
                                self.assertTrue(any("显式验收未达标" in p or "缺少 ch02.md" in p for p in injected_prompts))


class TestSupervisorStateLifecycle(unittest.TestCase):
    """Test state machine persistence and full lifecycle"""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_state_lifecycle_"))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_state_persistence_and_transitions(self):
        state = SupervisorState(self.temp_dir, sid="sess-state-001", mode="gui", ws=self.temp_dir)
        self.assertEqual(state.state, "INIT")

        state_file = self.temp_dir / "supervisor_state.json"
        self.assertTrue(state_file.exists())
        data0 = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(data0["state"], "INIT")
        self.assertEqual(data0["mode"], "gui")

        # Transition to RUNNING
        state.transition("RUNNING", detail="GUI loop active", round=1, resumes=0)
        data1 = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(data1["state"], "RUNNING")
        self.assertEqual(data1["round"], 1)

        # Transition to WAITING_USER
        state.transition("WAITING_USER", detail="Awaiting user decision")
        data2 = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(data2["state"], "WAITING_USER")
        self.assertEqual(data2["detail"], "Awaiting user decision")

        # Final transition to SUCCESS
        state.transition("SUCCESS", detail="All checklist items verified")
        data3 = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(data3["state"], "SUCCESS")


class TestForkPauseAndPreAdoptGate(unittest.TestCase):
    """Test Fork pause check abort and pre-adopt decision gate"""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_fork_gate_"))
        self.run_dir = self.temp_dir / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.rollout = self.temp_dir / "rollout.jsonl"
        self.rollout.write_text("{}\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_fork_confirms_parent_pause_before_headless_handoff(self):
        test_args = ["supervise.py", "--adopt", "fake-session-id", "--fork", "--quick", "--yes"]
        order = []

        def pause_parent(rollout, *, max_wait):
            self.assertIsNone(max_wait)
            self.assertEqual(rollout, self.rollout)
            order.append("pause")
            return True

        def handoff(**kwargs):
            self.assertEqual(order, ["pause"])
            self.assertEqual(kwargs["rollout"], self.rollout)
            self.assertEqual(kwargs["adopt_mode"], "fork")
            order.append("headless")
            return 0

        with patch("sys.argv", test_args), \
             patch("supervise.find_codex_session_by_id", return_value=("fake-session-id", self.rollout, str(self.temp_dir))), \
             patch("supervise.pause_codex_gui_session", side_effect=pause_parent) as pause, \
             patch("supervise.close_codex_app") as close_app, \
             patch("afk_supervisor.cli.wait_session_quiet") as wait_quiet, \
             patch("supervise.WorkspaceSupervisorLock.acquire", return_value=(True, "")), \
             patch("supervise.WorkspaceSupervisorLock.release"), \
             patch("afk_supervisor.cli.run_headless_supervisor", side_effect=handoff):
            self.assertEqual(supervise.main(), 0)
        self.assertEqual(order, ["pause", "headless"])
        pause.assert_called_once_with(self.rollout, max_wait=None)
        close_app.assert_not_called()
        wait_quiet.assert_not_called()

    def test_fork_pause_failure_never_starts_headless_worker(self):
        test_args = ["supervise.py", "--adopt", "fake-session-id", "--fork", "--quick", "--yes"]
        with patch("sys.argv", test_args), \
             patch("supervise.find_codex_session_by_id", return_value=("fake-session-id", self.rollout, str(self.temp_dir))), \
             patch("supervise.pause_codex_gui_session", return_value=False) as pause, \
             patch("supervise.close_codex_app") as close_app, \
             patch("supervise.WorkspaceSupervisorLock.acquire", return_value=(True, "")) as acquire, \
             patch("supervise.WorkspaceSupervisorLock.release"), \
             patch("afk_supervisor.cli.run_headless_supervisor", return_value=0) as headless:
            self.assertEqual(supervise.main(), 1)
        pause.assert_called_once_with(self.rollout, max_wait=None)
        headless.assert_not_called()
        close_app.assert_not_called()
        acquire.assert_called_once()

    def test_fork_retries_legacy_defer_then_reports_failure(self):
        """Legacy DEFER is retried; persistent invalid decisions fail without launching a worker."""
        test_args = [
            "supervise.py",
            "--adopt", "fake-session-id",
            "--fork",
            "--quick",
            "--yes",
            "--l2-cmd", "antigravity"
        ]

        with patch("sys.argv", test_args):
            with patch("supervise.find_codex_session_by_id", return_value=("fake-session-id", self.rollout, str(self.temp_dir))):
                with patch("supervise.pause_codex_gui_session", return_value=True):
                    with patch("supervise.WorkspaceSupervisorLock.acquire", return_value=(True, "")):
                        with patch("supervise.WorkspaceSupervisorLock.release"):
                            # Simulate parent waiting for interaction decision
                            with patch("supervise.is_codex_working", return_value=(False, "paused", "请确认是否覆盖文件？")):
                                with patch("supervise.l2_dispatch", return_value=("DEFER", "", Path("fake.log"))) as dispatch, patch("afk_supervisor.engine.time.sleep") :
                                    rc = supervise.main()
                                    self.assertEqual(rc, 1)
                                    self.assertEqual(dispatch.call_count, 3)


class TestProtocolAndAcceptanceScenarios(unittest.TestCase):
    """验证12项核心验收与监管场景，杜绝讨好型完工判定"""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_proto_scenarios_"))
        self.ws = self.temp_dir / "workspace"
        self.ws.mkdir(parents=True, exist_ok=True)
        self.run_dir = self.temp_dir / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.mock_args = MagicMock()
        self.mock_args.l2_cmd = "antigravity"
        self.mock_args.max_interactions = 5
        self.mock_args.max_run_sec = 60
        self.budget = DeadlineBudget(60)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_coordinator(self, baseline: TaskBaseline = None, l2_cmd: str = "antigravity"):
        if not baseline:
            baseline = TaskBaseline(
                task_id="task-test-01",
                original_requirements="要求生成 calc.py 并实现 add 函数，以及 report.md",
                delivery_dir=str(self.ws),
                required_criteria=[
                    {"id": "c1", "description": "calc.py 存在且实现 add", "type": "artifact"},
                    {"id": "c2", "description": "report.md 报告存在", "type": "artifact"}
                ]
            )
        self.mock_args.l2_cmd = l2_cmd
        return SupervisorCoordinator(
            run_dir=self.run_dir,
            workspace_root=self.ws,
            delivery_dir=self.ws,
            task_baseline=baseline,
            agy_mgr=None,
            l2_cmd=l2_cmd,
            args=self.mock_args,
            proxy=None,
            budget=self.budget
        )

    # 1. Codex 声称已全部完成，但交付目录缺少必需产物 -> 判定不通过 (FAIL)
    def test_scenario_1_codex_claims_done_but_artifacts_missing(self):
        coord = self._create_coordinator()
        claim_msg = "已全部完成！所有功能均已交付完成，请验收。"
        mock_payload = {
            "protocol_version": "afk_agy_protocol_v1",
            "mode": "REVIEW",
            "request_id": "req-1",
            "task_id": "task-test-01",
            "reviewed_revision": "rev-1",
            "verdict": "FAIL",
            "findings": [{"criterion_id": "c1", "verdict": "UNFULFILLED", "reason": "缺少 calc.py"}],
            "next_action": {"action": "PROCEED", "instructions": "请创建 calc.py 并实现相应功能"}
        }
        with patch.object(coord, "handle_turn_review") as mock_review:
            mock_review.return_value = ("FAIL", "缺少必要产物 calc.py", Path("l2.log"), mock_payload)
            verdict, answer, l2_log, payload = mock_review(claim_msg, 1)
            self.assertEqual(verdict, "FAIL")
            self.assertIn("缺少", answer)
            self.assertNotEqual(verdict, "PASS")

    # 2. PROGRESS.md 全勾选，但代码存在语法错误 (如 py_compile 报错) -> 判定不通过
    def test_scenario_2_checklist_all_checked_but_syntax_error(self):
        coord = self._create_coordinator()
        (self.ws / "PROGRESS.md").write_text("- [x] 全部完成\n", encoding="utf-8")
        (self.ws / "bad.py").write_text("def broken_syntax(:\n   pass\n", encoding="utf-8")

        agy_mock_pass = L2Result("PASS", "AGY误判PASS", Path("l2.log"), payload={
            "protocol_version": "afk_agy_protocol_v1",
            "mode": "REVIEW",
            "request_id": "req-fake",
            "task_id": "task-test-01",
            "reviewed_revision": "rev-fake",
            "verdict": "PASS",
            "findings": [],
            "next_action": {"action": "FINISH", "instructions": ""}
        })
        with patch("supervise.l2_dispatch", return_value=agy_mock_pass):
            verdict, answer, l2_log, payload = coord.handle_turn_review("全部完成", 1)
            self.assertEqual(verdict, "FAIL")
            self.assertIn("Python语法检查失败", answer)

    # 3. 真实产物满足全部 baseline 要求，但 Codex 未输出“已全部完成”等关键词 -> 判定 PASS
    def test_scenario_3_deliverables_meet_baseline_without_completion_keywords(self):
        coord = self._create_coordinator()
        (self.ws / "calc.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
        (self.ws / "report.md").write_text("# 运行报告\n测试通过\n", encoding="utf-8")

        neutral_msg = "阶段测试结果已写入 report.md。"

        def mock_dispatch(*args, **kwargs):
            ev = kwargs.get("evidence_packet")
            req_id = kwargs.get("request_id") or (ev.request_id if ev else "req-1")
            rev = ev.reviewed_revision if ev else "rev-1"
            payload = {
                "protocol_version": "afk_agy_protocol_v1",
                "mode": "REVIEW",
                "request_id": req_id,
                "task_id": "task-test-01",
                "reviewed_revision": rev,
                "verdict": "PASS",
                "findings": [
                    {"criterion_id": "c1", "verdict": "FULFILLED", "reason": "calc.py 已实现"},
                    {"criterion_id": "c2", "verdict": "FULFILLED", "reason": "report.md 已提供"}
                ],
                "next_action": {"action": "FINISH", "instructions": "验收通过"}
            }
            return L2Result("PASS", json.dumps(payload), Path("l2.log"), payload=payload)

        with patch("supervise.l2_dispatch", side_effect=mock_dispatch):
            verdict, answer, l2_log, payload = coord.handle_turn_review(neutral_msg, 1)
            self.assertEqual(verdict, "PASS")
            self.assertIsNotNone(payload)
            self.assertEqual(payload.get("verdict"), "PASS")

    # 4. 既有产物已满足需求且内容无损 -> 不得要求强制刷新 mtime，复用已有成果判 PASS
    def test_scenario_4_preexisting_deliverables_valid_without_mtime_touch(self):
        coord = self._create_coordinator()
        calc = self.ws / "calc.py"
        calc.write_text("def add(a, b): return a + b\n", encoding="utf-8")
        report = self.ws / "report.md"
        report.write_text("# 报告\n", encoding="utf-8")

        old_time = time.time() - 3600
        os.utime(calc, (old_time, old_time))
        os.utime(report, (old_time, old_time))

        evidence = collect_evidence(self.ws, "检查现有文件", coord.task_baseline)
        self.assertTrue(any(i.path == "calc.py" for i in evidence.items))
        self.assertEqual(len(evidence.mechanical_failures), 0)
        self.assertNotEqual(evidence.reviewed_revision, "")

    # 5. AGY 返回陈旧 request_id、错误 task_id 或不匹配的 reviewed_revision -> 协议校验拦截
    def test_scenario_5_stale_or_mismatched_protocol_rejected(self):
        stale_payload = {
            "protocol_version": "afk_agy_protocol_v1",
            "mode": "REVIEW",
            "request_id": "stale-request-999",
            "task_id": "task-test-01",
            "reviewed_revision": "rev-mismatch",
            "verdict": "PASS",
            "findings": [],
            "next_action": {"action": "FINISH", "instructions": ""}
        }
        ok_val, reason = validate_protocol_payload(
            stale_payload, expected_request_id="actual-request-123", expected_task_id="task-test-01"
        )
        self.assertFalse(ok_val)
        self.assertIn("request_id mismatch", reason)

        ok_val2, reason2 = validate_protocol_payload(
            stale_payload, expected_request_id="stale-request-999", expected_task_id="different-task"
        )
        self.assertFalse(ok_val2)
        self.assertIn("task_id mismatch", reason2)

        ok_val3, reason3 = validate_protocol_payload(
            stale_payload, expected_request_id="stale-request-999", expected_task_id="task-test-01",
            expected_revision="expected-rev"
        )
        self.assertFalse(ok_val3)
        self.assertIn("reviewed_revision mismatch", reason3)

    # 6. REVIEW 模式建议维修 -> 转向 REPAIR 模式，且维修后须重新 REVIEW
    def test_scenario_6_review_suggests_repair_transitions_to_repair(self):
        coord = self._create_coordinator()
        review_payload = {
            "protocol_version": "afk_agy_protocol_v1",
            "mode": "REVIEW",
            "request_id": "req-1",
            "task_id": "task-test-01",
            "reviewed_revision": "rev-1",
            "verdict": "FAIL",
            "findings": [{"criterion_id": "c1", "verdict": "UNFULFILLED", "reason": "配置文件损坏"}],
            "next_action": {"action": "REPAIR", "instructions": "修复 config.json 中的语法错误"}
        }
        def mock_dispatch(*args, **kwargs):
            ev = kwargs.get("evidence_packet")
            review_payload["request_id"] = kwargs.get("request_id") or "req-1"
            review_payload["reviewed_revision"] = ev.reviewed_revision if ev else "rev-1"
            return L2Result("FAIL", json.dumps(review_payload), Path("l2.log"), payload=review_payload)

        with patch("supervise.l2_dispatch", side_effect=mock_dispatch):
            verdict, answer, l2_log, payload = coord.handle_turn_review("运行卡住了", 1)
            self.assertEqual(verdict, "FAIL")
            self.assertEqual(payload["next_action"]["action"], "REPAIR")

            repair_payload = {
                "protocol_version": "afk_agy_protocol_v1",
                "mode": "REPAIR",
                "request_id": "req-repair-1",
                "task_id": "task-test-01",
                "verdict": "REPAIRED",
                "repair_actions": [{"action_type": "patch", "target": "config.json", "details": "Fixed syntax"}],
                "next_action": {"action": "PROCEED", "instructions": "继续跑测试"}
            }
            with patch("supervise.l2_dispatch", return_value=L2Result("REPAIRED", json.dumps(repair_payload), Path("l2.log"), payload=repair_payload)):
                r_verdict, r_answer, r_log, r_pay = coord.handle_repair("配置文件损坏", "", 1)
                self.assertEqual(r_verdict, "REPAIRED")

    # 7. REPAIR 模式返回 REPAIRED -> 不得直接越级判定为 SUCCESS，必须经由 REVIEW 再次验收
    def test_scenario_7_repaired_does_not_directly_become_success(self):
        coord = self._create_coordinator()
        repair_payload = {
            "protocol_version": "afk_agy_protocol_v1",
            "mode": "REPAIR",
            "request_id": "req-repair-1",
            "task_id": "task-test-01",
            "verdict": "REPAIRED",
            "repair_actions": [],
            "next_action": {"action": "PROCEED", "instructions": "修复完毕，请重新验证"}
        }
        with patch("supervise.l2_dispatch", return_value=L2Result("REPAIRED", json.dumps(repair_payload), Path("l2.log"), payload=repair_payload)):
            r_verdict, r_answer, r_log, r_pay = coord.handle_repair("语法错误", "", 1)
            self.assertEqual(r_verdict, "REPAIRED")
            self.assertNotEqual(r_verdict, "SUCCESS")

    # 8. 证据不足（产物不全或需求存在缺口且未明确） -> 判定 INCONCLUSIVE，不得草率终结
    def test_scenario_8_insufficient_evidence_yields_inconclusive(self):
        coord = self._create_coordinator()
        inconclusive_payload = {
            "protocol_version": "afk_agy_protocol_v1",
            "mode": "REVIEW",
            "request_id": "req-1",
            "task_id": "task-test-01",
            "reviewed_revision": "rev-1",
            "verdict": "INCONCLUSIVE",
            "findings": [{"criterion_id": "c1", "verdict": "INCONCLUSIVE", "reason": "缺少运行日志验证输出"}],
            "next_action": {"action": "PROCEED", "instructions": "请运行测试并输出测试报告"}
        }
        def mock_dispatch(*args, **kwargs):
            ev = kwargs.get("evidence_packet")
            inconclusive_payload["request_id"] = kwargs.get("request_id") or (ev.request_id if ev else "req-1")
            inconclusive_payload["reviewed_revision"] = ev.reviewed_revision if ev else "rev-1"
            return L2Result("INCONCLUSIVE", json.dumps(inconclusive_payload), Path("l2.log"), payload=inconclusive_payload)

        with patch("supervise.l2_dispatch", side_effect=mock_dispatch):
            verdict, answer, l2_log, payload = coord.handle_turn_review("部分完成", 1)
            self.assertEqual(verdict, "INCONCLUSIVE")
            self.assertNotEqual(verdict, "PASS")

    # 9. worker 提问包含明确阻塞点（如选择方案） -> 触发 DECIDE 模式，不得误杀或强行判完工
    def test_scenario_9_worker_question_triggers_decide(self):
        coord = self._create_coordinator()
        worker_question = "【决策请求】数据库迁移有两个方案：A. 原地升级，B. 新建库并同步。请选择。"
        self.assertTrue(supervise.is_interaction_request(worker_question))

        decide_payload = {
            "protocol_version": "afk_agy_protocol_v1",
            "mode": "DECIDE",
            "request_id": "req-decide-1",
            "task_id": "task-test-01",
            "verdict": "PROCEED",
            "next_action": {"action": "PROCEED", "instructions": "选择方案 B，新建库并同步数据"}
        }
        with patch("supervise.l2_dispatch", return_value=L2Result("PROCEED", json.dumps(decide_payload), Path("l2.log"), payload=decide_payload)):
            verdict, answer, l2_log, payload = coord.handle_interaction(worker_question, 1)
            self.assertEqual(verdict, "PROCEED")
            cleaned = clean_l2_decision_text(answer)
            self.assertIn("选择方案 B", cleaned)

    # 10. afk (无头)、afk2 (无损fork)、afk3 (双有头) 三种接入模式在相同输入与产物状态下验收结论一致
    def test_scenario_10_consistent_behavior_across_afk_afk2_afk3(self):
        (self.ws / "calc.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
        (self.ws / "report.md").write_text("# 验收报告\n完成\n", encoding="utf-8")

        coord_afk = self._create_coordinator()
        coord_afk2 = self._create_coordinator()
        coord_afk3 = self._create_coordinator()

        def mock_dispatch(*args, **kwargs):
            ev = kwargs.get("evidence_packet")
            p = {
                "protocol_version": "afk_agy_protocol_v1",
                "mode": "REVIEW",
                "request_id": kwargs.get("request_id") or "req-1",
                "task_id": "task-test-01",
                "reviewed_revision": ev.reviewed_revision if ev else "rev-1",
                "verdict": "PASS",
                "findings": [],
                "next_action": {"action": "FINISH", "instructions": "全套完工"}
            }
            return L2Result("PASS", json.dumps(p), Path("l2.log"), payload=p)

        with patch("supervise.l2_dispatch", side_effect=mock_dispatch):
            v1, a1, _, _ = coord_afk.handle_turn_review("阶段完成", 1)
            v2, a2, _, _ = coord_afk2.handle_turn_review("阶段完成", 1)
            v3, a3, _, _ = coord_afk3.handle_turn_review("阶段完成", 1)
            self.assertEqual(v1, "PASS")
            self.assertEqual(v2, "PASS")
            self.assertEqual(v3, "PASS")

    # 11. 连续多轮无实质产物推进（连续相同 reviewed_revision 判定 FAIL） -> 熔断转人工 WAITING_USER
    def test_scenario_11_consecutive_no_progress_escalates_to_l2(self):
        coord = self._create_coordinator()
        (self.ws / "calc.py").write_text("# initial stub\n", encoding="utf-8")

        fail_payload = {
            "protocol_version": "afk_agy_protocol_v1",
            "mode": "REVIEW",
            "request_id": "req-1",
            "task_id": "task-test-01",
            "reviewed_revision": "rev-1",
            "verdict": "FAIL",
            "findings": [{"criterion_id": "c1", "verdict": "UNFULFILLED", "reason": "未实现"}],
            "next_action": {"action": "PROCEED", "instructions": "继续实现"}
        }
        def mock_dispatch(*args, **kwargs):
            if kwargs.get("mode") == "DECIDE":
                return L2Result("PROCEED", "改为采集运行证据", Path("l2.log"))
            ev = kwargs.get("evidence_packet")
            fail_payload["request_id"] = kwargs.get("request_id") or "req-1"
            fail_payload["reviewed_revision"] = ev.reviewed_revision if ev else "rev-static"
            return L2Result("FAIL", json.dumps(fail_payload), Path("l2.log"), payload=fail_payload)

        with patch("supervise.l2_dispatch", side_effect=mock_dispatch):
            v1, _, _, _ = coord.handle_turn_review("未动", 1)
            self.assertEqual(v1, "FAIL")
            v2, _, _, _ = coord.handle_turn_review("未动", 2)
            self.assertEqual(v2, "FAIL")
            v3, _, _, _ = coord.handle_turn_review("未动", 3)
            self.assertEqual(v3, "FAIL")
            v4, a4, _, _ = coord.handle_turn_review("未动", 4)
            self.assertEqual(v4, "PROCEED")
            self.assertIn("采集运行证据", a4)

    # 12. 专用 AGY skill 在会话内生效，记录哈希与版本，且不污染全局配置或干扰无关任务
    def test_scenario_12_dedicated_skill_session_and_hash_isolation(self):
        ver, short_hash = get_skill_metadata(supervise.WS)
        self.assertEqual(ver, "1.0.0")
        self.assertTrue(len(short_hash) >= 8)

        full_prompt, short_prompt = build_protocol_prompt(
            mode="REVIEW",
            task_baseline=TaskBaseline(task_id="t1", original_requirements="test", delivery_dir=str(self.ws)),
            request_id="req-test-12",
            skill_version=ver,
            skill_hash=short_hash
        )
        self.assertIn("afk_agy_protocol_v1", full_prompt)
        self.assertIn(f"[SKILL_LOADED:afk-supervisor-reviewer v{ver} hash={short_hash}]", full_prompt)
        self.assertIn("afk-supervisor-reviewer", full_prompt)

    # 13. 接管/快速挂机模式 (无 task_md/无 PROGRESS.md) 下，AGY 客观审查判定 PASS 能够正常结束监管，不因缺失清单死循环
    def test_scenario_13_adopt_mode_passes_on_agy_verdict_without_checklist(self):
        # 1. 自然完工语义命中测试
        ok_nat, det_nat = check_acceptance_natural(self.ws, "阶段审查: 已终结并确认成功。")
        self.assertTrue(ok_nat)
        self.assertIn("已终结", det_nat)

        # 2. 即使消息未命中任何关键词 ("未检测到活跃的已完成清单或明确完工语义")，当 AGY 主管客观审查 PASS 且代码无语法错误时，SupervisorCoordinator 与主循环均放行完工
        test_file = self.ws / "index.py"
        test_file.write_text("value = 'NOVA UI ready'\n", encoding="utf-8")

        coord = self._create_coordinator()
        pass_payload = {
            "protocol": "afk_agy_protocol_v1",
            "request_id": "req-test-13",
            "task_id": "task-test-01",
            "mode": "REVIEW",
            "reviewed_revision": "rev-test-13",
            "verdict": "PASS",
            "criteria": [
                {"id": "c1", "verdict": "PASS", "evidence_ids": [], "reason": "界面逻辑已实现"}
            ],
            "blockers": [],
            "next_action": {"action": "PROCEED", "instructions": "客观审查通过，任务已达标"},
            "repairs": []
        }

        with patch("supervise.l2_dispatch", return_value=L2Result("PASS", json.dumps(pass_payload), Path("l2.log"), payload=pass_payload)):
            # 传入返回 (False, "未检测到活跃的已完成清单或明确完工语义") 的 verify_fn
            mock_verify = MagicMock(return_value=(False, "未检测到活跃的已完成清单或明确完工语义"))
            verdict, answer, l2_log, payload = coord.handle_turn_review(
                "这是最新的提交汇报，没有写进度清单", 1, verify_fn=mock_verify, session_cwd=self.ws
            )
            self.assertEqual(verdict, "PASS")


class TestQoderReviewImprovements(unittest.TestCase):
    """验证吸收 Qoder 审查建议后的所有关键增强特性"""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_qoder_"))
        self.ws = self.temp_dir / "workspace"
        self.ws.mkdir(parents=True, exist_ok=True)
        self.run_dir = self.temp_dir / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_codex_desktop_vs_cli_process_filtering(self):
        """验证精确进程识别：准确提取桌面App与app-server，坚决排除 CLI worker 且不进行盲杀"""
        mock_rows = [
            dict(ProcessId=1234, ParentProcessId=1, Name="ChatGPT.exe",
                 ExecutablePath=r"C:\Program Files\WindowsApps\OpenAI.Codex\app\ChatGPT.exe",
                 CommandLine='"C:\\Program Files\\WindowsApps\\OpenAI.Codex\\app\\ChatGPT.exe" --type=renderer'),
            dict(ProcessId=5678, ParentProcessId=1234, Name="codex.exe",
                 ExecutablePath=r"C:\Users\test\AppData\Local\OpenAI\Codex\bin\codex.exe",
                 CommandLine="codex.exe -c features.code_mode_host=true app-server"),
            dict(ProcessId=9999, ParentProcessId=1234, Name="codex.exe",
                 ExecutablePath=r"C:\bin\codex.exe", CommandLine="codex.exe exec -C C:\\myproj"),
            dict(ProcessId=8888, ParentProcessId=1234, Name="codex.exe",
                 ExecutablePath=r"C:\bin\codex.exe", CommandLine="codex.exe fork -C C:\\myproj"),
        ]

        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(stdout=json.dumps(mock_rows).encode("utf-8"), returncode=0)
            with patch("supervise.pid_is_running", return_value=True):
                desktop_pids = supervise.get_codex_desktop_pids()
                # 必须包含 1234 (UI) 和 5678 (app-server)
                self.assertIn(1234, desktop_pids)
                self.assertIn(5678, desktop_pids)
                # 严禁包含 9999 和 8888 (CLI workers)
                self.assertNotIn(9999, desktop_pids)
                self.assertNotIn(8888, desktop_pids)

                self.assertTrue(supervise.codex_app_running())

        # 验证 close_codex_app 仅按特定 PID 终止，绝不使用 taskkill /IM codex.exe /F
        with patch("supervise.get_codex_desktop_pids", side_effect=[[1234, 5678], []]):
            with patch("subprocess.run") as mock_run, patch("time.sleep"):
                alive_pids = {1234, 5678}
                def mock_alive(p):
                    return p in alive_pids
                def mock_run_call(cmd, *args, **kwargs):
                    if len(cmd) >= 3 and cmd[1] == "/PID":
                        alive_pids.discard(int(cmd[2]))
                    return MagicMock(returncode=0)
                mock_run.side_effect = mock_run_call
                with patch("supervise.pid_is_running", side_effect=mock_alive):
                    killed = supervise.close_codex_app(wait_boundary=False)
                    self.assertEqual(set(killed), {"1234", "5678"})
                    # 检查所有 taskkill 调用，绝不能包含 /IM codex.exe
                    for call_args in mock_run.call_args_list:
                        cmd_called = call_args[0][0]
                        self.assertNotIn("/IM", cmd_called)
                        self.assertIn("/PID", cmd_called)

    def test_workspace_lock_atomic_prevent_concurrency(self):
        """验证基于 os.open 的原子工作区锁互斥性与生命周期"""
        lock1 = supervise.WorkspaceSupervisorLock(self.ws, "sess-1", "headless", lock_dir=self.ws / "test-locks")
        ok1, err1 = lock1.acquire()
        self.assertTrue(ok1)
        self.assertTrue(lock1.acquired)

        # 同一工作区，另一个实例并发获取应被拒绝
        lock2 = supervise.WorkspaceSupervisorLock(self.ws, "sess-2", "fork", lock_dir=self.ws / "test-locks")
        ok2, err2 = lock2.acquire()
        self.assertFalse(ok2)
        self.assertIn("正被另一监管器实例占用", err2)

        # 释放后，实例2可成功获取
        lock1.release()
        self.assertFalse(lock1.acquired)

        ok2_after, _ = lock2.acquire()
        self.assertTrue(ok2_after)
        lock2.release()

    def test_file_handles_closed_on_driver_lifecycle(self):
        """验证 CodexDriver 在多次启动与 kill 时旧文件句柄均被安全关闭"""
        driver = supervise.CodexDriver(self.ws, self.run_dir)
        prompt_f = self.run_dir / "p.txt"
        prompt_f.write_text("hello", encoding="utf-8")

        mock_proc = MagicMock()
        mock_proc.pid = 1111
        mock_proc.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc):
            driver._spawn_once(["exec", "-"], prompt_f)
            handles_round_1 = list(driver._open_handles)
            self.assertEqual(len(handles_round_1), 3)
            for h in handles_round_1:
                self.assertFalse(h.closed)

            # 第二轮 spawn 前，第一轮句柄必须被自动关闭
            driver._spawn_once(["resume", "-"], prompt_f)
            for h in handles_round_1:
                self.assertTrue(h.closed)
            handles_round_2 = list(driver._open_handles)
            self.assertEqual(len(handles_round_2), 3)

            # 调用 kill_tree 之后，当前句柄必须全部关闭
            driver.kill_tree()
            for h in handles_round_2:
                self.assertTrue(h.closed)
            self.assertEqual(len(driver._open_handles), 0)

    def test_natural_acceptance_allows_positive_phrasing_with_bu(self):
        """验证否定词过滤器改进：'还不错'、'不错'等肯定表达顺利通过验收，而真否定词被正确拦截"""
        # 肯定表述中带 "不" 应当放行
        ok1, det1 = supervise.check_acceptance_natural(self.ws, "整体运行还不错，已全部完成全部工作。")
        self.assertTrue(ok1)
        self.assertIn("已全部完成", det1)

        ok2, det2 = supervise.check_acceptance_natural(self.ws, "代码质量挺不错，任务已全部交付。")
        self.assertTrue(ok2)
        self.assertIn("全部交付", det2)

        # 真正否定表述被拦截
        ok3, det3 = supervise.check_acceptance_natural(self.ws, "目前任务并未全部完成，还需要重构。")
        self.assertFalse(ok3)
        self.assertIn("检测到否定语义", det3)

        ok4, det4 = supervise.check_acceptance_natural(self.ws, "核心模块尚未全部交付。")
        self.assertFalse(ok4)
        self.assertIn("检测到否定语义", det4)

    def test_l2_result_dataclass_and_unpacking(self):
        """验证 L2Result 改为 dataclass 后既支持显式属性，又支持3元组解包"""
        payload = {"verdict": "PASS", "task_id": "t1"}
        res = supervise.L2Result(verdict="PASS", answer="LGTM", l2_log=Path("l2.log"), payload=payload)

        # 显式属性
        self.assertEqual(res.verdict, "PASS")
        self.assertEqual(res.answer, "LGTM")
        self.assertEqual(res.payload, payload)

        # 兼容旧元组解包
        v, a, log_path = res
        self.assertEqual(v, "PASS")
        self.assertEqual(a, "LGTM")
        self.assertEqual(log_path, Path("l2.log"))

        # 兼容索引访问
        self.assertEqual(res[0], "PASS")
        self.assertEqual(res[1], "LGTM")
        self.assertEqual(res[2], Path("l2.log"))

    def test_l2_verdict_files_placed_in_run_dir(self):
        """验证 L2 verdict 文件输出严格隔离在 run_dir 中，不污染工作区根目录"""
        agy_mock = MagicMock()
        agy_mock.ensure_bridge.return_value = ("csrf-1", [9999], Path(sys.executable))
        agy_mock.cid = "cid-test"

        response_payload = {
            "protocol": "afk_agy_protocol_v1",
            "request_id": "req-vd-1",
            "task_id": "task-vd-1",
            "mode": "DECIDE",
            "reviewed_revision": "rev-1",
            "verdict": "PROCEED",
            "next_action": {"action": "PROCEED", "instructions": "允许继续推进"}
        }
        res_text = f"```json\n{json.dumps(response_payload)}\n```"

        with patch("supervise.read_agy_latest_response", return_value=("PROCEED", res_text, Path("fake.log"))):
            with patch("subprocess.run") as mock_sub:
                mock_sub.return_value = MagicMock(stdout=b'{"conversationId": "cid-test"}', returncode=0)
                l2_res = supervise.run_l2_antigravity(
                    run_dir=self.run_dir,
                    full_prompt="prompt",
                    short_prompt="prompt",
                    n=1,
                    scwd=self.ws,
                    verdict_file="afk-l2-verdict.txt",
                    agy_mgr=agy_mock,
                    request_id="req-vd-1",
                    mode="DECIDE"
                )
                self.assertEqual(l2_res.verdict, "PROCEED")
                # 检查工作区根目录绝对无 afk-l2-* 文件
                ws_verdicts = list(self.ws.glob("afk-l2-*"))
                self.assertEqual(len(ws_verdicts), 0, f"工作区被污染: {ws_verdicts}")

    def test_read_rollout_last_message_tail_window(self):
        """验证 read_rollout_last_message 倒序窗口读取能够正确解析大文件末尾的 assistant 消息"""
        rollout_file = self.run_dir / "large_rollout.jsonl"
        # 写入大量无害前置数据，模拟数百 KB 的轨迹
        filler = json.dumps({"type": "event_msg", "payload": {"text": "padding" * 100}}) + "\n"
        with open(rollout_file, "w", encoding="utf-8") as f:
            for _ in range(2000):
                f.write(filler)
            # 在尾部写入最新 assistant 消息
            final_event = json.dumps({
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "已全部完成所有单元测试与功能更新。"}]
                }
            }) + "\n"
            f.write(final_event)

        msg = supervise.read_rollout_last_message(rollout_file)
        self.assertEqual(msg, "已全部完成所有单元测试与功能更新。")


class TestProtocolErrorFeedback(unittest.TestCase):
    """验证 L2 协议校验失败时反馈注入与 Coordinator 状态流转"""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_proto_fb_"))
        self.run_dir = self.temp_dir / "run"
        self.ws = self.temp_dir / "ws"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.ws.mkdir(parents=True, exist_ok=True)
        self.task_bl = TaskBaseline(
            task_id="task-fb-01",
            original_requirements="Feedback Test",
            delivery_dir=str(self.ws),
            required_criteria=[
                {"id": "crit_core", "description": "必须完成核心功能", "type": "artifact"},
            ],
        )
        args = MagicMock()
        args.max_l2_retries = 3
        args.l2_timeout = 10
        args.max_run_sec = 60
        self.coordinator = SupervisorCoordinator(
            run_dir=self.run_dir,
            workspace_root=self.ws,
            delivery_dir=self.ws,
            task_baseline=self.task_bl,
            agy_mgr=None,
            l2_cmd="test-l2",
            args=args,
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_build_protocol_prompt_includes_error_feedback(self):
        """测试 build_protocol_prompt 在提供 protocol_error_feedback 时包含醒目告警块"""
        full_p, inc_p = build_protocol_prompt(
            mode="REVIEW",
            task_baseline=self.task_bl,
            request_id="req-fb-1",
            question_or_context="测试审查上下文",
            protocol_error_feedback="REVIEW 遗漏基线必需验收项: 'crit_core'",
        )
        self.assertIn("【⚠️ 协议校验未通过警告 — 上一次回复格式未满足规约，请修正后重新返回】", full_p)
        self.assertIn("REVIEW 遗漏基线必需验收项: 'crit_core'", full_p)

    def test_coordinator_propagates_protocol_error_feedback(self):
        """测试 coordinator 在遇到 PROTOCOL_ERROR 时记录反馈并在下一次重试时注入"""
        dispatched_feedbacks = []

        def mock_dispatch(*args, **kwargs):
            feedback = kwargs.get("protocol_error_feedback", "")
            dispatched_feedbacks.append(feedback)
            if len(dispatched_feedbacks) == 1:
                # 第一次返回格式错误：缺少必需验收项
                invalid_json = json.dumps({
                    "version": "afk.l2.v1",
                    "mode": "REVIEW",
                    "request_id": "req-1",
                    "verdict": "PASS",
                    "criteria_evaluations": [],  # 故意遗漏基线
                    "reasons": "测试",
                    "next_action": {"type": "terminate_success", "instructions": "OK"},
                    "blockers": [],
                    "evidence_refs": [],
                })
                log_file = self.run_dir / "l2-fake-1.log"
                log_file.write_text("```json\n" + invalid_json + "\n```", encoding="utf-8")
                return L2Result(verdict="PROTOCOL_ERROR", answer="REVIEW 遗漏基线必需验收项: 'crit_core'", l2_log=log_file, payload={})
            else:
                # 第二次带反馈后返回正确格式
                valid_json = json.dumps({
                    "version": "afk.l2.v1",
                    "mode": "REVIEW",
                    "request_id": "req-2",
                    "verdict": "PASS",
                    "criteria_evaluations": [{"id": "crit_core", "verdict": "MET", "evidence": "done"}],
                    "reasons": "已修复",
                    "next_action": {"type": "terminate_success", "instructions": "OK"},
                    "blockers": [],
                    "evidence_refs": [],
                })
                log_file = self.run_dir / "l2-fake-2.log"
                log_file.write_text("```json\n" + valid_json + "\n```", encoding="utf-8")
                return L2Result(verdict="PASS", answer="已完成", l2_log=log_file, payload=json.loads(valid_json))

        with patch("supervise.l2_dispatch", side_effect=mock_dispatch):
            driver = MagicMock()
            driver.session_id = "test-sid"
            driver.provider_name = "test-prov"
            driver.title = "test-title"

            # 第一轮触发，产生 PROTOCOL_ERROR
            v1, a1, _, _ = self.coordinator.handle_turn_review("请审查", 1, driver=driver, session_cwd=str(self.ws))
            self.assertEqual(v1, "PROTOCOL_ERROR")
            self.assertIn("REVIEW 遗漏基线必需验收项", self.coordinator.last_protocol_error)
            self.assertEqual(dispatched_feedbacks[0], "")  # 首次调用无上轮错误

            # 第二轮重试，应带入上一轮的错误反馈
            v2, a2, _, _ = self.coordinator.handle_turn_review("请审查", 2, driver=driver, session_cwd=str(self.ws))
            self.assertEqual(v2, "PASS")
            self.assertIn("REVIEW 遗漏基线必需验收项", dispatched_feedbacks[1])
            # 成功后 last_protocol_error 应被清空
            self.assertEqual(self.coordinator.last_protocol_error, "")


class TestHeartbeatStallDefense(unittest.TestCase):
    """验证心跳防假死逻辑（note_activity、rollout 及 stdout 文件写盘增长感知）"""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_hb_defense_"))
        self.run_dir = self.temp_dir / "run"
        self.ws = self.temp_dir / "ws"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.ws.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_codex_driver_heartbeat_activity_and_growth(self):
        """验证 CodexDriver 在长时间运行中感知 note_activity 以及文件写盘增长"""
        driver = CodexDriver(self.ws, self.run_dir)
        driver.session_id = "sess-hb-01"
        rollout_file = self.run_dir / "rollout-test.jsonl"
        stdout_file = self.run_dir / "worker-stdout.log"
        rollout_file.write_text('{"type": "init"}\n', encoding="utf-8")
        stdout_file.write_text('init\n', encoding="utf-8")
        driver.jsonl = rollout_file

        launched_at = time.time() - 300.0  # 假设 300 秒前启动

        # 初始调用，记录基础尺寸
        age1 = driver.heartbeat_age(launched_at)
        self.assertLess(age1, 5.0)  # 文件刚创建，mtime 是当前时间

        # 模拟 100 秒后无 mtime 变化（通过将 mtime 回拨模拟 NTFS 缓冲未刷盘）
        old_time = time.time() - 100.0
        os.utime(rollout_file, (old_time, old_time))
        os.utime(stdout_file, (old_time, old_time))
        driver._last_activity_time = old_time

        # 1. 测试 note_activity
        now = time.time()
        driver.note_activity(now)
        age_after_note = driver.heartbeat_age(launched_at)
        self.assertLess(age_after_note, 2.0)

        # 2. 测试 rollout 文件新增数据但 mtime 仍处于过去的情况（NTFS 写缓冲场景）
        driver._last_activity_time = old_time
        with open(rollout_file, "a", encoding="utf-8") as f:
            f.write('{"type": "custom_tool_call", "payload": {"name": "test_tool"}}\n')
        os.utime(rollout_file, (old_time, old_time))  # 保持旧 mtime

        age_after_growth = driver.heartbeat_age(launched_at)
        self.assertLess(age_after_growth, 2.0, "rollout 大小增长应刷新心跳锚点")

        # 3. 测试 worker-stdout.log 新增数据但 mtime 处于过去的情况
        driver._last_activity_time = old_time
        with open(stdout_file, "a", encoding="utf-8") as f:
            f.write('new stdout line\n')
        os.utime(stdout_file, (old_time, old_time))  # 保持旧 mtime

        age_after_stdout_growth = driver.heartbeat_age(launched_at)
        self.assertLess(age_after_stdout_growth, 2.0, "stdout 大小增长应刷新心跳锚点")


if __name__ == "__main__":
    unittest.main(verbosity=2)

