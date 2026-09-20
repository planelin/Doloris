"""
afk_protocol.py — AFK 结构化协议规范与证据/基线数据引擎兼容适配层
=============================================================================
保留原有顶层公共 API 与类型符号导出，保持向后兼容性；
底层具体实现由 afk_supervisor.baseline、afk_supervisor.evidence 及 afk_supervisor.l2.protocol 驱动。
"""

from afk_supervisor.models import (
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
    get_skill_metadata,
)

__all__ = [
    "PROTOCOL_VERSION",
    "VALID_MODES",
    "VALID_VERDICTS",
    "CRITERION_VERDICTS",
    "FORBIDDEN_INSTRUCTION_PATTERNS",
    "CriterionSpec",
    "TaskBaseline",
    "EvidenceItem",
    "EvidencePacket",
    "ActionType",
    "calculate_reviewed_revision",
    "calculate_artifact_revision",
    "compute_file_sha256_short",
    "get_skill_metadata",
    "extract_protocol_json",
    "validate_protocol_payload",
    "build_protocol_prompt",
    "normalize_next_action",
    "extract_task_baseline",
    "extract_explicit_delivery_dir",
    "collect_evidence",
]
