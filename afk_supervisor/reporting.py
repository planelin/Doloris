"""
afk_supervisor.reporting — 终态审计报告与通知推送
=================================================
基于真实 interventions.jsonl 时间线与 SupervisorState 状态生成 report.md；
支持通过 AFK_WEBHOOK_URL 推送钉钉/飞书/企微/Bark 卡片消息。
"""

import json
import os
from pathlib import Path
from typing import List, Optional

from afk_supervisor.platform.process import log
from afk_supervisor.baseline import TaskBaseline


def send_terminal_notification(state: str, detail: str, report_path: Path, title: str = ""):
    """向配置的 Webhook 发送任务终态通知。"""
    webhook_url = os.environ.get("AFK_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return
    try:
        import urllib.request
        body = {
            "msg_type": "text",
            "content": {
                "text": f"【AFK 托管终态通知】\n状态: {state}\n详情: {detail}\n任务: {title or '未命名任务'}\n报告: {report_path}"
            },
            "text": f"【AFK 托管终态: {state}】\n{detail}\n报告: {report_path}"
        }
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            pass
        log("NOTIFY   终态通知已成功投递至 Webhook")
    except Exception as e:
        log(f"NOTIFY   Webhook 通知投递失败: {e}")


def generate_final_report(
    run_dir: Path,
    state: str,
    detail: str,
    ts: str,
    session_id: str,
    jsonl_path: Optional[Path],
    providers_tried: List[str],
    resumes: int,
    chaos: Optional[object],
    ivl_path: Path,
    title: str = "",
    baseline: Optional[TaskBaseline] = None,
) -> Path:
    """基于真实运行记录与基线生成标准 report.md。"""
    report_file = run_dir / "report.md"
    req_deliv = baseline.requested_delivery_dir if baseline else ""
    eff_deliv = baseline.effective_delivery_dir if baseline else ""

    lines = [
        f"# 监管运行报告 — {ts}",
        "",
        f"- 终态: **{state}**",
        f"- 会话: `{session_id}`",
        f"- 任务: {title or (baseline.original_requirements[:60] if baseline else '未命名')}",
        f"- 请求交付目录: `{req_deliv}`",
        f"- 实际生效交付目录: `{eff_deliv or '(未生效/阻塞)'}`",
        f"- 供应商尝试顺序: {' → '.join(providers_tried)}",
        f"- 干预/续跑次数: {resumes} (chaos注入: {chaos})",
        f"- 验收详情: {detail}",
        "- 详细时间线: interventions.jsonl",
        "- worker日志: worker-stdout.log / worker-stderr.log",
        f"- 会话轨迹: {jsonl_path or '无'}",
        "",
        "## 时间线",
    ]

    if ivl_path.exists():
        for l in ivl_path.read_text(encoding="utf-8", errors="replace").strip().splitlines():
            try:
                r = json.loads(l)
                payload_str = json.dumps({k: v for k, v in r.items() if k not in ("ts", "event")}, ensure_ascii=False)
                lines.append(f"- `{r['ts']}` **{r['event']}** {payload_str}")
            except Exception:
                pass

    lines.append("")
    lines.append("## 系统动作")
    lines.append("- v0.2: 此处本应执行关机 (`shutdown /s /t 60`)，已跳过")
    lines.append("")

    report_content = "\n".join(lines)
    report_file.write_text(report_content, encoding="utf-8")
    log(f"REPORT   {report_file}")
    send_terminal_notification(state, detail, report_file, title=title)
    return report_file
