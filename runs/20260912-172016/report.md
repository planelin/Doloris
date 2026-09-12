# 监管运行报告 — 20260912-172016

- 终态: **FAILED**
- 会话: `db83966f-dc0c-4a2a-b953-659d15e6a0b1`
- 供应商尝试顺序: agO oringin → hui → genspark → kk → or
- 干预/续跑次数: 8 (chaos注入: ('kill', 120))
- 验收: PROGRESS.md 不存在
- 详细时间线: interventions.jsonl
- worker日志: worker-stdout.log / worker-stderr.log
- 会话轨迹: C:\Users\lastnut\.claude\projects\C-Agents-zcode-double\db83966f-dc0c-4a2a-b953-659d15e6a0b1.jsonl

## 时间线
- `2026-09-12T17:20:16` **LAUNCH** {"session": "db83966f-dc0c-4a2a-b953-659d15e6a0b1", "provider": "agO oringin", "chaos": "('kill', 120)", "pool_size": 11}
- `2026-09-12T17:20:16` **SPAWNED** {"pid": 688}
- `2026-09-12T17:20:21` **HEARTBEAT** {"alive": true, "stale_sec": 5}
- `2026-09-12T17:20:51` **HEARTBEAT** {"alive": true, "stale_sec": 35}
- `2026-09-12T17:21:21` **HEARTBEAT** {"alive": true, "stale_sec": 65}
- `2026-09-12T17:21:51` **HEARTBEAT** {"alive": true, "stale_sec": 95}
- `2026-09-12T17:22:16` **CHAOS_KILL** {"at_sec": 120.0}
- `2026-09-12T17:22:21` **HEARTBEAT** {"alive": false, "stale_sec": 125}
- `2026-09-12T17:22:21` **RESUME_WAIT** {"backoff_sec": 15, "attempt": 1, "reason": "crash", "provider": "agO oringin"}
- `2026-09-12T17:22:36` **RESUMED** {"attempt": 1, "pid": 4116, "provider": "agO oringin"}
- `2026-09-12T17:22:51` **HEARTBEAT** {"alive": true, "stale_sec": 15}
- `2026-09-12T17:23:21` **HEARTBEAT** {"alive": true, "stale_sec": 45}
- `2026-09-12T17:23:51` **HEARTBEAT** {"alive": true, "stale_sec": 75}
- `2026-09-12T17:24:21` **HEARTBEAT** {"alive": true, "stale_sec": 105}
- `2026-09-12T17:24:51` **HEARTBEAT** {"alive": true, "stale_sec": 135}
- `2026-09-12T17:25:21` **HEARTBEAT** {"alive": true, "stale_sec": 165}
- `2026-09-12T17:25:51` **HEARTBEAT** {"alive": true, "stale_sec": 195}
- `2026-09-12T17:26:21` **HEARTBEAT** {"alive": true, "stale_sec": 225}
- `2026-09-12T17:26:46` **PROVIDER_SWITCH** {"to": "hui", "after_failures": 2, "wait_sec": 10}
- `2026-09-12T17:26:46` **RESUME_WAIT** {"backoff_sec": 10, "attempt": 2, "reason": "crash", "provider": "hui"}
- `2026-09-12T17:26:56` **RESUMED** {"attempt": 2, "pid": 27028, "provider": "hui"}
- `2026-09-12T17:27:01` **HEARTBEAT** {"alive": false, "stale_sec": 5}
- `2026-09-12T17:27:01` **RESUME_WAIT** {"backoff_sec": 90, "attempt": 3, "reason": "crash", "provider": "hui"}
- `2026-09-12T17:28:31` **RESUMED** {"attempt": 3, "pid": 30852, "provider": "hui"}
- `2026-09-12T17:28:36` **HEARTBEAT** {"alive": false, "stale_sec": 5}
- `2026-09-12T17:28:36` **PROVIDER_SWITCH** {"to": "genspark", "after_failures": 2, "wait_sec": 10}
- `2026-09-12T17:28:36` **RESUME_WAIT** {"backoff_sec": 10, "attempt": 4, "reason": "crash", "provider": "genspark"}
- `2026-09-12T17:28:46` **RESUMED** {"attempt": 4, "pid": 31260, "provider": "genspark"}
- `2026-09-12T17:28:51` **RESUME_WAIT** {"backoff_sec": 120, "attempt": 5, "reason": "crash", "provider": "genspark"}
- `2026-09-12T17:30:51` **RESUMED** {"attempt": 5, "pid": 31476, "provider": "genspark"}
- `2026-09-12T17:30:56` **HEARTBEAT** {"alive": false, "stale_sec": 5}
- `2026-09-12T17:30:57` **PROVIDER_SWITCH** {"to": "kk", "after_failures": 2, "wait_sec": 10}
- `2026-09-12T17:30:57` **RESUME_WAIT** {"backoff_sec": 10, "attempt": 6, "reason": "crash", "provider": "kk"}
- `2026-09-12T17:31:07` **RESUMED** {"attempt": 6, "pid": 32748, "provider": "kk"}
- `2026-09-12T17:31:12` **RESUME_WAIT** {"backoff_sec": 120, "attempt": 7, "reason": "crash", "provider": "kk"}
- `2026-09-12T17:33:12` **RESUMED** {"attempt": 7, "pid": 15172, "provider": "kk"}
- `2026-09-12T17:33:17` **HEARTBEAT** {"alive": false, "stale_sec": 5}
- `2026-09-12T17:33:17` **PROVIDER_SWITCH** {"to": "or", "after_failures": 2, "wait_sec": 10}
- `2026-09-12T17:33:17` **RESUME_WAIT** {"backoff_sec": 10, "attempt": 8, "reason": "crash", "provider": "or"}
- `2026-09-12T17:33:27` **RESUMED** {"attempt": 8, "pid": 31064, "provider": "or"}
- `2026-09-12T17:33:32` **TERMINAL** {"state": "FAILED", "detail": "resume预算耗尽(8次), 最后状态=crash"}

## 系统动作
- v0.2: 此处本应执行关机 (`shutdown /s /t 60`), 已跳过
