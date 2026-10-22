# SolOps

SolOps 是 Solana RPC 自动化部署与运维工具集，使用 Ansible 在同一台服务器部署主网非投票 Agave RPC、Caddy 网关、Prometheus 和 Grafana。服务由 systemd 管理，默认逐台部署。

仓库仅提供示例配置。真实 inventory、API key、SSH 私钥和节点 identity 必须保留在本地或使用 Ansible Vault 管理；不要提交到 Git。测试中的 `fixture` / `test-key` 为虚构凭据。

## 部署前注意

**适用环境**：Ubuntu 22.04+、x86_64，建议使用 24.04；当前默认 Agave 版本为 4.3.0。仅在 [Latitude.sh](https://www.latitude.sh/) 上测试过，其他平台尚未验证。

- **设置防火墙和云安全组**：本 playbook 不会自动配置防火墙。按需放行实际 SSH 端口、P2P 的 TCP/UDP `8000-8030`、RPC/WS 的 TCP `8899/8900`、Grafana 的 TCP `3000`；启用域名 HTTPS 时放行 TCP `80/443`，UDP `443` 可选。SSH、Grafana 和私有 RPC 入口应限制可信来源；仅本机监听的 `7211/7212`、`2019`、`3090`、`9100` 不对外开放。

- **设置 API key**：部署前在本地 inventory 或 Ansible Vault 中填写非空的 `gateway_api_key`，真实密钥不要提交到 Git。客户端请求使用 `?api-key=<密钥>`，参数名为 `api-key`，不是 `api_key`；缺失或错误密钥会返回 `401`。公网访问使用 HTTPS/WSS，避免通过明文 HTTP/WS 传输密钥；详细配置和轮换方式见 [同机网关与监控](#同机网关与监控)。

## 按顺序部署

首次部署先完成 [环境与配置准备](deployment.md)。配置已就绪时，在 **控制机的 `solops` 目录** 按顺序执行，每一步成功后再执行下一步：

```bash
# 1. 检查配置语法
ansible-playbook site.yml --syntax-check

# 2. 安装基础依赖、准备磁盘、优化系统
ansible-playbook site.yml --limit rpc01 --tags hardware,tuning

# 3. 编译 Agave、生成或复用 identity 私钥
ansible-playbook site.yml --limit rpc01 --tags build,identity

# 4. 安装全部监控、slot 对比采集器和两个 Grafana 仪表盘
ansible-playbook site.yml --limit rpc01 --tags monitoring

# 5. 下载快照
ansible-playbook site.yml --limit rpc01 --tags snapshot

# 6. 启动 RPC，等待rpc health，再配置 Caddy 网关
ansible-playbook site.yml --limit rpc01 --tags service,gateway
```

将 `rpc01` 替换为实际主机名，sudo 需要密码时加 `-K`。磁盘初始化后将 `allow_format` 改回 `false`；部署完成后进行 [RPC 与监控验收](deployment.md#7-验证-rpc-访问)。

## 从哪里开始

| 你要做什么 | 入口 |
| --- | --- |
| 首次部署新服务器 | [部署指南：准备、配置、部署、验收](deployment.md) |
| 已有节点安装或更新全部监控 | 运行下方 `--tags monitoring` 命令 |
| 查看仪表盘和理解 slot 差值 | [监控与 Grafana](#监控与-grafana) |
| 更换参考 RPC | [配置 slot 对比](#配置-slot-对比) |
| 修改服务、升级版本或排查问题 | [日常运维](#日常运维) |
| 修改本仓库并运行验证 | [开发验证](#开发验证) |

本文中的 Ansible 命令均在控制机的 `solops` 目录执行；`systemctl`、日志和本机指标检查命令在目标服务器执行。

### 已有节点更新监控

```bash
ansible-playbook site.yml --limit rpc01 --tags monitoring
```

这个标签包含 Node Exporter、Solana Exporter、slot 对比采集器、Prometheus、Grafana 和两个仪表盘。**不会重启 Agave**；监控服务在配置变化时按需重启。

已有本地 inventory 可继续使用，新增监控参数由 [vars/solana_exporter.yml](vars/solana_exporter.yml) 补齐默认值。部署后进入 Grafana 的 **Solana Node Service** 文件夹。

## 服务与端口

| 服务 | 默认监听 | 用途 |
| --- | --- | --- |
| Caddy HTTP RPC | `:8899` | 带 API key 的 RPC 入口，转发到 `7211`；也支持 WS Upgrade |
| Caddy WebSocket | `:8900` | 带 API key 的 WS 入口，转发到 `7212` |
| Caddy HTTPS | `80/443` | 配置域名后启用证书验证、HTTP 跳转和 HTTPS/WSS |
| Grafana | `0.0.0.0:3000` | 仪表盘登录入口；也可通过独立域名访问 |
| Agave HTTP / WS | `127.0.0.1:7211/7212` | 内部 RPC；WS 端口始终为 RPC 端口加一 |
| Agave P2P | `8000-8030` TCP/UDP | 节点间通信，包含 gossip `8001` |
| Caddy 管理 / metrics | `127.0.0.1:2019` | 本机管理和指标采集 |
| Node Exporter | `127.0.0.1:9100` | 主机指标及 slot 对比指标 |
| Solana Exporter | `127.0.0.1:9200` | 本地 Solana 节点指标 |
| Prometheus | `127.0.0.1:3090` | 指标存储、查询和告警规则评估 |
| slot 对比采集器 | 无监听端口 | 定时查询 RPC，通过 Node Exporter 输出指标 |

端口可在本地 inventory 中覆盖。Playbook 会检查配置内的端口冲突，但不会配置防火墙；放行范围见 [网络配置](deployment.md#5-配置网络)。仅回环监听的端口不需要对外开放。`0.0.0.0` 是监听地址，客户端应使用服务器 IP 或域名。

## 监控与 Grafana

### 仪表盘

Grafana 自动配置 `Prometheus` 数据源，两个仪表盘都位于 **Solana Node Service** 文件夹：

| 名称 | 内容 | 导入文件 |
| --- | --- | --- |
| **Node Exporter Full** | CPU、内存、磁盘、网络、负载 | [node-dashboard.json](files/node-dashboard.json) |
| **Solana RPC Slot Comparison** | 本地健康、slot 高度、参考 RPC 差值、查询状态、耗时和数据年龄 | [solana-dashboard.json](files/solana-dashboard.json) |

登录地址和初始密码见 [Grafana 登录与验收](deployment.md#8-登录-grafana-并验收监控)。

两个仪表盘分别初始化一次。重复部署保留界面中的编辑，也不会重新创建手动删除的仪表盘。需要恢复或更新时，在 Grafana 的 **Dashboards → New → Import** 上传对应 JSON，并选择 Prometheus。

### 如何理解 slot 指标

| 指标 | 含义 |
| --- | --- |
| `solana_node_is_healthy` | 本地 `getHealth` 的健康状态 |
| `solana_node_num_slots_behind` | 本地 `getHealth` 自报的落后量；健康时为 0，**不是与指定外部 RPC 的差值** |
| `solana_node_slot_height` | Solana Exporter 采集的本地 finalized slot |
| `solana_rpc_slot_current` | 对比采集器取得的有效 slot；`rpc="local"` 为本地，其他名称为参考 RPC |
| `solana_rpc_slot_delta` | **参考 RPC slot − 本地 slot**；正数表示本地落后，负数表示本地领先 |
| `solana_rpc_probe_current` | 本轮有效查询状态：1 成功，0 失败 |

对比默认使用相同的 `finalized` commitment。参考 RPC 查询失败时不会拿旧 slot 继续比较；数据达到 60 秒或 Node Exporter 失联后，记录规则会隐藏过期值。无数据不能当作差值为 0。

最大参考差值只计算有效参考端点，应结合查询状态和数据年龄判断。采集、Prometheus 抓取、规则求值和网络传输都有延迟，少量 slot 差异不等于持续追块落后。

### 配置 slot 对比

默认参考为 [Solana Tracker](https://www.solanatracker.io/solana-rpc) 和 [PublicNode](https://solana.publicnode.com/)，不依赖 `api.mainnet.solana.com`。请求从 **目标 Solana 服务器** 发出，连通性以该服务器为准。

在本地 inventory 的 `mainnet_rpcs.vars` 中配置：

```yaml
solana_slot_commitment: finalized
solana_slot_timeout: 3
solana_slot_reference_rpcs:
  - name: solana-tracker
    url: https://rpc.solanatracker.io/public
  - name: publicnode
    url: https://solana-rpc.publicnode.com
  # - name: helius
  #   url: https://mainnet.helius-rpc.com/?api-key=填写自己的密钥
```

列表整体替换，支持 1–7 个参考端点。各端点必须属于主网，`name` 必须唯一；`local` 保留给自动加入的本机 RPC。需要认证请求头时可添加 `headers` 映射。真实 API key 只写本地 inventory 或 Vault，不提交到 Git。

仅修改参考端点后，在控制机执行：

```bash
ansible-playbook site.yml --limit rpc01 --tags solana_slot_compare
```

任务会更新配置并立即执行一轮采集。采集器并发发送 `getSlot`，默认超时 3 秒，单轮 systemd 硬超时 12 秒；完成后等待 15 秒开始下一轮，不重试或重叠执行。结果原子写入 Node Exporter textfile，指标和异常日志不包含端点 URL 或密钥。

### 采集与告警

Prometheus 默认每 15 秒采集自身、Node Exporter、Solana Exporter 和 Caddy。数据默认保留 **365 天或 50GB，以先达到的条件为准**；系统盘还需为 WAL 等预留空间。

Solana Exporter 使用轻量模式，直接访问本机 `7211`，不经过 Caddy，也不读取 identity 私钥。它提供健康、版本、identity、epoch、slot 和区块高度等节点指标，不采集投票账户、质押、奖励、余额和区块大小。默认每 15 秒查询 slot，单次 RPC 超时 2 秒；scrape 超时为 `3 × RPC 超时 + 1` 秒，默认 7 秒，必须不大于 `monitoring_scrape_interval`。

[告警规则](files/prometheus-alerts.yml) 覆盖：

| 条件 | 持续时间 |
| --- | --- |
| 指标采集失败、网关后端不健康、本地 Solana 不健康 | 2 分钟 |
| CPU 使用率超过 90% | 15 分钟 |
| 可用内存低于 10% | 10 分钟 |
| 系统盘或数据盘可用空间低于 10% | 5 分钟 |
| `getHealth` 自报落后超过 100 slots | 5 分钟 |
| 过去 5 分钟 finalized slot 不变，且采集正常 | 条件再持续 5 分钟 |
| slot 对比数据缺失或达到 60 秒，且 Node Exporter 正常 | 2 分钟 |

外部 RPC 差值目前用于仪表盘展示；“落后超过 100 slots”的告警使用的是 `getHealth` 自报值。**尚未配置 Alertmanager 通知**，规则仅在 Prometheus 中评估。同机监控无法在整机宕机时继续告警，需要外部监控补充。

## 日常运维

### 按修改内容执行

修改本地 inventory 后，在控制机按需运行：

| 操作 | 命令中的标签 | 影响 |
| --- | --- | --- |
| 安装或更新全部监控 | `--tags monitoring` | 不重启 Agave |
| 更新参考 RPC | `--tags solana_slot_compare` | 更新配置并采集一次 |
| 更新 Grafana 配置或首次导入仪表盘 | `--tags grafana` | 按需重启 Grafana，保留已有仪表盘修改 |
| 更新 Prometheus 配置与规则 | `--tags prometheus` | 校验后按需重启 Prometheus |
| 更新网关域名、白名单或 API key | `--tags gateway` | 校验后 reload Caddy |
| 修改 Agave 启动参数 | `--tags service` | 配置变化时重启 RPC，等待健康 |
| 升级 Agave 版本 | `--tags build,service` | 构建新版本并切换服务 |
| 首次启动前下载快照 | `--tags snapshot` | 已有账本或 RPC 正在运行时跳过 |

完整命令格式：

```bash
ansible-playbook site.yml --limit rpc01 --tags monitoring
```

单独更新组件时，也可使用 `node_exporter`、`solana_exporter` 标签。任务按 `site.yml` 的顺序执行，不按 `--tags` 的书写顺序执行。

从旧的内部 RPC 端口迁移到 `7211/7212` 时，使用 `--tags service,monitoring,gateway`。所选任务会更新监控配置，将 Agave 切换到内部端口并等待健康，最后让 Caddy 接管 `8899/8900`；直接访问旧内部端口的程序也需更新。

### 查看状态与日志

在目标服务器执行：

```bash
sudo systemctl status solana-rpc caddy node-exporter solana-exporter prometheus grafana-server
sudo systemctl status solana-slot-compare.timer
sudo tail -f /var/log/solana/validator.log
```

检查本地 RPC 和监控端点：

```bash
curl -fsS http://127.0.0.1:7211 -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"getHealth"}'
curl -fsS http://127.0.0.1:9200/metrics
curl -fsS http://127.0.0.1:3090/-/ready
curl -fsS http://127.0.0.1:3090/api/v1/targets
curl -fsS http://127.0.0.1:3000/api/health
```

### 暂停或恢复 slot 对比

在目标服务器执行。暂停时同时停止定时器和正在运行的采集进程：

```bash
sudo systemctl stop solana-slot-compare.timer solana-slot-compare.service

# 恢复定时采集
sudo systemctl start solana-slot-compare.timer
```

停止不会禁用开机启动。暂停期间仪表盘会显示数据过期，并触发相应告警。

### RPC 健康检查失败

`service` 默认最多等待约 30 分钟 `getHealth=ok`。超时会报错并保留运行进程。`FAILED - RETRYING` 仅说明尚未健康，应结合进程状态和日志判断：

| 现象 | 先检查什么 |
| --- | --- |
| `solana-rpc` 已经 failed | validator 日志中的退出原因 |
| 进程运行但不健康 | 是否仍在启动、追块，以及日志中的错误 |
| 日志提示 `requires the following capabilities` | systemd 是否包含 `CAP_NET_ADMIN`、`CAP_NET_RAW`；Agave 4.3 的 XDP 发送需要这两个能力 |
| RPC 未启动，exporter `/metrics` 返回 500 | 先启动并确认本地 RPC；exporter 会自动恢复采集 |
| 参考 RPC 失败或超时 | 从目标服务器检查端点连通性、限流和认证配置 |

修正启动配置后，在控制机运行 `--tags service,gateway`。详细访问验收见 [部署指南](deployment.md#7-验证-rpc-访问)。

## 网关行为

Caddy 从官方 GitHub Release 下载固定版本的标准 `.deb` 包，并校验 SHA-256；版本与校验值记录在 [vars/caddy.yml](vars/caddy.yml)，升级时需同时更新两者。安装前清理本项目旧的 Cloudsmith 源，避免该源返回 HTTP 402 时阻塞 APT。软件包保留官方用户及 systemd 服务，安装阶段禁止自动启动，配置校验后再启动或重启。

Caddy 使用标准软件包校验查询参数 **`api-key`**，大小写敏感。密钥允许 1–256 位字母、数字、下划线和连字符，必须配置。可以在本地 inventory 直接填写，也可通过 Vault 或控制机环境变量提供：

```yaml
gateway_api_key: "{{ lookup('ansible.builtin.env', 'SOLANA_RPC_API_KEY') }}"
```

缺失、空值或错误密钥返回 `401`；启用 `gateway_allowed_cidrs` 后，来源不符合白名单返回 `403`。通过认证后，Caddy 删除 URL 中的 `api-key` 再转发，其他参数保留；访问日志中过滤该密钥参数。目标配置权限为 `root:caddy 0640`，Ansible 不输出配置内容或 diff。

`8899/8900` 使用明文 HTTP/WS，公网加密访问应配置域名并使用 HTTPS/WSS。更换密钥后执行 `--tags gateway`，新连接使用新密钥；已有 WebSocket 连接按 Caddy reload 的关闭行为处理。

网关默认每 10 秒检查本机 HTTP RPC，要求 HTTP 200 且 JSON `result` 为 `ok`；未就绪时新请求返回 `503`。WS 后端也使用此 HTTP 健康检查，不单独探测 WS 端口。

当前配置面向客户端直连 Caddy，白名单不信任客户端提供的 `X-Forwarded-For`。前置 CDN 或其他代理时需另行配置受信代理及访问控制；本仓库未安装限流插件。

## 快照与 Agave

### Snapshot 下载

使用 [1dad-io/solana-snapshot-finder](https://github.com/1dad-io/solana-snapshot-finder) 的固定提交，在服务器独立 venv 中运行。任务获取上游源码与依赖，不复制或修改 Finder 源码。

Finder 从 `snapshot_rpc_url` 获取集群节点，按快照新鲜度、延迟和实际吞吐选源，下载 full 及兼容的 incremental；已有可复用 full 时优先补齐 incremental。**`snapshot_rpc_url` 与 slot 对比的参考 RPC 列表独立**；示例仍使用 `https://api.mainnet-beta.solana.com`，不可达时需单独配置服务器能访问的主网 RPC。

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `snapshot_finder_version` | 见 [inventory 示例](inventory.mainnet.rpcs.yml.example) | 固定完整 commit SHA |
| `snapshot_min_download_speed_mb` | 50 | 最低下载速度，十进制 MB/s |
| `snapshot_max_local_age` | 2500 | 本地 full 复用阈值，单位 slots |
| `snapshot_threads` | 128 | RPC 探测并发，不是单文件下载连接数 |
| `snapshot_search_timeout` | 600 秒 | 搜索预算；已开始的 full 下载及恢复可能超过该预算 |
| `snapshot_timeout` | 10800 秒 | 整个下载任务的硬超时 |
| `agave_snapshot_min_speed` | 10485760 字节/秒 | Agave 原生下载阈值，独立于 Finder |

Finder 使用 requests 单连接下载，每个文件先写 `.part`，完成后改名；慢源会中止并重新选源。失败文件会清理，重跑会重新下载该文件，不提供单文件分段下载或断点续传。

已有 `/mnt/ledger/rocksdb` 或 RPC 正在运行时，跳过 Finder 安装与下载。失败会让任务报错，不自动清理账本。设置 `snapshot_download: false` 可跳过 Finder，交给 Agave 原生获取；启动脚本保留网络拉取能力。

Ansible 每 15 秒回显一次下载进度，选源阶段只显示普通日志，过滤节点扫描的百分比进度条；实际下载文件且下载源提供总大小时才显示百分比和预计剩余时间。日志位于服务器 `/mnt/ledger/snapshots/snapshot-progress.log`，每次启动下载覆盖，原始日志保留扫描进度。Finder 的 `snapshot-finder.log`、`snapshot.json`、`blacklist.json` 也在快照目录中。

进度读取每次最多取日志末尾 8192 字节，并替换截断或写入中的不完整 UTF-8 字符，避免进度条导致 Ansible 解码失败；原始日志不变。如果错误定位到“等待快照下载并回显进度”，应结合完整错误和服务器日志确认下载结果，该任务位置本身不代表快照下载失败。

### 构建与运行约定

| 步骤 | 行为 |
| --- | --- |
| 磁盘 | 两盘全部检查通过后才操作；仅格式化显式允许的无签名空整盘。已有 ext4 保留；分区、RAID/LVM、系统盘、设备占用、非空挂载目录或 fstab 冲突均拒绝。使用 UUID 挂载并启用 noatime |
| 系统优化 | 配置 sysctl、socket buffer、map count、文件句柄、memlock 和 CPU governor；不修改 GRUB、SMT、IRQ 绑核或重启机器 |
| Agave 编译 | 固定 `v{{ agave_version }}`，使用上游 Rust toolchain 和 `scripts/cargo-install-all.sh`。源码与产物按版本隔离，成功后写 `.built` |
| identity | 目标机生成或迁移已有私钥，权限 0600；目标已存在时保留。不会复制回控制机，需自行安全备份；不创建 vote account |
| RPC 运行 | systemd 管理、绑定数据盘挂载、配置日志轮转，参数或版本变化才重启 |

当前启动参数针对 Agave 4.3：`--limit-blockstore-size` 按 **data + coding shreds 总数**计算，`--accounts-db-write-cache-limit` 按字节计算。默认只为 Address Lookup Table 程序建索引；需要完整 `program-id` 索引时，将 `account_index_include_keys` 改为 `[]` 并重新评估内存。

相同版本不会重复编译。需要调整编译选项并重编同一版本时，先停止 RPC，再删除该版本的 `.built` 标记并执行 `--tags build,service`，避免覆写正在运行的二进制；保留源码缓存。切换大版本前核对上游参数，启动任务也会用目标二进制检查参数解析。

## 文件与版本参考

### 服务器上的文件

以下以 `solana_user: solana`、`agave_version: 4.3.0` 为例：

| 路径 | 内容 |
| --- | --- |
| `/home/solana/agave/src/4.3.0/` | Agave 源码与编译缓存 |
| `/home/solana/agave/releases/4.3.0/` | `bin/agave-validator`、`bin/solana-keygen` 和 `.built` |
| `/home/solana/identity.json` | 节点私钥，0600 |
| `/home/solana/start_validator.sh` | 启动脚本 |
| `/home/solana/tools/` | 磁盘脚本、Rust 安装器、snapshot-finder 及 venv |
| `/mnt/ledger`、`/mnt/accounts` | 账本盘和账户盘；快照在 `/mnt/ledger/snapshots` |
| `/var/log/solana/validator.log` | RPC 日志 |
| `/opt/solana-exporter/` | 独立 Go 工具链、固定提交源码和 exporter 二进制 |
| `/etc/solana-slot-compare.json` | slot 对比配置，`root:solana_slot_compare 0640` |
| `/var/lib/node_exporter/textfile_collector/solana-slot-compare.prom` | slot 对比指标 |
| `/etc/grafana/admin_password` | Grafana 首次安装密码 |
| `/var/lib/grafana/.node-dashboard-imported` | 主机仪表盘初始化标记 |
| `/var/lib/grafana/.solana-dashboard-imported` | slot 仪表盘初始化标记 |

### 版本与仪表盘来源

Node Exporter、Prometheus 使用固定版本官方发布包并校验 SHA-256；更新时同步修改版本和校验和。Caddy、Grafana 从官方稳定 APT 源安装。

[Solana Exporter](https://github.com/asymmetric-research/solana-exporter) 当前固定为 `v3.1.0` 的提交 `4b7d4ab6e0358a63e20d7c1fff37642d2501ba7d`，使用独立 Go `1.26.8`，不替换系统 Go。源码包和工具链都校验 SHA-256，参数见 [默认配置](vars/solana_exporter.yml)。相同版本跳过构建，源码提交或 Go 版本变化时使用新的安装目录。

Node Exporter Full 原样使用 [Grafana ID 1860、revision 45](https://grafana.com/api/dashboards/1860/revisions/45/download)，UID 为 `rYdddlPWk`，源自 [rfmoz/grafana-dashboards](https://github.com/rfmoz/grafana-dashboards)，采用 [Apache-2.0](https://github.com/rfmoz/grafana-dashboards/blob/master/LICENSE)。原始 JSON SHA-256：

```text
184c6b7409f306da75525d7772f71945b10cea23ad16b5d78c4698ea0ea51986
```

更新主机仪表盘时核对许可证和原始文件校验和，原样替换 JSON，并在 Grafana 手动重新导入。依赖可选 `systemd`、`processes` 采集器的面板可能显示 No data。

旧版文件托管的主机仪表盘会在 `grafana` 任务中解除托管；保留空 provider 的 `disableDeletion: true` 设置以保留已有仪表盘。新 slot 仪表盘 UID 为 `solana-rpc-slots`，使用独立初始化标记。

### 仓库导航

| 路径 | 用途 |
| --- | --- |
| [deployment.md](deployment.md) | 首次部署与验收 |
| [inventory.mainnet.rpcs.yml.example](inventory.mainnet.rpcs.yml.example) | 可提交的配置示例；实际 `inventory.mainnet.rpcs.yml` 已忽略 |
| [site.yml](site.yml) | 部署入口、参数校验及任务标签 |
| [tasks/](tasks/) / [templates/](templates/) | 各组件任务及配置模板 |
| [files/](files/) | 采集脚本、告警规则、仪表盘 JSON |
| [action_plugins/snapshot_progress.py](action_plugins/snapshot_progress.py) | 异步下载进度回显 |
| [tests/](tests/) | 磁盘保护、RPC、网关、监控及仪表盘测试 |

## 开发验证

在控制机的 `solops` 目录执行基础检查：

```bash
cp -n inventory.mainnet.rpcs.yml.example inventory.mainnet.rpcs.yml
ansible-playbook site.yml --syntax-check
ansible-playbook site.yml --list-tasks
ruff format --check files tests action_plugins library
ruff check files tests action_plugins library
bash -n files/setup_disks.sh
promtool test rules tests/solana-alerts.yml tests/solana-slot-rules.yml
```

完整 Python 测试需要 `pyyaml`、`jinja2`、Ansible，以及对应的外部工具：

```bash
SNAPSHOT_FINDER_SOURCE=/path/to/solana-snapshot-finder \
CADDY_BIN=/path/to/caddy \
SOLANA_EXPORTER_BIN=/path/to/solana-exporter \
PROMTOOL_BIN=/path/to/promtool \
python3 -m unittest discover -s tests -v
```

| 测试 | 环境与验证范围 |
| --- | --- |
| 磁盘保护 | Linux；使用临时文件和命令替身，不操作真实磁盘 |
| Snapshot Finder | inventory 固定提交源码及其依赖；本地 HTTP fixture，不下载主网快照 |
| 下载进度与 identity | 真实 Ansible 本地连接；验证进度、超时、检查模式和私钥保留 |
| 网关 | 标准 Caddy、本地 HTTP/WS fixture 与临时 CA；验证鉴权、转发、健康恢复及白名单；占用 `22019/28443/28889/28890/28899/28900` |
| Solana Exporter | 固定提交构建的二进制；验证轻量模式、健康和 RPC 故障恢复 |
| slot 对比与规则 | 验证并发、同 commitment、正负差值、限流、超时、过期过滤及凭据保护 |
| Grafana 导入 | 模拟 API + 真实 Ansible；验证两个仪表盘独立初始化，保留编辑和删除 |

部署任务还会用 `promtool` 校验实际 Prometheus 配置和规则。`--check` 不保证能在全新机器上完整预演，因为后续任务依赖前面安装的程序与目录。完整构建、磁盘挂载、主网同步和负载表现仍需在目标硬件上验收。

---

参考：

[Agave 4.3 CLI](https://github.com/anza-xyz/agave/blob/v4.3.0/validator/src/cli.rs)

[blockstore 参数](https://github.com/anza-xyz/agave/blob/v4.3.0/validator/src/commands/run/args/blockstore_options.rs)

[XDP 启动要求](https://github.com/anza-xyz/agave/blob/v4.3.0/validator/src/commands/run/execute.rs)

[RPC 官方说明](https://docs.anza.xyz/operations/setup-an-rpc-node/)
