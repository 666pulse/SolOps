#!/usr/bin/env python3
"""磁盘命令替身：只读写测试临时目录中的状态，不操作真实设备或挂载。"""

import json
import os
from pathlib import Path
import sys

command = Path(sys.argv[0]).name
args = sys.argv[1:]
state_file = Path(os.environ["DISK_TEST_STATE"])
state = json.loads(state_file.read_text())
disks = state["disks"]

if command == "ls":
    if args[-1].startswith("/sys/class/block/"):
        name = args[-1].split("/")[-2]
        print(disks[name].get("holder", ""), end="")
    else:
        os.execv("/bin/ls", ["ls", *args])
elif command == "lsblk":
    dev = args[-1]
    disk = disks[Path(dev).name]
    if "TYPE" in args:
        print(disk.get("type", "disk"))
    else:
        print(dev)
        if disk.get("partitions"):
            print(dev + "p1")
elif command == "blkid":
    disk = disks[Path(args[-1]).name]
    print(disk.get("fs", "") if "TYPE" in args else disk.get("uuid", ""))
elif command == "wipefs":
    disk = disks[Path(args[-1]).name]
    print(disk.get("signatures", disk.get("fs", "")))
elif command == "findmnt":
    disk = None
    if "-S" in args:
        disk = disks[Path(args[args.index("-S") + 1]).name]
    elif "-M" in args:
        mountpoint = args[args.index("-M") + 1]
        disk = next(
            (value for value in disks.values() if value.get("mount") == mountpoint),
            None,
        )
    if not disk or not disk.get("mount"):
        sys.exit(1)
    column = args[args.index("-o") + 1] if "-o" in args else "TARGET"
    values = {
        "SOURCE": disk["device"],
        "TARGET": disk["mount"],
        "FSTYPE": disk.get("fs", ""),
        "OPTIONS": disk.get("options", "rw,noatime"),
    }
    print(" ".join(values[key] for key in column.split(",")))
elif command == "mkfs.ext4":
    disk = disks[Path(args[-1]).name]
    disk.update(fs="ext4", uuid="uuid-" + Path(args[-1]).name, signatures="ext4")
    state["formats"] += 1
elif command == "mount":
    mountpoint = args[-1]
    for disk in disks.values():
        if disk["expected_mount"] == mountpoint:
            disk.update(mount=mountpoint, options="rw,noatime")
            break
    else:
        sys.exit(1)
else:
    sys.exit(f"未实现的测试命令: {command}")

state_file.write_text(json.dumps(state))
