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
# 无需建任何文件夹: 内置续跑指令接管最近会话; 产物进被接管会话目录下的 afk-work/;
# 完成判据 = afk-work/PROGRESS.md 全部勾完。回来先读 runs/<最新>/report.md。

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
> **运行中如何共处**：接管期间 App 里**看不到实时进展**（锁会挡住视图刷新）——观察
> 用 afk 黑窗口、项目目录的 `afk-work/PROGRESS.md`、`runs/<最新>/interventions.jsonl`。
> afk 结束（SUCCESS/FAILED）后锁自动释放，打开 App 点会话即可拿回继续交互。
> ADOPT 日志会打印**任务标题**（首条用户消息前60字），用于核对接管对象是否正确。
> 另外quick模式每次启动会把上一轮遗留的`afk-work/PROGRESS.md`归档到
> `afk-work/archive/`，防止旧清单造成假验收通过。

## L2 升级agent（稳定通道救火队）

L1续跑预算耗尽且故障为崩溃/挂死时，自动调用L2 agent做最后一次诊断与修复。
**默认通道 = Antigravity**（`--model=flash`，Google官方通道，与codex中转完全无关）；
claude通道已弃用（同为中转，稳定性不合格）。

- 授权动作：改写 `~/.codex/config.toml` 切换中转供应商（可从 cc-switch 数据库只读
  备选端点与key，绕开故障路由）；判定 `NEW_SESSION`（上下文污染，记录后继续）；
  判定 `UNFIXABLE`（基础设施故障 → 诚实FAILED）
- L2 修复后追加 4 次续跑预算；最多升级 `--l2-max`（默认2）次
- **Antigravity 通道**：`--l2-cmd antigravity`——经 `language_server.exe agentapi` 桥创建
  修复会话（桥接三件套自动动态发现：CSRF取自App日志最新spawn行、网关端口取自
  language_server监听端口、项目id默认取最近会话元数据，可 `--l2-project-id` 指定；
  模型 `--l2-model flash_lite/flash/pro`）。异步执行+轮询verdict文件（agent把决议
  写入项目目录`afk-l2-verdict.txt`）。**观察修复过程：直接打开Antigravity App**，
  L2修复会话会出现在对应项目的会话列表里，全程可视
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

- L2 升级 agent 未接入：上下文污染换新会话+交接摘要、矛盾态诊断、托管交互问答
- 关机与推送通知未启用（报告已就位，通知通道待定）
- claude 驱动的干预存档（git commit 前置）尚未实现，回档脚本未写
