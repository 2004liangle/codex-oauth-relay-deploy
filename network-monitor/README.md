# 本机网络流量监控

`network_monitor.py` 是 Usage 后台使用的本机 sidecar。它只读取指定 Linux
网卡的 RX/TX 字节计数，以 SQLite WAL 持久化监控期累计值，并提供受内部令牌
保护的只读 JSON 接口。

它统计的是本机网卡流量估算值，包含 SSH、系统更新、上游请求和其他进程流量，
不等同于云厂商的计费数据。首次启动以当时网卡计数为基线，监控期累计从 0 开始。

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `NETWORK_INTERFACE` | `eth0` | 要监控的网卡 |
| `NETWORK_MONITOR_DATABASE` | `/var/lib/codex-network-monitor/traffic.sqlite3` | SQLite 文件 |
| `NETWORK_MONITOR_LISTEN_HOST` | `127.0.0.1` | 监听地址，部署时应保持本机回环 |
| `NETWORK_MONITOR_LISTEN_PORT` | `18082` | 监听端口 |
| `NETWORK_MONITOR_INTERNAL_TOKEN` | 无 | 必填，至少 16 个字符 |
| `NETWORK_MONITOR_SAMPLE_SECONDS` | `2` | 实时采样周期，范围 0.5 至 60 秒 |
| `TRAFFIC_PACKAGE_TOTAL_GB` | `0` | 套餐总量，十进制 GB，可为小数；0 表示未配置 |
| `TRAFFIC_PACKAGE_TOTAL_BYTES` | 无 | 套餐精确字节数，不能与 TOTAL_GB 同时设置 |
| `TRAFFIC_PACKAGE_START_AT` | 无 | 套餐开始时间，带时区的 ISO 8601 |
| `TRAFFIC_PACKAGE_END_AT` | 无 | 套餐结束时间，带时区的 ISO 8601 |
| `TRAFFIC_PACKAGE_TX_OFFSET_BYTES` | `0` | 启用监控前已经使用的出站字节数 |
| `PURCHASED_BANDWIDTH_MBPS` | `0` | 已购带宽，十进制 Mbps；0 表示未配置 |

## 本机接口

所有接口都必须携带 `X-Codex-Network-Token`。公网 Nginx 应先完成 Usage 管理员
鉴权，再注入该请求头；不要把 sidecar 端口开放到公网。

- `GET|HEAD /summary`：实时速率、峰值、监控期累计、开机以来原始计数、套餐和带宽进度。
- `GET|HEAD /history?range=24h`：速率历史；range 可选 `15m`、`1h`、`6h`、`24h`、`7d`、`30d`。
- `GET|HEAD /healthz`：采集是否正常。

实时接口每 2 秒采样。细粒度点保留 48 小时，分钟汇总保留 400 天；历史接口最多
返回 720 个点。所有响应均带 `Cache-Control: no-store`。
