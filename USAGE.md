# 使用手册 — 长流程任务自动挂机监管系统

## 三种模式与本轮审查修复

| 入口 | 交接方式 | App 与任务关系 |
|---|---|---|
| `afk.cmd` | 安全边界确认 → 自动退出 App → 写锁释放 → resume 原任务 | App 关闭，原任务无头续跑；不是无条件强杀 |
| `afk2.cmd` | 取得监管锁 → `HANDOFF_WAIT` → 暂停并无限等待停止确认 → fork +「继续」 | App 保留，父任务停止，仅子任务无头工作 |
| `afk3.cmd` | 监听目标回合 → L2 决策/修复/审查 → 验证目标身份后 GUI 发送 | Worker 与 L2 都保持有头，不 fork、不关闭 App |

本轮修复五项：GUI 目标身份、送达/ACK 防重复、GUI 无人值守 L2 分派、unknown 生命周期阻断，以及 AFK2 先加锁落盘再暂停。等待期间 Ctrl+C 会保存 `CANCELLED`、生成报告、调用已配置通知并释放锁，不启动子任务；重复实例在暂停前就被拒绝。

GUI 收件仅接受目标 rollout **发送偏移之后的新完整匹配指令事件**；`GUI_INJECT_ACK` 记录证据。明确 `NOT_SENT` 最多尝试 3 次、间隔至少 6 秒；`SENT` / `UNCERTAIN` 不重发。30 秒仍未确认则记录 `FAILED`，保留 `pending_command`、`dispatch_status`、偏移和尝试次数供核查。不要未核对实际接收情况就手工重发。

GUI 身份校验仅接受精确的机器身份标记（活动面板 `active-thread-<SID>`，或 Document 的 `thread-<SID>` / `codex://threads/<SID>` 元数据），并在该面板内查找输入框。标题、选中侧栏、聊天正文不作为授权依据。**当前 App 版本是否暴露这些元数据必须实机验证**；不满足时安全拒绝，不回退为“猜一个输入框”。这也影响 AFK/AFK2 的 GUI 暂停步骤。

GUI 旧式 `DEFER/request_user` 转 DECIDE；`UNRESOLVED + worker_fix` 执行 L2 的替代步骤；修复后重新 REVIEW。DECIDE/REPAIR 通道最多尝试 3 次，持续失败记为 `FAILED`，有证据的 L2 STOP 记为 `BLOCKED`，而非 `WAITING_USER`。

隔离验证入口：`python -B -X utf8 tests/run_isolated.py`。它使用临时源码副本、模拟进程/GUI/L2/通知边界，另编译实际 C# 注入器并仅调用纯身份判断函数；不会关闭真实 App 或向真实任务发送指令。真实 GUI 与 AGY 链路仍需受控联调。

## 2026-09-20：AFK2 决策续跑更新

实际测试后的补充修复：AGY 单次等待默认改为 1800 秒（可用 `--timeout-sec` 调整）；等待超时不再创建替代会话，在途请求记录于 `agy_pending_request.json`，重试复用请求 ID 并继续读取原回复。会话绑定失效或建会话结果不确定时保留证据并报错，不静默创建新会话。

**AFK2 工作模式校正：先暂停原任务，再 fork 无头续跑。** 保留 Codex App 和父任务记录，但不能让父任务与 fork 子任务同时运行，以免并发消耗 token、重复执行工作。恢复接管前的自动暂停与结果确认；无法确认暂停就不启动 fork，不再把等待原任务自然结束作为唯一交接方式。切换造成中断后，向无头子任务发送「继续」是正常续跑，不应误判为错误注入，也不需要为单纯切换额外请求一次 L2 决策。

**AFK2 暂停确认改为无限等待。** 不再以固定缓冲时间或暂停等待超时决定 fork。启动后仍自动向目标任务请求暂停，随后每秒检查 rollout；只有结构化轨迹显示当前回合已 `turn_aborted` / `task_complete`，且没有尚未返回的工具调用、损坏/未写完事件时，才继续交接。GUI 返回“暂停成功”、文件不再更新都不算确认；原任务已停止时无需额外等待。日志 `FORK_PARENT_WAIT` / `PAUSE_WAIT` 会显示等待状态及依据，确认后记录 `FORK_PARENT_PAUSED`。

等待期间 Codex App 保持打开，无头子任务尚未启动；可以 Ctrl+C 取消。如果 GUI 暂停请求没有成功送达，仍持续观察原任务，直到确实停止，不会到点强行 fork。暂停等待不消耗无头托管的 `--max-run-sec` 预算；经典 kill 模式的 `--handoff-timeout-sec` 仍有上限，不受此改动影响。隔离回归覆盖了模拟等待 180 秒后才暂停、随后 fork 并发送“继续”的链路；这不是实际桌面 GUI 联调的替代证明。

只有父任务确实停在提问/待决策处，才先交给 L2 判断，再把有效指令传入 fork；L2 明确返回有效 `PROCEED` +「继续」时也不再按措辞拦截。fork 启动前再次确认父任务未恢复运行。

继续保留本轮其他修复：内部审批/子代理轨迹不参与任务接管选择；任务身份按元数据的 `id` 精确匹配，避免把子会话的父 `session_id` 当作自身 ID。预览 URL 不再作为 Windows 交付路径解析。

产品目标见 [README.md](README.md)。原任务中的“向用户确认”节点现交给 L2 AGY 判断，不再由本地关键词规则直接拦截。Fork 前确有待决策问题时，与运行中的 DECIDE 共用决策入口；无效回复或旧式 `DEFER/request_user` 最多尝试三次，不用“继续”绕过失败的决策。正常切换续跑与 L2 已批准的“继续”不属于这种情况。

L2 确认无法继续时使用 `STOP` + `terminate_blocked`，提供 `blockers` 和具体停止理由，记录为 `BLOCKED`；决策通道持续异常记录为 `FAILED`。Fork 前置决策退出也会生成 `report.md` 并调用已配置的终态通知。协议仍保留 `afk_agy_protocol_v1` 名称，但不再接受旧的人工回复动作，外部 L2 实现需要同步这些枚举。

AFK2 决策入口和协议、GUI 送达确认与旧分支统一已有隔离回归覆盖；自动化测试不能替代真实 AGY 与桌面 UI 联调。

监管一个 headless agent（claude CLI 或 codex CLI）跑长任务：
启动 → 心跳监控 → 崩溃/挂死自动续跑（同会话）→ 供应商探针与轮换（claude）→
验收通过才停 → 写报告 →（本应关机，当前跳过）。

## 快速开始

```bash
cd <path-to-afk>

# ═══════════════════════════════════════════════════════════════════════════
# 托管模式选择 (三大一键脚本):
# ═══════════════════════════════════════════════════════════════════════════

# 1.【日常推荐】afk2.cmd — Fork 无头续跑 (不杀 App，暂停原任务，生成独立Thread)
afk2.cmd
# 底层原理: 自动请求暂停、无限等待原任务停止确认，再 codex exec fork；无待决策问题时向子任务发送“继续”。
# 优势: 不需要杀死桌面端 App，保留父任务记录；仅无头子任务续跑，避免父子并发消耗 token。

# 2.【经典模式】afk.cmd — 自动安全退出 App 后无头接管 (原地 resume 同一个 Thread)
afk.cmd
# 或: python supervise.py --adopt last --quick
# 优势: 最省系统资源，支持完全黑屏/锁屏静默执行，会话历史在同一个 Thread 延续。

# 3.【双有头模式】afk3.cmd — 原生桌面 GUI 交互监管 (App全程前台活跃跑)
afk3.cmd
# 底层原理: 桌面端 App 不关且前台生成，看门狗基于 task_complete 强生命周期契约监听停顿与决策请求，
# 优先通过 Windows 原生 UI Automation (UIA) 无损直写并触发 Send (备用方案结合精准进程过滤与坐标自适应聚焦)。
# 视觉审计: 每一轮 L2 主管决断均在 Antigravity 界面自动生成带标题的原生人类对话气泡，托管归来一目了然。
# 优势: 视觉零中断，能在屏幕上实时看桌面端流动生成，高可观测性与高审计性。

# ── 命令行灵活用法 ──
python supervise.py --adopt 1 --quick --fork               # 显式使用 Fork 模式接管第1个会话
python supervise.py --adopt codex://threads/... --gui      # 显式使用双有头 GUI 模式接管指定链接
python supervise.py --adopt <uuid> --quick                 # 显式使用经典 resume 模式接管
```

完成自动交接后可以锁屏/走人。机器不会睡（看门狗持有进程级执行状态）。

> **以下交接说明针对经典 kill / resume 模式，不适用于 AFK2。**
>
> 选定任务后 AFK **自动关闭 App**，不再询问是否关闭、不要求用户手工退出；`--yes` 只跳过会话选择。关闭 App 不依赖锁文件是否存在。
> 当前写锁目录是 `~/.codex/thread-writer-locks/`（不是旧的 `~/.codex/locks/`）。AFK 不删除锁文件：App 完全退出后用操作系统锁探测验证释放，再由 Codex resume 自行获取正式写锁。
>
> **“安全边界”精准排除高危崩溃状态**：扫描 rollout 的结构化事件，按 `call_id` 配对工具调用（`*_call` 与 `*_output`）。仅当存在**在途工具调用未返回**（如命令/脚本正在执行、文件写入未完成）的高危边界时进行等待；只要没有在途工具（模型处于文本生成、思考、已完成回合或空闲等任何状态），均可直接安全退出 App，由无头端接管并无缝继续。无需人工或 GUI 暂停。文件的 `mtime` 只作为日志中的 `file_age_sec` 展示，不用于许可关闭或接管。
> 这些是轨迹中已记录调用的交接证据，不是对任意脱离工具运行的后台作业、其他 App 任务的安全保证；kill 前不要在同一 App 中并行运行不希望中断的任务。
>
> 安全检查默认最多等待 90 秒，可用 `afk.cmd --handoff-timeout-sec 300` 延长。若工具调用超时仍未返回、进程无法退出或写锁仍被其他进程持有时，记录 `FAILED` 和报告/通知，**不超时盲杀、不启动冲突的无头 worker**。
> 整个关闭与无头续跑链路无需人工点击操作，可在看到 `WRITER_RELEASED` / `ADOPTED_RESUME` 后放心锁屏。
>
> **可观测性**：`interventions.jsonl` 依次记录 `HANDOFF_START`、`HANDOFF_BOUNDARY`（状态、原因、最新事件、未返回调用 ID、文件年龄）、`APP_CLOSED`、`WRITER_RELEASED`，再进入 `LAUNCH` / `ADOPTED_RESUME`。`APP_CLOSED` 只在退出检查成功后记录。
> **不要用“归档”或删除会话交接**，它们会改变或破坏 rollout 锚点。
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
2. 可维护 `PROGRESS.md` 记录已验证进度和续跑上下文；勾选不是完成证明，不是唯一断点依据。
3. 写明任务范围与授权边界；常规提问交给已接入的 L2 决策，不要求用户必须在线。

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
| `--delivery-dir` | (自动检测) | 显式指定用户交付目录，严格保护不被工作区改写或静默回退 |
| `--proxy` | (自动检测) | 显式指定网络代理（默认自动探测系统代理） |
| `--max-interactions` | 10 | 交互决策代答与审查轮次上限 |
| `--handoff-timeout-sec` | 90 | kill 模式安全边界等待秒数；超时报告失败，不盲杀/并发接管 |
| `--chaos kill:N` | 关 | 测试用：worker 运行 N 秒时杀掉它，验证自愈 |
| `--chaos net:N:M` | 关 | 测试用：N 秒时断网 M 秒（模拟中转宕机，需管理员；结束/退出自动恢复） |
| `--max-resumes` | 8 | 续跑预算，耗尽即 FAILED 终态 |
| `--max-run-sec` | 0 (不设限) | 总时长上限（秒），默认0=跑完为止，不设超时熔断 |
| `--no-probe` | 关 | 跳过 claude 启动探针 |
| `--heartbeat-stale` | 600 | 挂死阈值，代码当前采用梯度 600→1200→1800s（大上下文安全，以代码为准） |

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

- **SUCCESS**：交付物与验证证据满足验收；开启 L2 时还需当前版本的审查通过。
- **FAILED**：交接安全条件未确认、送达未确认、有界恢复耗尽或基础设施失败。
- **BLOCKED**：L2 提供停止依据、阻塞证据和已尝试替代方案。
- **TIMEOUT**：达到已配置运行预算；AFK2 暂停等待不计入该预算。
- **CANCELLED**：用户取消；保存现场并清理本实例资源。
- 终态均应保存状态、报告及已配置通知；当前不会真正关机。常规决策不以 `WAITING_USER` 结束。

## 通知集成与已知边界

- **推送通知**：支持环境变量 `AFK_WEBHOOK_URL`，在任务达到终态（SUCCESS / FAILED / BLOCKED / TIMEOUT / CANCELLED）时自动向飞书/企业微信/钉钉/Bark等发送卡片摘要与报告路径。
- L2 升级 agent 换会话闭环：上下文污染判定 `NEW_SESSION` 时的新会话开辟与交接摘要注入
- claude 驱动的干预存档（git commit 前置）尚未实现，回档脚本未写
