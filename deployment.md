# Solana RPC 部署指南

配置已就绪：直接查看 [按顺序部署](readme.md#按顺序部署)。已有节点只更新监控：使用 [监控更新命令](readme.md#已有节点更新监控)。

首次部署按本页的 **准备服务器 → 配置 inventory → 分步部署 → 验收** 操作。

文中的 **控制机** 是运行 Ansible 的电脑；**目标服务器** 是运行 Solana 的机器。除明确标注外，Ansible 命令均在控制机的 `solops` 目录执行。

## 1. 准备服务器

| 项目 | 要求 |
| --- | --- |
| 系统 | Ubuntu 22.04+、x86_64，建议 24.04 |
| 登录 | SSH 用户能够 sudo |
| 磁盘 | 系统盘之外，两块独立 NVMe：ledger 和 accounts |
| 快照空间 | `/mnt/ledger/snapshots`，需同时容纳快照归档、账本和启动过程数据 |
| 运行方式 | systemd，不需要 Docker |

资源容量应按索引、历史保留量和请求负载规划，参考 [Agave 硬件要求](https://docs.anza.xyz/operations/requirements)。本仓库仅在 Latitude.sh 上测试过。

在 **目标服务器** 安装 Python 和 sudo：

```bash
sudo apt-get update
sudo apt-get install -y python3 sudo
```

## 2. 准备控制机

进入克隆后的 `solops` 仓库根目录，安装 `ansible-core >= 2.17`。如果当前已在仓库根目录，跳过第一行：

```bash
cd solops
python3 -m venv .venv
source .venv/bin/activate
python -m pip install 'ansible-core>=2.17'
```

首次创建本地 inventory；已有配置不覆盖：

```bash
cp -n inventory.mainnet.rpcs.yml.example inventory.mainnet.rpcs.yml
```

[配置示例：inventory](inventory.mainnet.rpcs.yml.example) 中的 IP、私钥路径和磁盘名不能直接使用。`ansible.cfg` 默认读取 `inventory.mainnet.rpcs.yml`。

也可以使用 `inventory.mainnet.rpcs.local.yml`，但后续每条 Ansible 命令都需加 `-i inventory.mainnet.rpcs.local.yml`。真实主机配置、密钥和 API key 不提交到 Git。

## 3. 配置 SSH 与磁盘

### SSH 连接

编辑 `mainnet_rpcs.hosts.rpc01` 下的字段：

```yaml
ansible_host: 服务器 IP
ansible_user: 服务器 SSH 用户名
ansible_ssh_private_key_file: ~/.ssh/私钥
```

私钥路径位于控制机。将命令中的 `rpc01` 替换为实际主机名；sudo 需要密码时加 `-K`。

在 **控制机** 测试连接并查看磁盘：

```bash
ansible rpc01 -m ping
ansible rpc01 -b -m command -a 'lsblk -o NAME,PATH,SIZE,TYPE,FSTYPE,MOUNTPOINTS'
ansible rpc01 -b -m command -a 'ls -l /dev/disk/by-id/'
```

### 数据盘

根据实际磁盘信息，修改同一主机下的字段：

```yaml
ledger_disk: /dev/disk/by-id/实际ledger整盘ID
accounts_disk: /dev/disk/by-id/实际accounts整盘ID
allow_format: false
```

必须是两块不同的整盘设备，不能填分区或 `/mnt/ledger`、`/mnt/accounts` 等挂载目录。

- 已有 ext4 数据盘：保持 `allow_format: false`。
- 两块需要初始化的无签名空盘：确认设备后，首次部署才设置 `allow_format: true`。
- 磁盘初始化成功后：将 `allow_format` 改回 `false`。

Playbook 将两盘挂载到 `/mnt/ledger` 和 `/mnt/accounts`，保留已有 ext4 数据；其他签名、系统盘或占用冲突会让任务拒绝操作。

## 4. 配置网关与参考 RPC

### 网关必填项

编辑 `mainnet_rpcs.vars`：

```yaml
gateway_api_key: ""  # 必须填写实际密钥
gateway_allowed_cidrs: []
gateway_domain: ""
grafana_domain: ""
```

| 参数 | 配置方式 |
| --- | --- |
| `gateway_api_key` | 必填，1–256 位字母、数字、下划线或连字符；也可使用 Ansible Vault |
| `gateway_allowed_cidrs` | 可选来源白名单；空列表表示仅校验 API key |
| `gateway_domain` | 空值使用 IP 直连；填写真实域名并完成 DNS 解析后启用 RPC HTTPS/WSS |
| `grafana_domain` | 可选独立域名，不能与 RPC 域名相同；空值使用 IP 的 3000 端口 |

客户端参数名为 **`api-key`**，不是 `api_key`。公网加密访问需使用 HTTPS/WSS；`8899/8900` 是明文 HTTP/WS。认证、密钥轮换和代理行为见 [网关说明](readme.md#网关行为)。

### 参考 RPC 与服务器出站网络

slot 对比默认使用 PublicNode 和 Solana Tracker，无需 API key，不使用 `api.mainnet.solana.com`。更换或添加 Helius、QuickNode 等端点，见 [配置 slot 对比](readme.md#配置-slot-对比)。这些请求从目标服务器发出。

**快照使用另一个参数 `snapshot_rpc_url`**，示例仍为 `https://api.mainnet-beta.solana.com`。若服务器访问不了官方 RPC，需要单独更换该参数；更改 slot 参考列表不会影响快照选源。

目标服务器还需能够下载 Ubuntu APT 软件包、GitHub 源码/发布包、Rust 工具链与依赖、Python 依赖、Go 工具链/模块，以及 Caddy/Grafana 官方软件源。网络不通时应先解决对应下载或 RPC 访问问题。

## 5. 配置网络

在云安全组和服务器防火墙中按需放行；**playbook 不会自动修改防火墙**：

| 入站端口 | 用途 | 放行范围 |
| --- | --- | --- |
| 实际 SSH 端口，通常 TCP 22 | 运维连接 | 管理来源 |
| TCP/UDP 8000–8030 | Solana P2P，含 gossip 8001 | 节点通信所需范围 |
| TCP 8899 / 8900 | Caddy HTTP RPC / WebSocket | 需要访问的客户端 |
| TCP 3000 | Grafana IP 直连 | 可信管理来源 |
| TCP 80 / 443 | 域名证书、跳转、HTTPS/WSS | 配置域名时按需放行；UDP 443 可选 |

`7211/7212`、`2019`、`3090`、`9100`、`9200` 仅监听 `127.0.0.1`，不需要对外开放。slot 对比采集器不监听端口。完整用途见 [服务与端口](readme.md#服务与端口)。

修改 inventory 中的端口后同步调整网络规则。部署前的端口校验只检查本仓库配置间的冲突，不能代替检查其他进程的实际占用。

## 6. 按顺序部署

准备完成后，执行 [README 开头的完整部署命令](readme.md#按顺序部署)。命令在 **控制机的 `solops` 目录** 运行，每一步成功后再执行下一步。

磁盘初始化后及时恢复 `allow_format: false`。节点身份私钥只保存在服务器，需自行安全备份。

| 阶段 | 预期表现 |
| --- | --- |
| 安装监控后、RPC 启动前 | 本地 slot 查询失败、Solana Exporter `/metrics` 可能返回 500，属于此阶段的预期状态 |
| 下载快照 | Ansible 每 15 秒回显进度；[下载详情及日志](readme.md#snapshot-下载) |
| 启动 RPC | 默认最多等待约 30 分钟 `getHealth=ok`；超时保留服务供排查 |
| RPC 健康后 | 本地指标自动恢复，Caddy 开始转发请求，再进行下方验收 |

这些步骤可以按需重跑：保留已有 identity 和监控数据；相同版本完成构建后不重复编译；已有账本或 RPC 正在运行时跳过快照下载。

配置熟悉后也可用 `ansible-playbook site.yml --limit rpc01` 执行完整 playbook。完整任务按 `site.yml` 顺序运行，首次部署优先使用 README 中的分步命令。

## 7. 验证 RPC 访问

### 先在目标服务器检查本地服务

```bash
sudo systemctl status solana-rpc caddy
curl -fsS http://127.0.0.1:7211 -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"getHealth"}'
```

正常响应中的 `result` 为 `ok`：

```json
{"jsonrpc":"2.0","result":"ok","id":1}
```

尚未健康时，查看 `sudo tail -f /var/log/solana/validator.log`，并按 [RPC 健康检查失败](readme.md#rpc-健康检查失败) 排查。`FAILED - RETRYING` 本身不能证明节点正在正常追块。

### 再从客户端检查网关

将以下地址和密钥替换为实际值。配置了域名时使用 HTTPS：

```bash
curl 'https://你的RPC域名/?api-key=你的实际密钥' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"getHealth"}'
```

未配置域名、使用受控网络中的 IP 直连时，对应地址如下：

| 协议 | IP 直连 | 域名访问 |
| --- | --- | --- |
| HTTP RPC | `http://服务器IP:8899/?api-key=密钥` | `https://RPC域名/?api-key=密钥` |
| WebSocket | `ws://服务器IP:8900/?api-key=密钥` | `wss://RPC域名/?api-key=密钥` |

| 网关状态码 | 含义 |
| --- | --- |
| `401` | API key 缺失、空白或错误 |
| `403` | 来源不符合额外配置的 IP 白名单 |
| `503` | 网关后端不可用或 RPC 尚未健康 |

## 8. 登录 Grafana 并验收监控

### 登录

- 未配置域名：`http://服务器IP:3000`。
- 配置了 `grafana_domain`：`https://Grafana域名`。
- 首次安装用户名：`admin`。

在 **目标服务器** 查看首次安装密码：

```bash
sudo cat /etc/grafana/admin_password
```

已有 Grafana 数据库会保留原账号和密码，重复部署不会重置。若已在界面改过密码，请使用修改后的密码；修改该文件不会重置现有管理员密码。

### 查看两个仪表盘

进入 **Dashboards → Solana Node Service**：

| 仪表盘 | 验收重点 |
| --- | --- |
| **Node Exporter Full** | CPU、内存、磁盘、网络等主机指标有数据 |
| **Solana RPC Slot Comparison** | 本地健康、slot 曲线、PublicNode / Solana Tracker 查询状态及差值有数据 |

slot 差值为 **外部 RPC slot − 本地 slot**，正数表示本地落后。`solana_node_num_slots_behind` 是 `getHealth` 自报值，不能等同于这条差值曲线。详细解释见 [slot 指标说明](readme.md#如何理解-slot-指标)。

两个仪表盘独立初始化，已有主机仪表盘不会阻止新 slot 仪表盘导入。重复部署保留界面编辑和删除；手动恢复方法见 [仪表盘说明](readme.md#仪表盘)。

### 检查采集链路

在 **目标服务器** 执行：

```bash
sudo systemctl status node-exporter solana-exporter prometheus grafana-server
sudo systemctl status solana-slot-compare.timer
curl -fsS http://127.0.0.1:9200/metrics
curl -fsS http://127.0.0.1:3090/api/v1/targets
curl -fsS http://127.0.0.1:3000/api/health
```

Prometheus 中 `node`、`solana`、`caddy` 目标应恢复为 `up`，Grafana 健康接口应返回数据库 `ok`。slot 对比的 oneshot 服务每轮结束后处于 inactive 是正常现象，应检查定时器与仪表盘的数据年龄：

```bash
sudo journalctl -u solana-slot-compare.service -n 30 --no-pager
cat /var/lib/node_exporter/textfile_collector/solana-slot-compare.prom
```

参考 RPC 失败时先排查服务器出站网络、认证或限流；数据达到 60 秒会被对比图过滤。告警当前只在 Prometheus 中评估，未配置通知。
