# 监管运行报告 — 20260912-224449

- 终态: **SUCCESS**
- 会话: `01a09613-4efd-7af2-8665-8b5da21ba98f`
- 供应商尝试顺序: codex(cc-switch本地代理)
- 干预/续跑次数: 0 (chaos注入: ('kill', 60))
- 验收: 全部满足
- 详细时间线: interventions.jsonl
- worker日志: worker-stdout.log / worker-stderr.log
- 会话轨迹: None

## 时间线
- `2026-09-12T22:44:49` **EGRESS** {"proxy": "http://127.0.0.1:7897", "driver": "codex"}
- `2026-09-12T22:44:49` **ADOPT** {"session": "01a09613-4efd-7af2-8665-8b5da21ba98f", "rollout": "rollout-2026-09-12T22-44-03-01a09613-4efd-7af2-8665-8b5da21b", "session_cwd": "C:\\Agents\\zcode\\double"}
- `2026-09-12T22:45:04` **LAUNCH** {"session": "01a09613-4efd-7af2-8665-8b5da21ba98f", "provider": "codex(cc-switch本地代理)", "chaos": "('kill', 60)"}
- `2026-09-12T22:45:04` **ADOPTED_RESUME** {"pid": 32140}
- `2026-09-12T22:45:09` **HEARTBEAT** {"alive": true, "stale_sec": 5}
- `2026-09-12T22:45:39` **HEARTBEAT** {"alive": true, "stale_sec": 35}
- `2026-09-12T22:45:54` **EXIT_OK** {"acceptance": "全部满足"}
- `2026-09-12T22:45:54` **TERMINAL** {"state": "SUCCESS", "detail": "全部满足"}

## 系统动作
- v0.2: 此处本应执行关机 (`shutdown /s /t 60`), 已跳过
