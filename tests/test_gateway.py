"""使用标准 Caddy 验证 API key、HTTP/WS、白名单和健康检查。"""

import base64
import hashlib
import http.client
import http.server
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
CADDY = os.environ.get("CADDY_BIN") or shutil.which("caddy")
API_KEY = "gateway-test-key"


class RpcFixture(http.server.BaseHTTPRequestHandler):
    healthy = True
    paths = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        type(self).paths.append(self.path)
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        result = {"jsonrpc": "2.0", "id": data["id"]}
        if data["method"] == "getHealth" and not type(self).healthy:
            result["error"] = {"code": -32005, "message": "Node is behind"}
        else:
            result["result"] = "ok"
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)


class WebsocketFixture(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    paths = []

    def log_message(self, *_args):
        pass

    def do_GET(self):
        type(self).paths.append(self.path)
        key = self.headers["Sec-WebSocket-Key"]
        accept = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
            ).digest()
        ).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.write(b"\x81\x02ok")
        self.wfile.flush()
        self.close_connection = True


@unittest.skipUnless(CADDY, "设置 CADDY_BIN 后运行真实网关集成测试")
class GatewayTests(unittest.TestCase):
    def test_gateway(self):
        import yaml
        from jinja2 import Environment, StrictUndefined

        variables = yaml.safe_load((ROOT / "inventory.mainnet.rpcs.yml").read_text())[
            "mainnet_rpcs"
        ]["vars"]
        # 固定高位端口仅在独立测试环境内使用，不访问主网、不申请公网证书。
        variables.update(
            rpc_port=28899,
            caddy_admin_port=22019,
            gateway_domain="https://localhost:28443",
            grafana_domain="https://grafana.localhost:28443",
            grafana_port=23000,
            gateway_rpc_port=28889,
            gateway_ws_port=28890,
            gateway_api_key=API_KEY,
            gateway_allowed_cidrs=[],
            gateway_health_interval="100ms",
            gateway_health_timeout="100ms",
        )
        template = Environment(undefined=StrictUndefined).from_string(
            (ROOT / "templates/caddyfile.j2").read_text()
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            config = directory / "caddyfile"

            def render():
                config.write_text(
                    template.render(variables).replace(
                        "{\n", "{\n    local_certs\n    skip_install_trust\n", 1
                    )
                )

            render()
            servers = [
                http.server.ThreadingHTTPServer(("127.0.0.1", 28899), RpcFixture),
                http.server.ThreadingHTTPServer(("127.0.0.1", 28900), WebsocketFixture),
            ]
            for server in servers:
                threading.Thread(target=server.serve_forever, daemon=True).start()
            env = {
                **os.environ,
                "XDG_DATA_HOME": temporary,
                "XDG_CONFIG_HOME": temporary,
                "HOME": temporary,
            }
            with (directory / "caddy.log").open("w+") as log:
                process = subprocess.Popen(
                    [CADDY, "run", "--config", str(config), "--adapter", "caddyfile"],
                    stdout=log,
                    stderr=log,
                    env=env,
                )
                try:
                    context = ssl._create_unverified_context()  # 临时测试 CA

                    def request(
                        headers=None, port=28443, tls=True, path=f"/?api-key={API_KEY}"
                    ):
                        if tls:
                            connection = http.client.HTTPSConnection(
                                "localhost", port, context=context, timeout=2
                            )
                        else:
                            connection = http.client.HTTPConnection(
                                "localhost", port, timeout=2
                            )
                        try:
                            connection.request(
                                "POST",
                                path,
                                '{"jsonrpc":"2.0","id":1,"method":"getHealth"}',
                                {"Content-Type": "application/json", **(headers or {})},
                            )
                            response = connection.getresponse()
                            return response.status, response.read()
                        finally:
                            connection.close()

                    def wait_status(expected, port=28443, tls=True):
                        for _ in range(100):
                            try:
                                if request(port=port, tls=tls)[0] == expected:
                                    return
                            except (OSError, http.client.HTTPException):
                                pass
                            time.sleep(0.1)
                        log.seek(0)
                        self.fail(f"未得到 HTTP {expected}: {log.read()}")

                    wait_status(200)
                    self.assertEqual(json.loads(request()[1])["result"], "ok")
                    wait_status(200, port=28889, tls=False)
                    self.assertEqual(
                        json.loads(request(port=28889, tls=False)[1])["result"], "ok"
                    )
                    for port, tls in [(28443, True), (28889, False), (28890, False)]:
                        for path in [
                            "/",
                            "/?api-key=",
                            "/?api-key=wrong",
                            f"/?api-key={API_KEY}-extra",
                            f"/?api_key={API_KEY}",
                        ]:
                            self.assertEqual(
                                request(port=port, tls=tls, path=path)[0], 401
                            )
                            self.assertEqual(
                                request(
                                    {"Upgrade": "websocket", "Connection": "Upgrade"},
                                    port=port,
                                    tls=tls,
                                    path=path,
                                )[0],
                                401,
                            )
                    self.assertEqual(
                        request(path=f"/?api-key={API_KEY}&test=keep")[0], 200
                    )
                    self.assertIn("/?test=keep", RpcFixture.paths)
                    for port, tls in [(28443, True), (28889, False), (28890, False)]:
                        with socket.create_connection(
                            ("127.0.0.1", port), timeout=3
                        ) as raw:
                            with (
                                context.wrap_socket(raw, server_hostname="localhost")
                                if tls
                                else raw
                            ) as stream:
                                stream.sendall(
                                    f"GET /?api-key={API_KEY}&test=ws HTTP/1.1\r\nHost: localhost:{port}\r\n".encode()
                                    + b"Connection: Upgrade\r\nUpgrade: websocket\r\n"
                                    b"Sec-WebSocket-Version: 13\r\n"
                                    b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n"
                                )
                                received = b""
                                while b"\x81\x02ok" not in received:
                                    part = stream.recv(4096)
                                    if not part:
                                        break
                                    received += part
                                self.assertIn(b"101 Switching Protocols", received)
                                self.assertIn(b"\x81\x02ok", received)
                    self.assertIn("/?test=ws", WebsocketFixture.paths)
                    self.assertFalse(
                        any(
                            "api-key" in path
                            for path in RpcFixture.paths + WebsocketFixture.paths
                        )
                    )

                    RpcFixture.healthy = False
                    wait_status(503)
                    wait_status(503, port=28889, tls=False)
                    expected = 'caddy_reverse_proxy_upstreams_healthy{upstream="127.0.0.1:28899"} 0'
                    # Caddy 的健康 gauge 独立每 10 秒更新。
                    for _ in range(120):
                        connection = http.client.HTTPConnection("127.0.0.1", 22019)
                        connection.request("GET", "/metrics")
                        metrics = connection.getresponse().read().decode()
                        connection.close()
                        if expected in metrics:
                            break
                        time.sleep(0.1)
                    self.assertIn(
                        expected,
                        "\n".join(
                            line
                            for line in metrics.splitlines()
                            if line.startswith("caddy_reverse_proxy")
                        ),
                    )
                    RpcFixture.healthy = True
                    wait_status(200)
                    wait_status(200, port=28889, tls=False)

                    variables["gateway_allowed_cidrs"] = ["192.0.2.0/24"]
                    render()
                    subprocess.run(
                        [
                            CADDY,
                            "reload",
                            "--config",
                            str(config),
                            "--adapter",
                            "caddyfile",
                            "--address",
                            "127.0.0.1:22019",
                        ],
                        check=True,
                        stdout=log,
                        stderr=log,
                        env=env,
                    )
                    for port, tls in [(28443, True), (28889, False), (28890, False)]:
                        self.assertEqual(
                            request(
                                {"X-Forwarded-For": "192.0.2.10"}, port=port, tls=tls
                            )[0],
                            403,
                        )
                        self.assertEqual(
                            request(
                                {"Upgrade": "websocket", "Connection": "Upgrade"},
                                port=port,
                                tls=tls,
                            )[0],
                            403,
                        )
                    # IP 入口不依赖域名；同时验证更换密钥后旧密钥立即失效。
                    variables.update(
                        gateway_domain="",
                        grafana_domain="",
                        gateway_allowed_cidrs=[],
                        gateway_api_key="rotated-test-key",
                    )
                    render()
                    subprocess.run(
                        [
                            CADDY,
                            "reload",
                            "--config",
                            str(config),
                            "--adapter",
                            "caddyfile",
                            "--address",
                            "127.0.0.1:22019",
                        ],
                        check=True,
                        stdout=log,
                        stderr=log,
                        env=env,
                    )
                    self.assertEqual(request(port=28889, tls=False)[0], 401)
                    self.assertEqual(
                        request(
                            port=28889, tls=False, path="/?api-key=rotated-test-key"
                        )[0],
                        200,
                    )
                    log.flush()
                    log.seek(0)
                    logs = log.read()
                    self.assertNotIn(API_KEY, logs)
                    self.assertNotIn("rotated-test-key", logs)
                finally:
                    RpcFixture.healthy = True
                    process.terminate()
                    process.wait(timeout=10)
                    for server in servers:
                        server.shutdown()
                        server.server_close()


if __name__ == "__main__":
    unittest.main()
