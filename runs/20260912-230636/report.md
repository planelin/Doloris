# 监管运行报告 — 20260912-230636

- 终态: **SUCCESS**
- 会话: `01a09627-c44d-7503-958d-c7fdda041c09`
- 供应商尝试顺序: codex(cc-switch本地代理)
- 干预/续跑次数: 0 (chaos注入: None)
- 验收: 快速验收: 勾选12, 未勾0
- 详细时间线: interventions.jsonl
- worker日志: worker-stdout.log / worker-stderr.log
- 会话轨迹: None

## 时间线
- `2026-09-12T23:06:36` **EGRESS** {"proxy": "http://127.0.0.1:7897", "driver": "codex"}
- `2026-09-12T23:06:36` **ADOPT** {"session": "01a09627-c44d-7503-958d-c7fdda041c09", "rollout": "rollout-2026-09-12T23-06-23-01a09627-c44d-7503-958d-c7fdda04", "session_cwd": "C:\\Agents\\zcode\\double"}
- `2026-09-12T23:06:51` **LAUNCH** {"session": "01a09627-c44d-7503-958d-c7fdda041c09", "provider": "codex(cc-switch本地代理)", "chaos": "off"}
- `2026-09-12T23:06:51` **ADOPTED_RESUME** {"pid": 32256}
- `2026-09-12T23:06:56` **HEARTBEAT** {"alive": true, "stale_sec": 5}
- `2026-09-12T23:07:26` **HEARTBEAT** {"alive": true, "stale_sec": 35}
- `2026-09-12T23:07:56` **HEARTBEAT** {"alive": true, "stale_sec": 65}
- `2026-09-12T23:08:06` **EXIT_OK** {"acceptance": "快速验收: 勾选12, 未勾0"}
- `2026-09-12T23:08:06` **TERMINAL** {"state": "SUCCESS", "detail": "快速验收: 勾选12, 未勾0"}

## 系统动作
- v0.2: 此处本应执行关机 (`shutdown /s /t 60`), 已跳过
