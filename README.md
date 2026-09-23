<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue.svg" alt="Python Version" />
  <img src="https://img.shields.io/badge/License-MIT-emerald.svg" alt="License" />
  <img src="https://img.shields.io/badge/Dependencies-Zero%20External-orange.svg" alt="Zero Dependencies" />
  <img src="https://img.shields.io/badge/BYOP-Custom%20Pet%20Ready-pink.svg" alt="BYOP Ready" />
  <img src="https://img.shields.io/badge/Platform-Windows-lightgrey.svg" alt="Platform Windows" />
  <a href="https://linux.do"><img src="https://img.shields.io/badge/Community-LINUX%20DO-2563eb.svg?logo=linux&logoColor=white" alt="LINUX DO Community" /></a>
</p>

<h1 align="center">Doloris (ドロリス)</h1>

<p align="center">
  <strong>面向 AI Coding Agent（Codex / Claude）的桌宠级长任务无人值守托管与自愈系统</strong><br>
  <em>“桌宠代班，长夜守护。”</em>
</p>

<p align="center">
  <a href="#-为什么是-doloris">项目理念</a> •
  <a href="#-三大托管模式">三大模式</a> •
  <a href="#-终端实况模拟">实况演示</a> •
  <a href="#-byop-自备桌宠生态">自备桌宠</a> •
  <a href="#-快速上手">快速上手</a> •
  <a href="#-测试与验证">自测验证</a> •
  <a href="USAGE.md">使用手册</a> •
  <a href="#-社区与友链">社区友链</a>
</p>

---

## 🌟 为什么是 Doloris？

在日常使用 Codex 或 Claude 等 AI 编程助手推进长流程任务时，开发者经常遭遇这样的困境：
- 离开座位（AFK）去开会或休息，模型却停留在**中间选择题**、**主视觉/架构确认提问**上，白白发呆一整夜；
- 遭遇偶发**网络断连（429 / 5xx）、工具报错或进程崩溃**，任务直接意外中断；
- 多个写入者冲突导致会话锁死，甚至产生“表面已完成”的代码幻觉。


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
[20:48:31] 🏆 TERMINAL  SUCCESS — 终态报告已生成
```

---

## 🛡️ 四大托管模式

Doloris 提供了四种开箱即用的交接形态，随时根据需要灵活切换：

| 模式 | 启动命令 | 工作原理 | 适用场景 |
|---|---|---|---|
| **1. Goal 目标模式** | `doloris goal`<br>或桌宠右键开启 | 60s 倒计时目标定义 / 智能懒人模式提炼 → 纯净下发 `/goal [目标]` → 自动方案审批与推荐选项跟进 → 看门狗守护运行 | 运行中途中断转 Goal，零额外 Token 消耗，自动跟进推荐决策，完工即交付，自带防熄屏 |
| **2. Fork 无头续跑** | `doloris fork`<br>或 `afk2.cmd` | 取得监管锁 → 请求暂停并无限等待原任务停止 → `fork` 子任务无头续跑 | 保留父任务记录，无需关闭 App，杜绝父子任务并发消耗 Token |
| **3. kill快速接管** | `doloris resume`<br>或 `afk.cmd` | 确认安全边界 → 退出 Codex App → 释放单写者锁 → 原地 headless resume | 最省系统资源，支持锁屏静默执行，历史在同会话延续 |
| **4. 双有头原生 GUI** | `doloris gui`<br>或 `afk3.cmd` | 保持 App 前台活跃 → 校验活动面板机器身份 → UI Automation 精确注入 | 桌面窗口完全可见，实时观察生成流动，视觉零中断、极高审计性 |

---

## 🐾 BYOP (Bring Your Own Pet) 自备桌宠生态

**桌宠不应被千篇一律的固定形象束缚！**

Doloris 采用**“底层大脑 + 表现层身体”完全解耦**的架构：
- **底层大脑（Python 3.10+ 标准库）**：负责严格的生命周期判定、写锁释放、L2 委托决策与自愈；
- **表现层桌宠（Desktop Mascot Engine）**：
  - **内置免素材矢量皮肤**：首发搭载 **Doloris**（纯代码矢量动态绘制，带鸭舌帽、吉他与萌系表情）与经典 **金毛小代班**；
  - **BYOP 自定义导入**：支持用户直接将任意素材包放入 `./pets/` 或 `~/.codex/pets/`，引擎自动加载：
    1. **Codex V2 官方 Atlas 大图**：将 1536x2288 的 `spritesheet.png` 放入文件夹，引擎自动按 8x11 矩阵切片为各状态动画；
    2. **分状态帧动画目录**：支持直接按动作建立子文件夹（`idle/`, `running/`, `waiting/`, `failed/`, `review/`）放入 PNG 序列图。

```text
pets/
└── my-custom-pet/
    ├── pet.json             # （可选）配置显示名称与元数据
    ├── spritesheet.png      # 方式 1：标准 1536x2288 Codex V2 Atlas 大图
    └── states/              # 方式 2：分状态散图序列
        ├── idle/            # 空闲发呆帧（01.png, 02.png...）
        ├── running/         # 托管打工中（敲键盘、戴安全帽动画）
        ├── waiting/         # L2 决策思考中（托腮、转圈圈思考）
        ├── failed/          # 报错与自愈中（掏扳手、排查网络）
        └── review/          # 任务完成（撒花、立正敬礼、递交报告）
```

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
4. **零第三方 Python 依赖（Zero Python Dependencies）**：核心监管引擎基于 Python 3.10+ 标准库实现，`pip install` 不拉取任何第三方包；仅在对交付的 JavaScript 做语法核验时可选调用系统 `node`。

---

## 🚀 快速上手

### 1. 环境需求
- **操作系统**：Windows 10 / 11
- **Python 版本**：Python 3.10+
- **第三方依赖**：
  - 核心 CLI 监管引擎：**零第三方 Python 依赖**（纯 Python 标准库）
  - 桌宠伴侣 GUI：需 `Pillow` 图形库（`pip install Pillow`）
- **可选外部工具**：
  - `node`：对交付目录内的 `.js` 文件做语法核验；缺失时该项记为 `SKIPPED` 并阻止相应功能项判为通过，不会静默放行。
  - `powershell`：GUI 双有头注入与网络适配器实验所需；Windows 10 / 11 自带。

---

### 2. 双重交互形态（任选其一）

#### 方式 A：桌面伴侣桌宠形态 (推荐，沉浸陪伴)

无需记忆任何复杂命令行参数，让可爱的代班桌宠常驻桌面角落：

```powershell
# 启动桌宠应用（控制台模式）
.\doloris-app.cmd

# 或双击根目录下的 doloris-app.vbs 静默启动（无黑框弹出）
```

**桌宠交互指南**：
* 🐾 **一键托管**：鼠标**右键点击桌宠** -> 在弹出菜单中选择「🚀 开始托管」-> 展开最近活跃的 Codex 会话列表，点击即开！
* 🖱️ **自由移动**：按住**鼠标左键**即可将桌宠拖拽至屏幕任意位置。
* 🔍 **无级缩放**：按住 `Ctrl + 鼠标滚轮` 即可在 **5% ~ 500%** 之间平滑无级缩放；亦可在右键菜单中直接输入精确百分比。
* 💬 **状态感知气泡**：桌宠头顶带有动态漫画气泡，实时展示任务心跳（打工中、决策思考中、故障自愈中、已完工）；完工后点击气泡可直接打开交付报告！
* 🎨 **皮肤切换**：右键菜单支持在默认皮肤（Doloris）与测试皮肤（金毛小代班）及自定义 BYOP 皮肤之间无缝热插拔。

---

#### 方式 B：终端命令行 CLI 形态 (极客 & 自动化集成)

适合喜欢纯终端操作、远程脚本调用或无桌面环境的使用场景：

```powershell
# 推荐：一键启动 Fork 托管（默认模式，不杀 App）
.\doloris.cmd

# 明确指定三大托管模式
.\doloris.cmd fork      # Mode 1: Fork 无头续跑 (保留 App，不争抢Token)
.\doloris.cmd resume    # Mode 2: 经典安全接管 (安全退出 App，原会话静默续跑)
.\doloris.cmd gui       # Mode 3: 原生双有头 GUI 注入 (App 保持前台可见，UIA直写)

# 亦可使用全局注册命令（需 pip install -e .）
doloris --adopt last --quick --fork
```

*(原 `afk.cmd` / `afk2.cmd` / `afk3.cmd` 脚本均完整保留向后兼容)*

---

## 🧪 测试与验证

项目自带 100% 隔离的自测沙箱，不抢占真实工作区锁、不影响运行中的桌面应用：

```powershell
# 运行完全隔离的沙箱回归测试
python -B -X utf8 tests/run_isolated.py

# 或运行全量单元测试套件
python -m unittest
```

当前包含 **260 项自动化测试**（全量套件与完全隔离沙箱均为 260/260 通过），涵盖安全退出边界、状态机迁移、GUI 机器身份校验、桌宠切片解析、懒人模式目标回退与协议序列化；GitHub Actions 会在 Windows + Python 3.10/3.12 上执行 Ruff 静态检查和同一套测试。

---

## 📄 文档导航

- [使用手册与参数详解 (USAGE.md)](USAGE.md) — 适合新用户的全流程上手指南与常见场景
- [系统设计准则与技术规范 (docs/SPECIFICATION.md)](docs/SPECIFICATION.md) — 核心设计哲学、安全红线与状态机规范
- [开源贡献指南 (CONTRIBUTING.md)](CONTRIBUTING.md) — 架构解析与 PR 提交流程
- [许可证 (LICENSE)](LICENSE) — MIT 开源授权协议

---

## 🤝 社区与友链

本项目首发并活跃于 **[LINUX DO](https://linux.do)** 技术社区：
- **论坛交流**：[LINUX DO (https://linux.do)](https://linux.do)
- **社区探讨**：欢迎前往 LINUX DO 社区参与 Doloris 的长任务无人值守经验交流、架构探讨与 BYOP 自定义桌宠皮肤分享！
- **社区精神**：*真诚、友善、团结、专业*，共建高品质技术社区。

---

## 📜 许可证

本项目基于 [MIT License](LICENSE) 开源发布。
