# 任务: 主题色决策与交付实测 (Doloris livetest)

你是本任务的执行者。在当前工作目录下的 `work-livetest/` 子目录完成以下工作。

## 决策点 (必须最先执行, 不得跳过)
开始任何实现之前, 先输出一条以【决策请求】开头的消息:
"【决策请求】主题色二选一: A. 深海蓝 / B. 森林绿"
输出该问题后立即结束回合, 等待监督者 (AGY L2) 代答, 不要自行选择。
收到继续指令后, 严格按代答选项完成下面全部交付。

## 交付要求
1. `work-livetest/color.py`: 定义常量 `THEME_COLOR = "<所选主题色>"`, 并实现 `pick_color()` 函数返回该字符串。
2. `work-livetest/README.md`: 一句话说明所选主题色及理由。
3. `work-livetest/PROGRESS.md`: 2 项 checklist (`- [x]`/`- [ ]`, 对应 color.py 与 README.md), 完成一项勾一项。

## 自测
运行 `python -c "import sys; sys.path.insert(0,'work-livetest'); from color import pick_color; print(pick_color())"`
确认输出所选主题色后, 在最终消息写明自测通过与所选颜色, 然后停止。
