"""等待 Ansible 异步下载，并定期将下载器原有的 tqdm 输出回显到控制机。"""

import re
import time

from ansible.plugins.action import ActionBase
from ansible.utils.display import Display


display = Display()
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# 上游节点扫描条没有名称；文件下载条以 snapshot 文件名开头。
NODE_SCAN_PROGRESS = re.compile(r"^\d+%\|")


def progress_lines(output):
    """将 tqdm 的回车刷新和普通日志转换为安全的终端文本。"""
    return [
        cleaned
        for line in ANSI_ESCAPE.sub("", output).splitlines()
        if (cleaned := "".join(char for char in line if char.isprintable()).strip())
        and not NODE_SCAN_PROGRESS.match(cleaned)
    ]


class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None):
        result = super().run(tmp, task_vars)
        if self._task.check_mode:
            return {**result, "skipped": True, "msg": "检查模式不启动或等待快照下载"}

        _, args = self.validate_argument_spec(
            argument_spec={
                "jid": {"type": "str", "required": True},
                "async_dir": {"type": "path", "required": True},
                "log_path": {"type": "path", "required": True},
                "timeout": {"type": "int", "required": True},
                "interval": {"type": "int", "default": 15},
            }
        )
        if args["timeout"] <= 0 or args["interval"] <= 0:
            return {**result, "failed": True, "msg": "timeout 和 interval 必须大于 0"}

        task_vars = task_vars or {}
        host = task_vars.get("inventory_hostname", "snapshot")
        status_args = {"jid": args["jid"], "_async_dir": args["async_dir"]}
        # 异步 wrapper 负责终止超时进程；额外留出写回退出状态的时间。
        deadline = time.monotonic() + args["timeout"] + 30
        previous = None
        while True:
            status = self._execute_module(
                module_name="ansible.builtin.async_status",
                module_args=status_args,
                task_vars=task_vars,
            )
            if status.get("unreachable"):
                return {**result, **status}
            log = self._execute_module(
                module_name="snapshot_log_tail",
                module_args={"path": args["log_path"]},
                task_vars=task_vars,
            )
            lines = progress_lines(log.get("stdout", ""))
            latest = lines[-1] if lines else "等待下载器输出进度…"
            if latest != previous and not self._task.no_log:
                # 与 tqdm 一样使用 stderr，避免 stdout 接入管道时缓冲进度。
                display.display(f"snapshot [{host}] {latest}", stderr=True)
                previous = latest

            if status.get("finished") or status.get("failed"):
                failed = bool(status.get("failed") or status.get("rc", 0) != 0)
                if status.get("finished"):
                    self._execute_module(
                        module_name="ansible.builtin.async_status",
                        module_args={**status_args, "mode": "cleanup"},
                        task_vars=task_vars,
                    )
                return {
                    **result,
                    **status,
                    "changed": False,
                    "failed": failed,
                    "msg": (
                        f"快照下载失败：{status.get('msg', '下载器返回非零退出码')}；"
                        if failed
                        else "快照下载完成；"
                    )
                    + f"完整进度日志：{args['log_path']}",
                    "progress_tail": lines[-20:] if failed else lines[-1:],
                }

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {
                    **result,
                    "failed": True,
                    "ansible_job_id": args["jid"],
                    "msg": f"等待异步下载结果超时，请检查 {args['log_path']}",
                }
            time.sleep(min(args["interval"], remaining))
