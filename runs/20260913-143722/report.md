# 监管运行报告 — 20260913-143722

- 终态: **FAILED**
- 会话: `01a0997a-e6d8-76b2-a6e9-a815ca254e85`
- 供应商尝试顺序: codex(cc-switch本地代理)
- 干预/续跑次数: 1 (chaos注入: None)
- 验收: afk-work/PROGRESS.md 不存在(worker未建立清单)
- 详细时间线: interventions.jsonl
- worker日志: worker-stdout.log / worker-stderr.log
- 会话轨迹: None

## 时间线
- `2026-09-13T14:37:22` **EGRESS** {"proxy": "http://127.0.0.1:7897", "driver": "codex"}
- `2026-09-13T14:37:25` **ADOPT** {"session": "01a0997a-e6d8-76b2-a6e9-a815ca254e85", "title": "在 testproj/ 目录下构建一个「Agent监管系统监控台」纯静态网站，按顺序推进，每完成一个文件算一步：\n\n1.", "rollout": "rollout-2026-09-13T14-36-03-01a0997a-e6d8-76b2-a6e9-a815ca25", "session_cwd": "C:\\Agents\\zcode\\double"}
- `2026-09-13T14:37:33` **APP_CLOSED** {"killed": ["ChatGPT.exe"]}
- `2026-09-13T14:37:33` **STALE_LOCK_REMOVED** {"pre": "接管准备"}
- `2026-09-13T14:37:33` **LAUNCH** {"session": "01a0997a-e6d8-76b2-a6e9-a815ca254e85", "provider": "codex(cc-switch本地代理)", "chaos": "off"}
- `2026-09-13T14:37:33` **ADOPTED_RESUME** {"pid": 9820}
- `2026-09-13T14:37:38` **HEARTBEAT** {"alive": true, "stale_sec": 5}
- `2026-09-13T14:37:43` **EXIT_OK** {"acceptance": "afk-work/PROGRESS.md 不存在(worker未建立清单)"}
- `2026-09-13T14:37:43` **RESUME_WAIT** {"backoff_sec": 15, "attempt": 1, "reason": "early_exit", "provider": "codex(cc-switch本地代理)"}
- `2026-09-13T14:37:58` **RESUMED** {"attempt": 1, "pid": 31992, "provider": "codex(cc-switch本地代理)"}
- `2026-09-13T14:38:08` **HEARTBEAT** {"alive": false, "stale_sec": 10}
- `2026-09-13T14:38:08` **EXIT_OK** {"acceptance": "afk-work/PROGRESS.md 不存在(worker未建立清单)"}
- `2026-09-13T14:38:08` **TERMINAL** {"state": "FAILED", "detail": "矛盾态: worker连续2次空转退出但验收不通过(afk-work/PROGRESS.md 不存在(worker未建立清单)) — 需人工/L2介入"}

## 系统动作
- v0.2: 此处本应执行关机 (`shutdown /s /t 60`), 已跳过
