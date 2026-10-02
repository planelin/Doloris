"""包内分层规则静态校验 (AST 扫描, 防分层退化)。

目标分层 (由下至上):
  L0 core            — 零内部依赖 (log 等)
  L1 基础层           — models / storage / compat / state / baseline / sessions / evidence / observations
  L2 平台层           — platform / drivers
  L3 决策与协议层     — l2 / decisions / verification / acceptance
  L4 编排层           — coordinator / engine / gui_engine / goal_engine / reporting / cli / supervise

历史上曾出现 5 条反向依赖边 (见 docs), 已全部切断并以本测试固化:
  sessions.discovery -> platform.process (log)      -> 已下沉 core.log
  l2.protocol/transport -> baseline (TaskBaseline)  -> 已下沉 models
  acceptance -> l2.transport (worker_last_message)  -> 已移入 sessions.rollout
  l2.transport -> drivers.claude (get_relay_pool)   -> 已改为依赖注入
  (compat.get_sym 的 supervise 反查仍保留, 属测试热补丁机制, 不在静态规则内)
"""
import ast
import unittest
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent / "afk_supervisor"

LAYERS = {
    "core": 0,
    "models": 1, "storage": 1, "compat": 1, "state": 1, "baseline": 1,
    "sessions": 1, "evidence": 1, "observations": 1, "actions": 1,
    "platform": 2, "drivers": 2,
    "l2": 3, "decisions": 3, "verification": 3, "acceptance": 3,
    "coordinator": 4, "engine": 4, "gui_engine": 4, "goal_engine": 4,
    "reporting": 4, "cli": 4,
}

# 显式禁止的跨层导入 (module_prefix -> 禁止导入的顶层子模块集合)
FORBIDDEN = {
    "core": set(LAYERS) - {"core"},
    "models": set(LAYERS) - {"models"},
    "storage": set(LAYERS) - {"storage"},
    "compat": set(LAYERS) - {"compat"},
    "sessions": {"platform", "drivers", "l2", "decisions", "coordinator",
                 "engine", "gui_engine", "goal_engine", "reporting", "cli"},
    "baseline": {"platform", "drivers", "l2", "decisions", "coordinator",
                 "engine", "gui_engine", "goal_engine", "reporting", "cli"},
    "platform": {"drivers", "l2", "decisions", "coordinator",
                 "engine", "gui_engine", "goal_engine", "reporting", "cli"},
    "drivers": {"l2", "decisions", "coordinator",
                "engine", "gui_engine", "goal_engine", "reporting", "cli"},
    "l2": {"drivers", "baseline", "coordinator",
           "engine", "gui_engine", "goal_engine", "reporting", "cli"},
    "decisions": {"platform", "drivers", "l2", "coordinator",
                  "engine", "gui_engine", "goal_engine", "reporting", "cli"},
}


def package_imports(tree: ast.AST):
    """收集模块内全部 afk_supervisor.* 导入 (含函数内延迟导入)。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("afk_supervisor"):
            yield node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("afk_supervisor"):
                    yield alias.name


def top_module(dotted: str) -> str:
    parts = dotted.split(".")
    return parts[1] if len(parts) > 1 else ""


class LayeringRules(unittest.TestCase):
    def test_every_module_maps_to_a_layer(self):
        for py in PKG_ROOT.rglob("*.py"):
            rel = py.relative_to(PKG_ROOT).with_suffix("").as_posix()
            top = rel.split("/")[0]
            if top == "__init__" or "__pycache__" in rel:
                continue
            self.assertIn(top, LAYERS, f"新顶层子包 {top} 未登记分层, 请更新 LAYERS")

    def test_no_reverse_dependencies(self):
        violations = []
        for py in PKG_ROOT.rglob("*.py"):
            rel = py.relative_to(PKG_ROOT).with_suffix("").as_posix()
            if "__pycache__" in rel:
                continue
            top = rel.split("/")[0]
            banned = FORBIDDEN.get(top)
            if banned is None:
                continue
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            for mod in package_imports(tree):
                imported = top_module(mod)
                if imported in banned:
                    violations.append(f"{rel} -> {mod}")
        self.assertEqual(violations, [], "发现反向/越层依赖:\n" + "\n".join(violations))

    def test_known_cut_edges_stay_cut(self):
        """四条已切断的历史反向边逐一固化。"""
        forbidden_pairs = [
            ("sessions/discovery.py", "afk_supervisor.platform.process"),
            ("l2/protocol.py", "afk_supervisor.baseline"),
            ("l2/transport.py", "afk_supervisor.baseline"),
            ("l2/transport.py", "afk_supervisor.drivers.claude"),
            ("acceptance.py", "afk_supervisor.l2.transport"),
        ]
        for rel_mod, banned_mod in forbidden_pairs:
            py = PKG_ROOT / rel_mod
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            mods = list(package_imports(tree))
            self.assertNotIn(banned_mod, mods, f"{rel_mod} 不得再导入 {banned_mod}")

    def test_taskbaseline_single_source_of_truth(self):
        from afk_supervisor.baseline import TaskBaseline as from_baseline
        from afk_supervisor.models import TaskBaseline as from_models
        self.assertIs(from_baseline, from_models)


if __name__ == "__main__":
    unittest.main()
