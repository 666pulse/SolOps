"""在临时目录执行真实 Ansible 身份任务，验证迁移不会替换已有私钥。"""

import getpass
import grp
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
ANSIBLE = shutil.which("ansible-playbook")


@unittest.skipUnless(ANSIBLE, "身份迁移测试需要 ansible-playbook")
class IdentityTests(unittest.TestCase):
    def test_identity_is_preserved_and_reruns_are_idempotent(self):
        for case in ("fresh", "legacy", "both", "legacy_with_other_files"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary)
                identity = home / "identity.json"
                legacy = home / "identity/validator-keypair.json"
                release = home / "agave/releases/4.3.0"
                (release / "bin").mkdir(parents=True)
                keygen = release / "bin/solana-keygen"
                keygen.write_text(
                    f"#!{sys.executable}\n"
                    "from pathlib import Path\n"
                    "import sys\n"
                    "Path(sys.argv[-1]).write_text('generated-fixture')\n"
                )
                keygen.chmod(0o755)
                if case != "fresh":
                    legacy.parent.mkdir()
                    legacy.write_text("legacy-fixture")
                    original_inode = legacy.stat().st_ino
                if case == "both":
                    identity.write_text("current-fixture")
                    original_inode = identity.stat().st_ino
                if case == "legacy_with_other_files":
                    (legacy.parent / ".keep").write_text("preserve this file")

                tasks = yaml.safe_load((ROOT / "tasks/identity.yml").read_text())
                for task in tasks:
                    task.pop("become_user", None)
                    if "ansible.builtin.file" in task:
                        task["ansible.builtin.file"]["group"] = grp.getgrgid(
                            os.getgid()
                        ).gr_name
                playbook = home / "identity.yml"
                playbook.write_text(
                    yaml.safe_dump(
                        [
                            {
                                "hosts": "all",
                                "gather_facts": False,
                                "vars": {
                                    "ansible_python_interpreter": sys.executable,
                                    "solana_home": str(home),
                                    "solana_identity": str(identity),
                                    "solana_user": getpass.getuser(),
                                    "agave_release": str(release),
                                },
                                "tasks": tasks,
                            }
                        ],
                        allow_unicode=True,
                    )
                )
                for run in range(2):
                    result = subprocess.run(
                        [ANSIBLE, "-i", "localhost,", "-c", "local", str(playbook)],
                        env={
                            **os.environ,
                            "ANSIBLE_CONFIG": str(ROOT / "ansible.cfg"),
                            "ANSIBLE_LOCAL_TEMP": str(home / "local"),
                            "ANSIBLE_REMOTE_TEMP": str(home / "remote"),
                            "ANSIBLE_NOCOLOR": "1",
                        },
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )
                    if run == 1:
                        self.assertIn("changed=0", result.stdout)
                expected = {
                    "fresh": "generated-fixture",
                    "both": "current-fixture",
                }.get(case, "legacy-fixture")
                self.assertEqual(identity.read_text(), expected)
                self.assertEqual(identity.stat().st_mode & 0o777, 0o600)
                if case != "fresh":
                    self.assertEqual(identity.stat().st_ino, original_inode)
                if case == "both":
                    self.assertEqual(legacy.read_text(), "legacy-fixture")
                elif case == "legacy_with_other_files":
                    self.assertFalse(legacy.exists())
                    self.assertTrue((legacy.parent / ".keep").exists())
                else:
                    self.assertFalse(legacy.parent.exists())


if __name__ == "__main__":
    unittest.main()
