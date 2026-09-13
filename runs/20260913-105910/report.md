# 监管运行报告 — 20260913-105910

- 终态: **FAILED**
- 会话: `01a09646-9d87-77f2-b5fc-d903e91d3223`
- 供应商尝试顺序: codex(cc-switch本地代理)
- 干预/续跑次数: 1 (chaos注入: None)
- 验收: afk-work/PROGRESS.md 不存在(worker未建立清单)
- 详细时间线: interventions.jsonl
- worker日志: worker-stdout.log / worker-stderr.log
- 会话轨迹: None

## 时间线
- `2026-09-13T10:59:10` **EGRESS** {"proxy": "http://127.0.0.1:7897", "driver": "codex"}
- `2026-09-13T10:59:10` **ADOPT** {"session": "01a09646-9d87-77f2-b5fc-d903e91d3223", "rollout": "rollout-2026-09-12T23-43-59-01a09646-9d87-77f2-b5fc-d903e91d", "session_cwd": "C:\\Agents\\zcode\\double"}
- `2026-09-13T10:59:25` **LAUNCH** {"session": "01a09646-9d87-77f2-b5fc-d903e91d3223", "provider": "codex(cc-switch本地代理)", "chaos": "off"}
- `2026-09-13T10:59:25` **ADOPTED_RESUME** {"pid": 31824}
- `2026-09-13T10:59:31` **HEARTBEAT** {"alive": true, "stale_sec": 5}
- `2026-09-13T11:00:01` **HEARTBEAT** {"alive": true, "stale_sec": 35}
- `2026-09-13T11:00:31` **HEARTBEAT** {"alive": true, "stale_sec": 65}
- `2026-09-13T11:01:01` **HEARTBEAT** {"alive": true, "stale_sec": 95}
- `2026-09-13T11:01:31` **HEARTBEAT** {"alive": true, "stale_sec": 126}
- `2026-09-13T11:01:36` **EXIT_OK** {"acceptance": "afk-work/PROGRESS.md 不存在(worker未建立清单)"}
- `2026-09-13T11:01:36` **RESUME_WAIT** {"backoff_sec": 15, "attempt": 1, "reason": "early_exit", "provider": "codex(cc-switch本地代理)"}
- `2026-09-13T11:01:51` **RESUMED** {"attempt": 1, "pid": 5584, "provider": "codex(cc-switch本地代理)"}
- `2026-09-13T11:02:01` **HEARTBEAT** {"alive": true, "stale_sec": 10}
- `2026-09-13T11:02:31` **HEARTBEAT** {"alive": true, "stale_sec": 40}
- `2026-09-13T11:03:02` **HEARTBEAT** {"alive": true, "stale_sec": 70}
- `2026-09-13T11:03:32` **HEARTBEAT** {"alive": true, "stale_sec": 101}
- `2026-09-13T11:03:52` **EXIT_OK** {"acceptance": "afk-work/PROGRESS.md 不存在(worker未建立清单)"}
- `2026-09-13T11:03:52` **TERMINAL** {"state": "FAILED", "detail": "矛盾态: worker连续2次空转退出但验收不通过(afk-work/PROGRESS.md 不存在(worker未建立清单)) — 需人工/L2介入"}

## 系统动作
- v0.2: 此处本应执行关机 (`shutdown /s /t 60`), 已跳过
