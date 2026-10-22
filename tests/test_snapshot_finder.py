"""用 inventory 固定的上游源码验证真实参数、发现和 HTTP 下载流程。"""

import http.server
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = os.environ.get("SNAPSHOT_FINDER_SOURCE")
FULL = "snapshot-100-abc.tar.zst"
INCREMENTAL = "incremental-snapshot-100-120-abc.tar.zst"
PAYLOAD = b"snapshot-fixture\n" * 65536


class SnapshotServer(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if request["method"] == "getSlot":
            result = 125
        elif request["method"] == "getClusterNodes":
            result = [
                {"rpc": f"127.0.0.1:{self.server.server_port}", "version": "4.3.0"}
            ]
        else:
            self.send_error(400)
            return
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.respond(head=True)

    def do_GET(self):
        self.respond()

    def respond(self, head=False):
        aliases = {
            "/snapshot.tar.bz2": FULL,
            "/incremental-snapshot.tar.bz2": INCREMENTAL,
        }
        if self.path in aliases:
            self.send_response(302)
            self.send_header("Location", "/" + aliases[self.path])
            self.send_header("Content-Length", 0)
            self.end_headers()
        elif self.path in {"/" + FULL, "/" + INCREMENTAL}:
            self.send_response(200)
            self.send_header("Content-Length", len(PAYLOAD))
            self.end_headers()
            if not head:
                self.wfile.write(PAYLOAD)
        else:
            self.send_error(404)


@unittest.skipUnless(SOURCE, "设置 SNAPSHOT_FINDER_SOURCE 后运行上游集成测试")
class SnapshotFinderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import yaml
        from jinja2 import Environment, StrictUndefined

        cls.environment = Environment(undefined=StrictUndefined)
        cls.environment.filters["bool"] = bool
        cls.inventory = yaml.safe_load(
            (ROOT / "inventory.mainnet.rpcs.yml").read_text()
        )["mainnet_rpcs"]["vars"]
        cls.tasks = yaml.safe_load((ROOT / "tasks/snapshot.yml").read_text())
        cls.block = next(task for task in cls.tasks if "block" in task)
        cls.download = next(
            task for task in cls.block["block"] if "ansible.builtin.command" in task
        )
        cls.source = Path(SOURCE).resolve()
        version = subprocess.check_output(
            ["git", "-C", str(cls.source), "rev-parse", "HEAD"], text=True
        ).strip()
        if version != cls.inventory["snapshot_finder_version"]:
            raise RuntimeError("测试的 Finder 源码提交必须与 inventory 一致")
        spec = importlib.util.spec_from_file_location(
            "upstream_finder", cls.source / "snapshot-finder.py"
        )
        cls.finder = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.finder
        spec.loader.exec_module(cls.finder)

    def render_arguments(self, **overrides):
        variables = {
            **self.inventory,
            "snapshot_finder_dir": "/home/solana/tools/snapshot-finder",
            "snapshot_path": "/mnt/ledger/snapshots",
            **overrides,
        }
        # 前四个参数仅负责重定向输出，Finder 的 CLI 参数保持不变。
        return [
            self.environment.from_string(value).render(variables)
            for value in self.download["ansible.builtin.command"]["argv"]
        ][4:]

    def test_inventory_arguments_match_pinned_upstream_parser(self):
        argv = self.render_arguments()
        args = self.finder.build_parser().parse_args(argv[2:])
        config = self.finder.build_config(args)
        self.assertEqual(
            config.min_download_speed_mb,
            self.inventory["snapshot_min_download_speed_mb"],
        )
        self.assertEqual(
            config.maximum_local_snapshot_age, self.inventory["snapshot_max_local_age"]
        )
        self.assertEqual(config.threads_count, self.inventory["snapshot_threads"])
        self.assertEqual(config.rpc_address, self.inventory["snapshot_rpc_url"])
        self.assertEqual(
            config.full_snapshot_archive_path, config.incremental_snapshot_archive_path
        )
        self.assertEqual(argv[0], "/home/solana/tools/snapshot-finder/venv/bin/python")

    def test_download_guards_skip_existing_or_running_nodes(self):
        for enabled, ledger, running, expected in [
            (True, False, False, True),
            (False, False, False, False),
            (True, True, False, False),
            (True, False, True, False),
        ]:
            with self.subTest(enabled=enabled, ledger=ledger, running=running):
                context = {
                    "snapshot_download": enabled,
                    "existing_ledger": {"stat": {"exists": ledger}},
                    "ansible_facts": {
                        "services": {
                            "solana-rpc.service": {
                                "state": "running" if running else "stopped"
                            }
                        }
                    },
                }
                actual = all(
                    self.environment.compile_expression(condition)(context)
                    for condition in self.block["when"]
                )
                self.assertEqual(actual, expected)

    def test_real_cli_discovers_and_downloads_pair_over_http(self):
        with http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), SnapshotServer
        ) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with tempfile.TemporaryDirectory() as temporary:
                    argv = self.render_arguments(
                        snapshot_path=temporary,
                        snapshot_rpc_url=f"http://127.0.0.1:{server.server_port}",
                        snapshot_min_download_speed_mb=1,
                        snapshot_search_timeout=5,
                        snapshot_threads=2,
                    )
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(self.source / "snapshot-finder.py"),
                            *argv[2:],
                        ],
                        capture_output=True,
                        text=True,
                        timeout=30,
                        env={
                            **os.environ,
                            "NO_PROXY": "127.0.0.1,localhost",
                            "no_proxy": "127.0.0.1,localhost",
                        },
                    )
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )
                    self.assertEqual((Path(temporary) / FULL).read_bytes(), PAYLOAD)
                    self.assertEqual(
                        (Path(temporary) / INCREMENTAL).read_bytes(), PAYLOAD
                    )
                    self.assertFalse(list(Path(temporary).glob("*.part")))
            finally:
                server.shutdown()
                worker.join()

    def test_failed_download_does_not_publish_archive(self):
        from unittest.mock import patch
        import requests

        with tempfile.TemporaryDirectory() as temporary:
            argv = self.render_arguments(snapshot_path=temporary)
            config = self.finder.build_config(
                self.finder.build_parser().parse_args(argv[2:])
            )
            finder = self.finder.SnapshotFinder(config)
            partial = Path(temporary) / (FULL + ".part")
            partial.write_bytes(b"unfinished")
            with patch.object(
                self.finder.requests,
                "get",
                side_effect=requests.ConnectionError("test disconnect"),
            ):
                with self.assertRaises(self.finder.DownloadError):
                    finder.download("http://127.0.0.1/" + FULL, Path(temporary))
            self.assertFalse((Path(temporary) / FULL).exists())
            self.assertFalse(partial.exists())


if __name__ == "__main__":
    unittest.main()
