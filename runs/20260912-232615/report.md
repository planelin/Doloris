# 监管运行报告 — 20260912-232615

- 终态: **SUCCESS**
- 会话: `01a09637-c2e9-7291-8437-3e5e56aaa3a3`
- 供应商尝试顺序: codex(cc-switch本地代理)
- 干预/续跑次数: 0 (chaos注入: ('kill', 30))
- 验收: 快速验收: 勾选12, 未勾0
- 详细时间线: interventions.jsonl
- worker日志: worker-stdout.log / worker-stderr.log
- 会话轨迹: None

## 时间线
- `2026-09-12T23:26:15` **EGRESS** {"proxy": "http://127.0.0.1:7897", "driver": "codex"}
- `2026-09-12T23:26:16` **ADOPT** {"session": "01a09637-c2e9-7291-8437-3e5e56aaa3a3", "rollout": "rollout-2026-09-12T23-25-45-01a09637-c2e9-7291-8437-3e5e56aa", "session_cwd": "C:\\Agents\\zcode\\double"}
- `2026-09-12T23:27:46` **LAUNCH** {"session": "01a09637-c2e9-7291-8437-3e5e56aaa3a3", "provider": "codex(cc-switch本地代理)", "chaos": "('kill', 30)"}
- `2026-09-12T23:27:46` **ADOPTED_RESUME** {"pid": 19864}
- `2026-09-12T23:27:51` **HEARTBEAT** {"alive": false, "stale_sec": 5}
- `2026-09-12T23:27:51` **EXIT_CRASH** {"rc": 1, "provider": "codex(cc-switch本地代理)"}
- `2026-09-12T23:27:51` **TERMINAL** {"state": "SUCCESS", "detail": "验收通过(免唤醒): 快速验收: 勾选12, 未勾0"}

## 系统动作
- v0.2: 此处本应执行关机 (`shutdown /s /t 60`), 已跳过
