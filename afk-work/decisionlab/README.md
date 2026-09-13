# 决策实验站

一个纯静态、无构建依赖的四页实验站，用连续路径展示心跳检测、会话接管、决策代理与终态报告。

## 目录结构

```text
decisionlab/
├── index.html                  # 站点总览与实验路径入口
├── PROGRESS.md                 # 六项交付检查清单
├── README.md                   # 本文档
├── assets/
│   ├── app.js                  # 移动导航、当前页标记与进入动画
│   └── style.css               # 全站暗色赛博风样式
├── data/
│   └── config.json             # 两次决策结果记录
└── pages/
    ├── 01-heartbeat.html       # 心跳检测
    ├── 02-takeover.html        # 会话接管
    ├── 03-decision.html        # 决策代理
    └── 04-report.html          # 终态报告
```

## 本地打开方式

方式一：直接双击 `index.html`。站点使用相对路径，不依赖服务器，可在现代浏览器中直接浏览。

方式二：在 `decisionlab/` 目录启动任意静态文件服务器，然后访问显示的本地地址。例如：

```bash
python -m http.server 8000
```

随后打开 `http://localhost:8000/`。建议使用第二种方式验证完整的本地导航行为。

## 决策记录

| 决策门 | 选项 | 实际结果 | 记录值 |
| --- | --- | --- | --- |
| A：全站视觉主题 | 甲：暗色赛博风 | 采用主题甲：深色背景、霓虹蓝绿配色 | `cyber-dark` |
| B：全站文案语言 | 甲：全部简体中文 | 采用选项甲：全部简体中文 | `zh-CN` |

结构化结果保存在 `data/config.json`：

```json
{
  "theme": "cyber-dark",
  "lang": "zh-CN"
}
```
