# 监管运行报告 — 20260912-221150

- 终态: **SUCCESS**
- 会话: `01a095f5-d447-7f43-98fa-2bfd43eb67ef`
- 供应商尝试顺序: codex(cc-switch本地代理)
- 干预/续跑次数: 0 (chaos注入: None)
- 验收: 全部满足
- 详细时间线: interventions.jsonl
- worker日志: worker-stdout.log / worker-stderr.log
- 会话轨迹: C:\Users\lastnut\.codex\sessions\2026\09\12\rollout-2026-09-12T22-11-51-01a095f5-d447-7f43-98fa-2bfd43eb67ef.jsonl

## 时间线
- `2026-09-12T22:11:50` **EGRESS** {"proxy": "(直连)", "driver": "codex"}
- `2026-09-12T22:11:50` **LAUNCH** {"session": "(运行时发现)", "provider": "codex(cc-switch本地代理)", "chaos": "off"}
- `2026-09-12T22:11:50` **SPAWNED** {"pid": 30616}
- `2026-09-12T22:11:55` **HEARTBEAT** {"alive": true, "stale_sec": 5}
- `2026-09-12T22:12:00` **ERROR_SIGNATURE** {"patterns": ["500", "rate_limit"]}
- `2026-09-12T22:12:05` **ERROR_SIGNATURE** {"patterns": ["rate_limit"]}
- `2026-09-12T22:12:25` **HEARTBEAT** {"alive": true, "stale_sec": 35}
- `2026-09-12T22:12:30` **EXIT_OK** {"acceptance": "全部满足"}
- `2026-09-12T22:12:30` **TERMINAL** {"state": "SUCCESS", "detail": "全部满足"}

## 系统动作
- v0.2: 此处本应执行关机 (`shutdown /s /t 60`), 已跳过
