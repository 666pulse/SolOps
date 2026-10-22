#!/usr/bin/env python3
"""并发查询同一 commitment 的 RPC slot，原子更新 Node Exporter textfile。"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.parse
import urllib.request


def validate_config(config):
    if config.get("commitment") not in {"processed", "confirmed", "finalized"}:
        raise ValueError("invalid commitment")
    timeout = config.get("timeout_seconds")
    if (
        type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or not 0 < timeout <= 10
    ):
        raise ValueError("invalid timeout")
    endpoints = config.get("endpoints", [])
    if not 2 <= len(endpoints) <= 8:
        raise ValueError("expected local RPC and 1-7 references")
    names = set()
    for endpoint in endpoints:
        name = endpoint.get("name", "")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name) or name in names:
            raise ValueError("invalid or duplicate RPC name")
        names.add(name)
        url = urllib.parse.urlsplit(endpoint.get("url", ""))
        if url.scheme not in {"http", "https"} or not url.hostname or url.fragment:
            raise ValueError("invalid RPC URL")
        if not isinstance(endpoint.get("headers", {}), dict):
            raise ValueError("invalid RPC headers")
    if "local" not in names:
        raise ValueError("local RPC is required")


def query_slot(endpoint, commitment, timeout):
    started = time.monotonic()
    slot = None
    try:
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSlot",
                "params": [{"commitment": commitment}],
            }
        ).encode()
        request = urllib.request.Request(
            endpoint["url"],
            data=body,
            headers={"Content-Type": "application/json", **endpoint.get("headers", {})},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = response.read(65537)
            if response.status != 200 or len(content) > 65536:
                raise ValueError("invalid RPC response")
        result = json.loads(content)
        if result.get("jsonrpc") != "2.0" or result.get("id") != 1 or "error" in result:
            raise ValueError("RPC error")
        value = result.get("result")
        if type(value) is not int or value < 0:
            raise ValueError("invalid slot")
        slot = value
    except Exception:
        # URL/异常可能包含 API key，不写入日志或指标。失败时删除本轮 slot。
        pass
    return endpoint["name"], slot, time.monotonic() - started


def collect(config):
    validate_config(config)
    # 使用轮次开始时间；查询耗时也算入数据年龄，长请求不会让旧结果显得新鲜。
    timestamp = time.time()
    with ThreadPoolExecutor(max_workers=len(config["endpoints"])) as pool:
        futures = [
            pool.submit(
                query_slot, endpoint, config["commitment"], config["timeout_seconds"]
            )
            for endpoint in config["endpoints"]
        ]
        results = [future.result() for future in futures]
    metrics = [
        "# HELP solana_rpc_slot Current slot from getSlot; absent when the current probe failed.",
        "# TYPE solana_rpc_slot gauge",
        "# HELP solana_rpc_probe_success Whether the current getSlot probe succeeded.",
        "# TYPE solana_rpc_probe_success gauge",
        "# HELP solana_rpc_probe_duration_seconds Duration of the current getSlot probe.",
        "# TYPE solana_rpc_probe_duration_seconds gauge",
        "# HELP solana_rpc_comparison_timestamp_seconds Unix timestamp when this probe round started.",
        "# TYPE solana_rpc_comparison_timestamp_seconds gauge",
        f"solana_rpc_comparison_timestamp_seconds {timestamp:.6f}",
    ]
    for name, slot, duration in results:
        labels = f'rpc="{name}",commitment="{config["commitment"]}"'
        metrics.append(f"solana_rpc_probe_success{{{labels}}} {int(slot is not None)}")
        metrics.append(f"solana_rpc_probe_duration_seconds{{{labels}}} {duration:.6f}")
        if slot is not None:
            metrics.append(f"solana_rpc_slot{{{labels}}} {slot}")
    return "\n".join(metrics) + "\n"


def write_metrics(output, content):
    output = Path(output)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=output.parent, prefix=".solana-slot-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        config = json.loads(Path(args.config).read_text())
        write_metrics(args.output, collect(config))
    except Exception:
        # 配置解析异常同样不能泄露认证信息。
        parser.exit(
            1, "slot comparison failed; check configuration and output permissions\n"
        )


if __name__ == "__main__":
    main()
