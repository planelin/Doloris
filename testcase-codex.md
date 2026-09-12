# Codex 实弹测试案例 — 亲历"托管+自愈"全流程

## 第一步：在Codex里开任务（模拟平时干活）

在 Codex（桌面App/IDE，工作目录 = `C:\Agents\zcode\double`）新建会话，粘贴以下提示词：

```text
在 testproj/ 目录下构建一个「Agent监管系统监控台」纯静态网站，按顺序推进，每完成一个文件算一步：
1. testproj/index.html 总览页 + testproj/assets/style.css + testproj/assets/app.js
2. testproj/pages/ 下12个页面：01-heartbeat.html 到 12-report.html，
   每页介绍一种监管机制（心跳检测/指数退避/会话接管/供应商轮换/端点探针/出站代理/
   验收清单/干预存档/睡眠抑制/混沌测试/终态报告/授权边界），含导航链接回 index
3. testproj/data/mechanisms.json 写12条机制摘要（名称/一句话要点/当前状态），
   app.js 读取它渲染到 index 页
4. testproj/README.md 说明目录结构与本地打开方式
全部完成后停止，不要提问。
```

看到 `testproj/` 里陆续出现文件、Codex还在继续写时——**就是"要出门"的时刻**。

## 第二步：启动看门狗（两种玩法）

**玩法A（纯观察托管）**：直接双击 `afk.cmd`，或终端运行：

```bash
cd C:\Agents\zcode\double
.\afk.cmd
```

**玩法B（观察击杀+自愈，推荐）**：注入一次确定性故障，看它自己爬起来：

```bash
cd C:\Agents\zcode\double
.\afk.cmd --chaos kill:30
```

worker运行30秒时会被看门狗亲手击杀，然后你应该看到它自动检测→退避→续跑→写完。

## 第三步：对照观察点（黑窗口里的行话）

| 日志行 | 含义 |
|---|---|
| `ADOPT ... 等待会话静默` | 确认你已停止在原会话输入（15秒静默才接管） |
| `ADOPT 会话已静默, 开始接管` → `RESUME codex session=…` | 无头进程接管了你 leave 下的会话 |
| `HEARTBEAT alive: True, stale_sec: N` | 心跳正常，N是距上次会话活动的秒数 |
| `CHAOS_KILL` → `EXIT_CRASH` → `RESUME_WAIT` → `RESUMED` | 故障注入→检测→退避→自愈（玩法B） |
| `ERROR_SIGNATURE ['429', ...]` | 中转报错被看门狗看见（如果发生） |
| `EXIT_OK 快速验收: 勾选N, 未勾0` | worker自建清单且全部完成 |
| `TERMINAL SUCCESS — SHUTDOWN WOULD HAPPEN HERE` | 终态：验收通过，正式版此处会关机 |

## 第四步：验收成果

- 打开 `testproj/index.html`（浏览器直接开）——网站应该完整可用
- 打开 `runs\<最新时间戳>\report.md`——本次托管的时间线报告
- `afk-work\PROGRESS.md`——worker自建的完成清单

## 纪律（两条，违反会翻车）

1. **afk 启动后，别再往原 Codex 会话里打字**——同一会话不能有两个写入者；想看进度就看 afk 黑窗口或 `runs/` 报告
2. 接管的前提是会话静默15秒——启动 afk 前先停手，正在流式输出时启动它会等你（最多90秒后强行接管并告警）
