#!/usr/bin/env python3
"""
supervise.py — AFK Supervisor 统一启动入口与公共兼容外观 (Facade)
===================================================================
作为系统的根入口与轻量门面，保留原有 CLI 命令兼容性与公共 API 导出；
核心职责已彻底模块化解耦到 afk_supervisor/ 各子模块。
"""

import os
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 根路径与数据目录常量 (支持测试热补丁 monkeypatch)
HOME = Path.home()
WS = Path(__file__).resolve().parent
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CC_SWITCH_DB = HOME / ".cc-switch" / "cc-switch.db"
CODEX_SESSIONS = HOME / ".codex" / "sessions"
CODEX_LOCKS = HOME / ".codex" / "thread-writer-locks"
CODEX_APP_DATA = Path(os.environ.get("APPDATA", "")) / "Codex"

ERROR_PATTERNS = [
    r"\bAPI Error\b", r"\bOverloaded\b", r"\boverloaded\b",
    r"\bConnection error\b", r"\bECONNRESET\b", r"\bETIMEDOUT\b", r"\bCredit balance\b",
    r"\busage limit\b", r"\brate limit\b", r"\brate_limit\b", r"\bstream error\b", r"\bdisconnected\b",
    r"\binvalid_request_error\b", r"\bauthentication_error\b",
    r"(?:status[_\s]*[:=]\s*|\bHTTP\s+|\berror\s+code\s*[:=]?\s*|\bcode\s*[:=]\s*)(?:429|500|502|503|504)\b",
]

BACKOFFS = [15, 45, 90, 120, 120, 120, 120, 120]
STALE_LIMITS = [600, 1200, 1800]
FAILS_BEFORE_SWITCH = 2

# ---------------------------------------------------------------- 模块导出与兼容别名
from afk_supervisor.models import (
    DeadlineBudget,
    AgyResponseResult,
    L2Result,
    CriterionSpec,
    EvidenceItem,
    EvidencePacket,
    ActionType,
)
from afk_supervisor.baseline import (
    TaskBaseline,
    extract_task_baseline,
    extract_explicit_delivery_dir,
)
from afk_supervisor.evidence import (
    collect_evidence,
    compute_file_sha256_short,
    calculate_reviewed_revision,
    calculate_artifact_revision,
)
from afk_supervisor.l2.protocol import (
    PROTOCOL_VERSION,
    VALID_MODES,
    VALID_VERDICTS,
    CRITERION_VERDICTS,
    FORBIDDEN_INSTRUCTION_PATTERNS,
    extract_protocol_json,
    validate_protocol_payload,
    build_protocol_prompt,
    normalize_next_action,
)
from afk_supervisor.l2.bridge import (
    AntigravityManager,
    discover_antigravity_bridge,
    discover_antigravity_project_id,
    get_skill_metadata,
    parse_verdict_from_text,
    check_agy_transcript_error,
    read_agy_latest_response,
    is_agy_working,
    wait_for_agy_idle,
)
from afk_supervisor.l2.transport import (
    clean_l2_decision_text,
    worker_last_message,
    get_recent_workspace_files,
    run_l2_antigravity,
    run_l2_agent,
    l2_dispatch,
)
from afk_supervisor.state import SupervisorState
from afk_supervisor.platform.windows import (
    keep_awake,
    set_keep_awake,
    detect_system_proxy,
    find_connected_adapter,
    net_disable,
    net_enable,
)
from afk_supervisor.platform.process import (
    log,
    munged_cwd,
    pid_is_running,
    WorkspaceSupervisorLock,
    get_codex_desktop_pids,
    codex_app_running,
    close_codex_app,
    safe_kill,
)
from afk_supervisor.platform.gui import (
    find_best_codex_window,
    reveal_codex_session_in_ui,
    inject_into_codex_gui,
    pause_codex_gui_session,
    ensure_codex_window_restored,
)
from afk_supervisor.drivers.dummy import DummyDriver
from afk_supervisor.sessions.rollout import (
    rollout_tail_state,
    peek_rollout_activity,
    is_codex_working,
    wait_for_codex_idle,
    codex_rollout_is_turn_complete,
    read_rollout_last_message,
)
from afk_supervisor.sessions.discovery import (
    clean_session_id,
    load_codex_thread_titles,
    read_session_title,
    list_recent_codex_sessions,
    find_last_codex_session,
    find_codex_session_by_id,
    register_thread_for_codex_ui,
)
from afk_supervisor.drivers.claude import ClaudeDriver, get_relay_pool, probe_pool
from afk_supervisor.drivers.codex import CodexDriver
from afk_supervisor.acceptance import (
    check_acceptance,
    check_acceptance_natural,
    check_acceptance_quick,
    _acceptance_selftest,
    is_interaction_request,
    DONE_SIGNALS,
    ASK_MARKERS,
)
from afk_supervisor.coordinator import SupervisorCoordinator
from afk_supervisor.reporting import generate_final_report, send_terminal_notification
from afk_supervisor.engine import run_headless_supervisor
from afk_supervisor.gui_engine import run_gui_supervisor
from afk_supervisor.goal_engine import run_goal_supervisor
from afk_supervisor.cli import (
    build_arg_parser,
    wait_session_quiet,
    backup_workspace,
    BACKUP_EXCLUDE_DIRS,
    main as cli_main,
)


def main(argv=None) -> int:
    """CLI 兼容包装函数。"""
    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
