"""只读 GUI 身份探针诊断: 不点击、不输入、不写剪贴板、不发送任何指令。

用法 (在仓库根目录):
    python tests/check_gui_probe.py
    python tests/check_gui_probe.py --sid 01a0cc11-580a-7ca0-97bf-8d44b2ae978f
    python tests/check_gui_probe.py --hwnd 7736016 --timeout-ms 15000

gui 模式"接管之后无任何反应"时先跑这个脚本: 它做一次 UI Automation 身份自检,
把机读判断 (reason / proc_open_error / dom_nodes) 与处置建议直接打出来。
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from afk_supervisor.platform.gui import (  # noqa: E402
    find_best_codex_window,
    find_gui_inject_script,
    gui_probe_blocker,
    probe_codex_gui,
    resolve_target_titles,
)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description="只读 GUI 身份探针诊断")
    parser.add_argument("--sid", default="", help="目标会话 SID; 留空则只按窗口探测")
    parser.add_argument("--hwnd", type=int, default=0, help="目标窗口句柄; 留空则自动发现桌面端主窗口")
    parser.add_argument("--title", default="", help="附加标题候选; 留空则由会话索引推导")
    parser.add_argument("--timeout-ms", type=int, default=12000, help="探针超时毫秒数")
    args = parser.parse_args()

    script = find_gui_inject_script()
    hwnd = args.hwnd or find_best_codex_window()
    sid = args.sid.strip()
    title = args.title
    if sid and not title:
        title = resolve_target_titles(sid, "", None)
    print(f"script : {script}")
    print(f"hwnd   : {hwnd}   sid: {sid or '-'}")
    if script is None or not hwnd:
        print("结论: 前置条件不满足 (缺少注入脚本或桌面端主窗口未找到), 未发送任何输入")
        return 1

    probe = probe_codex_gui(target_hwnd=hwnd, target_sid=sid, target_title=title,
                            timeout_ms=args.timeout_ms)
    print(json.dumps(probe, ensure_ascii=False, indent=2))
    blocker = gui_probe_blocker(probe)
    if blocker:
        print(f"结论: 已阻断, 未发送 - {blocker}")
        return 1
    reason = str(probe.get("reason") or "")
    if reason == "IDENTITY_OK":
        print("结论: 目标会话身份匹配, gui 模式可以注入")
        return 0
    if reason == "TARGET_NOT_FOCUSED":
        print("结论: 窗口可达但未聚焦; 注入脚本会先深链导航再重试")
        return 0
    print(f"结论: 探针未能给出确定判断 (reason={reason or '无回执'}), 注入脚本仍会自行把关")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
