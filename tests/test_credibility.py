"""
tests/test_credibility.py — 验收可信度与刚性证据约束回归测试集
============================================================
覆盖规范要求:
- Item 1: 用户绝对交付目录不同于 cwd 时，基线严格保留显式绝对路径，不被 cwd 子目录猜测覆盖。
- Item 2: 交付目录不存在或不可写时记录阻塞，禁止扫描 cwd 冒充产物达标。
- Item 3: 仅凭 Worker 自述或进度勾选，缺乏客观行为/测试证据时拒绝功能项 PASS。
- Item 4: 运行器缺失、执行超时或空证据包时记录明确失败，禁止吞异常当通过。
- Item 5: 充分客观证据、产物齐全、语法通过时，即使无固定完工套话仍可正常 PASS。
- Item 10: 审查判定 PASS 后若产物发生变动，旧审查结果失效并触发重新采证复审。
- Item 16: 产物版本与修改时间戳 (mtime) 隔离，仅 touch 不改内容时 artifact_revision 保持不变。
"""

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from afk_supervisor.baseline import TaskBaseline, extract_task_baseline
from afk_supervisor.evidence import (
    calculate_artifact_revision,
    calculate_reviewed_revision,
    collect_evidence,
)
from afk_supervisor.models import ActionType, EvidenceItem, EvidencePacket
from afk_supervisor.l2.protocol import validate_protocol_payload


class TestAcceptanceCredibility(unittest.TestCase):
    """验证验收可信度相关刚性规则。"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="afk_credibility_")
        self.base_p = Path(self.tmp_dir)

    def tearDown(self):
        try:
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
        except Exception:
            pass

    def test_item1_explicit_delivery_dir_preserved_across_different_cwd(self):
        """Item 1: 用户在需求中显式指定绝对交付目录时，提取基线必须保留该显式路径，严禁被 cwd 覆盖。"""
        fake_cwd = self.base_p / "actual_worker_cwd"
        fake_cwd.mkdir(parents=True, exist_ok=True)

        target_delivery = self.base_p / "explicit_target_workspace"
        target_delivery.mkdir(parents=True, exist_ok=True)

        user_req = f"请在交付目录: {target_delivery} 下构建待办事项应用，生成 index.html 和 app.js"
        baseline = extract_task_baseline(
            session_cwd=fake_cwd,
            title=user_req,
            writable_roots=[str(self.base_p)],
        )

        self.assertEqual(
            Path(baseline.requested_delivery_dir).resolve(),
            target_delivery.resolve(),
            "基线中的 requested_delivery_dir 必须为用户显式指定的交付目录"
        )
        self.assertEqual(
            Path(baseline.effective_delivery_dir).resolve(),
            target_delivery.resolve(),
            "有效交付目录必须匹配用户指定目标"
        )
        self.assertNotEqual(
            Path(baseline.requested_delivery_dir).resolve(),
            fake_cwd.resolve(),
            "请求的交付目录不能被实际 worker cwd 覆盖"
        )
        self.assertEqual(len(baseline.blockers), 0, "合法的目标目录不应产生阻塞")

    def test_item2_nonexistent_delivery_path_creates_blocker_no_silent_fallback(self):
        """Item 2: 交付目录不存在时必须记录 blocker，采证时严禁静默扫描 cwd 冒充产物达标。"""
        fake_cwd = self.base_p / "worker_cwd"
        fake_cwd.mkdir(parents=True, exist_ok=True)
        # 在 cwd 中放假产物，检验是否会发生静默扫描 cwd
        (fake_cwd / "fake_artifact.js").write_text("console.log('trap');", encoding="utf-8")

        non_existent_dir = self.base_p / "does_not_exist_delivery_dir"

        baseline = extract_task_baseline(
            session_cwd=fake_cwd,
            title=f"工作区: {non_existent_dir} 构建系统",
            writable_roots=[str(self.base_p)],
        )

        self.assertIn("不存在", "".join(baseline.blockers), "不存在的目标目录必须产生明确 blocker")
        self.assertEqual(baseline.effective_delivery_dir, "", "不存在的目录不能作为 effective_delivery_dir")

        evidence = collect_evidence(
            session_cwd=fake_cwd,
            last_msg="All done!",
            task_baseline=baseline,
        )

        # 严禁采集到 fake_cwd 中的产物
        artifact_paths = [it.path for it in evidence.items if it.category == "artifact"]
        self.assertEqual(artifact_paths, [], "目标目录不存在时严禁将 cwd 文件作为交付产物采集")

        # 必须有机械失败记录
        self.assertTrue(any("目标交付目录不存在" in f for f in evidence.mechanical_failures))

    def test_item3_functional_completeness_rejected_with_only_worker_statement(self):
        """Item 3: 功能验收项 crit_functional_completeness 仅有 Worker 自述或清单勾选时，严禁评定为 PASS。"""
        baseline = TaskBaseline(
            task_id="task_calc",
            original_requirements="实现计算器",
            session_cwd=str(self.base_p),
            effective_delivery_dir=str(self.base_p),
            required_criteria=[
                {"id": "crit_functional_completeness", "type": "functional", "description": "核心计算功能验证"},
                {"id": "crit_core_artifacts", "type": "artifact", "description": "产物存在"},
            ]
        )

        ev_items = [
            EvidenceItem(
                id="ev_worker_last_msg",
                category="worker_statement",
                summary="Worker自述完成",
                details="已完成计算器所有功能，但由于没有浏览器未进行UI交互测试",
            ),
            EvidenceItem(
                id="ev_file_calc_js",
                category="artifact",
                summary="calc.js 文件",
                path="calc.js",
                size=200,
                sha256_short="abc123456789",
            ),
        ]
        packet = EvidencePacket(
            task_id="task_calc",
            delivery_dir=str(self.base_p),
            reviewed_revision="rev-test-123",
            artifact_revision="art-test-123",
            request_id="req-test-1",
            items=ev_items,
        )

        # 企图仅用 ev_worker_last_msg 让 crit_functional_completeness 通过
        payload = {
            "protocol": "afk_agy_protocol_v1",
            "request_id": "req-test-1",
            "task_id": "task_calc",
            "mode": "REVIEW",
            "reviewed_revision": "rev-test-123",
            "verdict": "PASS",
            "criteria": [
                {"id": "crit_core_artifacts", "verdict": "PASS", "evidence_ids": ["ev_file_calc_js"], "reason": "文件存在"},
                {"id": "crit_functional_completeness", "verdict": "PASS", "evidence_ids": ["ev_worker_last_msg"], "reason": "Worker留言自述完成"},
            ],
            "blockers": [],
            "next_action": {"type": ActionType.TERMINATE_SUCCESS, "instructions": "全部完成，验收通过"},
        }

        ok, reason = validate_protocol_payload(
            payload,
            expected_request_id="req-test-1",
            expected_task_id="task_calc",
            expected_mode="REVIEW",
            expected_revision="rev-test-123",
            task_baseline=baseline,
            evidence_packet=packet,
        )

        self.assertFalse(ok, "仅依赖 worker_statement 时功能验收项不可通过")
        self.assertIn("缺乏客观执行验证证据", reason)

    def test_item4_missing_runner_or_timeout_recorded_as_mechanical_failure(self):
        """Item 4: 执行语法检查或自测时若运行器缺失或超时，必须记录为 mechanical_failures 并阻止 PASS。"""
        js_file = self.base_p / "broken.js"
        js_file.write_text("var a = 1;", encoding="utf-8")

        baseline = TaskBaseline(
            task_id="task_runner_check",
            original_requirements="JS应用",
            session_cwd=str(self.base_p),
            effective_delivery_dir=str(self.base_p),
            required_criteria=[
                {"id": "crit_core_artifacts", "type": "artifact", "description": "产物存在"},
                {"id": "crit_verification_checks", "type": "verification", "description": "语法验证"},
            ]
        )

        # 模拟 node 运行器缺失 (FileNotFoundError)
        with patch("subprocess.run", side_effect=FileNotFoundError("node executable not found")):
            evidence = collect_evidence(
                session_cwd=self.base_p,
                last_msg="ready",
                task_baseline=baseline,
                request_id="req-missing-node",
            )

        self.assertTrue(len(evidence.mechanical_failures) > 0, "运行器缺失必须记录机械失败")
        self.assertTrue(any("Node.js" in f or "Node" in f for f in evidence.mechanical_failures))

        # 验证该证据包下不能评定 PASS
        payload = {
            "protocol": "afk_agy_protocol_v1",
            "request_id": "req-missing-node",
            "task_id": "task_runner_check",
            "mode": "REVIEW",
            "reviewed_revision": evidence.reviewed_revision,
            "verdict": "PASS",
            "criteria": [
                {"id": "crit_core_artifacts", "verdict": "PASS", "evidence_ids": [evidence.items[0].id], "reason": "产物存在"},
                {"id": "crit_verification_checks", "verdict": "PASS", "evidence_ids": [evidence.items[1].id], "reason": "伪称通过"},
            ],
            "blockers": [],
            "next_action": {"type": ActionType.TERMINATE_SUCCESS, "instructions": "通过"},
        }

        ok, reason = validate_protocol_payload(
            payload,
            expected_request_id="req-missing-node",
            expected_task_id="task_runner_check",
            expected_mode="REVIEW",
            expected_revision=evidence.reviewed_revision,
            task_baseline=baseline,
            evidence_packet=evidence,
        )
        self.assertFalse(ok, "存在机械失败时绝对不可判定为 PASS")
        self.assertIn("机械检查存在明确失败", reason)

    def test_item5_substantive_deliverables_pass_without_worker_completion_keywords(self):
        """Item 5: 产物齐全、语法核验通过、具备执行验证证据时，即使 Worker 留言没有任何特定套话，仍能正常判定 PASS。"""
        js_file = self.base_p / "app.js"
        js_file.write_text("console.log('hello');", encoding="utf-8")

        baseline = TaskBaseline(
            task_id="task_calc_ok",
            original_requirements="开发应用",
            session_cwd=str(self.base_p),
            effective_delivery_dir=str(self.base_p),
            required_criteria=[
                {"id": "crit_core_artifacts", "type": "artifact", "description": "核心产物"},
                {"id": "crit_verification_checks", "type": "verification", "description": "语法核验"},
            ]
        )

        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = ""
        mock_res.stderr = ""

        with patch("subprocess.run", return_value=mock_res):
            evidence = collect_evidence(
                session_cwd=self.base_p,
                last_msg="Finished milestone 2. Code is clean and checked.",  # 无任何'已全部完成'套话
                task_baseline=baseline,
                request_id="req-ok-1",
            )

        self.assertEqual(len(evidence.mechanical_failures), 0)
        art_id = [it.id for it in evidence.items if it.category == "artifact"][0]
        verif_id = [it.id for it in evidence.items if it.category == "verification_result"][0]

        payload = {
            "protocol": "afk_agy_protocol_v1",
            "request_id": "req-ok-1",
            "task_id": "task_calc_ok",
            "mode": "REVIEW",
            "reviewed_revision": evidence.reviewed_revision,
            "verdict": "PASS",
            "criteria": [
                {"id": "crit_core_artifacts", "verdict": "PASS", "evidence_ids": [art_id], "reason": "产物真实存在且非空"},
                {"id": "crit_verification_checks", "verdict": "PASS", "evidence_ids": [verif_id], "reason": "node --check 语法无错误"},
            ],
            "blockers": [],
            "next_action": {"type": ActionType.TERMINATE_SUCCESS, "instructions": "所有客观验收项通过"},
        }

        ok, reason = validate_protocol_payload(
            payload,
            expected_request_id="req-ok-1",
            expected_task_id="task_calc_ok",
            expected_mode="REVIEW",
            expected_revision=evidence.reviewed_revision,
            task_baseline=baseline,
            evidence_packet=evidence,
        )
        self.assertTrue(ok, f"客观证据完备时应通过校验，实际报错: {reason}")

    def test_item10_artifact_mutation_invalidates_reviewed_revision(self):
        """Item 10: 获得 PASS 后若产物发生变动（新文件或内容修改），审查版本发生改变，旧审查自动失效。"""
        app_js = self.base_p / "app.js"
        app_js.write_text("console.log('v1');", encoding="utf-8")

        baseline = TaskBaseline(
            task_id="task_mutation",
            original_requirements="开发应用",
            session_cwd=str(self.base_p),
            effective_delivery_dir=str(self.base_p),
        )

        ev1 = collect_evidence(self.base_p, "done", baseline, request_id="req-1")
        old_reviewed_rev = ev1.reviewed_revision

        # 模拟在退出前 Worker 或进程写入了新修改
        time.sleep(0.01)
        app_js.write_text("console.log('v2-modified');", encoding="utf-8")

        ev2 = collect_evidence(self.base_p, "done", baseline, request_id="req-pre-exit")
        new_reviewed_rev = ev2.reviewed_revision

        self.assertNotEqual(
            old_reviewed_rev, new_reviewed_rev,
            "产物内容改变后，reviewed_revision 必须发生改变以致旧审查结论失效"
        )

        # 尝试用旧版本 payload 进行校验
        payload = {
            "protocol": "afk_agy_protocol_v1",
            "request_id": "req-2",
            "task_id": "task_mutation",
            "mode": "REVIEW",
            "reviewed_revision": old_reviewed_rev,
            "verdict": "PASS",
            "criteria": [],
            "blockers": [],
            "next_action": {"type": ActionType.TERMINATE_SUCCESS, "instructions": "pass"},
        }
        ok, reason = validate_protocol_payload(
            payload,
            expected_revision=new_reviewed_rev,
        )
        self.assertFalse(ok, "版本已失效的旧 payload 必须被拒绝")
        self.assertIn("reviewed_revision", reason)

    def test_item16_artifact_revision_ignores_mtime_when_content_is_unchanged(self):
        """Item 16: 产物内容不变仅刷新修改时间戳 (touch)，artifact_revision 必须保持完全一致，防止伪造进展。"""
        target_file = self.base_p / "chapter1.txt"
        target_file.write_text("This is chapter 1 content.", encoding="utf-8")

        baseline = TaskBaseline(
            task_id="task_mtime_test",
            original_requirements="写文章",
            session_cwd=str(self.base_p),
            effective_delivery_dir=str(self.base_p),
        )

        ev_initial = collect_evidence(self.base_p, "", baseline)
        initial_art_rev = ev_initial.artifact_revision

        # 仅修改 mtime 时间戳 (推进 10 小时)
        future_time = time.time() + 36000
        os.utime(str(target_file), (future_time, future_time))

        ev_touched = collect_evidence(self.base_p, "", baseline)
        touched_art_rev = ev_touched.artifact_revision

        self.assertEqual(
            initial_art_rev, touched_art_rev,
            "仅 touch 文件刷新 mtime 但内容完全相同时，artifact_revision 必须严格保持不变"
        )


if __name__ == "__main__":
    unittest.main()
