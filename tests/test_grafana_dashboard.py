"""真实 Ansible + 本地 Grafana API fixture，验证独立初始化标记和用户修改保留。"""

import base64
import getpass
import grp
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
ANSIBLE = shutil.which("ansible-playbook")


class GrafanaFixture(http.server.BaseHTTPRequestHandler):
    dashboards = {}
    folder_exists = False
    writes = []

    def log_message(self, *_args):
        pass

    def respond(self, code, payload):
        content = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def authorized(self):
        expected = "Basic " + base64.b64encode(b"admin:fixture-password").decode()
        if self.headers.get("Authorization") != expected:
            self.respond(401, {})
            return False
        return True

    def do_GET(self):
        if not self.authorized():
            return
        if self.path.startswith("/api/dashboards/uid/"):
            uid = self.path.rsplit("/", 1)[1]
            self.respond(
                200 if uid in self.dashboards else 404,
                {"dashboard": self.dashboards.get(uid)},
            )
        elif self.path.startswith("/api/search?"):
            self.respond(
                200,
                [{"uid": "fixture-folder", "title": "Solana Node Service"}]
                if self.folder_exists
                else [],
            )
        else:
            self.respond(404, {})

    def do_POST(self):
        if not self.authorized():
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).writes.append((self.path, body))
        if self.path == "/apis/folder.grafana.app/v1/namespaces/default/folders":
            type(self).folder_exists = True
            self.respond(201, {"metadata": {"name": "fixture-folder"}})
        elif (
            self.path == "/apis/dashboard.grafana.app/v1/namespaces/default/dashboards"
        ):
            uid = body["metadata"]["name"]
            type(self).dashboards[uid] = body["spec"]
            self.respond(201, {"metadata": {"name": uid}})
        else:
            self.respond(404, {})


@unittest.skipUnless(ANSIBLE, "需要 ansible-playbook")
class GrafanaDashboardTests(unittest.TestCase):
    def test_independent_markers_preserve_edits_and_deletions(self):
        GrafanaFixture.dashboards = {}
        GrafanaFixture.folder_exists = False
        GrafanaFixture.writes = []
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), GrafanaFixture)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            password = directory / "password"
            password.write_text("fixture-password\n")

            def adapt(value):
                if isinstance(value, list):
                    return [adapt(item) for item in value]
                if isinstance(value, dict):
                    return {
                        key: (
                            getpass.getuser()
                            if key == "owner"
                            else grp.getgrgid(os.getgid()).gr_name
                        )
                        if key in {"owner", "group"} and item == "root"
                        else adapt(item)
                        for key, item in value.items()
                    }
                if isinstance(value, str):
                    return value.replace("/var/lib/grafana", str(directory)).replace(
                        "/etc/grafana/admin_password", str(password)
                    )
                return value

            task_file = directory / "dashboard.yml"
            task_file.write_text(
                yaml.safe_dump(
                    adapt(
                        yaml.safe_load(
                            (ROOT / "tasks/grafana_dashboard.yml").read_text()
                        )
                    ),
                    allow_unicode=True,
                )
            )
            playbook = directory / "playbook.yml"
            playbook.write_text(
                yaml.safe_dump(
                    [
                        {
                            "hosts": "all",
                            "gather_facts": False,
                            "vars": {
                                "grafana_port": server.server_port,
                                "ansible_python_interpreter": sys.executable,
                            },
                            "tasks": [
                                {
                                    "ansible.builtin.import_tasks": str(task_file),
                                    "vars": {
                                        "grafana_dashboard_file": str(
                                            ROOT / f"files/{name}-dashboard.json"
                                        ),
                                        "grafana_dashboard_marker": f".{name}-dashboard-imported",
                                    },
                                }
                                for name in ["node", "solana"]
                            ],
                        }
                    ],
                    allow_unicode=True,
                )
            )
            node = json.loads((ROOT / "files/node-dashboard.json").read_text())
            node["title"] = "用户已有主机仪表盘"
            GrafanaFixture.dashboards[node["uid"]] = node
            (directory / ".node-dashboard-imported").write_text(node["uid"])

            def run():
                result = subprocess.run(
                    [ANSIBLE, "-i", "localhost,", "-c", "local", str(playbook)],
                    env={
                        **os.environ,
                        "ANSIBLE_LOCAL_TEMP": str(directory / "local"),
                        "ANSIBLE_REMOTE_TEMP": str(directory / "remote"),
                        "ANSIBLE_NOCOLOR": "1",
                    },
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            run()
            self.assertEqual(
                GrafanaFixture.dashboards[node["uid"]]["title"], "用户已有主机仪表盘"
            )
            expected = json.loads((ROOT / "files/solana-dashboard.json").read_text())
            self.assertEqual(GrafanaFixture.dashboards[expected["uid"]], expected)
            self.assertTrue((directory / ".solana-dashboard-imported").exists())
            writes = len(GrafanaFixture.writes)
            GrafanaFixture.dashboards[expected["uid"]]["title"] = (
                "用户修改后的 slot 仪表盘"
            )
            run()
            self.assertEqual(
                GrafanaFixture.dashboards[expected["uid"]]["title"],
                "用户修改后的 slot 仪表盘",
            )
            del GrafanaFixture.dashboards[expected["uid"]]
            run()
            self.assertNotIn(expected["uid"], GrafanaFixture.dashboards)
            self.assertEqual(len(GrafanaFixture.writes), writes)
