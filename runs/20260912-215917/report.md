# 监管运行报告 — 20260912-215917

- 终态: **SUCCESS**
- 会话: `01a095ea-55a3-7643-b4f8-bc71782c8cef`
- 供应商尝试顺序: codex(cc-switch本地代理)
- 干预/续跑次数: 1 (chaos注入: ('kill', 30))
- 验收: PROGRESS勾选=12/12, 缺失章节=无
- 详细时间线: interventions.jsonl
- worker日志: worker-stdout.log / worker-stderr.log
- 会话轨迹: None

## 时间线
- `2026-09-12T21:59:17` **EGRESS** {"proxy": "(直连)", "driver": "codex"}
- `2026-09-12T21:59:17` **LAUNCH** {"session": "(运行时发现)", "provider": "codex(cc-switch本地代理)", "chaos": "('kill', 30)"}
- `2026-09-12T21:59:17` **SPAWNED** {"pid": 25512}
- `2026-09-12T21:59:22` **HEARTBEAT** {"alive": true, "stale_sec": 5}
- `2026-09-12T21:59:27` **ERROR_SIGNATURE** {"patterns": ["rate_limit"]}
- `2026-09-12T21:59:37` **ERROR_SIGNATURE** {"patterns": ["429"]}
- `2026-09-12T21:59:47` **CHAOS_KILL** {"at_sec": 30.0}
- `2026-09-12T21:59:52` **HEARTBEAT** {"alive": false, "stale_sec": 35}
- `2026-09-12T21:59:52` **EXIT_CRASH** {"rc": 1, "provider": "codex(cc-switch本地代理)"}
- `2026-09-12T21:59:52` **RESUME_WAIT** {"backoff_sec": 15, "attempt": 1, "reason": "crash", "provider": "codex(cc-switch本地代理)"}
- `2026-09-12T22:00:07` **RESUMED** {"attempt": 1, "pid": 22784, "provider": "codex(cc-switch本地代理)"}
- `2026-09-12T22:00:22` **HEARTBEAT** {"alive": true, "stale_sec": 15}
- `2026-09-12T22:00:52` **HEARTBEAT** {"alive": true, "stale_sec": 45}
- `2026-09-12T22:01:23` **HEARTBEAT** {"alive": true, "stale_sec": 75}
- `2026-09-12T22:01:48` **EXIT_OK** {"acceptance": "PROGRESS勾选=12/12, 缺失章节=无"}
- `2026-09-12T22:01:48` **TERMINAL** {"state": "SUCCESS", "detail": "PROGRESS勾选=12/12, 缺失章节=无"}

## 系统动作
- v0.2: 此处本应执行关机 (`shutdown /s /t 60`), 已跳过
