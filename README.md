# Codex OAuth Personal Relay Deploy

用于在个人服务器上部署 Codex OAuth 中转、OpenAI-compatible API、用量后台和受限管理查看入口。

> 这不是 OpenAI 官方项目或官方中转服务。脚本会安装并配置第三方社区组件，只建议同一账号持有人在自己的设备之间使用。

## 一键部署

```bash
curl -fsSL https://github.com/2004liangle/codex-oauth-relay-deploy/releases/latest/download/install.sh -o /tmp/install.sh && sudo bash /tmp/install.sh
```

安装过程中，终端会显示 Codex 设备授权网址和代码。可以在另一台带浏览器的电脑上完成授权。

这条命令只做三件事：下载引导脚本、自动校验完整安装器、开始安装。SHA-256 校验仍然存在，但不需要手工复制长字符串。

## 环境要求

- Ubuntu 22.04+ 或 Debian 12+
- x86_64 或 aarch64
- glibc 2.34+
- systemd 作为 PID 1
- root 或 sudo 权限
- 出站 TCP 443 可访问 GitHub、OpenAI 和 ChatGPT 相关域名
- 公网端口默认 `8317`；内部端口 `18080`、`18081`、`18082`、`18317`、`18318` 不得占用

基础中转支持上述系统。飞书图片 Sidecar 需要 Python 3.11+；即梦 Agent 透明抠图还需要 Node.js 22+、Chromium 和一次有效的即梦网页登录。登录配置会保存在服务端私有目录，不随临时目录或重启丢失。

## 安装结果

安装器会生成三组独立凭据，并保存到仅 root 可读的文件：

```text
/root/codex-relay-credentials.txt
```

文件中包含：

- OpenAI-compatible API Base URL 与 Relay API Key
- Usage Dashboard 地址与登录密码
- Management Panel 地址与 Management Key

客户端项目文件仍由客户端本地的 Codex 或兼容工具读写；服务器只处理模型请求。启用 Request Log 后，进入模型上下文的代码、提示词、工具参数和响应可能被记录在服务器上。

公网 API 精确放行模型列表、Chat Completions、Completions、Responses、图片生成和图片编辑。通用文件上传仍不开放。

## 用量后台

新浏览器第一次打开用量后台时默认显示简体中文，页面把常见性能缩写换成了直接说明，例如“开始回复等待时间”“整次请求耗时”“每分钟请求数”和“每分钟总用量”。`Token`、`API Key` 等接口中的固定名称会保留，避免配置时对不上字段。

右上角仍可切换英文或繁体中文，手动选择后会记住该语言。这个定制只替换浏览器中的静态页面；用量数据库、登录密码和后台 API 仍由 CPA Usage Keeper 管理。

### 本机网速和流量

用量后台的“服务器流量”页会显示当前上传、下载速度、监控以来的累计流量、峰值带宽和历史曲线。采集服务只监听 `127.0.0.1:18082`，外网不能直接连接；浏览器请求先由 CPA Usage Keeper 校验管理员会话，再由 Nginx 转发。使用 Relay API Key 登录的只读账号不能查看本机流量。

首次安装以当时的网卡计数为起点，不会把开机以来的历史流量误算成流量包用量。默认只统计、不启用流量包进度。安装时可设置流量包大小、周期和购买带宽，例如：

```bash
sudo env \
  TRAFFIC_PACKAGE_TOTAL_GB=1000 \
  TRAFFIC_PACKAGE_START_AT=2026-07-01T00:00:00+08:00 \
  TRAFFIC_PACKAGE_END_AT=2026-08-01T00:00:00+08:00 \
  PURCHASED_BANDWIDTH_MBPS=100 \
  bash /tmp/install.sh
```

如果流量包在安装监控前已经使用了一部分，可额外设置 `TRAFFIC_PACKAGE_TX_OFFSET_BYTES`，把云厂商已记录的出站字节数计入进度。安装后也可编辑 `/etc/codex-network-monitor/env` 并执行 `sudo systemctl restart codex-network-monitor` 更新这些值；数据库中的累计监控记录不会因此清空。

这里读取的是指定网卡的全部收发字节，包括中转请求、SSH、系统更新、飞书上传及其他网络通信。流量包通常按服务器出站流量结算，但云厂商可能有免计费方向、地域或协议规则，因此页面标注为本机估算，最终扣量仍以云厂商控制台为准。

## 图片生成与编辑

使用原来的 Base URL 和 Relay API Key 调用标准 OpenAI Images API。GPT Image 返回 Base64，下面的命令会在客户端解码成图片文件：

```bash
curl -sS "$API_BASE_URL/images/generations" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  --data '{
    "model": "gpt-image-2",
    "prompt": "A clean product photo of a white ceramic cup",
    "quality": "low",
    "output_format": "png"
  }' | jq -r '.data[0].b64_json' | base64 --decode > generated.png
```

图片 Base64 会经过中转并写入 Request Log。当前入口默认是 HTTP；经过不可信网络时必须先配置 HTTPS。

图片编辑使用标准 `POST /v1/images/edits` multipart 接口，支持最多 16 张本地参考图和可选 PNG alpha 遮罩。每张输入图必须小于 50 MB，整个请求不得超过 64 MiB；`/v1/files` 仍返回 `404`。64 MiB 是针对小内存个人服务器的安全上限，多张图片和遮罩需要共同计入。

## 通用 Relay Artifacts Skill

客户端不需要运行系统安装命令。下载 [`relay-artifacts.zip`](https://github.com/2004liangle/codex-oauth-relay-deploy/releases/latest/download/relay-artifacts.zip)，再用客户端的“上传技能”或“导入 Skill”功能选择这个 ZIP 即可。包内的使用说明、命令帮助和常见错误提示均为简体中文。

这个客户端包依赖服务器已经启用飞书附件 Sidecar；服务端安装和目录配置见 [`artifact-relay/README.md`](artifact-relay/README.md)。

ZIP 是标准 Agent Skills 文件包，顶层只有一个同名目录：

```text
relay-artifacts/
├── SKILL.md
├── scripts/
├── references/
└── assets/
```

不同客户端的入口名称会略有差异：

- WorkBuddy：进入“专家·技能·连接器”，点击“上传技能”，选择 ZIP。
- Kimi：在技能面板添加自定义 Skill；Kimi Code 可先解压 ZIP，再把里面的 `relay-artifacts/` 目录放入其 Skills 目录。
- 豆包：只有客户端明确支持标准 Agent Skills、执行随包 Python 脚本和读写本地项目文件时才能使用；仅能导入提示词但不能执行脚本的版本无法完成附件传输。这里不承诺所有豆包版本兼容。

上传 ZIP 后，直接用自然语言要求客户端执行任务，例如“生成一张低质量草稿并保存到项目目录”“用当前目录的两张参考图合成一张高质量图片”“把这张人物图抠成透明 PNG”“把这个附件通过飞书交给服务端处理并下载结果”。抠图会走服务端即梦 Agent 专用任务，不先重绘人物，也不需要手工输入脚本命令。

公开 ZIP 不包含个人 IP、Relay Key、飞书凭据或私有配置。首次使用仍需按 Skill 提示在客户端本地提供 Relay 地址和 Key；附件模式还要求客户端能使用与服务端相同的飞书身份。飞书里的输入附件和处理结果不会自动删除，只有用户明确要求时才删除。

客户端必须支持标准 Agent Skills、访问 Relay 和飞书、读写当前项目文件，并且能执行包内 Python 3 脚本，或具备等价的原生 HTTP 与飞书文件工具。云端客户端即使能导入 ZIP，如果不能访问本地项目目录，也不能代替本地客户端修改文件。

### Codex CLI 专用版

原来的 `relay-images` Skill 仍保留给 Codex CLI 或需要直接运行脚本的高级用法，但它不是 WorkBuddy、Kimi、豆包等客户端的首选安装方式。Codex CLI 用户可继续使用 [`install-relay-images-skill.sh`](https://github.com/2004liangle/codex-oauth-relay-deploy/releases/latest/download/install-relay-images-skill.sh)。

## 安全边界

- GitHub 引导脚本通过 HTTPS 下载，并在内部使用固定 SHA-256 校验完整安装器。
- 部署后的 Nginx 入口默认使用 HTTP。开放端口前，应把云安全组来源限制为自己的客户端 IP；来源不固定时先配置 HTTPS。
- CLIProxyAPI、Squid 和 Usage Keeper 的内部端口不得直接开放到公网。
- 网络流量采集服务的 `18082` 端口只监听本机回环地址，不需要也不应加入云安全组或系统防火墙放行规则。
- 所有公网推理路由都在 Nginx 读取正文前核对完整 Relay Key；仅有非空但错误的 Authorization 不会进入 CLIProxyAPI 请求日志。
- `/v1/images/edits` 只允许 `POST`，单请求上限为 64 MiB，并限制全局同时处理一个编辑上传；通用 `/v1/files`、尾斜杠和子路径保持关闭。
- CLIProxyAPI 使用 systemd 内存与交换空间上限。达到上限时单次请求可能失败，但不会无限挤占整台服务器。
- 公网管理入口阻止写方法、OAuth 凭据下载、队列消费以及 OAuth 发起/回调端点，但授权后的查看响应仍可能包含 Relay Key、配置和完整请求正文。
- 不要公开 `/root/codex-relay-credentials.txt` 或 `/var/lib/cliproxyapi/auth/*.json`。

## 可选参数

```bash
curl -fsSL https://github.com/2004liangle/codex-oauth-relay-deploy/releases/latest/download/install.sh -o /tmp/install.sh && \
  sudo env PUBLIC_HOST=relay.example.com PUBLIC_PORT=8317 TZ=Asia/Shanghai bash /tmp/install.sh
```

查看全部选项：

```bash
curl -fsSL https://github.com/2004liangle/codex-oauth-relay-deploy/releases/latest/download/install.sh -o /tmp/install.sh && \
  sudo bash /tmp/install.sh --help
```

安装中途失败时可直接重跑同一条命令。修复已完成且由本安装器管理的部署时，设置 `REPAIR=1`；原公网地址、端口和时区会从安装状态中恢复。

## 固定组件版本

- CLIProxyAPI `7.2.80`
- CPA Usage Keeper `1.13.2`
- 白话中文用量页面 `1.13.2-plain-zh.2`
- Codex Network Monitor（随本仓库发布并固定 SHA-256）
- CLIProxy API Management Center `1.18.3`

脚本会校验每个下载产物的 SHA-256，不匹配时立即停止。
