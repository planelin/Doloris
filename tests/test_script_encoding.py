"""PowerShell 脚本编码回归测试。

历史故障：gui_inject.ps1 含中文注释却以无 BOM 的 UTF-8 保存，而 gui.py 用
``powershell`` (Windows PowerShell 5.1) 执行它。5.1 对无 BOM 文件按系统 ANSI
代码页 (CP936) 解码，中文注释的尾字节会与换行拼成一个双字节字符，于是吞掉下一行：

    // 仍然只在同一 SID 的机读标识前提下生效，不退回纯标题匹配。
    private static readonly char[] TitleSeparators = new char[] { (char)0x1F };

两行被合并为一条 ``//`` 注释，Add-Type 编译报 "当前上下文中不存在名称 TitleSeparators"，
注入脚本在 Add-Type 处直接退出：gui 模式表现为"接管后无任何反应"，
kill / fork 的 GUI 身份校验也一并失效。

这里锁定两条不变量：含非 ASCII 的 .ps1 必须带 UTF-8 BOM；按 PowerShell 5.1 的规则解码后，
源码里没有注释的代码行必须仍然独立成行。另加一条打包副本与根目录副本必须逐字节一致的断言。
"""

import codecs
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".venv", "__pycache__", "build", "node_modules", "runs", "work"}


def _powershell_scripts():
    for path in sorted(REPO_ROOT.rglob("*.ps1")):
        if any(part in SKIP_DIRS for part in path.relative_to(REPO_ROOT).parts):
            continue
        yield path


def _decode_like_windows_powershell(data: bytes) -> str:
    """有 BOM 时按 UTF-8，无 BOM 时按系统 ANSI 代码页 (本机 CP936)。"""
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig")
    return data.decode("cp936", errors="replace")


class ScriptEncodingRegressions(unittest.TestCase):
    def test_non_ascii_scripts_carry_utf8_bom(self):
        offenders = []
        for script in _powershell_scripts():
            data = script.read_bytes()
            if not any(byte > 0x7F for byte in data):
                continue
            if not data.startswith(codecs.BOM_UTF8):
                offenders.append(str(script.relative_to(REPO_ROOT)))
        self.assertEqual(
            offenders,
            [],
            "含非 ASCII 的 .ps1 必须保存为 UTF-8 BOM，否则 Windows PowerShell 5.1 会按 ANSI 解码",
        )

    def test_powershell51_decoding_never_swallows_code_lines(self):
        for script in _powershell_scripts():
            data = script.read_bytes()
            source_lines = data.decode("utf-8-sig").splitlines()
            decoded_lines = {
                line.strip()
                for line in _decode_like_windows_powershell(data).splitlines()
                if line.strip()
            }
            for number, line in enumerate(source_lines, start=1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                with self.subTest(script=script.name, line=number):
                    self.assertIn(
                        stripped,
                        decoded_lines,
                        f"{script.relative_to(REPO_ROOT)}:{number} 被注释吞掉，"
                        "Windows PowerShell 5.1 执行时会丢失该行代码",
                    )

    def test_root_and_packaged_injector_are_byte_identical(self):
        root_copy = REPO_ROOT / "gui_inject.ps1"
        packaged_copy = REPO_ROOT / "afk_supervisor" / "platform" / "gui_inject.ps1"
        self.assertEqual(root_copy.read_bytes(), packaged_copy.read_bytes())

    def test_injectors_force_utf8_stdout_before_writing_receipts(self):
        """回执乱码回归: 5.1 被重定向输出时按 [Console]::OutputEncoding 编码字符串。

        默认是系统 OEM 代码页 (本机 CP936), 探针的中文结论到了 Python 侧就成了
        乱码; 因此两份脚本都必须在首次输出前把控制台输出编码固定为无 BOM 的 UTF-8。
        """
        for script in (REPO_ROOT / "gui_inject.ps1", REPO_ROOT / "afk_supervisor" / "platform" / "gui_inject.ps1"):
            text = script.read_text(encoding="utf-8-sig")
            with self.subTest(script=str(script.relative_to(REPO_ROOT))):
                self.assertIn("[Console]::OutputEncoding", text)
                self.assertIn("UTF8Encoding", text)
                self.assertLess(
                    text.index("[Console]::OutputEncoding"),
                    text.index("Add-Type"),
                    "输出编码必须在脚本开始产出回执前设定",
                )


if __name__ == "__main__":
    unittest.main()
