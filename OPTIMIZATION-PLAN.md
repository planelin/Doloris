# Doloris 优化执行计划（自主长会话，2026-10-02）

> 本文档是自主工作会话的持久锚点：每完成一项就把 `[ ]` 改 `[x]`；发现风险超预期的项目降级记入"遗留项"，不硬改。
> 护栏：每批完成 = 沙箱 + unittest + ruff 全绿后本地提交；绝不 push；沙箱基线 508/508 只增不减。

## Batch 0 — 卫生与提交基线
- [x] 写入本计划文档
- [x] 删除 build/ dist/ doloris.egg-info/（陈旧构建副本）
- [x] 删除根目录遗留测试副本 work-livetest/（保留 work/work-livetest/ 运行证据）
- [x] 提交 1: feat: core/log 第0层包（inert）
- [x] 提交 2: fix+refactor: P0/P1 正确性批次与分层重构（含全部测试，508/508 状态；计划中的 a/b 合并——两者在 cli/coordinator/transport 内交织，拆分会产生不可导入的中间提交）
- [x] 提交 3: chore: 移除跟踪的运行产物 + .gitignore 补全（+livetest 夹具入库、work*/ 忽略）
- [x] 提交后全量沙箱确认绿（508/508 + ruff 通过）

## Batch 1 — 正确性修复 10 项（逐项先写回归测试）
- [x] 1. engine.py:401 恢复元组加 "repair"，移除死值 "early_exit"（L2 抖动 → 死循环的 P0）
- [x] 2. goal_engine.py:1651 删除尾部 `or settle_quiet`（planning 阶段安静 8 秒误判成功的 P1）
- [x] 3. L2 不确定投递保留 pending 标记 phase=UNCERTAIN（transport.py:380-407,595-600）
- [x] 4. 带 cid 的过期 pending 恢复路径：转录找答案→采纳/清理；死亡→解绑（transport.py:198-199）
- [x] 5. 旧轮响应不可采信：USER_INPUT 行号之后的 PLANNER_RESPONSE 才有效；非协议响应须回带 request_id（bridge.py:259-287）
- [x] 6. 工作区锁 TOCTOU：unlink 前复检内容快照（process.py:170-176）
- [x] 7. cmd.exe /c 元字符启动前检测、快速失败（codex.py:79 / claude.py:170）
- [x] 8. gui_engine.py:313 交互预算只对 is_interaction_request 计数
- [x] 9. goal 限流熔断：连续失败计数（对齐 gui 8 连击）+ 有界 sleep（goal_engine.py:1541-1560）
- [x] 10. min_mtime 用回合起点时间替代 st_ctime（gui_engine.py:376, cli.py:674）
- [x] 附带: storage.py 重试放宽 ~2s + observations.py best-effort 跳过无变化
- [x] 附带: task.md 读取 errors="replace"（cli.py:429）
- [x] 附带: reporting.py 报告写盘保护 + detail 单行化
- [x] 附带: goal 终态 webhook 去重（goal_engine.py finish_goal）
- [x] 附带: terminal_finalized 在 4 处终态路径真正置位
- [x] 审查代理复核 diff → 全绿 → 提交

## Batch 2 — 验收门对抗性加固
- [x] 否定词全文优先扫描（acceptance.py:356-371）
- [x] checklist 逐行解析（剥围栏代码块）替换 5 处子串计数，统一 helper
- [x] evidence.py:386 status 由检查分支直接给
- [x] 协议收紧：PASS 须引显式 PASS 证据项 / REVIEW 空 revision 拒绝 / FAIL 须引 FAIL 判据 / blockers+repairs 过 FORBIDDEN / 删重复死检查
- [x] extract_protocol_json 多候选 fail-closed
- [x] redaction.py 补 ghp_/github_pat_/AKIA/AIza/连字符键名/Cookie/C:/Users
- [x] 审查代理复核 → 全绿 → 提交

## Batch 3 — 性能
- [x] rollout 快照缓存 load_events(path,size,mtime_ns)；goal 4 个 loader 吃快照
- [x] compute_artifact_revision_only() 轻量变更检测；check_completion 复核降为 hash-only
- [x] find_codex_session_by_id 绑定后跳过全树 rglob
- [x] acceptance glob 目录排除 + 文件数上限
- [x] 审查代理复核 → 全绿 → 提交

## Batch 4 — 结构债
- [x] get_sym 收敛（25 处/17 符号；先 verdict 路径，后纯函数，同步改测试，最后删反查）
- [x] 死代码清理：get_recent_workspace_files / wait_for_codex_idle / codex_rollout_is_turn_complete / read_rollout_last_message / state.py 死字段（同步更新 supervise.py 门面）
- [ ] 视余量拆 run_l2_antigravity（六阶段）与 run_goal_supervisor；不够则记录方案
- [x] 审查代理复核 → 全绿 → 提交

## Batch 5 — 文档与终验
- [ ] USAGE.md 重写第 8 节（子命令语法/Goal 补章/--resume 修正）；CONTRIBUTING 补 ruff 门槛
- [ ] 终验：沙箱 + unittest + ruff 全绿
- [ ] 真实 E2E 冒烟：tasks/livetest-agy 再跑一轮完整监管
- [ ] 更新本文档勾选与遗留项；输出最终变更报告

## 遗留项（风险超预期或时间不足时记录于此）
- 遗留自 Batch 1 审查 (设计取舍, 已记录):
  - goal 静默推进在无窗口主机上会消耗 autopilot 预算至 FAILED (~7.5min); 可考虑注入 RECORDED (未送达) 时不计数
  - DECIDE/REPAIR 采纳路径不校验 revision (无 evidence_packet); 现依赖重试同问语义
- Batch 4 未做: run_l2_antigravity (~600行) 与 run_goal_supervisor (~1700行) 的阶段化拆分;
  建议下会话以纯函数抽取方式做 (六阶段地图见 Batch 2 审查报告)
- Batch 2 提及的 gui_inject.ps1 手拼 JSON 改 ConvertTo-Json (探针硬阻断被静默吞) 未处理
- Batch 1-2 的 UNCERTAIN pending: 新 req 轮询沿用旧 initial_line_count, 若 AGY 侧轮转可能漏读 (低概率)
- 真实 E2E 冒烟 #2 (2026-10-02 21:57): SUCCESS, 3 分钟, reviews=1, terminal_finalized=True (Batch 1 修复生产验证)
