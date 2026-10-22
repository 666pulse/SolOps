"""使用本地 JSON-RPC 验证同轮 slot 采集、失败清理及凭据保护。"""

import http.server
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "slot_compare", ROOT / "files/solana_slot_compare.py"
)
COLLECTOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COLLECTOR)


class RpcFixture(http.server.BaseHTTPRequestHandler):
    slots = {"/local": 100, "/ahead": 108, "/behind": 95}
    requests = []
    barrier = None

    def log_message(self, *_args):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(data)
        path = self.path.split("?")[0]
        if type(self).barrier is not None:
            type(self).barrier.wait(timeout=2)
        if path == "/slow":
            time.sleep(0.2)
        body = json.dumps(
            {"jsonrpc": "2.0", "id": data["id"], "result": type(self).slots.get(path)}
        ).encode()
        self.send_response(429 if path == "/limited" else 200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class SlotComparisonTests(unittest.TestCase):
    def setUp(self):
        RpcFixture.slots = {"/local": 100, "/ahead": 108, "/behind": 95}
        RpcFixture.requests = []
        RpcFixture.barrier = None
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RpcFixture)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def config(self, names):
        return {
            "commitment": "finalized",
            "timeout_seconds": 1,
            "endpoints": [
                {
                    "name": name,
                    "url": f"http://127.0.0.1:{self.server.server_port}/{name}?api-key=secret-fixture",
                }
                for name in names
            ],
        }

    def test_parallel_same_commitment_and_atomic_failure_replacement(self):
        config = self.config(["local", "ahead", "behind"])
        # 只有真正并发发起这三个请求才会放行，避免按顺序采样造成额外偏差。
        RpcFixture.barrier = threading.Barrier(3)
        content = COLLECTOR.collect(config)
        self.assertIn(
            'solana_rpc_slot{rpc="ahead",commitment="finalized"} 108\n', content
        )
        self.assertIn(
            'solana_rpc_slot{rpc="behind",commitment="finalized"} 95\n', content
        )
        self.assertTrue(
            all(
                request["method"] == "getSlot"
                and request["params"] == [{"commitment": "finalized"}]
                for request in RpcFixture.requests
            )
        )
        self.assertNotIn("secret-fixture", content)
        self.assertNotIn("http://", content)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "slot.prom"
            COLLECTOR.write_metrics(output, content)
            self.assertEqual(output.stat().st_mode & 0o777, 0o644)
            del RpcFixture.slots["/ahead"]
            del RpcFixture.slots["/local"]
            content = COLLECTOR.collect(config)
            COLLECTOR.write_metrics(output, content)
            self.assertNotIn('solana_rpc_slot{rpc="ahead"', output.read_text())
            self.assertNotIn('solana_rpc_slot{rpc="local"', output.read_text())
            self.assertIn(
                'solana_rpc_probe_success{rpc="ahead",commitment="finalized"} 0\n',
                content,
            )
            self.assertEqual(list(Path(temporary).iterdir()), [output])
        promtool = os.environ.get("PROMTOOL_BIN")
        if promtool:
            result = subprocess.run(
                [promtool, "check", "metrics"],
                input=content,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_timeout_rate_limit_and_invalid_slot_do_not_become_zero(self):
        config = self.config(["local", "slow", "limited", "bad"])
        config["timeout_seconds"] = 0.03
        RpcFixture.slots["/bad"] = True
        content = COLLECTOR.collect(config)
        for name in ["slow", "limited", "bad"]:
            self.assertIn(
                f'solana_rpc_probe_success{{rpc="{name}",commitment="finalized"}} 0\n',
                content,
            )
            self.assertNotIn(f'solana_rpc_slot{{rpc="{name}"', content)
        self.assertIn(
            'solana_rpc_slot{rpc="local",commitment="finalized"} 100\n', content
        )

    def test_invalid_configuration_cannot_leak_credentials(self):
        config = self.config(["local", "ahead"])
        config["endpoints"][1]["name"] = 'bad"\nname'
        with self.assertRaises(ValueError):
            COLLECTOR.collect(config)
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "config.json"
            source.write_text('{broken: "secret-fixture"')
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "files/solana_slot_compare.py"),
                    "--config",
                    str(source),
                    "--output",
                    str(Path(temporary) / "slot.prom"),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 1)
            self.assertNotIn("secret-fixture", result.stdout + result.stderr)
