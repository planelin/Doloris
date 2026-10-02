"""afk_supervisor.compat — 已废弃的兼容层
========================================
历史上本模块通过 sys.modules["supervise"] 动态反查门面符号，使旧测试对
supervise 顶层的热补丁穿透到子模块。该机制已在分层收敛中移除 (生产代码全部
直调本模块导入)；测试如需替换边界, 请 patch 消费模块命名空间内的符号, 例如:

    patch("afk_supervisor.coordinator.l2_dispatch", ...)
    patch("afk_supervisor.cli.pause_codex_gui_session", ...)
    patch("afk_supervisor.platform.process.pid_is_running", ...)

本模块暂时保留空壳以维持 afk_supervisor.compat 导入路径与分层注册表的稳定。
"""
