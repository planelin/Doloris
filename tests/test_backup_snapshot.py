"""接管前工作区快照的回归测试。

历史故障：run_dir 位于被备份工作区内部 (scwd=<项目父目录>, run_dir=<项目>/runs/<ts>)，
zip 直接写进 run_dir，os.walk 把"正在写的 zip"当成普通文件卷进自身，
runs/ 膨胀到 13.5GB，所有归档都无法解压，fork 模式永久卡在 BACKUP 阶段。
这里锁定修复后的不变量：有界耗时、排除自身产物、原子落盘、不留半成品。
"""

import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from afk_supervisor.cli import backup_workspace


class BackupSnapshotRegressions(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="afk-backup-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.ws = self.root / "workspace"
        (self.ws / "src").mkdir(parents=True)
        (self.ws / "src" / "app.py").write_text("value = 1\n" * 50, encoding="utf-8")
        # run_dir 位于工作区内部，且 runs/ 下已有历史备份产物。
        self.run_dir = (self.ws / "runs" / "20260923-115900").resolve()
        self.run_dir.mkdir(parents=True)

    def test_archive_never_contains_the_run_directory_or_its_own_output(self):
        stale = self.run_dir / "backup-pre-adopt-workspace-20250101-000000.zip"
        stale.write_bytes(b"PK\x03\x04" + b"0" * 4096)

        started = time.monotonic()
        archive = backup_workspace(self.ws, self.run_dir, max_size_mb=50)
        elapsed = time.monotonic() - started

        # 必须在有界时间内完成，且不能因为遍历自身产物而递归膨胀。
        self.assertLess(elapsed, 30.0)
        self.assertIsNotNone(archive)
        self.assertEqual(archive.parent.resolve(), self.run_dir.resolve())
        self.assertTrue(archive.name.startswith("backup-pre-adopt-workspace-"))
        self.assertTrue(archive.name.endswith(".zip"))

        with zipfile.ZipFile(archive) as zf:
            self.assertIsNone(zf.testzip())
            names = zf.namelist()
            self.assertIn("src/app.py", names)
            self.assertFalse([n for n in names if n.startswith("runs/")])
            self.assertFalse([n for n in names if "backup-pre-adopt" in n])

        # 历史归档保持原样，未被重新写入或被删除。
        self.assertEqual(stale.stat().st_size, 4096 + 4)

    def test_no_partial_file_survives_and_archive_is_atomic(self):
        archive = backup_workspace(self.ws, self.run_dir, max_size_mb=50)

        self.assertIsNotNone(archive)
        self.assertEqual(list(self.run_dir.glob("*.part")), [])
        with zipfile.ZipFile(archive) as zf:
            self.assertIn("src/app.py", zf.namelist())

    def test_file_count_limit_truncates_without_corrupting_archive(self):
        for index in range(40):
            (self.ws / "src" / f"mod_{index:03d}.py").write_text("x = 1\n", encoding="utf-8")

        archive = backup_workspace(self.ws, self.run_dir, max_size_mb=50, max_files=5)

        self.assertIsNotNone(archive)
        self.assertEqual(list(self.run_dir.glob("*.part")), [])
        with zipfile.ZipFile(archive) as zf:
            self.assertIsNone(zf.testzip())
            self.assertLessEqual(len(zf.namelist()), 5)

    def test_oversized_single_file_is_skipped(self):
        huge = self.ws / "src" / "huge.bin"
        with open(huge, "wb") as stream:
            stream.truncate(60 * 1024 * 1024)

        archive = backup_workspace(self.ws, self.run_dir, max_size_mb=100)

        self.assertIsNotNone(archive)
        with zipfile.ZipFile(archive) as zf:
            self.assertNotIn("src/huge.bin", zf.namelist())
            self.assertIn("src/app.py", zf.namelist())

    def test_failure_leaves_no_partial_archive(self):
        # 在遍历过程中抛错：此时 *.zip.part 已经创建，必须被就地清理。
        with patch("afk_supervisor.cli._is_backup_artifact", side_effect=RuntimeError("boom")):
            archive = backup_workspace(self.ws, self.run_dir, max_size_mb=50)

        self.assertIsNone(archive)
        self.assertEqual(list(self.run_dir.glob("*.part")), [])
        self.assertEqual(list(self.run_dir.glob("*.zip")), [])

    def test_missing_workspace_is_not_an_error(self):
        self.assertIsNone(backup_workspace(self.root / "nope", self.run_dir))


if __name__ == "__main__":
    unittest.main()
