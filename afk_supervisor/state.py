"""
afk_supervisor.state — 状态持久化与可恢复检查点管理器
=====================================================
跟踪记录 parent_session_id、worker_session_id、agy_session_id；
精准维护 interactions (决策代答)、reviews (审查轮次)、repairs (环境修复)、retries (故障重试)；
提供原子落盘 (tempfile + os.replace) 与检查点恢复防重机制。
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from afk_supervisor.storage import atomic_json


CHECKPOINT_FIELDS = (
    "schema_version", "run_config", "budget_elapsed_sec", "loop_context", "coordinator_context",
    "worker_pid", "worker_rollout", "parent_rollout", "launch_at", "launch_kind", "dispatch_status",
    "dispatch_offset", "dispatch_stdout_offset", "dispatch_target", "dispatch_at", "dispatch_attempts",
    "dispatch_context", "last_handled_event", "event_path", "verification_plan", "terminal_finalized",
)


class SupervisorState:
    """统一长程运行状态机与原子状态持久化管理器。"""

    def __init__(
        self,
        run_dir: Path,
        sid: str = "",
        mode: str = "fresh",
        ws: Path = None,
        parent_session_id: str = "",
        worker_session_id: str = "",
        agy_session_id: str = "",
        *,
        persist_initial: bool = True,
    ):
        self.run_dir = Path(run_dir).resolve()
        # sid 字段保持向下兼容（默认为 worker_session_id 或 sid）
        self.parent_session_id = parent_session_id or sid
        self.worker_session_id = worker_session_id or sid
        self.sid = self.worker_session_id or self.parent_session_id
        self.agy_session_id = agy_session_id
        self.agy_cid = agy_session_id  # 兼容旧代码直接访问 agy_cid

        self.mode = mode
        self.ws = str(Path(ws).resolve()) if ws else ""
        self.state = "INIT"
        self.detail = ""

        self.round = 0
        self.resumes = 0
        self.max_resumes = 8
        self.interactions = 0
        self.max_interactions = 10
        self.reviews = 0
        self.repairs = 0
        self.retries = 0

        self.pending_command = ""
        self.pending_action_type = ""
        self.last_dispatched_request_id = ""
        self.event_offset = 0
        self.updated_at = datetime.now().isoformat()
        self.schema_version = 2
        self.run_config = {}
        self.budget_elapsed_sec = 0.0
        self.loop_context = {}
        self.coordinator_context = {}
        self.worker_pid = 0
        self.worker_rollout = ""
        self.parent_rollout = ""
        self.launch_at = 0.0
        self.launch_kind = ""
        self.dispatch_status = "NONE"
        self.dispatch_offset = 0
        self.dispatch_stdout_offset = 0
        self.dispatch_target = ""
        self.dispatch_at = 0.0
        self.dispatch_attempts = 0
        self.dispatch_context = {}
        self.last_handled_event = ""
        self.event_path = ""
        self.verification_plan = {}
        self.terminal_finalized = False

        if persist_initial:
            self.save()

    def set_worker_session_id(self, worker_sid: str):
        """当 Fork 进程或新启动进程发现真实子会话 ID 时更新绑定。"""
        if worker_sid and worker_sid != self.worker_session_id:
            self.worker_session_id = worker_sid
            self.sid = worker_sid
            self.save()

    def set_agy_session_id(self, agy_cid: str):
        """当与 L2 建立会话或会话更新时同步持久化。"""
        if agy_cid and agy_cid != self.agy_session_id:
            self.agy_session_id = agy_cid
            self.agy_cid = agy_cid
            self.save()

    def transition(self, new_state: str, detail: str = "", **kwargs):
        """推进状态并持久化。"""
        self.state = new_state
        self.detail = detail
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)
        if "agy_cid" in kwargs:
            self.agy_session_id = kwargs["agy_cid"]
        if "worker_session_id" in kwargs:
            self.sid = kwargs["worker_session_id"]
        self.updated_at = datetime.now().isoformat()
        self.save()

    def begin_round(self, mode: str):
        """Record actual consultations, keeping decision/review/repair counts distinct."""
        self.round += 1
        counter = {"DECIDE": "interactions", "REVIEW": "reviews", "REPAIR": "repairs"}[mode]
        setattr(self, counter, getattr(self, counter) + 1)
        self.transition({"DECIDE": "DECIDING", "REVIEW": "REVIEWING", "REPAIR": "REPAIRING"}[mode])

    def acknowledge_command(self):
        """Clear the pending text only after worker activity/acceptance is observed."""
        self.pending_command = ""
        self.pending_action_type = ""
        self.dispatch_status = "ACCEPTED"
        self.save()

    def record_dispatched(self, request_id: str, action_type: str, command: str = "", **context):
        """记录已分派动作，杜绝故障恢复时无条件重复发送。"""
        self.last_dispatched_request_id = request_id
        self.pending_action_type = action_type
        self.pending_command = command
        self.dispatch_status = "PREPARED"
        self.dispatch_attempts = 0
        self.dispatch_context = context.pop("dispatch_context", {})
        for key, value in context.items():
            if not hasattr(self, key):
                raise ValueError("Unknown dispatch checkpoint field: " + key)
            setattr(self, key, value)
        self.updated_at = datetime.now().isoformat()
        self.save()

    def should_dispatch(self, request_id: str) -> bool:
        """核验该请求是否已分派，避免重复执行。"""
        if not request_id:
            return True
        return self.last_dispatched_request_id != request_id or self.dispatch_status == "NOT_SENT"

    def mark_delivery(self, status, **fields):
        if status not in {"PREPARED", "NOT_SENT", "SENDING", "SENT", "UNCERTAIN", "ACCEPTED", "FAILED"}:
            raise ValueError("Unknown delivery status: " + status)
        self.dispatch_status = status
        for key, value in fields.items():
            if not hasattr(self, key):
                raise ValueError(key)
            setattr(self, key, value)
        self.save()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "detail": self.detail,
            "sid": self.sid,
            "parent_session_id": self.parent_session_id,
            "worker_session_id": self.worker_session_id,
            "agy_session_id": self.agy_session_id,
            "agy_cid": self.agy_session_id,
            "mode": self.mode,
            "workspace": self.ws,
            "round": self.round,
            "resumes": self.resumes,
            "max_resumes": self.max_resumes,
            "interactions": self.interactions,
            "max_interactions": self.max_interactions,
            "reviews": self.reviews,
            "repairs": self.repairs,
            "retries": self.retries,
            "pending_command": self.pending_command,
            "pending_action_type": self.pending_action_type,
            "last_dispatched_request_id": self.last_dispatched_request_id,
            "event_offset": self.event_offset,
            "updated_at": self.updated_at,
            **{key: getattr(self, key) for key in CHECKPOINT_FIELDS},
        }

    def save(self):
        """原子持久化检查点到 supervisor_state.json。"""
        atomic_json(self.run_dir / "supervisor_state.json", self.to_dict())

    @classmethod
    def load(cls, run_dir: Path) -> Optional["SupervisorState"]:
        """从检查点文件安全加载状态。"""
        target = Path(run_dir) / "supervisor_state.json"
        if not target.exists():
            return None
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            inst = cls(
                run_dir=run_dir,
                sid=data.get("sid", ""),
                mode=data.get("mode", "fresh"),
                ws=Path(data["workspace"]) if data.get("workspace") else None,
                parent_session_id=data.get("parent_session_id", ""),
                worker_session_id=data.get("worker_session_id", ""),
                agy_session_id=data.get("agy_session_id", "") or data.get("agy_cid", ""),
                persist_initial=False,
            )
            inst.state = data.get("state", "INIT")
            inst.detail = data.get("detail", "")
            inst.round = data.get("round", 0)
            inst.resumes = data.get("resumes", 0)
            inst.max_resumes = data.get("max_resumes", 8)
            inst.interactions = data.get("interactions", 0)
            inst.max_interactions = data.get("max_interactions", 10)
            inst.reviews = data.get("reviews", 0)
            inst.repairs = data.get("repairs", 0)
            inst.retries = data.get("retries", 0)
            inst.pending_command = data.get("pending_command", "")
            inst.pending_action_type = data.get("pending_action_type", "")
            inst.last_dispatched_request_id = data.get("last_dispatched_request_id", "")
            inst.event_offset = data.get("event_offset", 0)
            inst.updated_at = data.get("updated_at", "")
            for key in CHECKPOINT_FIELDS:
                if key in data:
                    setattr(inst, key, data[key])
            if "dispatch_status" not in data and inst.pending_command:
                inst.dispatch_status = "UNCERTAIN"
            return inst
        except Exception:
            return None
