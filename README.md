# Codex OAuth Personal Relay Deploy

用于在个人服务器上部署 Codex OAuth 中转、OpenAI-compatible API、用量后台和受限管理查看入口。

> 这不是 OpenAI 官方项目或官方中转服务。脚本会安装并配置第三方社区组件，只建议同一账号持有人在自己的设备之间使用。

## 一键部署

```bash
curl -fL https://github.com/2004liangle/codex-oauth-relay-deploy/releases/download/v1.0.0/install-codex-relay.sh -o /tmp/install-codex-relay.sh && echo 'a1afb9e61311e2cdf5557ea55a86952ca9059a3299b6b840e39a7a127318e43b  /tmp/install-codex-relay.sh' | sha256sum -c - && sudo bash /tmp/install-codex-relay.sh
```

安装过程中，终端会显示 Codex 设备授权网址和代码。可以在另一台带浏览器的电脑上完成授权。

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

## 安全边界

- GitHub 安装脚本通过 HTTPS 下载，并使用 README 中固定的 SHA-256 再校验。
- 部署后的 Nginx 入口默认使用 HTTP。开放端口前，应把云安全组来源限制为自己的客户端 IP；来源不固定时先配置 HTTPS。
- CLIProxyAPI、Squid 和 Usage Keeper 的内部端口不得直接开放到公网。
- 公网管理入口阻止写方法、OAuth 凭据下载、队列消费以及 OAuth 发起/回调端点，但授权后的查看响应仍可能包含 Relay Key、配置和完整请求正文。
- 不要公开 `/root/codex-relay-credentials.txt` 或 `/var/lib/cliproxyapi/auth/*.json`。

## 可选参数

```bash
sudo PUBLIC_HOST=relay.example.com PUBLIC_PORT=8317 TZ=Asia/Shanghai \
  bash /tmp/install-codex-relay.sh
```

查看全部选项：

```bash
bash /tmp/install-codex-relay.sh --help
```

安装中途失败时可直接重跑同一条命令。修复已完成且由本安装器管理的部署时，设置 `REPAIR=1`；原公网地址、端口和时区会从安装状态中恢复。

## 固定组件版本

- CLIProxyAPI `7.2.80`
- CPA Usage Keeper `1.13.2`
- CLIProxy API Management Center `1.18.3`

脚本会校验每个下载产物的 SHA-256，不匹配时立即停止。
