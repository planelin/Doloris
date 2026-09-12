# 使用手册 — 长流程任务自动挂机监管系统

监管一个 headless agent（claude CLI 或 codex CLI）跑长任务：
启动 → 心跳监控 → 崩溃/挂死自动续跑（同会话）→ 供应商探针与轮换（claude）→
验收通过才停 → 写报告 →（本应关机，当前跳过）。

## 快速开始

```bash
cd C:\Agents\zcode\double

# codex 驱动（走 cc-switch 本地代理，无需额外配置）
python supervise.py --task tasks/<你的任务>/task.md --driver codex --work-dir work-我的任务

# claude 驱动（自动注入系统代理 + cc-switch 供应商探针/轮换）
python supervise.py --task tasks/<你的任务>/task.md
```

启动后可以锁屏/走人。机器不会睡（看门狗持有进程级执行状态）。

## 写一个任务（两个文件）

`tasks/<任务名>/task.md` —— 任务书，全文会作为提示词发给 worker。**必须包含**：

1. 明确的产物路径约定（默认落在启动目录的 `--work-dir` 下，如 `work-xxx/`）
2. 强制要求 worker 维护 `PROGRESS.md` 勾选清单（`- [x]`/`- [ ]`），
   每完成一步立即打勾 —— 这是断点续传的唯一进度依据，worker被杀后靠它接着干
3. 「一口气完成、不要中途提问」的指令（交互托管的 L2 尚未接入）

`tasks/<任务名>/acceptance.md` —— 验收清单（机器可验证的完成判据），每行一个断言：

```
# 注释行
checklist: work-xxx/PROGRESS.md :12    # 文件内 '- [x]' 数量 ≥ 12
work-xxx/report.md                     # 至少1个非空文件
work-xxx/chapters/ch*.md :12           # 至少12个非空文件（glob 相对仓库根）
```

不写 `acceptance.md` 时回退 selftest 默认（12章+12勾，仅适合测试任务）。

## 常用参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--driver` | claude | `claude` / `codex` |
| `--work-dir` | work | worker 产物目录（相对启动目录） |
| `--chaos kill:N` | 关 | 测试用：worker 运行 N 秒时杀掉它，验证自愈 |
| `--max-resumes` | 8 | 续跑预算，耗尽即 FAILED 终态 |
| `--max-run-sec` | 3600 | 总时长上限（含退避），超限 FAILED |
| `--no-probe` | 关 | 跳过 claude 启动探针 |
| `--heartbeat-stale` | 150 | 挂死阈值（claude），按击杀次数梯度 150→300→450s |

## 运行产物

```
runs/<时间戳>/
├── report.md              # 终态报告：先读这个（成功/失败、时间线、供应商顺序、验收详情）
├── interventions.jsonl    # 全部事件：LAUNCH/CHAOS_KILL/DETECT_HANG/RESUMED/PROVIDER_SWITCH/TERMINAL...
├── worker-stdout.log      # worker 最终消息与报错（诊断金矿）
├── worker-stderr.log
├── prompt.txt / resume-prompt.txt
└── codex-last-message.txt # codex 驱动：agent 最终消息落盘

pool-health.json           # claude 供应商探针结果缓存（哪些端点真能通 CLI）
work-xxx/                  # worker 的实际产出
```

## 终态语义

- **SUCCESS** = worker正常退出 且 acceptance.md 全部满足 → 写报告 →（应关机）
- **FAILED** = 续跑预算耗尽 / 总时长超限 / 矛盾态（worker两次空转退出但验收不过）→
  写现场报告 →（应关机）。矛盾态说明需要 L2 智能介入或人工处理。

## 已知边界（下一步清单）

- L2 升级 agent 未接入：上下文污染换新会话+交接摘要、矛盾态诊断、托管交互问答
- 关机与推送通知未启用（报告已就位，通知通道待定）
- claude 驱动的干预存档（git commit 前置）尚未实现，回档脚本未写
