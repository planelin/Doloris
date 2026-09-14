# 使用手册 — 长流程任务自动挂机监管系统

监管一个 headless agent（claude CLI 或 codex CLI）跑长任务：
启动 → 心跳监控 → 崩溃/挂死自动续跑（同会话）→ 供应商探针与轮换（claude）→
验收通过才停 → 写报告 →（本应关机，当前跳过）。

## 快速开始

```bash
cd C:\Agents\zcode\double

# ── 日常推荐: 零准备快速托管 ──
# 在 Codex 界面推进项目 → 要出门 → 双击 afk.cmd, 或:
python supervise.py --adopt last --quick
# 零侵入透明挂机: 无需建任何特定文件夹，Worker 在其原本的项目目录正常工作；
# 完成判据 = 自动识别自然完工语义或项目原生清单（PROGRESS.md/TODO.md）全勾。回来先读 runs/<最新>/report.md。

# ── 严肃任务: 显式任务书(可自定义验收) ──
# 接管既有会话 + 任务书:
python supervise.py --adopt last --task tasks/<你的任务>/task.md
# 从零发起新任务挂机:
python supervise.py --task tasks/<你的任务>/task.md --driver codex --work-dir work-我的任务

# ── claude 驱动（自动注入系统代理 + cc-switch 供应商探针/轮换）──
python supervise.py --task tasks/<你的任务>/task.md
```

启动后可以锁屏/走人。机器不会睡（看门狗持有进程级执行状态）。

> **接管纪律**：codex 对 App 本次运行中打开过的每个会话持有单写者锁——锁文件在
> `~/.codex/thread-writer-locks/<线程id>.lock`，**与任务是否暂停无关**（暂停无效）。
> 释放锁的唯一可靠手势：**彻底退出 Codex App**。afk 会识别进程存活状态自适应处理：
> App活着→静默等待；App退出但锁文件残留→立即清除，≤20秒接管。
> **不要用"归档"**（会把rollout搬进archived_sessions/，破坏锚点），**更不要删除**。
>
> **最优雅交接（零打断，推荐）**：离开前在 App 输入框输入交接指令（如「完成当前步骤
> 后停止，更新进度清单」）并点击**「发送，但不打断模型」**——官方队列会在安全边界
> 投递，agent 收尾后自然停止；此时退出 App/按 y，交接零伤害。该按钮只能人手点，
> afk 不做UI自动化。
>
> **运行中如何共处**：接管期间 App 里**看不到实时进展**（锁会挡住视图刷新）——观察
> 用 afk 黑窗口、项目原生清单（如 `PROGRESS.md`）、`runs/<最新>/interventions.jsonl`。
> afk 结束（SUCCESS/FAILED）后锁自动释放，打开 App 点会话即可拿回继续交互。
> afk 绝不修改或重定向用户的工程工作区，保持对原生项目环境的绝对忠实；接管前会自动建立工作区轻量快照（`.zip` 备份写入 `runs/<最新>/` 目录，彻底杜绝污染原工程）。

## L2 升级agent（稳定通道救火队）

L1续跑预算耗尽且故障为崩溃/挂死时，自动调用L2 agent做最后一次诊断与修复。
**默认通道 = Antigravity**（`--model=flash`，Google官方通道，与codex中转完全无关）；
claude通道已弃用（同为中转，稳定性不合格）。

- 授权动作：改写 `~/.codex/config.toml` 切换中转供应商（可从 cc-switch 数据库只读
  备选端点与key，绕开故障路由）；判定 `NEW_SESSION`（上下文污染，记录后继续）；
  判定 `UNFIXABLE`（基础设施故障 → 诚实FAILED）
- L2 修复后追加 4 次续跑预算；最多升级 `--l2-max`（默认2）次
- **Antigravity 通道**：`--l2-cmd antigravity`——由 `AntigravityManager` 管理无头/有头双模桥接：
  - 优先复用已有实例（桌面端开着时直接复用）；若桌面端关闭，在后台直接启动轻量独立微核心（`resources/bin/language_server.exe --standalone`），彻底避开桌面端单实例互斥锁，用户前台可自由开/关 App；
  - **单一会话强绑定**：1 个 Codex 会话严格对应 1 个 AGY 会话，首轮 `new-conversation` 注入全景背景，后续决策与修复一律通过 `send-message` 增量通信，且自动持久化至 `runs/<最新>/agy_session.json`；
  - 桥接三件套动态发现：CSRF、LS监听端口、项目id（默认取最近会话元数据，可 `--l2-project-id` 指定；模型 `--l2-model flash_lite/flash/pro`）；
  - 轮询verdict文件回收输出（决策代答写入 `afk-l2-answer.txt`，经清洗后纯净回喂 Worker；故障诊断写入 `afk-l2-verdict.txt`）；
  - **两段式安全停机**：回收与销毁时优先发送 `CTRL_BREAK` 等优雅信号，留出刷盘缓冲，保护 SQLite WAL 数据库完好。
- 禁用L2：`--l2-cmd off`
- 每次L2的提示词与回复完整留档：`runs/<最新>/l2-N-prompt.txt` / `l2-N.log`

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
| `--chaos net:N:M` | 关 | 测试用：N 秒时断网 M 秒（模拟中转宕机，需管理员；结束/退出自动恢复） |
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

- L2 升级 agent 换会话闭环：上下文污染判定 `NEW_SESSION` 时的新会话开辟与交接摘要注入
- 关机与多通道推送通知（报告已就位，通知通道如飞书/钉钉/企业微信/Bark待接入）
- claude 驱动的干预存档（git commit 前置）尚未实现，回档脚本未写
