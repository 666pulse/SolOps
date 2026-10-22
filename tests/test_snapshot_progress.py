"""通过真实 Ansible 本地任务验证实时回显、失败和异步超时，不连接生产服务器。"""

import copy
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
ANSIBLE = shutil.which("ansible-playbook")


@unittest.skipUnless(ANSIBLE, "实时进度集成测试需要 ansible-playbook")
class SnapshotProgressTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="snapshot-progress-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.snapshots = self.root / "snapshots with spaces"
        self.snapshots.mkdir()
        self.finder = self.root / "tools/snapshot-finder"
        (self.finder / "venv/bin").mkdir(parents=True)
        (self.finder / "venv/bin/python").symlink_to(sys.executable)
        self.env = {
            **os.environ,
            "ANSIBLE_CONFIG": str(ROOT / "ansible.cfg"),
            "ANSIBLE_LOCAL_TEMP": str(self.root / "local"),
            "ANSIBLE_REMOTE_TEMP": str(self.root / "remote"),
            "ANSIBLE_ASYNC_DIR": str(self.root / "async"),
            "ANSIBLE_ACTION_PLUGINS": str(ROOT / "action_plugins"),
            "ANSIBLE_LIBRARY": str(ROOT / "library"),
            "ANSIBLE_STDOUT_CALLBACK": "default",
            "ANSIBLE_NOCOLOR": "1",
        }

    def prepare(self, script, *, timeout=45, no_log=False):
        (self.finder / "snapshot-finder.py").write_text(textwrap.dedent(script))
        tasks = yaml.safe_load((ROOT / "tasks/snapshot.yml").read_text())
        block = next(task["block"] for task in tasks if "block" in task)
        download, progress = copy.deepcopy(block[-2:])
        for task in (download, progress):
            task.pop("become_user")
            task["no_log"] = no_log
        progress["snapshot_progress"]["interval"] = 1
        variables = {
            "ansible_python_interpreter": sys.executable,
            "snapshot_finder_dir": str(self.finder),
            "snapshot_path": str(self.snapshots),
            "snapshot_timeout": timeout,
            "snapshot_rpc_url": "http://127.0.0.1:1",
            "snapshot_min_download_speed_mb": 1,
            "snapshot_max_local_age": 2500,
            "snapshot_threads": 1,
            "snapshot_search_timeout": 5,
        }
        playbook = self.root / "progress.yml"
        playbook.write_text(
            yaml.safe_dump(
                [
                    {
                        "hosts": "all",
                        "gather_facts": False,
                        "vars": variables,
                        "tasks": [download, progress],
                    }
                ],
                allow_unicode=True,
            )
        )
        return [ANSIBLE, "-i", "localhost,", "-c", "local", str(playbook)]

    def run_playbook(self, command):
        return subprocess.run(
            command, env=self.env, capture_output=True, text=True, timeout=60
        )

    def assert_job_cleaned(self):
        self.assertFalse(list((self.root / "async").glob("j*")))

    def test_progress_is_visible_before_download_finishes(self):
        command = self.prepare(
            """
            from pathlib import Path
            import sys
            import time

            root = Path(sys.argv[sys.argv.index('--snapshots') + 1])
            print('选源完成', flush=True)
            print('\x1b[2J\\rfull-snapshot: 25%|##| 1G/4G [00:01<00:03, 1GB/s]',
                  file=sys.stderr, end='', flush=True)
            deadline = time.monotonic() + 30
            while not (root / 'progress-seen').exists():
                if time.monotonic() >= deadline:
                    sys.exit('控制机未在下载结束前看到进度')
                time.sleep(0.1)
            print('\\rfull-snapshot: 100%|########| 4G/4G', file=sys.stderr, flush=True)
            (root / 'completed').touch()
            """
        )
        output = []
        seen = threading.Event()
        with subprocess.Popen(
            command,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        ) as process:

            def read_output():
                for line in process.stdout:
                    output.append(line)
                    if "snapshot [localhost]" in line and "25%" in line:
                        # 下载器等待该确认；因此只能在进度实时可见时成功。
                        seen.set()
                        (self.snapshots / "progress-seen").touch()

            reader = threading.Thread(target=read_output)
            reader.start()
            try:
                code = process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                self.fail("Ansible 未按预期完成实时进度测试")
            finally:
                reader.join(timeout=5)
        combined = "".join(output)
        self.assertEqual(code, 0, combined)
        self.assertTrue(seen.is_set(), combined)
        self.assertTrue((self.snapshots / "completed").exists())
        self.assertIn("100%", combined)
        self.assertNotIn("\x1b[2J", combined)
        self.assert_job_cleaned()

    def test_failure_is_returned_with_download_log(self):
        command = self.prepare(
            """
            import sys
            print('snapshot 下载连接中断', file=sys.stderr, flush=True)
            sys.exit(7)
            """
        )
        result = self.run_playbook(command)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("快照下载失败", result.stdout)
        self.assertIn("snapshot 下载连接中断", result.stdout)
        self.assertIn('"rc": 7', result.stdout)
        self.assert_job_cleaned()

    def test_async_timeout_still_fails(self):
        command = self.prepare(
            """
            import time
            print('等待网络响应', flush=True)
            time.sleep(30)
            """,
            timeout=1,
        )
        result = self.run_playbook(command)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Timeout exceeded", result.stdout)
        self.assert_job_cleaned()

    def test_unknown_total_size_is_displayed(self):
        command = self.prepare(
            """
            import sys
            print('snapshot: 1.00GiB [00:01, 90.0MiB/s]', file=sys.stderr, flush=True)
            """
        )
        result = self.run_playbook(command)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("snapshot [localhost] snapshot: 1.00GiB", result.stderr)
        self.assert_job_cleaned()

    def test_node_scan_progress_is_not_displayed(self):
        command = self.prepare(
            """
            import sys
            print('正在查找快照下载源', flush=True)
            print('50%|█████     | 139/276 [00:00<00:00, 1380.66it/s]',
                  file=sys.stderr, flush=True)
            """
        )
        result = self.run_playbook(command)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("正在查找快照下载源", result.stderr)
        self.assertNotIn("139/276", result.stdout + result.stderr)
        self.assertNotIn("50%", result.stdout + result.stderr)
        self.assert_job_cleaned()

    def test_utf8_progress_survives_tail_cut_inside_character(self):
        command = self.prepare(
            """
            import sys
            # 8192 字节窗口从三字节字符中间开始，模拟 tqdm 的长进度日志。
            sys.stdout.buffer.write(('█' * 4000 + '\\r下载完成!\\n').encode('utf-8'))
            """
        )
        result = self.run_playbook(command)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("snapshot [localhost] 下载完成", result.stderr)
        self.assert_job_cleaned()

    def test_incomplete_utf8_does_not_hide_download_failure(self):
        command = self.prepare(
            """
            import sys
            sys.stdout.buffer.write('下载连接中断\\n'.encode('utf-8') + b'\\xe2\\x96')
            sys.exit(7)
            """
        )
        result = self.run_playbook(command)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("快照下载失败", result.stdout)
        self.assertIn("下载连接中断", result.stdout)
        self.assertIn('"rc": 7', result.stdout)
        self.assert_job_cleaned()

    def test_no_log_suppresses_live_output(self):
        command = self.prepare(
            "print('private-progress-text', flush=True)", no_log=True
        )
        result = self.run_playbook(command)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("private-progress-text", result.stdout + result.stderr)

    def test_check_mode_does_not_start_download(self):
        command = self.prepare("raise RuntimeError('不应执行下载')")
        result = self.run_playbook([*command, "--check"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.snapshots / "snapshot-progress.log").exists())
        self.assert_job_cleaned()


if __name__ == "__main__":
    unittest.main()
