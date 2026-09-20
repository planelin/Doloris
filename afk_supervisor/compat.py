"""
afk_supervisor.compat — 动态符号解析器与向后兼容桥接
===================================================
当外部测试或脚本对 supervise 顶层模块进行 monkeypatch 或 patch.object 时，
通过动态符号解析器无缝穿透，确保子模块行为与单体架构保持 100% 行为一致。
"""

import sys
from typing import Any


def get_sym(name: str, default: Any = None) -> Any:
    """动态获取 supervise 模块上的符号；若未被 patch 或不存在，回退到默认实现。"""
    sup = sys.modules.get("supervise")
    if sup is not None and hasattr(sup, name):
        return getattr(sup, name)
    return default
