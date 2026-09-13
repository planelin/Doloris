# Agent 监管系统监控台

纯静态、零后端的监管机制监控台。总览页通过 `data/mechanisms.json` 渲染 12 项机制摘要，各机制页提供独立的说明、判定信号、处置步骤和失效模式。

## 目录结构

```text
testproj/
├── index.html                 # 总览页
├── README.md                  # 本说明
├── assets/
│   ├── style.css              # 全站样式
│   └── app.js                 # 时钟、滚动显现、状态渲染
├── data/
│   └── mechanisms.json        # 12 条机制摘要与页面索引
└── pages/
    ├── 01-heartbeat.html      # 心跳检测
    ├── 02-backoff.html        # 指数退避
    ├── 03-takeover.html       # 会话接管
    ├── 04-rotation.html       # 供应商轮换
    ├── 05-probe.html          # 端点探针
    ├── 06-egress.html         # 出站代理
    ├── 07-checklist.html      # 验收清单
    ├── 08-archive.html        # 干预存档
    ├── 09-sleep-guard.html    # 睡眠抑制
    ├── 10-chaos.html          # 混沌测试
    ├── 11-report.html         # 终态报告
    └── 12-boundary.html       # 授权边界
```

## 本地打开方式

推荐在 `afk-work/testproj/` 目录启动一个本地静态服务器，这样浏览器可以直接读取 `data/mechanisms.json`：

```powershell
python -m http.server 8000
```

然后访问 `http://localhost:8000/`。

也可以直接双击 `index.html`。大多数浏览器会拦截 `file://` 页面读取本地 JSON，此时 `app.js` 会自动使用内置摘要副本完成渲染，页面功能仍然可见；要验证真实的 JSON 数据源，请使用上面的本地服务器方式。

## 页面约定

- 所有页面只依赖本地 `assets/style.css` 与 `assets/app.js`。
- 机制页通过 `data-root=".."` 声明站点根路径，可读取总览页共享的数据文件。
- 页面导航与机制编号保持一致：`11` 为终态报告，`12` 为授权边界。
- 页面无外部字体、图标或网络请求，可直接离线部署到任意静态托管目录。
