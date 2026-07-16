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
- 公网端口默认 `8317`；内部端口 `18080`、`18081`、`18317` 不得占用

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

## Relay Images Skill

在需要调用中转的 Codex 客户端上一键安装：

```bash
curl -fsSL https://github.com/2004liangle/codex-oauth-relay-deploy/releases/latest/download/install-relay-images-skill.sh -o /tmp/install-relay-images-skill.sh && bash /tmp/install-relay-images-skill.sh
```

首次配置会交互式读取 Relay Key，并以 `0600` 权限保存在客户端：

```bash
~/.codex/skills/relay-images/scripts/relay_images.py configure \
  --base-url "$API_BASE_URL" --allow-http
```

重启 Codex 或新开会话后，可以直接要求 `$relay-images` 文生图、图生图、多图合成或遮罩编辑。也可以直接运行脚本：

```bash
# 低质量草稿
~/.codex/skills/relay-images/scripts/relay_images.py generate \
  --prompt 'A clean product photo of a white ceramic cup' \
  --quality low --size 1024x1024 --output draft.png

# 高质量图生图
~/.codex/skills/relay-images/scripts/relay_images.py edit \
  --image source.png --prompt 'Keep the subject and replace the background with snow mountains' \
  --quality high --size 2048x2048 --output final.png
```

`quality` 支持 `low`、`medium`、`high` 和 `auto`；输出支持 PNG、JPEG、WebP 和压缩控制。脚本不会打印 Key 或图片 Base64，但服务器 Request Log 仍可能保存提示词、输入图和输出图。

需要严格尺寸或格式时加 `--strict-output`。中转若返回了不同尺寸/格式，脚本仍会安全保存已生成文件，但将 `output_contract_met` 标记为 `false` 并以非零状态退出，避免自动化误判成功。

## 安全边界

- GitHub 引导脚本通过 HTTPS 下载，并在内部使用固定 SHA-256 校验完整安装器。
- 部署后的 Nginx 入口默认使用 HTTP。开放端口前，应把云安全组来源限制为自己的客户端 IP；来源不固定时先配置 HTTPS。
- CLIProxyAPI、Squid 和 Usage Keeper 的内部端口不得直接开放到公网。
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
- CLIProxy API Management Center `1.18.3`

脚本会校验每个下载产物的 SHA-256，不匹配时立即停止。
