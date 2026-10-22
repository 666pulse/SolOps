import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.uname().sysname == "Linux", "磁盘脚本测试需要 Linux")
class DiskTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fstab = self.root / "fstab"
        self.fstab.write_text("# existing configuration\n")
        self.state_file = self.root / "state.json"
        self.state = {"formats": 0, "disks": {}}
        for name in ["ledger", "accounts"]:
            device = self.root / name
            device.touch()
            self.state["disks"][name] = {
                "device": str(device),
                "expected_mount": str(self.root / f"mnt-{name}"),
            }
        self.commands = self.root / "bin"
        self.commands.mkdir()
        shim = self.commands / "disk_command.py"
        shim.write_text((ROOT / "tests/disk_command.py").read_text())
        shim.chmod(0o755)
        for command in [
            "ls",
            "lsblk",
            "blkid",
            "wipefs",
            "findmnt",
            "mount",
            "mkfs.ext4",
        ]:
            (self.commands / command).symlink_to(shim)
        # 仅替换系统路径/权限入口，测试生产脚本的检查、排序与重复执行逻辑。
        script = (ROOT / "files/setup_disks.sh").read_text()
        script = script.replace("/etc/fstab", str(self.fstab))
        script = script.replace("/run/lock/solana-disks.lock", str(self.root / "lock"))
        script = script.replace("[[ $EUID -eq 0 ]]", "true")
        for name in ["ledger", "accounts"]:
            script = script.replace(f"/mnt/{name}", str(self.root / f"mnt-{name}"))
        self.script = self.root / "setup_disks.sh"
        self.script.write_text(script)

    def run_script(self, allow=False):
        self.state_file.write_text(json.dumps(self.state))
        result = subprocess.run(
            [
                "bash",
                str(self.script),
                "--ledger_disk",
                str(self.root / "ledger"),
                "--accounts_disk",
                str(self.root / "accounts"),
                "--owner",
                "root",
                *(["--allow-format"] if allow else []),
            ],
            env={
                **os.environ,
                "DISK_TEST_STATE": str(self.state_file),
                "PATH": f"{self.commands}:{os.environ['PATH']}",
            },
            capture_output=True,
            text=True,
        )
        self.state = json.loads(self.state_file.read_text())
        return result

    def test_blank_disks_require_explicit_flag(self):
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state["formats"], 0)

    def test_checks_both_disks_before_formatting(self):
        for failure in [
            {"partitions": True},
            {"fs": "xfs"},
            {"signatures": "linux_raid_member"},
            {"mount": "/"},
            {"holder": "dm-0"},
        ]:
            with self.subTest(failure=failure):
                self.state["disks"]["accounts"] = {
                    "device": str(self.root / "accounts"),
                    "expected_mount": str(self.root / "mnt-accounts"),
                    **failure,
                }
                result = self.run_script(allow=True)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(self.state["formats"], 0)

    def test_format_mount_and_second_run_are_idempotent(self):
        first = self.run_script(allow=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.state["formats"], 2)
        fstab = self.fstab.read_text()
        second = self.run_script()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.state["formats"], 2)
        self.assertNotIn("CHANGED:", second.stdout)
        self.assertEqual(self.fstab.read_text(), fstab)

    def test_refuses_nonempty_mount_directory(self):
        mountpoint = self.root / "mnt-accounts"
        mountpoint.mkdir()
        (mountpoint / "important-data").touch()
        result = self.run_script(allow=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state["formats"], 0)

    def test_refuses_conflicting_fstab(self):
        self.fstab.write_text(
            f"UUID=other {self.root}/mnt-accounts ext4 defaults 0 2\n"
        )
        result = self.run_script(allow=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state["formats"], 0)


if __name__ == "__main__":
    unittest.main()
