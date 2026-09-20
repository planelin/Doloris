# AFK Supervisor

<p align="center">
  <strong>面向 AI Coding Agent（Codex / Claude）的长任务无人值守托管与自愈系统</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue.svg" alt="Python Version" />
  <img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License" />
  <img src="https://img.shields.io/badge/Dependencies-Zero%20External-orange.svg" alt="Zero Dependencies" />
  <img src="https://img.shields.io/badge/Platform-Windows-lightgrey.svg" alt="Platform Windows" />
</p>

---

## 为什么需要 AFK？

在日常使用 Codex 或 Claude 等 AI 编程助手时，长流程任务往往需要数十分钟甚至数小时。然而：
- 离开座位（AFK）后，模型常停留在**中间选择题**、**主视觉/架构确认提问**上等待人工输入；
- 遭遇偶发**网络波动（429 / 5xx）、工具报错或进程崩溃**时，任务直接中断；
- 多个写入者冲突导致会话锁死，或产生“假完成”幻觉。

**AFK 的核心使命**：让任务在用户缺席时**真正持续推进并完成**。
它不仅是一个心跳探测器，更提供**分级决策与自愈环路**——小问题自动代答决策、环境故障自动诊断修复、基于真实交付证据验收终态，绝不因为一道常规提问而停滞等待不在场的用户。

---

## 三大托管模式

AFK 提供三种开箱即用的交接模式，适应不同的工作习惯：

| 模式 | 入口脚本 | 工作原理 | 适用场景 |
|---|---|---|---|
| **1. 经典无头接管** | [`afk.cmd`](afk.cmd) | 确认安全边界 → 退出 Codex App → 释放单写者锁 → 原地 headless resume 原任务 | 最省资源，支持锁屏/离席黑屏静默执行，历史在同会话延续 |
| **2. Fork 无头续跑** *(推荐)* | [`afk2.cmd`](afk2.cmd) | 取得互斥锁 → 自动请求原任务暂停并确认停止 → `fork` 子任务无头续跑 | **无需关闭 App**，保留父任务记录，避免父子并发消耗 Token |
| **3. 双有头原生 GUI** | [`afk3.cmd`](afk3.cmd) | 保持 App 前台活跃 → 校验活动面板机器身份 → UI Automation 精确注入指令 | 桌面窗口可见，实时观察生成流动，视觉零中断与高审计性 |

---

## 核心架构与设计

AFK 采用 **L1 看门狗 + L2 委托智能** 双层架构：

```mermaid
flowchart TD
    subgraph L1["L1 编排与看门狗 (afk_supervisor)"]
        A[监听 Worker 状态 / Rollout 轨迹] --> B{生命周期判定}
        B -->|正常运行| C[心跳监测 / 防挂死 / 供应商轮换]
        B -->|进程崩溃 / 网络超时| D[指数退避 / 自动重启续跑]
        B -->|停在提问 / 待决策 / 可修复报错| E[触发 L2 委托环路]
        B -->|任务结束| F[基于证据的交付验收]
    end

    subgraph L2["L2 委托智能与自愈 (Antigravity AGY)"]
        E --> G[L2 决策代答 (DECIDE)]
        E --> H[L2 故障诊断与修复 (REPAIR)]
        G --> I[清洗指令并安全喂回 Worker]
        H --> I
    end

    subgraph Final["终态与报告"]
        F --> J{验收是否通过?}
        J -->|真实交付物 + 证据充分| K[SUCCESS: 生成终态报告 + 触发通知]
        J -->|证据不足 / 持续阻塞| L[BLOCKED / FAILED: 保存现场与日志]
    end
```

### 核心特性

- **严格安全交接边界**：绝不以“文件 15 秒没动静”作为空闲依据；按 `call_id` 配对工具调用，排查未返回事件与写锁状态，防并发冲突。
- **零人工依赖的无人值守决策**：Worker 提出的设计确认或选项问题，由 L2 根据任务目标和上下文拍板，杜绝常规问题回退人工。
- **证据驱动的真实验收**：不听信模型“我已完成”的口头宣称，严密审查实际文件产出、自建进度清单（`PROGRESS.md`）及验证执行记录。
- **全通道通知集成**：支持通过 `AFK_WEBHOOK_URL` 向飞书、钉钉、企业微信、Bark 等推送带时间线和状态卡片的终态通知。
- **零第三方依赖**：纯 Python 3.10+ 标准库实现，轻量、稳定、无需配置繁琐环境。

---

## 快速上手

### 1. 环境准备
- Windows 10 / 11
- Python 3.10+
- 可选安装为全局命令：
  ```bash
  pip install -e .
  ```

### 2. 一键启动托管
进入你的工作目录，双击或在命令行运行：

```powershell
# 推荐：Fork 无头续跑（不杀 App，安全暂停后无头推进）
.\afk2.cmd

# 经典：自动退出 App 原地接管同会话
.\afk.cmd

# GUI：保持桌面 App 开启，通过 UI Automation 注入
.\afk3.cmd
```

### 3. 命令行灵活调用

```powershell
# 接管最新的会话并启用 Fork 模式
python supervise.py --adopt last --quick --fork

# 指定接管特定的 Codex 线程 URL 并使用 GUI 模式
python supervise.py --adopt codex://threads/<THREAD_ID> --gui

# 限制续跑预算与最大运行时间（秒）
python supervise.py --adopt last --max-resumes 5 --max-run-sec 3600
```

更多高级参数请参阅 [使用手册 (USAGE.md)](USAGE.md)。

---

## 运行产物与目录结构

每次托管运行均在 `runs/` 独立归档（已在 `.gitignore` 保护，不污染仓库）：

```text
runs/<时间戳>/
├── report.md              # 终态报告：成功/失败、时间线、验收详情
├── interventions.jsonl    # 完整事件审计：LAUNCH / HANG / RESUMED / L2_DECIDE...
├── worker-stdout.log      # Worker 终端输出与报错
└── codex-last-message.txt # 任务最终消息
```

---

## 自动化测试与验证

本项目自带完备的**隔离测试套件**（不影响真实 App、不抢占真实工作区锁）：

```powershell
python -B -X utf8 tests/run_isolated.py
```

当前包含 197 项自动化测试，覆盖安全边界探测、状态机迁移、GUI 身份校验与协议解析。

---

## 更多文档

- [使用手册与命令行参数全集 (USAGE.md)](USAGE.md)
- [系统架构准则与技术规范 (docs/SPECIFICATION.md)](docs/SPECIFICATION.md)
- [开源贡献指南 (CONTRIBUTING.md)](CONTRIBUTING.md)

---

## 许可证

本项目采用 [MIT License](LICENSE) 开源许可证。
