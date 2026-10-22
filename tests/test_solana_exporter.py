"""通过实际服务参数验证固定版本 exporter，不访问主网。"""

import http.server
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
EXPORTER = os.environ.get("SOLANA_EXPORTER_BIN")


class RpcFixture(http.server.BaseHTTPRequestHandler):
    mode = "unavailable"
    methods = set()
    epoch = {
        "absoluteSlot": 432100,
        "blockHeight": 400000,
        "epoch": 1,
        "slotIndex": 100,
        "slotsInEpoch": 432000,
        "transactionCount": 1000000,
    }

    def log_message(self, *_args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        method = request["method"]
        type(self).methods.add(method)
        response = {"jsonrpc": "2.0", "id": request["id"]}
        results = {
            "getHealth": "ok",
            "getVersion": {"solana-core": "4.3.0", "feature-set": 1},
            "getIdentity": {"identity": "11111111111111111111111111111111"},
            "getEpochInfo": self.epoch,
        }
        if self.mode == "behind" and method == "getHealth":
            response["error"] = {
                "code": -32005,
                "message": "Node is behind",
                "data": {"numSlotsBehind": 123},
            }
        elif self.mode == "unknown" and method == "getHealth":
            response["error"] = {"code": -32005, "message": "Node is unhealthy"}
        elif method in results:
            response["result"] = results[method]
        else:
            response["error"] = {"code": -32601, "message": "Unexpected RPC method"}
        body = (
            b"Service unavailable"
            if self.mode == "unavailable"
            else json.dumps(response).encode()
        )
        self.send_response(503 if self.mode == "unavailable" else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@unittest.skipUnless(EXPORTER, "设置 SOLANA_EXPORTER_BIN 后运行真实 exporter 集成测试")
class SolanaExporterTests(unittest.TestCase):
    def test_light_mode_health_and_recovery(self):
        import yaml
        from jinja2 import Environment, StrictUndefined

        variables = yaml.safe_load((ROOT / "vars/solana_exporter.yml").read_text())[
            "solana_exporter_defaults"
        ]
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        RpcFixture.mode = "unavailable"
        RpcFixture.methods = set()
        rpc = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RpcFixture)
        thread = threading.Thread(target=rpc.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(rpc.server_close)
        self.addCleanup(rpc.shutdown)
        variables.update(
            solana_exporter_release="/unused",
            rpc_port=rpc.server_port,
            solana_exporter_port=port,
            solana_exporter_slot_pace=1,
        )
        unit = (
            Environment(undefined=StrictUndefined)
            .from_string((ROOT / "templates/solana-exporter.service.j2").read_text())
            .render(variables)
        )
        command = next(
            line.removeprefix("ExecStart=")
            for line in unit.splitlines()
            if line.startswith("ExecStart=")
        )
        args = [EXPORTER, *shlex.split(command)[1:]]
        with tempfile.TemporaryFile(mode="w+") as log:
            process = subprocess.Popen(args, stdout=log, stderr=log)
            try:

                def scrape():
                    try:
                        with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/metrics", timeout=3
                        ) as response:
                            return response.status, response.read().decode()
                    except urllib.error.HTTPError as error:
                        return error.code, error.read().decode()

                def wait_metrics(status, *metrics):
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        self.assertIsNone(process.poll(), "exporter 提前退出")
                        try:
                            code, body = scrape()
                            if code == status and all(
                                metric in body for metric in metrics
                            ):
                                return body
                        except OSError:
                            pass
                        time.sleep(0.1)
                    log.seek(0)
                    self.fail(f"指标未就绪：{log.read()[-6000:]}")

                # RPC 尚未启动时，exporter 仍监听，采集明确失败。
                wait_metrics(500)
                RpcFixture.mode = "healthy"
                wait_metrics(
                    200,
                    "solana_node_is_healthy 1\n",
                    "solana_node_num_slots_behind 0\n",
                    "solana_node_slot_height 432100\n",
                    "solana_node_block_height 400000\n",
                    'solana_node_version{version="4.3.0"} 1\n',
                )
                RpcFixture.mode = "behind"
                wait_metrics(
                    200,
                    "solana_node_is_healthy 0\n",
                    "solana_node_num_slots_behind 123\n",
                )
                RpcFixture.mode = "unknown"
                wait_metrics(500)
                RpcFixture.mode = "unavailable"
                wait_metrics(500)
                RpcFixture.mode = "healthy"
                wait_metrics(200, "solana_node_is_healthy 1\n")
                self.assertEqual(
                    RpcFixture.methods,
                    {"getHealth", "getVersion", "getIdentity", "getEpochInfo"},
                )
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
