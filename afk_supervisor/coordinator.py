"""
afk_supervisor.coordinator — 集中式监管协调器 (DECIDE / REPAIR / REVIEW)
======================================================================
统一管理 DECIDE、REPAIR、REVIEW 状态流与证据链；
将任务内确认项交给 L2 代为决策；
基于业务产物内容哈希 (artifact_revision) 检测进展，防无进展死循环；
机械规则与本地验证双重把关，杜绝讨好型完工判定。
"""

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from afk_supervisor.models import ActionType, DeadlineBudget, EvidencePacket, L2Result
from afk_supervisor.platform.process import log
from afk_supervisor.baseline import TaskBaseline
from afk_supervisor.evidence import collect_evidence, calculate_reviewed_revision
from afk_supervisor.l2.protocol import extract_protocol_json, normalize_next_action
from afk_supervisor.l2.transport import l2_dispatch
from afk_supervisor.compat import get_sym
from afk_supervisor.verification import VerificationRunner


def _dispatch_l2(*args, **kwargs):
    fn = get_sym("l2_dispatch", l2_dispatch)
    return fn(*args, **kwargs)


def _unpack_result(result):
    """Accept dataclasses and legacy 3/4-tuples without requiring an optional payload."""
    verdict, answer, log_path = result[0], result[1], result[2]
    payload = getattr(result, "payload", None)
    if payload is None and isinstance(result, (tuple, list)) and len(result) > 3:
        payload = result[3] if isinstance(result[3], dict) else None
    if payload is None and isinstance(answer, str):
        payload = extract_protocol_json(answer)
    if payload and "next_action" in payload:
        payload["next_action"] = normalize_next_action(payload["next_action"])
    return verdict, answer, log_path, payload


class SupervisorCoordinator:
    """集中式监管协调器: 统一管理 DECIDE / REPAIR / REVIEW 状态流与证据链。"""

    def __init__(
        self,
        run_dir: Path,
        workspace_root: Path,
        delivery_dir: Path,
        task_baseline: TaskBaseline,
        agy_mgr: Any,
        l2_cmd: str,
        args: Any,
        proxy: Optional[str] = None,
        budget: Optional[DeadlineBudget] = None,
        ivl_fn: Optional[Callable[..., None]] = None,
        task_dir: Optional[Path] = None,
    ):
        self.run_dir = Path(run_dir).resolve()
        self.workspace_root = Path(workspace_root).resolve()
        self.delivery_dir = Path(delivery_dir).resolve()
        self.task_baseline = task_baseline
        self.agy_mgr = agy_mgr
        self.l2_cmd = l2_cmd
        self.args = args
        self.proxy = proxy
        self.budget = budget
        self.ivl = ivl_fn or (lambda event, **kw: None)
        self.task_dir = Path(task_dir) if task_dir else None
        self.audit_log_path = self.run_dir / "l2_audit.jsonl"
        self.consecutive_no_progress = 0
        self.consecutive_protocol_errors = 0
        self.last_artifact_revision = ""
        self.last_reviewed_revision = ""
        self.repair_count = 0
        self.last_review_evidence: Optional[EvidencePacket] = None
        self.last_review_message = ""
        self.pending_review = None
        self.pending_decision = None
        self.last_progress_revision = ""
        self.state_mgr = None
        plan = getattr(args, "approved_verification_plan", None)
        if not isinstance(plan, dict):
            plan = None
        self.verification_runner = VerificationRunner(self.run_dir, plan, budget)
        self.pending_repair = None
        self.active_consultation = None
        self.last_protocol_error = ""

    def restore_checkpoint(self):
        data = self.state_mgr.coordinator_context if self.state_mgr else {}
        for key in ("consecutive_no_progress", "consecutive_protocol_errors", "last_artifact_revision",
                    "last_reviewed_revision", "repair_count", "last_progress_revision", "active_consultation",
                    "last_protocol_error"):
            if key in data:
                setattr(self, key, data[key])
        self.pending_decision = data.get("pending_decision")
        self.pending_repair = data.get("pending_repair")

    def save_checkpoint(self):
        if self.state_mgr:
            self.state_mgr.coordinator_context = {key: getattr(self, key) for key in (
                "consecutive_no_progress", "consecutive_protocol_errors", "last_artifact_revision",
                "last_reviewed_revision", "repair_count", "last_progress_revision", "pending_decision",
                "pending_repair", "active_consultation")}
            self.state_mgr.save()

    def _begin_request(self, mode, request_id, **context):
        self.active_consultation = {"mode": mode, "request_id": request_id, **context}
        self.save_checkpoint()

    def _end_request(self, verdict):
        if verdict not in ("NO-VERDICT", "NO-BRIDGE"):
            self.active_consultation = None
        self.save_checkpoint()

    def log_audit(self, event: str, **kw):
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "event": event,
            **kw,
        }
        try:
            with open(self.audit_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _record_consultation(self, mode: str):
        if self.state_mgr is not None:
            self.state_mgr.begin_round(mode)

    def _sync_agy_session(self, driver=None):
        cid = getattr(self.agy_mgr, "cid", "") or getattr(driver, "_agy_cid", "")
        if self.state_mgr is not None and isinstance(cid, str) and cid:
            self.state_mgr.set_agy_session_id(cid)

    def check_completion(self, last_msg: str, payload: Optional[dict], verify_fn) -> Tuple[bool, str, bool]:
        """Common final gate for resume/fork/GUI: same context, fresh evidence, no writes.

        The third result requests another REVIEW instead of resuming the worker.
        A review packet must match the exact packet the coordinator sent, not an
        unrelated packet rebuilt with an empty worker message or missing task spec.
        """
        try:
            ok, detail = verify_fn(custom_last_msg=last_msg)
        except Exception as error:
            return False, f"本地验收器异常: {error}", False
        l2_enabled = bool(self.l2_cmd and self.l2_cmd.lower() not in ("off", "none"))
        if not ok and l2_enabled and not self.task_dir:
            # Preserve natural-mode semantics: wording alone is not a defect.
            if "未检测到" in detail or "未发现" in detail:
                ok, detail = True, "AGY主管客观审查达标（无需固定完工措辞）"
        if not ok:
            return False, detail, False
        reviewed = self.last_review_evidence
        if reviewed is None:
            return False, "缺少本轮审查证据，必须重新审查", True
        claimed_revision = payload.get("reviewed_revision", "") if payload else ""
        if claimed_revision and claimed_revision != reviewed.reviewed_revision:
            return False, "审查响应未绑定当前证据版本，必须重新审查", True
        fresh = collect_evidence(
            session_cwd=self.workspace_root, last_msg=self.last_review_message,
            task_baseline=self.task_baseline, task_dir=self.task_dir,
            request_id=reviewed.request_id,
            verification_runner=self.verification_runner,
        )
        if fresh.mechanical_failures or fresh.reviewed_revision != reviewed.reviewed_revision:
            self.log_audit("COMPLETION_INVALIDATED", old_rev=reviewed.reviewed_revision,
                           new_rev=fresh.reviewed_revision, failures=fresh.mechanical_failures)
            return False, "审查后产物、需求或验证结果改变，必须重新采证审查", True
        return True, detail, False

    def handle_interaction(self, last_msg: str, n_interaction: int, driver: Any = None, session_cwd: Any = None) -> Tuple[str, str, Path, Optional[dict]]:
        """处理交互决策 (mode=DECIDE): worker 提问 / 选项 / 阻塞点。
        原任务中的确认节点作为 L2 上下文，不在本地转人工。
        """
        cwd_p = Path(session_cwd or self.workspace_root).resolve()
        self._record_consultation("DECIDE")
        self.log_audit("INTERACTION_START", n=n_interaction, snippet=last_msg[:160])

        req_id = (self.pending_decision[1] if self.pending_decision and self.pending_decision[0] == last_msg
                  else f"req-decide-{n_interaction}-{uuid.uuid4().hex[:8]}")
        self.pending_decision = (last_msg, req_id)
        self._begin_request("DECIDE", req_id, last_msg=last_msg, number=n_interaction)
        res = _dispatch_l2(
            self.l2_cmd, self.args, self.proxy, self.run_dir, driver,
            str(cwd_p), n_interaction,
            request_id=req_id,
            outcome_detail=last_msg, errors_text=last_msg, kind="interaction",
            mode="DECIDE", agy_mgr=self.agy_mgr, title=getattr(driver, "title", ""),
            budget=self.budget, task_baseline=self.task_baseline,
            protocol_error_feedback=self.last_protocol_error,
        )
        self._sync_agy_session(driver)
        verdict, answer, l2_log, payload = _unpack_result(res)
        if verdict == "PROTOCOL_ERROR":
            self.last_protocol_error = answer
        else:
            self.last_protocol_error = ""

        self.pending_decision = (last_msg, req_id) if verdict in ("NO-VERDICT", "NO-BRIDGE") else None
        self._end_request(verdict)
        self.log_audit("INTERACTION_RESULT", n=n_interaction, request_id=req_id, verdict=verdict, answer=answer[:200])
        return verdict, answer, l2_log, payload

    def handle_repair(self, failure_detail: str, last_msg: str, n_repair: int, driver: Any = None, session_cwd: Any = None) -> Tuple[str, str, Path, Optional[dict]]:
        """处理异常修复 (mode=REPAIR): 语法错误 / 崩溃 / 配置损毁。
        注意: REPAIRED 决议绝不直接视为任务 SUCCESS，必须在后续轮次通过 REVIEW 重新采证核验！
        """
        self._record_consultation("REPAIR")
        self.repair_count += 1
        cwd_p = Path(session_cwd or self.workspace_root).resolve()
        self.log_audit("REPAIR_START", n=n_repair, failure=failure_detail[:160])
        context = [failure_detail, last_msg]
        req_id = (self.pending_repair[1] if self.pending_repair and self.pending_repair[0] == context
                  else f"req-repair-{n_repair}-{uuid.uuid4().hex[:8]}")
        self.pending_repair = (context, req_id)
        self._begin_request("REPAIR", req_id, last_msg=last_msg, failure_detail=failure_detail, number=n_repair)
        self.verification_runner.invalidate()
        self.last_review_evidence = None
        res = _dispatch_l2(
            self.l2_cmd, self.args, self.proxy, self.run_dir, driver,
            str(cwd_p), n_repair,
            outcome_detail=failure_detail, errors_text=failure_detail, err_tail=last_msg,
            request_id=req_id, kind="repair", mode="REPAIR", agy_mgr=self.agy_mgr, title=getattr(driver, "title", ""),
            budget=self.budget, task_baseline=self.task_baseline,
            protocol_error_feedback=self.last_protocol_error,
        )
        self._sync_agy_session(driver)
        verdict, answer, l2_log, payload = _unpack_result(res)
        if verdict == "PROTOCOL_ERROR":
            self.last_protocol_error = answer
        else:
            self.last_protocol_error = ""

        self.pending_repair = (context, req_id) if verdict in ("NO-VERDICT", "NO-BRIDGE") else None
        self._end_request(verdict)
        self.log_audit("REPAIR_RESULT", n=n_repair, verdict=verdict, answer=answer[:200])
        return verdict, answer, l2_log, payload

    def handle_turn_review(
        self,
        last_msg: str,
        n_interaction: int,
        driver: Any = None,
        session_cwd: Any = None,
        verify_fn: Optional[Callable[..., Tuple[bool, str]]] = None,
    ) -> Tuple[str, str, Path, Optional[dict]]:
        """处理轮次审查与完工验收 (mode=REVIEW)。"""
        cwd_p = Path(session_cwd or self.workspace_root).resolve()
        self._record_consultation("REVIEW")
        req_id = f"req-{n_interaction}-{uuid.uuid4().hex[:8]}"

        evidence = collect_evidence(
            session_cwd=cwd_p,
            last_msg=last_msg,
            task_baseline=self.task_baseline,
            min_mtime=0.0,
            task_dir=self.task_dir,
            request_id=req_id,
            verification_runner=self.verification_runner,
        )
        if self.pending_review and self.pending_review.reviewed_revision == evidence.reviewed_revision:
            evidence = self.pending_review
        self.last_review_evidence = evidence
        self.last_review_message = last_msg
        self.log_audit(
            "EVIDENCE_COLLECTED",
            request_id=evidence.request_id,
            revision=evidence.reviewed_revision,
            artifact_revision=evidence.artifact_revision,
            artifacts_count=len([i for i in evidence.items if i.category == "artifact"]),
            mech_failures=evidence.mechanical_failures,
        )

        # 降级模式: 若显式关闭 L2
        if not self.l2_cmd or self.l2_cmd.strip().lower() in ("off", "none"):
            if verify_fn:
                ok_acc, acc_detail = verify_fn(custom_last_msg=last_msg)
                if ok_acc and len(evidence.mechanical_failures) == 0:
                    self.log_audit("LOCAL_ACCEPT_PASS", detail=acc_detail)
                    return ("PASS", f"本地机械验收通过: {acc_detail}", self.run_dir / "local_review.log", None)
                else:
                    self.log_audit("LOCAL_ACCEPT_FAIL", detail=acc_detail)
                    return ("FAIL", f"本地机械验收未达标: {acc_detail}", self.run_dir / "local_review.log", None)
            else:
                if len(evidence.mechanical_failures) == 0:
                    return ("PASS", "本地机械核验通过", self.run_dir / "local_review.log", None)
                else:
                    return ("FAIL", f"机械核验失败: {evidence.mechanical_failures[0]}", self.run_dir / "local_review.log", None)

        self._begin_request("REVIEW", evidence.request_id, last_msg=last_msg, number=n_interaction)
        # 调用 AGY REVIEW
        res = _dispatch_l2(
            self.l2_cmd, self.args, self.proxy, self.run_dir, driver,
            str(cwd_p), n_interaction,
            outcome_detail=last_msg, errors_text=last_msg, kind="supervise",
            mode="REVIEW", agy_mgr=self.agy_mgr, title=getattr(driver, "title", ""),
            budget=self.budget, task_baseline=self.task_baseline,
            evidence_packet=evidence, request_id=evidence.request_id,
            protocol_error_feedback=self.last_protocol_error,
        )
        self._sync_agy_session(driver)
        verdict, answer, l2_log, payload = _unpack_result(res)

        self.pending_review = evidence if verdict in ("NO-VERDICT", "NO-BRIDGE") else None
        self._end_request(verdict)

        if verdict == "PROTOCOL_ERROR":
            self.consecutive_protocol_errors += 1
            self.last_protocol_error = answer
            self.log_audit(
                "REVIEW_PROTOCOL_ERROR",
                request_id=evidence.request_id,
                consecutive=self.consecutive_protocol_errors,
                reason=answer,
            )
            return ("PROTOCOL_ERROR", answer, l2_log, payload)

        self.consecutive_protocol_errors = 0
        self.last_protocol_error = ""

        # 机械刚性底线覆盖: 若存在机械故障，即使判定 PASS 也坚决驳回并转 switch_to_repair
        if len(evidence.mechanical_failures) > 0 and verdict in ("PASS", "COMPLETED"):
            mech_msg = "; ".join(evidence.mechanical_failures)
            self.log_audit(
                "REVIEW_MECHANICAL_OVERRIDE",
                original_verdict=verdict,
                failures=evidence.mechanical_failures,
            )
            verdict = "FAIL"
            override_text = f"审查发现存在代码语法或显式机械校验失败，禁止通过验收：{mech_msg}。请立即修复报错。"
            if payload and isinstance(payload.get("next_action"), dict):
                payload["next_action"]["type"] = ActionType.SWITCH_TO_REPAIR
                payload["next_action"]["instructions"] = override_text
                payload["verdict"] = "FAIL"
            answer = override_text

        # 本地验证器双重门
        if verdict in ("PASS", "COMPLETED") and verify_fn:
            try:
                ok_local, local_detail = verify_fn(custom_last_msg=last_msg)
                if not ok_local and (self.task_dir or "缺少" in local_detail or "未通过" in local_detail):
                    self.log_audit("REVIEW_LOCAL_VERIFY_OVERRIDE", detail=local_detail)
                    verdict = "FAIL"
                    override_text = f"显式验收检查未通过: {local_detail}。请继续推进并修正上述问题。"
                    if payload and isinstance(payload.get("next_action"), dict):
                        payload["next_action"]["type"] = ActionType.WORKER_INSTRUCTION
                        payload["next_action"]["instructions"] = override_text
                        payload["verdict"] = "FAIL"
                    answer = override_text
            except Exception as error:
                verdict = "INCONCLUSIVE"
                answer = f"本地验收器异常，未能建立通过证据: {error}"
                self.log_audit("REVIEW_LOCAL_VERIFY_ERROR", detail=answer)
                if payload:
                    payload["verdict"] = verdict
                    payload["next_action"] = {"type": ActionType.GATHER_EVIDENCE, "instructions": answer}

        # 记录建议维修审计
        if payload and isinstance(payload.get("next_action"), dict):
            if payload["next_action"].get("type") == ActionType.SWITCH_TO_REPAIR:
                self.log_audit("REVIEW_SUGGESTS_REPAIR", instructions=payload["next_action"].get("instructions", ""))

        # 防死循环无进展守卫: 基于业务产物内容版本 artifact_revision 判定，不受时间戳干扰
        if verdict in ("FAIL", "INCONCLUSIVE"):
            current_art_rev = evidence.artifact_revision
            progress_revision = calculate_reviewed_revision(
                evidence.task_id, self.delivery_dir,
                [item for item in evidence.items if item.category not in {"worker_statement", "progress_doc"}],
                current_art_rev, self.task_baseline, evidence.mechanical_failures,
            )
            if progress_revision == self.last_progress_revision:
                self.consecutive_no_progress += 1
            else:
                self.consecutive_no_progress = 0
            self.last_progress_revision = progress_revision
            self.last_artifact_revision = current_art_rev
            self.last_reviewed_revision = evidence.reviewed_revision

            if self.consecutive_no_progress >= 3:
                self.log_audit(
                    "REVIEW_TERMINAL_NO_PROGRESS",
                    artifact_revision=current_art_rev,
                    count=self.consecutive_no_progress,
                )
                # Escalate stagnation to L2 instead of fabricating a human decision.
                return self.handle_interaction(
                    f"连续 {self.consecutive_no_progress} 轮审查无实质进展。请判断采证、改变工作方法或有依据地停止。\n{last_msg}\n{answer}",
                    n_interaction, driver=driver, session_cwd=cwd_p,
                )

        else:
            self.consecutive_no_progress = 0
            self.last_artifact_revision = evidence.artifact_revision
            self.last_reviewed_revision = evidence.reviewed_revision

        self.log_audit("REVIEW_RESULT", request_id=evidence.request_id, verdict=verdict, answer=answer[:200])
        return verdict, answer, l2_log, payload
