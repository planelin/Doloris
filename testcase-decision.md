# 决策环路实弹测试 — 决策实验站

测试目标: 验证 v2.5 交互决策环路——codex在决策门停下 → afk识别【决策请求】→
agy(flash)按委托策略代答 → 决定喂回codex → 产物肉眼可验证。

## 第一步: Codex新会话(工作目录 = C:\Agents\zcode\double), 粘贴此提示词

```text
在 decisionlab/ 目录下构建一个「决策实验站」纯静态网站。严格按以下阶段顺序推进,
每个阶段完成后立即更新 decisionlab/PROGRESS.md(6项清单: 基建/内容页/主题应用/
语言应用/配置记录/说明文档, 用 - [x] / - [ ] 勾选):

阶段1(基建): decisionlab/index.html + decisionlab/assets/style.css + decisionlab/assets/app.js,
  包含指向4个内容页的导航骨架。

阶段2(内容页): decisionlab/pages/ 下4个主题页: 01-heartbeat.html(心跳检测)、
  02-takeover.html(会话接管)、03-decision.html(决策代理)、04-report.html(终态报告),
  每页至少3段实质内容。完成本阶段后先停下来。

阶段3(决策门A): 需要用户决定全站视觉主题, 停止并在最终消息以【决策请求】开头,
  给出两个选项:
  主题甲: 暗色赛博风(深色背景+霓虹蓝绿配色)
  主题乙: 亮色极简风(白色背景+黑字橙色点缀)
  收到选择后, 把选定主题应用到所有页面的样式中。

阶段4(决策门B): 再次需要用户决定全站文案语言, 停止并在最终消息以【决策请求】开头:
  选项甲: 全部简体中文
  选项乙: 全部英文
  收到选择后, 把所有页面文案改为所选语言。

收尾: decisionlab/data/config.json 用JSON记录两次决策的实际结果(字段: theme, lang),
decisionlab/README.md 说明目录结构、本地打开方式与决策记录。全部完成后停止。

规则: 需要决策时严格使用【决策请求】格式并给出甲/乙选项, 其余事项自行处理;
只使用文件读写工具; 每个阶段一口气完成。
```

## 第二步: 时机(关键)

- 看到阶段1完成(index.html出现)后, **立刻启动afk**——这样两个决策门都会发生在
  接管之后, 由agy代答:
  ```powershell
  .\afk.cmd --max-run-sec 0
  ```
- 启动afk后不要再往Codex会话里打字。

## 第三步: 预期链路(黑窗口)

```
ADOPT(标题: 在 decisionlab/ 目录下构建...) → 接管 → HEARTBEAT
→ worker继续阶段2 → EXIT_OK + INTERACTION(检测到【决策请求】)
→ L2_CONSULT → Antigravity里出现决策会话(flash模型在甲/乙间拍板)
→ L2_ANSWER verdict=PROCEED → RESUMED_WITH_DECISION
→ 主题应用 → 第二次 INTERACTION → L2_CONSULT → ... → 语言应用
→ EXIT_OK 快速验收全勾 → TERMINAL SUCCESS
```

## 第四步: 验收(三处证据)

1. **打开 decisionlab/index.html**——主题是暗色还是亮色?文案是中文还是英文?
   → 这就是agy替你做的两个决定, 肉眼可验证
2. `runs\<最新>\answer-1.txt` / `answer-2.txt`——agy给出的决策指令原文
3. `runs\<最新>\interventions.jsonl`——INTERACTION / L2_CONSULT / L2_ANSWER /
   RESUMED_WITH_DECISION 事件时间线

## 可选加练(故障+修复链路)

阶段3进行中时切断cc-switch路由 → worker崩溃×2 → L2_ESCALATE(修复模式) →
Antigravity里出现诊断修复会话 → 改写~/.codex/config.toml绕开故障路由 →
worker复活续跑。观察顺序: 先决策表演, 后修复表演。

## 纪律

- afk启动后别碰Codex会话(单写者锁)
- agy拍板可能与你个人偏好不同——这正是测试点: 验证委托决策链路是否闭环,
  而不是验证agy猜你喜欢
