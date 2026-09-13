# 值守策略 — codex://threads/01a099ed-44b7-7df0-9c6c-f789af15cd9b

用户 2026-09-13 16:49 前后外出，委托 ZCode 反复轮询该会话状态并自动回复。

## 会话目标（用户原话摘要）
- 优化 mujica 计划的简易 3D 模型管线（ taki_stage4_rigged.blend）
- 升级 `C:\Agents\codex\mujica4_test\3.0` 的既有管线经验 → 产出**更优质的管线经验**
- 可用 Blender：`C:\blender\blender-4.3.2-windows-x64\blender.exe` 实测验证
- 终极目的：便利后续重复此计划（可重复、可验证）

## 自动回复原则（按优先级）
1. 不改动原始模型；测试结果留在实验目录 `C:\Agents\codex\mujica4_test`
2. 实测结论与未验证经验必须分开标注；验收口径要可验证
3. 鼓励一口气完成、不中途提问；角色参数集中配置、每阶段失败即停并保留结果
4. 若 agent 提出事实性问题：优先依据上述目标回答；无法确定的选择与目标一致
   的方向，并在回复中显式声明假设，便于用户回来纠正
5. 若 agent 报告完成：核对经验库(4.0)是否落盘、是否经 Blender 验证，未达标则
   指出缺口要求继续；达标则礼貌收尾，不再强推新轮次

## 事件处理
- task_complete → 读最后 agent 消息 → 决定回复 → GUI 发送 → 重启看门
- STALL(420s 无写入) → 截图检查：审批弹窗则逐条审阅（Blender/文件操作在
  实验目录内→批准；删改实验目录外/下载可执行文件/系统配置→拒绝并说明）
- error/turn_aborted → 记录，尝试重新发送继续指令一次
- App 进程死亡 → 记录；尝试重启 App 并恢复线程（codex:// 深链）
- 预算：最多 12 次干预回复；连续 2 次"无实质进展"后停止干预，仅监控

## 产物
- interventions.jsonl 事件流水
- last-agent-msg.txt 每次 task_complete 的最终消息快照
- report.md 值守报告（用户回来先读这个）
