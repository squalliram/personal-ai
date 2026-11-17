import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from instagram_agent import host_probe


class HostProbeTests(unittest.TestCase):
    def test_reports_tools_limits_and_disk(self):
        report = host_probe.lines(Path.cwd())
        self.assertTrue(report[0].startswith("ffmpeg: "))
        self.assertTrue(report[1].startswith("ffprobe: "))
        self.assertTrue(any(line.startswith("cpus: ") for line in report))
        self.assertTrue(any(line.startswith("disk free: ") for line in report))

    def test_cgroup_limits_come_from_the_process_cgroup(self):
        root = Path("/cg")
        v2 = host_probe.cgroup_files("0::/user.slice/lve/1001\n", root)
        self.assertIn((root / "user.slice/lve/1001/memory.max", "cgroup memory.max"), v2)
        self.assertIn((root / "user.slice/lve/1001/cpu.max", "cgroup cpu.max"), v2)
        v1 = host_probe.cgroup_files("4:memory:/lve/1001\n3:cpu,cpuacct:/lve/1001\n2:pids:/x\nbad\n", root)
        self.assertEqual(
            v1,
            [
                (root / "memory/lve/1001/memory.limit_in_bytes", "cgroup v1 memory limit"),
                (root / "cpu/lve/1001/cpu.cfs_quota_us", "cgroup v1 cpu quota (us)"),
            ],
        )

    def test_missing_tool_is_not_found(self):
        with (
            mock.patch.object(host_probe.shutil, "which", return_value=None),
            mock.patch.object(host_probe, "TOOL_DIRS", ()),
        ):
            self.assertIsNone(host_probe.find_tool("ffprobe"))
            self.assertIn("ffprobe: not found", host_probe.lines(Path.cwd()))

    def test_tool_in_extra_folder_and_its_version(self):
        with tempfile.TemporaryDirectory() as folder:
            tool = Path(folder) / "ffprobe"
            tool.write_text("#!/bin/sh\necho 'ffprobe version 7.1 Copyright'\n")
            tool.chmod(stat.S_IRWXU)
            with (
                mock.patch.object(host_probe.shutil, "which", return_value=None),
                mock.patch.object(host_probe, "TOOL_DIRS", (folder,)),
            ):
                self.assertEqual(host_probe.find_tool("ffprobe"), str(tool))
            self.assertEqual(host_probe.tool_version(str(tool)), "ffprobe version 7.1 Copyright")

    def test_broken_tool_is_unusable(self):
        with tempfile.TemporaryDirectory() as folder:
            tool = Path(folder) / "ffmpeg"
            tool.write_text("#!/bin/sh\nexit 3\n")
            tool.chmod(stat.S_IRWXU)
            self.assertEqual(host_probe.tool_version(str(tool)), "unusable (exit 3)")
            self.assertTrue(host_probe.tool_version(os.path.join(folder, "missing")).startswith("unusable ("))


if __name__ == "__main__":
    unittest.main()
