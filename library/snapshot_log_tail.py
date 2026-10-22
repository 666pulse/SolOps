#!/usr/bin/python
"""以固定大小读取日志末尾，在返回给 Ansible 前处理不完整的 UTF-8 字符。"""

import os

from ansible.module_utils.basic import AnsibleModule


DOCUMENTATION = r"""
---
module: snapshot_log_tail
short_description: Read a bounded UTF-8 snapshot progress log tail
description:
  - Reads at most 8192 bytes and replaces incomplete or invalid UTF-8 characters.
  - A missing log returns empty output while the downloader starts.
options:
  path:
    description: Path to the downloader progress log.
    type: path
    required: true
"""

EXAMPLES = r"""
- name: Read snapshot progress
  snapshot_log_tail:
    path: /mnt/ledger/snapshots/snapshot-progress.log
"""

RETURN = r"""
stdout:
  description: UTF-8 log tail with replacement characters at incomplete boundaries.
  type: str
  returned: success
"""


def main():
    module = AnsibleModule(
        argument_spec={"path": {"type": "path", "required": True}},
        supports_check_mode=True,
    )
    try:
        with open(module.params["path"], "rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - 8192))
            output = stream.read(8192).decode("utf-8", errors="replace")
    except FileNotFoundError:
        output = ""
    except OSError as error:
        module.fail_json(msg=f"无法读取快照进度日志：{error}")
    module.exit_json(changed=False, stdout=output)


if __name__ == "__main__":
    main()
