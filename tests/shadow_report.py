"""Shadow 对照报告 CLI — 只读分析 runs/<id>/l2_audit.jsonl, 不影响任何监管行为。

用法:
    python -B -X utf8 tests/shadow_report.py               # 最新 run
    python -B -X utf8 tests/shadow_report.py 20260925-144105-f339   # 指定 run
    python -B -X utf8 tests/shadow_report.py --all         # 全部 run 逐个 + 总汇总
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from afk_supervisor.decisions.report import (  # noqa: E402
    build_gates,
    format_run_report,
    format_summary,
    parse_audit_events,
    summarize,
)

RUNS_ROOT = Path(__file__).resolve().parents[1] / "runs"


def audit_path(run_id: str) -> Path:
    return RUNS_ROOT / run_id / "l2_audit.jsonl"


def latest_run_id() -> str:
    candidates = sorted(
        (p for p in RUNS_ROOT.iterdir() if p.is_dir() and (p / "l2_audit.jsonl").exists()),
        key=lambda p: p.name,
    )
    if not candidates:
        raise SystemExit("runs/ 下没有包含 l2_audit.jsonl 的 run")
    return candidates[-1].name


def report_one(run_id: str) -> str:
    path = audit_path(run_id)
    if not path.exists():
        raise SystemExit(f"未找到 {path}")
    gates = build_gates(parse_audit_events(path))
    parts = [format_run_report(gates, run_id)]
    if "--all" not in sys.argv:
        parts.append(format_summary(summarize(gates)))
    return "\n".join(parts)


def main() -> int:
    argv = [a for a in sys.argv[1:] if a != "--json"]
    as_json = "--json" in sys.argv
    if argv and argv[0] == "--all":
        run_ids = sorted(p.name for p in RUNS_ROOT.iterdir()
                         if p.is_dir() and (p / "l2_audit.jsonl").exists())
        all_gates = []
        for run_id in run_ids:
            gates = build_gates(parse_audit_events(audit_path(run_id)))
            print(format_run_report(gates, run_id))
            print()
            all_gates.extend(gates)
        print(format_summary(summarize(all_gates)))
        return 0

    run_id = argv[0] if argv else latest_run_id()
    gates = build_gates(parse_audit_events(audit_path(run_id)))
    text = format_run_report(gates, run_id) + "\n" + format_summary(summarize(gates))
    if as_json:
        from afk_supervisor.decisions.report import summarize as _s
        print(json.dumps(_s(gates), ensure_ascii=False, indent=2))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
