# 监管运行报告 — 20260912-233311

- 终态: **SUCCESS**
- 会话: `01a0963d-a1e1-7293-a93a-c046150db014`
- 供应商尝试顺序: codex(cc-switch本地代理)
- 干预/续跑次数: 0 (chaos注入: ('kill', 30))
- 验收: 快速验收: 勾选12, 未勾0
- 详细时间线: interventions.jsonl
- worker日志: worker-stdout.log / worker-stderr.log
- 会话轨迹: None

## 时间线
- `2026-09-12T23:33:11` **EGRESS** {"proxy": "http://127.0.0.1:7897", "driver": "codex"}
- `2026-09-12T23:33:11` **ADOPT** {"session": "01a0963d-a1e1-7293-a93a-c046150db014", "rollout": "rollout-2026-09-12T23-30-16-01a0963d-a1e1-7293-a93a-c046150d", "session_cwd": "C:\\Agents\\zcode\\double"}
- `2026-09-12T23:33:26` **LAUNCH** {"session": "01a0963d-a1e1-7293-a93a-c046150db014", "provider": "codex(cc-switch本地代理)", "chaos": "('kill', 30)"}
- `2026-09-12T23:33:26` **ADOPTED_RESUME** {"pid": 2740}
- `2026-09-12T23:33:31` **HEARTBEAT** {"alive": false, "stale_sec": 5}
- `2026-09-12T23:33:31` **EXIT_CRASH** {"rc": 1, "provider": "codex(cc-switch本地代理)"}
- `2026-09-12T23:33:31` **TERMINAL** {"state": "SUCCESS", "detail": "验收通过(免唤醒): 快速验收: 勾选12, 未勾0"}

## 系统动作
- v0.2: 此处本应执行关机 (`shutdown /s /t 60`), 已跳过
