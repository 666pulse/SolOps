#!/usr/bin/env bash
# 只处理空整盘或已有 ext4，不提供 FORCE/wipe
set -euo pipefail
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
changed() { printf 'CHANGED: %s\n' "$*"; }

ledger_disk='' accounts_disk='' owner=solana allow_format=0
while (($#)); do
    case "$1" in
        --ledger_disk|--accounts_disk|--owner)
            [[ $# -ge 2 && -n "$2" && "$2" != -* ]] || die "$1 缺少参数"
            case "$1" in
                --ledger_disk) ledger_disk=$2 ;;
                --accounts_disk) accounts_disk=$2 ;;
                --owner) owner=$2 ;;
            esac
            shift 2 ;;
        --allow-format) allow_format=1; shift ;;
        *) die "用法: $0 --ledger_disk /dev/... --accounts_disk /dev/... --owner solana [--allow-format]" ;;
    esac
done
[[ $EUID -eq 0 ]] || die '必须以 root 运行'
[[ -n "$ledger_disk" && -n "$accounts_disk" ]] || die '必须指定两块盘'
for cmd in lsblk blkid wipefs mkfs.ext4 findmnt mount readlink flock awk cmp stat; do
    command -v "$cmd" >/dev/null || die "缺少命令: $cmd"
done
id "$owner" >/dev/null 2>&1 || die "用户不存在: $owner"
exec 9>/run/lock/solana-disks.lock
flock -n 9 || die '另一个磁盘准备进程正在运行'

ledger_disk=$(readlink -f "$ledger_disk")
accounts_disk=$(readlink -f "$accounts_disk")
[[ "$ledger_disk" != "$accounts_disk" ]] || die '两块盘不能是同一设备（包括别名）'
devices=("$ledger_disk" "$accounts_disk")
mounts=(/mnt/ledger /mnt/accounts)
filesystems=()

# 完整检查两块盘，再执行任何格式化，避免第二块盘有问题时已改动第一块。
for index in 0 1; do
    dev=${devices[$index]}
    mnt=${mounts[$index]}
    [[ $(lsblk -dnro TYPE "$dev") == disk ]] || die "$dev 必须是整盘"
    [[ $(lsblk -nrpo NAME "$dev" | wc -l) -eq 1 ]] || die "$dev 存在分区/映射设备"
    [[ -z $(ls "/sys/class/block/${dev##*/}/holders") ]] || die "$dev 被其他设备占用"
    current=$(findmnt -rn -S "$dev" -o TARGET || true)
    [[ -z "$current" || "$current" == "$mnt" ]] || die "$dev 已挂载到 $current"
    mounted_source=$(findmnt -rn -M "$mnt" -o SOURCE || true)
    if [[ -n "$mounted_source" ]]; then
        [[ $(readlink -f "$mounted_source") == "$dev" ]] || die "$mnt 被其他设备占用"
    elif [[ -d "$mnt" && -n $(ls -A "$mnt") ]]; then
        die "$mnt 非空，拒绝覆盖系统盘上的现有数据"
    fi
    [[ ! -L "$mnt" ]] || die "$mnt 不能是符号链接"
    fs=$(blkid -s TYPE -o value "$dev" || true)
    signatures=$(wipefs --noheadings --output TYPE "$dev" | awk 'NF {print $1}')
    case "$fs" in
        ext4)
            # 重复的 ext4 superblock 签名可以保留，分区表等其他签名不能保留。
            [[ -z $(printf '%s\n' "$signatures" | awk '$1 != "ext4" {print}') ]] || die "$dev 存在额外磁盘签名"
            uuid=$(blkid -s UUID -o value "$dev")
            [[ -n "$uuid" ]] || die "$dev 缺少 UUID"
            ;;
        '')
            [[ -z "$current" && -z "$mounted_source" ]] || die "$dev 已挂载但无法识别文件系统"
            [[ -z "$signatures" ]] || die "$dev 存在磁盘签名，拒绝格式化"
            [[ $allow_format -eq 1 ]] || die "$dev 是空盘；核对设备后设置 allow_format: true"
            ;;
        *) die "$dev 已有 $fs，拒绝格式化或按 ext4 挂载" ;;
    esac
    # 不覆盖系统管理员为同一 UUID/设备/挂载点设置的不同映射。
    awk -v dev="$dev" -v uuid="${uuid:-}" -v mnt="$mnt" '
        /^[[:space:]]*#/ || NF < 2 {next}
        ($1 == dev || (uuid != "" && $1 == "UUID=" uuid)) && $2 != mnt {exit 1}
        $2 == mnt && $1 != dev && (uuid == "" || $1 != "UUID=" uuid) {exit 1}
    ' /etc/fstab || die "$dev 或 $mnt 与现有 fstab 映射冲突"
    filesystems+=("$fs")
    unset uuid
done

for index in 0 1; do
    dev=${devices[$index]}
    mnt=${mounts[$index]}
    if [[ -z ${filesystems[$index]} ]]; then
        mkfs.ext4 -q -m 0 -L "solana-${mnt##*/}" "$dev"
        changed "格式化 $dev 为 ext4"
    fi
    uuid=$(blkid -s UUID -o value "$dev")
    [[ -n "$uuid" ]] || die "$dev 格式化后未找到 UUID"
    mkdir -p "$mnt"
    tmp=$(mktemp /etc/fstab.solana.XXXXXX)
    trap 'rm -f "$tmp"' EXIT
    awk -v uuid="$uuid" -v mnt="$mnt" -v dev="$dev" '
        BEGIN {entry="UUID=" uuid " " mnt " ext4 defaults,noatime 0 2"}
        $1 == "UUID=" uuid || $1 == dev || $2 == mnt {if (!seen++) print entry; next}
        {print}
        END {if (!seen) print entry}
    ' /etc/fstab > "$tmp"
    if ! cmp -s /etc/fstab "$tmp"; then
        [[ -f /etc/fstab.before-solana ]] || cp -a /etc/fstab /etc/fstab.before-solana
        cat "$tmp" > /etc/fstab
        changed "更新 fstab: $mnt"
    fi
    rm -f "$tmp"
    trap - EXIT
    if ! findmnt -rn -M "$mnt" >/dev/null; then
        mount "$mnt"
        changed "挂载 $mnt"
    fi
    [[ $(findmnt -rn -M "$mnt" -o FSTYPE) == ext4 ]] || die "$mnt 不是 ext4"
    [[ $(readlink -f "$(findmnt -rn -M "$mnt" -o SOURCE)") == "$dev" ]] || die "$mnt 挂载源不符"
    options=",$(findmnt -rn -M "$mnt" -o OPTIONS),"
    if [[ "$options" != *,noatime,* ]]; then
        mount -o remount,noatime "$mnt"
        changed "设置 $mnt noatime"
    fi
    if [[ $(stat -c '%U:%G' "$mnt") != "$owner:$owner" ]]; then
        chown "$owner:$owner" "$mnt"
        changed "设置 $mnt 属主"
    fi
done
findmnt -rn -M /mnt/ledger -o SOURCE,TARGET,FSTYPE,OPTIONS
findmnt -rn -M /mnt/accounts -o SOURCE,TARGET,FSTYPE,OPTIONS
