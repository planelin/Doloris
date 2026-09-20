<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue.svg" alt="Python Version" />
  <img src="https://img.shields.io/badge/License-MIT-emerald.svg" alt="License" />
  <img src="https://img.shields.io/badge/Dependencies-Zero%20External-orange.svg" alt="Zero Dependencies" />
  <img src="https://img.shields.io/badge/Tests-197%20Passed%20(100%25)-indigo.svg" alt="197 Tests Passed" />
  <img src="https://img.shields.io/badge/BYOP-Custom%20Pet%20Ready-pink.svg" alt="BYOP Ready" />
  <img src="https://img.shields.io/badge/Platform-Windows-lightgrey.svg" alt="Platform Windows" />
</p>

<h1 align="center">Doloris (ドロリス)</h1>

<p align="center">
  <strong>面向 AI Coding Agent（Codex / Claude）的桌宠级长任务无人值守托管与自愈系统</strong><br>
  <em>“既是初音，又是金毛；桌宠代班，长夜守护。”</em>
</p>

<p align="center">
  <a href="#-为什么是-doloris">项目理念</a> •
  <a href="#-三大托管模式">三大模式</a> •
  <a href="#-终端实况模拟">实况演示</a> •
  <a href="#-byop-自备桌宠生态规划">自备桌宠</a> •
  <a href="#-快速上手">快速上手</a> •
  <a href="#-测试与验证">自测验证</a> •
  <a href="USAGE.md">使用手册</a>
</p>

---

## 🌟 为什么是 Doloris？

在日常使用 Codex 或 Claude 等 AI 编程助手推进长流程任务时，开发者经常遭遇这样的困境：
- 离开座位（AFK）去开会或休息，模型却停留在**中间选择题**、**主视觉/架构确认提问**上，白白发呆一整夜；
- 遭遇偶发**网络断连（429 / 5xx）、工具报错或进程崩溃**，任务直接意外中断；
- 多个写入者冲突导致会话锁死，甚至产生“表面已完成”的代码幻觉。

### 名字的由来与双重隐喻
**Doloris** 灵感源自《BanG Dream! It's MyGO!!!!! / Ave Mujica》中三角初华（Misumi Uika）的舞台艺名：
- **初音（Hatsune）**：初华之音，象征着代码的初心与最初奏响；
- **金毛（Golden Retriever）**：初华标志性的金发，更是**忠诚、聪明、时刻蹲守在工位为你代班的桌面伴侣犬**的绝妙代称。

平时，她是在桌面角落安静陪伴你的可爱桌宠；当你临时离席时，只需轻轻点她一下，她便戴上面具化身为全权统御的 **Doloris**——接管 Codex 进程，替你做决策、修报错、跑测试，直到你回来时双手递交满分的工作成果！

---

## 📺 终端实况模拟

来看看 Doloris 是如何在无人值守状态下化解停顿与报错的：

```text
$ .\doloris.cmd fork --adopt last --quick
[20:45:01] 🔒 ACQUIRE   工作区互斥锁已锁定 -> 状态落盘 HANDOFF_WAIT
[20:45:02] ⏸️  PAUSE     向原任务发送暂停请求，无限等待回合安全停止...
[20:45:05] ✅ PAUSED    轨迹证实 turn_aborted，无未返回工具调用，安全门通过！
[20:45:06] 🔱 FORK      无头子任务拉起 (Thread: 8f92a1)，Codex 桌面保持静止
[20:45:12] ⚠️  QUESTION  检测到 Worker 停在提问：【全站主题风格决策：A. 暗黑霓虹 / B. 极简白】
[20:45:14] 🧠 L2 DECIDE  触发委托决策 -> Antigravity AGY 介入拍板：选择 A（已建立深色底座）
[20:45:16] 🚀 DISPATCH  清洗决策指令并安全回喂 Worker -> 任务继续推进，无需等待离席人类！
[20:46:20] 🔧 REPAIR    检测到网络 429 抖动 -> 指数退避重试 (1/3) -> 自动恢复！
[20:48:30] 🎯 ACCEPT    真实验收：12个页面与 PROGRESS.md 全部完成勾选
[20:48:31] 🏆 TERMINAL  SUCCESS — 终态报告已生成，Webhook 卡片已送达飞书/钉钉！
```

---

## 🛡️ 三大托管模式

Doloris 提供了三种开箱即用的交接形态，随时根据需要灵活切换：

| 模式 | 启动命令 | 工作原理 | 适用场景 |
|---|---|---|---|
| **1. Fork 无头续跑** *(推荐)* | `doloris fork`<br>或 `afk2.cmd` | 取得监管锁 → 请求暂停并无限等待原任务停止 → `fork` 子任务无头续跑 | **无需关闭 App**，保留父任务记录，彻底杜绝父子任务并发消耗 Token |
| **2. 经典无头接管** | `doloris resume`<br>或 `afk.cmd` | 确认安全边界 → 退出 Codex App → 释放单写者锁 → 原地 headless resume | 最省系统资源，支持完全黑屏/锁屏静默执行，历史在同会话延续 |
| **3. 双有头原生 GUI** | `doloris gui`<br>或 `afk3.cmd` | 保持 App 前台活跃 → 校验活动面板机器身份 → UI Automation 精确注入 | 桌面窗口完全可见，实时观察生成流动，视觉零中断、极高审计性 |

---

## 🐾 BYOP (Bring Your Own Pet) 自备桌宠生态规划

**桌宠不应被千篇一律的固定形象束缚！**

Doloris 采用**“大脑 + 身体”完全解耦**的架构：
- **底层大脑（Python 3.10+ 标准库）**：负责严格的生命周期判定、写锁释放、L2 委托决策与自愈；
- **表现层桌宠（Desktop Mascot Engine）**：支持用户**直接导入自己喜爱的桌宠图片包**（包括 Codex 原生桌宠、Shimeji 动漫包、像素图、自家宠物表情包）。

```text
my-custom-pet/
├── pet.json             # （可选）配置动画帧率、气泡偏移、提示音
├── idle/                # 空闲发呆帧（01.png, 02.png...）
├── working/             # 托管打工中（敲键盘、戴安全帽动画）
├── thinking/            # L2 决策拍板中（托腮、转圈圈思考）
├── fixing/              # 报错与自愈中（掏扳手、排查网络）
└── success/             # 任务完成（撒花、立正敬礼、递交报告）
```

> 💡 **Codex 桌宠一键提取**：Doloris 将内置一键提取工具，自动读取你当前 Codex App 中正在使用的桌宠动作序列，无缝转换为 Doloris 的代班皮肤！

---

## ⚡ 核心架构流程

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

### 核心设计原则
1. **严格单写者锁保护**：绝不以“文件 15 秒没动静”作为接管凭据；按 `call_id` 配对工具调用，验证操作系统真实写锁释放。
2. **拒不空等人工（Zero Human-in-the-Loop）**：常规设计确认与选择题交由 L2 拍板，杜绝把 `WAITING_USER` 当作常规路径退出。
3. **铁证验收（Evidence-Based Acceptance）**：不听信模型口头宣称的“我已完成”，严格核查实际文件产出、自建清单及执行测试。
4. **零外部依赖（Zero Dependencies）**：纯 Python 3.10+ 标准库实现，开箱即用，免除繁杂的 pip 依赖地狱。

---

## 🚀 快速上手

### 1. 环境需求
- Windows 10 / 11
- Python 3.10+
- 可选安装为全局命令：
  ```bash
  pip install -e .
  ```

### 2. 命令行一键启动

```powershell
# 推荐：一键启动 Fork 托管（默认模式，不杀 App）
.\doloris.cmd

# 明确指定模式
.\doloris.cmd fork      # Fork 无头续跑
.\doloris.cmd resume    # 原地接管恢复
.\doloris.cmd gui       # 原生双有头 GUI 注入

# 亦可使用全局注册命令（需 pip install -e .）
doloris --adopt last --quick --fork
```

*(原 `afk.cmd` / `afk2.cmd` / `afk3.cmd` 脚本均完整保留兼容)*

---

## 🧪 测试与验证

项目自带 100% 隔离的自测沙箱，不抢占真实工作区锁、不影响运行中的桌面应用：

```powershell
python -B -X utf8 tests/run_isolated.py
```

当前包含 **197 项自动化测试全部通过**，涵盖安全退出边界、状态机迁移、GUI 机器身份校验与协议解析。

---

## 📄 文档导航

- [使用手册与参数详解 (USAGE.md)](USAGE.md)
- [系统设计准则与技术规范 (docs/SPECIFICATION.md)](docs/SPECIFICATION.md)
- [开源贡献指南 (CONTRIBUTING.md)](CONTRIBUTING.md)
- [许可证 (LICENSE)](LICENSE)

---

## 📜 许可证

本项目基于 [MIT License](LICENSE) 开源发布。
