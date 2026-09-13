# 监管运行报告 — 20260913-135440

- 终态: **FAILED**
- 会话: `01a09954-f9c1-72a3-b3f6-845bcb108aba`
- 供应商尝试顺序: codex(cc-switch本地代理)
- 干预/续跑次数: 1 (chaos注入: None)
- 验收: afk-work/PROGRESS.md 不存在(worker未建立清单)
- 详细时间线: interventions.jsonl
- worker日志: worker-stdout.log / worker-stderr.log
- 会话轨迹: None

## 时间线
- `2026-09-13T13:54:40` **EGRESS** {"proxy": "http://127.0.0.1:7897", "driver": "codex"}
- `2026-09-13T13:54:40` **ADOPT** {"session": "01a09954-f9c1-72a3-b3f6-845bcb108aba", "rollout": "rollout-2026-09-13T13-54-38-01a09954-f9c1-72a3-b3f6-845bcb10", "session_cwd": "C:\\Agents\\zcode\\double"}
- `2026-09-13T13:54:55` **LAUNCH** {"session": "01a09954-f9c1-72a3-b3f6-845bcb108aba", "provider": "codex(cc-switch本地代理)", "chaos": "off"}
- `2026-09-13T13:54:55` **ARCHIVE_STALE_CHECKLIST** {"dest": "C:\\Agents\\zcode\\double\\afk-work\\archive\\PROGRESS-20260913-135440.md"}
- `2026-09-13T13:54:55` **ADOPTED_RESUME** {"pid": 30508}
- `2026-09-13T13:55:00` **HEARTBEAT** {"alive": false, "stale_sec": 5}
- `2026-09-13T13:55:00` **EXIT_CRASH** {"rc": 1, "provider": "codex(cc-switch本地代理)", "err": "Error: thread/resume: thread/resume failed: thread 01a09954-f9c1-72a3-b3f6-845bcb108aba already has an active writer (code -32600)"}
- `2026-09-13T13:55:00` **SESSION_BUSY** {"hint": "原会话仍被桌面/界面占用(codex单写者锁)。请停止或关闭原Codex界面中的该会话, 看门狗将耐心重试(不占续跑预算)"}
- `2026-09-13T13:55:00` **BUSY_WAIT** {"wait_sec": 20, "n": 1, "lock": "held"}
- `2026-09-13T13:55:25` **EXIT_CRASH** {"rc": 1, "provider": "codex(cc-switch本地代理)", "err": "Error: thread/resume: thread/resume failed: thread 01a09954-f9c1-72a3-b3f6-845bcb108aba already has an active writer (code -32600)"}
- `2026-09-13T13:55:25` **SESSION_BUSY** {"hint": "原会话仍被桌面/界面占用(codex单写者锁)。请停止或关闭原Codex界面中的该会话, 看门狗将耐心重试(不占续跑预算)"}
- `2026-09-13T13:55:50` **HEARTBEAT** {"alive": false, "stale_sec": 55}
- `2026-09-13T13:55:50` **EXIT_CRASH** {"rc": 1, "provider": "codex(cc-switch本地代理)", "err": "Error: thread/resume: thread/resume failed: thread 01a09954-f9c1-72a3-b3f6-845bcb108aba already has an active writer (code -32600)"}
- `2026-09-13T13:55:50` **SESSION_BUSY** {"hint": "原会话仍被桌面/界面占用(codex单写者锁)。请停止或关闭原Codex界面中的该会话, 看门狗将耐心重试(不占续跑预算)"}
- `2026-09-13T13:56:15` **EXIT_CRASH** {"rc": 1, "provider": "codex(cc-switch本地代理)", "err": "Error: thread/resume: thread/resume failed: thread 01a09954-f9c1-72a3-b3f6-845bcb108aba already has an active writer (code -32600)"}
- `2026-09-13T13:56:15` **SESSION_BUSY** {"hint": "原会话仍被桌面/界面占用(codex单写者锁)。请停止或关闭原Codex界面中的该会话, 看门狗将耐心重试(不占续跑预算)"}
- `2026-09-13T13:56:15` **BUSY_WAIT** {"wait_sec": 20, "n": 4, "lock": "held"}
- `2026-09-13T13:56:40` **HEARTBEAT** {"alive": false, "stale_sec": 105}
- `2026-09-13T13:56:40` **EXIT_CRASH** {"rc": 1, "provider": "codex(cc-switch本地代理)", "err": "Error: thread/resume: thread/resume failed: thread 01a09954-f9c1-72a3-b3f6-845bcb108aba already has an active writer (code -32600)"}
- `2026-09-13T13:56:40` **SESSION_BUSY** {"hint": "原会话仍被桌面/界面占用(codex单写者锁)。请停止或关闭原Codex界面中的该会话, 看门狗将耐心重试(不占续跑预算)"}
- `2026-09-13T13:57:05` **EXIT_CRASH** {"rc": 1, "provider": "codex(cc-switch本地代理)", "err": "Error: thread/resume: thread/resume failed: thread 01a09954-f9c1-72a3-b3f6-845bcb108aba already has an active writer (code -32600)"}
- `2026-09-13T13:57:05` **SESSION_BUSY** {"hint": "原会话仍被桌面/界面占用(codex单写者锁)。请停止或关闭原Codex界面中的该会话, 看门狗将耐心重试(不占续跑预算)"}
- `2026-09-13T13:57:05` **BUSY_TAKEOVER** {"lock": "probe-stale"}
- `2026-09-13T13:57:05` **RESUMED_BUSY** {"pid": 25184, "provider": "codex(cc-switch本地代理)"}
- `2026-09-13T13:57:10` **HEARTBEAT** {"alive": true, "stale_sec": 5}
- `2026-09-13T13:57:35` **EXIT_OK** {"acceptance": "afk-work/PROGRESS.md 不存在(worker未建立清单)"}
- `2026-09-13T13:57:35` **RESUME_WAIT** {"backoff_sec": 15, "attempt": 1, "reason": "early_exit", "provider": "codex(cc-switch本地代理)"}
- `2026-09-13T13:57:50` **RESUMED** {"attempt": 1, "pid": 13776, "provider": "codex(cc-switch本地代理)"}
- `2026-09-13T13:57:55` **HEARTBEAT** {"alive": true, "stale_sec": 5}
- `2026-09-13T13:58:05` **EXIT_OK** {"acceptance": "afk-work/PROGRESS.md 不存在(worker未建立清单)"}
- `2026-09-13T13:58:11` **TERMINAL** {"state": "FAILED", "detail": "矛盾态: worker连续2次空转退出但验收不通过(afk-work/PROGRESS.md 不存在(worker未建立清单)) — 需人工/L2介入"}

## 系统动作
- v0.2: 此处本应执行关机 (`shutdown /s /t 60`), 已跳过
